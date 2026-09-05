"""Longer-range composer/tool signals from Cursor state.vscdb.

Read-only. Complements sdk-agent-store habits with chat/composer history.
Buckets use the subscription renewal day (default 19th), not calendar months.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .billing import period_label, period_start_for
from .config import SUBSCRIPTION_RENEWAL_DAY, data_dir

log = logging.getLogger(__name__)

DEFAULT_VSCDB = (
    Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
)
CACHE_VERSION = 2


@dataclass(frozen=True)
class VscdbPeriodBucket:
    """One subscription billing period of composer/tool signals."""

    label: str
    period_start: str  # ISO date
    composers: int = 0
    completed: int = 0
    aborted: int = 0
    status_none: int = 0
    abort_rate_pct: int | None = None
    agentic: int = 0
    mode_edit: int = 0
    mode_chat: int = 0
    mode_plan: int = 0
    tool_completed: int = 0
    tool_error: int = 0
    tool_cancelled: int = 0
    tool_error_rate_pct: int | None = None
    tab_suggested: int = 0
    tab_accepted: int = 0
    composer_suggested: int = 0
    composer_accepted: int = 0
    accept_rate_pct: int | None = None


# Back-compat alias for older imports
VscdbMonthBucket = VscdbPeriodBucket


@dataclass(frozen=True)
class VscdbHabitsPreview:
    available: bool
    db_path: str = ""
    earliest: str = ""
    latest: str = ""
    composers: int = 0
    periods: tuple[VscdbPeriodBucket, ...] = field(default_factory=tuple)
    insight: str = ""
    error_message: str = ""
    cache_hit: bool = False
    elapsed_ms: int = 0

    @property
    def months(self) -> tuple[VscdbPeriodBucket, ...]:
        """Alias kept for older UI call sites."""
        return self.periods

    @staticmethod
    def unavailable(message: str) -> VscdbHabitsPreview:
        return VscdbHabitsPreview(available=False, error_message=message)


def _cache_path() -> Path:
    return data_dir() / "vscdb-habits-cache.json"


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    uri = f"file:{path}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        log.debug("Could not open %s: %s", path, exc)
        return None


def _loads(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = value.decode("utf-8", "ignore")
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _period_from_ms(
    ms: int | float | None,
    *,
    renewal_day: int,
) -> date | None:
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return period_start_for(dt, renewal_day=renewal_day)


def _period_from_iso(value: str | None, *, renewal_day: int) -> date | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return period_start_for(dt, renewal_day=renewal_day)


def _period_from_day_key(day_key: str, *, renewal_day: int) -> date | None:
    """Parse YYYY-MM-DD from aiCodeTracking keys."""
    try:
        d = date.fromisoformat(day_key[:10])
    except ValueError:
        return None
    return period_start_for(d, renewal_day=renewal_day)


def _pct(num: int, den: int) -> int | None:
    if den <= 0:
        return None
    return round(100 * num / den)


def _insight(periods: list[VscdbPeriodBucket]) -> str:
    if not periods:
        return ""
    latest = periods[-1]
    parts: list[str] = []
    if latest.abort_rate_pct is not None:
        parts.append(f"abort {latest.abort_rate_pct}%")
    if latest.tool_error_rate_pct is not None:
        parts.append(f"tool err {latest.tool_error_rate_pct}%")
    if latest.accept_rate_pct is not None:
        parts.append(f"accept {latest.accept_rate_pct}%")
    if not parts:
        return f"{latest.composers} composers in {latest.label}"
    return f"{latest.label}: " + " · ".join(parts)


def _load_cache(db_path: Path) -> VscdbHabitsPreview | None:
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != CACHE_VERSION:
        return None
    if payload.get("renewal_day") != SUBSCRIPTION_RENEWAL_DAY:
        return None
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        return None
    if payload.get("db_mtime_ns") != mtime_ns:
        return None
    try:
        raw_periods = payload.get("periods") or payload.get("months") or []
        periods = tuple(VscdbPeriodBucket(**m) for m in raw_periods)
        return VscdbHabitsPreview(
            available=True,
            db_path=str(db_path),
            earliest=payload.get("earliest", ""),
            latest=payload.get("latest", ""),
            composers=int(payload.get("composers", 0)),
            periods=periods,
            insight=payload.get("insight", ""),
            cache_hit=True,
            elapsed_ms=0,
        )
    except (TypeError, ValueError, KeyError):
        return None


def _save_cache(db_path: Path, preview: VscdbHabitsPreview) -> None:
    try:
        data_dir().mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "renewal_day": SUBSCRIPTION_RENEWAL_DAY,
            "db_mtime_ns": db_path.stat().st_mtime_ns,
            "earliest": preview.earliest,
            "latest": preview.latest,
            "composers": preview.composers,
            "insight": preview.insight,
            "periods": [p.__dict__ for p in preview.periods],
        }
        _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.debug("Could not write vscdb cache: %s", exc)


def collect_vscdb_habits_preview(
    *,
    db_path: Path | None = None,
    use_cache: bool = True,
    renewal_day: int = SUBSCRIPTION_RENEWAL_DAY,
) -> VscdbHabitsPreview:
    """Aggregate billing-period composer/tool signals from state.vscdb."""
    t0 = time.perf_counter()
    path = (db_path or DEFAULT_VSCDB).expanduser()
    try:
        path = path.resolve()
    except OSError:
        return VscdbHabitsPreview.unavailable("Cursor state.vscdb path unavailable")
    if not path.is_file():
        return VscdbHabitsPreview.unavailable("No state.vscdb found")

    if use_cache and renewal_day == SUBSCRIPTION_RENEWAL_DAY:
        cached = _load_cache(path)
        if cached is not None:
            return cached

    con = _connect_ro(path)
    if con is None:
        return VscdbHabitsPreview.unavailable("Could not open state.vscdb (locked?)")

    composer_period: dict[str, date] = {}
    try:
        for composer_id, created_at in con.execute(
            "SELECT composerId, createdAt FROM composerHeaders WHERE composerId IS NOT NULL"
        ):
            start = _period_from_ms(created_at, renewal_day=renewal_day)
            if composer_id and start is not None:
                composer_period[str(composer_id)] = start
    except sqlite3.Error as exc:
        log.debug("composerHeaders query failed: %s", exc)

    buckets: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    try:
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        )
        for key, value in rows:
            obj = _loads(value)
            if not isinstance(obj, dict):
                continue
            composer_id = str(
                obj.get("composerId") or str(key).removeprefix("composerData:")
            )
            start = composer_period.get(composer_id)
            if start is None:
                created = obj.get("createdAt")
                start = _period_from_iso(
                    created if isinstance(created, str) else None,
                    renewal_day=renewal_day,
                )
            if start is None:
                continue
            composer_period.setdefault(composer_id, start)
            b = buckets[start]
            status = str(obj.get("status") or "none")
            if status == "completed":
                b["completed"] += 1
            elif status == "aborted":
                b["aborted"] += 1
            else:
                b["status_none"] += 1
            if obj.get("isAgentic"):
                b["agentic"] += 1
            mode = str(obj.get("forceMode") or "")
            if mode == "edit":
                b["mode_edit"] += 1
            elif mode == "chat":
                b["mode_chat"] += 1
            elif mode == "plan":
                b["mode_plan"] += 1
    except sqlite3.Error as exc:
        log.debug("composerData scan failed: %s", exc)

    header_volume: Counter[date] = Counter(composer_period.values())
    for start, count in header_volume.items():
        buckets[start]["composers"] = count

    try:
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        )
        for key, value in rows:
            parts = str(key).split(":", 2)
            if len(parts) < 3:
                continue
            start = composer_period.get(parts[1])
            if start is None:
                continue
            obj = _loads(value)
            if not isinstance(obj, dict):
                continue
            tfd = obj.get("toolFormerData")
            if not isinstance(tfd, dict):
                continue
            status = str(tfd.get("status") or "")
            b = buckets[start]
            if status == "completed":
                b["tool_completed"] += 1
            elif status == "error":
                b["tool_error"] += 1
            elif status == "cancelled":
                b["tool_cancelled"] += 1
    except sqlite3.Error as exc:
        log.debug("bubble tool scan failed: %s", exc)

    try:
        rows = con.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE 'aiCodeTracking.dailyStats%'"
        )
        for key, value in rows:
            day_key = str(key).rsplit(".", 1)[-1]
            start = _period_from_day_key(day_key, renewal_day=renewal_day)
            if start is None:
                continue
            obj = _loads(value)
            if not isinstance(obj, dict):
                continue
            b = buckets[start]
            b["tab_suggested"] += int(obj.get("tabSuggestedLines") or 0)
            b["tab_accepted"] += int(obj.get("tabAcceptedLines") or 0)
            b["composer_suggested"] += int(obj.get("composerSuggestedLines") or 0)
            b["composer_accepted"] += int(obj.get("composerAcceptedLines") or 0)
    except sqlite3.Error as exc:
        log.debug("aiCodeTracking scan failed: %s", exc)

    con.close()

    periods: list[VscdbPeriodBucket] = []
    for start in sorted(buckets):
        b = buckets[start]
        completed = b["completed"]
        aborted = b["aborted"]
        tool_done = b["tool_completed"]
        tool_err = b["tool_error"]
        tool_cancel = b["tool_cancelled"]
        suggested = b["composer_suggested"]
        accepted = b["composer_accepted"]
        accept = _pct(accepted, suggested)
        if accept is not None:
            accept = min(accept, 100)
        periods.append(
            VscdbPeriodBucket(
                label=period_label(start, renewal_day=renewal_day),
                period_start=start.isoformat(),
                composers=b["composers"] or header_volume.get(start, 0),
                completed=completed,
                aborted=aborted,
                status_none=b["status_none"],
                abort_rate_pct=_pct(aborted, completed + aborted),
                agentic=b["agentic"],
                mode_edit=b["mode_edit"],
                mode_chat=b["mode_chat"],
                mode_plan=b["mode_plan"],
                tool_completed=tool_done,
                tool_error=tool_err,
                tool_cancelled=tool_cancel,
                tool_error_rate_pct=_pct(
                    tool_err, tool_done + tool_err + tool_cancel
                ),
                tab_suggested=b["tab_suggested"],
                tab_accepted=b["tab_accepted"],
                composer_suggested=suggested,
                composer_accepted=accepted,
                accept_rate_pct=accept,
            )
        )

    if not periods:
        return VscdbHabitsPreview.unavailable("No composer history in state.vscdb")

    preview = VscdbHabitsPreview(
        available=True,
        db_path=str(path),
        earliest=periods[0].label,
        latest=periods[-1].label,
        composers=sum(p.composers for p in periods),
        periods=tuple(periods),
        insight=_insight(periods),
        cache_hit=False,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )
    if renewal_day == SUBSCRIPTION_RENEWAL_DAY:
        _save_cache(path, preview)
    return preview
