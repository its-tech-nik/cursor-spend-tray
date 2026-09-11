---
name: install-app
description: >-
  Install or reinstall Cursor Spend Tray on the local machine from this repo.
  Use when the user asks to install the app, reinstall the latest build, set up
  the tray package, run install-local.sh, or get Cursor Spend Tray into KRunner
  / the system tray.
disable-model-invocation: true
---

# Install Cursor Spend Tray

Follow these steps. Do **not** defer to the README — this skill has the install procedure.

Work from the repo root:

```bash
cd /home/technik/Documents/Projects/cursor-spend-tray
```

(If the workspace path differs, use the current project root.)

## Choose the install path

1. **Arch Linux (preferred when `pacman` / `yay` exist)** → [Arch local package](#arch-linux-local-package)
2. **Any other distro, or a one-off run without packaging** → [From source](#from-source-any-distro)

Detect with: `command -v pacman` and `command -v yay`.

## Arch Linux (local package)

The app is **not** on the AUR. Build a local package from this tree and install it so Plasma/KRunner can launch **Cursor Spend Tray** (`/usr/share/applications/cursor-spend-tray.desktop`).

### Install / reinstall (latest tree)

```bash
./packaging/aur/install-local.sh
```

That is equivalent to:

```bash
./packaging/aur/build-local.sh
yay -U dist/cursor-spend-tray-*.pkg.tar.zst
# or, without yay:
# sudo pacman -U dist/cursor-spend-tray-*.pkg.tar.zst
```

What the script does:

1. Builds `dist/cursor-spend-tray-*-any.pkg.tar.zst` from the current checkout
2. Installs the newest matching package with `yay -U --noconfirm` if `yay` exists, else `sudo pacman -U --noconfirm`
3. Refreshes the desktop database / hicolor icon cache when those tools exist

After a successful install:

- Binary: `/usr/bin/cursor-spend-tray`
- Desktop entry: `/usr/share/applications/cursor-spend-tray.desktop`
- Launch via KRunner: type `cursor`, `spend`, or **Cursor Spend Tray**
- If the tray is already running, restart it so it picks up the new build

### Sudo / password in agent shells

Package install needs root. Non-interactive agent terminals often fail with:

`sudo: a terminal is required to read the password`

If that happens:

1. Confirm the package **built** (file under `dist/cursor-spend-tray-*.pkg.tar.zst`)
2. Retry install with a GUI askpass when available, e.g.:

```bash
SUDO_ASKPASS=/usr/bin/ksshaskpass yay -U --noconfirm --sudoflags="-A" dist/cursor-spend-tray-0.1.0-1-any.pkg.tar.zst
```

(Adjust the exact `.pkg.tar.zst` name to the latest under `dist/`.)

3. If askpass is unavailable, tell the user to run in their own terminal:

```bash
yay -U --noconfirm /absolute/path/to/dist/cursor-spend-tray-*.pkg.tar.zst
# or
sudo pacman -U --noconfirm /absolute/path/to/dist/cursor-spend-tray-*.pkg.tar.zst
```

### Uninstall (Arch package)

Removes the binary, `.desktop` entry, and hicolor icons — **not** user data:

```bash
sudo pacman -Rns cursor-spend-tray
# or: yay -Rns cursor-spend-tray
```

Optional wipe of config / cache / dedicated browser profiles:

```bash
rm -rf ~/.local/share/cursor-spend-tray
```

## From source (any distro)

For running without a system package:

```bash
uv sync
uv run cursor-spend-tray
```

Notes:

- On Wayland (e.g. Plasma), the app defaults to **XWayland (`QT_QPA_PLATFORM=xcb`)** so the popup can move and dismiss on outside click
- Icon click coords come from Plasma StatusNotifierItem `Activate(x, y)` (Qt `QSystemTrayIcon.geometry()` is empty on Linux)

## Packaging status (do not invent installers)

- **Arch local package:** supported via `packaging/aur/` + `install-local.sh`
- **AUR publish:** not available yet
- **Debian/Ubuntu `.deb`:** not packaged yet — use [From source](#from-source-any-distro)

## After install checklist

- [ ] Package or `uv run` succeeded
- [ ] User can launch from KRunner / menu (Arch) or `uv run cursor-spend-tray` (source)
- [ ] If replacing a running instance, remind them to restart the tray app
- [ ] First-time browser login still uses the dedicated automation profile under `~/.local/share/cursor-spend-tray/` (separate from daily browser)

## Agent behavior

- Prefer executing the Arch script when on Arch; do not only print commands unless sudo blocks you
- Rebuild from the **current** tree so local commits are included
- After install, report the installed package version (`pacman -Q cursor-spend-tray`) when on Arch
- Do not delete `~/.local/share/cursor-spend-tray/` unless the user explicitly asks to wipe data
