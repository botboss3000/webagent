'use strict';

/**
 * Storage UI driver — mounts on App Config → Database tab (#ac-section-database).
 * Element IDs are prefixed "ac-storage-". Talks to /admin/storage/* endpoints.
 */

import { app } from './state.js';
import { apiPath } from './config.js';
import { isAdmin } from './left-login.js';

let PAGE = null;

function qs(id) { return document.getElementById(id); }
function uid() { return app.currentUserId || localStorage.getItem('auth_user_id') || ''; }

function bindMount(prefix) {
  const m = {
    prefix,
    root: qs('ac-section-database'),
    envBanner: qs(`${prefix}env-banner`),
    activeBadge: qs(`${prefix}active-badge`),
    provider: qs(`${prefix}db-provider`),
    fields: qs(`${prefix}db-fields`),
    btnTest: qs(`${prefix}db-test`),
    btnShowSQL: qs(`${prefix}db-show-sql`),
    btnBootstrap: qs(`${prefix}db-bootstrap`),
    btnSave: qs(`${prefix}db-save`),
    btnActivate: qs(`${prefix}db-activate`),
    output: qs(`${prefix}db-output`),
    secretsBadge: qs(`${prefix}secrets-badge`),
    secretsProv: qs(`${prefix}secrets-provider`),
    secretsWarn: qs(`${prefix}secrets-warning`),
    secretsTest: qs(`${prefix}secrets-test`),
    secretsSave: qs(`${prefix}secrets-save`),
    secretsOutput: qs(`${prefix}secrets-output`),
    encBadge: qs(`${prefix}enc-badge`),
    encVaultWarn: qs(`${prefix}enc-vault-warning`),
    encLevel: qs(`${prefix}enc-level`),
    encSave: qs(`${prefix}enc-save`),
    encTest: qs(`${prefix}enc-test`),
    encKekGen: qs(`${prefix}enc-kek-generate`),
    encKekRotate: qs(`${prefix}enc-kek-rotate`),
    encMigrate: qs(`${prefix}enc-migrate`),
    encDecryptAll: qs(`${prefix}enc-decrypt-all`),
    encTenants: qs(`${prefix}enc-tenants`),
    encOutput: qs(`${prefix}enc-output`),
    dbNotes: qs(`${prefix}db-notes`),
    secretsNotes: qs(`${prefix}secrets-notes`),
    encNotes: qs(`${prefix}enc-notes`),
    exportBtn: qs(`${prefix}export-btn`),
    importInput: qs(`${prefix}import-input`),
    migrateOutput: qs(`${prefix}migrate-output`),
  };
  if (!m.root || !m.provider) return null;
  return m;
}

function out(elt, text, ok) {
  if (!elt) return;
  elt.style.display = 'block';
  elt.style.color = ok === true ? '#9ece6a' : ok === false ? '#f7768e' : '';
  elt.textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
}

// ── Trade-off notes shown beneath each selector ────────────────────────────

const GOOD = 'var(--success)';
const BAD = 'var(--danger)';

function notesHtml(pros, cons, extra) {
  const li = (items, color, sign) => items.map(t =>
    `<li style="margin:1px 0;"><span style="color:${color};font-weight:600;">${sign}</span> ${t}</li>`
  ).join('');
  const extraLine = extra ? `<div style="margin-top:6px;opacity:0.75;font-style:italic;">${extra}</div>` : '';
  return `<ul style="margin:0;padding-left:14px;list-style:none;">${li(pros, GOOD, '+')}${li(cons, BAD, '−')}</ul>${extraLine}`;
}

