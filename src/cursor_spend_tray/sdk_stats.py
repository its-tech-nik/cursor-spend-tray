"""Local Cursor SDK agent-habit stats from sdk-agent-store index.db files.

Read-only aggregation for the tray popup preview. Shell non-zero exit codes are
the reliable failure signal — Cursor marks most completed tools as success.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path

from .billing import period_label, period_start_for, short_period_label
from .config import SUBSCRIPTION_RENEWAL_DAY

log = logging.getLogger(__name__)

DEFAULT_PROJECTS_ROOT = Path.home() / ".cursor" / "projects"
DEFAULT_RECENT_RUNS = 50  # kept for callers; history uses billing periods

RunRow = tuple[str, str, str | None, str | None, Path]


@dataclass(frozen=True)
class SdkHabitsBatch:
    """One consecutive window of runs (typically 50) for trend charts."""

    label: str
    runs: int
    started_at: str | None
    ended_at: str | None
    finished: int = 0
    cancelled: int = 0
    error: int = 0
    friction_rate_pct: int | None = None
    shell_fail_pct: int | None = None
    shell_nonzero: int = 0
    shell_total: int = 0
    median_total_tokens: int | None = None
    median_tools_per_run: float | None = None
    incomplete_tools: int = 0


@dataclass(frozen=True)
class SdkHabitsPreview:
    """Compact preview of recent local SDK agent runs."""

    available: bool
    window_label: str = "last 50 runs"
    indexes_scanned: int = 0
    runs_considered: int = 0
    finished: int = 0
    cancelled: int = 0
    error: int = 0
    other_status: int = 0
    friction_rate_pct: int | None = None
    median_total_tokens: int | None = None
    median_tools_per_run: float | None = None
    shell_nonzero: int = 0
    shell_total: int = 0
    incomplete_tools: int = 0
    insight: str = ""
    error_message: str = ""
    lines: tuple[str, ...] = field(default_factory=tuple)
    history: tuple[SdkHabitsBatch, ...] = field(default_factory=tuple)

    @staticmethod
    def unavailable(message: str = "No local SDK run data") -> SdkHabitsPreview:
        return SdkHabitsPreview(
            available=False,
            error_message=message,
            lines=(message,),
        )


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _median_int(values: list[int]) -> int | None:
    if not values:
        return None
    return int(round(statistics.median(values)))


def _median_float(values: list[int]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    uri = f"file:{path}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        log.debug("Could not open %s: %s", path, exc)
        return None


def _iter_index_dbs(projects_root: Path) -> list[Path]:
    if not projects_root.is_dir():
        return []
    return sorted(
        p for p in projects_root.rglob("index.db") if "sdk-agent-store" in p.parts
    )


def _load_runs(index_path: Path) -> list[RunRow]:
    """Return (run_id, status, usage_json, created_at, index_path)."""
    con = _connect_ro(index_path)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT run_id, status, usage_json, created_at FROM runs"
        ).fetchall()
    except sqlite3.Error as exc:
        log.debug("runs query failed for %s: %s", index_path, exc)
        return []
    finally:
        con.close()
    out: list[RunRow] = []
    for run_id, status, usage_json, created_at in rows:
        if not run_id:
            continue
        out.append(
            (
                str(run_id),
                str(status or ""),
                usage_json if isinstance(usage_json, str) else None,
                created_at if isinstance(created_at, str) else None,
                index_path,
            )
        )
    return out


def _token_total(usage_json: str | None) -> int | None:
    if not usage_json:
        return None
    try:
        obj = json.loads(usage_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    for key in ("totalTokens", "inputTokens"):
        val = obj.get(key)
        if isinstance(val, (int, float)) and val >= 0:
            return int(val)
    return None


def _tool_stats_for_runs(
    index_path: Path, run_ids: set[str]
) -> tuple[dict[str, int], int, int, int, Counter[int]]:
    """Per-run completed tool counts, shell totals, incomplete, exit codes."""
    completed_by_run: dict[str, int] = {rid: 0 for rid in run_ids}
    shell_total = 0
    shell_nonzero = 0
    incomplete = 0
    exit_codes: Counter[int] = Counter()
    if not run_ids:
        return completed_by_run, shell_total, shell_nonzero, incomplete, exit_codes

    con = _connect_ro(index_path)
    if con is None:
        return completed_by_run, shell_total, shell_nonzero, incomplete, exit_codes

    placeholders = ",".join("?" * len(run_ids))
    call_states: dict[str, dict] = {}
    try:
        rows = con.execute(
            f"""
            SELECT run_id, payload_json FROM run_events
            WHERE run_id IN ({placeholders}) AND payload_json IS NOT NULL
            """,
            tuple(run_ids),
        ).fetchall()
    except sqlite3.Error as exc:
        log.debug("run_events query failed for %s: %s", index_path, exc)
        con.close()
        return completed_by_run, shell_total, shell_nonzero, incomplete, exit_codes

    for run_id, payload in rows:
        if run_id not in run_ids:
            continue
        try:
            obj = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        if not isinstance(msg, dict) or msg.get("type") != "tool_call":
            continue
        name = str(msg.get("name") or "?")
        status = msg.get("status")
        call_id = msg.get("call_id")
        if call_id:
            st = call_states.setdefault(
                str(call_id),
                {"name": name, "run_id": run_id, "statuses": set()},
            )
            st["statuses"].add(status)

        if status != "completed":
            continue
        completed_by_run[run_id] = completed_by_run.get(run_id, 0) + 1
        result = msg.get("result") or {}
        value = result.get("value")
        if name == "shell" and isinstance(value, dict):
            shell_total += 1
            code = value.get("exitCode")
            if isinstance(code, int) and code != 0:
                shell_nonzero += 1
                exit_codes[code] += 1

    for st in call_states.values():
        statuses = st["statuses"]
        if "running" in statuses and "completed" not in statuses:
            incomplete += 1

    con.close()
    return completed_by_run, shell_total, shell_nonzero, incomplete, exit_codes


def _insight_line(
    *,
    friction_rate_pct: int | None,
    shell_nonzero: int,
    shell_total: int,
    exit_codes: Counter[int],
    incomplete_tools: int,
) -> str:
    if shell_nonzero > 0 and shell_total > 0:
        top = exit_codes.most_common(1)
        if top:
            code, count = top[0]
            rate = round(100 * shell_nonzero / shell_total)
            return f"Mostly shell exits — top code {code} ({count}×, {rate}% fail)"
        return "Shell commands often exit non-zero"
    if incomplete_tools > 0:
        return f"{incomplete_tools} tool call(s) started but never finished"
    if friction_rate_pct is not None and friction_rate_pct >= 25:
        return f"High cancel/error rate ({friction_rate_pct}%) — check prompt clarity"
    if friction_rate_pct == 0 and shell_nonzero == 0:
        return "Clean recent runs — low cancel/error and shell friction"
    return "Review prompts when cancel/error or shell exits climb"


def _format_lines(preview: SdkHabitsPreview) -> tuple[str, ...]:
    if not preview.available:
        return (preview.error_message or "Unavailable",)

    total = preview.runs_considered
    status = (
        f"Runs ({preview.window_label}): "
        f"{preview.finished} ok · {preview.cancelled} cancel · {preview.error} err"
    )
    if preview.friction_rate_pct is not None:
        status += f" · {preview.friction_rate_pct}% friction"

    if preview.median_total_tokens is not None:
        tokens = f"Median tokens/run: {preview.median_total_tokens:,}"
    else:
        tokens = "Median tokens/run: —"

    if preview.median_tools_per_run is not None:
        tools = f"Median tools/run: {preview.median_tools_per_run:g}"
    else:
        tools = "Median tools/run: —"
    if preview.shell_total:
        shell_rate = round(100 * preview.shell_nonzero / preview.shell_total)
        tools += f" · shell ≠0 {preview.shell_nonzero}/{preview.shell_total} ({shell_rate}%)"
    else:
        tools += " · shell ≠0 0"

    incomplete = f"Incomplete tools: {preview.incomplete_tools}"
    lines = [status, tokens, tools, incomplete]
    if preview.insight:
        lines.append(preview.insight)
    if total == 0:
        return ("No recent SDK runs found",)
    return tuple(lines)


def _aggregate_batch(
    selected: list[RunRow],
    *,
    label: str,
) -> tuple[SdkHabitsBatch, Counter[int]]:
    """Aggregate one run window; also return exit-code counts for insights."""
    status_counts: Counter[str] = Counter()
    token_values: list[int] = []
    by_index: dict[Path, set[str]] = {}
    started_at: str | None = None
    ended_at: str | None = None

    for run_id, status, usage_json, created_at, index_path in selected:
        key = status.upper() if status else "UNKNOWN"
        status_counts[key] += 1
        tok = _token_total(usage_json)
        if tok is not None:
            token_values.append(tok)
        by_index.setdefault(index_path, set()).add(run_id)
        if created_at:
            if started_at is None or _parse_ts(created_at) < _parse_ts(started_at):
                started_at = created_at
            if ended_at is None or _parse_ts(created_at) > _parse_ts(ended_at):
                ended_at = created_at

    tools_per_run: list[int] = []
    shell_total = 0
    shell_nonzero = 0
    incomplete_tools = 0
    exit_codes: Counter[int] = Counter()
    for index_path, run_ids in by_index.items():
        completed, st, sn, inc, exits = _tool_stats_for_runs(index_path, run_ids)
        tools_per_run.extend(completed.values())
        shell_total += st
        shell_nonzero += sn
        incomplete_tools += inc
        exit_codes.update(exits)

    finished = status_counts.get("FINISHED", 0)
    cancelled = status_counts.get("CANCELLED", 0)
    error = status_counts.get("ERROR", 0)
    known = finished + cancelled + error
    friction_n = cancelled + error
    friction_rate = round(100 * friction_n / known) if known else None
    shell_fail = (
        round(100 * shell_nonzero / shell_total) if shell_total else None
    )

    batch = SdkHabitsBatch(
        label=label,
        runs=len(selected),
        started_at=started_at,
        ended_at=ended_at,
        finished=finished,
        cancelled=cancelled,
        error=error,
        friction_rate_pct=friction_rate,
        shell_fail_pct=shell_fail,
        shell_nonzero=shell_nonzero,
        shell_total=shell_total,
        median_total_tokens=_median_int(token_values),
        median_tools_per_run=_median_float(tools_per_run),
        incomplete_tools=incomplete_tools,
    )
    return batch, exit_codes


def _history_windows_by_period(
    all_runs: list[RunRow],
    *,
    renewal_day: int,
) -> list[tuple[date, list[RunRow]]]:
    """Group runs into subscription billing periods; oldest → newest."""
    if not all_runs:
        return []
    groups: dict[date, list[RunRow]] = {}
    for row in all_runs:
        start = period_start_for(_parse_ts(row[3]), renewal_day=renewal_day)
        groups.setdefault(start, []).append(row)
    out: list[tuple[date, list[RunRow]]] = []
    for start in sorted(groups):
        window = sorted(groups[start], key=lambda r: _parse_ts(r[3]), reverse=True)
        out.append((start, window))
    return out


def collect_habits_preview(
    *,
    projects_root: Path | None = None,
    recent_runs: int = DEFAULT_RECENT_RUNS,
    renewal_day: int = SUBSCRIPTION_RENEWAL_DAY,
) -> SdkHabitsPreview:
    """Aggregate a scannable preview plus per-billing-period history."""
    del recent_runs  # current period defines the snapshot window
    root = (projects_root or DEFAULT_PROJECTS_ROOT).expanduser()
    try:
        root = root.resolve()
    except OSError:
        return SdkHabitsPreview.unavailable("SDK projects path unavailable")

    indexes = _iter_index_dbs(root)
    if not indexes:
        return SdkHabitsPreview.unavailable("No local SDK indexes found")

    all_runs: list[RunRow] = []
    for index_path in indexes:
        all_runs.extend(_load_runs(index_path))

    if not all_runs:
        return SdkHabitsPreview(
            available=True,
            indexes_scanned=len(indexes),
            runs_considered=0,
            window_label="current billing period",
            insight="",
            lines=("No recent SDK runs found",),
        )

    windows = _history_windows_by_period(all_runs, renewal_day=renewal_day)
    history: list[SdkHabitsBatch] = []
    latest_exits: Counter[int] = Counter()
    for start, window in windows:
        label = period_label(start, renewal_day=renewal_day)
        batch, exits = _aggregate_batch(window, label=label)
        history.append(batch)
        latest_exits = exits

    latest = history[-1]
    latest_start = windows[-1][0]
    window_label = (
        f"{short_period_label(latest_start)} period · {latest.runs} runs"
    )
    insight = _insight_line(
        friction_rate_pct=latest.friction_rate_pct,
        shell_nonzero=latest.shell_nonzero,
        shell_total=latest.shell_total,
        exit_codes=latest_exits,
        incomplete_tools=latest.incomplete_tools,
    )
    known = latest.finished + latest.cancelled + latest.error
    other = max(0, latest.runs - known)
    preview = SdkHabitsPreview(
        available=True,
        window_label=window_label,
        indexes_scanned=len(indexes),
        runs_considered=latest.runs,
        finished=latest.finished,
        cancelled=latest.cancelled,
        error=latest.error,
        other_status=other,
        friction_rate_pct=latest.friction_rate_pct,
        median_total_tokens=latest.median_total_tokens,
        median_tools_per_run=latest.median_tools_per_run,
        shell_nonzero=latest.shell_nonzero,
        shell_total=latest.shell_total,
        incomplete_tools=latest.incomplete_tools,
        insight=insight,
        history=tuple(history),
    )
    return replace(preview, lines=_format_lines(preview))
