"""Seed content for the default `home` page rendered into a new user's
Dashboard iframe on first visit. Fully self-contained: inline CSS + JS,
no external fetches, no localStorage (the iframe is sandboxed).

Theme: dark by default. Parent posts {type:'theme', value:'light'|'dark'}
via window.postMessage and the page toggles `<html class="light">` to swap
palettes. Falls back to `prefers-color-scheme` when opened standalone."""


def home_page_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>webAgent — Home</title>
<style>
:root {
  color-scheme: dark light;
  --bg-page: #0a0a0f;
  --bg-header-from: #0d0d1e;
  --bg-header-to: #160a28;
  --bg-card: #0d0d1e;
  --bg-card-hover: #11112a;
  --bg-card-active-from: rgba(122,162,247,0.08);
  --bg-card-active-to: rgba(187,154,247,0.06);
  --bg-detail-from: #0e0e22;
  --bg-detail-to: #0c0c1b;
  --bg-code: #1a1a2e;
  --bg-badge: rgba(122,162,247,0.10);

  --border: #1e1e3a;
  --border-hover: #3d3d6e;
  --border-active: #7aa2f7;
  --border-code: #2a2a4a;
  --border-badge: rgba(122,162,247,0.25);
  --active-glow: rgba(122,162,247,0.18);

  --fg-strong: #e0def4;
  --fg-body: #c0caf5;
  --fg-soft: #a9b1d6;
  --fg-muted: #6272a4;
  --fg-faint: #565f89;
  --fg-code: #7dcfff;
  --fg-accent: #7aa2f7;
  --fg-link: #7dcfff;

  --title-grad-from: #7dcfff;
  --title-grad-mid:  #7aa2f7;
  --title-grad-to:   #bb9af7;
  --bullet: #bb9af7;
}

html.light {
  --bg-page: #fffaf5;
  --bg-header-from: #ffffff;
  --bg-header-to: #fff5eb;
  --bg-card: #ffffff;
  --bg-card-hover: #fff5eb;
  --bg-card-active-from: rgba(255,140,66,0.10);
  --bg-card-active-to: rgba(255,180,120,0.06);
  --bg-detail-from: #ffffff;
  --bg-detail-to: #fff7f0;
  --bg-code: #fff2e6;
  --bg-badge: rgba(255,140,66,0.10);

  --border: #ffdec4;
  --border-hover: #ffb380;
  --border-active: #ff8c42;
  --border-code: #ffd4a8;
  --border-badge: rgba(255,140,66,0.30);
  --active-glow: rgba(255,140,66,0.22);

  --fg-strong: #3d2c2e;
  --fg-body: #4d3a3c;
  --fg-soft: #5c4a4a;
  --fg-muted: #8a6f5c;
  --fg-faint: #b09580;
  --fg-code: #b85a14;
  --fg-accent: #ff8c42;
  --fg-link: #ff7a2e;

  --title-grad-from: #ff8c42;
  --title-grad-mid:  #ff7a2e;
  --title-grad-to:   #d4873a;
  --bullet: #ff8c42;
}

/* Auto-pick light when no parent override and OS prefers light */
@media (prefers-color-scheme: light) {
  html:not(.dark) {
    --bg-page: #fffaf5;
    --bg-header-from: #ffffff;
    --bg-header-to: #fff5eb;
    --bg-card: #ffffff;
    --bg-card-hover: #fff5eb;
    --bg-card-active-from: rgba(255,140,66,0.10);
    --bg-card-active-to: rgba(255,180,120,0.06);
    --bg-detail-from: #ffffff;
    --bg-detail-to: #fff7f0;
    --bg-code: #fff2e6;
    --bg-badge: rgba(255,140,66,0.10);
    --border: #ffdec4;
    --border-hover: #ffb380;
    --border-active: #ff8c42;
    --border-code: #ffd4a8;
    --border-badge: rgba(255,140,66,0.30);
    --active-glow: rgba(255,140,66,0.22);
    --fg-strong: #3d2c2e;
    --fg-body: #4d3a3c;
    --fg-soft: #5c4a4a;
    --fg-muted: #8a6f5c;
    --fg-faint: #b09580;
    --fg-code: #b85a14;
    --fg-accent: #ff8c42;
    --fg-link: #ff7a2e;
    --title-grad-from: #ff8c42;
    --title-grad-mid: #ff7a2e;
    --title-grad-to: #d4873a;
    --bullet: #ff8c42;
  }
}