const DB_NOTES = {
  sqlite: notesHtml(
    ['Zero ops, zero latency, zero cost, works offline.', 'Single-file backup (just copy <code>local.db</code>).', 'Only backend that supports the full-DB SQLCipher encryption option.'],
    ['Single host — no horizontal scale or multi-instance deploys.', 'Weak concurrent-write story (one writer at a time).', 'You own backups, retention, and at-rest disk encryption.'],
    'Best for: personal use, dev, embedded/edge deployments.'
  ),
  supabase: notesHtml(
    ['Managed Postgres with backups, PITR, encryption at rest, generous free tier.', 'Survives app-host death; multi-instance deploys share the same DB.', 'Auth/Storage/Realtime available if you adopt more of the Supabase stack later.'],
    ['Network latency on every query (cheap from same region).', 'Egress costs if your app runs outside Supabase.', 'Adds an external dependency and credentials to manage.'],
    'Best for: hosted multi-user deployments where you want batteries-included Postgres.'
  ),
  postgres: notesHtml(
    ['Connect to any Postgres you own — self-hosted, RDS, Azure, on-prem.', 'Strongest write/concurrency story and full SQL feature set.', 'Extensions, replication, logical decoding all available.'],
    ['You operate it: provisioning, backups, upgrades, monitoring.', 'Needs reachable network and credentials.', 'No managed PITR or HA unless you build it.'],
    'Best for: teams with existing Postgres infrastructure and DBA capacity.'
  ),
  gcp_cloud_sql: notesHtml(
    ['Fully managed Postgres with HA, automated backups, PITR, IAM auth.', 'Tight integration if your app runs on Cloud Run or GKE.', 'Private IP / IAM auth removes password-handling burden.'],
    ['Always-on billing — no scale-to-zero, smallest tier still costs monthly.', 'Needs Cloud SQL Proxy or private IP for safe connectivity.', 'Vendor lock-in to GCP networking & IAM.'],
    'Best for: GCP-native deployments where uptime matters more than idle cost.'
  ),
  neon: notesHtml(
    ['Serverless Postgres with scale-to-zero — cheap when idle, autoscales under load.', 'Database branching for preview/test environments.', 'Pure Postgres wire protocol — trivial to migrate to or from.'],
    ['Cold-start latency on the first query after idling.', 'Connection pooling is mandatory for serverless app deployments.', 'Newer vendor — smaller ecosystem and tooling than RDS/Cloud SQL.'],
    'Best for: bursty workloads, preview environments, indie projects watching cost.'
  ),
  mysql: notesHtml(
    ['Familiar engine, hosted everywhere (PlanetScale, RDS, DigitalOcean, etc.).', 'Excellent read scaling with replicas.', 'Mature tooling and operator experience.'],
    ['No JSONB or Postgres-specific FTS features used elsewhere in webAgent.', 'Schema migrations here are tested primarily against SQLite + Postgres — expect minor friction.', 'Some advanced indexing patterns differ from Postgres.'],
    'Best for: orgs already standardised on MySQL.'
  ),
};

const SECRETS_NOTES = {
  inline_db: notesHtml(
    ['Zero setup — works out of the box on any DB backend.'],
    ['Secrets stored as plaintext in <code>auth_elements.secret_ref</code>.', 'Anyone with DB read access sees every OAuth token and API key.', 'Combining this with field-level encryption defeats the purpose: keys and ciphertext live in the same DB.'],
    'Acceptable for local dev only. Upgrade to a real vault before enabling encryption or going to production.'
  ),
  env: notesHtml(
    ['Read-only and inert — secrets injected by your deployment environment.', 'Excellent fit for Docker / Cloud Run / Kubernetes with sealed secrets.', 'Survives container restarts; no app-side write path to compromise.'],
    ['Read-only: the app cannot store new secrets (OAuth callbacks will fail to persist tokens).', 'You manage rotation outside the app.', 'Not suitable as the keystore for the encryption feature, which needs to write per-tenant DEKs.'],
    'Best for: read-only secret injection in immutable deployments.'
  ),
  os_keyring: notesHtml(
    ['Uses the OS credential store: Windows Credential Manager, macOS Keychain, freedesktop Secret Service.', 'Strong single-host security at zero cost.', 'Recommended pairing with SQLite + field-level encryption on a workstation.'],
    ['Tied to one machine — multi-instance deploys cannot share secrets.', 'Requires a logged-in user session on Linux (Secret Service / D-Bus).', 'No audit log, no programmatic rotation.'],
    'Best for: single-host installations on a laptop, workstation, or dedicated server.'
  ),
  gcp_secret_manager: notesHtml(
    ['Managed, audit-logged, IAM-controlled, versioned secrets in GCP.', 'Pairs naturally with Cloud Run / GKE workloads via workload identity.', 'Supports automatic version pinning and rotation policies.'],
    ['Per-secret-version pricing plus per-access cost.', 'Requires GCP credentials wherever the app runs (even if not deployed on GCP).', 'Cross-region latency if your app is outside GCP.'],
    'Best for: GCP-hosted production deployments.'
  ),
  aws_secrets_manager: notesHtml(
    ['Managed, audit-logged, IAM-controlled secrets with built-in rotation Lambdas.', 'Best fit for ECS / Lambda / EC2 deployments via IAM roles.', 'Integrates with CloudTrail for full access auditing.'],
    ['Per-secret monthly cost plus per-API-call cost.', 'Requires AWS credentials wherever the app runs.', 'Rotation Lambdas need to be written per provider.'],
    'Best for: AWS-hosted production deployments.'
  ),
};

