from __future__ import annotations

import json
import shlex
import time
from datetime import datetime
from datetime import time as time_of_day
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, PrivateAttr, model_validator

from .browser import (
    BrowserInfo,
    browser_is_headless,
    browser_is_running,
    data_dir_for_app,
    launch_argv,
    list_installed_browsers,
    profile_dir_for,
    resolve_automation_browser,
    stop_automation_browser,
)

BetweenScrapesMode = Literal["keep_open", "quit"]


APP_NAME = "cursor-spend-tray"
SPENDING_URL = "https://cursor.com/dashboard/spending"
SETTINGS_URL = "https://cursor.com/dashboard/settings"
# Spending URL redirects to sign-in when the dedicated profile has no session.
LOGIN_URL = SPENDING_URL
# Context-menu choices (minutes). Default is 8; older installs may still have 10.
POLL_INTERVAL_MINUTES: tuple[int, ...] = (1, 2, 4, 8, 16)
DEFAULT_POLL_SECONDS = 8 * 60
DEFAULT_BIDI_PORT = 9222
# Stable fallback billing day when state.json has no reset stamp yet.
SUBSCRIPTION_RENEWAL_DAY = 19


def poll_interval_label(minutes: int) -> str:
    """Human label for a poll interval, e.g. '8 minutes'."""
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def data_dir() -> Path:
    return data_dir_for_app(APP_NAME)


def config_path() -> Path:
    return data_dir() / "config.json"


def state_path() -> Path:
    return data_dir() / "state.json"


def panel_order_path() -> Path:
    return data_dir() / "popup_panel_order.json"


DEFAULT_POPUP_PANEL_ORDER: tuple[str, ...] = ("spend", "habits", "composer")


def load_popup_panel_order() -> list[str]:
    """Return saved vertical order for the three main popup cards."""
    path = panel_order_path()
    known = set(DEFAULT_POPUP_PANEL_ORDER)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                order = [str(x) for x in raw if str(x) in known]
                for key in DEFAULT_POPUP_PANEL_ORDER:
                    if key not in order:
                        order.append(key)
                return order
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return list(DEFAULT_POPUP_PANEL_ORDER)


def save_popup_panel_order(order: list[str]) -> None:
    known = set(DEFAULT_POPUP_PANEL_ORDER)
    cleaned = [x for x in order if x in known]
    for key in DEFAULT_POPUP_PANEL_ORDER:
        if key not in cleaned:
            cleaned.append(key)
    data_dir().mkdir(parents=True, exist_ok=True)
    panel_order_path().write_text(json.dumps(cleaned, indent=2), encoding="utf-8")


def zen_is_running() -> bool:
    """True when the dedicated automation browser profile is running."""
    return browser_is_running(resolve_automation_browser(), app_name=APP_NAME)