*, *::before, *::after { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; padding: 0; background: var(--bg-page); color: var(--fg-body);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 15px; line-height: 1.6;
  transition: background 0.18s, color 0.18s; }
.container { max-width: 980px; margin: 0 auto; padding: 0 24px; }
code { background: var(--bg-code); border: 1px solid var(--border-code); border-radius: 4px;
  padding: 1px 6px; font-size: 12px; font-family: 'Fira Code', monospace; color: var(--fg-code); }

/* Hero */
header { background: linear-gradient(135deg, var(--bg-header-from) 0%, var(--bg-header-to) 100%);
  border-bottom: 1px solid var(--border); padding: 48px 0 40px; text-align: center;
  transition: background 0.18s, border-color 0.18s; }
.badge { display: inline-block; background: var(--bg-badge);
  border: 1px solid var(--border-badge); border-radius: 20px; padding: 4px 14px;
  font-size: 11px; color: var(--fg-accent); letter-spacing: 0.08em; text-transform: uppercase;
  margin-bottom: 16px; }
header h1 { margin: 0 0 12px; font-size: 46px; font-weight: 700;
  background: linear-gradient(135deg, var(--title-grad-from) 0%, var(--title-grad-mid) 50%, var(--title-grad-to) 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
header p { margin: 0 auto; font-size: 16px; color: var(--fg-soft); max-width: 600px; }

main { padding: 40px 0 56px; }

/* Card grid */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
  margin-bottom: 28px;
}
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px 16px;
  cursor: pointer;
  user-select: none;
  transition: border-color 0.18s, transform 0.15s, background 0.18s;
  outline: none;
}
.card:hover, .card:focus-visible {
  border-color: var(--border-hover);
  transform: translateY(-2px);
  background: var(--bg-card-hover);
}
.card.active {
  border-color: var(--border-active);
  background: linear-gradient(135deg, var(--bg-card-active-from), var(--bg-card-active-to));
  box-shadow: 0 0 0 1px var(--active-glow);
}
.card .icon { font-size: 22px; margin-bottom: 10px; line-height: 1; }
.card h3 { margin: 0 0 5px; font-size: 14px; font-weight: 600; color: var(--fg-strong); }
.card p { margin: 0; font-size: 12.5px; color: var(--fg-muted); line-height: 1.5; }

/* Detail panel */
.detail {
  background: linear-gradient(180deg, var(--bg-detail-from) 0%, var(--bg-detail-to) 100%);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 28px 28px 24px;
  margin-bottom: 24px;
  transition: background 0.18s, border-color 0.18s;
}
.detail-head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.detail-head .icon { font-size: 28px; line-height: 1; }
.detail-head h2 { margin: 0; font-size: 22px; font-weight: 600; color: var(--fg-strong); }
.detail-body { font-size: 14px; color: var(--fg-soft); }
.detail-body p { margin: 0 0 12px; }
.detail-body p:last-child { margin: 0; }
.detail-body strong { color: var(--fg-body); }
.detail-body em { color: var(--fg-link); font-style: normal; }
.detail-list { margin: 6px 0 14px; padding: 0; list-style: none; display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px 18px; }
.detail-list li { display: flex; gap: 8px; font-size: 13px; color: var(--fg-soft); }
.detail-list .b { color: var(--bullet); flex-shrink: 0; font-weight: 700; }

/* Footer hint */
.hint { text-align: center; padding: 20px 0; font-size: 13px; color: var(--fg-muted);
  border-top: 1px solid var(--border); }
