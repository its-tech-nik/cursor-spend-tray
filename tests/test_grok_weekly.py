"""Unit tests for Grok weekly peak tracking and billing-period proration."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

from cursor_spend_tray.grok_weekly import (
    GrokWeekPeak,
    average_weekly_peaks_for_period,
    parse_grok_week_end,
    record_grok_sample,
    week_bounds_for_reset,
)


class ParseGrokWeekEndTests(unittest.TestCase):
    def test_days_left(self) -> None:
        when = datetime(2025, 10, 29, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            parse_grok_week_end("Nov 1 (3 days left)", when),
            date(2025, 11, 1),
        )

    def test_today(self) -> None:
        when = datetime(2025, 11, 1, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_grok_week_end("Resets today", when), date(2025, 11, 1))

    def test_month_day_without_days_left(self) -> None:
        when = datetime(2025, 10, 30, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_grok_week_end("Nov 1", when), date(2025, 11, 1))


class WeekBoundsTests(unittest.TestCase):
    def test_seven_day_half_open(self) -> None:
        start, end = week_bounds_for_reset(date(2025, 11, 1))
        self.assertEqual(start, date(2025, 10, 25))
        self.assertEqual(end, date(2025, 11, 1))


class AverageWeeklyPeaksProrationTests(unittest.TestCase):
    def test_full_weeks_simple_average(self) -> None:
        weeks = (
            GrokWeekPeak(date(2025, 8, 19), date(2025, 8, 26), 40),
            GrokWeekPeak(date(2025, 8, 26), date(2025, 9, 2), 60),
        )
        # Period Aug 19 → Sep 19 exclusive; both weeks fully inside.
        avg = average_weekly_peaks_for_period(
            date(2025, 8, 19),
            renewal_day=19,
            renewal_time=time(0, 0),
            weeks=weeks,
        )
        self.assertEqual(avg, 50)

    def test_straddle_week_prorated_into_both_periods(self) -> None:
        # Renewal day 19. Split week: Sep 15–Sep 22.
        # Period A Aug 19–Sep 19: overlap Sep 15–19 = 4 days
        # Period B Sep 19–Oct 19: overlap Sep 19–22 = 3 days
        split = GrokWeekPeak(date(2025, 9, 15), date(2025, 9, 22), 70)
        early = GrokWeekPeak(date(2025, 9, 8), date(2025, 9, 15), 10)
        late = GrokWeekPeak(date(2025, 9, 22), date(2025, 9, 29), 90)

        avg_a = average_weekly_peaks_for_period(
            date(2025, 8, 19),
            renewal_day=19,
            weeks=(early, split),
        )
        # early: 7 days @ 10; split: 4 days @ 70 → (70+280)/11 = 31.818 → 32
        self.assertEqual(avg_a, 32)

        avg_b = average_weekly_peaks_for_period(
            date(2025, 9, 19),
            renewal_day=19,
            weeks=(split, late),
        )
        # split: 3 @ 70; late: 7 @ 90 → (210+630)/10 = 84
        self.assertEqual(avg_b, 84)

    def test_empty_weeks(self) -> None:
        self.assertIsNone(
            average_weekly_peaks_for_period(
                date(2025, 8, 19), renewal_day=19, weeks=()
            )
        )


class RecordGrokSampleTests(unittest.TestCase):
    def test_keeps_peak_within_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "cursor_spend_tray.grok_weekly.data_dir", return_value=root
            ):
                when = datetime(2025, 10, 29, 12, 0, tzinfo=timezone.utc)
                first = record_grok_sample(
                    13, reset_hint="Nov 1 (3 days left)", when=when
                )
                second = record_grok_sample(
                    40, reset_hint="Nov 1 (3 days left)", when=when
                )
                third = record_grok_sample(
                    25,
                    reset_hint="Nov 1 (2 days left)",
                    when=when + timedelta(days=1),
                )
                self.assertIsNotNone(first)
                assert first is not None and second is not None and third is not None
                self.assertEqual(first.peak_pct, 13)
                self.assertEqual(second.peak_pct, 40)
                self.assertEqual(third.peak_pct, 40)
                self.assertEqual(third.week_end, date(2025, 11, 1))


if __name__ == "__main__":
    unittest.main()
