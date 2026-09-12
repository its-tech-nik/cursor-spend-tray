from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from .bidi_client import BidiClient
from .browser import BrowserFamily
from .cdp_client import CdpClient
from .config import AppConfig, UsageSnapshot
from .scraper import SpendingScraper

log = logging.getLogger(__name__)

# Quiet check while paused — not a spending scrape, just “is Remote Agent up?”
_PROBE_INTERVAL_MS = 30_000
# Launch this many seconds before the poll deadline in quit-between mode so the
# scrape can start on time (matches TrayApp._LAUNCH_SETTLE_MS).
BROWSER_WARMUP_SECONDS = 10


class _ScrapeWorker(QThread):
    finished_ok = pyqtSignal(object)
    finished_err = pyqtSignal(str)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    def run(self) -> None:
        try:
            snap = SpendingScraper(self._config).fetch_sync()
            self.finished_ok.emit(snap)
        except Exception as exc:
            log.exception("Worker scrape failed")
            self.finished_err.emit(str(exc))


class _ProbeWorker(QThread):
    """Lightweight BiDi reachability check (no page scrape)."""

    finished_available = pyqtSignal(bool)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    def run(self) -> None:
        try:
            browser = self._config.browser
            if browser.family is BrowserFamily.CHROMIUM:
                client: BidiClient | CdpClient = CdpClient(
                    self._config.bidi_host, self._config.bidi_port
                )
            else:
                client = BidiClient(self._config.bidi_host, self._config.bidi_port)
            ok = asyncio.run(client.is_available())
        except Exception:
            log.exception("Probe failed")
            ok = False
        self.finished_available.emit(ok)


