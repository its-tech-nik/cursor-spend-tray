"""Unit tests for GitHub billing-period activity aggregation."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path
from unittest.mock import patch

from cursor_spend_tray.github_stats import (
    BRANCH_PREF_DEV,
    BRANCH_PREF_MAIN,
    CACHE_VERSION,
    GithubHabitsPreview,
    GithubProjectRow,
    _CACHE_MAX_AGE_S,
    _build_daily_commit_series,
    _commit_day,
    _is_merge_commit,
    _load_cache,
    _prefs_fingerprint,
    _save_cache,
    collect_github_habits_preview,
    default_branch_preference,
    normalize_branch_preference,
    peek_cached_github_habits_preview,
    resolve_track_branch,
)


class BranchPreferenceTests(unittest.TestCase):
    def test_default_pref_follows_repo_default(self) -> None:
        self.assertEqual(default_branch_preference("main"), BRANCH_PREF_MAIN)
        self.assertEqual(default_branch_preference("dev"), BRANCH_PREF_DEV)
        self.assertEqual(default_branch_preference("master"), BRANCH_PREF_MAIN)

    def test_normalize_maps_auto_to_default(self) -> None:
        self.assertEqual(
            normalize_branch_preference("auto", default_branch="dev"),
            BRANCH_PREF_DEV,
        )
        self.assertEqual(
            normalize_branch_preference("main", default_branch="dev"),
            BRANCH_PREF_MAIN,
        )
        self.assertEqual(resolve_track_branch(BRANCH_PREF_DEV), "dev")


class MergeCommitTests(unittest.TestCase):
    def test_parents_mark_merge(self) -> None:
        self.assertTrue(
            _is_merge_commit({"parents": [{}, {}], "commit": {"message": "x"}})
        )
        self.assertFalse(
            _is_merge_commit({"parents": [{}], "commit": {"message": "feat: x"}})
        )


class CommitDaySeriesTests(unittest.TestCase):
    def test_commit_day_prefers_author_utc(self) -> None:
        day = _commit_day(
            {
                "commit": {
                    "author": {"date": "2026-10-20T23:30:00Z"},
                    "committer": {"date": "2026-10-21T01:00:00Z"},
                }
            }
        )
        self.assertEqual(day, date(2026, 10, 20))

    def test_build_daily_series_fills_gaps_and_cumulative(self) -> None:
        labels, daily, cumulative = _build_daily_commit_series(
            {date(2026, 10, 19): 2, date(2026, 10, 21): 3},
            period_start=date(2026, 10, 19),
            period_end=date(2026, 11, 18),
            as_of=date(2026, 10, 21),
        )
        self.assertEqual(labels, ("Oct 19", "Oct 20", "Oct 21"))
        self.assertEqual(daily, (2.0, 0.0, 3.0))
        self.assertEqual(cumulative, (2.0, 2.0, 5.0))


class CollectGithubHabitsPreviewTests(unittest.TestCase):
    def test_unavailable_when_gh_missing(self) -> None:
        with patch("cursor_spend_tray.github_stats.shutil.which", return_value=None):
            preview = collect_github_habits_preview(
                renewal_day=19,
                renewal_time=time(20, 0),
                use_cache=False,
                now=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc),
            )
        self.assertFalse(preview.available)
        self.assertIn("gh CLI", preview.error_message)

    def test_sums_authored_commit_stats_on_selected_branch(self) -> None:
        discovered = [
            {
                "sha": "ignore",
                "parents": [{}],
                "commit": {"message": "other"},
                "repository": {
                    "full_name": "acme/frontend",
                    "name": "frontend",
                },
            }
        ]

        with (
            patch(
                "cursor_spend_tray.github_stats._viewer_login",
                return_value="dev",
            ),
            patch(
                "cursor_spend_tray.github_stats._search_authored_commits",
                return_value=discovered,
            ),
            patch(
                "cursor_spend_tray.github_stats._fetch_pr_counts",
                return_value=("dev", {"acme/frontend": 2}),
            ),
            patch(
                "cursor_spend_tray.github_stats._repo_default_branch",
                return_value="main",
            ),
            patch(
                "cursor_spend_tray.github_stats._branch_period_activity",
                return_value=(7, 1004, 261, {date(2026, 10, 20): 7}),
            ) as activity,
            patch("cursor_spend_tray.github_stats._save_cache"),
            patch(
                "cursor_spend_tray.github_stats._load_cache",
                return_value=None,
            ),
        ):
            preview = collect_github_habits_preview(
                renewal_day=19,
                renewal_time=time(20, 0),
                use_cache=False,
                now=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc),
                branch_prefs={"acme/frontend": BRANCH_PREF_DEV},
            )

        self.assertTrue(preview.available)
        self.assertEqual(preview.period_start, date(2026, 10, 19).isoformat())
        self.assertEqual(preview.total_commits, 7)
        self.assertEqual(preview.total_prs, 2)
        self.assertEqual(preview.total_additions, 1004)
        self.assertEqual(preview.total_deletions, 261)
        self.assertGreaterEqual(len(preview.day_labels), 2)
        self.assertEqual(len(preview.day_labels), len(preview.daily_commits))
        self.assertEqual(sum(preview.daily_commits), 7.0)
        self.assertEqual(preview.cumulative_commits[-1], 7.0)
        row = preview.projects[0]
        self.assertIsInstance(row, GithubProjectRow)
        self.assertEqual(row.branch_preference, BRANCH_PREF_DEV)
        self.assertEqual(row.track_branch, "dev")
        activity.assert_called_once()
        self.assertEqual(activity.call_args.kwargs["branch"], "dev")
        self.assertEqual(activity.call_args.kwargs["author"], "dev")
        self.assertIsInstance(preview, GithubHabitsPreview)


class GithubHabitsCacheTtlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self._data_dir = patch(
            "cursor_spend_tray.github_stats.data_dir", return_value=self.root
        )
        self._data_dir.start()
        self.preview = GithubHabitsPreview(
            available=True,
            login="dev",
            period_label="Oct 19 – Nov 18",
            period_start=date(2026, 10, 19).isoformat(),
            period_end=date(2026, 11, 18).isoformat(),
            projects=(
                GithubProjectRow(
                    name="cursor-spend-tray",
                    full_name="its-tech-nik/cursor-spend-tray",
                    commit_count=6,
                    pr_count=1,
                    additions=10,
                    deletions=2,
                ),
            ),
            total_commits=6,
            total_prs=1,
            total_additions=10,
            total_deletions=2,
        )
        self.prefs_fp = _prefs_fingerprint({})
        self.fetched_at = 1_700_000_000.0

    def tearDown(self) -> None:
        self._data_dir.stop()
        self._tmpdir.cleanup()

    def _write_cache(self, *, fetched_at: float | None) -> None:
        _save_cache(
            self.preview,
            renewal_day=19,
            renewal_time=time(20, 0),
            prefs_fingerprint=self.prefs_fp,
            fetched_at=fetched_at if fetched_at is not None else self.fetched_at,
        )
        if fetched_at is None:
            # Simulate a pre-TTL cache file with no stamp.
            path = self.root / "github-habits-cache.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("fetched_at", None)
            path.write_text(json.dumps(payload), encoding="utf-8")

    def test_fresh_cache_is_a_hit(self) -> None:
        self._write_cache(fetched_at=self.fetched_at)
        cached = _load_cache(
            renewal_day=19,
            renewal_time=time(20, 0),
            period_start=date(2026, 10, 19),
            prefs_fingerprint=self.prefs_fp,
            now=self.fetched_at + 30,
        )
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.total_commits, 6)
        self.assertTrue(cached.cache_hit)

    def test_aged_cache_misses_so_worker_revalidates(self) -> None:
        self._write_cache(fetched_at=self.fetched_at)
        cached = _load_cache(
            renewal_day=19,
            renewal_time=time(20, 0),
            period_start=date(2026, 10, 19),
            prefs_fingerprint=self.prefs_fp,
            now=self.fetched_at + _CACHE_MAX_AGE_S + 1,
        )
        self.assertIsNone(cached)

    def test_missing_fetched_at_misses(self) -> None:
        self._write_cache(fetched_at=None)
        cached = _load_cache(
            renewal_day=19,
            renewal_time=time(20, 0),
            period_start=date(2026, 10, 19),
            prefs_fingerprint=self.prefs_fp,
            now=self.fetched_at,
        )
        self.assertIsNone(cached)

    def test_peek_returns_stale_cache_for_hydrate(self) -> None:
        self._write_cache(fetched_at=self.fetched_at)
        # Age past TTL — collect would miss, but hydrate still paints.
        peeked = peek_cached_github_habits_preview()
        self.assertIsNotNone(peeked)
        assert peeked is not None
        self.assertEqual(peeked.total_commits, 6)
        self.assertEqual(peeked.projects[0].commit_count, 6)
        # Sanity: version stamped.
        raw = json.loads(
            (self.root / "github-habits-cache.json").read_text(encoding="utf-8")
        )
        self.assertEqual(raw["version"], CACHE_VERSION)

    def test_collect_requeries_when_cache_aged(self) -> None:
        self._write_cache(fetched_at=self.fetched_at)
        discovered = [
            {
                "sha": "abc",
                "parents": [{}],
                "commit": {"message": "feat"},
                "repository": {
                    "full_name": "its-tech-nik/cursor-spend-tray",
                    "name": "cursor-spend-tray",
                },
            }
        ]
        with (
            patch(
                "cursor_spend_tray.github_stats.time.time",
                return_value=self.fetched_at + _CACHE_MAX_AGE_S + 5,
            ),
            patch(
                "cursor_spend_tray.github_stats._viewer_login",
                return_value="dev",
            ),
            patch(
                "cursor_spend_tray.github_stats._search_authored_commits",
                return_value=discovered,
            ),
            patch(
                "cursor_spend_tray.github_stats._fetch_pr_counts",
                return_value=("dev", {}),
            ),
            patch(
                "cursor_spend_tray.github_stats._repo_default_branch",
                return_value="main",
            ),
            patch(
                "cursor_spend_tray.github_stats._branch_period_activity",
                return_value=(11, 50, 10, {date(2026, 11, 1): 11}),
            ) as activity,
        ):
            preview = collect_github_habits_preview(
                renewal_day=19,
                renewal_time=time(20, 0),
                use_cache=True,
                now=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc),
                branch_prefs={},
            )
        self.assertTrue(preview.available)
        self.assertFalse(preview.cache_hit)
        self.assertEqual(preview.total_commits, 11)
        self.assertEqual(sum(preview.daily_commits), 11.0)
        activity.assert_called_once()


if __name__ == "__main__":
    unittest.main()
