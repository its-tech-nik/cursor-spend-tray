#!/usr/bin/env bash
# Restart the tray app when Python sources under src/ change.
# Uses SIGTERM (via watchfiles) so Qt aboutToQuit can stop the browser and release the lock.
set -euo pipefail
cd "$(dirname "$0")/.."

exec uv run watchfiles \
  --filter python \
  --target-type command \
  --sigint-timeout 15 \
  cursor-spend-tray \
  src
