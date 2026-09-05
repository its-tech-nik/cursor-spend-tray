# Cursor Spend Tray

## Purpose

Track your **Cursor Pro** spending from the Linux system tray, without a public API.

Cursor’s Pro tier exposes usage on [cursor.com/dashboard/spending](https://cursor.com/dashboard/spending), but not through an official API. This app keeps a small tray icon (with live mini usage rings) and a popup that mirrors the **Included in Pro** view — **AUTO** and **API** percentages — by reading that page through a dedicated browser profile, separate from your daily session.

It polls on a configurable interval (default **8 minutes**; choose 1 / 2 / 4 / 8 / 16 from the tray menu), caches the last good snapshot, and lets you refresh immediately by clicking the countdown in the popup.

## How it works

- **Stack:** Python + PyQt6 tray popup (Plasma StatusNotifierItem for icon click coords)
- **Browser:** Detects your XDG default browser; Firefox-family uses WebDriver BiDi, Chromium-family (Chrome / Brave / Helium / …) uses CDP, both on `127.0.0.1:9222`
- **Fallback:** If the default is missing/unsupported, prefers an installed Chromium browser, then Firefox/Zen
- **Launcher:** on startup the app starts a dedicated automation profile (popup still shows a copyable command if needed)
- **Dedicated spending tab:** the app reuses/reloads that tab
- **Countdown:** popup shows time until next poll; **click the timer to refresh now**

## Install

### Arch Linux (local package)

The app is not on the AUR. Build from this repo and install with `yay` or `pacman` so Plasma/KRunner can launch **Cursor Spend Tray** like any other app (`/usr/share/applications/cursor-spend-tray.desktop`).

```bash
./packaging/aur/install-local.sh
# same as: ./packaging/aur/build-local.sh && yay -U dist/cursor-spend-tray-*.pkg.tar.zst
```

After install, open KRunner and type `cursor`, `spend`, or `Cursor Spend Tray`.

Undo (remove the local package and launcher):

```bash
sudo pacman -Rns cursor-spend-tray
# or: yay -Rns cursor-spend-tray
```

That deletes `/usr/bin/cursor-spend-tray`, the `.desktop` entry, and the hicolor icons. It does **not** remove `~/.local/share/cursor-spend-tray/` (config, cache, dedicated browser profiles). To wipe those too:

```bash
rm -rf ~/.local/share/cursor-spend-tray
```

### From source (any distro)

```bash
uv sync
uv run cursor-spend-tray
```

On Wayland (e.g. Plasma), the app defaults to **XWayland (`QT_QPA_PLATFORM=xcb`)** so the popup can be moved and dismissed on outside click. Icon position comes from Plasma’s `Activate(x, y)` (Qt’s `QSystemTrayIcon.geometry()` is always empty on Linux). The popup opens under a top panel icon and above a bottom panel icon.

### Packaging notes

- **Local Arch package:** `packaging/aur/PKGBUILD` installs `cursor-spend-tray`, a `.desktop` entry, and hicolor icons. Use `./packaging/aur/install-local.sh` (no AUR account).
- **AUR publish:** skipped for now (registration). [`.github/workflows/aur.yml`](.github/workflows/aur.yml) is unused until an AUR package exists.
- **Debian/Ubuntu (.deb):** not packaged yet; same app can be wrapped later with a `debian/` package or something like `fpm` once an Arch package is solid.

## Dedicated automation browser

The tray detects your default browser via `xdg-settings` / `xdg-mime` and launches an isolated profile so your daily session stays untouched.

**Firefox-family** (Firefox, Zen, LibreWolf, …) — WebDriver BiDi:

```bash
zen-browser --new-instance --profile ~/.local/share/cursor-spend-tray/zen-profile --headless --remote-debugging-port=9222 --remote-allow-hosts=localhost
```

**Chromium-family** (Chrome, Brave, Helium, …) — Chrome DevTools Protocol:

```bash
brave --user-data-dir=~/.local/share/cursor-spend-tray/brave-profile --headless=new --remote-debugging-port=9222 --no-first-run --no-default-browser-check
```

Sign into Cursor once in that dedicated profile; the login persists across restarts of the same profile path. Open (or leave) the spending page, then click the tray countdown to pull fresh numbers.

Without remote debugging listening, the tray still runs and shows last cached values / a connection status message. The popup copies the launch command for the detected browser.

## Launch at login

Right-click the tray icon and check **Launch at login**. That writes
`~/.config/autostart/cursor-spend-tray.desktop` (XDG autostart; Plasma, GNOME, and most
other desktops pick it up on the next graphical login). Uncheck to remove it.

## Refresh interval

Right-click the tray icon → **Refresh interval** and pick 1, 2, 4, 8, or 16 minutes.
The choice is saved in `~/.local/share/cursor-spend-tray/config.json` (`poll_seconds`) and
restored the next time the app starts.

## Config / cache

- `~/.local/share/cursor-spend-tray/config.json` — poll interval, host/port
- `~/.local/share/cursor-spend-tray/state.json` — last snapshot
- `~/.local/share/cursor-spend-tray/usage-csv/` — per billing-period usage-events CSVs + token totals; `period-spend-pct.json` links scraped AUTO/API % to the current period
- `~/.local/share/cursor-spend-tray/*-profile` — dedicated automation profile (e.g. `zen-profile`, `brave-profile`)
- `~/.config/autostart/cursor-spend-tray.desktop` — optional launch-at-login entry

Default poll interval: **8 minutes**.
