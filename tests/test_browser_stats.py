"""Unit tests for browser resource scoring helpers."""

from __future__ import annotations

from cursor_spend_tray.browser_stats import (
    BrowserRunStats,
    average_runs,
    efficiency_score,
    pick_most_efficient,
)


def _run(*, mem_mb: float, cpu: float, samples: int = 2) -> BrowserRunStats:
    return BrowserRunStats(
        started_at=1.0,
        last_sample_at=2.0,
        cpu_seconds=cpu,
        wall_seconds=1.0,
        peak_cpu_pct=cpu,
        peak_mem_bytes=int(mem_mb * 1024 * 1024),
        mem_bytes_sum=int(mem_mb * 1024 * 1024) * samples,
        samples=samples,
    )


def test_efficiency_score_prefers_lower_memory_and_cpu() -> None:
    light = _run(mem_mb=400, cpu=2.0)
    heavy = _run(mem_mb=900, cpu=8.0)
    assert efficiency_score(light) < efficiency_score(heavy)


def test_average_runs_means_usable_samples() -> None:
    a = _run(mem_mb=400, cpu=2.0)
    b = _run(mem_mb=600, cpu=4.0)
    avg = average_runs([a, b])
    assert avg is not None
    assert avg.peak_mem_bytes == int(500 * 1024 * 1024)
    assert avg.peak_cpu_pct == 3.0


def test_pick_most_efficient_requires_usable_runs() -> None:
    winner = pick_most_efficient(
        {
            "brave": [_run(mem_mb=800, cpu=5.0), _run(mem_mb=820, cpu=4.5)],
            "helium": [_run(mem_mb=500, cpu=3.0), _run(mem_mb=480, cpu=2.5)],
            "chrome": [_run(mem_mb=900, cpu=1.0, samples=1)],  # not usable
        }
    )
    assert winner is not None
    key, avg = winner
    assert key == "helium"
    assert avg.usable