class AppConfig(BaseModel):
    bidi_host: str = "127.0.0.1"
    bidi_port: int = DEFAULT_BIDI_PORT
    poll_seconds: int = DEFAULT_POLL_SECONDS
    spending_url: str = SPENDING_URL
    dedicated_tab: bool = True
    # Preferred automation browser key (zen, chrome, …). None → auto-detect.
    browser_key: str | None = None
    # keep_open: leave headless browser running between polls.
    # quit: stop after each scrape and relaunch before the next.
    between_scrapes: BetweenScrapesMode = "keep_open"

    _browser: BrowserInfo | None = PrivateAttr(default=None)

    def bidi_http_base(self) -> str:
        return f"http://{self.bidi_host}:{self.bidi_port}"

    def debug_http_base(self) -> str:
        return self.bidi_http_base()

    @property
    def browser(self) -> BrowserInfo:
        if self._browser is None:
            self._browser = resolve_automation_browser(self.browser_key)
        return self._browser

    def refresh_browser(self) -> BrowserInfo:
        self._browser = resolve_automation_browser(self.browser_key)
        return self._browser

    def set_browser_key(self, key: str | None) -> BrowserInfo:
        """Persist a preferred automation browser and refresh the resolved BrowserInfo."""
        self.browser_key = key
        self._browser = resolve_automation_browser(key)
        self.save()
        return self._browser

    def set_between_scrapes(self, mode: BetweenScrapesMode) -> None:
        self.between_scrapes = mode
        self.save()

    def installed_browsers(self) -> list[BrowserInfo]:
        return list_installed_browsers()

    def browser_is_running(self) -> bool:
        return browser_is_running(self.browser, app_name=APP_NAME)

    def browser_is_headless(self) -> bool | None:
        return browser_is_headless(self.browser, app_name=APP_NAME)

    def stop_browser(self, timeout: float = 8.0) -> bool:
        return stop_automation_browser(self.browser, app_name=APP_NAME, timeout=timeout)

    def profile_dir(self) -> Path:
        return profile_dir_for(self.browser, APP_NAME)

    def browser_launch_argv(
        self,
        *,
        headless: bool = True,
        url: str | None = None,
    ) -> list[str]:
        """Argv to start an isolated browser with remote debugging for scraping."""
        return launch_argv(
            self.browser,
            port=self.bidi_port,
            app_name=APP_NAME,
            headless=headless,
            url=url,
        )

    def browser_login_argv(self) -> list[str]:
        """Argv for a visible dedicated-profile window on the Cursor login/spending page."""
        return self.browser_launch_argv(headless=False, url=LOGIN_URL)

    def browser_launch_command(self) -> str:
        return shlex.join(self.browser_launch_argv())

    # Back-compat names used throughout the tray UI.
    def zen_launch_argv(self) -> list[str]:
        return self.browser_launch_argv()

    def zen_launch_command(self) -> str:
        return self.browser_launch_command()

    def save(self) -> None:
        config_path().write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> AppConfig:
        path = config_path()
        if not path.exists():
            cfg = cls()
            cfg.save()
            return cfg
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def default_usage_reset_at() -> float:
    """Placeholder reset stamp (renewal day at midnight local) until a real AUTO >0→0%."""
    now = datetime.now().astimezone()
    return datetime(
        now.year,
        now.month,
        SUBSCRIPTION_RENEWAL_DAY,
        0,
        0,
        tzinfo=now.tzinfo,
    ).timestamp()


def renewal_from_usage_reset(
    usage_reset_at: float | None,
) -> tuple[int, time_of_day]:
    """Billing-cycle day + local clock from the reset stamp (stable day-19 fallback)."""
    if usage_reset_at is None:
        return SUBSCRIPTION_RENEWAL_DAY, time_of_day(0, 0)
    dt = datetime.fromtimestamp(usage_reset_at).astimezone()
    day = max(1, min(28, dt.day))
    return day, time_of_day(dt.hour, dt.minute, dt.second)


def usage_reset_stamp_from_day_time(
    day: int,
    hour: int,
    minute: int,
    *,
    second: int = 0,
) -> float:
    """Build a local timestamp whose day/clock become the billing boundary."""
    now = datetime.now().astimezone()
    day = max(1, min(28, int(day)))
    return datetime(
        now.year,
        now.month,
        day,
        max(0, min(23, int(hour))),
        max(0, min(59, int(minute))),
        max(0, min(59, int(second))),
        tzinfo=now.tzinfo,
    ).timestamp()


