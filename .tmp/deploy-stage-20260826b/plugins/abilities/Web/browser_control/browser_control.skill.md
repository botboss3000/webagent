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

This is also how you act on the user's behalf with **external providers that
don't have a direct integration** in the app: the user stores their login in the
encrypted vault, you call `vault_login` to sign in headlessly (you never see the
password), then you navigate, scrape, and make changes just like a human would.
See "Acting on unsupported external providers" below for the full recipe.

For a plain one-shot API call or fetching a URL's raw response, prefer
`http_request` instead — it's faster and doesn't spin up a page.

## The tools

- **`browser_action`** — drives a single browser tab (navigate, click, type, read,
  screenshot, run JS, close). One `action` per call.
- **`vault_login`** — logs into a site **using credentials the user saved in the
  encrypted vault** (email + password). The credentials are read server-side and
  typed into the login form — the agent never sees them. Returns `logged_in`,
  `needs_2fa`, or an error. Use this when the user needs you to act on an
  external provider that doesn't have a direct integration — DNS registrars,
  hosting panels, billing dashboards, any site behind a login form. See the
  "Acting on unsupported external providers" section below for the full pattern.
- **`http_request`** — arbitrary outbound HTTP (GET/POST/…). No page, no cookies
  from the browser session — use it for **public** APIs, not for logged-in web flows.
- **`web_session_status` / `web_session_fetch` / `web_session_graphql`** — the
  **cookie-replay** tools: lightweight HTTP/GraphQL calls that act **AS the
  logged-in user** on sites with no public API (e.g. Facebook Messenger). See below.
- **`browser_popup`** — **show the USER a page** in a small floating browser window
  on their screen (`mode="open"`, `url=…`), and close it (`mode="close"`). Use it
  when you want the human to *look at / interact with* a site — "have a look at
  this", "check this booking", "does this look right?" — not when you just need to
  read the page yourself (that's `browser_action`). It renders through the same
  in-app proxy as the Browser page, so framing-blocked sites still show. The user
  can close it themselves; you can also close it. Needs a live chat session on
  screen (skips on background / event-triggered runs). For a real **login** the user
  must complete, prefer `browser_backend(mode="local")` (their real Chrome) — a
  protected login won't complete inside the popup.

## Acting as the user with cookie-replay (`web_session_*`)

When a site has **no public API** but the user is **logged in**, you don't have to
click through the whole page with `browser_action`. The `web_session_*` tools fire a
fast, headless HTTP (or GraphQL) request **carrying the user's cookies**, so the
site treats it as the user.

**Where the cookies come from — you usually do nothing:**

1. **Live in-app browser login (preferred, automatic).** These tools read the
   cookies from the user's **own Browser-page session** — whatever they're logged
   into on the in-app Browser tab — filtered to the request's domain. So the setup
   is simply: the user is logged into the site on the Browser page. If they aren't,
   the natural move is to drive `browser_action` to that site's login first (or ask
   the user to log in on the Browser tab), *then* use the `web_session_*` tools.
2. **Pasted-cookie fallback.** If the user instead pasted cookies into **Browser
   Control → Credentials** (to bring a login from an external browser), those are
   used when no live session cookie matches.

**How to use them:**

- **`web_session_status`** — check first. Tells you whether any logged-in session or
  pasted cookies are available. If it reports `not_configured`, get the user logged
  in (drive the login with `browser_action`, or ask them to).
- **`web_session_fetch`** — one HTTP call to a `url` (GET/POST/…), with the cookies
  attached. Returns status + headers + parsed body. The response's `cookie_source`
  tells you whether it used the `live` session or `pasted` cookies.
- **`web_session_graphql`** — POST a GraphQL-style form (by `doc_id` or `query`),
  optionally scraping CSRF tokens from a page first (`csrf_token_url` +
  `csrf_token_names`).
- A `session_expired` status means the login went stale — drive a fresh login on the
  Browser page (or have the user refresh it), then retry.
- **Site-specific details** (URL templates, GraphQL `doc_id`s, CSRF token names like
  `fb_dtsg`) belong in a **site recipe** — your prompt/skills, not these tools, which
  are deliberately domain-agnostic.

These calls **act as the user and can change remote state**, so `web_session_fetch`
and `web_session_graphql` are marked destructive (confirmed at chat time).

## Cookies persistence — what's kept, and the allowlist you manage

The browser keeps logins between actions and across restarts, but it does **not**
hoard everything. Two things keep the saved cookie jar clean:

