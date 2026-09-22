"""Unit tests for time-aware billing-period boundaries."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timezone

from cursor_spend_tray.billing import period_start_for
from cursor_spend_tray.config import (
    format_usage_reset_label,
    renewal_from_usage_reset,
)


class PeriodStartTimeAwareTests(unittest.TestCase):
    def test_before_renewal_clock_stays_previous_period(self) -> None:
        local = datetime.now().astimezone().tzinfo or timezone.utc
        when = datetime(2026, 9, 19, 15, 0, tzinfo=local)
        got = period_start_for(
            when, renewal_day=19, renewal_time=time(20, 12)
        )
        self.assertEqual(got, date(2026, 8, 19))

    def test_at_and_after_renewal_clock_starts_new_period(self) -> None:
        local = datetime.now().astimezone().tzinfo or timezone.utc
        boundary = datetime(2026, 9, 19, 20, 12, tzinfo=local)
        self.assertEqual(
            period_start_for(
                boundary, renewal_day=19, renewal_time=time(20, 12)
            ),
            date(2026, 9, 19),
        )
        after = datetime(2026, 9, 19, 20, 13, tzinfo=local)
        self.assertEqual(
            period_start_for(after, renewal_day=19, renewal_time=time(20, 12)),
            date(2026, 9, 19),
        )

    def test_midnight_default_matches_day_only_behavior(self) -> None:
        local = datetime.now().astimezone().tzinfo or timezone.utc
        when = datetime(2026, 9, 19, 0, 0, tzinfo=local)
        self.assertEqual(
            period_start_for(when, renewal_day=19),
            date(2026, 9, 19),
        )
        before = datetime(2026, 9, 18, 23, 59, tzinfo=local)
        self.assertEqual(
            period_start_for(before, renewal_day=19),
            date(2026, 8, 19),
        )


class RenewalFromUsageResetTests(unittest.TestCase):
    def test_extracts_day_and_clock(self) -> None:
        local = datetime.now().astimezone().tzinfo or timezone.utc
        ts = datetime(2026, 9, 19, 20, 12, 16, tzinfo=local).timestamp()
        day, tod = renewal_from_usage_reset(ts)
        self.assertEqual(day, 19)
        self.assertEqual(tod, time(20, 12, 16))

    def test_none_falls_back_to_day_19_midnight(self) -> None:
        day, tod = renewal_from_usage_reset(None)
        self.assertEqual(day, 19)
        self.assertEqual(tod, time(0, 0))

    def test_label_matches_scraped_stamp(self) -> None:
        local = datetime.now().astimezone().tzinfo or timezone.utc
        ts = datetime(2026, 9, 19, 20, 12, tzinfo=local).timestamp()
        self.assertEqual(
            format_usage_reset_label(ts),
            "resets on the 19th at 20:12",
        )


if __name__ == "__main__":
    unittest.main()
