"""Watch browser network traffic while signed out; auto-complete login.

Only active during the headed sign-in wait. Chromium uses CDP Network events;
Firefox-family uses WebDriver BiDi network events. A successful usage-dashboard
API response (or a return to the dashboard after an auth hop) means the session
is likely ready: the tray switches to headless and scrapes without a manual refresh.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .auth_detect import url_suggests_auth
from .bidi_client import BidiClient, BidiError
from .browser import BrowserFamily
from .cdp_client import CdpClient, CdpError
from .config import AppConfig

log = logging.getLogger(__name__)

# Authenticated dashboard fetches — not the bare spending HTML redirect.
_USAGE_API_MARKERS: tuple[str, ...] = (
    "cursor.com/api/dashboard",
)

_DASHBOARD_PAGE_MARKERS: tuple[str, ...] = (
    "cursor.com/dashboard/spending",
    "cursor.com/dashboard/usage",
    "cursor.com/dashboard/settings",
)

# Wait for remote debugging after headed relaunch before giving up.
_CONNECT_RETRY_SECONDS = 90.0
_CONNECT_RETRY_SLEEP = 1.0


def url_looks_like_auth(url: str) -> bool:
    """True when the URL is a Cursor auth / accounts host (or login path)."""
    return url_suggests_auth(url)


def url_looks_like_usage_api(url: str) -> bool:
    """True for Cursor usage/dashboard API requests."""
    lowered = (url or "").strip().lower()
    return any(marker in lowered for marker in _USAGE_API_MARKERS)


def url_looks_like_dashboard_page(url: str) -> bool:
    """True for main dashboard document URLs (not authenticator)."""
    lowered = (url or "").strip().lower()
    if url_looks_like_auth(lowered):
        return False
    host = (urlparse(lowered).hostname or "").lower()
    if host not in {"cursor.com", "www.cursor.com"}:
        return False
    return any(marker in lowered for marker in _DASHBOARD_PAGE_MARKERS)


def response_suggests_authenticated_session(
    url: str,
    status: int | None,
    *,
    saw_auth: bool,
) -> bool:
    """Heuristic: logged-in usage traffic after (or without needing) an auth hop.

    - Any 2xx/3xx ``/api/dashboard`` response is a strong signed-in signal.
    - A dashboard *page* load only counts after we already saw an auth URL in
      this watch session (avoids firing on the initial spending redirect).
    """
    if status is not None and not (200 <= int(status) < 400):
        return False
    if url_looks_like_usage_api(url):
        return True
    if saw_auth and url_looks_like_dashboard_page(url):
        return True
    return False


class _LoginNetworkWatchThread(QThread):
    """Background CDP/BiDi listener; emits once when dashboard activity is seen."""

    detected = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def request_stop(self) -> None:
        """Ask the async watcher to exit (thread-safe)."""
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)

    def run(self) -> None:
        try:
            asyncio.run(self._run_async())
        except Exception:
            log.exception("Login network watch failed")

    async def _wait_stop_or_timeout(self, timeout: float) -> bool:
        """Return True if stop was requested within timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return True
            await asyncio.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return self._stop.is_set()

    async def _run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._stop.is_set():
            return
        browser = self._config.browser
        if browser.family is BrowserFamily.CHROMIUM:
            await self._watch_cdp()
        else:
            await self._watch_bidi()

    async def _wait_available(
        self,
        client: CdpClient | BidiClient,
    ) -> bool:
        deadline = time.monotonic() + _CONNECT_RETRY_SECONDS
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            try:
                if await client.is_available():
                    return True
            except Exception:
                pass
            if await self._wait_stop_or_timeout(_CONNECT_RETRY_SLEEP):
                return False
        return False

    async def _watch_until(self, fired: asyncio.Event) -> None:
        while not self._stop.is_set() and not fired.is_set():
            await asyncio.sleep(0.15)

    async def _watch_cdp(self) -> None:
        client = CdpClient(self._config.bidi_host, self._config.bidi_port)
        if not await self._wait_available(client):
            return
        saw_auth = False
        fired = asyncio.Event()
        trigger_url = ""

        def on_event(method: str, params: dict[str, Any], session_id: str | None) -> None:
            nonlocal saw_auth, trigger_url
            if fired.is_set() or self._stop.is_set():
                return
            if method == "Target.attachedToTarget":
                sid = params.get("sessionId") or session_id
                target = params.get("targetInfo") or {}
                if sid and target.get("type") == "page":
                    asyncio.create_task(_enable_network(client, sid))
                return
            if method == "Network.responseReceived":
                response = params.get("response") or {}
                url = str(response.get("url") or "")
                status = response.get("status")
                status_i = int(status) if isinstance(status, (int, float)) else None
                if url_looks_like_auth(url):
                    saw_auth = True
                if response_suggests_authenticated_session(
                    url, status_i, saw_auth=saw_auth
                ):
                    trigger_url = url
                    fired.set()
                return
            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                url = str(req.get("url") or "")
                if url_looks_like_auth(url):
                    saw_auth = True

        async def _enable_network(c: CdpClient, sid: str) -> None:
            try:
                await c.enable_network(sid)
            except CdpError as exc:
                log.debug("Network.enable failed: %s", exc)

        client.add_event_listener(on_event)
        try:
            await client.connect()
            self.status.emit("Watching for sign-in…")
            try:
                await client.call(
                    "Target.setAutoAttach",
                    {
                        "autoAttach": True,
                        "waitForDebuggerOnStart": False,
                        "flatten": True,
                    },
                )
            except CdpError as exc:
                log.debug("Target.setAutoAttach: %s", exc)

            for target in await client.list_page_targets():
                try:
                    sid = await client.attach_page_target(target["targetId"])
                    await _enable_network(client, sid)
                except CdpError as exc:
                    log.debug("Attach/Network.enable: %s", exc)

            await self._watch_until(fired)
            if fired.is_set() and not self._stop.is_set():
                log.info("Login network watch (CDP) detected: %s", trigger_url)
                self.detected.emit(trigger_url or "usage-dashboard")
        finally:
            client.remove_event_listener(on_event)
            try:
                await client.close()
            except Exception:
                log.debug("CDP login watch close failed", exc_info=True)

    async def _watch_bidi(self) -> None:
        client = BidiClient(self._config.bidi_host, self._config.bidi_port)
        if not await self._wait_available(client):
            return
        saw_auth = False
        fired = asyncio.Event()
        trigger_url = ""

        def on_event(method: str, params: dict[str, Any], _session_id: str | None) -> None:
            nonlocal saw_auth, trigger_url
            if fired.is_set() or self._stop.is_set():
                return
            if method == "network.beforeRequestSent":
                req = params.get("request") or {}
                url = str(req.get("url") or "")
                if url_looks_like_auth(url):
                    saw_auth = True
                return
            if method == "network.responseCompleted":
                req = params.get("request") or {}
                response = params.get("response") or {}
                url = str(req.get("url") or response.get("url") or "")
                status = response.get("status")
                status_i = int(status) if isinstance(status, (int, float)) else None
                if url_looks_like_auth(url):
                    saw_auth = True
                if response_suggests_authenticated_session(
                    url, status_i, saw_auth=saw_auth
                ):
                    trigger_url = url
                    fired.set()
                return
            if method.startswith("browsingContext."):
                url = str(params.get("url") or "")
                if url_looks_like_auth(url):
                    saw_auth = True
                elif saw_auth and url_looks_like_dashboard_page(url):
                    trigger_url = url
                    fired.set()

        client.add_event_listener(on_event)
        try:
            await client.connect()
            self.status.emit("Watching for sign-in…")
            try:
                await client.call(
                    "session.subscribe",
                    {
                        "events": [
                            "network.beforeRequestSent",
                            "network.responseCompleted",
                            "browsingContext.navigationStarted",
                            "browsingContext.navigationCommitted",
                        ]
                    },
                )
            except BidiError as exc:
                log.warning("BiDi full network subscribe failed (%s); retrying subset", exc)
                await client.call(
                    "session.subscribe",
                    {
                        "events": [
                            "network.beforeRequestSent",
                            "network.responseCompleted",
                        ]
                    },
                )

            await self._watch_until(fired)
            if fired.is_set() and not self._stop.is_set():
                log.info("Login network watch (BiDi) detected: %s", trigger_url)
                self.detected.emit(trigger_url or "usage-dashboard")
        finally:
            client.remove_event_listener(on_event)
            try:
                await client.close()
            except Exception:
                log.debug("BiDi login watch close failed", exc_info=True)


class LoginNetworkWatcher(QObject):
    """Owns at most one watch thread; only run while the tray awaits sign-in."""

    detected = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._thread: _LoginNetworkWatchThread | None = None

    def is_active(self) -> bool:
        return bool(self._thread and self._thread.isRunning())

    def start(self) -> None:
        """Start watching; no-op if already running."""
        if self.is_active():
            return
        thread = _LoginNetworkWatchThread(self._config)
        thread.detected.connect(self.detected.emit)
        thread.status.connect(self.status.emit)
        thread.finished.connect(self._on_thread_finished)
        self._thread = thread
        log.info("Starting login network watch (%s)", self._config.browser.display_name)
        thread.start()

    def stop(self, *, wait_ms: int = 5_000) -> None:
        """Stop the watcher and release the BiDi/CDP session before scrapes."""
        thread = self._thread
        if thread is None:
            return
        if thread.isRunning():
            thread.request_stop()
            if not thread.wait(wait_ms):
                log.warning("Login network watch thread did not exit within %dms", wait_ms)
                thread.request_stop()
                thread.wait(1_000)
        self._thread = None

    def _on_thread_finished(self) -> None:
        if self._thread is self.sender():
            self._thread = None
