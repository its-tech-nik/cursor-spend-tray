"""Detect the default browser and build family-specific automation launch argv."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


class BrowserFamily(str, Enum):
    FIREFOX = "firefox"
    CHROMIUM = "chromium"


@dataclass(frozen=True)
class BrowserInfo:
    """Resolved automation browser (dedicated profile, not the daily session)."""

    family: BrowserFamily
    key: str  # zen | firefox | chrome | brave | helium | chromium | …
    binary: str
    display_name: str
    desktop_id: str | None = None


# Exec basenames / desktop ids → (family, key, display)
_KNOWN: dict[str, tuple[BrowserFamily, str, str]] = {
    "zen": (BrowserFamily.FIREFOX, "zen", "Zen"),
    "zen-browser": (BrowserFamily.FIREFOX, "zen", "Zen"),
    "zen-bin": (BrowserFamily.FIREFOX, "zen", "Zen"),
    "firefox": (BrowserFamily.FIREFOX, "firefox", "Firefox"),
    "firefox-bin": (BrowserFamily.FIREFOX, "firefox", "Firefox"),
    "firefox-esr": (BrowserFamily.FIREFOX, "firefox", "Firefox"),
    "librewolf": (BrowserFamily.FIREFOX, "librewolf", "LibreWolf"),
    "floorp": (BrowserFamily.FIREFOX, "floorp", "Floorp"),
    "waterfox": (BrowserFamily.FIREFOX, "waterfox", "Waterfox"),
    "google-chrome": (BrowserFamily.CHROMIUM, "chrome", "Chrome"),
    "google-chrome-stable": (BrowserFamily.CHROMIUM, "chrome", "Chrome"),
    "chrome": (BrowserFamily.CHROMIUM, "chrome", "Chrome"),
    "chromium": (BrowserFamily.CHROMIUM, "chromium", "Chromium"),
    "chromium-browser": (BrowserFamily.CHROMIUM, "chromium", "Chromium"),
    "brave": (BrowserFamily.CHROMIUM, "brave", "Brave"),
    "brave-browser": (BrowserFamily.CHROMIUM, "brave", "Brave"),
    "helium": (BrowserFamily.CHROMIUM, "helium", "Helium"),
    "helium-browser": (BrowserFamily.CHROMIUM, "helium", "Helium"),
    "microsoft-edge": (BrowserFamily.CHROMIUM, "edge", "Edge"),
    "microsoft-edge-stable": (BrowserFamily.CHROMIUM, "edge", "Edge"),
    "vivaldi": (BrowserFamily.CHROMIUM, "vivaldi", "Vivaldi"),
    "opera": (BrowserFamily.CHROMIUM, "opera", "Opera"),
}

# Prefer Chromium when the default is missing/unsupported (per product choice).
_CHROMIUM_FALLBACKS: tuple[tuple[str, ...], ...] = (
    ("google-chrome-stable", "google-chrome", "chrome"),
    ("helium-browser", "helium"),
    ("brave", "brave-browser"),
    ("chromium", "chromium-browser"),
    ("microsoft-edge-stable", "microsoft-edge"),
    ("vivaldi",),
)

_FIREFOX_FALLBACKS: tuple[tuple[str, ...], ...] = (
    ("zen-browser", "zen"),
    ("firefox", "firefox-bin", "firefox-esr"),
    ("librewolf",),
    ("floorp",),
)

_EXTRA_PATHS: dict[str, tuple[str, ...]] = {
    "zen": ("/opt/zen-browser-bin/zen", "/opt/zen-browser-bin/zen-bin", "/usr/bin/zen-browser"),
    "chrome": ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"),
    "helium": ("/usr/bin/helium-browser", "/opt/helium-browser-bin/helium-browser"),
    "brave": ("/usr/bin/brave", "/usr/bin/brave-browser"),
}


def data_dir_for_app(app_name: str = "cursor-spend-tray") -> Path:
    path = Path.home() / ".local" / "share" / app_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_dir_for(info: BrowserInfo, app_name: str = "cursor-spend-tray") -> Path:
    """Dedicated profile so scraping does not attach to the daily session."""
    # Keep historical Zen path so existing logins survive.
    name = "zen-profile" if info.key == "zen" else f"{info.key}-profile"
    path = data_dir_for_app(app_name) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_automation_browser() -> BrowserInfo:
    """Pick the browser used for scraping: default if supported, else Chromium, else Firefox."""
    detected = detect_default_browser()
    if detected is not None:
        log.info(
            "Using default browser %s (%s, %s)",
            detected.display_name,
            detected.family.value,
            detected.binary,
        )
        return detected

    for names in _CHROMIUM_FALLBACKS:
        found = _resolve_binary(names)
        if found:
            key = _KNOWN.get(Path(found).name.lower(), (BrowserFamily.CHROMIUM, "chrome", "Chrome"))
            info = BrowserInfo(family=key[0], key=key[1], binary=found, display_name=key[2])
            log.info("No usable default; falling back to %s (%s)", info.display_name, info.binary)
            return info

    for names in _FIREFOX_FALLBACKS:
        found = _resolve_binary(names)
        if found:
            key = _KNOWN.get(Path(found).name.lower(), (BrowserFamily.FIREFOX, "firefox", "Firefox"))
            info = BrowserInfo(family=key[0], key=key[1], binary=found, display_name=key[2])
            log.info("No Chromium fallback; using %s (%s)", info.display_name, info.binary)
            return info

    # Last resort: keep today's Zen-oriented defaults so Launch Browser still shows a command.
    zen = _resolve_binary(("zen-browser", "zen")) or "zen-browser"
    return BrowserInfo(
        family=BrowserFamily.FIREFOX,
        key="zen",
        binary=zen,
        display_name="Zen",
    )


def detect_default_browser() -> BrowserInfo | None:
    """Resolve the XDG default web browser into a known Firefox/Chromium family entry."""
    desktop_id = _default_desktop_id()
    if not desktop_id:
        return None
    desktop_path = _find_desktop_file(desktop_id)
    exec_bin = _exec_from_desktop(desktop_path) if desktop_path else None
    candidates = [
        Path(exec_bin).name.lower() if exec_bin else "",
        desktop_id.lower().removesuffix(".desktop"),
        re.sub(r"^userapp-", "", desktop_id.lower().removesuffix(".desktop")),
    ]
    for cand in candidates:
        if not cand:
            continue
        tokens = {cand, cand.split("-")[0]}
        # userapp-Zen-8D5D62 → also try "zen"
        stripped = re.sub(r"^userapp-", "", cand)
        tokens.add(stripped)
        tokens.add(stripped.split("-")[0])
        for token in tokens:
            known = _KNOWN.get(token)
            if not known:
                continue
            family, key, display = known
            binary = exec_bin or _resolve_binary(_binaries_for_key(key))
            if not binary:
                continue
            return BrowserInfo(
                family=family,
                key=key,
                binary=binary,
                display_name=display,
                desktop_id=desktop_id,
            )
    # Fuzzy: basename contains a known token
    if exec_bin:
        name = Path(exec_bin).name.lower()
        for token, known in _KNOWN.items():
            if token in name:
                family, key, display = known
                return BrowserInfo(
                    family=family,
                    key=key,
                    binary=exec_bin,
                    display_name=display,
                    desktop_id=desktop_id,
                )
    log.info("Default browser desktop %s is unsupported", desktop_id)
    return None


def launch_argv(info: BrowserInfo, *, port: int, app_name: str = "cursor-spend-tray") -> list[str]:
    """Argv for an isolated automation instance with remote debugging enabled."""
    profile = str(profile_dir_for(info, app_name))
    if info.family is BrowserFamily.FIREFOX:
        return [
            info.binary,
            "--new-instance",
            "--profile",
            profile,
            "--headless",
            f"--remote-debugging-port={port}",
            "--remote-allow-hosts=localhost",
        ]
    return [
        info.binary,
        f"--user-data-dir={profile}",
        "--headless=new",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def browser_is_running(info: BrowserInfo, *, app_name: str = "cursor-spend-tray") -> bool:
    """True when the dedicated automation profile instance is up."""
    profile = str(profile_dir_for(info, app_name))
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
        if "-contentproc" in joined or "--type=" in joined:
            continue
        if profile not in parts and f"--profile={profile}" not in joined and f"--user-data-dir={profile}" not in joined:
            # Also accept space-separated --user-data-dir PATH form
            if "--user-data-dir" in parts:
                try:
                    idx = parts.index("--user-data-dir")
                    if idx + 1 < len(parts) and parts[idx + 1] == profile:
                        return True
                except ValueError:
                    pass
            if "--profile" in parts:
                try:
                    idx = parts.index("--profile")
                    if idx + 1 < len(parts) and parts[idx + 1] == profile:
                        return True
                except ValueError:
                    pass
            continue
        return True
    return False


def _default_desktop_id() -> str | None:
    for argv in (
        ["xdg-settings", "get", "default-web-browser"],
        ["xdg-mime", "query", "default", "x-scheme-handler/https"],
        ["xdg-mime", "query", "default", "x-scheme-handler/http"],
    ):
        try:
            out = subprocess.check_output(argv, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            continue
        if out and "no default" not in out.lower():
            return out
    # mimeapps.list fallback
    for path in (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mimeapps.list",
        Path.home() / ".config" / "mimeapps.list",
    ):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("x-scheme-handler/https="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value.split(";")[0].strip() or None
    return None


def _find_desktop_file(desktop_id: str) -> Path | None:
    bases = [
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")),
        Path("/usr/local/share"),
        Path("/usr/share"),
    ]
    for base in bases:
        candidate = base / "applications" / desktop_id
        if candidate.is_file():
            return candidate
    return None


def _exec_from_desktop(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        if not raw.startswith("Exec="):
            continue
        # Skip Desktop Action groups — first Exec in [Desktop Entry] wins.
        cmd = raw[5:].strip()
        # Strip field codes and split
        cmd = re.sub(r"\s*%[a-zA-Z]", "", cmd).strip()
        if not cmd:
            continue
        parts = shlex.split(cmd)
        if not parts:
            continue
        binary = parts[0]
        if Path(binary).is_file() or shutil.which(binary):
            return shutil.which(binary) or binary
        return binary
    return None


def _binaries_for_key(key: str) -> tuple[str, ...]:
    reverse: list[str] = []
    for name, (_fam, k, _disp) in _KNOWN.items():
        if k == key:
            reverse.append(name)
    return tuple(dict.fromkeys(reverse))  # preserve order, unique


def _resolve_binary(names: tuple[str, ...]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        key = _KNOWN.get(name, (None, name, None))[1]
        for candidate in _EXTRA_PATHS.get(key or "", ()):
            if Path(candidate).is_file():
                return candidate
        # bare path if name looks absolute
        if name.startswith("/") and Path(name).is_file():
            return name
    return None
