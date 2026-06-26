'use strict';

// Cloud VMs — drop-in Admin Tools view module.
// Discovered via ui/admin-tools/cloud-vms/page.json and driven generically by the
// admin shell (ui/shared/js/files.js applySidebarView), which dynamically imports
// this module and calls the exported startView / stopView when the view is shown /
// hidden. Talks to this folder's server.py (/admin/cloud-vms/*), which reuses the
// deploy subsystem (cloud key in the vault + saved project/zone in deploy.json).
//
// The SIDEBAR is the account manager: a provider dropdown + a "+" button add a
// cloud account; each account is a row that expands into its own login sub-menu
// (the id fields + secret key for THAT account, with Save / Sign out / Remove).
// The MAIN pane lists every server across EVERY added account in one combined
// view, grouped per account. Per-server Open / Start / Stop / Delete act on the
// account the row belongs to. Start/Stop/Delete confirm first (billable /
// destructive); Delete streams a live log.
//
// Aggregation is done here client-side by calling the existing per-account list
// endpoint once per added account — no special backend route, so one account
// failing never hides the others. All colours come from the design-system palette
// so the view is correct in dark + light. Sister module: ui/admin-tools/update/update.js.
// REMOVE-WHEN: the Cloud VMs view is dropped from the admin page catalog.

import { apiPath } from '../../shared/js/config.js';
import { _esc, _escAttr, _refreshLucideIcons } from '../../shared/js/dom-utils.js';

// providers : every cloud target that supports server management (from /providers).
// adding    : provider ids the user pressed "+" on but hasn't saved yet (client-only).
// expanded  : provider ids whose sidebar login sub-menu is open.
const state = { providers: [], adding: new Set(), expanded: new Set(), wired: false, busy: false };

function _qs(id) { return document.getElementById(id); }
function _userId() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }
function _byId(id) { return state.providers.find(p => p.id === id) || null; }

async function _get(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath('/admin/cloud-vms' + path + sep + 'requesting_user_id=' + encodeURIComponent(_userId())));
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
  return data;
}

