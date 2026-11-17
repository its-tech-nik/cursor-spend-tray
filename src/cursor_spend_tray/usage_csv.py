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
from datetime import date, datetime, timedelta, timezone
from datetime import time as time_of_day
from pathlib import Path
from typing import Any, Protocol

from .billing import (
    history_start_for,
    period_end_for,
    period_label,
    period_ms_range,
    period_range_label,
    period_start_for,
    renewal_instant,
    short_period_label,
)
from .config import UsageSnapshot, data_dir, renewal_from_usage_reset
from .grok_weekly import average_weekly_peaks_for_period, load_grok_week_peaks

log = logging.getLogger(__name__)

EXPORT_URL = "https://cursor.com/api/dashboard/export-usage-events-csv"
CACHE_VERSION = 3
# Soft cap on year-axis points once legacy + current eras are concatenated.
_YEAR_PERIOD_CAP = 14


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
    grok_bot_pct: int | None = None
    # "legacy" = previous renewal-day family; "current" = active subscription.
    era: str = "current"
    renewal_day: int = 0
    # HH:MM:SS for month-view bucketing; empty → midnight.
    renewal_time: str = ""
    # Inclusive label/axis end (ISO); empty → natural period_end_for.
    period_end: str = ""


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
    # When set, year chart splits tokens into legacy vs current series here.
    change_date: str = ""
    previous_renewal_day: int | None = None

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


def _local_tz():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_row_datetime(date_raw: str) -> datetime | None:
    """Parse a usage-CSV Date cell to a timezone-aware local datetime."""
    text = date_raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            d = date.fromisoformat(text[:10])
        except ValueError:
            return None
        return datetime(d.year, d.month, d.day, tzinfo=_local_tz())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_local_tz())


