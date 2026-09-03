#!/usr/bin/env bash
# Build from the current tree and install locally. Does not publish to the AUR.
# Usage: ./packaging/aur/install-local.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
"$ROOT/packaging/aur/build-local.sh"
shopt -s nullglob
pkgs=("$ROOT"/dist/cursor-spend-tray-*.pkg.tar.*)
if ((${#pkgs[@]} == 0)); then
  echo "No package in $ROOT/dist" >&2
  exit 1
fi
latest="$(ls -1t "${pkgs[@]}" | head -1)"
echo "Installing $latest"
if command -v yay >/dev/null; then
  yay -U --noconfirm "$latest"
else
  sudo pacman -U --noconfirm "$latest"
fi
if command -v update-desktop-database >/dev/null; then
  sudo update-desktop-database /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null; then
  sudo gtk-update-icon-cache -f /usr/share/icons/hicolor || true
fi
echo "KRunner should find “Cursor Spend Tray” (also try “cursor” or “spend”)."
