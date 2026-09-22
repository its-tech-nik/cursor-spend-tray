"""Cursor subscription billing-period helpers.

Period boundaries use the renewal day plus optional clock time from the
active usage-reset stamp in state.json (auto-discovered or set manually).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


def _clamp_day(renewal_day: int) -> int:
    return max(1, min(28, int(renewal_day)))


def _local_tz():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _as_local_datetime(when: datetime | date) -> datetime:
    """Normalize to a timezone-aware local datetime.

    Plain ``date`` values are treated as local midnight (used when only a
    calendar day is known, e.g. daily-stats keys).
    """
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(_local_tz())
    return datetime(when.year, when.month, when.day, tzinfo=_local_tz())


def renewal_instant(
    year: int,
    month: int,
    *,
    renewal_day: int,
    renewal_time: time | None = None,
) -> datetime:
    """Local datetime of the renewal boundary in ``year``/``month``."""
    day = _clamp_day(renewal_day)
    tod = renewal_time or time(0, 0)
    return datetime(
        year,
        month,
        day,
        tod.hour,
        tod.minute,
        tod.second,
        tod.microsecond,
        tzinfo=_local_tz(),
    )


def _prev_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _next_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def period_start_for(
    when: datetime | date,
    *,
    renewal_day: int,
    renewal_time: time | None = None,
) -> date:
    """Return the billing-period start date (renewal day) containing ``when``.

    When ``renewal_time`` is set, events strictly before that clock on the
    renewal day belong to the previous period.
    """
    dt = _as_local_datetime(when)
    day = _clamp_day(renewal_day)
    candidate = renewal_instant(
        dt.year, dt.month, renewal_day=day, renewal_time=renewal_time
    )
    if dt >= candidate:
        return candidate.date()
    py, pm = _prev_month(dt.year, dt.month)
    return renewal_instant(
        py, pm, renewal_day=day, renewal_time=renewal_time
    ).date()


def period_end_for(
    start: date,
    *,
    renewal_day: int,
    renewal_time: time | None = None,
) -> date:
    """Inclusive end *date* of the billing period that starts on ``start``.

    ``renewal_time`` is accepted for API symmetry; labels still use calendar
    days (day before the next renewal date).
    """
    del renewal_time
    day = _clamp_day(renewal_day)
    ny, nm = _next_month(start.year, start.month)
    nxt = date(ny, nm, day)
    return nxt - timedelta(days=1)


def period_start_instant(
    start: date,
    *,
    renewal_time: time | None = None,
) -> datetime:
    """Inclusive local start instant of the period whose start date is ``start``."""
    tod = renewal_time or time(0, 0)
    return datetime(
        start.year,
        start.month,
        start.day,
        tod.hour,
        tod.minute,
        tod.second,
        tod.microsecond,
        tzinfo=_local_tz(),
    )


def period_end_instant(
    start: date,
    *,
    renewal_day: int,
    renewal_time: time | None = None,
) -> datetime:
    """Exclusive local end instant of the period that starts on ``start``."""
    day = _clamp_day(renewal_day)
    ny, nm = _next_month(start.year, start.month)
    return renewal_instant(ny, nm, renewal_day=day, renewal_time=renewal_time)


def period_ms_range(
    start: date,
    *,
    renewal_day: int,
    renewal_time: time | None = None,
) -> tuple[int, int]:
    """Inclusive start / inclusive end milliseconds for CSV export queries."""
    start_dt = period_start_instant(start, renewal_time=renewal_time)
    end_dt = period_end_instant(
        start, renewal_day=renewal_day, renewal_time=renewal_time
    ) - timedelta(milliseconds=1)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def period_key(
    when: datetime | date,
    *,
    renewal_day: int,
    renewal_time: time | None = None,
) -> str:
    """Stable period id, e.g. '2026-08-19'."""
    return period_start_for(
        when, renewal_day=renewal_day, renewal_time=renewal_time
    ).isoformat()


def period_label(
    start: date,
    *,
    renewal_day: int,
    renewal_time: time | None = None,
) -> str:
    """Human label like 'Aug 19–Sep 18, 2026'."""
    end = period_end_for(start, renewal_day=renewal_day, renewal_time=renewal_time)
    if start.year != end.year:
        return (
            f"{start.strftime('%b')} {start.day}, {start.year}–"
            f"{end.strftime('%b')} {end.day}, {end.year}"
        )
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}, {start.year}"
    return f"{start.strftime('%b')} {start.day}–{end.strftime('%b')} {end.day}, {start.year}"


def short_period_label(start: date) -> str:
    """Compact axis label, e.g. 'Aug 19'."""
    return f"{start.strftime('%b')} {start.day}"


def history_start_for(*, renewal_day: int, periods_back: int = 13) -> date:
    """First period start to sync (~``periods_back`` cycles before now)."""
    now = datetime.now().astimezone()
    day = _clamp_day(renewal_day)
    y, m = now.year, now.month - periods_back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, day)
