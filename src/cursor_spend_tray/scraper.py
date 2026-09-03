from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

from .bidi_client import BidiClient, BidiError
from .browser import BrowserFamily
from .cdp_client import CdpClient, CdpError
from .config import AppConfig, UsageSnapshot

log = logging.getLogger(__name__)

EXTRACT_JS = r"""
(() => {
  const bodyText = document.body ? document.body.innerText : "";
  const pickPct = (label) => {
    const re = new RegExp(
      label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "[\\s\\S]{0,240}?(\\d{1,3})%\\s*used",
      "i"
    );
    const m = bodyText.match(re);
    return m ? Number(m[1]) : null;
  };

  // Prefer structured nodes when present
  const fromDom = () => {
    const out = { cursorModelsPct: null, otherModelsPct: null };
    const nodes = Array.from(document.querySelectorAll("body *"));
    for (const el of nodes) {
      const t = (el.textContent || "").trim();
      if (!t) continue;
      if (/^Cursor Models\b/i.test(t) || t.includes("Includes Cursor Grok")) {
        let scope = el.closest("section,div,article,li") || el.parentElement;
        for (let i = 0; i < 6 && scope; i++) {
          const m = (scope.innerText || "").match(/(\d{1,3})%\s*used/i);
          if (m) {
            out.cursorModelsPct = Number(m[1]);
            break;
          }
          scope = scope.parentElement;
        }
      }
      if (/^Other Models\b/i.test(t) && t.length < 80) {
        let scope = el.closest("section,div,article,li") || el.parentElement;
        for (let i = 0; i < 6 && scope; i++) {
          const m = (scope.innerText || "").match(/(\d{1,3})%\s*used/i);
          if (m) {
            out.otherModelsPct = Number(m[1]);
            break;
          }
          scope = scope.parentElement;
        }
      }
    }
    return out;
  };

  const dom = fromDom();
  const cursorModelsPct = dom.cursorModelsPct ?? pickPct("Cursor Models");
  const otherModelsPct = dom.otherModelsPct ?? pickPct("Other Models");
  const loggedOut = /sign\s*in|log\s*in|authenticate/i.test(bodyText)
    && !/Included in Pro/i.test(bodyText);

  return {
    cursorModelsPct,
    otherModelsPct,
    loggedOut,
    hasIncludedInPro: /Included in Pro/i.test(bodyText),
    hint: bodyText.slice(0, 500),
  };
})()
"""


class _ScrapeClient(Protocol):
    http_base: str

    async def is_available(self) -> bool: ...
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def find_or_open_tab(self, url: str, reuse: bool = True) -> str: ...
    async def reload(self, handle: str) -> None: ...
    async def evaluate(self, handle: str, expression: str) -> Any: ...


