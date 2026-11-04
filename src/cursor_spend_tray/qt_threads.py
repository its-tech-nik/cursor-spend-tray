"""Safe QThread teardown helpers for process quit.

Qt aborts with a fatal error if a ``QThread`` is destroyed while still running
(e.g. as a child of a widget deleted during ``QApplication`` cleanup). These
helpers disconnect slots, detach from parents, wait briefly, and leave a stuck
thread orphaned until process exit rather than calling ``terminate()``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from PyQt6.QtCore import QThread

log = logging.getLogger(__name__)


def disconnect_signals(signals: Iterable[Any]) -> None:
    """Best-effort disconnect of every connection on each signal."""
    for signal in signals:
        try:
            signal.disconnect()
        except TypeError:
            pass


def stop_qthread(
    thread: QThread | None,
    *,
    wait_ms: int = 5_000,
    label: str = "QThread",
    request_stop: Callable[[], None] | None = None,
    signals: Iterable[Any] | None = None,
) -> None:
    """Stop ``thread`` without risking Qt's "destroyed while running" abort.

    Order:
      1. Cooperative stop (``request_stop`` or ``requestInterruption``)
      2. Disconnect slots so late finishes cannot touch dying UI
      3. Detach from any QObject parent
      4. ``wait`` up to ``wait_ms``
      5. ``deleteLater`` if finished; otherwise keep detached until process exit
    """
    if thread is None:
        return

    if request_stop is not None:
        try:
            request_stop()
        except Exception:  # noqa: BLE001 — quit path must not raise
            log.exception("request_stop failed for %s", label)
    else:
        thread.requestInterruption()

    if signals is not None:
        disconnect_signals(signals)

    # Detach before wait returns into widget teardown races.
    try:
        thread.setParent(None)
    except RuntimeError:
        # C++ object already gone.
        return

    try:
        running = thread.isRunning()
    except RuntimeError:
        return

    if not running:
        thread.deleteLater()
        return

    if thread.wait(wait_ms):
        thread.deleteLater()
        return

    log.warning(
        "%s still running after %sms; leaving detached until process exit",
        label,
        wait_ms,
    )
    try:
        thread.finished.connect(thread.deleteLater)
    except RuntimeError:
        pass
