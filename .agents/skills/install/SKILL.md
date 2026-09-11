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

1. **Arch Linux (preferred when `pacman` exists)** → [Arch local package](#arch-linux-local-package)
2. **Any other distro, or a one-off run without packaging** → [From source](#from-source-any-distro)

Detect with: `command -v pacman`.

## Arch Linux (local package)

The app is **not** on the AUR. Build a local package from this tree and install it so Plasma/KRunner can launch **Cursor Spend Tray** (`/usr/share/applications/cursor-spend-tray.desktop`).

### Install / reinstall (latest tree)

```bash
./packaging/aur/install-local.sh
```

That is equivalent to:

```bash
./packaging/aur/build-local.sh
sudo pacman -U dist/cursor-spend-tray-*.pkg.tar.zst
```

What the script does:

1. Takes an exclusive lock (`dist/.install-local.lock`) so overlapping skill runs (e.g. pi + Cursor) do not both elevate — waiters skip after the winner finishes
2. Builds `dist/cursor-spend-tray-*-any.pkg.tar.zst` from the current checkout
3. Installs the newest matching package with a **single** `sudo pacman -U --noconfirm` (desktop DB / icon cache come from pacman hooks in that same transaction)

For non-interactive / agent shells with a GUI askpass:

```bash
SUDO_ASKPASS=/usr/bin/ksshaskpass ./packaging/aur/install-local.sh
```

(`install-local.sh` passes `sudo -A` when `SUDO_ASKPASS` is set.)

If askpass is unavailable, tell the user to run in their own terminal:

```bash
./packaging/aur/install-local.sh
# or:
sudo pacman -U --noconfirm /absolute/path/to/dist/cursor-spend-tray-*.pkg.tar.zst
```
