"""Tests for safe QThread quit helpers."""

from __future__ import annotations

import sys
import time

from PyQt6.QtCore import QCoreApplication, QThread, pyqtSignal

from cursor_spend_tray.qt_threads import stop_qthread


class _BusyWorker(QThread):
    finished_ok = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self._stop:
            time.sleep(0.05)
        self.finished_ok.emit()


def test_stop_qthread_waits_for_cooperative_exit() -> None:
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    worker = _BusyWorker()
    worker.start()
    assert worker.isRunning()
    stop_qthread(
        worker,
        wait_ms=3_000,
        label="test worker",
        request_stop=worker.request_stop,
        signals=(worker.finished_ok, worker.finished),
    )
    assert not worker.isRunning()
    # Keep app referenced so Qt does not tear down mid-assertion.
    assert app is not None
