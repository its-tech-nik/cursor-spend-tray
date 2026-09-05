"""Cursor subscription billing-period helpers.

Renewal day is hardcoded for now; later it will be scraped from the spending page.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .config import SUBSCRIPTION_RENEWAL_DAY


def period_start_for(
    when: datetime | date,
    *,
    renewal_day: int = SUBSCRIPTION_RENEWAL_DAY,
) -> date:
    """Return the billing-period start date (renewal_day) containing `when`."""
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        d = when.astimezone().date()
    else:
        d = when
    day = max(1, min(28, int(renewal_day)))  # keep valid across months
    if d.day >= day:
        return date(d.year, d.month, day)
    if d.month == 1:
        return date(d.year - 1, 12, day)
    return date(d.year, d.month - 1, day)


def period_end_for(
    start: date,
    *,
    renewal_day: int = SUBSCRIPTION_RENEWAL_DAY,
) -> date:
    """Inclusive end date of the billing period that starts on `start`."""
    day = max(1, min(28, int(renewal_day)))
    if start.month == 12:
        nxt = date(start.year + 1, 1, day)
    else:
        nxt = date(start.year, start.month + 1, day)
    return nxt - timedelta(days=1)


def period_key(
    when: datetime | date,
    *,
    renewal_day: int = SUBSCRIPTION_RENEWAL_DAY,
) -> str:
    """Stable period id, e.g. '2026-08-19'."""
    return period_start_for(when, renewal_day=renewal_day).isoformat()


def period_label(
    start: date,
    *,
    renewal_day: int = SUBSCRIPTION_RENEWAL_DAY,
) -> str:
    """Human label like 'Aug 19–Sep 18, 2026'."""
    end = period_end_for(start, renewal_day=renewal_day)
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
