'use strict';

// Database & Devices — drop-in Admin Tools view module.
//
// The app's shared backbone, as a first-class page. The admin SIGNS IN to the
// database and the secrets vault they want; the page then shows every other
// DEVICE signed in to that same database. Rendered into #smb-root (the `smb-` =
// "shared backbone" prefix is internal, like `cvm-` on the Server Manager page).
//
// Discovered via ui/admin-tools/database-devices/page.json and driven generically
// by the admin shell (ui/shared/js/files.js applySidebarView), which dynamically
// imports this module and calls the exported startView / stopView when the view
// is shown / hidden.
//
// IMPORTANT — this does NOT re-implement the storage backend. Connect/Test/
// Activate call the existing /admin/storage/* endpoints (the same ones the legacy
// App Config -> Database page uses), so behaviour stays identical even though this
// is a fresh UI. Field KEYS below must match those endpoints exactly. Live health,
// the shared/local classification, and the linked-device list come from this
// folder's server.py (/admin/database-devices/*).
//
// All colours come from the design-system palette (CSS variables only) so the
// page is correct in dark + light.
// REMOVE-WHEN: the Database & Devices view is dropped from the admin page catalog.

import { apiPath } from '../../shared/js/config.js';
import { _esc, _escAttr, _refreshLucideIcons } from '../../shared/js/dom-utils.js';

// open: which cards are expanded into their connect form.
// sel: provider chosen in each card's dropdown (null = follow the active one),
//   tracked so a re-render keeps the user's choice instead of snapping back.
// status/config: last /backend-status + /admin/storage/config. devices: presence.
const S = {
  wired: false, open: { db: false, vault: false }, sel: { db: null, vault: null },
  status: null, config: null, devices: null, busy: false,
};

function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }
function _root() { return document.getElementById('smb-root'); }

// ── API helpers (talk to /admin/storage/* and /admin/database-devices/*) ─────
async function _get(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath(path + sep + 'requesting_user_id=' + encodeURIComponent(_uid())));
  let body = null;
  try { body = await res.json(); } catch {}
  return { ok: res.ok, status: res.status, body };
}
async function _post(path, payload) {
  const res = await fetch(apiPath(path), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _uid(), ...(payload || {}) }),
  });
  let body = null;
  try { body = await res.json(); } catch {}
  return { ok: res.ok, status: res.status, body };
}

// ── Provider field specs (keys MUST match the /admin/storage endpoints) ──────
// DB providers + their connection fields (mirrors FIELD_SPECS in storage.js).
const DB_PROVIDERS = [
  { v: 'sqlite', t: 'SQLite (local file)' },
  { v: 'postgres', t: 'Postgres' },
  { v: 'supabase', t: 'Supabase' },
  { v: 'aws_rds', t: 'Amazon RDS' },
  { v: 'gcp_cloud_sql', t: 'Google Cloud SQL' },
  { v: 'azure_postgres', t: 'Azure Database' },
  { v: 'neon', t: 'Neon' },
];
const _PG_FIELDS = [
  { key: 'host', label: 'Host', type: 'text', required: true },
  { key: 'port', label: 'Port', type: 'number', placeholder: '5432', half: true },
  { key: 'database', label: 'Database name', type: 'text', required: true },
  { key: 'username', label: 'Username', type: 'text', required: true },
  { key: 'password', label: 'Password', type: 'password', keep: true },
];
const DB_FIELDS = {
  sqlite: [],
  supabase: [
    { key: 'supabase_url', label: 'Project URL', type: 'text', required: true, placeholder: 'https://xxxx.supabase.co' },
    { key: 'supabase_service_key', label: 'Service role key', type: 'password', keep: true, placeholder: 'eyJhbGciOi…' },
  ],
  postgres: [..._PG_FIELDS, { key: 'ssl_mode', label: 'SSL mode', type: 'select', options: ['disable', 'require', 'verify-ca', 'verify-full'], half: true }],
  aws_rds: _PG_FIELDS,
  gcp_cloud_sql: _PG_FIELDS,
  azure_postgres: _PG_FIELDS,
  neon: _PG_FIELDS,
};
const DB_NOTE = {
  sqlite: 'A single local file on this device. Zero setup — but local-only: nothing is shared with other devices.',
  postgres: 'Any Postgres you run. Shared across every device that signs in to it.',
  supabase: 'Managed Postgres (URL + service-role key). Shared across devices; survives this app-host dying.',
  aws_rds: 'Managed Postgres on AWS (RDS / Aurora). Shared across devices.',
  gcp_cloud_sql: 'Managed Postgres on Google Cloud. Shared across devices.',
  azure_postgres: 'Managed Postgres on Azure (Flexible Server). Shared across devices.',
  neon: 'Serverless Postgres with scale-to-zero. Shared across devices.',
};

