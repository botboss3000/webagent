# WebAgent feedback relay

A small Cloudflare Worker that sits between the WebAgent app and GitHub
Issues. The app (every clone in the wild, including yours) POSTs feedback
here; this Worker applies rate limits + a blocklist, drops honeypot-tripped
spam, and creates an issue in the configured private feedback repo.

The GitHub token lives only on the Worker. Clones never see it.

**Deployed instance:** `https://webagent-feedback-relay.botboss.workers.dev`
(the free `*.workers.dev` URL — no custom domain). This is the default every
clone talks to; it's the `_DEFAULT_RELAY_URL` in `app/api/feedback.py`.

**Captcha is OFF by default.** A Turnstile widget is locked to a fixed list of
hostnames (free tier) or needs Enterprise "Any Hostname" to work on arbitrary
domains — neither fits an open repo that anyone clones onto their own host. So
this relay ships captcha-less and relies on rate limits + blocklist + a
host-independent honeypot. Turnstile support is left in the code as an optional
opt-in for an operator running on their own known domain (see below).

## Architecture

```
[WebAgent app, anywhere]
   │ POST /api/v1/feedback        (browser → FastAPI)
   ▼
[FastAPI app/api/feedback.py]
   │ POST https://webagent-feedback-relay.botboss.workers.dev/submit
   ▼
[this Worker]
   │ POST https://api.github.com/repos/.../issues   (with PAT)
   ▼
[GitHub: botboss3000/webagent-feedback]
```

## One-time setup

### 1. Create the feedback repo on GitHub
Private. Issues only. Name it something like `webagent-feedback`. Add a few
labels: `bug`, `enhancement`, `feedback`.

### 2. Generate a fine-grained PAT
On github.com, create a fine-grained personal access token scoped to **just
that repo**, with permission **Issues: Read and write**. Nothing else.

### 3. Install wrangler and create KV namespaces
```bash
npm install
npx wrangler login
npx wrangler kv namespace create RATE_LIMIT_KV
npx wrangler kv namespace create BLOCKLIST_KV
```
Paste the two returned IDs into `wrangler.toml` (replacing the
`REPLACE_WITH_*` placeholders).

### 4. Set the GitHub token secret
```bash
npx wrangler secret put GITHUB_TOKEN
```
This is the only required secret. It's stored encrypted on Cloudflare and
never written to any file in the repo.

### 5. Deploy
```bash
npx wrangler deploy
```
Wrangler prints the worker's `*.workers.dev` URL. Put that URL (with `/submit`)
into `_DEFAULT_RELAY_URL` in `app/api/feedback.py` so every clone uses it.

### (Optional) Custom domain
Not used for this deployment — we run on the free `*.workers.dev` URL since the
relay is a backend endpoint users never see. If you'd rather use a custom
domain: Worker → Triggers → Custom Domains → add your subdomain (Cloudflare
handles DNS automatically when the apex domain is in the same account), then
point `_DEFAULT_RELAY_URL` at it.

### (Optional) Turnstile captcha
Off by default (see top of this README for why it doesn't fit open clones). If
you run on a **known, fixed domain** and want captcha:
1. dash.cloudflare.com → Turnstile → Add widget → **Managed** mode → add your
   hostname(s). You get a **site key** (public) and a **secret key**.
2. Paste the site key into `wrangler.toml` `TURNSTILE_SITE_KEY` (the relay
   serves it from `/config` so the form picks it up).
3. `npx wrangler secret put TURNSTILE_SECRET_KEY`, then `npx wrangler deploy`.

The relay verifies a token only when `TURNSTILE_SECRET_KEY` is set; the form
renders the widget only when a site key is present. Leave both unset to stay
captcha-less.

### (Optional) Configure the app
By default the app talks to the deployed relay above — no setup needed. To
override, edit `app-settings.json` (project root) or the App Settings UI:
- `feedback_relay_url` — leave empty to use the default
  (`https://webagent-feedback-relay.botboss.workers.dev/submit`), or a custom URL
- `turnstile_site_key` — leave empty to inherit from the relay, or paste a
  specific site key
- `feedback_enabled` — set to `false` to hide the feedback button entirely

## Local development

```bash
npm run dev
```
Wrangler runs a local Worker on port 8787. Use a dev Turnstile key (the
"always passes" testing key from Cloudflare's docs) so you don't have to
solve a real captcha during local testing.

## Blocking abuse

If a specific IP or installation is spamming:
```bash
npx wrangler kv key put --binding=BLOCKLIST_KV "ip:1.2.3.4" 1
npx wrangler kv key put --binding=BLOCKLIST_KV "install:<uuid>" 1
```
The relay checks the blocklist on every request; entries take effect
immediately. To unblock, `kv key delete` the key.

## Tunables

In `src/index.ts`:
- `RATE_LIMIT_PER_IP` — 10/hour per source IP
- `RATE_LIMIT_PER_INSTALL` — 30/hour per installation_id
- `MAX_BODY_LEN` — 8000 chars
- `RATE_LIMIT_WINDOW_SECONDS` — 3600

In `wrangler.toml`:
- `GITHUB_REPO` — destination repo (`owner/name`)
- `ALLOWED_ORIGINS` — CORS allowlist; `*` is fine here since auth is
  Turnstile-based, not origin-based