class SpendingScraper:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def fetch_sync(self) -> UsageSnapshot:
        return asyncio.run(self.fetch())

    def _make_client(self) -> tuple[_ScrapeClient, str]:
        browser = self.config.browser
        if browser.family is BrowserFamily.CHROMIUM:
            return (
                CdpClient(self.config.bidi_host, self.config.bidi_port),
                "cdp",
            )
        return (
            BidiClient(self.config.bidi_host, self.config.bidi_port),
            "bidi",
        )

    async def fetch(self) -> UsageSnapshot:
        prev = UsageSnapshot.load()
        browser = self.config.browser
        print(
            f"[scrape] start browser={browser.display_name}/{browser.family.value} "
            f"prev=cursor:{prev.cursor_models_pct}% "
            f"other:{prev.other_models_pct}% source={prev.source!r} "
            f"url={self.config.spending_url}",
            flush=True,
        )
        client, source = self._make_client()
        try:
            available = await client.is_available()
            print(
                f"[scrape] remote debugging at {client.http_base}: "
                f"{'available' if available else 'UNAVAILABLE'}",
                flush=True,
            )
            if not available:
                err = (
                    f"No remote debugging on {client.http_base}. "
                    f"Start the dedicated {browser.display_name} profile with remote debugging "
                    "(copy the command from the tray popup, or use Launch Browser)."
                )
                print(f"[scrape] abort: {err}", flush=True)
                return UsageSnapshot(
                    cursor_models_pct=prev.cursor_models_pct,
                    other_models_pct=prev.other_models_pct,
                    error=err,
                    fetched_at=time.time(),
                    source="unavailable",
                    raw_hint=prev.raw_hint,
                )
            await client.connect()
            print(f"[scrape] {source.upper()} connected", flush=True)
            handle = await client.find_or_open_tab(
                self.config.spending_url,
                reuse=self.config.dedicated_tab,
            )
            print(f"[scrape] tab handle={handle}", flush=True)
            try:
                await client.reload(handle)
                print("[scrape] reloaded spending tab", flush=True)
            except (BidiError, CdpError) as reload_exc:
                print(f"[scrape] reload failed ({reload_exc}); navigating", flush=True)
                if isinstance(client, BidiClient):
                    await client.call(
                        "browsingContext.navigate",
                        {
                            "context": handle,
                            "url": self.config.spending_url,
                            "wait": "complete",
                        },
                    )
                else:
                    assert isinstance(client, CdpClient)
                    await client.call(
                        "Page.navigate",
                        {"url": self.config.spending_url},
                        session_id=handle,
                    )
                    await client._wait_load(handle)
                print("[scrape] navigated to spending url", flush=True)

            data = await self._extract_with_retry(client, handle)
            print(
                f"[scrape] extract raw cursorModelsPct={data.get('cursorModelsPct')!r} "
                f"otherModelsPct={data.get('otherModelsPct')!r} "
                f"loggedOut={data.get('loggedOut')!r} "
                f"hasIncludedInPro={data.get('hasIncludedInPro')!r}",
                flush=True,
            )
            hint = data.get("hint") or ""
            print(f"[scrape] page hint ({len(hint)} chars): {hint[:400]!r}", flush=True)
            if data.get("loggedOut"):
                print("[scrape] page looks logged out; keeping previous snapshot", flush=True)
                return UsageSnapshot(
                    cursor_models_pct=prev.cursor_models_pct,
                    other_models_pct=prev.other_models_pct,
                    error=(
                        f"Cursor session looks logged out in {browser.display_name}. "
                        "Sign in, then refresh."
                    ),
                    fetched_at=time.time(),
                    source=source,
                    raw_hint=data.get("hint") or prev.raw_hint,
                )

            cursor_pct = _clamp_pct(data.get("cursorModelsPct"))
            other_pct = _clamp_pct(data.get("otherModelsPct"))
            print(
                f"[scrape] clamped pct cursor={cursor_pct!r} other={other_pct!r}",
                flush=True,
            )
            if cursor_pct is None and other_pct is None:
                print("[scrape] parse failed; keeping previous snapshot", flush=True)
                return UsageSnapshot(
                    cursor_models_pct=prev.cursor_models_pct,
                    other_models_pct=prev.other_models_pct,
                    error="Could not parse spending percentages (page structure may have changed).",
                    fetched_at=time.time(),
                    source=source,
                    raw_hint=data.get("hint") or prev.raw_hint,
                )

            snap = UsageSnapshot(
                cursor_models_pct=cursor_pct,
                other_models_pct=other_pct,
                fetched_at=time.time(),
                source=source,
                raw_hint=data.get("hint"),
            )
            snap.save()
            print(
                f"[scrape] OK saved cursor={snap.cursor_models_pct}% "
                f"other={snap.other_models_pct}%",
                flush=True,
            )
            return snap
        except Exception as exc:
            print(f"[scrape] exception: {exc!r}", flush=True)
            is_session_stuck = "maximum number of active sessions" in str(exc).lower()
            if is_session_stuck:
                log.warning("BiDi session stuck; will retry: %s", exc)
                return UsageSnapshot(
                    cursor_models_pct=prev.cursor_models_pct,
                    other_models_pct=prev.other_models_pct,
                    fetched_at=time.time(),
                    source="unavailable",
                    error=(
                        f"Automation session busy — restart {browser.display_name} "
                        "with remote debugging to clear it."
                    ),
                    raw_hint=prev.raw_hint,
                )
            log.exception("Scrape failed")
            return UsageSnapshot(
                cursor_models_pct=prev.cursor_models_pct,
                other_models_pct=prev.other_models_pct,
                fetched_at=time.time(),
                source="error",
                error=str(exc),
                raw_hint=prev.raw_hint,
            )
        finally:
            await client.close()
            print("[scrape] client closed", flush=True)

    async def _extract_with_retry(
        self, client: _ScrapeClient, handle: str, attempts: int = 8
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for i in range(attempts):
            last = await client.evaluate(handle, EXTRACT_JS) or {}
            if not isinstance(last, dict):
                last = {}
            print(
                f"[scrape] attempt {i + 1}/{attempts}: "
                f"cursor={last.get('cursorModelsPct')!r} "
                f"other={last.get('otherModelsPct')!r} "
                f"loggedOut={last.get('loggedOut')!r} "
                f"hasIncludedInPro={last.get('hasIncludedInPro')!r}",
                flush=True,
            )
            if last.get("cursorModelsPct") is not None or last.get("otherModelsPct") is not None:
                return last
            if last.get("loggedOut"):
                return last
            await asyncio.sleep(0.75 + i * 0.15)
        print("[scrape] extract retries exhausted", flush=True)
        return last


def _clamp_pct(value: Any) -> int | None:
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, n))
