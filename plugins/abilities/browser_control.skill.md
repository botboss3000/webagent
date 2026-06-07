# Driving the browser

This skill is attached to the **Browser Control** ability. It loads on demand —
the agent sees the one-line "when to use" in its `# [SKILLS]` list and pulls this
full body with `load_skill` only when a task actually needs to use a browser.

## When to use

Use this when a task needs a real, stateful web page — logging into a site,
clicking through a flow, filling a form, reading content that only appears after
JavaScript runs, or taking a screenshot of what a page looks like. The page is a
**persistent browser session** (a tab) that keeps its cookies and login between
actions and across server restarts.

For a plain one-shot API call or fetching a URL's raw response, prefer
`http_request` instead — it's faster and doesn't spin up a page.

## The two tools

- **`browser_action`** — drives a single browser tab (navigate, click, type, read,
  screenshot, run JS, close). One `action` per call.
- **`http_request`** — arbitrary outbound HTTP (GET/POST/…). No page, no cookies
  from the browser session — use it for APIs, not for logged-in web flows.

## Browser sessions — the shareable tab

A **browser session** is one persistent tab with its own cookie jar. It is the
unit you act on and the unit the user can watch.

- You normally don't pass anything: omit `browser_session_id` and `browser_action`
  uses (or auto-creates) **your own shared tab** — the one linked to you and marked
  shared. This is the tab the user watches: when they open the **Web** tab it
  **automatically opens this same tab** (it resolves to your shared tab the same way
  you do), so they see exactly what you're driving live — no manual "Share with
  agent" step is needed.
- Because of that, just navigate. When the user says "use the integrated web
  browser to go to X" or "show me X in the browser", the right move is a plain
  `browser_action` `navigate` with no id — that *is* the panel they're looking at.
  Don't fall back to `http_request` or `web_search` to "look the site up" instead;
  drive the actual page so it appears in their Web tab.
- To act on a specific tab, pass its `browser_session_id`. **The sharing gate only
  lets you reach a tab that is linked to you AND marked shared.** A user's private
  tab is invisible to you — a `browser_action` against it is denied. That's by
  design; don't try to work around it.
- Same session id ⇒ same live page. You and the user are looking at the *same*
  page, not two copies.

## The loop

`browser_action` is one action per call. The pattern is:
**navigate → (read / screenshot to see state) → click / type → read again**,
repeating until the task is done, then **close** if the tab was only for this task.

1. **`navigate`** — go to a `url`. Cookies from earlier in the session persist, so
   if you logged in before you may already be authenticated.
2. **`get_text` / `get_html`** — read the page (optionally narrowed by a CSS
   `selector`) before deciding what to click. Don't act blind.
3. **`click`** — click the element matching `selector`.
4. **`type`** — type `text` into the element matching `selector` (e.g. a form
   field, then `click` the submit button).
5. **`screenshot`** — capture what the page looks like (`full_page` defaults true).
   Use it to show the user the result, or to see a layout `get_text` can't convey.
6. **`wait`** — pause up to `timeout_ms` for the page to settle after an action.
7. **`evaluate`** — run a snippet of `js` in the page for anything the other
   actions don't cover; returns the result.
8. **`title` / `url`** — read the current page's title or address (handy to confirm
   a navigation or redirect landed where you expected).
9. **`close`** — end the tab. Its login state is saved first, so reopening the same
   session id later comes back authenticated.

## Rules of thumb

- **Read before you click.** Use `get_text` (or a `screenshot`) to confirm the page
  is in the state you think it is before sending a `click` or `type`.
- **One action per call.** Send `navigate`, see the result, then the next action —
  don't assume a page is ready the instant you navigate; `wait` if it's slow.
- **Selectors are CSS.** `#id`, `.class`, `button[type=submit]`, etc. If a `click`
  or `type` finds nothing, re-read the HTML to get the right selector.
- **Logins stick.** Because the session persists its cookies, log in once and later
  actions (and later sessions with the same id) stay authenticated. You don't need
  to re-login every task.
- **Respect privacy.** You can only touch tabs shared with you. If you need a tab
  and have none, just act without an id — you'll get your own shared tab the user
  can watch. Never expect to see a user's private tab.

## Example: log in and grab a value

1. `navigate` to the login page.
2. `type` the username into its field, `type` the password into its field.
3. `click` the submit button, then `wait`, then `get_text` to confirm you're in.
4. `navigate` to the page you actually need; `get_text` (or `screenshot`) the value.
5. Leave the tab open (login persists) or `close` it when done.