def _day_ordinal(day: int) -> str:
    if 11 <= (day % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def format_usage_reset_label(ts: float) -> str:
    """e.g. 'resets on the 19th at 20:12'."""
    dt = datetime.fromtimestamp(ts).astimezone()
    return f"resets on the {_day_ordinal(dt.day)} at {dt.strftime('%H:%M')}"


def resolve_usage_reset_at(
    *,
    prev_cursor_pct: int | None,
    new_cursor_pct: int | None,
    prev_reset_at: float | None,
    auto_discover: bool = True,
    now: float | None = None,
) -> float | None:
    """Update the auto stamp only when auto-discover is on and AUTO drops >0 → 0%."""
    if not auto_discover:
        return prev_reset_at
    if (
        prev_cursor_pct is not None
        and prev_cursor_pct > 0
        and new_cursor_pct is not None
        and new_cursor_pct == 0
    ):
        return time.time() if now is None else now
    return prev_reset_at


class UsageSnapshot(BaseModel):
    cursor_models_pct: int | None = None
    other_models_pct: int | None = None
    # From dashboard settings / sidebar (refreshed with each successful scrape).
    account_email: str | None = None
    subscription_level: str | None = None
    account_avatar_url: str | None = None
    # Auto-discovered billing-cycle boundary (written on AUTO >0→0% when discover is on).
    usage_reset_at_auto: float | None = None
    # Manual override boundary (written by Resets on day/time pickers).
    usage_reset_at_manual: float | None = None
    # When True, use the auto stamp and allow scrapes to rewrite usage_reset_at_auto.
    usage_reset_auto_discover: bool = True
    fetched_at: float | None = None
    source: str = "none"
    error: str | None = None
    raw_hint: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_usage_reset_at(cls, data: Any) -> Any:
        """Split legacy single usage_reset_at into auto + manual fields."""
        if not isinstance(data, dict) or "usage_reset_at" not in data:
            return data
        legacy = data.pop("usage_reset_at")
        if legacy is not None:
            # Seed both so toggling modes does not drop the known stamp.
            if data.get("usage_reset_at_auto") is None:
                data["usage_reset_at_auto"] = legacy
            if data.get("usage_reset_at_manual") is None:
                data["usage_reset_at_manual"] = legacy
        return data

    def effective_usage_reset_at(self) -> float | None:
        """Stamp currently in effect for billing/UI (auto vs manual menu choice)."""
        if self.usage_reset_auto_discover:
            return self.usage_reset_at_auto
        return self.usage_reset_at_manual

    def reset_stamp_fields(self) -> dict[str, float | None | bool]:
        """Copy reset-related fields for UsageSnapshot reconstruction in the scraper."""
        return {
            "usage_reset_at_auto": self.usage_reset_at_auto,
            "usage_reset_at_manual": self.usage_reset_at_manual,
            "usage_reset_auto_discover": self.usage_reset_auto_discover,
        }

    def renewal_boundary(self) -> tuple[int, time_of_day]:
        """Day + clock for period splits (falls back to the 19th at midnight)."""
        return renewal_from_usage_reset(self.effective_usage_reset_at())

    def set_usage_reset_day_time(self, day: int, hour: int, minute: int) -> None:
        """Persist a manual day/time boundary and disable auto-discover."""
        self.usage_reset_at_manual = usage_reset_stamp_from_day_time(day, hour, minute)
        self.usage_reset_auto_discover = False
        self.save()

    def set_usage_reset_auto_discover(self, enabled: bool) -> None:
        self.usage_reset_auto_discover = bool(enabled)
        # Seed manual from auto (or default) the first time the user leaves auto.
        if not enabled and self.usage_reset_at_manual is None:
            self.usage_reset_at_manual = (
                self.usage_reset_at_auto
                if self.usage_reset_at_auto is not None
                else default_usage_reset_at()
            )
        self.save()

    def save(self) -> None:
        state_path().write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> UsageSnapshot:
        path = state_path()
        if not path.exists():
            snap = cls(usage_reset_at_auto=default_usage_reset_at())
            snap.save()
            return snap
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            had_legacy = isinstance(payload, dict) and "usage_reset_at" in payload
            snap = cls.model_validate(payload)
        except Exception:
            snap = cls(usage_reset_at_auto=default_usage_reset_at())
            snap.save()
            return snap
        changed = had_legacy
        if snap.effective_usage_reset_at() is None:
            fallback = default_usage_reset_at()
            if snap.usage_reset_auto_discover:
                snap.usage_reset_at_auto = fallback
            else:
                snap.usage_reset_at_manual = fallback
            changed = True
        if changed:
            snap.save()
        return snap
