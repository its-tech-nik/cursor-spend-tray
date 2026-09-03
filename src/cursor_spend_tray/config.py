from __future__ import annotations

import shlex
import shutil
from pathlib import Path

from pydantic import BaseModel, Field


APP_NAME = "cursor-spend-tray"
SPENDING_URL = "https://cursor.com/dashboard/spending"
DEFAULT_POLL_SECONDS = 10 * 60
DEFAULT_BIDI_PORT = 9222


def zen_binary() -> str:
    """Resolve the Zen launcher / binary on PATH or common install paths."""
    for name in ("zen-browser", "zen"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        "/opt/zen-browser-bin/zen",
        "/usr/bin/zen-browser",
        "/usr/bin/zen",
    ):
        if Path(candidate).is_file():
            return candidate
    return "zen-browser"


def zen_profile_dir() -> Path:
    """Dedicated Zen profile so scraping does not attach to the daily session."""
    path = data_dir() / "zen-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def zen_is_running() -> bool:
    """True when the tray's dedicated Zen instance is up (not the daily browser)."""
    profile = str(zen_profile_dir())
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        parts = [p.decode(errors="ignore") for p in raw.split(b"\0") if p]
        if not parts:
            continue
        joined = " ".join(parts)
        if "-contentproc" in joined:
            continue
        exe = Path(parts[0]).name.lower()
        if exe not in {"zen", "zen-browser"} and "zen-browser" not in parts[0].lower():
            continue
        if profile in parts or f"--profile={profile}" in parts:
            return True
    return False


def data_dir() -> Path:
    path = Path.home() / ".local" / "share" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return data_dir() / "config.json"


def state_path() -> Path:
    return data_dir() / "state.json"


class AppConfig(BaseModel):
    bidi_host: str = "127.0.0.1"
    bidi_port: int = DEFAULT_BIDI_PORT
    poll_seconds: int = DEFAULT_POLL_SECONDS
    spending_url: str = SPENDING_URL
    dedicated_tab: bool = True

    def bidi_http_base(self) -> str:
        return f"http://{self.bidi_host}:{self.bidi_port}"

    def zen_launch_argv(self) -> list[str]:
        """Argv to start an isolated Zen with Remote Agent enabled for scraping."""
        return [
            zen_binary(),
            "--new-instance",
            "--profile",
            str(zen_profile_dir()),
            "--headless",
            f"--remote-debugging-port={self.bidi_port}",
            "--remote-allow-hosts=localhost",
        ]

    def zen_launch_command(self) -> str:
        """Shell command to start an isolated Zen with Remote Agent enabled for scraping."""
        return shlex.join(self.zen_launch_argv())

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