// Vault providers + their connection fields (mirrors SECRETS_FIELDS in storage.js).
const VAULT_PROVIDERS = [
  { v: 'inline_db', t: 'App DB (default, plaintext)' },
  { v: 'env', t: 'Environment variables (read-only)' },
  { v: 'os_keyring', t: 'OS Keyring (this machine only)' },
  { v: 'hashicorp_vault', t: 'HashiCorp Vault' },
  { v: 'azure_key_vault', t: 'Azure Key Vault' },
  { v: 'gcp_secret_manager', t: 'GCP Secret Manager' },
  { v: 'aws_secrets_manager', t: 'AWS Secrets Manager' },
];
const VAULT_FIELDS = {
  hashicorp_vault: [
    { key: 'address', label: 'Vault address', placeholder: 'https://vault.example.com:8200', required: true },
    { key: 'token', label: 'Vault token', secret: true, placeholder: 's.xxxxxxxx' },
    { key: 'kv_mount', label: 'KV mount', placeholder: 'secret', optional: true, half: true },
    { key: 'kv_prefix', label: 'Key prefix', placeholder: 'webagent', optional: true, half: true },
  ],
  azure_key_vault: [
    { key: 'vault_url', label: 'Key Vault URL', placeholder: 'https://my-vault.vault.azure.net', required: true },
    { key: 'tenant_id', label: 'Tenant ID', placeholder: '(optional)', optional: true, half: true },
    { key: 'client_id', label: 'Client ID', placeholder: '(optional)', optional: true, half: true },
    { key: 'token', label: 'Client secret', secret: true, placeholder: '(optional)', optional: true },
  ],
  gcp_secret_manager: [
    { key: 'project', label: 'GCP project ID', placeholder: 'my-gcp-project', required: true },
    { key: 'token', label: 'Service-account key (JSON)', secret: true, textarea: true, optional: true, placeholder: 'Paste the whole service-account JSON, or leave blank to use the host\'s default Google credentials.' },
  ],
  aws_secrets_manager: [
    { key: 'region', label: 'AWS region', placeholder: 'us-east-1', required: true },
    { key: 'access_key_id', label: 'Access key ID', placeholder: '(optional)', optional: true, half: true },
    { key: 'token', label: 'Secret access key', secret: true, placeholder: '(optional)', optional: true, half: true },
  ],
};
const VAULT_NOTE = {
  inline_db: 'Secrets stored in the app database (plaintext). Fine for local dev — but on a shared remote DB this traps the DB password inside the DB it unlocks. Upgrade before production.',
  env: 'Secrets injected by your deployment as environment variables. Read-only — the app can\'t store new tokens.',
  os_keyring: 'This machine\'s own credential store. Strong on one box, but per-machine: other devices can\'t share these secrets.',
  hashicorp_vault: 'A Vault server you run (or HCP Vault). Cloud-agnostic and shared across every device.',
  azure_key_vault: 'Managed secrets in Azure, shared across devices.',
  gcp_secret_manager: 'Managed secrets in Google Cloud, shared across every device — the right pick for a multi-device setup.',
  aws_secrets_manager: 'Managed secrets in AWS, shared across devices.',
};

// ── Small render helpers ─────────────────────────────────────────────────────
function _dot(reachable, busy) {
  const cls = busy ? 'cvm-dot-busy' : reachable ? 'cvm-dot-run' : 'cvm-dot-stop';
  return '<span class="cvm-dot ' + cls + '"></span>';
}
function _provLabel(list, v) { const o = list.find(x => x.v === v); return o ? o.t : v; }

