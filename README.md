# Cursor Spend Tray

## Purpose

Track your **Cursor Pro** spending from the Linux system tray, without a public API.

Cursor’s Pro tier exposes usage on [cursor.com/dashboard/spending](https://cursor.com/dashboard/spending), but not through an official API. This app keeps a small tray icon (with live mini usage bars) and a popup that mirrors the **Included in Pro** view — **Cursor Models** and **Other Models** percentages — by reading that page through your already-logged-in **Zen Browser** session (Firefox Remote Agent / WebDriver BiDi).

It polls every **10 minutes**, caches the last good snapshot, and lets you refresh immediately by clicking the countdown in the popup.

## How it works

- **Stack:** Python + PyQt6 tray popup (Plasma StatusNotifierItem for icon click coords)
- **Browser:** Reuse your daily Zen session via BiDi on `127.0.0.1:9222`
- **Zen launcher wrapper:** not installed yet (seamless connection comes later). Start Zen with remote debugging yourself when scraping.
- **Dedicated spending tab:** the app reuses/reloads that tab
- **Countdown:** popup shows time until next poll; **click the timer to refresh now**

## Install

### Arch Linux (AUR)

Once published to the AUR:

```bash
yay -S cursor-spend-tray
# or: paru -S cursor-spend-tray
```

Build the package locally from this repo (no AUR account needed):

```bash
./packaging/aur/build-local.sh
sudo pacman -U dist/cursor-spend-tray-*.pkg.tar.zst
```

### From source (any distro)

```bash
uv sync
uv run cursor-spend-tray
```

On Wayland (e.g. Plasma), the app defaults to **XWayland (`QT_QPA_PLATFORM=xcb`)** so the popup can be moved and dismissed on outside click. Icon position comes from Plasma’s `Activate(x, y)` (Qt’s `QSystemTrayIcon.geometry()` is always empty on Linux). The popup opens under a top panel icon and above a bottom panel icon.

### Packaging notes

- **AUR:** `packaging/aur/PKGBUILD` installs a system `cursor-spend-tray` command plus a `.desktop` entry, using Arch’s `python-pyqt6` / `python-httpx` / etc.
- **AUR CI:** publishing a GitHub Release runs [`.github/workflows/aur.yml`](.github/workflows/aur.yml), which bumps `pkgver`/`sha256sums`, regenerates `.SRCINFO`, and pushes to `aur.archlinux.org`. Requires repo secret `AUR_SSH_PRIVATE_KEY` (and a one-time empty AUR package). Optional secrets: `AUR_USERNAME`, `AUR_EMAIL`. You can also run the workflow manually via **Actions → Publish AUR**.
- **Debian/Ubuntu (.deb):** not packaged yet; same app can be wrapped later with a `debian/` package or something like `fpm` once an Arch package is solid.

## Temporary Zen connection (until seamless setup)

Quit Zen fully, then start once with:

```bash
zen-browser --remote-debugging-port=9222 --remote-allow-hosts=localhost
```

Stay logged into Cursor, open (or leave) the spending page. Click the tray icon, then click the countdown to pull fresh numbers.

Without the Remote Agent listening, the tray still runs and shows last cached values / a connection status message.

## Config / cache

- `~/.local/share/cursor-spend-tray/config.json` — poll interval, host/port
- `~/.local/share/cursor-spend-tray/state.json` — last snapshot

Default poll interval: **10 minutes**.
