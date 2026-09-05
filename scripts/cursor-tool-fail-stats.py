#!/usr/bin/env python3
"""Aggregate tool-call outcomes from local Cursor SDK run indexes.

Cursor's agent-transcripts/*.jsonl files only store tool *requests* (tool_use),
not results. Per-run outcomes live in:

  ~/.cursor/projects/<project>/sdk-agent-store/**/index.db
    tables: runs, run_events (event_type=run_stream_event, tool_call messages)

The host usually marks completed tools as result.status=success even when a
shell command exits non-zero. This script therefore reports:

  - completed tool counts by name
  - shell exitCode != 0 (practical failures)
  - tool calls that started (running) but never completed
  - any result.status other than success (rare)

Usage:
  ./scripts/cursor-tool-fail-stats.py
  ./scripts/cursor-tool-fail-stats.py --project cursor-spend-tray
  ./scripts/cursor-tool-fail-stats.py --root ~/.cursor/projects --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _project_slug(index_path: Path, projects_root: Path) -> str:
    try:
        rel = index_path.relative_to(projects_root)
        return rel.parts[0]
    except ValueError:
        return index_path.parent.name


def _iter_tool_messages(index_path: Path):
    uri = f"file:{index_path}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return
    try:
        rows = con.execute(
            "SELECT run_id, payload_json FROM run_events WHERE payload_json IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        con.close()
        return
    for run_id, payload in rows:
        try:
            obj = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        if isinstance(msg, dict) and msg.get("type") == "tool_call":
            yield run_id, msg
    con.close()


def analyze(projects_root: Path, project_filter: str | None = None) -> dict:
    indexes = sorted(projects_root.rglob("index.db"))
    # Prefer sdk-agent-store indexes (have runs/run_events).
    indexes = [p for p in indexes if "sdk-agent-store" in p.parts]

    completed_by_tool: Counter[str] = Counter()
    result_status: Counter[str] = Counter()
    shell_exits: Counter[int] = Counter()
    shell_fail_by_project: Counter[str] = Counter()
    never_completed_by_tool: Counter[str] = Counter()
    nonsuccess_examples: list[dict] = []
    shell_fail_examples: list[dict] = []
    call_states: dict[str, dict] = {}
    runs_seen: set[str] = set()
    indexes_used = 0

    for index_path in indexes:
        project = _project_slug(index_path, projects_root)
        if project_filter and project_filter not in project:
            continue
        indexes_used += 1
        for run_id, msg in _iter_tool_messages(index_path):
            runs_seen.add(run_id)
            name = str(msg.get("name") or "?")
            status = msg.get("status")
            call_id = msg.get("call_id")
            if call_id:
                st = call_states.setdefault(
                    call_id,
                    {
                        "name": name,
                        "project": project,
                        "run_id": run_id,
                        "statuses": set(),
                    },
                )
                st["statuses"].add(status)

            if status != "completed":
                continue

            completed_by_tool[name] += 1
            result = msg.get("result") or {}
            rs = result.get("status")
            result_status[str(rs)] += 1
            if rs and rs != "success":
                if len(nonsuccess_examples) < 20:
                    nonsuccess_examples.append(
                        {
                            "project": project,
                            "run_id": run_id,
                            "tool": name,
                            "result.status": rs,
                            "value": result.get("value"),
                        }
                    )

            value = result.get("value")
            if name == "shell" and isinstance(value, dict):
                code = value.get("exitCode")
                if isinstance(code, int) and code != 0:
                    shell_exits[code] += 1
                    shell_fail_by_project[project] += 1
                    if len(shell_fail_examples) < 30:
                        shell_fail_examples.append(
                            {
                                "project": project,
                                "run_id": run_id,
                                "exitCode": code,
                                "stderr": (value.get("stderr") or "")[:400],
                                "stdout": (value.get("stdout") or "")[:200],
                                "args": msg.get("args"),
                            }
                        )

    for st in call_states.values():
        if "running" in st["statuses"] and "completed" not in st["statuses"]:
            never_completed_by_tool[st["name"]] += 1

    return {
        "projects_root": str(projects_root),
        "indexes_scanned": indexes_used,
        "unique_runs": len(runs_seen),
        "completed_tool_calls": sum(completed_by_tool.values()),
        "completed_by_tool": dict(completed_by_tool.most_common()),
        "result_status": dict(result_status),
        "shell_nonzero_exits": {
            "total": sum(shell_exits.values()),
            "by_exit_code": {str(k): v for k, v in sorted(shell_exits.items())},
            "by_project": dict(shell_fail_by_project.most_common()),
            "examples": shell_fail_examples,
        },
        "never_completed": {
            "total": sum(never_completed_by_tool.values()),
            "by_tool": dict(never_completed_by_tool.most_common()),
        },
        "nonsuccess_result_status_examples": nonsuccess_examples,
        "notes": [
            "agent-transcripts/*.jsonl only contain tool_use requests, not outcomes.",
            "Cursor marks most completed tools as result.status=success; shell failures show up as exitCode != 0.",
        ],
    }


def _print_human(report: dict) -> None:
    print(f"Scanned {report['indexes_scanned']} sdk-agent-store index.db under {report['projects_root']}")
    print(f"Unique runs: {report['unique_runs']}")
    print(f"Completed tool calls: {report['completed_tool_calls']}")
    print("\nCompleted by tool:")
    for name, count in report["completed_by_tool"].items():
        print(f"  {count:5d}  {name}")

    print("\nresult.status:")
    for status, count in report["result_status"].items():
        print(f"  {count:5d}  {status}")

    shell = report["shell_nonzero_exits"]
    print(f"\nShell non-zero exits: {shell['total']}")
    if shell["by_exit_code"]:
        print("  by exit code:")
        for code, count in shell["by_exit_code"].items():
            print(f"    {count:5d}  exit {code}")
    if shell["by_project"]:
        print("  by project:")
        for project, count in shell["by_project"].items():
            print(f"    {count:5d}  {project}")

    never = report["never_completed"]
    print(f"\nStarted but never completed: {never['total']}")
    for name, count in never["by_tool"].items():
        print(f"  {count:5d}  {name}")

    if shell["examples"]:
        print("\nSample shell failures:")
        for ex in shell["examples"][:10]:
            cmd = ""
            args = ex.get("args") or {}
            if isinstance(args, dict):
                cmd = args.get("command") or args.get("cmd") or ""
            print(f"  [{ex['project']}] exit {ex['exitCode']}: {cmd[:120]}")
            err = (ex.get("stderr") or "").strip().replace("\n", " | ")
            if err:
                print(f"      stderr: {err[:200]}")

    for note in report["notes"]:
        print(f"\nNote: {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".cursor" / "projects",
        help="Cursor projects directory (default: ~/.cursor/projects)",
    )
    parser.add_argument(
        "--project",
        help="Substring filter on project folder name (e.g. cursor-spend-tray)",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"Projects root not found: {root}", file=sys.stderr)
        return 1

    report = analyze(root, args.project)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
