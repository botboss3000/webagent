'use strict';

/**
 * Data Management page — Tables / Storage tab driver.
 *
 * Wires the sidebar-header Tables/Storage tab strip and renders the Storage
 * pane (backend selector + provider config + test button) by talking to
 * /admin/storage/attachments/*. Tables pane already works via the relocated
 * legacy DB viewer; this module only owns Storage and the tab switcher.
 */

import { apiPath } from './config.js';
import { icon } from './icons.js';
import { isAdmin } from './left-login.js';

let _statusCache = null;
let _renderedFor = null; // last provider rendered as the selected detail pane

const PROVIDER_META = {
  local: {
    label: 'Local filesystem',
    blurb: 'Files saved to UPLOAD_DIR on the server (default uploads/). Served via /uploads.',
    fields: [
      { key: 'upload_dir', label: 'Upload directory', placeholder: 'uploads',
        help: 'Optional absolute path. Leave blank to use the UPLOAD_DIR env var or default ./uploads.' },
    ],
  },
  browser: {
    label: 'Browser (IndexedDB)',
    blurb: 'Bytes live in the user\'s browser via IndexedDB. The server records only the row metadata. Server-side tools (read_attachment, agent vision) cannot read bytes when this backend is active. Files do not follow users across devices.',
    fields: [],
  },
  supabase: {
    label: 'Supabase Storage',
    blurb: 'Uses the Supabase project from your DB settings. SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in the environment.',
    fields: [
      { key: 'bucket', label: 'Bucket name', placeholder: 'attachments', required: true },
      { key: 'public', label: 'Public bucket', type: 'checkbox',
        help: 'Public URL if checked; otherwise a 7-day signed URL.' },
    ],
  },
  s3: {
    label: 'AWS S3 (or S3-compatible)',
    blurb: 'Requires boto3 installed and AWS credentials from env/IAM. S3_ENDPOINT_URL lets you point at MinIO / Cloudflare R2 / etc.',
    fields: [
      { key: 'bucket', label: 'Bucket', placeholder: 'my-webagent-uploads', required: true },
      { key: 'region', label: 'Region', placeholder: 'us-east-1' },
      { key: 'prefix', label: 'Key prefix', placeholder: 'attachments/' },
      { key: 'endpoint_url', label: 'Endpoint URL', placeholder: 'https://… (optional)' },
    ],
  },
  gcs: {
    label: 'Google Cloud Storage',
    blurb: 'Requires google-cloud-storage installed and GOOGLE_APPLICATION_CREDENTIALS (or run inside GCP with default ADC).',
    fields: [
      { key: 'bucket', label: 'Bucket', placeholder: 'my-webagent-uploads', required: true },
      { key: 'prefix', label: 'Object prefix', placeholder: 'attachments/' },
    ],
  },
};

function _qs(id) { return document.getElementById(id); }
function _userId() {
  try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; }
}
function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Tab strip ──────────────────────────────────────────────────────────────

function _initTabs() {
  const tabs = document.querySelectorAll('#files-panel-database .dm-sb-tab');
  if (!tabs.length) return;
  tabs.forEach(t => {
    if (t.dataset.dmWired) return;
    t.dataset.dmWired = '1';
    t.addEventListener('click', () => _switchTab(t.dataset.dmTab));
  });
}

