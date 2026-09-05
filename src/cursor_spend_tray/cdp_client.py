"""Minimal Chrome DevTools Protocol client for Chromium-family automation."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

log = logging.getLogger(__name__)


class CdpError(RuntimeError):
    pass


class CdpClient:
    """Page automation via CDP over the Chromium remote-debugging port."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9222) -> None:
        self.host = host
        self.port = port
        self._ws: ClientConnection | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._sessions: dict[str, str] = {}  # targetId -> sessionId

    @property
    def http_base(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                for path in ("/json/version", "/json/list", "/"):
                    try:
                        resp = await client.get(f"{self.http_base}{path}")
                        if resp.status_code < 500:
                            return True
                    except httpx.HTTPError:
                        continue
        except Exception:
            return False
        return False

    async def connect(self) -> None:
        ws_url = await self._browser_ws_url()
        if not ws_url:
            raise CdpError(
                f"Chromium DevTools not reachable at {self.http_base}. "
                "Start Chrome/Brave/Helium with --remote-debugging-port and a dedicated --user-data-dir."
            )
        parsed = urlparse(ws_url)
        if parsed.hostname in {"localhost", "127.0.0.1"}:
            ws_url = parsed._replace(netloc=f"{self.host}:{parsed.port or self.port}").geturl()

        log.info("Connecting CDP websocket %s", ws_url)
        # Usage CSV exports can be multi-MB when returned through Runtime.evaluate.
        self._ws = await websockets.connect(ws_url, max_size=32 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._read_loop(), name="cdp-reader")
        try:
            await self.call("Target.setDiscoverTargets", {"discover": True})
        except CdpError as exc:
            log.debug("Target.setDiscoverTargets failed: %s", exc)

    async def close(self) -> None:
        for session_id in list(self._sessions.values()):
            try:
                await asyncio.wait_for(
                    self.call("Target.detachFromTarget", {"sessionId": session_id}),
                    timeout=2,
                )
            except Exception:
                pass
        self._sessions.clear()
        await self._force_disconnect()

    async def find_or_open_tab(self, url: str, reuse: bool = True) -> str:
        """Return a CDP session id attached to the spending tab (or a new one)."""
        targets = await self._page_targets()
        if reuse:
            for target in targets:
                target_url = (target.get("url") or "").rstrip("/")
                if "cursor.com/dashboard/spending" in target_url:
                    return await self._attach(target["targetId"])
                if "cursor.com" in target_url and "spending" in target_url:
                    return await self._attach(target["targetId"])

        for target in targets:
            target_url = target.get("url") or ""
            if target_url in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}:
                session_id = await self._attach(target["targetId"])
                await self.call(
                    "Page.navigate",
                    {"url": url},
                    session_id=session_id,
                )
                await self._wait_load(session_id)
                return session_id

        created = await self.call("Target.createTarget", {"url": url})
        target_id = created.get("targetId")
        if not target_id:
            raise CdpError(f"Target.createTarget returned no targetId: {created!r}")
        session_id = await self._attach(target_id)
        await self._wait_load(session_id)
        return session_id

    async def reload(self, session_id: str) -> None:
        await self.call("Page.reload", {"ignoreCache": False}, session_id=session_id)
        await self._wait_load(session_id)

    async def evaluate(self, session_id: str, expression: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            session_id=session_id,
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise CdpError(f"Runtime.evaluate failed: {details}")
        remote = result.get("result") or {}
        return remote.get("value")

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not self._ws:
            raise CdpError("Not connected")
        msg_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[msg_id] = fut
        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=60)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise CdpError(f"Timeout calling {method}") from exc

    async def _browser_ws_url(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                resp = await client.get(f"{self.http_base}/json/version")
                if resp.status_code == 200:
                    data = resp.json()
                    url = data.get("webSocketDebuggerUrl")
                    if url:
                        return url
        except Exception:
            pass
        return None

    async def _page_targets(self) -> list[dict[str, Any]]:
        result = await self.call("Target.getTargets")
        targets = result.get("targetInfos") or []
        return [t for t in targets if t.get("type") == "page"]

    async def _attach(self, target_id: str) -> str:
        if target_id in self._sessions:
            return self._sessions[target_id]
        attached = await self.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = attached.get("sessionId")
        if not session_id:
            raise CdpError(f"attachToTarget returned no sessionId: {attached!r}")
        self._sessions[target_id] = session_id
        try:
            await self.call("Page.enable", {}, session_id=session_id)
            await self.call("Runtime.enable", {}, session_id=session_id)
        except CdpError as exc:
            log.debug("Page/Runtime.enable: %s", exc)
        return session_id

    async def _wait_load(self, session_id: str, timeout: float = 30.0) -> None:
        """Best-effort wait: poll document.readyState."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                state = await self.evaluate(session_id, "document.readyState")
            except CdpError:
                await asyncio.sleep(0.2)
                continue
            if state == "complete":
                return
            await asyncio.sleep(0.2)

    async def _force_disconnect(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CdpError("Connection closed"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Non-JSON CDP message: %r", raw[:200])
                    continue
                msg_id = msg.get("id")
                if msg_id is None:
                    continue
                fut = self._pending.pop(msg_id, None)
                if not fut:
                    continue
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(
                        CdpError(f"{err.get('message') or err} ({err.get('code')})")
                    )
                else:
                    fut.set_result(msg.get("result") or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("CDP reader failed")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(CdpError(str(exc)))
            self._pending.clear()
