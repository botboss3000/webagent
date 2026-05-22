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
    input.style.cssText = 'width:100%;padding:6px 8px;border:1px solid var(--border,#2a2a4a);border-radius:4px;font-size:12px;background:transparent;color:inherit;';
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
  m.activeBadge.textContent = `active: ${dbCfg.provider || 'sqlite'}`;
  const sec = state.secrets || {};
  m.secretsProv.value = sec.provider || 'inline_db';
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
    row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border,#2a2a4a);';
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

  m.provider.addEventListener('change', () => renderFields(m, m.provider.value, null));

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

// ── Public entry ────────────────────────────────────────────────────────────

export function initStorageUi() {
  PAGE = bindMount('ac-storage-');
  wire(PAGE);

  if (PAGE && PAGE.root) {
    window.__refreshStorageSection = () => {
      if (PAGE && isAdmin()) {
        loadConfig(PAGE).then(s => applyState(PAGE, s));
        loadEncryption(PAGE);
        loadTenants(PAGE);
      }
    };
    setTimeout(() => { try { window.__refreshStorageSection(); } catch {} }, 200);
  }
}
