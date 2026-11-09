"""Grok Bot weekly-allowance samples → billing-period averages.

Grok renews weekly; the Habits year chart is billing-period (≈monthly).
Each scrape updates the peak % for the current Grok week. Period points are
the day-weighted average of those weekly peaks. Weeks that straddle two
billing periods contribute to each side in proportion to overlap days.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as time_of_day
from pathlib import Path
from typing import Any

from .billing import period_end_instant, period_start_instant
from .config import data_dir

log = logging.getLogger(__name__)

_WEEK_DAYS = 7
_CACHE_VERSION = 1


@dataclass(frozen=True)
class GrokWeekPeak:
    week_start: date
    week_end: date  # exclusive; Cursor reset day
    peak_pct: int


def _grok_path() -> Path:
    path = data_dir() / "usage-csv"
    path.mkdir(parents=True, exist_ok=True)
    return path / "grok-weekly.json"


def parse_grok_week_end(hint: str | None, when: datetime) -> date | None:
    """Resolve the exclusive week-end (reset day) from a spending-page hint."""
    if not hint or not str(hint).strip():
        return None
    text = str(hint).strip()
    local = when.astimezone() if when.tzinfo else when.replace(tzinfo=timezone.utc)
    local = local.astimezone()
    today = local.date()

    if re.search(r"\btoday\b", text, re.I):
        return today

    days_left = re.search(r"(\d+)\s+days?\s+left", text, re.I)
    if days_left:
        return today + timedelta(days=int(days_left.group(1)))

    mon_day = re.search(r"\b([A-Za-z]{3})\s+(\d{1,2})\b", text)
    if not mon_day:
        return None
    mon, day_s = mon_day.group(1).title(), int(mon_day.group(2))
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            candidate = datetime.strptime(f"{mon} {day_s} {year}", "%b %d %Y").date()
        except ValueError:
            continue
        # Prefer a reset within the next week or up to one day in the past.
        delta = (candidate - today).days
        if -1 <= delta <= _WEEK_DAYS:
            return candidate
    try:
        return datetime.strptime(f"{mon} {day_s} {today.year}", "%b %d %Y").date()
    except ValueError:
        return None


def week_bounds_for_reset(week_end: date) -> tuple[date, date]:
    """Return half-open [week_start, week_end) for a 7-day Grok cycle."""
    return week_end - timedelta(days=_WEEK_DAYS), week_end


def _load_raw() -> dict[str, Any]:
    path = _grok_path()
    if not path.is_file():
        return {"version": _CACHE_VERSION, "weeks": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Could not read grok weekly cache: %s", exc)
        return {"version": _CACHE_VERSION, "weeks": {}}
    if not isinstance(raw, dict):
        return {"version": _CACHE_VERSION, "weeks": {}}
    weeks = raw.get("weeks")
    if not isinstance(weeks, dict):
        weeks = {}
    return {"version": _CACHE_VERSION, "weeks": weeks}


def _save_raw(payload: dict[str, Any]) -> None:
    path = _grok_path()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def record_grok_sample(
    pct: int | None,
    *,
    reset_hint: str | None = None,
    when: datetime | date | None = None,
) -> GrokWeekPeak | None:
    """Update the peak % for the Grok week containing ``when``."""
    if pct is None:
        return None
    value = max(0, min(100, int(pct)))
    now = when or datetime.now(timezone.utc).astimezone()
    if isinstance(now, date) and not isinstance(now, datetime):
        now = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).astimezone()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc).astimezone()
    else:
        now = now.astimezone()

    week_end = parse_grok_week_end(reset_hint, now)
    if week_end is None:
        # Fallback: ISO week ending next Monday (exclusive end = Monday).
        today = now.date()
        # Monday=0 … Sunday=6 → days until next Monday
        days_until_monday = (7 - today.weekday()) % 7
        week_end = today + timedelta(days=days_until_monday or 7)

    week_start, week_end = week_bounds_for_reset(week_end)
    key = week_end.isoformat()
    payload = _load_raw()
    weeks: dict[str, Any] = dict(payload.get("weeks") or {})
    entry = dict(weeks.get(key) or {})
    prev_peak = entry.get("peak_pct")
    peak = value if prev_peak is None else max(int(prev_peak), value)
    entry.update(
        {
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "peak_pct": peak,
            "samples": int(entry.get("samples") or 0) + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    weeks[key] = entry
    payload["weeks"] = weeks
    try:
        _save_raw(payload)
    except OSError as exc:
        log.debug("Could not save grok weekly cache: %s", exc)
        return None
    return GrokWeekPeak(week_start=week_start, week_end=week_end, peak_pct=peak)


def load_grok_week_peaks() -> tuple[GrokWeekPeak, ...]:
    """All stored weekly peaks, oldest first."""
    weeks = _load_raw().get("weeks") or {}
    out: list[GrokWeekPeak] = []
    for raw in weeks.values():
        if not isinstance(raw, dict):
            continue
        try:
            peak = int(raw["peak_pct"])
            week_end = date.fromisoformat(str(raw["week_end"]))
            if raw.get("week_start"):
                week_start = date.fromisoformat(str(raw["week_start"]))
            else:
                week_start, _ = week_bounds_for_reset(week_end)
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            GrokWeekPeak(week_start=week_start, week_end=week_end, peak_pct=peak)
        )
    out.sort(key=lambda w: w.week_start)
    return tuple(out)


def _overlap_days(
    week: GrokWeekPeak,
    period_start: date,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None,
) -> int:
    """Calendar days of ``week`` that fall inside the billing period."""
    # Prefer calendar-day overlap so a week split across renewal day prorates
    # by whole days (matches "half week" intuition on the year chart).
    period_lo = period_start_instant(
        period_start, renewal_time=renewal_time
    ).date()
    period_hi = period_end_instant(
        period_start, renewal_day=renewal_day, renewal_time=renewal_time
    ).date()
    lo = max(week.week_start, period_lo)
    hi = min(week.week_end, period_hi)
    return max(0, (hi - lo).days)


def average_weekly_peaks_for_period(
    period_start: date,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
    weeks: tuple[GrokWeekPeak, ...] | None = None,
) -> int | None:
    """Day-weighted average of weekly peaks overlapping a billing period.

    A week entirely inside the period contributes weight 7. A week split by the
    renewal boundary contributes its peak to each side with weight equal to the
    overlapping day count (proration).
    """
    peaks = weeks if weeks is not None else load_grok_week_peaks()
    if not peaks:
        return None
    weighted = 0.0
    weight = 0
    for week in peaks:
        days = _overlap_days(
            week,
            period_start,
            renewal_day=renewal_day,
            renewal_time=renewal_time,
        )
        if days <= 0:
            continue
        weighted += week.peak_pct * days
        weight += days
    if weight <= 0:
        return None
    return int(round(weighted / weight))