.hint strong { color: var(--fg-link); font-weight: 600; }
</style>
</head>
<body>

<header>
  <div class="container">
    <div class="badge">Open Agent Platform</div>
    <h1>webAgent</h1>
    <p>Build AI agents that fit your business — from a simple support chatbot to an autonomous developer that ships code — and watch every step they take.</p>
  </div>
</header>

<main>
  <div class="container">

    <div class="grid" id="grid" role="tablist" aria-label="Platform features">
      <div class="card" role="tab" tabindex="0" data-feature="agents" aria-selected="true">
        <div class="icon">🛠️</div>
        <h3>Build Custom Agents</h3>
        <p>From simple chatbots to autonomous codebase editors — pick the tools, knowledge, and personality.</p>
      </div>
      <div class="card" role="tab" tabindex="0" data-feature="hosting">
        <div class="icon">☁️</div>
        <h3>Self-Host Anywhere</h3>
        <p>Deploy to AWS, GCP, Azure, or bare metal. No vendor lock-in on infra or LLM.</p>
      </div>
      <div class="card" role="tab" tabindex="0" data-feature="tenants">
        <div class="icon">👥</div>
        <h3>Multi-Tenant by Design</h3>
        <p>One agent, unlimited users — each with their own isolated session, memory, and tailored experience.</p>
      </div>
      <div class="card" role="tab" tabindex="0" data-feature="local">
        <div class="icon">💻</div>
        <h3>Run Locally</h3>
        <p>Clone, set an API key, and run on your laptop in minutes. Works offline with a local model.</p>
      </div>
      <div class="card" role="tab" tabindex="0" data-feature="code">
        <div class="icon">📝</div>
        <h3>Full Code Read / Write / Edit</h3>
        <p>Agents read, modify, and ship code in the very codebase they live in. Drop into any app for instant AI.</p>
      </div>
      <div class="card" role="tab" tabindex="0" data-feature="ui">
        <div class="icon">🎨</div>
        <h3>AI-Built UI</h3>
        <p>Agents generate functional pages on demand. This one is an example — edit it with a prompt above.</p>
      </div>
    </div>

    <div class="detail" id="detail" role="tabpanel" aria-live="polite">
      <div class="detail-head">
        <div class="icon" id="detail-icon">🛠️</div>
        <h2 id="detail-title">Build Custom Agents</h2>
      </div>
      <div class="detail-body" id="detail-body"></div>
    </div>

    <div class="hint">
      <strong>Try it:</strong> use the prompt bar above to say <em>"add a section about pricing"</em> or <em>"redesign the layout"</em> and the agent will edit this page directly.
    </div>

  </div>
</main>