const ENC_NOTES = {
  none: notesHtml(
    ['No setup, no key management, full performance.'],
    ['OAuth tokens and API keys are plaintext in the DB.', 'Anyone with DB read access can immediately use your tenants\' credentials.', 'A leaked database backup leaks every secret with it.'],
    'Acceptable only for ephemeral dev. Switch to <strong>field</strong> before going to production.'
  ),
  field: notesHtml(
    ['Per-tenant Fernet encryption on <code>auth_elements.secret_ref</code>.', 'Wrapped DEKs live in the Secrets Vault — never in the application DB.', 'One tenant\'s key compromise affects only that tenant\'s rows.', 'Works on every database backend.'],
    ['Adds ~1ms vault round-trip on the first read per tenant (cached after).', 'Requires a real vault provider (not <strong>inline_db</strong>) — otherwise keys and ciphertext live in the same DB.', 'Schema migration adds a <code>tenant_key_meta</code> table.'],
    'Recommended default for any deployment with real users.'
  ),
  full_db: notesHtml(
    ['Encrypts the entire SQLite file with SQLCipher — every column, every index, every page.', 'Strong defense if the laptop or server disk is lost or stolen.', 'Stack with field-level for both whole-file and per-tenant protection.'],
    ['SQLite only — disabled when the active DB backend is anything else.', 'Requires the optional <code>pysqlcipher3</code> package.', 'Single passphrase — not tenant-isolated on its own.', 'You manage the passphrase storage outside the app.'],
    'Best for: single-host SQLite deployments where physical device loss is in the threat model.'
  ),
  kms: notesHtml(
    ['KEK lives in cloud KMS (GCP / AWS) and never leaves the HSM boundary.', 'Per-tenant DEKs wrapped by KMS; full audit log of every unwrap.', 'Best-practice key management for regulated environments.'],
    ['Not yet implemented — UI placeholder for the next release.', 'Will require KMS API credentials at app startup.', 'Adds per-unwrap KMS cost (small, but non-zero).'],
    'Coming soon. Use <strong>field</strong> with a cloud secret manager today for similar guarantees.'
  ),
};

function renderNotes(elt, map, key) {
  if (!elt) return;
  const html = map[key];
  if (!html) { elt.style.display = 'none'; return; }
  elt.style.display = 'block';
  elt.innerHTML = html;
}

// ── Provider field templates ───────────────────────────────────────────────

