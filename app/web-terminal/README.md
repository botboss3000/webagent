# web-terminal

A **self-contained, terminal-styled chat page** for webAgent. It is served as a
static bundle at **`/web-terminal/`** and embedded in the main UI as the
**Terminal** header tab (an `<iframe>` inside `#tab-terminal`). It can also be
opened on its own — when there is no backend it runs a polished **demo**.

It is a single-page **React 18 + Babel-standalone** app (JSX compiled in the
browser, no build step). Visually it is a TUI: an animated ASCII logo, a chat
transcript with expandable **tool cards**, 23 color themes, a games arcade,
"crew" companions, a design-assistant pill, and a notes/todo sidebar.

## How it connects to webAgent

`agent-bridge.js` decides at load time whether a real backend is reachable:

- **Embedded (real agent).** Same origin as the API → it reuses the main app's
  auth token and **selected agent** from shared `localStorage`
  (`auth_token`, `auth_user_id` / `webagent_active_user_id`, `selectedAgentId`),
  bootstraps an anonymous session if needed (`POST /api/v1/auth/anonymous`), and
  streams replies over **`POST /api/v1/chat/stream`** (SSE). The header status
  reads **`agent`**.
- **Standalone (demo).** No backend (e.g. `file://`, or the probe to
  `/api/v1/auth/access-mode` fails) → `available()` stays false and the app uses
  the canned **`REPLIES`** in `tooldata.js`. The header status reads **`demo`**.
  The same fallback fires if a live turn errors before any output (so the page
  never looks broken). The Terminal keeps its **own** chat session
  (`webagent.terminal.session`), separate from the main chat panel.

> Real chat needs a **selected agent**. The bridge forwards `selectedAgentId`;
> if the user has not picked/created an agent yet the backend returns
> *"No agent assigned"* and the page falls back to the demo.

## Files

| File | Role |
|------|------|
| `index.html` | Entry point. Loads React/ReactDOM/Babel from CDN, then the data/engine scripts, then the JSX in dependency order. |
| `styles.css` | All styling. Re-skins entirely from CSS variables set by the active theme. |
| `themes.js` | `window.THEMES` + `THEME_ORDER` — 23 flat token sets (16 dark, 7 light). |
| `ascii.js` | The ASCII **framebuffer engine** (`makeBuf`/`clearBuf`/`bufStr`, `STAGE_W`/`STAGE_H`) used by the logo. `buf` exposes `set` / `plotz` (z-tested) / `line` / `text` / `center`. |
| `anims.js` | `window.ANIMS` — 20 logo animations `{ id, name, desc, fn(buf, t, state) }`. Brightness is conveyed by glyph choice (the stage is one CSS color). |
| `tooldata.js` | `TOOL_META` (per-tool glyph/color/verb), `DEMO` (seed transcript), `REPLIES` (offline replies). Tool `data` shapes must match the renderers in `components.jsx → ToolBody`. |
| `agent-bridge.js` | `window.AgentBridge` — real-backend transport + probe + graceful demo fallback (see above). |
| `components.jsx` | `AsciiStage`, `Btn`, `ToolCard`, `ToolBody` (per-tool + a generic `__raw` renderer for live tool events), `Msg`, `ThemeMenu`. |
| `app.jsx` | The `App` shell: header, transcript, composer, drawers, keyboard shortcuts, and `send()` (routes to `sendLive` or `sendDemo`). |
| `designer.jsx` | Floating design-assistant chat that can re-skin the UI / swap the logo / open a game. |
| `crew.jsx` | The strolling/dancing ASCII companions (and the crew picker). |
| `sidebar.jsx` | Notes & todo scratchpad (localStorage). |
| `games-shell.jsx` | Games manifest + shared hooks (`useTick`, `useKeydown`), char-board renderer, menu + modal host. Loads **before** the game files. |
| `games-arcade.jsx` / `games-grid.jsx` | The 12 games (Snake, Pong, Breakout, Flap, Maze, Invaders / 2048, Tetris, Minesweeper, Tic-Tac-Toe, Simon, Lights Out). |

Script **load order matters** and is fixed in `index.html`: plain data/engine
scripts first (set `window.*`), then the JSX (`games-shell` before the game
files; `app.jsx` last).

## How it's served & embedded (in the parent repo)

- **Mount** — `app/main.py`: `StaticFiles(directory=app/web-terminal, html=True)`
  at `/web-terminal`, so `/web-terminal/` serves `index.html`.
- **Public** — `app/auth/middleware.py` `PUBLIC_PREFIXES` includes `/web-terminal`
  (no login required to load the page; chat APIs still authenticate).
- **No-store** — `NoCacheMiddleware` covers `/web-terminal` so assets never go stale.
- **Service worker** — `sw.js` explicitly skips `/web-terminal` (otherwise `.js`
  files would be `stale-while-revalidate` cached and a deploy would be one
  version behind).
- **Tab wiring** — header button + `#tab-terminal` iframe in `index.html`;
  `terminal` branch in `ui/js/tabs.js`; `.terminal-frame` in `ui/css/app2.css`.

## Theming (dark + light)

The Terminal carries its **own** theme system (23 themes, including 7 light:
Paper, Latte, Solarized Light, Rosé Dawn, Mint, Sorbet, Daylight) chosen from the
`◐ theme` menu and persisted locally — it is **independent** of the host app's
`body.light-mode`. The only host-side surface is `.terminal-frame`, which uses
`var(--bg-0)` so the frame behind the iframe is correct in both host modes.

## Extending

- **Add a logo animation** → append `{ id, name, desc, fn(buf,t,state) }` to
  `ANIMS` in `anims.js`. Keep `id` a lowercase single word (the designer matches
  on it). Paint with `buf.set` / `buf.plotz` / `buf.line`; use the `state` arg
  for persistent per-animation data (it resets on switch).
- **Add a theme** → add a token set to `THEMES` and its id to `THEME_ORDER` in
  `themes.js`.
- **Add a tool renderer** → add a branch to `ToolBody` in `components.jsx` and a
  matching `TOOL_META` entry; for live backend tools the generic `__raw`
  renderer already shows args/result.

## Develop standalone

A static dev server config exists in `.claude/launch.json` (`web-terminal`,
`python -m http.server 7654 --directory app/web-terminal`) for previewing the
demo in isolation. Run the full backend (`run.py`, port 8080) to exercise the
real-agent path.

> **Production note:** JSX is compiled in the browser by Babel-standalone. That
> keeps the bundle dependency-free and easy to edit, at the cost of a one-time
> compile on load. If startup latency ever matters, precompile the JSX and drop
> the Babel `<script>`.