class RefreshScheduler(QObject):
    """Owns poll interval, countdown, and click-to-refresh."""

    snapshot_updated = pyqtSignal(object)
    seconds_changed = pyqtSignal(int)
    status_changed = pyqtSignal(str)
    refreshing_changed = pyqtSignal(bool)
    # Fired once per poll cycle when quit-between should pre-launch the browser.
    browser_warmup_requested = pyqtSignal()

    def __init__(
        self,
        config: AppConfig,
        parent: QObject | None = None,
        *,
        ensure_ready: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._ensure_ready = ensure_ready
        self._worker: _ScrapeWorker | None = None
        self._probe_worker: _ProbeWorker | None = None
        self._deadline = time.monotonic() + config.poll_seconds
        self._paused = False
        self._awaiting_ready = False
        self._warmup_fired = False
        self._tick = QTimer(self)
        self._tick.setInterval(250)
        self._tick.timeout.connect(self._on_tick)
        self._probe = QTimer(self)
        self._probe.setInterval(_PROBE_INTERVAL_MS)
        self._probe.timeout.connect(self._on_probe)

    def start(self, *, refresh: bool = True) -> None:
        self._tick.start()
        self._emit_seconds()
        if refresh:
            self.refresh()

    def stop(self) -> None:
        self._tick.stop()
        self._stop_probe()
        if self._worker and self._worker.isRunning():
            self._worker.wait(3000)
        if self._probe_worker and self._probe_worker.isRunning():
            self._probe_worker.wait(3000)

    def remaining_seconds(self) -> int:
        if self._paused:
            return -1
        return max(0, int(round(self._deadline - time.monotonic())))

    def is_refreshing(self) -> bool:
        return bool(self._worker and self._worker.isRunning())

    def cancel_awaiting_ready(self) -> None:
        """Clear the browser-launch gate so a later refresh can run."""
        self._awaiting_ready = False

    def pause_for_login(self) -> None:
        """Stop the countdown until sign-in (manual refresh or network auto-detect)."""
        self._paused = True
        self._stop_probe()
        self._warmup_fired = True
        self._emit_seconds()

    def refresh(self) -> None:
        if self._worker and self._worker.isRunning():
            self.status_changed.emit("Refresh already in progress…")
            return
        if self._awaiting_ready:
            return

        def start_worker() -> None:
            self._awaiting_ready = False
            self._warmup_fired = True  # scrape owns this cycle; no more warmup
            # While a manual/auto scrape runs, keep the countdown from firing again.
            self._deadline = time.monotonic() + self.config.poll_seconds
            self.refreshing_changed.emit(True)
            self.status_changed.emit("Refreshing…")
            self._worker = _ScrapeWorker(self.config)
            self._worker.finished_ok.connect(self._on_ok)
            self._worker.finished_err.connect(self._on_err)
            self._worker.start()

        if self._ensure_ready is not None:
            self._awaiting_ready = True
            self._ensure_ready(start_worker)
        else:
            start_worker()

    def set_poll_seconds(self, seconds: int) -> None:
        """Update the poll interval, persist it, and re-arm the countdown."""
        seconds = max(60, int(seconds))
        self.config.poll_seconds = seconds
        self.config.save()
        if not self._paused and not (self._worker and self._worker.isRunning()):
            self._deadline = time.monotonic() + seconds
            self._warmup_fired = False
            self._emit_seconds()

    def _arm_next(self) -> None:
        self._paused = False
        self._stop_probe()
        self._deadline = time.monotonic() + self.config.poll_seconds
        self._warmup_fired = False
        self._emit_seconds()

    def probe_or_refresh(
        self,
        on_unavailable: "Callable[[], None] | None" = None,
        on_available: "Callable[[], None] | None" = None,
    ) -> None:
        """Lightweight BiDi check; if available trigger a full refresh, else call on_unavailable."""
        if self._worker and self._worker.isRunning():
            return
        if self._probe_worker and self._probe_worker.isRunning():
            return
        self._probe_on_unavailable = on_unavailable
        self._probe_on_available = on_available
        pw = _ProbeWorker(self.config)
        pw.finished_available.connect(self._on_probe_or_refresh_result)
        pw.start()
        self._probe_worker = pw

    def _on_probe_or_refresh_result(self, available: bool) -> None:
        unavail_cb = getattr(self, "_probe_on_unavailable", None)
        avail_cb = getattr(self, "_probe_on_available", None)
        self._probe_on_unavailable = None
        self._probe_on_available = None
        if available:
            if avail_cb is not None:
                avail_cb()
            else:
                self.refresh()
        elif unavail_cb is not None:
            unavail_cb()

    def _start_probe(self) -> None:
        if not self._probe.isActive():
            self._probe.start()

    def _stop_probe(self) -> None:
        self._probe.stop()

    def _on_ok(self, snap: object) -> None:
        assert isinstance(snap, UsageSnapshot)
        if snap.source == "unavailable":
            # Mark paused before clearing refreshing so the countdown stays hidden.
            self._paused = True
            self.refreshing_changed.emit(False)
            self._emit_seconds()
            self._start_probe()
            self.status_changed.emit(
                f"Browser inaccessible — waiting for {self.config.browser.display_name} "
                "with remote debugging. Copy the launch command below, then relaunch."
            )
        elif snap.source == "logged_out":
            # No auto-poll while signed out — hide countdown; network watch or manual refresh.
            self.pause_for_login()
            self.refreshing_changed.emit(False)
            self.status_changed.emit(
                snap.error
                or "Sign in to Cursor in the dedicated browser window "
                "(refresh runs automatically when the dashboard loads)."
            )
        else:
            self.refreshing_changed.emit(False)
            self._arm_next()
            if snap.error and snap.cursor_models_pct is None and snap.other_models_pct is None:
                self.status_changed.emit(snap.error)
            elif snap.error:
                self.status_changed.emit(f"Partial/cached data — {snap.error}")
            else:
                # Clear the footer on success — no "Updated" label.
                self.status_changed.emit("")
        self.snapshot_updated.emit(snap)

    def _on_err(self, message: str) -> None:
        self.refreshing_changed.emit(False)
        self._arm_next()
        self.status_changed.emit(message)

    def _on_tick(self) -> None:
        if self._paused:
            return
        self._emit_seconds()
        remaining = self.remaining_seconds()
        if (
            not self._warmup_fired
            and self.config.between_scrapes == "quit"
            and 0 < remaining <= BROWSER_WARMUP_SECONDS
            and not self.is_refreshing()
            and not self._awaiting_ready
        ):
            self._warmup_fired = True
            self.browser_warmup_requested.emit()
        if remaining <= 0 and not self.is_refreshing():
            self.refresh()

    def _on_probe(self) -> None:
        if not self._paused:
            self._stop_probe()
            return
        if self._worker and self._worker.isRunning():
            return
        if self._probe_worker and self._probe_worker.isRunning():
            return
        self._probe_worker = _ProbeWorker(self.config)
        self._probe_worker.finished_available.connect(self._on_probe_result)
        self._probe_worker.start()

    def _on_probe_result(self, available: bool) -> None:
        if not self._paused:
            return
        if not available:
            return
        self.status_changed.emit("Browser available again — refreshing…")
        self.refresh()

    def _emit_seconds(self) -> None:
        self.seconds_changed.emit(self.remaining_seconds())