const FIELD_SPECS = {
  sqlite: [
    { key: 'database', label: 'DB file path (blank = default)', type: 'text', placeholder: 'app/db/local.db' },
  ],
  supabase: [
    { key: 'supabase_url', label: 'Project URL', type: 'text', placeholder: 'https://xxxx.supabase.co', required: true },
    { key: 'supabase_service_key', label: 'Service role key', type: 'password', placeholder: 'eyJhbGciOi...' },
  ],
  postgres: [
    { key: 'host', label: 'Host', type: 'text', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432' },
    { key: 'database', label: 'Database name', type: 'text', required: true },
    { key: 'username', label: 'Username', type: 'text', required: true },
    { key: 'password', label: 'Password', type: 'password' },
    { key: 'ssl_mode', label: 'SSL mode', type: 'select', options: ['disable','require','verify-ca','verify-full'] },
  ],
  gcp_cloud_sql: [
    { key: 'host', label: 'Public IP / Cloud SQL proxy host', type: 'text', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432' },
    { key: 'database', label: 'Database name', type: 'text', required: true },
    { key: 'username', label: 'Username', type: 'text', required: true },
    { key: 'password', label: 'Password', type: 'password' },
  ],
  neon: [
    { key: 'host', label: 'Endpoint (e.g. ep-xxxx.neon.tech)', type: 'text', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432' },
    { key: 'database', label: 'Database name', type: 'text', required: true },
    { key: 'username', label: 'Username', type: 'text', required: true },
    { key: 'password', label: 'Password', type: 'password' },
  ],
  mysql: [
    { key: 'host', label: 'Host', type: 'text', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '3306' },
    { key: 'database', label: 'Database name', type: 'text', required: true },
    { key: 'username', label: 'Username', type: 'text', required: true },
    { key: 'password', label: 'Password', type: 'password' },
  ],
};

function renderFields(m, provider, existing) {
  if (!m || !m.fields) return;
  m.fields.innerHTML = '';
  const specs = FIELD_SPECS[provider] || [];
  for (const spec of specs) {
    const wrap = document.createElement('div');
    wrap.style.gridColumn = (spec.type === 'number' || spec.key === 'ssl_mode') ? 'span 1' : 'span 2';
    const lbl = document.createElement('label');
    lbl.textContent = spec.label + (spec.required ? ' *' : '');
    lbl.style.cssText = 'display:block;font-size:11px;margin-bottom:2px;opacity:0.8;';
    wrap.appendChild(lbl);
    let input;
    if (spec.type === 'select') {
      input = document.createElement('select');
      for (const opt of spec.options) {
        const o = document.createElement('option');
        o.value = opt; o.textContent = opt;
        input.appendChild(o);
      }
    } else {
      input = document.createElement('input');
      input.type = spec.type;
      if (spec.placeholder) input.placeholder = spec.placeholder;
    }
    input.dataset.fieldKey = spec.key;
    input.style.cssText = 'width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-size:12px;background:transparent;color:inherit;';
    if (existing && existing[spec.key] != null) input.value = existing[spec.key];
    wrap.appendChild(input);
    m.fields.appendChild(wrap);
  }
}

function collectFields(m) {
  if (!m) return {};
  const provider = m.provider.value;
  const specs = FIELD_SPECS[provider] || [];
  const o = { provider };
  for (const spec of specs) {
    const el = m.fields.querySelector(`[data-field-key="${spec.key}"]`);
    if (!el) continue;
    let v = el.value;
    if (spec.type === 'number' && v) v = parseInt(v, 10);
    if (v === '') v = null;
    o[spec.key] = v;
  }
  return o;
}

// ── API ─────────────────────────────────────────────────────────────────────

async function loadConfig(m) {
  const u = uid();
  const res = await fetch(apiPath(`/admin/storage/config?requesting_user_id=${encodeURIComponent(u)}`));
  if (!res.ok) {
    out(m.output, `Failed to load config (status ${res.status}). Are you signed in as an admin?`, false);
    return null;
  }
  return res.json();
}

function applyState(m, state) {
  if (!state) return;
  m.envBanner.style.display = state.env_locked ? 'block' : 'none';
  const dbCfg = state.db.active || {};
  m.provider.value = dbCfg.provider || 'sqlite';
  renderFields(m, m.provider.value, dbCfg);
  renderNotes(m.dbNotes, DB_NOTES, m.provider.value);
  m.activeBadge.textContent = `active: ${dbCfg.provider || 'sqlite'}`;
  const sec = state.secrets || {};
  m.secretsProv.value = sec.provider || 'inline_db';
  renderNotes(m.secretsNotes, SECRETS_NOTES, m.secretsProv.value);
  m.secretsBadge.textContent = `active: ${sec.provider || 'inline_db'}`;
  m.secretsWarn.style.display = (sec.provider === 'inline_db') ? 'block' : 'none';
  const lock = !!state.env_locked;
  for (const btn of [m.btnSave, m.btnActivate, m.secretsSave, m.encSave, m.encKekGen, m.encKekRotate, m.encMigrate, m.encDecryptAll]) {
    if (btn) { btn.disabled = lock; btn.style.opacity = lock ? '0.45' : '1'; }
  }
}

async function loadEncryption(m) {
  if (!m || !m.encLevel) return;
  const u = uid();
  const res = await fetch(apiPath(`/admin/storage/encryption/config?requesting_user_id=${encodeURIComponent(u)}`));
  if (!res.ok) {
    if (m.encBadge) m.encBadge.textContent = `error ${res.status}`;
    return;
  }
  const s = await res.json();
  m.encLevel.value = s.level || 'none';
  renderNotes(m.encNotes, ENC_NOTES, m.encLevel.value);
  m.encBadge.textContent = `active: ${s.level || 'none'}`;
  m.encVaultWarn.style.display = s.warning ? 'block' : 'none';
  if (s.warning) m.encVaultWarn.textContent = s.warning;
  // Disable full_db when DB provider isn't sqlite
  const fullDbOpt = m.encLevel.querySelector('option[value="full_db"]');
  if (fullDbOpt) {
    const dbProv = (s.db_provider || 'sqlite').toLowerCase();
    fullDbOpt.disabled = (dbProv !== 'sqlite');
  }
}

async function loadTenants(m) {
  if (!m || !m.encTenants) return;
  const u = uid();
  const res = await fetch(apiPath(`/admin/storage/encryption/tenants?requesting_user_id=${encodeURIComponent(u)}`));
  if (!res.ok) return;
  const body = await res.json();
  const tenants = body.tenants || [];
  if (!tenants.length) {
    m.encTenants.style.display = 'block';
    m.encTenants.innerHTML = '<div style="opacity:0.6;">No tenants with key material yet. Run "Encrypt Existing Rows" to provision DEKs.</div>';
    return;
  }
  m.encTenants.style.display = 'block';
  m.encTenants.innerHTML = '';
  for (const t of tenants) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border);';
    const label = document.createElement('span');
    label.style.cssText = 'flex:1;font-family:monospace;';
    label.textContent = `${t.user_id} — active v${t.active_version ?? '–'} (${t.total_versions} versions)`;
    const btn = document.createElement('button');
    btn.className = 'ac-btn';
    btn.style.cssText = 'padding:2px 8px;font-size:10px;';
    btn.textContent = 'Rotate DEK';
    btn.onclick = async () => {
      if (!confirm(`Rotate DEK for user "${t.user_id}"? This re-encrypts all of their rows.`)) return;
      out(m.encOutput, `Rotating DEK for ${t.user_id}...`, null);
      const r = await call('/admin/storage/encryption/dek/rotate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: uid(), user_id: t.user_id }),
      });
      out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
      loadTenants(m);
    };
    row.appendChild(label);
    row.appendChild(btn);
    m.encTenants.appendChild(row);
  }
}

