---
name: map-user-features
description: >-
  Map Cursor Spend Tray user-facing capabilities into a gated parent/child
  JSON tree with code references, and keep the standalone HTML viewer in sync.
  Invoke only via /map-user-features when the user asks to map, update, or
  audit product capabilities from the user perspective.
disable-model-invocation: true
---

# Map user features

Maintain the **user-perspective capability map** for this product so developers
and vibe-coders can see what a person can do, what gates what, and where it
lives in code.

## Artifacts (this skill folder)

| Path | Role |
|------|------|
| [assets/capability-map.json](assets/capability-map.json) | Source of truth for the feature tree |
| [assets/index.html](assets/index.html) | Offline left→right tree viewer (loads the JSON beside it) |

Do **not** recreate `docs/features/` or a top-level `feature-viewer/` folder.
Keep data + viewer under this skill’s `assets/`.

Open the viewer:

```bash
xdg-open .agents/skills/map-user-features/assets/index.html
# or: cd .agents/skills/map-user-features/assets && python3 -m http.server
```

## When invoked

1. Read the current [assets/capability-map.json](assets/capability-map.json).
2. Inspect the tray/popup/menu code under `src/cursor_spend_tray/` (especially
   `app.py`, `popup.py`, `config.py`, `autostart.py`, `scheduler.py`,
   `auth_detect.py`, `session_cookie.py`).
3. Add, move, or prune nodes so the tree matches **what the person can do**.
4. Bump `meta.version` when the tree changes meaningfully.
5. Leave `assets/index.html` alone unless the JSON schema changes and the
   viewer must adapt.

## Inclusion rules (strict)

**Include only** capabilities the person uses immediately:

- Clicks, menu items, toggles, visible panels, and **settings they can change**
- Journeys the person starts (e.g. sign in in the dedicated browser)

**Do not include** background/system nodes as their own features:

- Browser lifecycle / auto-launch / spinner while waiting
- Network sign-in or sign-out detection
- Pause-while-signed-out, probe loops, cache writes
- Hover-only tips, status footer text, chart hover cards (unless the user
  asks for micro-UX)

If background behavior exists **only because of a setting**, keep a node for
the **setting** and mention the side effect in that node’s description.
Example: “Choose how often data refreshes” may note that updates run on that
schedule while signed in — do not add a separate “automatic refresh” child.

## Hierarchy rules

- Parent = what must be true / reachable first (entry surface or gate).
- Child = unlocked by or nested under that surface (submenu item, gated UI).
- Prefer gates that match product state: tray visible → popup / context menu /
  sign-in; signed-in → live spending / rich account card; browser reachable →
  refresh actions that are hidden when disconnected.
- Do not invent umbrella parents that are only “implementation plumbing.”

## Naming for developers

Write titles and descriptions in plain language for non-technical readers, but
when naming a **visual component or control**, use the **real product / code
name** so a search finds it:

| Prefer | Avoid |
|--------|--------|
| “Refresh interval” (menu label) | “the timer setting” |
| “View Browser” | “open the scraping window” |
| `SpendPopup`, `UsageRing`, `_CtxAccountCard` in `code[].function` | Vague “UI widget” |
| Exact menu strings: “Launch at login”, “Keep open”, “Between Scrapes” | Paraphrased labels |

`code` entries must point at the implementing symbol:

```json
{
  "file": "src/cursor_spend_tray/app.py",
  "function": "TrayApp._on_view_browser",
  "line": 1242
}
```

- `file`: repo-relative path
- `function`: class/method or free function name as in source
- `line`: current definition line (re-check with search after edits; do not
  leave stale `1` placeholders)

## JSON schema

Top level:

```json
{
  "meta": {
    "title": "…",
    "description": "…",
    "version": 2,
    "product": "cursor-spend-tray"
  },
  "states": [
    { "id": "app_running", "label": "…" }
  ],
  "features": [ /* root nodes */ ]
}
```

Each feature node:

```json
{
  "id": "snake_case_stable_id",
  "title": "Plain title (use real UI names for controls)",
  "description": "What the person does / sees",
  "available_when": "Gate in plain language",
  "code": [
    { "file": "src/…", "function": "Class.method", "line": 123 }
  ],
  "children": []
}
```

- Keep `id` stable across edits when the capability is the same.
- `children` is always an array (empty if leaf).
- Multiple `code` refs are fine when several symbols implement one capability.

## Workflow checklist

Copy and track:

```
Capability map update:
- [ ] Read assets/capability-map.json
- [ ] Diff against tray/popup/menu code
- [ ] Add only user actions + settings
- [ ] Nest by gates / UI hierarchy
- [ ] Use real control and symbol names
- [ ] Refresh file/function/line refs
- [ ] Prune background-only nodes
- [ ] Bump meta.version if structure changed
- [ ] Confirm viewer still loads assets/capability-map.json
```

## Out of scope

- Changing tray app behavior (unless the user also asked for that)
- Auto-running this skill on unrelated coding tasks
- Publishing or packaging the viewer as part of the installed app
