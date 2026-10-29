"""Unit tests for Grok Bot weekly-allowance text patterns used by the scraper."""

from __future__ import annotations

import re
import unittest


def pick_pct(body: str, label: str) -> int | None:
    """Python mirror of EXTRACT_JS pickPct."""
    esc = re.escape(label)
    m = re.search(rf"{esc}[\s\S]{{0,240}}?(\d{{1,3}})%\s*used", body, re.I)
    return int(m.group(1)) if m else None


def pick_grok_reset_hint(body: str) -> str | None:
    m = re.search(r"Grok Bot[\s\S]{0,500}?Resets\s+([^\n]+)", body, re.I)
    return m.group(1).strip() if m else None


SAMPLE = """
Included in Pro
Cursor Models
Includes Cursor Grok
42% used
Other Models
17% used

Grok Bot
Included in Pro. Learn more
Weekly usage                    13% used
Resets Nov 1 (3 days left)
Grok Bot pauses at the limit until it resets.
"""


class GrokBotExtractTests(unittest.TestCase):
    def test_picks_grok_bot_pct_not_cursor_grok(self) -> None:
        self.assertEqual(pick_pct(SAMPLE, "Grok Bot"), 13)
        self.assertEqual(pick_pct(SAMPLE, "Cursor Models"), 42)
        self.assertEqual(pick_pct(SAMPLE, "Other Models"), 17)

    def test_picks_weekly_reset_hint(self) -> None:
        self.assertEqual(pick_grok_reset_hint(SAMPLE), "Nov 1 (3 days left)")

    def test_missing_grok_section(self) -> None:
        body = "Cursor Models\n10% used\nOther Models\n5% used\n"
        self.assertIsNone(pick_pct(body, "Grok Bot"))
        self.assertIsNone(pick_grok_reset_hint(body))


if __name__ == "__main__":
    unittest.main()
