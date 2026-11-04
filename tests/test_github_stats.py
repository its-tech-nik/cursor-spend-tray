"""Unit tests for GitHub billing-period activity aggregation."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timezone
from unittest.mock import patch

from cursor_spend_tray.github_stats import (
    BRANCH_PREF_DEV,
    BRANCH_PREF_MAIN,
    GithubHabitsPreview,
    GithubProjectRow,
    collect_github_habits_preview,
    default_branch_preference,
    normalize_branch_preference,
    resolve_track_branch,
    _is_merge_commit,
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
                return_value=(7, 1004, 261),
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
        row = preview.projects[0]
        self.assertIsInstance(row, GithubProjectRow)
        self.assertEqual(row.branch_preference, BRANCH_PREF_DEV)
        self.assertEqual(row.track_branch, "dev")
        activity.assert_called_once()
        self.assertEqual(activity.call_args.kwargs["branch"], "dev")
        self.assertEqual(activity.call_args.kwargs["author"], "dev")
        self.assertIsInstance(preview, GithubHabitsPreview)


if __name__ == "__main__":
    unittest.main()
