from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

from .auth_detect import page_requires_login_from_extract
from .bidi_client import BidiClient, BidiError
from .browser import BrowserFamily
from .cdp_client import CdpClient, CdpError
from .config import SETTINGS_URL, AppConfig, UsageSnapshot, resolve_usage_reset_at
from .usage_csv import associate_spend_pct, sync_usage_csvs

log = logging.getLogger(__name__)

# Longest-first so "Pro+" wins over "Pro".
_PLAN_NAMES_JS = '["Ultra", "Pro+", "Business", "Teams", "Pro", "Hobby", "Free"]'

EXTRACT_JS = r"""
(() => {
  const plans = __PLANS__;
  const pageUrl = String(location.href || "");
  // Slow loads / mid-navigation: body may be null — soft-skip until next poll.
  if (!document.body) {
    return {
      pageUrl,
      cursorModelsPct: null,
      otherModelsPct: null,
      subscription: null,
      loggedOut: false,
      hasIncludedInPro: false,
      pageReady: false,
      hint: "",
    };
  }
  const bodyText = document.body.innerText || "";
  const pickPct = (label) => {
    const re = new RegExp(
      label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "[\\s\\S]{0,240}?(\\d{1,3})%\\s*used",
      "i"
    );
    const m = bodyText.match(re);
    return m ? Number(m[1]) : null;
  };

  const matchPlan = (text) => {
    const t = (text || "").trim();
    if (!t || t.length > 64) return null;
    for (const p of plans) {
      if (t === p) return p;
    }
    for (const p of plans) {
      const esc = p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      if (new RegExp("^" + esc + "\\b", "i").test(t)) return p;
    }
    return null;
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

  // Plan on spending: "CURRENT PLAN" block, else first leaf plan label.
  let subscription = null;
  const currentPlan = bodyText.match(
    /CURRENT\s+PLAN[\s\S]{0,80}?\b(Ultra|Pro\+|Business|Teams|Pro|Hobby|Free)\b/i
  );
  if (currentPlan) {
    subscription = matchPlan(currentPlan[1]) || currentPlan[1];
  }
  if (!subscription) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let n;
    while ((n = walker.nextNode())) {
      if (n.children.length !== 0) continue;
      const hit = matchPlan(n.textContent);
      if (hit) {
        subscription = hit;
        break;
      }
    }
  }

  const dom = fromDom();
  const cursorModelsPct = dom.cursorModelsPct ?? pickPct("Cursor Models");
  const otherModelsPct = dom.otherModelsPct ?? pickPct("Other Models");
  const hasDashboard = /Included in Pro/i.test(bodyText);
  const loggedOut = (
    /authenticator\.cursor\.sh/i.test(pageUrl)
    || /authenticator\.cursor\.sh/i.test(bodyText)
    || /performing security verification/i.test(bodyText)
    || (/verify you are not a bot/i.test(bodyText) && /cursor/i.test(pageUrl + bodyText))
    || (
      /sign\s*in|log\s*in|authenticate/i.test(bodyText)
      && !hasDashboard
    )
  );

  return {
    pageUrl,
    cursorModelsPct,
    otherModelsPct,
    subscription,
    loggedOut,
    hasIncludedInPro: hasDashboard,
    pageReady: true,
    hint: bodyText.slice(0, 500),
  };
})()
""".replace("__PLANS__", _PLAN_NAMES_JS)

