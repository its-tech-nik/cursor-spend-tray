"""GitHub account activity for the current Cursor billing period.

Uses the locally authenticated ``gh`` CLI (same account as ``gh auth login``).

For each repository the user touched this period, commit / line totals sum your
**authored non-merge commits** reachable from the selected branch (``main`` /
``dev``) in the billing window — per-commit additions/deletions, not a tree
diff. PR counts stay period-wide (authored PRs you opened).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, time as time_of_day, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .billing import period_end_for, period_label, period_start_for
from .config import data_dir

log = logging.getLogger(__name__)

CACHE_VERSION = 6
_GH_TIMEOUT_S = 90
_COMMIT_PAGE_SIZE = 100
_COMMIT_SOFT_CAP = 400
_STATS_BATCH = 25
_BRANCH_COMMITS_PAGE = 100
_BRANCH_COMMITS_CAP = 300

BRANCH_PREF_MAIN = "main"
BRANCH_PREF_DEV = "dev"
BRANCH_PREFS: tuple[str, ...] = (BRANCH_PREF_MAIN, BRANCH_PREF_DEV)

_REPO_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")

_PR_SEARCH_QUERY = """
query($q: String!, $after: String) {
  viewer { login }
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        repository { name nameWithOwner }
      }
    }
  }
}
"""

_VIEWER_QUERY = """
query {
  viewer { login }
}
"""

_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")


@dataclass(frozen=True)
class GithubProjectRow:
    """One repository's authored activity for the billing period."""

    name: str
    full_name: str
    commit_count: int = 0
    pr_count: int = 0
    additions: int = 0
    deletions: int = 0
    branch_preference: str = BRANCH_PREF_MAIN
    default_branch: str = "main"
    track_branch: str = "main"


@dataclass(frozen=True)
class GithubHabitsPreview:
    available: bool
    login: str = ""
    period_label: str = ""
    period_start: str = ""
    period_end: str = ""
    projects: tuple[GithubProjectRow, ...] = field(default_factory=tuple)
    total_commits: int = 0
    total_prs: int = 0
    total_additions: int = 0
    total_deletions: int = 0
    error_message: str = ""
    cache_hit: bool = False
    elapsed_ms: int = 0

    @staticmethod
    def unavailable(message: str) -> GithubHabitsPreview:
        return GithubHabitsPreview(available=False, error_message=message)


def _cache_path() -> Path:
    return data_dir() / "github-habits-cache.json"


def _branch_prefs_path() -> Path:
    return data_dir() / "github-branch-prefs.json"


def invalidate_github_habits_cache() -> None:
    """Drop the on-disk GitHub summary cache (e.g. after a manual reset-day change)."""
    path = _cache_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("Could not remove github cache: %s", exc)


def normalize_branch_preference(value: object, *, default_branch: str = "main") -> str:
    """Return ``main`` or ``dev``. Unknown / ``auto`` fall back to the repo default."""
    text = str(value or "").strip().lower()
    if text in BRANCH_PREFS:
        return text
    # Legacy "auto" and missing prefs → whichever of main/dev matches the default branch.
    return default_branch_preference(default_branch)


def default_branch_preference(default_branch: str) -> str:
    """Initial selection: ``dev`` only when that is the repo default, else ``main``."""
    default = (default_branch or "main").strip().lower() or "main"
    if default == BRANCH_PREF_DEV:
        return BRANCH_PREF_DEV
    return BRANCH_PREF_MAIN


def resolve_track_branch(preference: str, default_branch: str = "main") -> str:
    return normalize_branch_preference(preference, default_branch=default_branch)


def load_github_branch_prefs() -> dict[str, str]:
    """Map ``owner/repo`` → ``main`` | ``dev``."""
    path = _branch_prefs_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        full = str(key).strip()
        if not full or "/" not in full:
            continue
        text = str(value or "").strip().lower()
        if text in BRANCH_PREFS:
            out[full] = text
    return out