- **Ad/tracker requests are blocked at the network layer**, so their cookies are
  never set (pages also load a bit faster).
- **Only first-party + allowlisted cookies are persisted.** "First-party" = a site
  the session actually visited. Cookies from third-party domains (the endless
  ad-tech long tail) are dropped when the jar is saved. Logins for sites you visit,
  and the major identity providers (Microsoft, Google, Apple, Okta, …), are kept
  automatically.

**You manage the allowlist on the user's behalf** with the **`cookie_allowlist`**
tool. When the user says something like *"remember my login for example.com"*,
*"keep me signed into X"*, or *"stop saving cookies for Y"*, translate that into a
call:

- `cookie_allowlist(action="add", domain="example.com")` — keep that site's cookies
  across restarts even if they don't visit it every session.
- `cookie_allowlist(action="remove", domain="example.com")` — stop pinning it.
- `cookie_allowlist(action="list")` — show what's currently pinned.

Pass a bare registrable domain (`example.com`), not a full URL — the tool
normalizes it. You normally DON'T need to add the sites the user is actively using
(those are kept automatically as first-party); use it for logins they want
remembered long-term, or to honor an explicit "remember/forget this site" request.

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

## Driving the real browser on the user's device (`browser_backend`)

A session normally runs in the **in-app headless browser** (what the user sees in
the Web tab). When that isn't enough — a site blocks being shown in the in-app
frame, there's a CAPTCHA or a hardware/2FA login, or the user just wants to do
part of it themselves — you can move the **same session** to the **real browser on
the user's device** with `browser_backend`:

- **`browser_backend(mode="local")`** — open/attach the real browser on the
  device and point this session at it. It opens where the session was; the Web tab
  switches to a **live pixel mirror** of that real window, so the user keeps
  watching (and can take over) inside the app. By default it uses the user's
  everyday Chrome profile (their real logins). This **acts in the user's own
  browser, so it is confirmed in chat** before it happens.
- **`browser_backend(mode="headless")`** — switch back to the in-app browser. The
  login is carried forward, so whatever got signed in on the device persists. The
  user can then close the device window and keep working in the app.
- **`browser_backend()`** with no mode — read-only status: which backend the
  session is on, and whether the device even has a usable browser
  (`chrome_found`) / one already running (`chrome_running`).

Everything else is unchanged across the switch: same `browser_action` calls, same
session id, same shared tab the user watches. You don't need to do anything
special — `navigate`/`click`/`type` keep working whichever backend is live.

**When to reach for it:** prefer the in-app browser. Switch to the device only
when a page won't load/show in-app, a login needs the user's real device/2FA, or
the user explicitly asks to use their own browser. If a switch fails, the result
message says why (e.g. their everyday Chrome is already open and must be closed
first, or the device has no Chrome) — relay that to the user.

> **Same-machine only for now.** This drives a browser on the *same machine as the
> server*. When the app is opened from a different device, that device's browser
> isn't reachable yet (a later phase adds a small local companion for that).

## The loop

`browser_action` is one action per call. The pattern is:
**navigate → (read / screenshot to see state) → click / type → read again**,
repeating until the task is done, then **close**.

Treat the live headless browser like a leased resource, not the durable session
record. **Default to `browser_action(action="close")` as the final browser step,
including after an error or abandoned path.** Closing first saves cookies and
local storage, so it does not forget the login or delete the browser-session tab;
the same session can reopen later with its saved state and last URL.

Put cleanup in the same execution path as the browser work: once no further
browser action is required, close before writing the final response. If an
intermediate browser call fails, make one best-effort close call before changing
approach or ending the turn. Do not rely on the idle timeout as normal cleanup;
it is a safety net for crashed, interrupted, or non-compliant runs.

Leave the live browser open only when there is a concrete handoff in progress:
the user is actively watching/interacting in the Browser view, must complete 2FA,
explicitly asked you to leave the page open, or the very next turn is expected to
continue the same browser workflow. Say that you are leaving it open. Do not keep
a headless browser alive merely because its login may be useful later.

1. **`navigate`** — go to a `url`. Cookies from earlier in the session persist, so
   if you logged in before you may already be authenticated.
2. **`get_text` / `get_html`** — read the page before deciding what to click.
   Don't act blind. **`get_text` REQUIRES a `selector`** — to read the whole page
   pass `selector="body"`, or target an element like `selector="h1"`. (`get_html`
   defaults to `body` if you omit the selector, but `get_text` does not.) There is
   **no `read` action** — use `get_text`/`get_html`.
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