<script>
const DETAILS = {
  agents: {
    icon: "🛠️",
    title: "Build Custom Agents",
    body:
      '<p>Design an agent for any role across the full capability spectrum — pick the tools, knowledge sources, system prompt, and personality.</p>' +
      '<ul class="detail-list">' +
        '<li><span class="b">•</span><span><strong>Support agent</strong> — answers questions from your docs, escalates to humans.</span></li>' +
        '<li><span class="b">•</span><span><strong>Research agent</strong> — searches the web, builds a memory of findings.</span></li>' +
        '<li><span class="b">•</span><span><strong>Workflow agent</strong> — triggers automations, calls webhooks on a schedule.</span></li>' +
        '<li><span class="b">•</span><span><strong>Codebase agent</strong> — reads, edits, tests, and ships changes to a repo.</span></li>' +
      '</ul>' +
      '<p>Every reasoning step, tool call, and state change streams live to the <em>Stream</em>, <em>Flow</em>, and <em>Runtime Loop</em> tabs — you can <strong>watch the agent think</strong> while it works, diagnose surprises, and tune behavior from real traces.</p>'
  },
  hosting: {
    icon: "☁️",
    title: "Self-Host Anywhere",
    body:
      '<p>Deploy to any provider you already trust — AWS, GCP, Azure, Cloud Run, Fly.io, your own bare metal — or run it inside your existing app. The codebase is plain Python + FastAPI with a Dockerfile included.</p>' +
      '<p>No lock-in on the LLM either: swap between OpenRouter, OpenAI, Anthropic, local Llama models, or anything else with an OpenAI-compatible API. Provider keys live in a pluggable secrets vault (env vars, OS keyring, GCP Secret Manager, or AWS Secrets Manager).</p>'
  },
  tenants: {
    icon: "👥",
    title: "Multi-Tenant by Design",
    body:
      '<p>One agent definition, unlimited users. Every user gets their own isolated session, memory, attachments, automations, and personalized agent state — but they all share the same underlying agent template you built.</p>' +
      '<p>Per-tenant data isolation is enforced at the storage layer with <code>user_id</code> scoping on every row. Optional field-level encryption with per-tenant keys means even a DB compromise doesn\\'t leak one user\\'s data to another.</p>'
  },
  local: {
    icon: "💻",
    title: "Run Locally",
    body:
      '<p>Local-first is the default. Clone the repo, copy <code>.env.example</code> to <code>.env</code>, set a single API key, run <code>python run.py</code>, and you have a full agent platform on your laptop in minutes.</p>' +
      '<p>SQLite ships as the default database, so there\\'s nothing external to provision. Point it at a local Llama model and the whole thing runs offline — useful for private data, air-gapped environments, or just developing without burning API credits.</p>'
  },
  code: {
    icon: "📝",
    title: "Full Code Read / Write / Edit",
    body:
      '<p>Agents can read, modify, and ship code in the codebase they\\'re embedded in. Drop webAgent into your existing website or web app and the agent immediately gets the power to edit the surrounding code — turning any product into one with AI capabilities.</p>' +
      '<p>Or run it as an <strong>admin AI for your own repo</strong>: an agent that lives inside the code, ships pull requests, fixes bugs, refactors modules, and updates docs on demand. Every file the agent touches is streamed live so you can review intent and impact before merging.</p>'
  },
  ui: {
    icon: "🎨",
    title: "AI-Built UI",
    body:
      '<p>Agents generate functional HTML pages on demand. This is one of them. Use the prompt bar above the iframe to say <em>"add a pricing section"</em> or <em>"make it lighter"</em> and the agent rewrites the page in place.</p>' +
      '<p>Add brand-new pages by describing what you want — a dashboard, a notes app, a generative-art sketch, an internal admin tool. Each page gets its own dedicated agent that knows its purpose, so you can iterate on pages independently without losing context.</p>'
  }
};

const grid = document.getElementById('grid');
const detailIcon = document.getElementById('detail-icon');
const detailTitle = document.getElementById('detail-title');
const detailBody = document.getElementById('detail-body');

function select(featureId) {
  const data = DETAILS[featureId];
  if (!data) return;
  detailIcon.textContent = data.icon;
  detailTitle.textContent = data.title;
  detailBody.innerHTML = data.body;
  for (const card of grid.querySelectorAll('.card')) {
    const isActive = card.dataset.feature === featureId;
    card.classList.toggle('active', isActive);
    card.setAttribute('aria-selected', isActive ? 'true' : 'false');
  }
}

grid.addEventListener('click', (e) => {
  const card = e.target.closest('.card');
  if (card) select(card.dataset.feature);
});

grid.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const card = e.target.closest('.card');
  if (!card) return;
  e.preventDefault();
  select(card.dataset.feature);
});

// Theme: listen for the parent app's theme via postMessage. Falls back to
// the @media(prefers-color-scheme) rule above when opened standalone.
window.addEventListener('message', (e) => {
  const msg = e && e.data;
  if (!msg || msg.type !== 'theme') return;
  const html = document.documentElement;
  if (msg.value === 'light') { html.classList.add('light'); html.classList.remove('dark'); }
  else { html.classList.add('dark'); html.classList.remove('light'); }
});

// Prepopulate with the first card
select('agents');
</script>

</body>
</html>"""
