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
        client = BidiClient(self.config.bidi_host, self.config.bidi_port)
        try:
            if not await client.is_available():
                return UsageSnapshot(
                    cursor_models_pct=prev.cursor_models_pct,
                    other_models_pct=prev.other_models_pct,
                    error=(
                        f"No Remote Agent on {client.http_base}. "
                        "Start Zen temporarily with "
                        "`zen-browser --remote-debugging-port=9222` "
                        "(seamless launcher comes later)."
                    ),
                    fetched_at=time.time(),
                    source="unavailable",
                    raw_hint=prev.raw_hint,
                )
            await client.connect()
            context_id = await client.find_or_open_tab(
                self.config.spending_url,
                reuse=self.config.dedicated_tab,
            )
            # Refresh dedicated tab for latest numbers
            try:
                await client.reload(context_id)
            except BidiError:
                await client.call(
                    "browsingContext.navigate",
                    {
                        "context": context_id,
                        "url": self.config.spending_url,
                        "wait": "complete",
                    },
                )

            data = await self._extract_with_retry(client, context_id)
            if data.get("loggedOut"):
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
            if cursor_pct is None and other_pct is None:
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
            return snap
        except Exception as exc:
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

    async def _extract_with_retry(
        self, client: BidiClient, context_id: str, attempts: int = 8
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for i in range(attempts):
            last = await client.evaluate(context_id, EXTRACT_JS) or {}
            if not isinstance(last, dict):
                last = {}
            if last.get("cursorModelsPct") is not None or last.get("otherModelsPct") is not None:
                return last
            if last.get("loggedOut"):
                return last
            await asyncio.sleep(0.75 + i * 0.15)
        return last


def _clamp_pct(value: Any) -> int | None:
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, n))