function _switchTab(name) {
  document.querySelectorAll('#files-panel-database .dm-sb-tab').forEach(t => {
    const active = t.dataset.dmTab === name;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('#files-panel-database .dm-sb-pane').forEach(p => {
    p.hidden = p.dataset.dmPane !== name;
  });
  document.querySelectorAll('#files-database-main .dm-main-pane').forEach(p => {
    p.hidden = p.dataset.dmPane !== name;
  });
  if (name === 'storage') _loadStorage();
}

// ── Storage tab content ────────────────────────────────────────────────────

function _ensureStorageMainShell() {
  const main = _qs('dm-main-storage');
  if (!main) return null;
  if (main.dataset.dmShellReady === '1') return main;
  main.innerHTML = `
    <div class="dm-storage-section" id="dm-storage-overview">
      <div class="dm-storage-banner" id="dm-storage-banner" hidden></div>
      <h3>Active backend</h3>
      <div class="dm-helptext">
        Where chat attachments (images, voice, dropped files) are stored. Switch any time —
        existing files keep working because each attachment row remembers which backend
        wrote it.
      </div>
      <div class="dm-storage-row">
        <label for="dm-storage-mode">Backend</label>
        <select id="dm-storage-mode"></select>
        <button class="dm-storage-btn primary" id="dm-storage-save">Save</button>
        <button class="dm-storage-btn" id="dm-storage-test">Test</button>
        <span class="dm-storage-status" id="dm-storage-status"></span>
      </div>
    </div>
    <div class="dm-storage-section" id="dm-storage-detail">
      <h3 id="dm-storage-detail-title">Provider configuration</h3>
      <div class="dm-helptext" id="dm-storage-detail-blurb"></div>
      <div id="dm-storage-fields"></div>
      <div class="dm-storage-actions">
        <button class="dm-storage-btn primary" id="dm-storage-save-cfg">Save provider settings</button>
        <span class="dm-storage-status" id="dm-storage-cfg-status"></span>
      </div>
    </div>
  `;
  main.dataset.dmShellReady = '1';
  // Wire once
  _qs('dm-storage-save')?.addEventListener('click', _saveMode);
  _qs('dm-storage-test')?.addEventListener('click', _testBackend);
  _qs('dm-storage-save-cfg')?.addEventListener('click', _saveProviderConfig);
  return main;
}

async function _loadStorage() {
  _ensureStorageMainShell();
  const status = _qs('dm-storage-status');
  if (status) { status.textContent = 'Loading…'; status.className = 'dm-storage-status'; }
  try {
    const res = await fetch(apiPath('/admin/storage/attachments/status?requesting_user_id=' + encodeURIComponent(_userId())));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    _statusCache = await res.json();
  } catch (e) {
    if (status) { status.textContent = 'Could not load: ' + e.message; status.className = 'dm-storage-status err'; }
    return;
  }
  if (status) status.textContent = '';
  _renderOverview();
  _renderSidebarList();
  _renderProviderDetail(_renderedFor || _statusCache.mode);
}

function _renderOverview() {
  const sel = _qs('dm-storage-mode');
  if (!sel) return;
  sel.innerHTML = '';
  (_statusCache?.available || []).forEach(m => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = (PROVIDER_META[m]?.label || m) + (m === _statusCache.mode ? ' (active)' : '');
    sel.appendChild(opt);
  });
  sel.value = _statusCache?.mode || 'local';

  const banner = _qs('dm-storage-banner');
  if (banner) {
    if (_statusCache?.env_locked) {
      banner.hidden = false;
      banner.classList.add('warn');
      banner.textContent =
        'Config is env-locked (WEBAGENT_CONFIG_SOURCE=env or WEBAGENT_ATTACHMENTS_STORE set). ' +
        'Backend can be inspected but not changed from this UI.';
    } else {
      banner.hidden = true;
    }
  }

  const counts = _statusCache?.counts || {};
  const nonZero = Object.entries(counts).filter(([, n]) => n > 0);
  if (nonZero.length > 1) {
    const lines = nonZero.map(([k, n]) => `${PROVIDER_META[k]?.label || k}: ${n}`).join(' · ');
    let chip = _qs('dm-storage-counts-chip');
    if (!chip) {
      chip = document.createElement('div');
      chip.id = 'dm-storage-counts-chip';
      chip.className = 'dm-helptext';
      chip.style.marginTop = '8px';
      _qs('dm-storage-overview')?.appendChild(chip);
    }
    chip.innerHTML = 'Existing attachments across backends — ' + _esc(lines);
  }

  const lockedActions = _statusCache?.env_locked;
  ['dm-storage-save', 'dm-storage-save-cfg'].forEach(id => {
    const btn = _qs(id);
    if (btn) btn.disabled = !!lockedActions;
  });
  // Active indicator chip in the sidebar
  const active = _qs('dm-storage-active');
  if (active) active.textContent = PROVIDER_META[_statusCache?.mode]?.label || _statusCache?.mode || '—';
}

function _renderSidebarList() {
  const ul = _qs('dm-storage-sb-list');
  if (!ul) return;
  ul.innerHTML = '';
  const active = _statusCache?.mode;
  const counts = _statusCache?.counts || {};
  (_statusCache?.available || []).forEach(m => {
    const li = document.createElement('li');
    if (m === (_renderedFor || active)) li.classList.add('active');
    const label = document.createElement('span');
    label.textContent = PROVIDER_META[m]?.label || m;
    li.appendChild(label);
    const n = counts[m] || 0;
    if (n > 0) {
      const badge = document.createElement('span');
      badge.className = 'dm-prov-count';
      badge.textContent = String(n);
      li.appendChild(badge);
    } else if (m === active) {
      const badge = document.createElement('span');
      badge.className = 'dm-prov-count';
      badge.textContent = 'active';
      li.appendChild(badge);
    }
    li.addEventListener('click', () => {
      _renderProviderDetail(m);
      _renderSidebarList();
    });
    ul.appendChild(li);
  });
}

function _renderProviderDetail(provider) {
  _renderedFor = provider;
  const meta = PROVIDER_META[provider];
  const title = _qs('dm-storage-detail-title');
  const blurb = _qs('dm-storage-detail-blurb');
  const fields = _qs('dm-storage-fields');
  const saveBtn = _qs('dm-storage-save-cfg');
  if (!meta || !title || !blurb || !fields) return;
  title.textContent = meta.label + ' settings';
  blurb.textContent = meta.blurb;
  fields.innerHTML = '';
  const cfg = (_statusCache?.providers || {})[provider] || {};

  if (!meta.fields.length) {
    fields.innerHTML = '<div class="dm-helptext">No additional configuration.</div>';
    if (saveBtn) saveBtn.disabled = true;
  } else {
    if (saveBtn) saveBtn.disabled = !!_statusCache?.env_locked;
    meta.fields.forEach(f => {
      const row = document.createElement('div');
      row.className = 'dm-storage-row';
      const lab = document.createElement('label');
      lab.textContent = f.label + (f.required ? ' *' : '');
      lab.setAttribute('for', `dm-fld-${provider}-${f.key}`);
      row.appendChild(lab);
      let inp;
      if (f.type === 'checkbox') {
        inp = document.createElement('input');
        inp.type = 'checkbox';
        inp.checked = cfg[f.key] !== undefined ? !!cfg[f.key] : true;
      } else {
        inp = document.createElement('input');
        inp.type = 'text';
        inp.placeholder = f.placeholder || '';
        inp.value = cfg[f.key] == null ? '' : String(cfg[f.key]);
      }
      inp.id = `dm-fld-${provider}-${f.key}`;
      inp.dataset.provider = provider;
      inp.dataset.key = f.key;
      if (f.type === 'checkbox') inp.dataset.kind = 'bool';
      inp.disabled = !!_statusCache?.env_locked;
      row.appendChild(inp);
      if (f.help) {
        const help = document.createElement('span');
        help.className = 'dm-helptext';
        help.style.fontSize = '11px';
        help.style.marginLeft = '4px';
        help.textContent = f.help;
        row.appendChild(help);
      }
      fields.appendChild(row);
    });
  }
}

// ── Actions ────────────────────────────────────────────────────────────────

async function _saveMode() {
  if (!isAdmin()) return;
  const sel = _qs('dm-storage-mode');
  const status = _qs('dm-storage-status');
  if (!sel) return;
  const mode = sel.value;
  if (status) { status.textContent = 'Saving…'; status.className = 'dm-storage-status'; }
  try {
    const res = await fetch(apiPath('/admin/storage/attachments/mode'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: _userId(), mode }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    if (status) { status.textContent = 'Active backend → ' + (PROVIDER_META[mode]?.label || mode); status.className = 'dm-storage-status ok'; }
    await _loadStorage();
    setTimeout(() => { if (status && status.classList.contains('ok')) status.textContent = ''; }, 3000);
  } catch (e) {
    if (status) { status.textContent = 'Error: ' + e.message; status.className = 'dm-storage-status err'; }
  }
}

async function _saveProviderConfig() {
  if (!isAdmin()) return;
  const status = _qs('dm-storage-cfg-status');
  if (!_renderedFor) return;
  const provider = _renderedFor;
  const cfg = {};
  document.querySelectorAll(`#dm-storage-fields [data-provider="${provider}"]`).forEach(inp => {
    const key = inp.dataset.key;
    if (!key) return;
    if (inp.dataset.kind === 'bool') {
      cfg[key] = !!inp.checked;
    } else {
      const v = inp.value.trim();
      if (v) cfg[key] = v;
    }
  });
  if (status) { status.textContent = 'Saving…'; status.className = 'dm-storage-status'; }
  try {
    const res = await fetch(apiPath('/admin/storage/attachments/provider-config'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: _userId(), provider, config: cfg }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    if (status) { status.textContent = 'Saved.'; status.className = 'dm-storage-status ok'; }
    await _loadStorage();
    setTimeout(() => { if (status && status.classList.contains('ok')) status.textContent = ''; }, 3000);
  } catch (e) {
    if (status) { status.textContent = 'Error: ' + e.message; status.className = 'dm-storage-status err'; }
  }
}

async function _testBackend() {
  const sel = _qs('dm-storage-mode');
  const status = _qs('dm-storage-status');
  if (!sel) return;
  const mode = sel.value;
  if (status) { status.textContent = 'Testing ' + mode + '…'; status.className = 'dm-storage-status'; }
  try {
    const res = await fetch(apiPath('/admin/storage/attachments/test'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: _userId(), mode }),
    });
    const data = await res.json();
    if (data.ok) {
      const note = data.note ? ' — ' + data.note : (data.storage_path ? ' — wrote ' + data.storage_path : '');
      status.textContent = 'OK' + note;
      status.className = 'dm-storage-status ok';
    } else {
      status.textContent = 'Failed: ' + (data.error || 'unknown');
      status.className = 'dm-storage-status err';
    }
  } catch (e) {
    if (status) { status.textContent = 'Error: ' + e.message; status.className = 'dm-storage-status err'; }
  }
}

// ── Init ───────────────────────────────────────────────────────────────────

export function initDataManagement() {
  _initTabs();
  // First Storage paint is lazy on tab click. Pre-warm the cache so the
  // active-backend chip on the sidebar shows even before the user clicks.
  fetch(apiPath('/admin/storage/attachments/status?requesting_user_id=' + encodeURIComponent(_userId())))
    .then(r => r.ok ? r.json() : null)
    .then(s => {
      if (!s) return;
      _statusCache = s;
      const active = _qs('dm-storage-active');
      if (active) active.textContent = PROVIDER_META[s.mode]?.label || s.mode;
      // Expose for attachments.js to know whether to use the IndexedDB path.
      try { window.__webagentAttachmentBackend = s.mode; } catch {}
    })
    .catch(() => {});
}

// Public helper for attachments.js — sync lookup; cached and refreshed on
// every storage-tab load. Defaults to 'local' if unknown.
export function getActiveAttachmentBackend() {
  if (_statusCache?.mode) return _statusCache.mode;
  try { return window.__webagentAttachmentBackend || 'local'; } catch { return 'local'; }
}
