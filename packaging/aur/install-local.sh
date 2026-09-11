#!/usr/bin/env bash
# Build from the current tree and install locally. Does not publish to the AUR.
# Usage: ./packaging/aur/install-local.sh
#
# One sudo elevation per install: pacman -U. Desktop DB / icon cache refresh is
# handled by pacman alpm hooks during that transaction (no extra sudo calls).
#
# Overlapping runs (e.g. pi + Cursor both invoking this skill) share a lock: the
# winner builds+installs; waiters skip after the lock releases so sudo is only
# prompted once.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/dist"
LOCK="$ROOT/dist/.install-local.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "Another install-local.sh is in progress; waiting for it to finish…"
  flock 9
  echo "Skipping duplicate install (already completed by the other run)."
  echo "KRunner should find “Cursor Spend Tray” (also try “cursor” or “spend”)."
  exit 0
fi

"$ROOT/packaging/aur/build-local.sh"
shopt -s nullglob
pkgs=("$ROOT"/dist/cursor-spend-tray-*.pkg.tar.*)
if ((${#pkgs[@]} == 0)); then
  echo "No package in $ROOT/dist" >&2
  exit 1
fi
latest="$(ls -1t "${pkgs[@]}" | head -1)"
echo "Installing $latest"
# Prefer askpass when set (e.g. SUDO_ASKPASS=/usr/bin/ksshaskpass) so non-TTY
# agent installs get one GUI prompt instead of hanging on a hidden password read.
sudo_cmd=(sudo)
if [[ -n "${SUDO_ASKPASS:-}" ]]; then
  sudo_cmd=(sudo -A)
fi
"${sudo_cmd[@]}" pacman -U --noconfirm "$latest"
echo "KRunner should find “Cursor Spend Tray” (also try “cursor” or “spend”)."
