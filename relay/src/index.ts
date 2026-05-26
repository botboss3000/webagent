/**
 * webAgent feedback relay (Cloudflare Worker).
 *
 * Receives JSON feedback submissions from any webAgent install (your hosted
 * deployment, or any cloned/forked instance), verifies a Turnstile token,
 * applies per-IP and per-installation rate limits, and creates a GitHub
 * issue in the configured private feedback repo.
 *
 * The clones never see the GitHub token. They only know this Worker's URL.
 *
 * Secrets (set via `wrangler secret put`):
 *   GITHUB_TOKEN          - PAT with `issues:write` on the feedback repo
 *   TURNSTILE_SECRET_KEY  - matching secret for the public site key
 *
 * Vars (set in wrangler.toml):
 *   GITHUB_REPO           - e.g. "botboss3000/webagent-feedback"
 *   ALLOWED_ORIGINS       - comma-separated list of origins permitted via CORS
 *
 * Bindings:
 *   RATE_LIMIT_KV   - Workers KV namespace used as a sliding-window counter
 *   BLOCKLIST_KV    - Workers KV namespace; keys are "ip:<addr>" or
 *                     "install:<uuid>"; presence = blocked
 */

export interface Env {
  GITHUB_TOKEN: string;
  TURNSTILE_SECRET_KEY: string;
  TURNSTILE_SITE_KEY: string;  // PUBLIC site key — safe to expose
  GITHUB_REPO: string;
  ALLOWED_ORIGINS: string;
  RATE_LIMIT_KV: KVNamespace;
  BLOCKLIST_KV: KVNamespace;
}

const MAX_BODY_LEN = 8000;
const MAX_EMAIL_LEN = 254;
const RATE_LIMIT_WINDOW_SECONDS = 3600;
const RATE_LIMIT_PER_IP = 10;
const RATE_LIMIT_PER_INSTALL = 30;

interface Payload {
  type: "bug" | "feature" | "message";
  body: string;
  email?: string | null;
  turnstile_token?: string | null;
  installation_id: string;
  app_version?: string;
  client_ip_hint?: string;
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const origin = request.headers.get("Origin") || "";
    const corsHeaders = buildCorsHeaders(origin, env.ALLOWED_ORIGINS);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return json({ ok: true }, 200, corsHeaders);
    }
    if (url.pathname === "/config") {
      // Public bits that clones need to render the form. Safe to expose.
      return json(
        { turnstile_site_key: env.TURNSTILE_SITE_KEY || "" },
        200,
        corsHeaders,
      );
    }
    if (request.method !== "POST" || url.pathname !== "/submit") {
      return json({ error: "Not found" }, 404, corsHeaders);
    }

    // ── Parse + validate ────────────────────────────────────────────────
    let p: Payload;
    try {
      p = await request.json();
    } catch {
      return json({ error: "Invalid JSON" }, 400, corsHeaders);
    }
    const err = validatePayload(p);
    if (err) return json({ error: err }, 400, corsHeaders);

    const ip =
      request.headers.get("CF-Connecting-IP") ||
      request.headers.get("X-Forwarded-For")?.split(",")[0]?.trim() ||
      "unknown";

    // ── Blocklist check ─────────────────────────────────────────────────
    const [ipBlocked, installBlocked] = await Promise.all([
      env.BLOCKLIST_KV.get(`ip:${ip}`),
      env.BLOCKLIST_KV.get(`install:${p.installation_id}`),
    ]);
    if (ipBlocked || installBlocked) {
      return json({ error: "Forbidden" }, 403, corsHeaders);
    }

    // ── Turnstile verification ──────────────────────────────────────────
    if (env.TURNSTILE_SECRET_KEY) {
      const ok = await verifyTurnstile(env.TURNSTILE_SECRET_KEY, p.turnstile_token || "", ip);
      if (!ok) {
        return json({ error: "Captcha verification failed" }, 403, corsHeaders);
      }
    }

    // ── Rate limit (sliding-window per IP and per installation) ─────────
    const rl = await checkRateLimits(env.RATE_LIMIT_KV, ip, p.installation_id);
    if (!rl.ok) {
      return json({ error: rl.reason }, 429, corsHeaders);
    }

    // ── Create the GitHub issue ─────────────────────────────────────────
    let issueUrl: string;
    try {
      issueUrl = await createGithubIssue(env, p);
    } catch (e) {
      console.error("GitHub issue creation failed", e);
      return json({ error: "Upstream error" }, 502, corsHeaders);
    }

    return json({ ok: true, issue_url: issueUrl }, 200, corsHeaders);
  },
};

