---
name: install
description: >-
  Install or reinstall Cursor Spend Tray on the local machine from this repo.
disable-model-invocation: true
---

# Install Cursor Spend Tray

Work from the repo root:

```bash
cd /home/technik/Documents/Projects/cursor-spend-tray
```

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