async function call(path, opts = {}) {
  const res = await fetch(apiPath(path), opts);
  let body = null;
  try { body = await res.json(); } catch {}
  return { ok: res.ok, status: res.status, body };
}

function buildBody(m, extra) {
  return JSON.stringify({ requesting_user_id: uid(), ...collectFields(m), ...(extra || {}) });
}

// ── Handlers ────────────────────────────────────────────────────────────────

function wire(m) {
  if (!m) return;

  m.provider.addEventListener('change', () => {
    renderFields(m, m.provider.value, null);
    renderNotes(m.dbNotes, DB_NOTES, m.provider.value);
  });

  if (m.secretsProv) {
    m.secretsProv.addEventListener('change', () => {
      renderNotes(m.secretsNotes, SECRETS_NOTES, m.secretsProv.value);
    });
  }

  if (m.encLevel) {
    m.encLevel.addEventListener('change', () => {
      renderNotes(m.encNotes, ENC_NOTES, m.encLevel.value);
    });
  }

  m.btnTest && m.btnTest.addEventListener('click', async () => {
    out(m.output, 'Testing connection...', null);
    const r = await call('/admin/storage/db/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: buildBody(m),
    });
    out(m.output, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.btnShowSQL && m.btnShowSQL.addEventListener('click', async () => {
    const provider = m.provider.value;
    const dialect = provider === 'mysql' ? 'mysql' : provider === 'sqlite' ? 'sqlite' : 'postgres';
    const r = await call('/admin/storage/db/schema-sql', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), dialect }),
    });
    if (r.body && r.body.ddl) out(m.output, r.body.ddl, true);
    else out(m.output, r.body || { error: 'no response' }, false);
  });

  m.btnBootstrap && m.btnBootstrap.addEventListener('click', async () => {
    if (!confirm('Connect to the configured DB and create all webAgent tables there? Idempotent.')) return;
    out(m.output, 'Bootstrapping schema...', null);
    const r = await call('/admin/storage/db/bootstrap', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: buildBody(m),
    });
    out(m.output, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.btnSave && m.btnSave.addEventListener('click', async () => {
    out(m.output, 'Saving config...', null);
    const r = await call('/admin/storage/db/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: buildBody(m),
    });
    out(m.output, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.btnActivate && m.btnActivate.addEventListener('click', async () => {
    if (!confirm('Switch live database backend to this provider? In-flight requests will use the new backend on their next call.')) return;
    out(m.output, 'Activating...', null);
    const r = await call('/admin/storage/db/activate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    out(m.output, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
    loadConfig(m).then(s => applyState(m, s));
  });

  m.secretsTest && m.secretsTest.addEventListener('click', async () => {
    const r = await call(`/admin/storage/secrets/test?requesting_user_id=${encodeURIComponent(uid())}`, { method: 'POST' });
    out(m.secretsOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.secretsSave && m.secretsSave.addEventListener('click', async () => {
    if (!confirm(`Switch secrets vault to "${m.secretsProv.value}"?`)) return;
    const r = await call('/admin/storage/secrets/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), provider: m.secretsProv.value }),
    });
    out(m.secretsOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
    m.secretsWarn.style.display = (m.secretsProv.value === 'inline_db') ? 'block' : 'none';
    m.secretsBadge.textContent = `active: ${m.secretsProv.value}`;
  });

  m.exportBtn && m.exportBtn.addEventListener('click', async () => {
    out(m.migrateOutput, 'Exporting...', null);
    const u = encodeURIComponent(uid());
    const res = await fetch(apiPath(`/admin/storage/migrate/export?requesting_user_id=${u}`), { method: 'POST' });
    if (!res.ok) { out(m.migrateOutput, `Export failed (status ${res.status})`, false); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `webagent-export-${Date.now()}.json`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    out(m.migrateOutput, 'Exported. File downloaded.', true);
  });

  m.encSave && m.encSave.addEventListener('click', async () => {
    const level = m.encLevel.value;
    let confirmFlag = false;
    if (level === 'none') {
      if (!confirm('Switch to "none"? You must first run "Decrypt Back to Plaintext" or rows will become unreadable through the decorator path.')) return;
      confirmFlag = true;
    } else {
      if (!confirm(`Activate encryption level "${level}"? Existing plaintext rows stay plaintext until you click "Encrypt Existing Rows".`)) return;
    }
    out(m.encOutput, 'Activating level...', null);
    const r = await call('/admin/storage/encryption/level', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), level, confirm: confirmFlag }),
    });
    out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
    loadEncryption(m);
  });

  m.encTest && m.encTest.addEventListener('click', async () => {
    out(m.encOutput, 'Running round-trip probe...', null);
    const r = await call(`/admin/storage/encryption/test?requesting_user_id=${encodeURIComponent(uid())}`);
    out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.encKekGen && m.encKekGen.addEventListener('click', async () => {
    if (!confirm('Generate a new KEK and store it in the active secrets vault? Fails if one already exists — use Rotate KEK for replacements.')) return;
    out(m.encOutput, 'Generating KEK...', null);
    const r = await call('/admin/storage/encryption/kek/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.encKekRotate && m.encKekRotate.addEventListener('click', async () => {
    if (!confirm('Rotate KEK? Re-wraps every per-tenant DEK in the vault. Row data is not touched.')) return;
    out(m.encOutput, 'Rotating KEK...', null);
    const r = await call('/admin/storage/encryption/kek/rotate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.encMigrate && m.encMigrate.addEventListener('click', async () => {
    if (!confirm('Encrypt all plaintext sensitive rows under the active encryption level? Idempotent.')) return;
    out(m.encOutput, 'Encrypting existing rows...', null);
    const r = await call('/admin/storage/encryption/migrate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
    loadTenants(m);
  });

  m.encDecryptAll && m.encDecryptAll.addEventListener('click', async () => {
    if (!confirm('Decrypt every encrypted row back to plaintext? Required before switching the level to "none" or exporting.')) return;
    out(m.encOutput, 'Decrypting all rows...', null);
    const r = await call('/admin/storage/encryption/decrypt-all', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid() }),
    });
    out(m.encOutput, r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  });

  m.importInput && m.importInput.addEventListener('change', async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    if (!confirm(`Import ${file.name} into active backend? Rows inserted (skipped on conflict).`)) return;
    out(m.migrateOutput, 'Reading file...', null);
    const text = await file.text();
    let payload;
    try { payload = JSON.parse(text); } catch (e) { out(m.migrateOutput, `Invalid JSON: ${e.message}`, false); return; }
    out(m.migrateOutput, 'Importing...', null);
    const u = encodeURIComponent(uid());
    const res = await fetch(apiPath(`/admin/storage/migrate/import?requesting_user_id=${u}`), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    let body = null;
    try { body = await res.json(); } catch {}
    out(m.migrateOutput, body || { error: 'no response' }, res.ok);
    ev.target.value = '';
  });
}

// ── Stream Buffer (in-memory event replay window) ──────────────────────────

async function loadStreamBufferSetting() {
  const sbufInput = qs('ac-storage-sbuf-seconds');
  const sbufStatus = qs('ac-storage-sbuf-status');
  const sbufBadge = qs('ac-storage-sbuf-badge');
  if (!sbufInput) return;
  try {
    const r = await fetch(apiPath('/admin/settings/app'));
    if (!r.ok) {
      if (sbufStatus) sbufStatus.textContent = `Load failed: ${r.status}`;
      return;
    }
    const data = await r.json();
    const sec = (typeof data.stream_buffer_retention_seconds === 'number')
      ? data.stream_buffer_retention_seconds
      : 60;
    sbufInput.value = sec;
    if (sbufBadge) sbufBadge.textContent = `active: ${sec}s`;
  } catch (e) {
    if (sbufStatus) sbufStatus.textContent = 'Load failed';
  }
}

function wireStreamBuffer() {
  const sbufInput = qs('ac-storage-sbuf-seconds');
  const sbufSave = qs('ac-storage-sbuf-save');
  const sbufStatus = qs('ac-storage-sbuf-status');
  const sbufBadge = qs('ac-storage-sbuf-badge');
  if (!sbufSave || !sbufInput) return;

  sbufSave.addEventListener('click', async () => {
    let val = parseInt(sbufInput.value, 10);
    if (isNaN(val) || val < 0) val = 0;
    if (val > 3600) val = 3600;
    sbufInput.value = val;
    if (sbufStatus) sbufStatus.textContent = 'Saving…';
    try {
      // Read current settings first so we don't clobber siblings like access_mode.
      const cur = await fetch(apiPath('/admin/settings/app'));
      const curData = cur.ok ? await cur.json() : {};
      curData.stream_buffer_retention_seconds = val;
      const r = await fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(curData),
      });
      if (!r.ok) {
        if (sbufStatus) sbufStatus.textContent = `Save failed: ${r.status}`;
        return;
      }
      const saved = await r.json();
      const ssec = (typeof saved.stream_buffer_retention_seconds === 'number')
        ? saved.stream_buffer_retention_seconds
        : val;
      if (sbufStatus) sbufStatus.textContent = `Saved (${ssec}s)`;
      if (sbufBadge) sbufBadge.textContent = `active: ${ssec}s`;
    } catch (e) {
      if (sbufStatus) sbufStatus.textContent = `Save failed: ${e.message || e}`;
    }
  });
}

// ── Public entry ────────────────────────────────────────────────────────────

// ── Agent Prompt Templates panel ───────────────────────────────────────────
//
// Renders #ac-tpl-* widgets in the Storage section. Talks to
// /admin/db/templates (GET) and /admin/db/templates/seed[-force] (POST).

function _esc(s) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(s == null ? '' : String(s)));
  return d.innerHTML;
}

async function loadTemplatePanel() {
  const listEl   = qs('ac-tpl-list');
  const badgeEl  = qs('ac-tpl-sync-badge');
  const hashRow  = qs('ac-tpl-hash-row');
  if (!listEl) return;
  try {
    const r = await fetch(apiPath('/admin/db/templates'));
    if (!r.ok) {
      listEl.innerHTML = `<div class="ac-hint" style="color:var(--danger);">Failed: HTTP ${r.status}</div>`;
      if (badgeEl) { badgeEl.textContent = 'error'; badgeEl.style.color = 'var(--danger)'; }
      return;
    }
    const data = await r.json();
    if (badgeEl) {
      badgeEl.textContent = data.in_sync ? 'in sync with JSON' : 'JSON changed — re-seed pending';
      badgeEl.style.color = data.in_sync ? 'var(--success)' : '#e0af68';
    }
    if (hashRow) {
      const cur = (data.current_manifest_hash || '').slice(0, 12);
      const sto = (data.stored_manifest_hash  || '').slice(0, 12) || '(none)';
      hashRow.textContent = `JSON manifest: ${cur} · stored: ${sto}`;
    }
    const templates = data.templates || [];
    if (!templates.length) {
      listEl.innerHTML = `<div class="ac-hint">No templates seeded yet — click Re-Seed.</div>`;
      return;
    }
    listEl.innerHTML = templates.map(t => {
      const adminBadge = t.admin_edited_slots > 0
        ? `<span style="background:#bb9af7;color:var(--bg-0);font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px;">${t.admin_edited_slots} admin-edited</span>`
        : '';
      const slotPills = (t.slots || []).map(s => {
        const color = s.source === 'admin' ? '#bb9af7' : 'var(--muted)';
        return `<span style="font-size:10px;color:${color};margin-right:8px;">${_esc(s.slot_name)} v${s.version}</span>`;
      }).join('');
      return `
        <div style="border:1px solid var(--border);border-radius:6px;padding:8px 10px;background:var(--bg-elev);">
          <div style="display:flex;align-items:center;gap:8px;">
            <strong style="font-size:12px;">${_esc(t.name)}</strong>
            <span style="font-size:10px;color:var(--muted);">${_esc(t.id)}</span>
            ${adminBadge}
            <span style="margin-left:auto;font-size:10px;color:var(--muted);">${t.slot_count} slots · v${t.min_version}${t.min_version !== t.max_version ? '–v' + t.max_version : ''}</span>
          </div>
          <div style="margin-top:6px;">${slotPills}</div>
        </div>`;
    }).join('');
  } catch (e) {
    listEl.innerHTML = `<div class="ac-hint" style="color:var(--danger);">Load failed: ${_esc(e.message || e)}</div>`;
    if (badgeEl) { badgeEl.textContent = 'error'; badgeEl.style.color = 'var(--danger)'; }
  }
}

async function runTemplateSeed(force) {
  const outEl = qs('ac-tpl-output');
  if (force) {
    const ok = window.confirm(
      'Force Re-Seed will OVERWRITE any agent_prompt_templates rows that were edited via the admin UI ' +
      '(source = admin). Their content + version will be reset to the on-disk JSON values.\n\n' +
      'Continue?'
    );
    if (!ok) return;
  }
  try {
    const r = await fetch(apiPath(force ? '/admin/db/templates/seed-force' : '/admin/db/templates/seed'), {
      method: 'POST',
    });
    const body = await r.json().catch(() => ({}));
    if (outEl) {
      outEl.style.display = 'block';
      outEl.style.color = r.ok ? '' : 'var(--danger)';
      outEl.textContent = JSON.stringify(body, null, 2);
    }
  } catch (e) {
    if (outEl) {
      outEl.style.display = 'block';
      outEl.style.color = 'var(--danger)';
      outEl.textContent = `Request failed: ${e.message || e}`;
    }
  }
  await loadTemplatePanel();
}

function wireTemplatePanel() {
  const refresh = qs('ac-tpl-refresh-btn');
  const seed    = qs('ac-tpl-seed-btn');
  const force   = qs('ac-tpl-seed-force-btn');
  if (refresh) refresh.addEventListener('click', () => loadTemplatePanel());
  if (seed)    seed.addEventListener('click',    () => runTemplateSeed(false));
  if (force)   force.addEventListener('click',   () => runTemplateSeed(true));
}


// ── Public entry ────────────────────────────────────────────────────────────

export function initStorageUi() {
  PAGE = bindMount('ac-storage-');
  wire(PAGE);
  wireStreamBuffer();
  wireTemplatePanel();

  if (PAGE) {
    renderNotes(PAGE.dbNotes, DB_NOTES, PAGE.provider && PAGE.provider.value);
    renderNotes(PAGE.secretsNotes, SECRETS_NOTES, PAGE.secretsProv && PAGE.secretsProv.value);
    renderNotes(PAGE.encNotes, ENC_NOTES, PAGE.encLevel && PAGE.encLevel.value);
  }

  if (PAGE && PAGE.root) {
    window.__refreshStorageSection = () => {
      if (PAGE && isAdmin()) {
        loadConfig(PAGE).then(s => applyState(PAGE, s));
        loadEncryption(PAGE);
        loadTenants(PAGE);
      }
      loadStreamBufferSetting();
      loadTemplatePanel();
    };
    setTimeout(() => { try { window.__refreshStorageSection(); } catch {} }, 200);
  }
}