// ── Helpers ────────────────────────────────────────────────────────────

function validatePayload(p: any): string | null {
  if (!p || typeof p !== "object") return "Bad payload";
  if (!["bug", "feature", "message"].includes(p.type)) return "Invalid type";
  if (typeof p.body !== "string" || p.body.length < 1 || p.body.length > MAX_BODY_LEN) {
    return "Body must be 1..8000 chars";
  }
  if (p.email != null) {
    if (typeof p.email !== "string" || p.email.length > MAX_EMAIL_LEN) return "Invalid email";
    if (p.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(p.email)) return "Invalid email";
  }
  if (typeof p.installation_id !== "string" || p.installation_id.length < 16) {
    return "Missing installation_id";
  }
  return null;
}

function buildCorsHeaders(origin: string, allowed: string): Record<string, string> {
  const list = (allowed || "").split(",").map((s) => s.trim()).filter(Boolean);
  const allow = list.includes("*") || list.includes(origin) ? origin || "*" : list[0] || "*";
  return {
    "Access-Control-Allow-Origin": allow,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

function json(body: unknown, status: number, extra: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extra },
  });
}

async function verifyTurnstile(secret: string, token: string, ip: string): Promise<boolean> {
  if (!token) return false;
  const form = new FormData();
  form.append("secret", secret);
  form.append("response", token);
  form.append("remoteip", ip);
  try {
    const r = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      body: form,
    });
    const data: any = await r.json();
    return Boolean(data?.success);
  } catch {
    return false;
  }
}

interface RateResult { ok: boolean; reason?: string }

async function checkRateLimits(kv: KVNamespace, ip: string, install: string): Promise<RateResult> {
  const now = Math.floor(Date.now() / 1000);
  const bucket = Math.floor(now / RATE_LIMIT_WINDOW_SECONDS);

  const [ipCountRaw, installCountRaw] = await Promise.all([
    kv.get(`ip:${ip}:${bucket}`),
    kv.get(`install:${install}:${bucket}`),
  ]);
  const ipCount = parseInt(ipCountRaw || "0", 10);
  const installCount = parseInt(installCountRaw || "0", 10);

  if (ipCount >= RATE_LIMIT_PER_IP) return { ok: false, reason: "IP rate limit exceeded" };
  if (installCount >= RATE_LIMIT_PER_INSTALL) {
    return { ok: false, reason: "Installation rate limit exceeded" };
  }

  await Promise.all([
    kv.put(`ip:${ip}:${bucket}`, String(ipCount + 1), { expirationTtl: RATE_LIMIT_WINDOW_SECONDS * 2 }),
    kv.put(`install:${install}:${bucket}`, String(installCount + 1), {
      expirationTtl: RATE_LIMIT_WINDOW_SECONDS * 2,
    }),
  ]);
  return { ok: true };
}

async function createGithubIssue(env: Env, p: Payload): Promise<string> {
  const typeLabels: Record<string, string> = {
    bug: "bug",
    feature: "enhancement",
    message: "feedback",
  };
  const titlePrefix: Record<string, string> = {
    bug: "[Bug]",
    feature: "[Feature]",
    message: "[Message]",
  };
  const firstLine = p.body.split("\n")[0]?.slice(0, 80) || "Untitled feedback";
  const title = `${titlePrefix[p.type]} ${firstLine}`;

  const meta = [
    `**Installation:** \`${p.installation_id}\``,
    p.app_version ? `**App version:** \`${p.app_version}\`` : "",
    p.email ? `**Reply email:** ${escapeMarkdown(p.email)}` : "",
  ].filter(Boolean).join("\n");

  const issueBody =
    `${meta}\n\n---\n\n${p.body}\n`;

  const [owner, repo] = env.GITHUB_REPO.split("/");
  const r = await fetch(`https://api.github.com/repos/${owner}/${repo}/issues`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "webagent-feedback-relay",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ title, body: issueBody, labels: [typeLabels[p.type]] }),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`GitHub ${r.status}: ${text.slice(0, 500)}`);
  }
  const data: any = await r.json();
  return data.html_url;
}

function escapeMarkdown(s: string): string {
  return s.replace(/[\\`*_{}\[\]()#+\-.!|>]/g, "\\$&");
}
