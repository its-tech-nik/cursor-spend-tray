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


class BidiError(RuntimeError):
    pass


class BidiClient:
    """Minimal WebDriver BiDi client for Firefox/Zen Remote Agent."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9222) -> None:
        self.host = host
        self.port = port
        self._ws: ClientConnection | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._session_active = False

    @property
    def http_base(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                # Firefox Remote Agent answers several probes; any HTTP response means up.
                for path in ("/json/version", "/status", "/"):
                    try:
                        resp = await client.get(f"{self.http_base}{path}")
                        if resp.status_code < 500:
                            return True
                    except httpx.HTTPError:
                        continue
            # Fallback: try websocket handshake URL discovery
            return await self._discover_ws_url() is not None
        except Exception:
            return False

    async def _discover_ws_url(self) -> str | None:
        candidates = [
            f"ws://{self.host}:{self.port}/session",
            f"ws://{self.host}:{self.port}",
        ]
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                resp = await client.get(f"{self.http_base}/json/version")
                if resp.status_code == 200:
                    data = resp.json()
                    for key in ("webSocketDebuggerUrl", "webSocketUrl"):
                        if key in data and data[key]:
                            candidates.insert(0, data[key])
        except Exception:
            pass

        for url in candidates:
            try:
                async with websockets.connect(url, open_timeout=1.5, close_timeout=1):
                    return url
            except Exception:
                continue
        return None

    async def connect(self) -> None:
        ws_url = await self._discover_ws_url()
        if not ws_url:
            raise BidiError(
                f"Zen/Firefox Remote Agent not reachable at {self.http_base}. "
                "Start Zen with --remote-debugging-port=9222 (localhost)."
            )
        # Normalize host for local connections
        parsed = urlparse(ws_url)
        if parsed.hostname in {"localhost", "127.0.0.1"}:
            ws_url = parsed._replace(netloc=f"{self.host}:{parsed.port or self.port}").geturl()

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                await self._connect_once(ws_url, attempt=attempt)
                return
            except BidiError as exc:
                last_exc = exc
                await self._force_disconnect()
                if not _is_session_limit_error(exc) or attempt == 3:
                    break
                # Orphaned BiDi sessions (no session.end) block new ones briefly.
                log.warning(
                    "BiDi session busy (%s); retrying in %.1fs (%d/3)",
                    exc,
                    attempt * 1.5,
                    attempt,
                )
                await asyncio.sleep(attempt * 1.5)

        raise BidiError(
            f"Could not start a WebDriver BiDi session at {self.http_base}: {last_exc}. "
            "Quit other automation clients, or fully restart Zen with "
            "--remote-debugging-port=9222, then try again."
        ) from last_exc

    async def _connect_once(self, ws_url: str, *, attempt: int) -> None:
        log.info("Connecting BiDi websocket %s (attempt %d)", ws_url, attempt)
        self._ws = await websockets.connect(ws_url, max_size=8 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._read_loop(), name="bidi-reader")
        self._session_active = False

        status: dict[str, Any] = {}
        try:
            status = await self.call("session.status", {})
        except BidiError as exc:
            log.debug("session.status failed before session.new: %s", exc)

        # If a previous session is still open (e.g. app was killed), end it first
        # so we can create a fresh one.
        if not status.get("result", status).get("ready", True):
            log.info("Existing BiDi session detected; ending it before creating a new one")
            try:
                await asyncio.wait_for(self.call("session.end", {}), timeout=5)
                log.info("Stale session ended")
            except Exception as exc:
                log.debug("session.end on stale session failed (%s); proceeding anyway", exc)

        try:
            await self.call(
                "session.new",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "acceptInsecureCerts": True,
                        }
                    }
                },
            )
        except BidiError as exc:
            if _is_session_limit_error(exc):
                raise BidiError(
                    f"session not created: Maximum number of active sessions "
                    f"(status={status!r})"
                ) from exc
            # Some builds expose a session-bound socket where session.new is rejected
            # after the session is already attached to this connection.
            log.info("session.new skipped/failed (%s); probing commands", exc)
            try:
                await self.call("browsingContext.getTree", {"maxDepth": 0})
            except BidiError as probe_exc:
                raise BidiError(
                    f"No usable BiDi session after session.new failure: {exc}"
                ) from probe_exc

        self._session_active = True
        log.info("BiDi session ready (status before new=%s)", status)

    async def close(self) -> None:
        if self._ws and self._session_active:
            try:
                await asyncio.wait_for(self.call("session.end", {}), timeout=3)
                log.info("BiDi session.end ok")
            except Exception as exc:
                log.warning("session.end failed during close: %s", exc)
            finally:
                self._session_active = False
        await self._force_disconnect()

    async def _force_disconnect(self) -> None:
        self._session_active = False
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
                fut.set_exception(BidiError("Connection closed"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Non-JSON BiDi message: %r", raw[:200])
                    continue
                msg_id = msg.get("id")
                if msg_id is None:
                    log.debug("BiDi event: %s", msg.get("method"))
                    continue
                fut = self._pending.pop(msg_id, None)
                if not fut:
                    continue
                if "error" in msg:
                    fut.set_exception(
                        BidiError(f"{msg.get('error')}: {msg.get('message')}")
                    )
                else:
                    fut.set_result(msg.get("result") or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("BiDi reader failed")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(BidiError(str(exc)))
            self._pending.clear()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._ws:
            raise BidiError("Not connected")
        msg_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[msg_id] = fut
        payload = {"id": msg_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=60)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise BidiError(f"Timeout calling {method}") from exc

    async def get_contexts(self) -> list[dict[str, Any]]:
        result = await self.call("browsingContext.getTree", {"maxDepth": 1})
        contexts = result.get("contexts") or []
        flat: list[dict[str, Any]] = []

        def walk(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                flat.append(node)
                children = node.get("children") or []
                if children:
                    walk(children)

        walk(contexts)
        return flat

    async def find_or_open_tab(self, url: str, reuse: bool = True) -> str:
        contexts = await self.get_contexts()
        if reuse:
            for ctx in contexts:
                ctx_url = (ctx.get("url") or "").rstrip("/")
                if "cursor.com/dashboard/spending" in ctx_url:
                    return ctx["context"]
                if "cursor.com" in ctx_url and "spending" in ctx_url:
                    return ctx["context"]

        # Prefer an empty tab; never steal the user's active non-spending tab.
        for ctx in contexts:
            ctx_url = ctx.get("url") or ""
            if ctx_url in {
                "about:blank",
                "about:newtab",
                "about:home",
                "chrome://browser/content/blanktab.html",
            }:
                await self.call(
                    "browsingContext.navigate",
                    {"context": ctx["context"], "url": url, "wait": "complete"},
                )
                return ctx["context"]

        created = await self.call("browsingContext.create", {"type": "tab"})
        context_id = created["context"]
        await self.call(
            "browsingContext.navigate",
            {"context": context_id, "url": url, "wait": "complete"},
        )
        return context_id

    async def reload(self, context_id: str) -> None:
        await self.call(
            "browsingContext.reload",
            {"context": context_id, "wait": "complete", "ignoreCache": False},
        )

    async def evaluate(self, context_id: str, expression: str) -> Any:
        result = await self.call(
            "script.evaluate",
            {
                "expression": expression,
                "target": {"context": context_id},
                "awaitPromise": True,
                "resultOwnership": "none",
            },
        )
        remote = result.get("result") or {}
        rtype = remote.get("type")
        if rtype == "object" and "value" in remote:
            return _deserialize_bidi_object(remote)
        if "value" in remote:
            return remote["value"]
        return remote


def _is_session_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "maximum number of active sessions" in text or "session already started" in text


def _deserialize_bidi_object(remote: dict[str, Any]) -> Any:
    """Best-effort conversion of BiDi RemoteValue object maps/lists."""
    rtype = remote.get("type")
    if rtype in {"string", "number", "boolean", "bigint"}:
        return remote.get("value")
    if rtype == "null" or rtype == "undefined":
        return None
    if rtype == "array":
        return [_deserialize_bidi_object(item) for item in remote.get("value") or []]
    if rtype == "object":
        out: dict[str, Any] = {}
        for item in remote.get("value") or []:
            # BiDi object value is list of [key, RemoteValue]
            if isinstance(item, (list, tuple)) and len(item) == 2:
                key, val = item
                out[str(key)] = _deserialize_bidi_object(val)
        return out
    return remote.get("value", remote)
