# Chat widget (`ui/chat-widget/`)

A **floating mini chat** any page can spawn to run a *task-scoped* agent session
without disturbing the main chat side-panel. Consumers:
- the git-control star (⭐) in the file editor sidebar (`ui/shared/js/files-git.js`),
  which asks the agent to commit & push in a widget instead of hijacking the main panel;
- the **ability-table search/chat pill** (`buildAbilitySearchPill` in
  `ui/shared/js/dom-utils.js`), which on send opens a widget talking to webAgent
  the manager via the exported helper **`spawnWebagentAbilityChat({ text, attachmentIds })`**.

## Files

| File | Purpose |
|------|---------|
| `js/chat-widget.js` | The factory — `createChatWidget(options)` returns a widget instance. All DOM is built in JS; state is per-instance, so several widgets can run at once. |
| `chat-widget.css` | Styling for the fixed bottom-right layer, the card, mini bubbles, tool-call group, and the minimized "Done" chip. Design-system variables only (dark + light). Linked from root `index.html`. |

## How it works

- The widget generates a **fresh UUID session id** (server creates the session
  implicitly on the first `POST /api/v1/chat/send`) and claims that session's
  live events via `registerSessionSubscriber(sessionId, handler)` in
  `ui/shared/js/agentWs.js` — the one global WebSocket then routes the
  session's events to the widget instead of dropping them as "foreign".
  **One handler per session** (last registration wins); each widget owns its
  own session so this never collides in practice.
- Streams text into mini bubbles (markdown on finalize via the shared
  `_fillAgentBubble` from `ui/chat-side-panel/js/chat-bubble.js`); tool calls
  render as a collapsible group using the shared `buildToolRow` from
  `ui/shared/js/chat-activity.js`.
- **Reliable updates — WS is smooth, the DB is durable.** Exactly like the main
  panel (`ui/chat-side-panel/js/chat-reconcile.js`), the WebSocket only delivers
  when the browser socket and the running agent share one server process. When
  they don't (dev port-stacking, prod multi-worker) or the socket goes briefly
  silent, each widget polls `GET /api/v1/db/session-tail` — **gated on WS
  silence** (`WS_SILENCE_MS`), so zero overhead while the socket is live — and
  renders the same bubbles from the DB tail. This is what makes the view update
  reliably right after a send and after Stop/Continue, and lets it finish a turn
  even if the live `response` event is lost. Both paths converge on the same
  per-key bubbles (idempotent SET), so there are no duplicates.
- The input row opts into the shared `.chat-pill` classes (CHAT-PILL-SYNC in
  `ui/shared/css/app1.css`) — never restyled here. **Continue** and **Stop**
  buttons sit above it: Stop (`POST /api/v1/chat/interrupt`) shows while running;
  Continue (sends `"continue"`) shows once a turn has finished — mirroring the
  main panel's continue/stop pairing.
- **Draggable + resizable.** Drag the header to move the card; drag any
  edge/corner to resize it (min 260×200). The first such gesture switches the
  card to floating (fixed) positioning, detaching it from the docked stack.
- On the final response: green **Done** state → collapses to a compact chip
  after ~1.5s (cancelled if the pointer is over the card; click the chip to
  re-expand). **Close (✕) ≠ Stop**: close only detaches the widget (the run
  continues server-side; the session stays in the main panel's session list).
- The docked layer shifts left when the main chat panel is open so they never
  overlap.
- **Survives page navigation (by design + watchdog).** The floating layer is
  attached to `<body>`, OUTSIDE every page (pages are just shown/hidden under
  `#stage`), so switching tabs can never remove or hide a running widget. A
  small `MutationObserver` watchdog hardens this further: if anything rebuilds
  `<body>` and removes the layer while a widget is still inside, the layer is
  re-attached as the top-most element. The guard stays dormant during normal
  use and never fights a real close — `close()`/`_maybeRemoveLayer` empty the
  layer first, so an empty removal is left alone.
- No persistence across page reloads (deliberate) — a running task continues
  server-side and is reachable from the session dropdown. Widgets capture the
  user id at creation; close them on account switch.

## Factory options

| Option | Meaning |
|--------|---------|
| `title` | Header label (replaced by the auto-generated session title when it arrives). |
| `iconName` | Lucide icon name for the header. |
| `agentId` / `ensureAgent` | A pre-resolved agent id, **or** an async function returning one (do ability setup in there). |
| `initialMessage` | Sent automatically once the agent is resolved. |
| `initialAttachmentIds` | `attachment_ids` to ride the **first** message only (the file-only first message — no text — is still a valid send). |
| `executionMode` | `'ask'` (default) / `'plan'` / `'auto'` — passed per send (legacy `'read'`/`'write'` still accepted by the backend). |
| `onDone(finalText)` | Fires on each final response (e.g. refresh a panel). |
| `onClose()` | Fires when the user closes the widget. |

Instance methods: `open()`, `close()`, `send(text)`, `interrupt()`,
`minimize()`, `restore()`; getters `sessionId`, `status`, `el`.

## Usage sketch (logic)

Build your task message → `createChatWidget({ title, ensureAgent, initialMessage, onDone })`
→ `.open()`. That's it — no session juggling, no touching `app.currentSessionId`.