// Build one labelled input/select for a connect form.
function _fieldHtml(scope, f, savedVal) {
  const id = 'smb-f-' + scope + '-' + _escAttr(f.key);
  const req = (f.required && !f.keep && !f.secret) ? ' <span class="smb-req">*</span>' : '';
  const half = f.half ? ' smb-field-half' : '';
  let control;
  if (f.type === 'select') {
    control = '<select id="' + id + '" class="smb-input" data-fk="' + _escAttr(f.key) + '">'
      + (f.options || []).map(o => '<option value="' + _escAttr(o) + '"' + (o === savedVal ? ' selected' : '') + '>' + _esc(o) + '</option>').join('')
      + '</select>';
  } else if (f.textarea) {
    const ph = (f.secret && f.keep) ? 'saved — leave blank to keep' : (f.placeholder || '');
    control = '<textarea id="' + id + '" class="smb-input smb-textarea" rows="3" data-fk="' + _escAttr(f.key)
      + '" data-secret="' + (f.secret ? '1' : '0') + '" placeholder="' + _escAttr(ph) + '"></textarea>';
  } else {
    const isSecret = f.secret || f.type === 'password';
    const keepPh = (isSecret || f.keep) ? 'saved — leave blank to keep' : (f.placeholder || '');
    const type = isSecret ? 'password' : (f.type === 'number' ? 'number' : 'text');
    const val = (!isSecret && savedVal != null) ? ' value="' + _escAttr(savedVal) + '"' : '';
    control = '<input id="' + id + '" class="smb-input" type="' + type + '" data-fk="' + _escAttr(f.key) + '"'
      + ' data-secret="' + (f.secret ? '1' : '0') + '"' + val
      + ' placeholder="' + _escAttr((isSecret || f.keep) ? keepPh : (f.placeholder || '')) + '"'
      + ' autocomplete="off" spellcheck="false">';
  }
  return '<div class="smb-field' + half + '"><label class="smb-label" for="' + id + '">' + _esc(f.label) + req + '</label>' + control + '</div>';
}

// ── The shared-vs-local info panel (the "brain map") ─────────────────────────
function _infoPanelHtml() {
  const st = S.status || {};
  const db = st.db || {}; const vault = st.vault || {};
  const dbShared = !!db.shared;
  const vaultReach = vault.reach || 'local';            // shared | local | in_db | external
  const dev = st.device || {};

  const shared = [
    'Users &amp; sign-ins', 'Agents &amp; their settings', 'Chats &amp; history',
    'Automations &amp; schedules', 'Gen UI &amp; wiki', 'Device presence &amp; the cross-device job queue',
  ];
  if (vaultReach === 'in_db') shared.push('Vault secrets <span class="smb-tag">in the app DB</span>');
  const local = [
    'This device\'s identity &amp; name', 'How to reach the database (connection details)',
    'How to reach the vault (connection details)', 'The model / LLM API key <span class="smb-tag">local file</span>',
    'This machine\'s own tools, browser &amp; terminal', 'Local cache / offline shell',
  ];
  if (vaultReach === 'local') local.push('OS-keyring secrets <span class="smb-tag">per machine</span>');

  const li = (items) => items.map(t => '<li>' + t + '</li>').join('');

  let headline;
  if (!dbShared) {
    headline = '<i data-lucide="alert-triangle"></i> This database is <b>local SQLite</b> — nothing is shared across devices yet. '
      + 'Sign in to a Postgres / Supabase database below to turn this into a shared, multi-device system.';
  } else {
    const vWord = vaultReach === 'shared' ? 'a <b>shared cloud vault</b>'
      : vaultReach === 'local' ? 'the <b>per-machine OS keyring</b>'
      : vaultReach === 'in_db' ? 'the <b>in-database vault</b>' : 'externally-managed secrets';
    headline = '<i data-lucide="check-circle"></i> Signed in to a shared database — every device here shares the same app. Secrets use ' + vWord + '.'
      + (vaultReach !== 'shared'
        ? ' <span class="smb-warn">For more than one device, switch to a shared cloud vault so every machine reads the same secrets.</span>'
        : '');
  }

  return '<div class="smb-info">'
    + '<div class="smb-info-headline">' + headline + '</div>'
    + '<div class="smb-info-cols">'
    +   '<div class="smb-info-col smb-col-shared">'
    +     '<div class="smb-col-head"><i data-lucide="globe"></i> Shared — one copy, every device sees it</div>'
    +     '<ul class="smb-col-list">' + li(shared) + '</ul>'
    +   '</div>'
    +   '<div class="smb-info-col smb-col-local">'
    +     '<div class="smb-col-head"><i data-lucide="hard-drive"></i> Local to this device' + (dev.label ? ' <span class="smb-tag">' + _esc(dev.label) + '</span>' : '') + '</div>'
    +     '<ul class="smb-col-list">' + li(local) + '</ul>'
    +   '</div>'
    + '</div>'
    + '</div>';
}

