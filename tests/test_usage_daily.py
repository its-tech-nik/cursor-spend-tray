"""Unit tests for in-period daily token series."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path
from unittest import mock

from cursor_spend_tray.usage_csv import (
    load_period_daily_series,
    sum_tokens_by_day,
)


class SumTokensByDayTests(unittest.TestCase):
    def test_buckets_local_days_inside_period(self) -> None:
        local = datetime.now().astimezone().tzinfo or timezone.utc
        rows = [
            {
                "date": datetime(2026, 10, 20, 12, 0, tzinfo=local).isoformat(),
                "total_tokens": 100,
            },
            {
                "date": datetime(2026, 10, 20, 18, 0, tzinfo=local).isoformat(),
                "total_tokens": 50,
            },
            {
                "date": datetime(2026, 10, 21, 9, 0, tzinfo=local).isoformat(),
                "total_tokens": 200,
            },
        ]
        got = sum_tokens_by_day(
            rows,
            period_start=date(2026, 10, 19),
            renewal_day=19,
            renewal_time=time(20, 12),
        )
        self.assertEqual(got[date(2026, 10, 20)], (150, 2))
        self.assertEqual(got[date(2026, 10, 21)], (200, 1))

    def test_excludes_events_before_renewal_clock(self) -> None:
        local = datetime.now().astimezone().tzinfo or timezone.utc
        rows = [
            {
                # Still previous period (before 20:12 on the 19th).
                "date": datetime(2026, 10, 19, 15, 0, tzinfo=local).isoformat(),
                "total_tokens": 999,
            },
            {
                "date": datetime(2026, 10, 19, 21, 0, tzinfo=local).isoformat(),
                "total_tokens": 40,
            },
        ]
        got = sum_tokens_by_day(
            rows,
            period_start=date(2026, 10, 19),
            renewal_day=19,
            renewal_time=time(20, 12),
        )
        self.assertEqual(got, {date(2026, 10, 19): (40, 1)})


class LoadPeriodDailySeriesTests(unittest.TestCase):
    def test_contiguous_axis_and_cumulative(self) -> None:
        local = datetime.now().astimezone().tzinfo or timezone.utc
        csv = (
            "Date,Model,Total Tokens\n"
            f'"{datetime(2026, 10, 19, 21, 0, tzinfo=local).isoformat()}",auto,100\n'
            f'"{datetime(2026, 10, 21, 12, 0, tzinfo=local).isoformat()}",auto,50\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_dir = root / "usage-csv"
            csv_dir.mkdir()
            (csv_dir / "2026-10-19_to_2026-11-18.csv").write_text(
                csv, encoding="utf-8"
            )
            with mock.patch(
                "cursor_spend_tray.usage_csv.data_dir", return_value=root
            ):
                series = load_period_daily_series(
                    date(2026, 10, 19),
                    renewal_day=19,
                    renewal_time=time(20, 12),
                    as_of=datetime(2026, 10, 22, 12, 0, tzinfo=local),
                )

        self.assertTrue(series.available)
        self.assertEqual(len(series.days), 4)  # 19..22
        self.assertEqual(list(series.daily_tokens), [100.0, 0.0, 50.0, 0.0])
        self.assertEqual(
            list(series.cumulative_tokens), [100.0, 100.0, 150.0, 150.0]
        )
        self.assertEqual(series.total_tokens, 150)


if __name__ == "__main__":
    unittest.main()
