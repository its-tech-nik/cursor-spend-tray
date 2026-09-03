"""XDG autostart (~/.config/autostart) for launch-at-login."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from .config import APP_NAME

log = logging.getLogger(__name__)

_DESKTOP_NAME = f"{APP_NAME}.desktop"


def autostart_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "autostart"


def autostart_desktop_path() -> Path:
    return autostart_dir() / _DESKTOP_NAME


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes"}


def is_enabled() -> bool:
    """True when a user autostart entry exists and is not Hidden/disabled."""
    path = autostart_desktop_path()
    if not path.is_file():
        return False
    hidden = False
    gnome_enabled = True
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key == "hidden":
            hidden = _truthy(value)
        elif key == "x-gnome-autostart-enabled":
            gnome_enabled = _truthy(value)
    return (not hidden) and gnome_enabled


def _exec_command() -> str:
    """Command that should work after a graphical login."""
    found = shutil.which("cursor-spend-tray")
    if found:
        return found
    argv0 = Path(sys.argv[0]).expanduser()
    try:
        argv0 = argv0.resolve()
    except OSError:
        pass
    if argv0.is_file() and os.access(argv0, os.X_OK) and argv0.name != "__main__.py":
        return str(argv0)
    return f"{sys.executable} -m cursor_spend_tray"


def _desktop_contents() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Cursor Spend Tray\n"
        "GenericName=Spending Monitor\n"
        "Comment=Track Cursor Pro spending from the system tray\n"
        f"Exec={_exec_command()}\n"
        "Icon=cursor-spend-tray\n"
        "Terminal=false\n"
        "Categories=Utility;Monitor;\n"
        "StartupNotify=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-KDE-autostart-after=panel\n"
        "Hidden=false\n"
    )


def set_enabled(enabled: bool) -> None:
    """Create or remove the user autostart desktop file."""
    path = autostart_desktop_path()
    if not enabled:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.exception("Failed to remove autostart entry %s", path)
            raise
        return

    autostart_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(_desktop_contents(), encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o100)
