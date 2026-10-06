"""Sample CPU / memory of the headless automation browser during scrapes."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime

from PyQt6.QtCore import QObject, QTimer

from .browser import automation_main_procs
from .config import APP_NAME, AppConfig, data_dir

log = logging.getLogger(__name__)

_SAMPLE_MS = 1_000
# Baseline + at least one later sample, so CPU has a delta.
_MIN_SAMPLES = 2

_CLK_TCK = os.sysconf("SC_CLK_TCK")
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
_NCPU = os.cpu_count() or 1


@dataclass
class BrowserRunStats:
    """Resource usage of the automation browser's process tree during one scrape."""

    started_at: float
    last_sample_at: float
    cpu_seconds: float = 0.0
    wall_seconds: float = 0.0
    peak_cpu_pct: float = 0.0
    peak_mem_bytes: int = 0
    mem_bytes_sum: int = 0
    samples: int = 0

    @property
    def avg_cpu_pct(self) -> float:
        """Share of total machine CPU (all cores = 100%)."""
        if self.wall_seconds <= 0:
            return 0.0
        return self.cpu_seconds / (self.wall_seconds * _NCPU) * 100.0

    @property
    def avg_mem_bytes(self) -> int:
        return self.mem_bytes_sum // self.samples if self.samples else 0

    @property
    def usable(self) -> bool:
        return self.samples >= _MIN_SAMPLES and self.wall_seconds > 0


def _stats_path():
    return data_dir() / "browser_stats.json"


def _load_stats() -> dict[str, BrowserRunStats]:
    path = _stats_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.debug("Could not read %s: %s", path, exc)
        return {}
    names = {f.name for f in fields(BrowserRunStats)}
    out: dict[str, BrowserRunStats] = {}
    for key, row in raw.items() if isinstance(raw, dict) else ():
        if not isinstance(row, dict):
            continue
        try:
            out[str(key)] = BrowserRunStats(**{k: v for k, v in row.items() if k in names})
        except TypeError:
            continue
    return out


def _save_stats(stats: dict[str, BrowserRunStats]) -> None:
    path = _stats_path()
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({k: asdict(v) for k, v in stats.items()}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        log.debug("Could not write %s: %s", path, exc)


def _proc_table() -> dict[int, tuple[int, int]]:
    """pid → (ppid, utime+stime ticks)."""
    table: dict[int, tuple[int, int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="ascii", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        # comm may contain spaces/parens; fields resume after the last ')'.
        rest = raw.rpartition(")")[2].split()
        try:
            table[int(name)] = (int(rest[1]), int(rest[11]) + int(rest[12]))
        except (IndexError, ValueError):
            continue
    return table


def _tree_pids(roots: frozenset[int], table: dict[int, tuple[int, int]]) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _ticks) in table.items():
        children.setdefault(ppid, []).append(pid)
    out: list[int] = []
    stack = [pid for pid in roots if pid in table]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(children.get(pid, ()))
    return out


def _pss_bytes(pid: int) -> int:
    """Proportional set size, so memory shared between browser processes counts once."""
    try:
        with open(f"/proc/{pid}/smaps_rollup", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    try:
        with open(f"/proc/{pid}/statm", encoding="ascii") as fh:
            return int(fh.read().split()[1]) * _PAGE_SIZE
    except (OSError, ValueError, IndexError):
        return 0


def _fmt_mem(n: int) -> str:
    mb = n / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


def _fmt_pct(p: float) -> str:
    return f"{p:.1f}%" if p < 10 else f"{p:.0f}%"


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    return f"{s // 60} min {s % 60} s"


def format_run_summary(run: BrowserRunStats) -> str:
    return f"{_fmt_mem(run.peak_mem_bytes)} · {_fmt_pct(run.avg_cpu_pct)} CPU"


def format_run_tooltip(run: BrowserRunStats) -> str:
    when = datetime.fromtimestamp(run.started_at).strftime("%b %d %H:%M")
    return (
        f"Last scrape ({when}, took {_fmt_duration(run.wall_seconds)})\n"
        f"CPU: avg {_fmt_pct(run.avg_cpu_pct)}, peak {_fmt_pct(run.peak_cpu_pct)} "
        f"of all {_NCPU} cores\n"
        f"Memory: avg {_fmt_mem(run.avg_mem_bytes)}, peak {_fmt_mem(run.peak_mem_bytes)}"
    )


class BrowserResourceMonitor(QObject):
    """Samples the headless automation browser's process tree while a scrape runs."""

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._stats = _load_stats()
        self._run: BrowserRunStats | None = None
        self._run_key: str | None = None
        self._prev_ticks: dict[int, int] = {}
        self._prev_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(_SAMPLE_MS)
        self._timer.timeout.connect(self._sample)

    def last_run(self, browser_key: str) -> BrowserRunStats | None:
        return self._stats.get(browser_key)

    def begin_scrape(self) -> None:
        self._discard_run()
        info = self._config.browser
        procs = automation_main_procs(info, app_name=APP_NAME)
        # Some Chromium forks rewrite argv into a single string; match on the join.
        if not any("--headless" in " ".join(parts) for _pid, parts in procs):
            return
        now = time.time()
        self._run_key = info.key
        self._run = BrowserRunStats(started_at=now, last_sample_at=now)
        self._prev_at = now
        self._sample(baseline=True)
        self._timer.start()

    def end_scrape(self) -> None:
        if self._run is None:
            return
        self._timer.stop()
        self._sample()
        run, key = self._run, self._run_key
        self._discard_run()
        if run is None or key is None or not run.usable:
            return
        self._stats[key] = run
        _save_stats(self._stats)

    def stop(self) -> None:
        self._discard_run()

    def _discard_run(self) -> None:
        self._timer.stop()
        self._run = None
        self._run_key = None
        self._prev_ticks = {}

    def _sample(self, *, baseline: bool = False) -> None:
        run = self._run
        if run is None:
            return
        procs = automation_main_procs(self._config.browser, app_name=APP_NAME)
        if not procs:
            return
        now = time.time()
        table = _proc_table()
        tree = _tree_pids(frozenset(pid for pid, _parts in procs), table)
        ticks = {pid: table[pid][1] for pid in tree}
        mem = sum(_pss_bytes(pid) for pid in tree)

        if not baseline:
            # Processes spawned mid-scrape have no baseline, so all their ticks count.
            delta = sum(max(0, t - self._prev_ticks.get(pid, 0)) for pid, t in ticks.items())
            dt = max(0.0, now - self._prev_at)
            run.cpu_seconds += delta / _CLK_TCK
            run.wall_seconds += dt
            if dt > 0:
                run.peak_cpu_pct = max(
                    run.peak_cpu_pct, delta / _CLK_TCK / (dt * _NCPU) * 100.0
                )
        run.peak_mem_bytes = max(run.peak_mem_bytes, mem)
        run.mem_bytes_sum += mem
        run.samples += 1
        run.last_sample_at = now
        self._prev_ticks = ticks
        self._prev_at = now