// ── "Couldn't reach the shared database" banner ──────────────────────────────
// Shown when the saved config points at a shared (Postgres/Supabase) database but
// the app silently dropped to this device's local SQLite copy. The local copy
// always probes green, so without this the failure is invisible — this is the
// loud, plain-English alert that replaces the old quiet fallback.
function _degradedBannerHtml() {
  const db = (S.status && S.status.db) || {};
  if (!db.degraded) return '';
  const msg = db.fallback_message
    || 'Couldn\'t reach the shared database, so the app is running on this device\'s local copy.';
  const detail = db.fallback_detail
    ? '<span class="smb-degraded-detail">' + _esc(db.fallback_detail) + '</span>' : '';
  return '<div class="smb-degraded">'
    + '<i data-lucide="database-zap"></i>'
    + '<div><span class="smb-degraded-title">Not connected to the shared database</span><br>'
    +   _esc(msg) + detail
    +   '<button class="smb-btn" data-act="fix-db">Open database sign-in</button>'
    + '</div></div>';
}

// ── The DATABASE card ────────────────────────────────────────────────────────
function _dbCardHtml() {
  const st = S.status || {};
  const db = st.db || {};
  const provLabel = _provLabel(DB_PROVIDERS, db.provider || 'sqlite');
  // A silent fallback to local still probes "reachable" — show the truth instead.
  const reachTxt = db.degraded ? 'On local copy — not connected'
    : db.reachable ? 'Connected' : (db.detail || 'Unreachable');
  const sharedBadge = db.shared
    ? '<span class="smb-badge smb-badge-shared">Shared</span>'
    : '<span class="smb-badge smb-badge-local">Local only</span>';
  const head =
      '<div class="smb-card-head" data-toggle="db">'
    +   _dot(db.degraded ? false : db.reachable, S.busy)
    +   '<i data-lucide="database" class="smb-card-icon"></i>'
    +   '<div class="smb-card-titles"><div class="smb-card-title">Database</div>'
    +     '<div class="smb-card-sub">' + _esc(provLabel) + ' · ' + _esc(db.target || '') + '</div></div>'
    +   sharedBadge
    +   '<span class="smb-card-status">' + _esc(reachTxt) + '</span>'
    +   '<i data-lucide="chevron-' + (S.open.db ? 'down' : 'right') + '" class="smb-chevron"></i>'
    + '</div>';
  const body = S.open.db ? '<div class="smb-card-body">' + _dbFormHtml() + '</div>' : '';
  return '<div class="smb-card' + (S.open.db ? ' smb-open' : '') + '">' + head + body + '</div>';
}

function _dbFormHtml() {
  const active = (S.config && S.config.db && S.config.db.active) || {};
  // On first open the selected provider IS the active one; after a manual switch
  // S.sel.db drives it. Only prefill fields when they match the active provider
  // (a different provider's saved values don't belong in this form).
  const sel = S.sel.db || active.provider || 'sqlite';
  const prefill = (sel === active.provider) ? active : {};
  const opts = DB_PROVIDERS.map(p => '<option value="' + p.v + '"' + (p.v === sel ? ' selected' : '') + '>' + _esc(p.t) + '</option>').join('');
  const isSupabase = sel === 'supabase';
  return ''
    + '<label class="smb-label">Provider</label>'
    + '<select id="smb-db-provider" class="smb-input">' + opts + '</select>'
    + '<div class="smb-note">' + _esc(DB_NOTE[sel] || '') + '</div>'
    + '<div id="smb-db-fields" class="smb-fields">' + _dbFieldsHtml(sel, prefill) + '</div>'
    + '<div class="smb-actions">'
    +   '<button class="smb-btn" data-act="db-test">Test</button>'
    +   '<button class="smb-btn" data-act="db-save">Save</button>'
    +   '<button class="smb-btn" data-act="db-sql">' + (isSupabase ? 'Create Tables (SQL Editor)' : 'Show Schema SQL') + '</button>'
    +   (isSupabase ? '' : '<button class="smb-btn" data-act="db-bootstrap">Auto-Create Tables</button>')
    +   '<button class="smb-btn smb-btn-primary" data-act="db-activate">Sign in (activate)</button>'
    + '</div>'
    + '<pre id="smb-db-output" class="smb-output" hidden></pre>';
}

function _dbFieldsHtml(provider, active) {
  const specs = DB_FIELDS[provider] || [];
  if (!specs.length) return '<div class="smb-hint">No connection details needed — the file lives under data/db/.</div>';
  return specs.map(f => _fieldHtml('db', f, active ? active[f.key] : '')).join('');
}

