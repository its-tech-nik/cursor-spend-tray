from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


APP_NAME = "cursor-spend-tray"
SPENDING_URL = "https://cursor.com/dashboard/spending"
DEFAULT_POLL_SECONDS = 10 * 60
DEFAULT_BIDI_PORT = 9222


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

    def zen_launch_command(self) -> str:
        """Shell command to start Zen with Remote Agent enabled for scraping."""
        return (
            f"zen-browser --remote-debugging-port={self.bidi_port} "
            "--remote-allow-hosts=localhost"
        )

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