def save_github_branch_pref(full_name: str, preference: str) -> None:
    """Persist one repo's ``main``/``dev`` choice and invalidate the summary cache."""
    full = str(full_name).strip()
    if not full or "/" not in full:
        return
    prefs = load_github_branch_prefs()
    pref = normalize_branch_preference(preference)
    prefs[full] = pref
    try:
        data_dir().mkdir(parents=True, exist_ok=True)
        _branch_prefs_path().write_text(
            json.dumps(prefs, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("Could not write github branch prefs: %s", exc)
    invalidate_github_habits_cache()


def _prefs_fingerprint(prefs: dict[str, str]) -> str:
    items = sorted((k, v) for k, v in prefs.items() if v in BRANCH_PREFS)
    return json.dumps(items, separators=(",", ":"))


def _renewal_time_key(renewal_time: time_of_day | None) -> str:
    tod = renewal_time or time_of_day(0, 0)
    return tod.strftime("%H:%M:%S")


def _preview_from_cache_payload(payload: dict) -> GithubHabitsPreview | None:
    try:
        projects = tuple(GithubProjectRow(**row) for row in payload.get("projects") or [])
        return GithubHabitsPreview(
            available=True,
            login=str(payload.get("login") or ""),
            period_label=str(payload.get("period_label") or ""),
            period_start=str(payload.get("period_start") or ""),
            period_end=str(payload.get("period_end") or ""),
            projects=projects,
            total_commits=int(payload.get("total_commits") or 0),
            total_prs=int(payload.get("total_prs") or 0),
            total_additions=int(payload.get("total_additions") or 0),
            total_deletions=int(payload.get("total_deletions") or 0),
            cache_hit=True,
            elapsed_ms=0,
        )
    except (TypeError, ValueError, KeyError):
        return None


def _read_cache_payload() -> dict | None:
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return None
    return payload


def _load_cache(
    *,
    renewal_day: int,
    renewal_time: time_of_day | None,
    period_start: date,
    prefs_fingerprint: str,
) -> GithubHabitsPreview | None:
    payload = _read_cache_payload()
    if payload is None:
        return None
    if payload.get("renewal_day") != renewal_day:
        return None
    if payload.get("renewal_time") != _renewal_time_key(renewal_time):
        return None
    if payload.get("period_start") != period_start.isoformat():
        return None
    if payload.get("prefs_fingerprint") != prefs_fingerprint:
        return None
    return _preview_from_cache_payload(payload)


def peek_cached_github_habits_preview() -> GithubHabitsPreview | None:
    """Last on-disk GitHub summary for instant UI hydrate (may be slightly stale)."""
    payload = _read_cache_payload()
    if payload is None:
        return None
    return _preview_from_cache_payload(payload)


def _save_cache(
    preview: GithubHabitsPreview,
    *,
    renewal_day: int,
    renewal_time: time_of_day | None,
    prefs_fingerprint: str,
) -> None:
    try:
        data_dir().mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "renewal_day": renewal_day,
            "renewal_time": _renewal_time_key(renewal_time),
            "prefs_fingerprint": prefs_fingerprint,
            "login": preview.login,
            "period_label": preview.period_label,
            "period_start": preview.period_start,
            "period_end": preview.period_end,
            "total_commits": preview.total_commits,
            "total_prs": preview.total_prs,
            "total_additions": preview.total_additions,
            "total_deletions": preview.total_deletions,
            "projects": [asdict(p) for p in preview.projects],
        }
        _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.debug("Could not write github cache: %s", exc)


def _ensure_gh() -> None:
    if shutil.which("gh") is None:
        raise RuntimeError("gh CLI not found — install GitHub CLI and run gh auth login")


def _auth_hint(stderr: str, code: int) -> RuntimeError:
    lower = stderr.lower()
    if "not logged" in lower or "auth" in lower or "401" in lower:
        return RuntimeError("Not logged in to GitHub — run gh auth login")
    return RuntimeError(stderr.strip() or f"gh exited {code}")


def _run_gh(args: list[str], *, timeout: float = _GH_TIMEOUT_S) -> str:
    _ensure_gh()
    try:
        proc = subprocess.run(
            ["gh", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub query timed out") from exc
    if proc.returncode != 0:
        raise _auth_hint((proc.stderr or proc.stdout or ""), proc.returncode)
    return proc.stdout


def _run_gh_graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run ``gh api graphql`` with the locally logged-in account."""
    cmd = ["api", "graphql", "--raw-field", f"query={query}"]
    for key, value in (variables or {}).items():
        if value is None:
            continue
        cmd.extend(["-F", f"{key}={value}"])
    raw = _run_gh(cmd)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid response from gh api graphql") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected GraphQL payload")
    if payload.get("errors"):
        messages = "; ".join(
            str(e.get("message") or e) for e in payload["errors"] if isinstance(e, dict)
        )
        raise RuntimeError(messages or "GraphQL error")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Missing GraphQL data")
    return data


def _run_gh_rest(
    path: str,
    *,
    headers: list[str] | None = None,
    allow_404: bool = False,
) -> Any:
    cmd = ["api", path]
    for header in headers or []:
        cmd.extend(["-H", header])
    _ensure_gh()
    try:
        proc = subprocess.run(
            ["gh", *cmd],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub query timed out") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if allow_404 and ("404" in err or "Not Found" in err):
            return None
        raise _auth_hint(err, proc.returncode)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid response from gh api") from exc


def _viewer_login() -> str:
    data = _run_gh_graphql(_VIEWER_QUERY)
    viewer = data.get("viewer") or {}
    login = str(viewer.get("login") or "").strip()
    if not login:
        raise RuntimeError("Could not resolve GitHub login")
    return login


def _search_authored_commits(
    login: str,
    *,
    period_start: date,
    period_end: date,
) -> list[dict[str, Any]]:
    """Authored commits in [start, end] — used to discover repos touched this period."""
    q = (
        f"author:{login} "
        f"author-date:{period_start.isoformat()}..{period_end.isoformat()}"
    )
    items: list[dict[str, Any]] = []
    page = 1
    while len(items) < _COMMIT_SOFT_CAP:
        path = (
            f"search/commits?q={quote(q)}"
            f"&per_page={_COMMIT_PAGE_SIZE}&page={page}"
        )
        payload = _run_gh_rest(
            path,
            headers=["Accept: application/vnd.github+json"],
        )
        if not isinstance(payload, dict):
            break
        batch = payload.get("items") or []
        if not isinstance(batch, list) or not batch:
            break
        items.extend(n for n in batch if isinstance(n, dict))
        total = int(payload.get("total_count") or 0)
        if page * _COMMIT_PAGE_SIZE >= total or len(batch) < _COMMIT_PAGE_SIZE:
            break
        page += 1
    return items[:_COMMIT_SOFT_CAP]


def _is_merge_commit(item: dict[str, Any]) -> bool:
    parents = item.get("parents")
    if isinstance(parents, list) and len(parents) > 1:
        return True
    message = ""
    commit = item.get("commit")
    if isinstance(commit, dict):
        message = str(commit.get("message") or "")
    return message.startswith("Merge ")


def _fetch_pr_counts(
    *,
    period_start: date,
    period_end: date,
) -> tuple[str, dict[str, int]]:
    """Return (login, full_name → authored PR count created in period)."""
    search_q = (
        f"author:@me type:pr created:{period_start.isoformat()}..{period_end.isoformat()}"
    )
    counts: dict[str, int] = {}
    login = ""
    after: str | None = None
    seen = 0
    while True:
        data = _run_gh_graphql(_PR_SEARCH_QUERY, {"q": search_q, "after": after})
        viewer = data.get("viewer") or {}
        if isinstance(viewer, dict) and viewer.get("login"):
            login = str(viewer["login"])
        search = data.get("search") or {}
        if not isinstance(search, dict):
            break
        batch = search.get("nodes") or []
        if isinstance(batch, list):
            for node in batch:
                if not isinstance(node, dict):
                    continue
                repo = node.get("repository") or {}
                if not isinstance(repo, dict):
                    continue
                full = str(repo.get("nameWithOwner") or "").strip()
                if not full:
                    continue
                counts[full] = counts.get(full, 0) + 1
                seen += 1
        page = search.get("pageInfo") or {}
        if not isinstance(page, dict) or not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after or seen >= 500:
            break
    return login, counts


def _period_since_until(period_start: date, period_end: date) -> tuple[str, str]:
    """GitHub commits API bounds: since inclusive-ish, until exclusive next midnight UTC."""
    since = datetime(period_start.year, period_start.month, period_start.day, tzinfo=timezone.utc)
    until_day = period_end + timedelta(days=1)
    until = datetime(until_day.year, until_day.month, until_day.day, tzinfo=timezone.utc)
    return since.strftime("%Y-%m-%dT%H:%M:%SZ"), until.strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_default_branch(full_name: str) -> str:
    payload = _run_gh_rest(f"repos/{full_name}", allow_404=True)
    if not isinstance(payload, dict):
        return "main"
    return str(payload.get("default_branch") or "main").strip() or "main"


def _alias(index: int) -> str:
    return f"c{index}"


def _split_full_name(full_name: str) -> tuple[str, str] | None:
    if "/" not in full_name:
        return None
    owner, name = full_name.split("/", 1)
    owner, name = owner.strip(), name.strip()
    if not owner or not name:
        return None
    return owner, name


def _fetch_commit_stats(
    commits: list[tuple[str, str]],
) -> dict[str, tuple[int, int]]:
    """Map ``owner/repo#sha`` → (additions, deletions) via batched GraphQL."""
    out: dict[str, tuple[int, int]] = {}
    for start in range(0, len(commits), _STATS_BATCH):
        batch = commits[start : start + _STATS_BATCH]
        fields: list[str] = []
        meta: list[tuple[str, str]] = []
        for i, (full_name, sha) in enumerate(batch):
            split = _split_full_name(full_name)
            if split is None:
                continue
            owner, name = split
            if not _SHA_RE.fullmatch(sha):
                continue
            if not _REPO_NAME_RE.fullmatch(owner) or not _REPO_NAME_RE.fullmatch(name):
                continue
            alias = _alias(start + i)
            fields.append(
                f'{alias}: repository(owner: "{owner}", name: "{name}") {{'
                f' object(oid: "{sha}") {{'
                f" ... on Commit {{ additions deletions }}"
                f" }} }}"
            )
            meta.append((alias, f"{full_name}#{sha}"))
        if not fields:
            continue
        query = "query {\n" + "\n".join(fields) + "\n}"
        try:
            data = _run_gh_graphql(query)
        except RuntimeError as exc:
            log.debug("Commit stats batch failed: %s", exc)
            continue
        for alias, key in meta:
            node = data.get(alias)
            if not isinstance(node, dict):
                continue
            obj = node.get("object")
            if not isinstance(obj, dict):
                continue
            out[key] = (int(obj.get("additions") or 0), int(obj.get("deletions") or 0))
    return out


def _list_branch_commits(
    full_name: str,
    *,
    branch: str,
    author: str,
    period_start: date,
    period_end: date,
) -> list[dict[str, Any]]:
    """Authored commits reachable from ``branch`` in the billing window."""
    if not _BRANCH_RE.fullmatch(branch):
        return []
    since, until = _period_since_until(period_start, period_end)
    items: list[dict[str, Any]] = []
    page = 1
    while len(items) < _BRANCH_COMMITS_CAP:
        path = (
            f"repos/{full_name}/commits"
            f"?sha={quote(branch, safe='')}"
            f"&author={quote(author)}"
            f"&since={quote(since)}"
            f"&until={quote(until)}"
            f"&per_page={_BRANCH_COMMITS_PAGE}"
            f"&page={page}"
        )
        payload = _run_gh_rest(path, allow_404=True)
        if payload is None:
            return []
        if not isinstance(payload, list):
            break
        if not payload:
            break
        items.extend(n for n in payload if isinstance(n, dict))
        if len(payload) < _BRANCH_COMMITS_PAGE:
            break
        page += 1
    return items[:_BRANCH_COMMITS_CAP]


def _branch_period_activity(
    full_name: str,
    *,
    branch: str,
    author: str,
    period_start: date,
    period_end: date,
) -> tuple[int, int, int]:
    """Return ``(commit_count, additions, deletions)`` for authored commits on branch.

    Sums each non-merge commit’s additions/deletions (no tree diff / compare).
    Missing branch → zeros.
    """
    commits = _list_branch_commits(
        full_name,
        branch=branch,
        author=author,
        period_start=period_start,
        period_end=period_end,
    )
    non_merge = [
        (full_name, str(c.get("sha") or ""))
        for c in commits
        if not _is_merge_commit(c) and c.get("sha")
    ]
    stats = _fetch_commit_stats(non_merge)
    additions = 0
    deletions = 0
    for _, sha in non_merge:
        add, dele = stats.get(f"{full_name}#{sha}", (0, 0))
        additions += add
        deletions += dele
    return len(non_merge), additions, deletions


def _display_name(short: str, full: str, short_counts: dict[str, int]) -> str:
    if short_counts.get(short, 0) > 1:
        return full
    return short or full


def _discover_repos(
    commits: list[dict[str, Any]],
    pr_counts: dict[str, int],
) -> dict[str, str]:
    """Map full_name → short name for repos seen in commits or PRs."""
    repos: dict[str, str] = {}
    for item in commits:
        repo = item.get("repository") or {}
        if not isinstance(repo, dict):
            continue
        full = str(repo.get("full_name") or "").strip()
        short = str(repo.get("name") or "").strip() or full.split("/")[-1]
        if full:
            repos[full] = short or full
    for full in pr_counts:
        short = full.split("/")[-1] if "/" in full else full
        repos.setdefault(full, short)
    return repos


def _project_from_branch(
    *,
    full_name: str,
    short_name: str,
    login: str,
    preference: str,
    default_branch: str,
    pr_count: int,
    period_start: date,
    period_end: date,
) -> GithubProjectRow:
    pref = normalize_branch_preference(preference, default_branch=default_branch)
    track = resolve_track_branch(pref, default_branch)
    commit_count, additions, deletions = _branch_period_activity(
        full_name,
        branch=track,
        author=login,
        period_start=period_start,
        period_end=period_end,
    )
    return GithubProjectRow(
        name=short_name,
        full_name=full_name,
        commit_count=commit_count,
        pr_count=pr_count,
        additions=additions,
        deletions=deletions,
        branch_preference=pref,
        default_branch=default_branch,
        track_branch=track,
    )


def collect_github_habits_preview(
    *,
    renewal_day: int,
    renewal_time: time_of_day | None = None,
    use_cache: bool = True,
    now: datetime | None = None,
    branch_prefs: dict[str, str] | None = None,
) -> GithubHabitsPreview:
    """Aggregate authored commits (per tracking branch) + PRs for the billing period."""
    t0 = time.perf_counter()
    when = now or datetime.now().astimezone()
    start = period_start_for(when, renewal_day=renewal_day, renewal_time=renewal_time)
    end = period_end_for(start, renewal_day=renewal_day, renewal_time=renewal_time)
    label = period_label(start, renewal_day=renewal_day, renewal_time=renewal_time)
    raw_prefs = (
        branch_prefs if branch_prefs is not None else load_github_branch_prefs()
    )
    prefs = {
        k: v
        for k, v in (
            (str(key).strip(), str(val).strip().lower())
            for key, val in raw_prefs.items()
        )
        if k and "/" in k and v in BRANCH_PREFS
    }
    fingerprint = _prefs_fingerprint(prefs)

    if use_cache:
        cached = _load_cache(
            renewal_day=renewal_day,
            renewal_time=renewal_time,
            period_start=start,
            prefs_fingerprint=fingerprint,
        )
        if cached is not None:
            return cached

    try:
        login = _viewer_login()
        discovered_commits = _search_authored_commits(
            login, period_start=start, period_end=end
        )
        pr_login, pr_counts = _fetch_pr_counts(period_start=start, period_end=end)
        if pr_login:
            login = pr_login
        repos = _discover_repos(discovered_commits, pr_counts)
        rows: list[GithubProjectRow] = []
        for full_name, short in repos.items():
            default_branch = _repo_default_branch(full_name)
            preference = prefs.get(
                full_name, default_branch_preference(default_branch)
            )
            rows.append(
                _project_from_branch(
                    full_name=full_name,
                    short_name=short,
                    login=login,
                    preference=preference,
                    default_branch=default_branch,
                    pr_count=int(pr_counts.get(full_name, 0)),
                    period_start=start,
                    period_end=end,
                )
            )
        short_counts: dict[str, int] = {}
        for row in rows:
            short_counts[row.name] = short_counts.get(row.name, 0) + 1
        projects = [
            GithubProjectRow(
                name=_display_name(row.name, row.full_name, short_counts),
                full_name=row.full_name,
                commit_count=row.commit_count,
                pr_count=row.pr_count,
                additions=row.additions,
                deletions=row.deletions,
                branch_preference=row.branch_preference,
                default_branch=row.default_branch,
                track_branch=row.track_branch,
            )
            for row in rows
        ]
        projects.sort(
            key=lambda r: (-r.commit_count, -r.pr_count, -r.additions, r.name.lower())
        )
    except RuntimeError as exc:
        return GithubHabitsPreview.unavailable(str(exc))
    except Exception as exc:  # noqa: BLE001 — surfaced in popup
        log.debug("GitHub summary failed", exc_info=True)
        return GithubHabitsPreview.unavailable(str(exc))

    preview = GithubHabitsPreview(
        available=True,
        login=login,
        period_label=label,
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        projects=tuple(projects),
        total_commits=sum(p.commit_count for p in projects),
        total_prs=sum(p.pr_count for p in projects),
        total_additions=sum(p.additions for p in projects),
        total_deletions=sum(p.deletions for p in projects),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )
    _save_cache(
        preview,
        renewal_day=renewal_day,
        renewal_time=renewal_time,
        prefs_fingerprint=fingerprint,
    )
    return preview