# Settings page: email cell + left-sidebar plan / avatar at the bottom.
ACCOUNT_EXTRACT_JS = r"""
(() => {
  const plans = __PLANS__;
  const pageUrl = String(location.href || "");
  if (!document.body) {
    return {
      email: null,
      subscription: null,
      avatarUrl: null,
      pageUrl,
      pageReady: false,
    };
  }
  const emailRe = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i;
  const matchPlan = (text) => {
    const t = (text || "").trim();
    if (!t || t.length > 64) return null;
    for (const p of plans) {
      if (t === p) return p;
    }
    for (const p of plans) {
      const esc = p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      if (new RegExp("^" + esc + "\\b", "i").test(t)) return p;
    }
    return null;
  };

  let email = null;
  const label = Array.from(document.querySelectorAll(".dashboard-cell-label")).find(
    (el) => (el.textContent || "").trim() === "Email"
  );
  if (label) {
    const cell = label.closest(".dashboard-cell");
    const m = ((cell && cell.innerText) || "").match(emailRe);
    if (m) email = m[0];
  }
  if (!email) {
    const m = (document.body.innerText || "").match(emailRe);
    if (m) email = m[0];
  }

  let subscription = null;
  const secondary = Array.from(
    document.querySelectorAll(
      'span[class*="text-secondary"], [class*="text-muted"], [class*="opacity"]'
    )
  );
  for (const el of secondary) {
    const hit = matchPlan(el.textContent);
    if (hit) subscription = hit;
  }
  if (!subscription) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let n;
    while ((n = walker.nextNode())) {
      if (n.children.length !== 0) continue;
      const hit = matchPlan(n.textContent);
      if (hit) {
        subscription = hit;
        break;
      }
    }
  }
  if (!subscription) {
    const body = document.body.innerText || "";
    const m = body.match(/\b(Ultra|Pro\+|Business|Teams|Pro|Hobby|Free)\b/);
    if (m) subscription = matchPlan(m[1]) || m[1];
  }

  let avatarUrl = null;
  // Prefer display size (sidebar avatars are ~28–40px); naturalWidth is often full-res.
  const imgs = Array.from(document.querySelectorAll("img")).filter((img) => {
    const w = img.width || img.clientWidth || 0;
    const h = img.height || img.clientHeight || 0;
    return w >= 16 && w <= 64 && h >= 16 && h <= 64 && (img.src || "").startsWith("http");
  });
  if (imgs.length) {
    avatarUrl = imgs[imgs.length - 1].src;
  }

  return {
    email,
    subscription,
    avatarUrl,
    pageUrl,
    pageReady: true,
  };
})()
""".replace("__PLANS__", _PLAN_NAMES_JS)


def _is_page_not_ready_error(exc: BaseException) -> bool:
    """True when CDP/BiDi evaluate failed because the DOM body wasn't ready yet."""
    msg = str(exc)
    return (
        "createTreeWalker" in msg
        or "parameter 1 is not of type 'Node'" in msg
    )


def _soft_skip_snapshot(prev: UsageSnapshot, *, source: str) -> UsageSnapshot:
    """Keep last good gauges; clear error so the next timer can try cleanly."""
    keep_source = prev.source if prev.source in {"cdp", "bidi"} else source
    return UsageSnapshot(
        cursor_models_pct=prev.cursor_models_pct,
        other_models_pct=prev.other_models_pct,
        usage_reset_at=prev.usage_reset_at,
        fetched_at=prev.fetched_at,
        source=keep_source,
        error=None,
        raw_hint=prev.raw_hint,
        **_account_fields(prev),
    )


class _ScrapeClient(Protocol):
    http_base: str

    async def is_available(self) -> bool: ...
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def find_or_open_tab(self, url: str, reuse: bool = True) -> str: ...
    async def reload(self, handle: str) -> None: ...
    async def evaluate(self, handle: str, expression: str) -> Any: ...


def _account_fields(prev: UsageSnapshot) -> dict[str, str | None]:
    return {
        "account_email": prev.account_email,
        "subscription_level": prev.subscription_level,
        "account_avatar_url": prev.account_avatar_url,
    }