async function _post(path, body) {
  const res = await fetch(apiPath('/admin/cloud-vms' + path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
  return data;
}

function _stats(msg) { const el = _qs('cvm-stats'); if (el) el.textContent = msg || ''; }

// ── Connection helpers (per account) ──
// An account is "added" (lives in the sidebar) once it has a stored key OR any
// saved id value — e.g. the Deploy card already configured the project — or the
// user just pressed "+". It is "connected" (can list servers) once the key is
// stored AND every required id field is filled.
function _idComplete(p) {
  return (p.connect_fields || []).filter(f => f.required).every(f => String(f.value || '').trim());
}
function _hasSavedId(p) { return (p.connect_fields || []).some(f => String(f.value || '').trim()); }
function _isAdded(p) { return !!p.has_key || _hasSavedId(p) || state.adding.has(p.id); }
function _isConnected(p) { return !!p.has_key && _idComplete(p); }

// The accounts shown in the sidebar / aggregated in the main view, in a stable
// order: backend-added accounts first, then any unsaved "+" additions.
function _accounts() {
  const added = state.providers.filter(p => p.has_key || _hasSavedId(p));
  const pending = state.providers.filter(p => state.adding.has(p.id) && !added.includes(p));
  return [...added, ...pending];
}

// ── Lifecycle (called by files.js applySidebarView) ──
export function startView() {
  _wire();
  _loadProviders().then(() => { _renderAccounts(); _loadAll(); });
}
export function stopView() { /* no polling — nothing to tear down */ }

function _wire() {
  if (state.wired) return; state.wired = true;
  _qs('cvm-refresh')?.addEventListener('click', _refreshAll);
  _qs('cvm-refresh-main')?.addEventListener('click', _refreshAll);
  _qs('cvm-add-btn')?.addEventListener('click', _addAccount);
  // The accounts list + the main body are re-rendered on every change, so their
  // controls are reached through one delegated listener each (survives re-renders).
  _qs('cvm-accounts')?.addEventListener('click', _onSideClick);
  _qs('cvm-body')?.addEventListener('click', _onBodyClick);
}

function _refreshAll() {
  _loadProviders().then(() => { _renderAccounts(); _loadAll(); });
}

// ── Providers (which clouds can be added at all) ──
async function _loadProviders() {
  try {
    const data = await _get('/providers');
    state.providers = (data.providers || []).filter(p => p.available !== false || p.has_key || _hasSavedId(p));
  } catch (e) { state.providers = []; _renderError(e.message); }
}

// ── Sidebar: the add control + one row (with login sub-menu) per account ──
function _renderAccounts() {
  const accounts = _accounts();
  const taken = new Set(accounts.map(p => p.id));

  // Dropdown of providers not yet added; hidden entirely when none remain.
  const addable = state.providers.filter(p => !taken.has(p.id));
  const add = _qs('cvm-add');
  const sel = _qs('cvm-add-provider');
  if (sel) sel.innerHTML = addable.map(p =>
    `<option value="${_escAttr(p.id)}">${_esc(p.display_name)}</option>`).join('');
  if (add) add.hidden = addable.length === 0;

  const wrap = _qs('cvm-accounts'); if (!wrap) return;
  if (!accounts.length) {
    wrap.innerHTML = '<div class="cvm-hint cvm-accounts-empty">No accounts yet — pick a cloud above and press +.</div>';
    return;
  }
  wrap.innerHTML = accounts.map(_acctHtml).join('');
  _refreshLucideIcons(wrap);
}

function _acctStatus(p) {
  if (_isConnected(p)) return { cls: 'cvm-acct-pill-ok', text: 'Connected' };
  if (p.has_key && !_idComplete(p)) return { cls: 'cvm-acct-pill-warn', text: 'Add id' };
  if (!p.has_key && _hasSavedId(p)) return { cls: 'cvm-acct-pill-warn', text: 'Needs key' };
  return { cls: 'cvm-acct-pill-off', text: 'Set up' };
}

function _acctHtml(p) {
  const open = state.expanded.has(p.id);
  const st = _acctStatus(p);
  const icon = p.icon || 'cloud';
  return '<div class="cvm-acct' + (open ? ' cvm-open' : '') + '" data-provider="' + _escAttr(p.id) + '">'
    + '<button class="cvm-acct-row" type="button" data-acct-toggle="' + _escAttr(p.id) + '">'
    +   '<i data-lucide="' + _escAttr(icon) + '" class="cvm-acct-icon"></i>'
    +   '<span class="cvm-acct-name">' + _esc(p.display_name) + '</span>'
    +   '<span class="cvm-acct-pill ' + st.cls + '">' + _esc(st.text) + '</span>'
    +   '<i data-lucide="chevron-down" class="cvm-acct-chevron"></i>'
    + '</button>'
    + '<div class="cvm-acct-menu"' + (open ? '' : ' hidden') + '>' + (open ? _connectFormHtml(p) : '') + '</div>'
    + '</div>';
}

// The per-account login form (rendered inside the row's sub-menu). Collects the
// cloud "id" (non-secret connect_fields, pre-filled) + the secret "key" (never
// pre-filled; blank keeps the stored key). Save writes both; Sign out forgets the
// key (keeps the id); Remove clears the account entirely.
function _connectFormHtml(p) {
  const haveKey = !!p.has_key;
  const idInputs = (p.connect_fields || []).map(f =>
    '<div class="cvm-field">'
    + '<label class="cvm-field-label">' + _esc(f.label || f.key) + (f.required ? ' <span class="cvm-req">*</span>' : '') + '</label>'
    + '<input type="text" class="cvm-connect-input" data-key="' + _escAttr(f.key) + '" '
    +   'value="' + _escAttr(f.value || '') + '" placeholder="' + _escAttr(f.placeholder || '') + '" '
    +   'autocomplete="off" spellcheck="false">'
    + (f.tip ? '<div class="cvm-field-tip">' + _esc(f.tip) + '</div>' : '')
    + '</div>'
  ).join('');

  const keyInputs = (p.credential_fields || []).filter(f => f.secret).map(f =>
    '<div class="cvm-field">'
    + '<label class="cvm-field-label">' + _esc(f.label || f.key) + (haveKey ? '' : ' <span class="cvm-req">*</span>') + '</label>'
    + '<textarea class="cvm-connect-input cvm-key-input" data-key="' + _escAttr(f.key) + '" rows="4" '
    +   'placeholder="' + _escAttr(haveKey ? 'A key is already saved — leave blank to keep it' : (f.placeholder || 'Paste this account’s cloud key here')) + '" '
    +   'autocomplete="off" spellcheck="false"></textarea>'
    + (f.tip ? '<div class="cvm-field-tip">' + _esc(f.tip) + '</div>' : '')
    + '</div>'
  ).join('');

  const actions = '<div class="cvm-acct-actions">'
    + '<button class="cvm-btn cvm-btn-primary" data-acct-save="' + _escAttr(p.id) + '"><i data-lucide="plug"></i> Save</button>'
    + (haveKey ? '<button class="cvm-btn" data-acct-signout="' + _escAttr(p.id) + '"><i data-lucide="log-out"></i> Sign out</button>' : '')
    + '<button class="cvm-btn cvm-btn-danger" data-acct-remove="' + _escAttr(p.id) + '"><i data-lucide="trash-2"></i> Remove</button>'
    + '<span class="cvm-key-status" data-acct-status="' + _escAttr(p.id) + '"></span>'
    + '</div>';

  return idInputs + keyInputs + actions
    + '<div class="cvm-field-tip cvm-acct-foot">Stored encrypted in the app vault — the same place the Deploy card keeps it — and never shown again after saving.</div>';
}

// ── Sidebar interactions (delegated) ──
function _onSideClick(e) {
  const toggle = e.target.closest('[data-acct-toggle]');
  if (toggle) { _toggle(toggle.dataset.acctToggle); return; }
  const save = e.target.closest('[data-acct-save]');
  if (save) { _saveAccount(save.dataset.acctSave); return; }
  const out = e.target.closest('[data-acct-signout]');
  if (out) { _signOut(out.dataset.acctSignout); return; }
  const rm = e.target.closest('[data-acct-remove]');
  if (rm) { _removeAccount(rm.dataset.acctRemove); return; }
}

function _toggle(id) {
  if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
  _renderAccounts();
}

function _addAccount() {
  const sel = _qs('cvm-add-provider');
  const id = sel && sel.value;
  if (!id) return;
  state.adding.add(id);
  state.expanded.add(id);      // open its login sub-menu straight away
  _renderAccounts();
  _loadAll();                  // reflect the new (not-yet-connected) account in the main view
}

function _menuEl(id) {
  const acct = document.querySelector('.cvm-acct[data-provider="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
  return acct ? acct.querySelector('.cvm-acct-menu') : null;
}
function _acctStatusEl(id) {
  return document.querySelector('[data-acct-status="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
}
function _setAcctStatus(id, text, kind) {
  const el = _acctStatusEl(id); if (!el) return;
  el.textContent = text || '';
  el.className = 'cvm-key-status' + (kind === 'ok' ? ' cvm-key-ok' : kind === 'err' ? ' cvm-key-err' : '');
}

async function _saveAccount(id) {
  const p = _byId(id); if (!p) return;
  const menu = _menuEl(id); if (!menu) return;
  const values = {};
  menu.querySelectorAll('.cvm-connect-input').forEach(el => {
    const v = (el.value || '').trim();
    if (v) values[el.dataset.key] = v;
  });
  // Required id fields must be present; the key is required only when none is
  // stored yet (blank otherwise = keep the stored key).
  const missingId = (p.connect_fields || []).filter(f => f.required && !values[f.key]);
  if (missingId.length) { _setAcctStatus(id, 'Enter ' + (missingId[0].label || 'the account id') + ' first.', 'err'); return; }
  const keyFields = (p.credential_fields || []).filter(f => f.secret);
  const keyGiven = keyFields.some(f => values[f.key]);
  if (!p.has_key && !keyGiven) { _setAcctStatus(id, 'Paste this account’s key first.', 'err'); return; }

  _setAcctStatus(id, 'Saving…');
  try {
    await _post('/connect', { provider: id, values });
    state.adding.delete(id);
    await _loadProviders();
    _renderAccounts();
    await _loadAll();
  } catch (e) { _setAcctStatus(id, e.message, 'err'); }
}

async function _signOut(id) {
  if (!window.confirm('Sign out of this cloud account?\n\nThis removes the stored cloud key. Your servers are not affected — reconnect any time by entering the key again.')) return;
  try { await _post('/disconnect', { provider: id }); } catch (e) { /* fall through to re-render regardless */ }
  await _loadProviders();
  state.expanded.add(id);   // keep the row open so the user can re-enter the key
  _renderAccounts();
  _loadAll();
}

async function _removeAccount(id) {
  const p = _byId(id);
  const backendAdded = p && (p.has_key || _hasSavedId(p));
  if (backendAdded) {
    if (!window.confirm('Remove this cloud account?\n\nIts stored key and saved id are cleared from the app. Your actual servers in the cloud are not affected.')) return;
    try { await _post('/disconnect', { provider: id, forget_config: true }); } catch (e) { /* fall through */ }
  }
  state.adding.delete(id);
  state.expanded.delete(id);
  await _loadProviders();
  _renderAccounts();
  _loadAll();
}

// ── Main pane: every server across every account, grouped per account ──
async function _loadAll() {
  const body = _qs('cvm-body'); if (!body) return;
  const accounts = _accounts();
  if (!accounts.length) {
    _stats('');
    body.innerHTML = '<div class="cvm-notice"><div class="cvm-notice-title"><i data-lucide="cloud"></i> No cloud accounts yet</div>'
      + '<div class="cvm-hint">Pick a cloud provider in the sidebar on the left and press the plus button to add it, then enter its id and key. '
      + 'Servers from every account you add show up here together.</div></div>';
    _refreshLucideIcons(body);
    return;
  }
  _stats('Loading…');
  body.innerHTML = '<div class="cvm-hint cvm-main-hint">Loading your servers…</div>';

  let total = 0, live = 0;
  const parts = await Promise.all(accounts.map(async (p) => {
    if (!_isConnected(p)) return _groupNotice(p, 'Open this account in the sidebar and enter its key to see its servers.');
    try {
      const data = await _get('/instances?provider=' + encodeURIComponent(p.id));
      if (data.needs_connect || data.needs_key) return _groupNotice(p, data.detail);
      if (data.ok === false) return _groupError(p, data.detail || 'Could not list servers.');
      const project = data.project || p.project || '';
      if (project) p.project = project;
      const list = data.instances || [];
      total += list.length; live += 1;
      return _groupHtml(p, project, list);
    } catch (e) { return _groupError(p, e.message); }
  }));

  body.innerHTML = parts.join('');
  _refreshLucideIcons(body);
  _stats(total + ' server' + (total === 1 ? '' : 's')
    + ' · ' + live + ' of ' + accounts.length + ' account' + (accounts.length === 1 ? '' : 's') + ' connected');
}

function _groupHead(p, meta) {
  return '<div class="cvm-group-head">'
    + '<i data-lucide="' + _escAttr(p.icon || 'cloud') + '"></i>'
    + '<span class="cvm-group-name">' + _esc(p.display_name) + '</span>'
    + (meta ? '<span class="cvm-group-meta">' + _esc(meta) + '</span>' : '')
    + '</div>';
}

function _groupHtml(p, project, list) {
  const meta = (project ? project + ' · ' : '') + list.length + ' server' + (list.length === 1 ? '' : 's');
  const inner = list.length
    ? list.map(inst => _rowHtml(inst, p.id)).join('')
    : '<div class="cvm-hint cvm-group-empty">No servers in this account yet. Create one from App Settings → Deploy.</div>';
  return '<div class="cvm-group">' + _groupHead(p, meta) + inner + '</div>';
}

function _groupNotice(p, detail) {
  return '<div class="cvm-group">' + _groupHead(p, 'not connected')
    + '<div class="cvm-notice cvm-group-notice"><div class="cvm-hint">'
    + _esc(detail || 'Open this account in the sidebar and enter its key to see its servers.')
    + '</div></div></div>';
}

function _groupError(p, msg) {
  return '<div class="cvm-group">' + _groupHead(p, 'error')
    + '<div class="cvm-notice cvm-notice-warn cvm-group-notice"><div class="cvm-hint">'
    + _esc(msg || 'Could not load this account.') + '</div></div></div>';
}

function _rowHtml(inst, providerId) {
  const st = _statusMeta(inst.status);
  const badges = (inst.is_this_app ? '<span class="cvm-badge cvm-badge-app">This app</span>' : '')
    + (inst.is_webagent && !inst.is_this_app ? '<span class="cvm-badge">webAgent</span>' : '');
  const running = (inst.status || '').toUpperCase() === 'RUNNING';
  const stopped = ['TERMINATED', 'SUSPENDED'].includes((inst.status || '').toUpperCase());
  const url = inst.ip ? ('http://' + inst.ip) : '';
  // ref carries the account too, so an action runs against the right cloud.
  const d = String(providerId) + '|' + String(inst.zone) + '|' + String(inst.name);

  const meta = [];
  if (inst.machine_type) meta.push('<span class="cvm-meta-item"><i data-lucide="cpu"></i>' + _esc(inst.machine_type) + '</span>');
  if (inst.ip) meta.push('<span class="cvm-meta-item"><i data-lucide="globe"></i>' + _esc(inst.ip) + '</span>');
  if (inst.created) meta.push('<span class="cvm-meta-item"><i data-lucide="calendar"></i>' + _esc(_fmtDate(inst.created)) + '</span>');

  const actions = [];
  if (running && url) actions.push('<a class="cvm-act cvm-act-open" href="' + _escAttr(url) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>');
  if (stopped) actions.push('<button class="cvm-act" data-action="start" data-ref="' + _escAttr(d) + '"><i data-lucide="play"></i>Start</button>');
  if (running) actions.push('<button class="cvm-act" data-action="stop" data-ref="' + _escAttr(d) + '"><i data-lucide="square"></i>Stop</button>');
  actions.push('<button class="cvm-act cvm-act-danger" data-action="delete" data-ref="' + _escAttr(d) + '"><i data-lucide="trash-2"></i>Delete</button>');

  return '<div class="cvm-card' + (inst.is_this_app ? ' cvm-card-app' : '') + '">'
    + '<div class="cvm-card-main">'
    +   '<div class="cvm-card-head">'
    +     '<span class="cvm-dot ' + st.dot + '" title="' + _escAttr(st.label) + '"></span>'
    +     '<span class="cvm-name">' + _esc(inst.name) + '</span>'
    +     badges
    +     '<span class="cvm-status">' + _esc(st.label) + '</span>'
    +   '</div>'
    +   (meta.length ? '<div class="cvm-meta">' + meta.join('') + '</div>' : '')
    + '</div>'
    + '<div class="cvm-actions">' + actions.join('') + '</div>'
    + '</div>';
}

// Google Compute states → a friendly label + status-dot class.
function _statusMeta(status) {
  switch ((status || '').toUpperCase()) {
    case 'RUNNING': return { label: 'Running', dot: 'cvm-dot-run' };
    case 'TERMINATED': return { label: 'Stopped', dot: 'cvm-dot-stop' };
    case 'SUSPENDED': return { label: 'Suspended', dot: 'cvm-dot-stop' };
    case 'STOPPING': return { label: 'Stopping…', dot: 'cvm-dot-busy' };
    case 'SUSPENDING': return { label: 'Suspending…', dot: 'cvm-dot-busy' };
    case 'PROVISIONING': return { label: 'Starting…', dot: 'cvm-dot-busy' };
    case 'STAGING': return { label: 'Starting…', dot: 'cvm-dot-busy' };
    case 'REPAIRING': return { label: 'Repairing…', dot: 'cvm-dot-busy' };
    default: return { label: status || 'Unknown', dot: 'cvm-dot-stop' };
  }
}

function _fmtDate(iso) {
  try {
    const dt = new Date(iso);
    if (isNaN(dt.getTime())) return iso;
    return 'Created ' + dt.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  } catch { return iso; }
}

// ── Error state (provider load failed) ──
function _renderError(msg) {
  const body = _qs('cvm-body'); if (!body) return;
  body.innerHTML = '<div class="cvm-notice cvm-notice-warn"><div class="cvm-notice-title">Couldn’t load servers</div>'
    + '<div class="cvm-hint">' + _esc(msg || 'Unknown error') + '</div></div>';
}

// ── Actions (Start / Stop / Delete) ──
function _onBodyClick(e) {
  const btn = e.target.closest('[data-action]');
  if (!btn || state.busy) return;
  const [provider, zone, name] = (btn.dataset.ref || '').split('|');
  _doAction(btn.dataset.action, provider, zone, name);
}

function _confirmText(action, name) {
  if (action === 'delete') return 'Delete the server “' + name + '”?\n\nThis permanently destroys it and its disk. This cannot be undone.';
  if (action === 'stop') return 'Stop the server “' + name + '”?\n\nIt will go offline until you start it again.';
  return 'Start the server “' + name + '”?\n\nThis resumes the server (and its billing).';
}

async function _doAction(action, provider, zone, name) {
  if (!provider || !zone || !name) return;
  if (!window.confirm(_confirmText(action, name))) return;
  state.busy = true;
  _setBusy(true);
  _logReset();
  const verb = action === 'delete' ? 'Deleting' : action === 'stop' ? 'Stopping' : 'Starting';
  _logLine(verb + ' ' + name + '…');
  try {
    await _stream('/instance/action', { provider, action, zone, name }, (result) => {
      _logLine(result.message || (result.ok ? 'Done.' : 'Failed.'), result.ok ? 'ok' : 'err');
    });
  } catch (e) { _logLine(e.message, 'err'); }
  finally {
    state.busy = false; _setBusy(false);
    await _loadAll();
  }
}

function _setBusy(busy) {
  document.querySelectorAll('#cvm-body [data-action], #cvm-refresh, #cvm-refresh-main, #cvm-add-btn, #cvm-add-provider')
    .forEach(el => { el.disabled = busy; });
}

// ── Live log (NDJSON stream reader, same shape as the Deploy card) ──
function _logReset() { const log = _qs('cvm-log'); if (log) { log.hidden = false; log.textContent = ''; } }
function _logLine(text, level) {
  const log = _qs('cvm-log'); if (!log) return;
  const mark = level === 'ok' ? '✓ ' : level === 'err' ? '✗ ' : level === 'warn' ? '! ' : '· ';
  log.textContent += mark + text + '\n';
  log.scrollTop = log.scrollHeight;
}

async function _stream(path, body, onDone) {
  const res = await fetch(apiPath('/admin/cloud-vms' + path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch {}
    throw new Error(detail || ('HTTP ' + res.status));
  }
  const ctype = res.headers.get('content-type') || '';
  if (!ctype.includes('ndjson') || !res.body || !res.body.getReader) {
    const obj = await res.json();
    onDone(obj.result || obj);
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const drain = (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      if (ev.phase === 'done') onDone(ev.result || {});
      else if (ev.message) _logLine(ev.message, ev.level);
    }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    drain(dec.decode(value, { stream: true }));
  }
  drain(dec.decode());
}