## Acting on unsupported external providers (`vault_login`)

Some providers don't have a direct integration in the app (no API connector, no
dedicated router), but the user still needs the agent to log into their account
and do something — check DNS records, configure a setting, pull a report, make a
change. This is where the vault + browser combo fills the gap.

**The user stores their login in the encrypted vault** (email + password, saved
via a credentials form on the page or through the Browser Control panel), and the
agent logs in headlessly with `vault_login` — the credentials are read server-side
and typed into the login form without the agent *ever* seeing them.

### The pattern

1. **Confirm the credential exists.** Call `check_credential(ability="browser_control")`.
   If `configured` is `false`, tell the user they need to save their login to the
   vault first — point them to the credentials form. Stop there.
2. **Log in headlessly.** Call `vault_login` with the `login_url` and the right CSS
   selectors for the site's email/username field, password field, and submit button.
   If the site uses a two-step login (username first, then password on a separate
   page), `vault_login` may not handle it — fall back to driving the browser manually
   with `browser_action` `type` calls (the vaulted values are still injected for you).
3. **Handle 2FA.** If the result is `needs_2fa`, tell the user to finish signing in
   on the Browser tab — the verification page is already open there. Once they confirm
   they're in, continue.
4. **Navigate to the target page.** The session's cookies persist, so you stay
   authenticated.
5. **Scrape or act.** Use `get_text` / `get_html` / `evaluate` to extract data, or
   `click` / `type` to make changes. One action per call, reading before each click.
6. **Report back.** Summarise what you found or changed in 1–2 sentences unless the
   user asks for detail.

### What the user sees (and doesn't see)

- **The agent never sees the credentials.** `vault_login` reads the email + password
  server-side from the encrypted vault and types them into the page. The agent only
  receives the outcome: `logged_in`, `needs_2fa`, or an error.
- **The user can watch the whole process** on the Web tab — the same shared browser
  session the agent drives is mirrored there live.
- **Cookies persist** across tasks, so you only need to log in once per session.

### Example: Namecheap DNS management

This is the exact pattern the Namecheap DNS genui uses:

1. `check_credential(ability="browser_control")` → confirmed.
2. `vault_login(login_url="https://www.namecheap.com/myaccount/login/", …)` → logged in.
3. Navigate to `https://ap.www.namecheap.com/Domains/DomainControlPanel/<domain>/advancedns`.
4. Scrape the DNS records table with `evaluate`, then publish them to a genui.
5. When the user asks to change a record, click the edit controls, type the new value,
   save, re-scrape, and refresh.

The whole flow runs headlessly in the in-app browser — no API key, no special setup,
just the user's Namecheap login saved to the vault.

## Rules of thumb

- **Read before you click.** Use `get_text` (with a `selector` — `body` for the
  whole page) or a `screenshot` to confirm the page is in the state you think it is
  before sending a `click` or `type`.
- **One action per call.** Send `navigate`, see the result, then the next action —
  don't assume a page is ready the instant you navigate; `wait` if it's slow.
- **Selectors are CSS.** `#id`, `.class`, `button[type=submit]`, etc. If a `click`
  or `type` finds nothing, re-read the HTML to get the right selector.
- **Logins stick.** Because the session persists its cookies, log in once and later
  actions (and later sessions with the same id) stay authenticated. You don't need
  to re-login every task.
- **Release the live browser.** Close at the end of the workflow and on failure;
  saved state survives. Keep it open only for an explicit user handoff or imminent
  continuation.
- **Respect privacy.** You can only touch tabs shared with you. If you need a tab
  and have none, just act without an id — you'll get your own shared tab the user
  can watch. Never expect to see a user's private tab.

## Example: log in and grab a value (with vault)

1. Call `check_credential(ability="browser_control")` to confirm the login is saved.
2. Call `vault_login(login_url="…", email_selector="…", password_selector="…", submit_selector="…")`.
   If `needs_2fa`, tell the user to finish on the Browser tab.
3. `navigate` to the page you need; `get_text` (or `screenshot` / `evaluate`) the value.
4. `close` it when done. The login persists and the same session can reopen later.

When `vault_login` cannot handle the site's form (e.g. a two-step login), fall
back to the manual approach: `navigate`, `type` the username + password, `click`
submit. The vaulted values are still injected for the `type` calls — you never
see them.
