from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .bidi_client import BidiClient, BidiError
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


class SpendingScraper:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def fetch_sync(self) -> UsageSnapshot:
        return asyncio.run(self.fetch())

    async def fetch(self) -> UsageSnapshot:
        prev = UsageSnapshot.load()
        print(
            f"[scrape] start prev=cursor:{prev.cursor_models_pct}% "
            f"other:{prev.other_models_pct}% source={prev.source!r} "
            f"url={self.config.spending_url}",
            flush=True,
        )
        client = BidiClient(self.config.bidi_host, self.config.bidi_port)
        try:
            available = await client.is_available()
            print(
                f"[scrape] remote agent at {client.http_base}: "
                f"{'available' if available else 'UNAVAILABLE'}",
                flush=True,
            )
            if not available:
                err = (
                    f"No Remote Agent on {client.http_base}. "
                    "Start Zen temporarily with "
                    "`zen-browser --remote-debugging-port=9222` "
                    "(seamless launcher comes later)."
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
            print("[scrape] BiDi connected", flush=True)
            context_id = await client.find_or_open_tab(
                self.config.spending_url,
                reuse=self.config.dedicated_tab,
            )
            print(f"[scrape] tab context_id={context_id}", flush=True)
            # Refresh dedicated tab for latest numbers
            try:
                await client.reload(context_id)
                print("[scrape] reloaded spending tab", flush=True)
            except BidiError as reload_exc:
                print(f"[scrape] reload failed ({reload_exc}); navigating", flush=True)
                await client.call(
                    "browsingContext.navigate",
                    {
                        "context": context_id,
                        "url": self.config.spending_url,
                        "wait": "complete",
                    },
                )
                print("[scrape] navigated to spending url", flush=True)

            data = await self._extract_with_retry(client, context_id)
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
                    error="Cursor session looks logged out in Zen. Sign in, then refresh.",
                    fetched_at=time.time(),
                    source="bidi",
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
                    source="bidi",
                    raw_hint=data.get("hint") or prev.raw_hint,
                )

            snap = UsageSnapshot(
                cursor_models_pct=cursor_pct,
                other_models_pct=other_pct,
                fetched_at=time.time(),
                source="bidi",
                raw_hint=data.get("hint"),
            )
            snap.save()
            print(
                f"[scrape] OK saved cursor={snap.cursor_models_pct}% "
                f"other={snap.other_models_pct}% "
                f"(dashboard expect ~25% / ~70%)",
                flush=True,
            )
            return snap
        except Exception as exc:
            print(f"[scrape] exception: {exc!r}", flush=True)
            # Treat a stuck BiDi session the same as "browser unavailable" so the
            # scheduler's probe loop retries rather than showing a generic error.
            is_session_stuck = "maximum number of active sessions" in str(exc).lower()
            if is_session_stuck:
                log.warning("BiDi session stuck (stale from previous run); will retry: %s", exc)
                return UsageSnapshot(
                    cursor_models_pct=prev.cursor_models_pct,
                    other_models_pct=prev.other_models_pct,
                    fetched_at=time.time(),
                    source="unavailable",
                    error="BiDi session busy — restart Zen with remote debugging to clear it.",
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
        self, client: BidiClient, context_id: str, attempts: int = 8
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for i in range(attempts):
            last = await client.evaluate(context_id, EXTRACT_JS) or {}
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