// ── The VAULT card ───────────────────────────────────────────────────────────
function _vaultCardHtml() {
  const st = S.status || {};
  const v = st.vault || {};
  const provLabel = _provLabel(VAULT_PROVIDERS, v.provider || 'inline_db');
  const reachTxt = v.reachable ? 'Reachable' : (v.detail || 'Unreachable');
  const reach = v.reach || 'local';
  const badge = reach === 'shared' ? '<span class="smb-badge smb-badge-shared">Shared</span>'
    : reach === 'in_db' ? '<span class="smb-badge smb-badge-warn">In DB</span>'
    : reach === 'external' ? '<span class="smb-badge smb-badge-local">External</span>'
    : '<span class="smb-badge smb-badge-local">This machine</span>';
  const head =
      '<div class="smb-card-head" data-toggle="vault">'
    +   _dot(v.reachable, S.busy)
    +   '<i data-lucide="key-round" class="smb-card-icon"></i>'
    +   '<div class="smb-card-titles"><div class="smb-card-title">Secrets vault</div>'
    +     '<div class="smb-card-sub">' + _esc(provLabel) + ' · ' + _esc(v.locator || '') + '</div></div>'
    +   badge
    +   '<span class="smb-card-status">' + _esc(reachTxt) + '</span>'
    +   '<i data-lucide="chevron-' + (S.open.vault ? 'down' : 'right') + '" class="smb-chevron"></i>'
    + '</div>';
  const body = S.open.vault ? '<div class="smb-card-body">' + _vaultFormHtml() + '</div>' : '';
  return '<div class="smb-card' + (S.open.vault ? ' smb-open' : '') + '">' + head + body + '</div>';
}

function _vaultFormHtml() {
  const secrets = (S.config && S.config.secrets) || {};
  const sel = S.sel.vault || secrets.provider || 'inline_db';
  const opts = VAULT_PROVIDERS.map(p => '<option value="' + p.v + '"' + (p.v === sel ? ' selected' : '') + '>' + _esc(p.t) + '</option>').join('');
  const restart = (S.status && S.status.vault && S.status.vault.restart_recommended)
    ? '<div class="smb-restart"><i data-lucide="alert-triangle"></i> Now serving from <b>' + _esc(S.status.vault.provider) + '</b>, but the server booted on <b>' + _esc(S.status.vault.boot_provider || '') + '</b>. The switch is live for new lookups; restart for a clean cutover. '
      + '<button class="smb-btn" data-act="vault-restart">Restart server</button> <span class="smb-restart-status"></span></div>'
    : '';
  return ''
    + '<label class="smb-label">Provider</label>'
    + '<select id="smb-vault-provider" class="smb-input">' + opts + '</select>'
    + '<div class="smb-note">' + _esc(VAULT_NOTE[sel] || '') + '</div>'
    + '<div id="smb-vault-fields" class="smb-fields">' + _vaultFieldsHtml(sel, secrets) + '</div>'
    + '<div class="smb-actions">'
    +   '<button class="smb-btn" data-act="vault-test">Test</button>'
    +   '<button class="smb-btn smb-btn-primary" data-act="vault-activate">Sign in (activate)</button>'
    + '</div>'
    + '<pre id="smb-vault-output" class="smb-output" hidden></pre>'
    + restart;
}

function _vaultFieldsHtml(provider, secrets) {
  const specs = VAULT_FIELDS[provider] || [];
  if (!specs.length) return '<div class="smb-hint">No connection details needed for this vault.</div>';
  const saved = (secrets.configs && secrets.configs[provider]) || {};
  const tokenSaved = secrets.token_saved && secrets.token_saved[provider];
  return specs.map(f => {
    const spec = (f.secret && tokenSaved) ? { ...f, keep: true } : f;
    return _fieldHtml('vault', spec, saved[f.key]);
  }).join('');
}