def _row_period(
    date_raw: str,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> date | None:
    dt = _parse_row_datetime(date_raw)
    if dt is None:
        return None
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


def sum_tokens_by_day(
    rows: list[dict[str, Any]],
    *,
    period_start: date,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> dict[date, tuple[int, int]]:
    """Map local calendar day → (total_tokens, event_count) inside one period."""
    totals: dict[date, list[int]] = {}
    for row in rows:
        dt = _parse_row_datetime(str(row.get("date") or ""))
        if dt is None:
            continue
        start = period_start_for(
            dt, renewal_day=renewal_day, renewal_time=renewal_time
        )
        if start != period_start:
            continue
        day = dt.date()
        bucket = totals.setdefault(day, [0, 0])
        bucket[0] += int(row.get("total_tokens") or 0)
        bucket[1] += 1
    return {k: (v[0], v[1]) for k, v in totals.items()}


@dataclass(frozen=True)
class UsageDailySeries:
    """Day-by-day tokens for one billing period (reset → reset)."""

    period_start: str
    label: str
    days: tuple[str, ...] = ()  # axis labels, e.g. 'Oct 19'
    daily_tokens: tuple[float, ...] = ()
    cumulative_tokens: tuple[float, ...] = ()
    total_tokens: int = 0
    event_count: int = 0
    error_message: str = ""

    @property
    def available(self) -> bool:
        return len(self.days) >= 2 and not self.error_message

    @staticmethod
    def unavailable(
        message: str, *, period_start: str = "", label: str = ""
    ) -> UsageDailySeries:
        return UsageDailySeries(
            period_start=period_start,
            label=label,
            error_message=message,
        )


def _row_dedupe_key(row: dict[str, Any]) -> str:
    return (
        f"{row.get('date')}|{row.get('total_tokens')}|{row.get('model')}|"
        f"{row.get('kind')}"
    )


def _iter_usage_rows_deduped() -> list[dict[str, Any]]:
    """All on-disk CSV rows, de-duplicated across overlapping period files."""
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for path in sorted(usage_csv_dir().glob("*_to_*.csv")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for row in parse_usage_csv(text):
            key = _row_dedupe_key(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def load_period_daily_series(
    period_start: date | str,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
    as_of: datetime | date | None = None,
    inclusive_end: date | str | None = None,
) -> UsageDailySeries:
    """Build contiguous daily + cumulative token series for one billing period."""
    if isinstance(period_start, str):
        try:
            start = date.fromisoformat(period_start)
        except ValueError:
            return UsageDailySeries.unavailable(
                f"Invalid period start: {period_start}"
            )
    else:
        start = period_start

    natural_end = period_end_for(
        start, renewal_day=renewal_day, renewal_time=renewal_time
    )
    end = natural_end
    if inclusive_end is not None:
        if isinstance(inclusive_end, str):
            try:
                end = date.fromisoformat(inclusive_end)
            except ValueError:
                end = natural_end
        else:
            end = inclusive_end
        if end > natural_end:
            end = natural_end
    label = period_range_label(start, end)
    if isinstance(as_of, datetime):
        now = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        now = now.astimezone(_local_tz())
    elif isinstance(as_of, date):
        now = datetime(as_of.year, as_of.month, as_of.day, tzinfo=_local_tz())
    else:
        now = datetime.now(timezone.utc).astimezone()
    current_start = period_start_for(
        now, renewal_day=renewal_day, renewal_time=renewal_time
    )
    axis_end = min(end, now.date()) if start == current_start else end
    if axis_end < start:
        return UsageDailySeries.unavailable(
            "Period has not started yet", period_start=start.isoformat(), label=label
        )

    by_day = sum_tokens_by_day(
        _iter_usage_rows_deduped(),
        period_start=start,
        renewal_day=renewal_day,
        renewal_time=renewal_time,
    )

    days: list[str] = []
    daily: list[float] = []
    cumulative: list[float] = []
    running = 0
    events = 0
    cursor = start
    while cursor <= axis_end:
        tok, cnt = by_day.get(cursor, (0, 0))
        running += tok
        events += cnt
        days.append(short_period_label(cursor))
        daily.append(float(tok))
        cumulative.append(float(running))
        cursor += timedelta(days=1)

    if len(days) < 2:
        return UsageDailySeries.unavailable(
            "Not enough days in this period yet",
            period_start=start.isoformat(),
            label=label,
        )

    return UsageDailySeries(
        period_start=start.isoformat(),
        label=label,
        days=tuple(days),
        daily_tokens=tuple(daily),
        cumulative_tokens=tuple(cumulative),
        total_tokens=running,
        event_count=events,
    )

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
    """Record scraped AUTO/API % against the current billing period (and tokens if known).

    Grok Bot is weekly — see ``grok_weekly.record_grok_sample``; period chart
    values are day-weighted averages of weekly peaks.
    """
    if cursor_models_pct is None and other_models_pct is None and total_tokens is None:
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


def _parse_period_csv_name(path: Path) -> tuple[date, date] | None:
    """Parse ``YYYY-MM-DD_to_YYYY-MM-DD.csv`` → (start, end)."""
    stem = path.stem
    if "_to_" not in stem:
        return None
    left, right = stem.split("_to_", 1)
    try:
        return date.fromisoformat(left), date.fromisoformat(right)
    except ValueError:
        return None


def discover_csv_families() -> dict[int, list[tuple[date, date, Path]]]:
    """Group on-disk period CSVs by ``period_start.day`` (renewal-day family)."""
    families: dict[int, list[tuple[date, date, Path]]] = {}
    for path in sorted(usage_csv_dir().glob("*_to_*.csv")):
        parsed = _parse_period_csv_name(path)
        if parsed is None:
            continue
        start, end = parsed
        families.setdefault(start.day, []).append((start, end, path))
    for day in families:
        families[day].sort(key=lambda item: item[0])
    return families


def infer_subscription_eras(
    *,
    active_day: int,
    snap: UsageSnapshot | None = None,
) -> tuple[int | None, date | None, time_of_day | None]:
    """Return ``(previous_renewal_day, change_date, previous_renewal_time)``.

    Previous family prefers the manual reset day when it differs from the
    active boundary and still has CSV files; otherwise the largest other
    family. Change date prefers ``renewal_boundary_history``, then the auto
    stamp date, then the earliest mtime of active-family files.
    """
    snap = snap or UsageSnapshot.load()
    families = discover_csv_families()
    active_day = max(1, min(28, int(active_day)))
    prev_day: int | None = None
    prev_time: time_of_day | None = None

    if snap.usage_reset_at_manual is not None:
        manual_day, manual_time = renewal_from_usage_reset(snap.usage_reset_at_manual)
        if manual_day != active_day and manual_day in families:
            prev_day = manual_day
            prev_time = manual_time

    if prev_day is None:
        others = [
            (day, len(files))
            for day, files in families.items()
            if day != active_day
        ]
        if others:
            prev_day = max(others, key=lambda item: (item[1], item[0]))[0]

    if prev_day is None:
        return None, None, None

    change_date: date | None = None
    for entry in reversed(snap.renewal_boundary_history):
        if entry.day == active_day:
            change_date = datetime.fromtimestamp(entry.since).astimezone().date()
            break

    if change_date is None and snap.usage_reset_at_auto is not None:
        auto_day, _ = renewal_from_usage_reset(snap.usage_reset_at_auto)
        if auto_day == active_day:
            change_date = (
                datetime.fromtimestamp(snap.usage_reset_at_auto).astimezone().date()
            )

    if change_date is None and active_day in families:
        mtimes = [path.stat().st_mtime for _, _, path in families[active_day]]
        if mtimes:
            change_date = datetime.fromtimestamp(min(mtimes)).astimezone().date()

    return prev_day, change_date, prev_time


def _aggregate_family_csvs(
    *,
    renewal_day: int,
    renewal_time: time_of_day | None,
) -> dict[date, tuple[int, int]]:
    """Sum tokens from CSVs whose filename period-start day matches ``renewal_day``.

    Overlapping families (e.g. day-19 and day-14 exports of the same events)
    are intentionally not mixed — that was double-counting year totals.
    """
    totals: dict[date, list[int]] = {}
    day = max(1, min(28, int(renewal_day)))
    for path in sorted(usage_csv_dir().glob("*_to_*.csv")):
        parsed = _parse_period_csv_name(path)
        if parsed is None or parsed[0].day != day:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows = parse_usage_csv(text)
        by_period = sum_tokens_by_period(
            rows, renewal_day=day, renewal_time=renewal_time
        )
        for start, (tok, cnt) in by_period.items():
            bucket = totals.setdefault(start, [0, 0])
            bucket[0] += tok
            bucket[1] += cnt
    return {k: (v[0], v[1]) for k, v in totals.items()}


def _aggregate_all_csvs(
    *,
    renewal_day: int,
    renewal_time: time_of_day | None,
) -> dict[date, tuple[int, int]]:
    """Sum tokens for the active renewal-day family only (no cross-family mix)."""
    return _aggregate_family_csvs(
        renewal_day=renewal_day, renewal_time=renewal_time
    )


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


def _bucket_for_start(
    start: date,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None,
    by_period: dict[date, tuple[int, int]],
    assoc: dict[str, dict[str, Any]],
    grok_weeks: Any,
    era: str,
    current_start: date | None,
    label_end: date | None = None,
) -> UsagePeriodBucket | None:
    key = start.isoformat()
    a = assoc.get(key) or {}
    tok, events = by_period.get(start, (0, 0))
    tokens = int(tok or a.get("total_tokens") or 0)
    cursor_pct = a.get("cursor_models_pct")
    other_pct = a.get("other_models_pct")
    grok_pct = average_weekly_peaks_for_period(
        start,
        renewal_day=renewal_day,
        renewal_time=renewal_time,
        weeks=grok_weeks,
    )
    if grok_pct is None and a.get("grok_bot_pct") is not None:
        try:
            grok_pct = int(a["grok_bot_pct"])
        except (TypeError, ValueError):
            grok_pct = None
    natural_end = period_end_for(
        start, renewal_day=renewal_day, renewal_time=renewal_time
    )
    end = label_end if label_end is not None else natural_end
    if end < start:
        end = start
    has_file = _period_csv_path(start, natural_end).is_file()
    is_current = current_start is not None and start == current_start
    if not tokens and not events and not a and not has_file and not is_current:
        if grok_pct is None:
            return None
    tod = renewal_time or time_of_day(0, 0)
    label = (
        period_range_label(start, end)
        if label_end is not None
        else period_label(
            start, renewal_day=renewal_day, renewal_time=renewal_time
        )
    )
    return UsagePeriodBucket(
        label=label,
        period_start=key,
        total_tokens=tokens,
        event_count=events,
        cursor_models_pct=int(cursor_pct) if cursor_pct is not None else None,
        other_models_pct=int(other_pct) if other_pct is not None else None,
        grok_bot_pct=grok_pct,
        era=era,
        renewal_day=renewal_day,
        renewal_time=tod.strftime("%H:%M:%S"),
        period_end=end.isoformat(),
    )


def build_usage_preview(
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
) -> UsageCsvPreview:
    """Build chart-ready period buckets from on-disk CSVs + spend % assoc.

    When a previous renewal-day CSV family exists, periods before the
    subscription change keep that family's shape (so AUTO/API keys still
    join); periods on/after the change use the active renewal day.
    """
    assoc = _load_assoc()
    csv_dir = usage_csv_dir()
    snap = UsageSnapshot.load()
    now = datetime.now(timezone.utc).astimezone()
    current_start = period_start_for(
        now, renewal_day=renewal_day, renewal_time=renewal_time
    )
    grok_weeks = load_grok_week_peaks()

    prev_day, change_date, prev_time = infer_subscription_eras(
        active_day=renewal_day, snap=snap
    )
    periods: list[UsagePeriodBucket] = []

    if prev_day is not None and change_date is not None:
        legacy_by = _aggregate_family_csvs(
            renewal_day=prev_day, renewal_time=prev_time
        )
        # Prefer real CSV period starts for the prior family (exact spend-% keys).
        # Include the in-progress period at the change (start < change_date) so
        # spend-% for that window is not dropped when end straddles the cut.
        legacy_starts = sorted(
            {
                start
                for start, _end, _path in discover_csv_families().get(prev_day, [])
                if start < change_date
            }
            | {start for start in legacy_by if start < change_date}
        )
        for start in legacy_starts:
            natural_end = period_end_for(
                start, renewal_day=prev_day, renewal_time=prev_time
            )
            # Truncate the in-progress cycle at the plan-change day so the
            # overlay end matches the new cycle's start (same calendar day).
            label_end = (
                change_date if natural_end >= change_date else None
            )
            bucket = _bucket_for_start(
                start,
                renewal_day=prev_day,
                renewal_time=prev_time,
                by_period=legacy_by,
                assoc=assoc,
                grok_weeks=grok_weeks,
                era="legacy",
                current_start=None,
                label_end=label_end,
            )
            if bucket is not None:
                periods.append(bucket)

        current_by = _aggregate_family_csvs(
            renewal_day=renewal_day, renewal_time=renewal_time
        )
        current_starts = [
            start
            for start in _iter_period_starts(
                history_start=change_date,
                renewal_day=renewal_day,
                renewal_time=renewal_time,
            )
            if start >= change_date
        ]
        # Also include any post-change family files not covered by the iterator.
        for start, _end, _path in discover_csv_families().get(renewal_day, []):
            if start >= change_date and start not in current_starts:
                current_starts.append(start)
        current_starts = sorted(set(current_starts))
        for start in current_starts:
            bucket = _bucket_for_start(
                start,
                renewal_day=renewal_day,
                renewal_time=renewal_time,
                by_period=current_by,
                assoc=assoc,
                grok_weeks=grok_weeks,
                era="current",
                current_start=current_start,
            )
            if bucket is not None:
                periods.append(bucket)

        # Keep the year axis readable: drop oldest legacy points if needed.
        if len(periods) > _YEAR_PERIOD_CAP:
            overflow = len(periods) - _YEAR_PERIOD_CAP
            legacy_count = sum(1 for p in periods if p.era == "legacy")
            drop = min(overflow, legacy_count)
            if drop:
                trimmed: list[UsagePeriodBucket] = []
                skip = drop
                for p in periods:
                    if p.era == "legacy" and skip > 0:
                        skip -= 1
                        continue
                    trimmed.append(p)
                periods = trimmed
    else:
        starts = _iter_period_starts(
            renewal_day=renewal_day, renewal_time=renewal_time
        )
        by_period = _aggregate_family_csvs(
            renewal_day=renewal_day, renewal_time=renewal_time
        )
        for start in starts:
            bucket = _bucket_for_start(
                start,
                renewal_day=renewal_day,
                renewal_time=renewal_time,
                by_period=by_period,
                assoc=assoc,
                grok_weeks=grok_weeks,
                era="current",
                current_start=current_start,
            )
            if bucket is not None:
                periods.append(bucket)

    if not periods or not any(
        p.total_tokens
        or p.event_count
        or p.cursor_models_pct is not None
        or p.other_models_pct is not None
        or p.grok_bot_pct is not None
        for p in periods
    ):
        return UsageCsvPreview(
            available=False,
            error_message="No usage CSV data yet — refresh while signed in",
            csv_dir=str(csv_dir),
        )

    # Refresh on-disk cache for the *active* family (sync / skip-fetch helper).
    try:
        active_cached = {
            p.period_start: {
                "label": p.label,
                "total_tokens": p.total_tokens,
                "event_count": p.event_count,
                "era": p.era,
            }
            for p in periods
            if p.era == "current" and p.renewal_day == renewal_day
        }
        _save_cached_totals(
            active_cached, renewal_day=renewal_day, renewal_time=renewal_time
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
    if latest.grok_bot_pct is not None:
        insight_parts.append(f"Grok {latest.grok_bot_pct}%")
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
        change_date=change_date.isoformat() if change_date else "",
        previous_renewal_day=prev_day,
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
