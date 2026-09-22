"""Cursor dashboard usage-events CSV: fetch, store, and billing-period totals.

Downloads via the logged-in automation browser (`fetch` with cookies), saves
under the app data dir, and associates scraped AUTO/API % with the current
period's token total going forward.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from datetime import time as time_of_day
from pathlib import Path
from typing import Any, Protocol

from .billing import (
    history_start_for,
    period_end_for,
    period_label,
    period_ms_range,
    period_start_for,
    renewal_instant,
    short_period_label,
)
from .config import data_dir

log = logging.getLogger(__name__)

EXPORT_URL = "https://cursor.com/api/dashboard/export-usage-events-csv"
CACHE_VERSION = 2


def _renewal_time_key(renewal_time: time_of_day | None) -> str:
    tod = renewal_time or time_of_day(0, 0)
    return tod.strftime("%H:%M:%S")


class _EvalClient(Protocol):
    async def evaluate(self, handle: str, expression: str) -> Any: ...


@dataclass(frozen=True)
class UsagePeriodBucket:
    """Token totals for one subscription billing period."""

    label: str
    period_start: str  # ISO date
    total_tokens: int = 0
    event_count: int = 0
    cursor_models_pct: int | None = None
    other_models_pct: int | None = None


@dataclass(frozen=True)
class UsageCsvPreview:
    available: bool
    periods: tuple[UsagePeriodBucket, ...] = field(default_factory=tuple)
    indexes_scanned: int = 0
    total_tokens: int = 0
    earliest: str = ""
    latest: str = ""
    insight: str = ""
    error_message: str = ""
    csv_dir: str = ""

    @staticmethod
    def unavailable(message: str) -> UsageCsvPreview:
        return UsageCsvPreview(available=False, error_message=message)


def usage_csv_dir() -> Path:
    path = data_dir() / "usage-csv"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _totals_path() -> Path:
    return usage_csv_dir() / "period-totals.json"


def invalidate_usage_totals_cache() -> None:
    """Drop period-totals.json so the next preview rebuilds from CSVs."""
    path = _totals_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("Could not remove period totals cache: %s", exc)


def _assoc_path() -> Path:
    return usage_csv_dir() / "period-spend-pct.json"


def _period_csv_path(start: date, end: date) -> Path:
    return usage_csv_dir() / f"{start.isoformat()}_to_{end.isoformat()}.csv"


def _period_ms_range(
    start: date,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> tuple[int, int]:
    return period_ms_range(
        start, renewal_day=renewal_day, renewal_time=renewal_time
    )


def _iter_period_starts(
    *,
    history_start: date | None = None,
    until: datetime | date | None = None,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> list[date]:
    """Billing-period starts from history_start through the period containing `until`."""
    end_at = until or datetime.now(timezone.utc).astimezone()
    last_start = period_start_for(
        end_at, renewal_day=renewal_day, renewal_time=renewal_time
    )
    # history_start is a period-start calendar day; anchor at the renewal clock
    # so a bare date is not treated as local midnight (which falls in the
    # previous period when renewal_time is non-zero).
    start_day = history_start or history_start_for(renewal_day=renewal_day)
    first = period_start_for(
        renewal_instant(
            start_day.year,
            start_day.month,
            renewal_day=renewal_day,
            renewal_time=renewal_time,
        ),
        renewal_day=renewal_day,
        renewal_time=renewal_time,
    )
    out: list[date] = []
    cur = first
    day = max(1, min(28, renewal_day))
    while cur <= last_start:
        out.append(cur)
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, cur.day)
        else:
            nxt = date(cur.year, cur.month + 1, cur.day)
        cur = date(nxt.year, nxt.month, day)
    return out


def _parse_int(value: str | None) -> int:
    if value is None:
        return 0
    text = value.strip().replace(",", "").replace('"', "")
    if not text or text.lower() in {"included", "free", "-", "n/a"}:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def parse_usage_csv(text: str) -> list[dict[str, Any]]:
    """Parse export CSV rows; resolve columns by header name."""
    if not text or not text.strip():
        return []
    # Strip UTF-8 BOM if present
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        return []
    headers = [h.strip().strip('"') for h in headers]
    idx = {name: i for i, name in enumerate(headers)}

    date_i = idx.get("Date")
    total_i = idx.get("Total Tokens")
    if date_i is None or total_i is None:
        log.warning("Usage CSV missing Date/Total Tokens columns: %s", headers[:12])
        return []

    model_i = idx.get("Model")
    kind_i = idx.get("Kind")
    rows: list[dict[str, Any]] = []
    for fields in reader:
        if not fields or len(fields) <= max(date_i, total_i):
            continue
        date_raw = fields[date_i].strip().strip('"')
        if not date_raw:
            continue
        rows.append(
            {
                "date": date_raw,
                "total_tokens": _parse_int(fields[total_i]),
                "model": fields[model_i].strip().strip('"') if model_i is not None and model_i < len(fields) else "",
                "kind": fields[kind_i].strip().strip('"') if kind_i is not None and kind_i < len(fields) else "",
            }
        )
    return rows


def _row_period(
    date_raw: str,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> date | None:
    text = date_raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Fallback: date-only
        try:
            d = date.fromisoformat(text[:10])
        except ValueError:
            return None
        return period_start_for(
            d, renewal_day=renewal_day, renewal_time=renewal_time
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return period_start_for(
        dt, renewal_day=renewal_day, renewal_time=renewal_time
    )


def sum_tokens_by_period(
    rows: list[dict[str, Any]],
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> dict[date, tuple[int, int]]:
    """Map period_start → (total_tokens, event_count)."""
    totals: dict[date, list[int]] = {}
    for row in rows:
        start = _row_period(
            str(row.get("date") or ""),
            renewal_day=renewal_day,
            renewal_time=renewal_time,
        )
        if start is None:
            continue
        bucket = totals.setdefault(start, [0, 0])
        bucket[0] += int(row.get("total_tokens") or 0)
        bucket[1] += 1
    return {k: (v[0], v[1]) for k, v in totals.items()}


def _load_assoc() -> dict[str, dict[str, Any]]:
    path = _assoc_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _save_assoc(data: dict[str, dict[str, Any]]) -> None:
    usage_csv_dir()
    _assoc_path().write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def associate_spend_pct(
    *,
    cursor_models_pct: int | None,
    other_models_pct: int | None,
    total_tokens: int | None = None,
    when: datetime | date | None = None,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> None:
    """Record scraped AUTO/API % against the current billing period (and tokens if known)."""
    if cursor_models_pct is None and other_models_pct is None:
        return
    now = when or datetime.now(timezone.utc).astimezone()
    start = period_start_for(
        now, renewal_day=renewal_day, renewal_time=renewal_time
    )
    key = start.isoformat()
    data = _load_assoc()
    entry = dict(data.get(key) or {})
    if cursor_models_pct is not None:
        entry["cursor_models_pct"] = int(cursor_models_pct)
    if other_models_pct is not None:
        entry["other_models_pct"] = int(other_models_pct)
    if total_tokens is not None:
        entry["total_tokens"] = int(total_tokens)
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    data[key] = entry
    try:
        _save_assoc(data)
    except OSError as exc:
        log.debug("Could not save spend%% association: %s", exc)


def _load_cached_totals(
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> dict[str, dict[str, Any]]:
    path = _totals_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if raw.get("version") != CACHE_VERSION:
        return {}
    if raw.get("renewal_day") != renewal_day:
        return {}
    if raw.get("renewal_time") != _renewal_time_key(renewal_time):
        return {}
    periods = raw.get("periods")
    if not isinstance(periods, dict):
        return {}
    return {str(k): v for k, v in periods.items() if isinstance(v, dict)}


def _save_cached_totals(
    periods: dict[str, dict[str, Any]],
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> None:
    payload = {
        "version": CACHE_VERSION,
        "renewal_day": renewal_day,
        "renewal_time": _renewal_time_key(renewal_time),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "periods": periods,
    }
    usage_csv_dir()
    _totals_path().write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _aggregate_all_csvs(
    *,
    renewal_day: int,
    renewal_time: time_of_day | None,
) -> dict[date, tuple[int, int]]:
    """Sum tokens across every on-disk period CSV with time-aware bucketing."""
    totals: dict[date, list[int]] = {}
    csv_dir = usage_csv_dir()
    for path in sorted(csv_dir.glob("*_to_*.csv")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows = parse_usage_csv(text)
        by_period = sum_tokens_by_period(
            rows, renewal_day=renewal_day, renewal_time=renewal_time
        )
        for start, (tok, cnt) in by_period.items():
            bucket = totals.setdefault(start, [0, 0])
            bucket[0] += tok
            bucket[1] += cnt
    return {k: (v[0], v[1]) for k, v in totals.items()}


FETCH_JS_TEMPLATE = """
(async () => {{
  const url = {url!r};
  try {{
    const res = await fetch(url, {{
      credentials: "include",
      headers: {{ Accept: "text/csv,*/*" }},
    }});
    const text = await res.text();
    return {{
      ok: res.ok,
      status: res.status,
      text,
      contentType: res.headers.get("content-type") || "",
    }};
  }} catch (err) {{
    return {{
      ok: false,
      status: 0,
      text: "",
      error: String(err && err.message ? err.message : err),
    }};
  }}
}})()
"""


async def fetch_period_csv(
    client: _EvalClient,
    handle: str,
    start: date,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> tuple[str, int]:
    """Download one billing period CSV via page-context fetch. Returns (text, http_status)."""
    start_ms, end_ms = _period_ms_range(
        start, renewal_day=renewal_day, renewal_time=renewal_time
    )
    url = f"{EXPORT_URL}?startDate={start_ms}&endDate={end_ms}&strategy=tokens"
    expr = FETCH_JS_TEMPLATE.format(url=url)
    result = await client.evaluate(handle, expr)
    if not isinstance(result, dict):
        return "", 0
    status = int(result.get("status") or 0)
    text = result.get("text") if isinstance(result.get("text"), str) else ""
    if result.get("error"):
        log.debug("Usage CSV fetch error for %s: %s", start, result.get("error"))
    return text, status


async def sync_usage_csvs(
    client: _EvalClient,
    handle: str,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
    force_current: bool = True,
) -> UsageCsvPreview:
    """Ensure period CSVs exist for recent billing periods; refresh current period."""
    starts = _iter_period_starts(
        renewal_day=renewal_day, renewal_time=renewal_time
    )
    if not starts:
        return UsageCsvPreview.unavailable("No billing periods to sync")

    now = datetime.now(timezone.utc).astimezone()
    current_start = period_start_for(
        now, renewal_day=renewal_day, renewal_time=renewal_time
    )
    cached = _load_cached_totals(
        renewal_day=renewal_day, renewal_time=renewal_time
    )
    errors: list[str] = []
    downloaded = 0

    for start in starts:
        end = period_end_for(
            start, renewal_day=renewal_day, renewal_time=renewal_time
        )
        path = _period_csv_path(start, end)
        is_current = start == current_start
        need_fetch = is_current and force_current
        if not need_fetch and path.is_file() and path.stat().st_size > 32:
            continue

        if not need_fetch and start.isoformat() in cached and path.is_file():
            continue

        text, status = await fetch_period_csv(
            client,
            handle,
            start,
            renewal_day=renewal_day,
            renewal_time=renewal_time,
        )
        if status == 401 or status == 403:
            return UsageCsvPreview.unavailable(
                f"Usage CSV export returned HTTP {status} (session may be logged out)"
            )
        if status != 200 or not text:
            # Empty body with 200 can mean no events; still save a stub marker
            if status == 200 and text == "":
                text = "Date,Model,Total Tokens\n"
            else:
                errors.append(f"{start.isoformat()}: HTTP {status}")
                log.warning(
                    "Usage CSV fetch failed for %s: status=%s len=%s",
                    start,
                    status,
                    len(text or ""),
                )
                continue

        # Reject HTML login pages mistaken for CSV
        head = text.lstrip()[:80].lower()
        if head.startswith("<!doctype") or head.startswith("<html"):
            return UsageCsvPreview.unavailable(
                "Usage CSV export returned HTML (sign in required)"
            )

        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            errors.append(f"{start.isoformat()}: write failed")
            log.debug("Could not write %s: %s", path, exc)
            continue

        downloaded += 1
        print(
            f"[usage-csv] fetched {start.isoformat()}→{end.isoformat()} "
            f"file={path.name}",
            flush=True,
        )

    # Always re-bucket every CSV with the renewal clock so spillover across
    # midnight-day files lands in the correct billing period.
    by_period = _aggregate_all_csvs(
        renewal_day=renewal_day, renewal_time=renewal_time
    )
    cached = {}
    for start in starts:
        end = period_end_for(
            start, renewal_day=renewal_day, renewal_time=renewal_time
        )
        path = _period_csv_path(start, end)
        tok, cnt = by_period.get(start, (0, 0))
        if not tok and not cnt and not path.is_file() and start != current_start:
            continue
        cached[start.isoformat()] = {
            "label": period_label(
                start, renewal_day=renewal_day, renewal_time=renewal_time
            ),
            "total_tokens": tok,
            "event_count": cnt,
            "csv": path.name if path.is_file() else "",
            "fetched_at": time.time(),
        }
        if start == current_start:
            print(
                f"[usage-csv] {start.isoformat()}→{end.isoformat()} "
                f"tokens={tok:,} events={cnt}",
                flush=True,
            )

    try:
        _save_cached_totals(
            cached, renewal_day=renewal_day, renewal_time=renewal_time
        )
    except OSError as exc:
        log.debug("Could not save period totals: %s", exc)

    preview = build_usage_preview(
        renewal_day=renewal_day, renewal_time=renewal_time
    )
    if errors and not preview.available:
        return UsageCsvPreview.unavailable("; ".join(errors[:3]))
    if downloaded:
        print(f"[usage-csv] synced {downloaded} period file(s)", flush=True)
    return preview


def build_usage_preview(
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> UsageCsvPreview:
    """Build chart-ready period buckets from on-disk CSVs / totals cache + spend % assoc."""
    assoc = _load_assoc()
    starts = _iter_period_starts(
        renewal_day=renewal_day, renewal_time=renewal_time
    )
    csv_dir = usage_csv_dir()
    by_period = _aggregate_all_csvs(
        renewal_day=renewal_day, renewal_time=renewal_time
    )
    now = datetime.now(timezone.utc).astimezone()
    current_start = period_start_for(
        now, renewal_day=renewal_day, renewal_time=renewal_time
    )

    periods: list[UsagePeriodBucket] = []
    for start in starts:
        key = start.isoformat()
        a = assoc.get(key) or {}
        tok, events = by_period.get(start, (0, 0))
        tokens = int(tok or a.get("total_tokens") or 0)
        cursor_pct = a.get("cursor_models_pct")
        other_pct = a.get("other_models_pct")
        has_file = _period_csv_path(
            start,
            period_end_for(
                start, renewal_day=renewal_day, renewal_time=renewal_time
            ),
        ).is_file()
        is_current = start == current_start
        if not tokens and not events and not a and not has_file and not is_current:
            continue
        periods.append(
            UsagePeriodBucket(
                label=period_label(
                    start, renewal_day=renewal_day, renewal_time=renewal_time
                ),
                period_start=key,
                total_tokens=tokens,
                event_count=events,
                cursor_models_pct=int(cursor_pct) if cursor_pct is not None else None,
                other_models_pct=int(other_pct) if other_pct is not None else None,
            )
        )

    if not periods or not any(
        p.total_tokens or p.event_count or p.cursor_models_pct is not None
        for p in periods
    ):
        return UsageCsvPreview(
            available=False,
            error_message="No usage CSV data yet — refresh while signed in",
            csv_dir=str(csv_dir),
        )

    # Refresh on-disk cache so tray restarts stay aligned with renewal clock.
    try:
        cached = {
            p.period_start: {
                "label": p.label,
                "total_tokens": p.total_tokens,
                "event_count": p.event_count,
            }
            for p in periods
        }
        _save_cached_totals(
            cached, renewal_day=renewal_day, renewal_time=renewal_time
        )
    except OSError as exc:
        log.debug("Could not save period totals: %s", exc)

    total = sum(p.total_tokens for p in periods)
    latest = periods[-1]
    insight_parts = [f"{latest.total_tokens:,} tokens"]
    if latest.cursor_models_pct is not None:
        insight_parts.append(f"AUTO {latest.cursor_models_pct}%")
    if latest.other_models_pct is not None:
        insight_parts.append(f"API {latest.other_models_pct}%")
    insight = f"{latest.label}: " + " · ".join(insight_parts)

    return UsageCsvPreview(
        available=True,
        periods=tuple(periods),
        indexes_scanned=len(list(csv_dir.glob("*.csv"))),
        total_tokens=total,
        earliest=short_period_label(date.fromisoformat(periods[0].period_start)),
        latest=periods[-1].label,
        insight=insight,
        csv_dir=str(csv_dir),
    )


def load_usage_preview(
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> UsageCsvPreview:
    """Read-only preview from disk (no network)."""
    try:
        return build_usage_preview(
            renewal_day=renewal_day, renewal_time=renewal_time
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("usage preview failed: %s", exc)
        return UsageCsvPreview.unavailable(str(exc))
