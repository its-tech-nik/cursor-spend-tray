"""Unit tests for AUTO usage-reset gating (record only on first >0 → 0%)."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cursor_spend_tray.config import (
    _day_ordinal,
    default_usage_reset_at,
    format_usage_reset_label,
    resolve_usage_reset_at,
)


class ResolveUsageResetAtTests(unittest.TestCase):
    def test_records_now_on_first_drop_from_positive_to_zero(self) -> None:
        got = resolve_usage_reset_at(
            prev_cursor_pct=42,
            new_cursor_pct=0,
            prev_reset_at=1_000.0,
            now=9_999.0,
        )
        self.assertEqual(got, 9_999.0)

    def test_records_again_on_later_cycle_after_usage_climbed(self) -> None:
        got = resolve_usage_reset_at(
            prev_cursor_pct=99,
            new_cursor_pct=0,
            prev_reset_at=1_000.0,
            now=4_000.0,
        )
        self.assertEqual(got, 4_000.0)

    def test_staying_at_zero_does_not_overwrite(self) -> None:
        got = resolve_usage_reset_at(
            prev_cursor_pct=0,
            new_cursor_pct=0,
            prev_reset_at=1_000.0,
            now=2_000.0,
        )
        self.assertEqual(got, 1_000.0)

    def test_many_polls_at_zero_keep_first_reset_timestamp(self) -> None:
        """Billing can sit at 0% for a day+; each poll must not nudge the stamp."""
        # First sighting of the monthly reset.
        reset_at = resolve_usage_reset_at(
            prev_cursor_pct=87,
            new_cursor_pct=0,
            prev_reset_at=500.0,
            now=10_000.0,
        )
        self.assertEqual(reset_at, 10_000.0)

        # Simulate ~24h of 8-minute polls still reading 0%.
        poll_interval = 8 * 60
        for i in range(1, 181):  # 180 * 8m ≈ 24h
            reset_at = resolve_usage_reset_at(
                prev_cursor_pct=0,
                new_cursor_pct=0,
                prev_reset_at=reset_at,
                now=10_000.0 + i * poll_interval,
            )
            self.assertEqual(
                reset_at,
                10_000.0,
                msg=f"poll {i} overwrote reset timestamp",
            )

    def test_climbing_from_zero_keeps_previous(self) -> None:
        got = resolve_usage_reset_at(
            prev_cursor_pct=0,
            new_cursor_pct=5,
            prev_reset_at=1_000.0,
            now=2_000.0,
        )
        self.assertEqual(got, 1_000.0)

    def test_climbing_while_already_positive_keeps_previous(self) -> None:
        got = resolve_usage_reset_at(
            prev_cursor_pct=5,
            new_cursor_pct=10,
            prev_reset_at=1_000.0,
            now=3_000.0,
        )
        self.assertEqual(got, 1_000.0)

    def test_drop_to_nonzero_does_not_record(self) -> None:
        got = resolve_usage_reset_at(
            prev_cursor_pct=50,
            new_cursor_pct=1,
            prev_reset_at=1_000.0,
            now=2_000.0,
        )
        self.assertEqual(got, 1_000.0)

    def test_none_to_zero_is_not_a_reset(self) -> None:
        """Missing previous reading must not look like a billing reset."""
        got = resolve_usage_reset_at(
            prev_cursor_pct=None,
            new_cursor_pct=0,
            prev_reset_at=1_000.0,
            now=2_000.0,
        )
        self.assertEqual(got, 1_000.0)

    def test_positive_to_none_keeps_previous(self) -> None:
        got = resolve_usage_reset_at(
            prev_cursor_pct=12,
            new_cursor_pct=None,
            prev_reset_at=1_000.0,
            now=2_000.0,
        )
        self.assertEqual(got, 1_000.0)

    def test_missing_prev_reset_falls_back_to_seed(self) -> None:
        got = resolve_usage_reset_at(
            prev_cursor_pct=10,
            new_cursor_pct=20,
            prev_reset_at=None,
            now=2_000.0,
        )
        self.assertEqual(got, default_usage_reset_at())

    def test_uses_time_time_when_now_omitted(self) -> None:
        with patch("cursor_spend_tray.config.time.time", return_value=5_555.5):
            got = resolve_usage_reset_at(
                prev_cursor_pct=7,
                new_cursor_pct=0,
                prev_reset_at=1.0,
            )
        self.assertEqual(got, 5_555.5)


class FormatUsageResetLabelTests(unittest.TestCase):
    def test_seed_placeholder_reads_eighteenth_at_0400(self) -> None:
        self.assertEqual(
            format_usage_reset_label(default_usage_reset_at()),
            "resets on the 18th at 04:00",
        )

    def test_day_ordinals(self) -> None:
        self.assertEqual(_day_ordinal(1), "1st")
        self.assertEqual(_day_ordinal(2), "2nd")
        self.assertEqual(_day_ordinal(3), "3rd")
        self.assertEqual(_day_ordinal(4), "4th")
        self.assertEqual(_day_ordinal(11), "11th")
        self.assertEqual(_day_ordinal(12), "12th")
        self.assertEqual(_day_ordinal(13), "13th")
        self.assertEqual(_day_ordinal(21), "21st")
        self.assertEqual(_day_ordinal(22), "22nd")
        self.assertEqual(_day_ordinal(23), "23rd")

    def test_label_uses_local_clock_components(self) -> None:
        # Fixed instant: 2026-09-18 04:00 Europe/Athens (UTC+3) → local 04:00.
        ts = datetime(2026, 9, 18, 4, 0, tzinfo=ZoneInfo("Europe/Athens")).timestamp()
        # Format is local to the machine; assert shape and day/hour when TZ matches,
        # otherwise at least the day ordinal for that local conversion.
        label = format_usage_reset_label(ts)
        self.assertTrue(label.startswith("resets on the "))
        self.assertIn(" at ", label)
        local = datetime.fromtimestamp(ts).astimezone()
        self.assertIn(_day_ordinal(local.day), label)
        self.assertIn(local.strftime("%H:%M"), label)


if __name__ == "__main__":
    unittest.main()
