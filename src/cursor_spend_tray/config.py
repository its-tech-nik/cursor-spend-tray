from __future__ import annotations

import shlex
from pathlib import Path

from pydantic import BaseModel, PrivateAttr

from .browser import (
    BrowserInfo,
    browser_is_headless,
    browser_is_running,
    data_dir_for_app,
    launch_argv,
    profile_dir_for,
    resolve_automation_browser,
    stop_automation_browser,
)


APP_NAME = "cursor-spend-tray"
SPENDING_URL = "https://cursor.com/dashboard/spending"
# Spending URL redirects to sign-in when the dedicated profile has no session.
LOGIN_URL = SPENDING_URL
# Context-menu choices (minutes). Default is 8; older installs may still have 10.
POLL_INTERVAL_MINUTES: tuple[int, ...] = (1, 2, 4, 8, 16)
DEFAULT_POLL_SECONDS = 8 * 60
DEFAULT_BIDI_PORT = 9222


def poll_interval_label(minutes: int) -> str:
    """Human label for a poll interval, e.g. '8 minutes'."""
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def data_dir() -> Path:
    return data_dir_for_app(APP_NAME)


def config_path() -> Path:
    return data_dir() / "config.json"


def state_path() -> Path:
    return data_dir() / "state.json"


def zen_is_running() -> bool:
    """True when the dedicated automation browser profile is running."""
    return browser_is_running(resolve_automation_browser(), app_name=APP_NAME)


class AppConfig(BaseModel):
    bidi_host: str = "127.0.0.1"
    bidi_port: int = DEFAULT_BIDI_PORT
    poll_seconds: int = DEFAULT_POLL_SECONDS
    spending_url: str = SPENDING_URL
    dedicated_tab: bool = True

    _browser: BrowserInfo | None = PrivateAttr(default=None)

    def bidi_http_base(self) -> str:
        return f"http://{self.bidi_host}:{self.bidi_port}"

    def debug_http_base(self) -> str:
        return self.bidi_http_base()

    @property
    def browser(self) -> BrowserInfo:
        if self._browser is None:
            self._browser = resolve_automation_browser()
        return self._browser

    def refresh_browser(self) -> BrowserInfo:
        self._browser = resolve_automation_browser()
        return self._browser

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


class UsageSnapshot(BaseModel):
    cursor_models_pct: int | None = None
    other_models_pct: int | None = None
    fetched_at: float | None = None
    source: str = "none"
    error: str | None = None
    raw_hint: str | None = None

    def save(self) -> None:
        state_path().write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> UsageSnapshot:
        path = state_path()
        if not path.exists():
            return cls()
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
