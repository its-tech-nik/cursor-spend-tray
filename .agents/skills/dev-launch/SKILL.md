---
name: dev-launch
description: >-
  Start or reuse cursor-spend-tray auto-reload (./scripts/dev.sh / watchfiles)
  in a single Herdr tab labeled "dev-launch" in the current workspace. Use when
  the user asks to run the dev server, watch/reload the tray app, open a Herdr
  tab for development, or invoke the dev-launch skill. Requires HERDR_ENV=1.
---

# Dev Launch

Start the source-dev auto-reloader for this repo in **one** Herdr tab labeled
`dev-launch`. Reuse that tab when it already exists; create it only if missing.

## Preconditions

1. Confirm this agent is inside Herdr:

```bash
test "${HERDR_ENV:-}" = 1
```

If that fails, say you are not running inside Herdr and stop. Do not invent a non-Herdr fallback unless the user asks.

2. Resolve the repo root: the directory that contains `scripts/dev.sh` (this project). Prefer the Cursor workspace / git root.

3. Ensure deps are ready once if needed: `uv sync` from the repo root (dev group includes `watchfiles`).

## Launch steps (exactly one tab)

**Hard rule:** create at most **one** tab in this invocation. Prefer reuse. Never create a second tab “just in case,” and never guess pane/tab IDs from earlier turns.

### 1. Find an existing tab

```bash
herdr tab list --workspace "$HERDR_WORKSPACE_ID"
```

Pick the first tab whose `.label` is exactly `dev-launch`.

Legacy cleanup (older skill used `dev`):

- If no `dev-launch` exists but one or more tabs are labeled `dev`, rename the best one with `herdr tab rename <tab_id> dev-launch` (prefer a pane already running `watchfiles` / `./scripts/dev.sh`).
- Close any **extra** leftover `dev` tabs that are idle shells (zsh/bash only, no watcher) with `herdr tab close <tab_id>`. Do not close the caller’s tab or unrelated tabs.

### 2. Create only if missing

If neither `dev-launch` nor a reusable legacy `dev` tab exists:

```bash
herdr tab create \
  --workspace "$HERDR_WORKSPACE_ID" \
  --cwd "<repo-root>" \
  --label "dev-launch" \
  --no-focus
```

Parse JSON **from this response only**: `.result.root_pane.pane_id` and `.result.tab.tab_id`. Do not run `tab create` again in the same invocation.

### 3. Resolve the pane for a reused tab

When reusing, do **not** create. Discover the pane:

```bash
herdr pane list --workspace "$HERDR_WORKSPACE_ID"
```

Use the pane whose `tab_id` matches the chosen tab (single-pane tab → that pane). Confirm with `herdr pane get <pane_id>` / `herdr pane process-info --pane <pane_id>` if needed.

### 4. Start the watcher only when needed

```bash
herdr pane process-info --pane <pane-id>
```

- If a foreground process is already `watchfiles` / `./scripts/dev.sh` / `uv run watchfiles … cursor-spend-tray`, **do not** send another `pane run`. Report the existing `tab_id` / `pane_id`.
- Otherwise run:

```bash
herdr pane run <pane-id> "./scripts/dev.sh"
```

If a different foreground command is stuck and the user asked to relaunch, send `herdr pane send-keys <pane-id> ctrl+c`, wait briefly, then `pane run` once.

### 5. Confirm

```bash
herdr pane read <pane-id> --source recent-unwrapped --lines 40
```

Report `tab_id`, `pane_id`, and whether the tab was **reused** or **created**.

## Defaults

- **One** tab labeled `dev-launch` — reuse if present; create only when absent.
- **`--no-focus`** on create so the caller keeps UI focus. Use `--focus` only if the user asks to switch to the tab.
- Command is always `./scripts/dev.sh` (watchfiles → restart `cursor-spend-tray` on `src/` `.py` changes). Do not substitute `uv run cursor-spend-tray` unless the user asks for a one-shot non-watching run.
- Do not close tabs/panes you did not create or identify as leftover duplicate `dev` / empty `dev-launch` shells from this skill.
- This is source-dev only; do not use the packaged `/usr/bin/cursor-spend-tray` path.
