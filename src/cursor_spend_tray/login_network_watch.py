"""Watch browser network traffic for session transitions (login / logout).

Login watch: active during the headed sign-in wait. Only a real session cookie
(``WorkosCursorSessionToken``) counts as success — not dashboard HTML loads or
opportunistic ``/api/dashboard`` calls during Cloudflare / OAuth hops. When the
cookie appears, the tray switches to headless and scrapes without a manual refresh.

Logout watch: active only while View Browser keeps a headed window open. Auth /
logout navigation (or a 401/403 on the dashboard API) flips the tray to signed-out
and opens the headed sign-in flow immediately.

Chromium uses CDP Network events; Firefox-family uses WebDriver BiDi.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Literal
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .auth_detect import url_suggests_auth
from .bidi_client import BidiClient, BidiError
from .browser import BrowserFamily
from .cdp_client import CdpClient, CdpError
from .config import AppConfig
from .session_cookie import CURSOR_SESSION_COOKIE, profile_has_cursor_session_cookie

log = logging.getLogger(__name__)

WatchMode = Literal["login", "logout"]

# Authenticated dashboard fetches — used for logout 401/403 detection.
_USAGE_API_MARKERS: tuple[str, ...] = (
    "cursor.com/api/dashboard",
)

_DASHBOARD_PAGE_MARKERS: tuple[str, ...] = (
    "cursor.com/dashboard/spending",
    "cursor.com/dashboard/usage",
    "cursor.com/dashboard/settings",
)

# Explicit sign-out endpoints / paths (any resource type).
_LOGOUT_URL_MARKERS: tuple[str, ...] = (
    "/logout",
    "/sign-out",
    "/signout",
    "sign_out",
    "signout",
)

# Wait for remote debugging after headed relaunch before giving up.
_CONNECT_RETRY_SECONDS = 90.0
_CONNECT_RETRY_SLEEP = 1.0
# How often to re-check for WorkosCursorSessionToken while awaiting sign-in.
_SESSION_COOKIE_POLL_SECONDS = 1.0


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


def url_looks_like_logout(url: str) -> bool:
    """True for explicit Cursor logout / sign-out URLs."""
    lowered = (url or "").strip().lower()
    return any(marker in lowered for marker in _LOGOUT_URL_MARKERS)


def _resource_is_document(resource_type: str | None) -> bool:
    """True for top-level document navigations (CDP type / BiDi destination)."""
    if not resource_type:
        return False
    return resource_type.strip().lower() in {"document", "main_frame", "mainframe"}


def _header_map(headers: Any) -> dict[str, str]:
    """Normalize CDP/BiDi header objects to a lower-case name → value map."""
    if not headers:
        return {}
    if isinstance(headers, dict):
        return {str(k).lower(): str(v) for k, v in headers.items()}
    if isinstance(headers, list):
        out: dict[str, str] = {}
        for item in headers:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if name is None or value is None:
                continue
            out[str(name).lower()] = str(value)
        return out
    return {}


def _cookie_header_names(cookie_header: str) -> set[str]:
    """Parse ``Cookie`` header names (exact), ignoring values."""
    names: set[str] = set()
    for part in (cookie_header or "").split(";"):
        name = part.split("=", 1)[0].strip()
        if name:
            names.add(name)
    return names


def _set_cookie_header_name(set_cookie_header: str) -> str:
    """Return the cookie name from a single ``Set-Cookie`` header value."""
    return (set_cookie_header or "").split("=", 1)[0].strip()


def headers_carry_session_cookie(headers: Any) -> bool:
    """True when Cookie / Set-Cookie set an exact WorkosCursorSessionToken name.

    Substring matches are intentionally rejected — auth pages can embed that
    token name inside other cookie *values* and previously false-triggered.
    """
    mapped = _header_map(headers)
    if CURSOR_SESSION_COOKIE in _cookie_header_names(mapped.get("cookie") or ""):
        return True
    # CDP may surface one Set-Cookie or a joined list; check each segment.
    raw_set = mapped.get("set-cookie") or ""
    for segment in raw_set.split("\n"):
        if _set_cookie_header_name(segment) == CURSOR_SESSION_COOKIE:
            return True
    if isinstance(headers, list):
        for item in headers:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").lower() != "set-cookie":
                continue
            if _set_cookie_header_name(str(item.get("value") or "")) == CURSOR_SESSION_COOKIE:
                return True
    return False


def cookies_include_session(cookies: Any) -> bool:
    """True when cookies include WorkosCursorSessionToken for the cursor.com app.

    Authenticator-only host cookies are ignored — they are not a finished login.
    """
    if not isinstance(cookies, list):
        return False
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if str(cookie.get("name") or "") != CURSOR_SESSION_COOKIE:
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        if domain in {"cursor.com", "www.cursor.com"} or domain.endswith(".cursor.com"):
            if "authenticator." in domain or "authenticate." in domain:
                continue
            return True
    return False


def response_suggests_authenticated_session(
    url: str = "",
    status: int | None = None,
    *,
    saw_auth: bool = False,
    headers: Any = None,
) -> bool:
    """True when Set-Cookie/Cookie carries a finished Cursor session token.

    Dashboard HTML and bare ``/api/dashboard`` calls are *not* enough on their
    own. Prefer combining this with a dashboard page URL check in the watcher.
    """
    _ = (url, status, saw_auth)
    return headers_carry_session_cookie(headers)


def response_suggests_logout(
    url: str,
    status: int | None,
    *,
    resource_type: str | None = None,
) -> bool:
    """Heuristic: user signed out (or session rejected) while viewing the browser.

    - Explicit logout / sign-out URLs (any resource).
    - Top-level navigation to Cursor auth / accounts (document only — avoids
      firing on incidental authenticator assets).
    - 401/403 on ``/api/dashboard``.
    """
    if url_looks_like_logout(url):
        return True
    if url_looks_like_usage_api(url) and status in {401, 403}:
        return True
    return bool(_resource_is_document(resource_type) and url_looks_like_auth(url))


def response_suggests_auth_rate_limit(url: str, status: int | None) -> bool:
    """True for HTTP 429 on Cursor authenticator / auth-callback hosts."""
    if status != 429:
        return False
    lowered = (url or "").strip().lower()
    if not lowered:
        return False
    if url_looks_like_auth(lowered):
        return True
    return any(
        marker in lowered
        for marker in (
            "authenticator.cursor.sh",
            "authenticate.cursor.sh",
            "cursor.com/api/auth",
            "cursor.sh/api/auth",
        )
    )


class _SessionNetworkWatchThread(QThread):
    """Background CDP/BiDi listener; emits once when login or logout is seen."""

    detected = pyqtSignal(str)
    rate_limited = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, config: AppConfig, *, mode: WatchMode) -> None:
        super().__init__()
        self._config = config
        self._mode: WatchMode = mode
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rate_limited = False

    def request_stop(self) -> None:
        """Ask the async watcher to exit (thread-safe)."""
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)

    def run(self) -> None:
        label = "Login" if self._mode == "login" else "Logout"
        try:
            asyncio.run(self._run_async())
        except Exception:
            log.exception("%s network watch failed", label)

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

    def _status_message(self) -> str:
        if self._mode == "logout":
            return "Watching for sign-out…"
        return "Waiting for successful Cursor sign-in…"

    def _fallback_trigger(self) -> str:
        return "logout" if self._mode == "logout" else CURSOR_SESSION_COOKIE

    async def _watch_until_with_cookie_poll(
        self,
        fired: asyncio.Event,
        *,
        poll: Any,
    ) -> str:
        """Wait until ``fired`` or ``poll()`` reports the session cookie.

        ``poll`` is an async callable returning a trigger string when the session
        cookie is present, else ``\"\"``.
        """
        while not self._stop.is_set() and not fired.is_set():
            try:
                trigger = await poll()
            except Exception:
                log.debug("Session cookie poll failed", exc_info=True)
                trigger = ""
            if trigger:
                return str(trigger)
            await self._wait_stop_or_timeout(_SESSION_COOKIE_POLL_SECONDS)
        return ""

    async def _cdp_on_dashboard_page(self, client: CdpClient) -> bool:
        """True when a page target is on the Cursor dashboard (not authenticator)."""
        try:
            targets = await client.list_page_targets()
        except CdpError as exc:
            log.debug("list_page_targets failed: %s", exc)
            return False
        return any(url_looks_like_dashboard_page(str(t.get("url") or "")) for t in targets)

    async def _cdp_session_cookie_present(self, client: CdpClient) -> str:
        """Return a trigger when session cookie exists *and* dashboard is open.

        Cookie alone is not enough: partial OAuth / authenticator traffic can
        look like a session. Closing only after the dashboard URL is visible
        matches a finished human login.
        """
        try:
            result = await client.call("Network.getAllCookies")
        except CdpError as exc:
            log.debug("Network.getAllCookies failed: %s", exc)
            return ""
        if not cookies_include_session(result.get("cookies")):
            return ""
        if not await self._cdp_on_dashboard_page(client):
            return ""
        return CURSOR_SESSION_COOKIE

    def _profile_session_cookie_present(self) -> str:
        """Return a trigger label when the dedicated profile DB has the cookie."""
        try:
            if profile_has_cursor_session_cookie(self._config.browser):
                return CURSOR_SESSION_COOKIE
        except Exception:
            log.debug("Profile session cookie check failed", exc_info=True)
        return ""

    def _emit_watch_result(self, *, mode: WatchMode, trigger_url: str) -> None:
        if self._stop.is_set():
            return
        if self._rate_limited:
            log.warning(
                "Login network watch (rate limit 429) detected: %s",
                trigger_url or "429",
            )
            self.rate_limited.emit(trigger_url or "429")
            return
        log.info(
            "%s network watch detected: %s",
            mode.capitalize(),
            trigger_url or self._fallback_trigger(),
        )
        self.detected.emit(trigger_url or self._fallback_trigger())

    async def _watch_cdp(self) -> None:
        client = CdpClient(self._config.bidi_host, self._config.bidi_port)
        if not await self._wait_available(client):
            return
        fired = asyncio.Event()
        trigger_url = ""
        mode = self._mode

        def _mark_rate_limit(url: str) -> None:
            nonlocal trigger_url
            self._rate_limited = True
            trigger_url = url or "429"
            fired.set()

        def on_event(method: str, params: dict[str, Any], session_id: str | None) -> None:
            nonlocal trigger_url
            if fired.is_set() or self._stop.is_set():
                return
            if method == "Target.attachedToTarget":
                sid = params.get("sessionId") or session_id
                target = params.get("targetInfo") or {}
                if sid and target.get("type") == "page":
                    asyncio.create_task(_enable_network(client, sid))
                return
            # Login success is polled (cookie + dashboard URL). Network events
            # alone previously false-triggered mid-OAuth; logout still uses them.
            # Auth HTTP 429 during login triggers a dedicated-profile wipe.
            if method == "Network.responseReceived":
                response = params.get("response") or {}
                url = str(response.get("url") or "")
                status = response.get("status")
                status_i = int(status) if isinstance(status, (int, float)) else None
                resource_type = str(params.get("type") or "") or None
                if mode == "login":
                    if response_suggests_auth_rate_limit(url, status_i):
                        _mark_rate_limit(url)
                    return
                if response_suggests_logout(
                    url, status_i, resource_type=resource_type
                ):
                    trigger_url = url
                    fired.set()
                return
            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                url = str(req.get("url") or "")
                resource_type = str(params.get("type") or "") or None
                if mode == "logout" and response_suggests_logout(
                    url, None, resource_type=resource_type
                ):
                    trigger_url = url
                    fired.set()

        async def _enable_network(c: CdpClient, sid: str) -> None:
            try:
                await c.enable_network(sid)
            except CdpError as exc:
                log.debug("Network.enable failed: %s", exc)

        client.add_event_listener(on_event)
        try:
            await client.connect()
            self.status.emit(self._status_message())
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

            if mode == "login":

                async def _poll_cdp_or_profile() -> str:
                    return (
                        await self._cdp_session_cookie_present(client)
                        or await asyncio.to_thread(self._profile_session_cookie_present)
                    )

                poll_trigger = await self._watch_until_with_cookie_poll(
                    fired,
                    poll=_poll_cdp_or_profile,
                )
                if poll_trigger and not self._rate_limited:
                    trigger_url = trigger_url or poll_trigger
                    fired.set()
            else:
                await self._watch_until(fired)
            if fired.is_set():
                self._emit_watch_result(mode=mode, trigger_url=trigger_url)
        finally:
            client.remove_event_listener(on_event)
            try:
                await client.close()
            except Exception:
                log.debug("CDP %s watch close failed", mode, exc_info=True)

    async def _watch_bidi(self) -> None:
        client = BidiClient(self._config.bidi_host, self._config.bidi_port)
        if not await self._wait_available(client):
            return
        fired = asyncio.Event()
        trigger_url = ""
        mode = self._mode

        def _mark_rate_limit(url: str) -> None:
            nonlocal trigger_url
            self._rate_limited = True
            trigger_url = url or "429"
            fired.set()

        def on_event(method: str, params: dict[str, Any], _session_id: str | None) -> None:
            nonlocal trigger_url
            if fired.is_set() or self._stop.is_set():
                return
            if method == "network.beforeRequestSent":
                req = params.get("request") or {}
                url = str(req.get("url") or "")
                destination = str(req.get("destination") or "") or None
                if mode == "logout" and response_suggests_logout(
                    url, None, resource_type=destination
                ):
                    trigger_url = url
                    fired.set()
                return
            if method == "network.responseCompleted":
                req = params.get("request") or {}
                response = params.get("response") or {}
                url = str(req.get("url") or response.get("url") or "")
                status = response.get("status")
                status_i = int(status) if isinstance(status, (int, float)) else None
                destination = str(req.get("destination") or "") or None
                if mode == "login":
                    if response_suggests_auth_rate_limit(url, status_i):
                        _mark_rate_limit(url)
                    return
                if response_suggests_logout(
                    url, status_i, resource_type=destination
                ):
                    trigger_url = url
                    fired.set()
                return
            if method.startswith("browsingContext."):
                url = str(params.get("url") or "")
                # Login success is polled (profile cookie); ignore navigations.
                if mode == "logout" and (
                    url_looks_like_auth(url) or url_looks_like_logout(url)
                ):
                    trigger_url = url
                    fired.set()

        client.add_event_listener(on_event)
        try:
            await client.connect()
            self.status.emit(self._status_message())
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

            if mode == "login":

                async def _poll_profile() -> str:
                    return await asyncio.to_thread(self._profile_session_cookie_present)

                poll_trigger = await self._watch_until_with_cookie_poll(
                    fired,
                    poll=_poll_profile,
                )
                if poll_trigger and not self._rate_limited:
                    trigger_url = trigger_url or poll_trigger
                    fired.set()
            else:
                await self._watch_until(fired)
            if fired.is_set():
                self._emit_watch_result(mode=mode, trigger_url=trigger_url)
        finally:
            client.remove_event_listener(on_event)
            try:
                await client.close()
            except Exception:
                log.debug("BiDi %s watch close failed", mode, exc_info=True)


class _NetworkWatcherBase(QObject):
    """Owns at most one watch thread for a given mode."""

    detected = pyqtSignal(str)
    rate_limited = pyqtSignal(str)
    status = pyqtSignal(str)

    _mode: WatchMode
    _log_label: str

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._thread: _SessionNetworkWatchThread | None = None

    def is_active(self) -> bool:
        return bool(self._thread and self._thread.isRunning())

    def start(self) -> None:
        """Start watching; no-op if already running."""
        if self.is_active():
            return
        thread = _SessionNetworkWatchThread(self._config, mode=self._mode)
        thread.detected.connect(self.detected.emit)
        thread.rate_limited.connect(self.rate_limited.emit)
        thread.status.connect(self.status.emit)
        thread.finished.connect(self._on_thread_finished)
        self._thread = thread
        log.info(
            "Starting %s network watch (%s)",
            self._log_label,
            self._config.browser.display_name,
        )
        thread.start()

    def stop(self, *, wait_ms: int = 5_000) -> None:
        """Stop the watcher and release the BiDi/CDP session before scrapes."""
        thread = self._thread
        if thread is None:
            return
        if thread.isRunning():
            thread.request_stop()
            if not thread.wait(wait_ms):
                log.warning(
                    "%s network watch thread did not exit within %dms",
                    self._log_label.capitalize(),
                    wait_ms,
                )
                thread.request_stop()
                thread.wait(1_000)
        self._thread = None

    def _on_thread_finished(self) -> None:
        if self._thread is self.sender():
            self._thread = None


class LoginNetworkWatcher(_NetworkWatcherBase):
    """Owns at most one watch thread; only run while the tray awaits sign-in."""

    _mode: WatchMode = "login"
    _log_label = "login"


class LogoutNetworkWatcher(_NetworkWatcherBase):
    """Owns at most one watch thread; only run while View Browser is headed."""

    _mode: WatchMode = "logout"
    _log_label = "logout"
