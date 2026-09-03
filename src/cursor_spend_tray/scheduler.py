from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from .config import AppConfig, UsageSnapshot
from .scraper import SpendingScraper

log = logging.getLogger(__name__)


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


class RefreshScheduler(QObject):
    """Owns poll interval, countdown, and click-to-refresh."""

    snapshot_updated = pyqtSignal(object)
    seconds_changed = pyqtSignal(int)
    status_changed = pyqtSignal(str)
    refreshing_changed = pyqtSignal(bool)

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._worker: _ScrapeWorker | None = None
        self._deadline = time.monotonic() + config.poll_seconds
        self._tick = QTimer(self)
        self._tick.setInterval(250)
        self._tick.timeout.connect(self._on_tick)

    def start(self) -> None:
        self._tick.start()
        self._emit_seconds()
        self.refresh()

    def stop(self) -> None:
        self._tick.stop()
        if self._worker and self._worker.isRunning():
            self._worker.wait(3000)

    def remaining_seconds(self) -> int:
        return max(0, int(round(self._deadline - time.monotonic())))

    def refresh(self) -> None:
        if self._worker and self._worker.isRunning():
            self.status_changed.emit("Refresh already in progress…")
            return
        # Pause countdown expiry until this run finishes
        self._deadline = time.monotonic() + self.config.poll_seconds
        self.refreshing_changed.emit(True)
        self.status_changed.emit("Refreshing…")
        self._worker = _ScrapeWorker(self.config)
        self._worker.finished_ok.connect(self._on_ok)
        self._worker.finished_err.connect(self._on_err)
        self._worker.start()

    def _arm_next(self) -> None:
        self._deadline = time.monotonic() + self.config.poll_seconds
        self._emit_seconds()

    def _on_ok(self, snap: object) -> None:
        assert isinstance(snap, UsageSnapshot)
        self.refreshing_changed.emit(False)
        self._arm_next()
        if snap.error and snap.cursor_models_pct is None and snap.other_models_pct is None:
            self.status_changed.emit(snap.error)
        elif snap.error:
            self.status_changed.emit(f"Partial/cached data — {snap.error}")
        else:
            self.status_changed.emit("Updated")
        self.snapshot_updated.emit(snap)

    def _on_err(self, message: str) -> None:
        self.refreshing_changed.emit(False)
        self._arm_next()
        self.status_changed.emit(message)

    def _on_tick(self) -> None:
        self._emit_seconds()
        if self.remaining_seconds() <= 0 and not (self._worker and self._worker.isRunning()):
            self.refresh()

    def _emit_seconds(self) -> None:
        self.seconds_changed.emit(self.remaining_seconds())
