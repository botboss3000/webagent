# webAgent feedback relay

A small Cloudflare Worker that sits between the webAgent app and GitHub
Issues. The app (every clone in the wild, including yours) POSTs feedback
here; this Worker verifies a Turnstile captcha, applies rate limits, and
creates an issue in the configured private feedback repo.

The GitHub token lives only on the Worker. Clones never see it.

## Architecture

```
[webAgent app, anywhere]
   │ POST /api/v1/feedback        (browser → FastAPI)
   ▼
[FastAPI app/api/feedback.py]
   │ POST https://feedback.webagent.live/submit
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

### 3. Set up Cloudflare Turnstile
At dash.cloudflare.com → Turnstile → Add site. Use the **Invisible** widget
mode. You'll get:
- a **site key** (public — paste it into `wrangler.toml` `TURNSTILE_SITE_KEY`;
  the relay serves it from `/config` so clones pick it up automatically)
- a **secret key** (set it via `wrangler secret put TURNSTILE_SECRET_KEY` below)

### 4. Install wrangler and create KV namespaces
```bash
npm install
npx wrangler login
npx wrangler kv namespace create RATE_LIMIT_KV
npx wrangler kv namespace create BLOCKLIST_KV
```
Paste the two returned IDs into `wrangler.toml` (replacing the
`REPLACE_WITH_*` placeholders).

### 5. Set secrets
```bash
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put TURNSTILE_SECRET_KEY
```

### 6. Deploy
```bash
npx wrangler deploy
```

### 7. Point your domain at the Worker
In the Cloudflare dashboard, open the Worker → Triggers → Custom Domains
→ add `feedback.webagent.live`. Cloudflare handles the DNS record
automatically when the apex domain is in the same Cloudflare account.

### 8. (Optional) Configure the app
By default the app talks to the upstream relay and pulls the Turnstile site
key from its `/config` endpoint — no setup needed in the app for normal users.

If you want to override either, edit `app-settings.json` (project root) or
use the App Settings UI:
- `feedback_relay_url` — leave empty to use the default
  (`https://feedback.webagent.live/submit`), or paste a custom URL
- `turnstile_site_key` — leave empty to inherit from the relay, or paste a
  specific site key to use a different Turnstile configuration
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