// ── Linked devices (every machine signed in to the shared database) ──────────
function _platformIcon(platform) {
  const p = (platform || '').toLowerCase();
  if (p.includes('win')) return 'monitor';
  if (p.includes('darwin') || p.includes('mac')) return 'laptop';
  if (p.includes('linux')) return 'terminal';
  return 'cpu';
}
function _openUrl(addr) {
  const a = String(addr || '').trim();
  if (!a) return '';
  return /^https?:\/\//i.test(a) ? a : 'http://' + a;
}
function _repoUrl(repo) {
  const r = String(repo || '').trim();
  if (/^https?:\/\//i.test(r)) return r;
  if (/^[\w.-]+\/[\w.-]+$/.test(r)) return 'https://github.com/' + r.replace(/\.git$/i, '');
  return r;
}

function _devicesHtml() {
  const st = S.status || {};
  const dbShared = !!(st.db && st.db.shared);
  const list = (S.devices && S.devices.devices) || [];
  const online = list.filter(d => d.online).length;
  const meta = list.length ? (online + ' online · ' + list.length + ' total') : '';

  let inner;
  if (S.devices == null) {
    inner = '<div class="smb-hint smb-dev-empty">Loading devices…</div>';
  } else if (!dbShared) {
    inner = '<div class="smb-hint smb-dev-empty">On a local SQLite database there is only this device. '
      + 'Sign in to a shared Postgres / Supabase database above, then run WebAgent on another machine pointed at the same database — it appears here automatically.</div>';
  } else if (!list.length) {
    inner = '<div class="smb-hint smb-dev-empty">No devices seen yet. Any machine running WebAgent against this database appears here automatically.</div>';
  } else {
    inner = list.map(_deviceCardHtml).join('');
  }

  return '<div class="smb-dev">'
    + '<div class="smb-dev-head"><i data-lucide="network"></i><span class="smb-dev-title">Linked devices</span>'
    +   (meta ? '<span class="smb-dev-meta">' + _esc(meta) + '</span>' : '') + '</div>'
    + '<div class="smb-dev-list">' + inner + '</div>'
    + '</div>';
}

function _deviceCardHtml(d) {
  const online = !!d.online;
  const badge = d.is_self ? '<span class="smb-badge smb-badge-shared">This device</span>' : '';
  const meta = [];
  if (d.platform) meta.push('<span class="smb-dev-item"><i data-lucide="' + _platformIcon(d.platform) + '"></i>' + _esc(d.platform) + '</span>');
  if (d.branch) meta.push('<span class="smb-dev-item"><i data-lucide="git-branch"></i>' + _esc(d.branch) + '</span>');
  if (d.repo) meta.push('<span class="smb-dev-item"><i data-lucide="github"></i><a class="smb-dev-link" href="' + _escAttr(_repoUrl(d.repo)) + '" target="_blank" rel="noopener">' + _esc(d.repo) + '</a></span>');
  if (d.endpoint) meta.push('<span class="smb-dev-item"><i data-lucide="link"></i>' + _esc(d.endpoint) + '</span>');
  const open = d.endpoint ? '<a class="smb-btn" href="' + _escAttr(_openUrl(d.endpoint)) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>' : '';
  return '<div class="smb-dev-card' + (d.is_self ? ' smb-dev-self' : '') + '">'
    + '<div class="smb-dev-card-main">'
    +   '<div class="smb-dev-card-head">' + _dot(online, false)
    +     '<span class="smb-dev-name">' + _esc(d.label || d.instance_id) + '</span>' + badge
    +     '<span class="smb-card-status">' + (online ? 'Online' : 'Offline') + '</span></div>'
    +   (meta.length ? '<div class="smb-dev-metas">' + meta.join('') + '</div>' : '')
    + '</div>' + (open ? '<div class="smb-dev-actions">' + open + '</div>' : '') + '</div>';
}

// ── Full render ──────────────────────────────────────────────────────────────
function _render() {
  const el = _root(); if (!el) return;
  el.innerHTML =
      '<div class="smb-section-head">'
    +   '<span class="smb-section-title"><i data-lucide="network"></i> Database &amp; devices</span>'
    +   '<button class="cvm-btn" data-act="check"><i data-lucide="refresh-cw"></i> Check now</button>'
    + '</div>'
    + _degradedBannerHtml()
    + _infoPanelHtml()
    + _dbCardHtml()
    + _vaultCardHtml()
    + _devicesHtml();
  _refreshLucideIcons(el);
}

// ── Load (status + storage config + devices in parallel) ─────────────────────
async function _load() {
  const el = _root(); if (!el) return;
  if (!S.status) el.innerHTML = '<div class="smb-loading"><i data-lucide="loader-2" class="smb-spin"></i> Loading backend status…</div>';
  _refreshLucideIcons(el);
  const [stat, cfg, dev] = await Promise.all([
    _get('/admin/database-devices/backend-status').catch(() => ({ body: null })),
    _get('/admin/storage/config').catch(() => ({ body: null })),
    _get('/admin/database-devices/devices').catch(() => ({ body: { devices: [] } })),
  ]);
  S.status = (stat && stat.body) || S.status || {};
  S.config = (cfg && cfg.body) || S.config || {};
  S.devices = (dev && dev.body) || { devices: [] };
  // A fresh load reflects the now-active providers — drop any pending dropdown
  // choice so the cards show reality (e.g. right after signing in).
  S.sel.db = null; S.sel.vault = null;
  _render();
}

// ── Output box helpers ───────────────────────────────────────────────────────
function _out(id, text, ok) {
  const el = document.getElementById(id); if (!el) return;
  el.hidden = false;
  el.style.color = ok === true ? 'var(--success)' : ok === false ? 'var(--danger)' : '';
  el.textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
}

// Collect DB form fields into the body shape /admin/storage/db/* expects.
function _collectDb() {
  const prov = document.getElementById('smb-db-provider');
  const provider = prov ? prov.value : 'sqlite';
  const o = { provider };
  (DB_FIELDS[provider] || []).forEach(f => {
    const inp = document.querySelector('#smb-db-fields [data-fk="' + (window.CSS && CSS.escape ? CSS.escape(f.key) : f.key) + '"]');
    if (!inp) return;
    let v = inp.value;
    if (f.type === 'number' && v) v = parseInt(v, 10);
    if (v === '') v = null;
    o[f.key] = v;
  });
  return o;
}

// Collect vault form fields into { provider, config, token }.
function _collectVault() {
  const prov = document.getElementById('smb-vault-provider');
  const provider = prov ? prov.value : 'inline_db';
  const out = { config: {}, token: undefined };
  (VAULT_FIELDS[provider] || []).forEach(f => {
    const inp = document.querySelector('#smb-vault-fields [data-fk="' + (window.CSS && CSS.escape ? CSS.escape(f.key) : f.key) + '"]');
    if (!inp) return;
    if (f.secret) { if (inp.value) out.token = inp.value; }
    else out.config[f.key] = (inp.value || '').trim();
  });
  return { provider, ...out };
}

// ── Actions ──────────────────────────────────────────────────────────────────
async function _dbTest() {
  _out('smb-db-output', 'Testing connection…', null);
  const r = await _post('/admin/storage/db/test', _collectDb());
  _out('smb-db-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
}
async function _dbSave() {
  _out('smb-db-output', 'Saving config…', null);
  const r = await _post('/admin/storage/db/config', _collectDb());
  _out('smb-db-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
}
async function _dbSql() {
  const provider = (document.getElementById('smb-db-provider') || {}).value || 'sqlite';
  const dialect = provider === 'sqlite' ? 'sqlite' : 'postgres';
  const r = await _post('/admin/storage/db/schema-sql', { dialect, idempotent: provider !== 'supabase' });
  if (r.body && r.body.ddl) _out('smb-db-output', r.body.ddl, true);
  else _out('smb-db-output', r.body || { error: 'no response' }, false);
}
async function _dbBootstrap() {
  if (!window.confirm('Connect to the configured database and create all WebAgent tables there? Idempotent.')) return;
  _out('smb-db-output', 'Creating tables…', null);
  const r = await _post('/admin/storage/db/bootstrap', _collectDb());
  if (r.body && r.body.needs_manual_sql && r.body.error) { _out('smb-db-output', r.body.error, false); return; }
  _out('smb-db-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
}
async function _dbActivate() {
  if (!window.confirm('Sign in to this database for the LIVE app? New requests use it immediately.')) return;
  _out('smb-db-output', 'Saving config…', null);
  const saved = await _post('/admin/storage/db/config', _collectDb());
  if (!(saved.body && saved.body.ok)) { _out('smb-db-output', saved.body || { error: 'Could not save config.' }, false); return; }
  _out('smb-db-output', 'Activating…', null);
  const r = await _post('/admin/storage/db/activate', {});
  const ok = !!(r.body && r.body.ok);
  if (!ok && r.body && typeof r.body.error === 'string') _out('smb-db-output', r.body.error, false);
  else _out('smb-db-output', r.body || { error: 'no response' }, ok);
  _load();
}

async function _saveVaultCfg() {
  const { provider, config, token } = _collectVault();
  if (!VAULT_FIELDS[provider]) return null;     // local providers have nothing to save
  return _post('/admin/storage/secrets/provider-config', { provider, config, token });
}
async function _vaultTest() {
  const provider = (document.getElementById('smb-vault-provider') || {}).value || 'inline_db';
  const saved = await _saveVaultCfg();
  if (saved && !(saved.body && saved.body.ok)) { _out('smb-vault-output', saved.body || { error: 'could not save details' }, false); return; }
  _out('smb-vault-output', 'Testing…', null);
  const r = await _post('/admin/storage/secrets/test', { provider });
  _out('smb-vault-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  _load();
}
async function _vaultActivate() {
  const provider = (document.getElementById('smb-vault-provider') || {}).value || 'inline_db';
  if (!window.confirm('Sign in to the "' + provider + '" vault?')) return;
  const saved = await _saveVaultCfg();
  if (saved && !(saved.body && saved.body.ok)) { _out('smb-vault-output', saved.body || { error: 'could not save details' }, false); return; }
  _out('smb-vault-output', 'Activating…', null);
  const r = await _post('/admin/storage/secrets/mode', { provider });
  _out('smb-vault-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  _load();
}

const _sleep = (ms) => new Promise(r => setTimeout(r, ms));
function _probeHealth() { return fetch(apiPath('/health'), { cache: 'no-store' }).then(r => r.ok).catch(() => false); }
async function _vaultRestart(btn) {
  const statusEl = btn && btn.parentElement && btn.parentElement.querySelector('.smb-restart-status');
  const set = (t) => { if (statusEl) statusEl.textContent = t; };
  if (btn) { btn.disabled = true; }
  set('Checking…');
  const info = await _get('/admin/storage/server/restart-info');
  if (!info.body || !info.body.auto_restart_available) {
    if (btn) btn.disabled = false;
    set((info.body && info.body.reason) || 'Automatic restart isn\'t available here — restart the server manually.');
    return;
  }
  set('Restarting…');
  const r = await _post('/admin/storage/server/restart', {});
  if (!r.ok) { if (btn) btn.disabled = false; set((r.body && r.body.detail) || 'Restart could not be started.'); return; }
  const downBy = Date.now() + 30000;
  while (Date.now() < downBy) { if (!(await _probeHealth())) break; await _sleep(700); }
  set('Waiting for the server to come back…');
  const upBy = Date.now() + 180000;
  while (Date.now() < upBy) { if (await _probeHealth()) { set('Server restarted ✓'); _load(); return; } await _sleep(1000); }
  if (btn) btn.disabled = false;
  set('Server hasn\'t answered yet — give it a moment, then Check now.');
}

// ── Event wiring (delegated; survives re-render) ─────────────────────────────
function _onClick(e) {
  const toggle = e.target.closest('[data-toggle]');
  if (toggle) { const k = toggle.dataset.toggle; S.open[k] = !S.open[k]; _render(); return; }
  const act = e.target.closest('[data-act]');
  if (!act) return;
  switch (act.dataset.act) {
    case 'check': _load(); return;
    case 'fix-db': {  // banner → open the DB card's sign-in form
      S.open.db = true; _render();
      const card = document.querySelector('.smb-card');
      if (card && card.scrollIntoView) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
    case 'db-test': _dbTest(); return;
    case 'db-save': _dbSave(); return;
    case 'db-sql': _dbSql(); return;
    case 'db-bootstrap': _dbBootstrap(); return;
    case 'db-activate': _dbActivate(); return;
    case 'vault-test': _vaultTest(); return;
    case 'vault-activate': _vaultActivate(); return;
    case 'vault-restart': _vaultRestart(act); return;
  }
}
function _onChange(e) {
  // Provider change → record the choice, then re-render that card's fields +
  // note (the card stays open; S.sel keeps the dropdown on the chosen value).
  if (e.target.id === 'smb-db-provider') { S.sel.db = e.target.value; _render(); return; }
  if (e.target.id === 'smb-vault-provider') { S.sel.vault = e.target.value; _render(); return; }
}

// ── Lifecycle (called by the admin shell: files.js applySidebarView) ─────────
export function startView() {
  const el = _root(); if (!el) return;
  if (!S.wired) {
    S.wired = true;
    el.addEventListener('click', _onClick);
    el.addEventListener('change', _onChange);
    // Sidebar refresh button lives outside #smb-root.
    const r = document.getElementById('dbd-refresh');
    if (r) r.addEventListener('click', _load);
  }
  _load();
}
export function stopView() { /* no polling — nothing to tear down */ }
