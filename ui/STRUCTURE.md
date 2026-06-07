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
│   │   │   ├── #admin-tools-group        (Admin Tools tab)
│   │   │   └── .main-tab          (Pages / Agent Manager / Automations / Web / …)
│   │   └── #main-tabs-chev-right
│   └── #status-right              (right side)
│       └── #chat-toggle-btn
│
├── #stage                         ← split container (whole app layout)
│   ├── #main-panel                ← left pane (tab content)
│   │   ├── #tab-autoagent         (Pages — current default)
│   │   ├── #tab-agents            (Agent Manager)
│   │   ├── #tab-automations       (Automations Dashboard)
│   │   ├── #tab-sessions          (Sessions Table)
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