def _cleared_account_fields() -> dict[str, str | None]:
    """Drop cached identity when the session is logged out / unauthenticated."""
    return {
        "account_email": None,
        "subscription_level": None,
        "account_avatar_url": None,
    }


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

    async def _navigate(self, client: _ScrapeClient, handle: str, url: str) -> None:
        if isinstance(client, BidiClient):
            await client.call(
                "browsingContext.navigate",
                {"context": handle, "url": url, "wait": "complete"},
            )
            return
        assert isinstance(client, CdpClient)
        await client.call("Page.navigate", {"url": url}, session_id=handle)
        await client._wait_load(handle)

    async def _fetch_account(
        self,
        client: _ScrapeClient,
        handle: str,
        prev: UsageSnapshot,
    ) -> dict[str, str | None]:
        """Navigate to settings, scrape email / plan / avatar, return to spending."""
        fields = _account_fields(prev)
        try:
            await self._navigate(client, handle, SETTINGS_URL)
            print("[scrape] navigated to settings for account info", flush=True)
            data: dict[str, Any] = {}
            for i in range(6):
                raw = await client.evaluate(handle, ACCOUNT_EXTRACT_JS) or {}
                data = raw if isinstance(raw, dict) else {}
                if data.get("email") or data.get("subscription"):
                    break
                await asyncio.sleep(0.6 + i * 0.15)
            email = data.get("email")
            subscription = data.get("subscription")
            avatar = data.get("avatarUrl")
            if isinstance(email, str) and email.strip():
                fields["account_email"] = email.strip()
            if isinstance(subscription, str) and subscription.strip():
                fields["subscription_level"] = subscription.strip()
            if isinstance(avatar, str) and avatar.startswith("http"):
                fields["account_avatar_url"] = avatar
            print(
                f"[scrape] account email={fields['account_email']!r} "
                f"subscription={fields['subscription_level']!r} "
                f"avatar={'yes' if fields['account_avatar_url'] else 'no'}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 — keep spend result
            print(f"[scrape] account extract failed: {exc!r}", flush=True)
            log.exception("Account extract failed")
        finally:
            try:
                await self._navigate(client, handle, self.config.spending_url)
                print("[scrape] returned to spending tab", flush=True)
            except Exception as back_exc:  # noqa: BLE001
                print(f"[scrape] return to spending failed: {back_exc!r}", flush=True)
                log.warning("Could not navigate back to spending: %s", back_exc)
        return fields

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
                    "(copy the command from the tray popup)."
                )
                print(f"[scrape] abort: {err}", flush=True)
                return UsageSnapshot(
                    cursor_models_pct=prev.cursor_models_pct,
                    other_models_pct=prev.other_models_pct,
                    usage_reset_at=prev.usage_reset_at,
                    error=err,
                    fetched_at=time.time(),
                    source="unavailable",
                    raw_hint=prev.raw_hint,
                    **_account_fields(prev),
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
                await self._navigate(client, handle, self.config.spending_url)
                print("[scrape] navigated to spending url", flush=True)

            data = await self._extract_with_retry(client, handle)
            if data.get("pageReady") is False:
                print(
                    "[scrape] page not ready (no document.body); "
                    "keeping previous snapshot until next poll",
                    flush=True,
                )
                return _soft_skip_snapshot(prev, source=source)
            print(
                f"[scrape] extract raw cursorModelsPct={data.get('cursorModelsPct')!r} "
                f"otherModelsPct={data.get('otherModelsPct')!r} "
                f"loggedOut={data.get('loggedOut')!r} "
                f"pageUrl={data.get('pageUrl')!r} "
                f"hasIncludedInPro={data.get('hasIncludedInPro')!r}",
                flush=True,
            )
            hint = data.get("hint") or ""
            print(f"[scrape] page hint ({len(hint)} chars): {hint[:400]!r}", flush=True)
            if page_requires_login_from_extract(data):
                print(
                    "[scrape] auth/bot page detected; needs headed sign-in "
                    f"(url={data.get('pageUrl')!r})",
                    flush=True,
                )
                snap = UsageSnapshot(
                    cursor_models_pct=prev.cursor_models_pct,
                    other_models_pct=prev.other_models_pct,
                    usage_reset_at=prev.usage_reset_at,
                    error=(
                        f"Cursor sign-in required in {browser.display_name}. "
                        "A sign-in window will open — complete any security check, "
                        "sign in, then refresh."
                    ),
                    fetched_at=time.time(),
                    source="logged_out",
                    raw_hint=data.get("hint") or prev.raw_hint,
                    **_cleared_account_fields(),
                )
                snap.save()
                print("[scrape] cleared account identity (logged out)", flush=True)
                return snap

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
                    usage_reset_at=prev.usage_reset_at,
                    error="Could not parse spending percentages (page structure may have changed).",
                    fetched_at=time.time(),
                    source=source,
                    raw_hint=data.get("hint") or prev.raw_hint,
                    **_account_fields(prev),
                )

            account = await self._fetch_account(client, handle, prev)
            spend_sub = data.get("subscription")
            if (
                not account.get("subscription_level")
                and isinstance(spend_sub, str)
                and spend_sub.strip()
            ):
                account["subscription_level"] = spend_sub.strip()
                print(
                    f"[scrape] subscription from spending page: "
                    f"{account['subscription_level']!r}",
                    flush=True,
                )
            fetched_at = time.time()
            usage_reset_at = resolve_usage_reset_at(
                prev_cursor_pct=prev.cursor_models_pct,
                new_cursor_pct=cursor_pct,
                prev_reset_at=prev.usage_reset_at,
                now=fetched_at,
            )
            snap = UsageSnapshot(
                cursor_models_pct=cursor_pct,
                other_models_pct=other_pct,
                usage_reset_at=usage_reset_at,
                fetched_at=fetched_at,
                source=source,
                raw_hint=data.get("hint"),
                **account,
            )
            snap.save()
            print(
                f"[scrape] OK saved cursor={snap.cursor_models_pct}% "
                f"other={snap.other_models_pct}% "
                f"usage_reset_at={snap.usage_reset_at!r}",
                flush=True,
            )

            # Usage-events CSV (same signed-in session) → billing-period token totals.
            try:
                usage_preview = await sync_usage_csvs(client, handle)
                current_tokens = None
                if usage_preview.available and usage_preview.periods:
                    current_tokens = usage_preview.periods[-1].total_tokens
                associate_spend_pct(
                    cursor_models_pct=cursor_pct,
                    other_models_pct=other_pct,
                    total_tokens=current_tokens,
                )
                if usage_preview.available:
                    print(
                        f"[usage-csv] periods={len(usage_preview.periods)} "
                        f"total_tokens={usage_preview.total_tokens:,} "
                        f"dir={usage_preview.csv_dir}",
                        flush=True,
                    )
                elif usage_preview.error_message:
                    print(
                        f"[usage-csv] skip: {usage_preview.error_message}",
                        flush=True,
                    )
            except Exception as csv_exc:  # noqa: BLE001 — spend scrape still succeeded
                print(f"[usage-csv] failed: {csv_exc!r}", flush=True)
                log.exception("Usage CSV sync failed")

            return snap
        except Exception as exc:
            print(f"[scrape] exception: {exc!r}", flush=True)
            if _is_page_not_ready_error(exc):
                print(
                    "[scrape] page not ready during evaluate; "
                    "keeping previous snapshot until next poll",
                    flush=True,
                )
                return _soft_skip_snapshot(prev, source=source)
            is_session_stuck = "maximum number of active sessions" in str(exc).lower()
            if is_session_stuck:
                log.warning("BiDi session stuck; will retry: %s", exc)
                return UsageSnapshot(
                    cursor_models_pct=prev.cursor_models_pct,
                    other_models_pct=prev.other_models_pct,
                    usage_reset_at=prev.usage_reset_at,
                    fetched_at=time.time(),
                    source="unavailable",
                    error=(
                        f"Automation session busy — restart {browser.display_name} "
                        "with remote debugging to clear it."
                    ),
                    raw_hint=prev.raw_hint,
                    **_account_fields(prev),
                )
            log.exception("Scrape failed")
            return UsageSnapshot(
                cursor_models_pct=prev.cursor_models_pct,
                other_models_pct=prev.other_models_pct,
                usage_reset_at=prev.usage_reset_at,
                fetched_at=time.time(),
                source="error",
                error=str(exc),
                raw_hint=prev.raw_hint,
                **_account_fields(prev),
            )
        finally:
            await client.close()
            print("[scrape] client closed", flush=True)

    async def _extract_with_retry(
        self, client: _ScrapeClient, handle: str, attempts: int = 8
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for i in range(attempts):
            try:
                last = await client.evaluate(handle, EXTRACT_JS) or {}
            except (BidiError, CdpError) as exc:
                if _is_page_not_ready_error(exc):
                    print(
                        f"[scrape] attempt {i + 1}/{attempts}: page not ready — "
                        "skipping until next poll",
                        flush=True,
                    )
                    return {"pageReady": False}
                raise
            if not isinstance(last, dict):
                last = {}
            print(
                f"[scrape] attempt {i + 1}/{attempts}: "
                f"cursor={last.get('cursorModelsPct')!r} "
                f"other={last.get('otherModelsPct')!r} "
                f"loggedOut={last.get('loggedOut')!r} "
                f"pageUrl={last.get('pageUrl')!r} "
                f"hasIncludedInPro={last.get('hasIncludedInPro')!r} "
                f"pageReady={last.get('pageReady')!r}",
                flush=True,
            )
            # Body missing — do not burn retries; next scheduled poll will try again.
            if last.get("pageReady") is False:
                return last
            if last.get("cursorModelsPct") is not None or last.get("otherModelsPct") is not None:
                return last
            if page_requires_login_from_extract(last):
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
