---
name: dev-launch
description: >-
  Start cursor-spend-tray auto-reload (./scripts/dev.sh / watchfiles) in a new
  Herdr tab in the current workspace. Use when the user asks to run the dev
  server, watch/reload the tray app, open a Herdr tab for development, or
  invoke the dev-launch skill. Requires HERDR_ENV=1.
---

# Dev Launch

Start the source-dev auto-reloader for this repo in a **new Herdr tab**.

## Preconditions

1. Confirm this agent is inside Herdr:

```bash
test "${HERDR_ENV:-}" = 1
```

If that fails, say you are not running inside Herdr and stop. Do not invent a non-Herdr fallback unless the user asks.

2. Resolve the repo root: the directory that contains `scripts/dev.sh` (this project). Prefer the Cursor workspace / git root.

3. Ensure deps are ready once if needed: `uv sync` from the repo root (dev group includes `watchfiles`).

## Launch steps

1. Create a new tab in the **current** workspace (never create a new workspace for this skill):

```bash
herdr tab create \
  --workspace "$HERDR_WORKSPACE_ID" \
  --cwd "<repo-root>" \
  --label "dev" \
  --no-focus
```

Parse JSON: use `.result.root_pane.pane_id` and `.result.tab.tab_id`.

2. Run the watcher in that tab’s root pane:

```bash
herdr pane run <root-pane-id> "./scripts/dev.sh"
```

3. Confirm startup with a short pane read (e.g. `herdr pane read <root-pane-id> --source recent-unwrapped --lines 40`). Report the new `tab_id` and `pane_id` to the user.

## Defaults

- **New tab**, not a split of the calling pane.
- **`--no-focus`** so the caller keeps UI focus. Use `--focus` on `tab create` only if the user asks to switch to the new tab.
- Command is always `./scripts/dev.sh` (watchfiles → restart `cursor-spend-tray` on `src/` `.py` changes). Do not substitute `uv run cursor-spend-tray` unless the user asks for a one-shot non-watching run.
- Do not close tabs/panes you did not create in this invocation.
- This is source-dev only; do not use the packaged `/usr/bin/cursor-spend-tray` path.
