# WebAgent Browser Connector (extension)

Client half of the **connector** browser backend. Install this in your own Chrome
and your WebAgent server can drive *this* browser — navigate, click, type, read,
screenshot — in your real, logged-in session, even when the server is on another
machine across the internet. You watch every action happen and can pause anytime.

The server half lives in the main repo:
- `app/api/connector_ws.py` — the authenticated WebSocket the extension connects to.
- `app/tools/browser_connector.py` — the per-user registry + command executor.
- `app/tools/browser.py` — the `"connector"` backend mode (third backend, beside
  the in-app headless browser and the same-machine on-device browser).

## How it fits

```
This browser (extension) ──WebSocket──► WebAgent server
  • holds one authed socket               • agent + browser_action (unchanged)
  • runs commands on real tabs            • routes to you when a browser session's
  • returns text / screenshots              backend is "connector"
```

A command only ever reaches the **authenticated owner's own** connection, so no
one else can drive your browser. The agent issues the same `browser_action` it
always does; it never knows which backend ran it.

## Install (Chrome, unpacked — for dogfooding)

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and choose this `clients/browser-extension/` folder.
4. In the WebAgent app, open the **Browser** page and click the **plug** icon
   (“Pair extension”). It shows a **Server URL** and a long-lived **Token** — copy
   both. (This is how you get a token without touching any developer tools; the
   token is scoped to your account and lasts ~1 year.)
5. Click the extension's toolbar icon → **Settings…**, paste the **Server URL**
   and **Token** from step 4, and Save.
   - You only need a token when the server is **not** in open (single-user) access
     mode. In open mode you can leave the token blank.
   - `http(s)://…` is auto-converted to `ws(s)://…`.
6. The toolbar badge shows **ON** (green) when connected.

> If the badge stays **OFF** and the page console shows the connector socket
> returning **403**, the server isn't in open mode and the token is missing or
> wrong — redo the “Pair extension” copy in step 4.

## Use it

1. In the WebAgent app, open the **Browser** page, pick (or create) a browser
   session shared with an agent, and switch its backend to **“My browser
   (extension)”**.
2. Ask the agent to browse. It opens/drives a visible tab here; the badge shows a
   number while a command runs.
3. **Pause / Resume** and **Connect / Disconnect** live in the toolbar popup.

## Commands supported (v1)

`navigate`, `click`, `type`, `get_text`, `get_html`, `screenshot`, `wait`,
`title`, `url`, `close`, and `evaluate` (custom JavaScript — **off by default
behind a per-action Allow prompt**).

## Security & privacy

- **You see everything** — commands run in visible tabs, and you can pause at any
  time.
- **`evaluate` (custom JS)** pops an Allow/Deny prompt for each call; closing the
  prompt or not deciding within 60s = deny.
- **Screenshots** are visible-tab only and can be turned off in Settings.
- Uses your **real browser profile** — your existing logins — so the server never
  receives or stores your cookies.

## Known limits (v1 / thin slice)

- **MV3 service worker** is killed when idle; the extension keeps a heartbeat and
  auto-reconnects, but brief drops are normal. A command issued while disconnected
  returns a clear "extension not connected" error.
- **Content-restricted pages**: strict-CSP sites can block `evaluate`; cross-origin
  iframes and some shadow-DOM/genui apps may not be fully driveable from a content
  script.
- **Screenshots** capture only the visible portion of the active tab (no full-page
  / background-tab capture yet).
- **Chrome only** for now; a Firefox port is planned.
