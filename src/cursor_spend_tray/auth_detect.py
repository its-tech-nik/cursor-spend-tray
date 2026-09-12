"""Detect Cursor auth redirects / bot interstitials that require headed sign-in."""

from __future__ import annotations

import re
from typing import Any

from .config import UsageSnapshot

# Hosts / URL fragments that mean we are not on the spending dashboard.
_AUTH_URL_MARKERS: tuple[str, ...] = (
    "authenticator.cursor.sh",
    "accounts.cursor.com",
    "/login",
    "/sign-in",
    "/signin",
    "auth.cursor",
)

# Body text that indicates login or a Cloudflare-style gate in front of auth.
_AUTH_HINT_RE = re.compile(
    r"sign\s*in|log\s*in|authenticate|"
    r"performing security verification|"
    r"verify you are not a bot|"
    r"just a moment|attention required|"
    r"cf-browser-verification|challenge-platform",
    re.I,
)

_DASHBOARD_HINT_RE = re.compile(r"included in pro|cursor models|other models", re.I)


def url_suggests_auth(url: str | None) -> bool:
    """True when a URL looks like Cursor auth / accounts (not the spending dashboard)."""
    url_l = (url or "").strip().lower()
    if not url_l:
        return False
    return any(marker in url_l for marker in _AUTH_URL_MARKERS)


def page_requires_login(
    *,
    url: str | None = None,
    hint: str | None = None,
    logged_out_flag: bool = False,
    has_spend_data: bool = False,
) -> bool:
    """True when the loaded page is auth / bot-check instead of spending usage.

    Shared by scrape classification (browser switch, first launch, and polls).
    """
    if has_spend_data:
        return False
    if logged_out_flag:
        return True

    url_l = (url or "").strip().lower()
    hint_l = (hint or "").strip().lower()
    combined = f"{url_l}\n{hint_l}"

    if any(marker in combined for marker in _AUTH_URL_MARKERS):
        return True
    if _AUTH_HINT_RE.search(hint_l) and not _DASHBOARD_HINT_RE.search(hint_l):
        return True
    # Spending URL that bounced to a challenge without dashboard chrome.
    if "cursor.com" in url_l and "spending" not in url_l and _AUTH_HINT_RE.search(hint_l):
        return True
    return False


def page_requires_login_from_extract(data: dict[str, Any]) -> bool:
    """Classify an EXTRACT_JS result dict."""
    cursor = data.get("cursorModelsPct")
    other = data.get("otherModelsPct")
    has_spend = cursor is not None or other is not None
    return page_requires_login(
        url=data.get("pageUrl") if isinstance(data.get("pageUrl"), str) else None,
        hint=data.get("hint") if isinstance(data.get("hint"), str) else None,
        logged_out_flag=bool(data.get("loggedOut")),
        has_spend_data=has_spend,
    )


def snapshot_needs_login(snap: UsageSnapshot) -> bool:
    """True when a scrape snapshot should open the headed sign-in flow."""
    if snap.source == "logged_out":
        return True
    # Belts-and-suspenders: parse errors that still look like auth pages.
    if snap.source in {"bidi", "cdp", "error"} and snap.cursor_models_pct is None:
        return page_requires_login(
            url=None,
            hint=snap.raw_hint,
            logged_out_flag=False,
            has_spend_data=False,
        )
    return False
