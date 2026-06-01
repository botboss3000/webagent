# UI Structure & Naming

Reference for the top-level layout containers in `index.html`. Keep this file
in sync when adding or renaming structural regions.

## Layout tree

```
body
├── .stargaze-canvas               (animated background)
├── .stargaze-overlay              (gradient overlay)
├── #cursor-glow                   (full-viewport cursor-follow glow, behind #stage)
│
├── #main-header                   ← top bar
│   ├── #main-tabs-wrap            (chevron-scroll carousel)
│   │   ├── #main-tabs-chev-left
│   │   ├── #main-tabs             (role="tablist"; overflow-x:auto → clips children)
│   │   │   ├── #user-dropdown     (account avatar + menu; first item, left of tabs)
│   │   │   │   ├── #top-user-id          (trigger)
│   │   │   │   └── #user-dropdown-menu   (position:fixed — see note below)
│   │   │   ├── #admin-tools-group        (#header-refresh-btn + Admin Tools tab)
│   │   │   └── .main-tab          (Pages / Agent Manager / Web / …)
│   │   └── #main-tabs-chev-right
│   └── #status-right              (right side)
│       └── #chat-toggle-btn
│
├── #stage                         ← split container (whole app layout)
│   ├── #main-panel                ← left pane (tab content)
│   │   ├── #tab-autoagent         (Pages — current default)
│   │   ├── #tab-agents            (Agent Manager)
│   │   ├── #tab-web               (Web — in-app AI-augmented browser; partial: ui/web.html)
│   │   ├── #tab-account           (Account, opened from user menu)
│   │   └── #tab-admin-tools       (Admin Tools — admins only)
│   │
│   ├── #chat-resize-handle        (drag bar — desktop only)
│   │
│   └── #chat-panel                ← right pane
│       ├── #chat-header           (agent + session pickers)
│       ├── #chat-messages
│       │   └── #chat-messages-inner
│       └── #chat-input-area
│           └── #chat-input-row    (the chat pill)
│               └── #chat-activity (floating activity indicator, above pill)
│                   ├── #chat-activity-panel (expandable tool-call list)
│                   └── #chat-activity-bar   (clickable ticking note chip)
│
└── modals (siblings of #stage, position: fixed)
    ├── #chat-expand-modal         (full-screen compose)
    ├── #cell-modal                (DB cell viewer)
    └── #feedback-modal            (send feedback)
```

## User dropdown lives inside the tab carousel

`#user-dropdown` (the top-left account avatar) is the **first child of `#main-tabs`**, not of `#status-right`. `#main-tabs` is the horizontally-scrolling tab carousel and has `overflow-x: auto`, which makes the browser compute `overflow-y` to `auto` as well — so any `position: absolute` child popover gets **clipped** to the ~30px-tall tab strip and is invisible.

For that reason `#user-dropdown-menu` is `position: fixed` (set in `app1.css` via the `#user-dropdown-menu.user-dropdown-menu` rule) and is anchored under the trigger by `initSessions()` in `ui/js/sessions.js` (`positionUserMenu()`) every time it opens. If you ever revert it to `absolute`, or move the menu without keeping it out of the carousel's clip region, the panel stops appearing on click even though the toggle handler still fires.

## Names at a glance

| Region | ID | Notes |
|---|---|---|
| Whole split layout | `#stage` | Holds main panel + chat panel. |
| Top bar | `#main-header` | Tabs on the left, account/chat-toggle on the right. |
| Left pane | `#main-panel` | Where tab content (Pages, Agents, Admin Tools, Account) renders. |
| Right pane | `#chat-panel` | The chat column. Has its own `#chat-header` inside it. |

Don't reuse "main" or "header" for nested children — `#chat-header` is the
chat panel's own header and is intentionally distinct from `#main-header`.

## Legacy names (do not reintroduce)

The following IDs were renamed; the old names should not come back:

| Old | New |
|---|---|
| `#app-container` | `#stage` |
| `#terminal-side` | `#main-panel` |
| `#chat-side` | `#chat-panel` |
| `#status-bar` | `#main-header` |

"Terminal" in the UI no longer refers to the left pane. It now refers only
to the real PTY shell inside Admin Tools (`ui/js/terminal.js`,
`app/api/terminal.py`).

## LocalStorage keys

Container-related keys stored on the client:

| Key | Purpose |
|---|---|
| `anonUserId` | Anonymous user UUID. Used when no `auth_token` is present so chat history and sessions stick across reloads. Migrated from legacy `terminalUserId` on first load. |
| `terminalSessionId` | Current session UUID. Legacy name — not yet renamed. |
| `chatPanelWidth` | Drag-resize width of `#chat-panel` (desktop). |
| `webagent_theme` | `'light'`, `'dark'`, or `'system'`. |
| `lastActiveTab` | Which `#main-tabs` tab to restore on load. |
| `webagent.chatVisible.<key>` | Per-layout chat panel show/hide. |
| `webagent.chatDraft.v1` | Unsent text typed into the chat pill (`#chat-input`). Saved on every keystroke, restored on load so a refresh keeps the draft, cleared on send. See `ui/js/chat.js`. |
| `diag_server_base` | Standalone Diagnostics page (`ui/diagnostics.html`) only — absolute server URL used when the page is opened from disk (`file://`). Ignored when the page is served same-origin. |
| `diag_token` | Standalone Diagnostics page only — optional admin token override (paste-in). When unset the page falls back to the shared `auth_token`. |
