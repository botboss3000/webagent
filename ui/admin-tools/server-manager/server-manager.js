'use strict';

// Server Manager — drop-in Admin Tools view module.
// Discovered via ui/admin-tools/server-manager/page.json and driven generically
// by the admin shell (ui/shared/js/files.js applySidebarView), which dynamically
// imports this module and calls the exported startView / stopView when the view
// is shown / hidden. Talks to this folder's server.py (/admin/server-manager/*).
//
// A single hub for every server the admin runs, grouped in the main pane:
//   • Cloud accounts — VMs in a cloud account (Open / Start / Stop / Delete).
//     The SIDEBAR account manager (provider dropdown + per-account login
//     sub-menu) is unchanged from the old Cloud VMs page.
//   • Devices        — every computer running WebAgent (the presence registry):
//     online/offline, platform, self-reported repo, Open link to its endpoint.
//   • Machines       — manually-tracked boxes that don't run WebAgent: address,
//     repo, notes, a reachability Ping, Edit / Remove.
//   • Sites          — manually-tracked hosted URLs: same, with an HTTP Ping.
//
// Each card shows the code repo it runs; cloud/device repos are editable via a
// pencil (saved as an annotation), manual ones via the entry's Edit form. All
// colours come from the design-system palette so the view is correct in
// dark + light. Element/class names keep the historical `cvm-` prefix.
// REMOVE-WHEN: the Server Manager view is dropped from the admin page catalog.

import { apiPath } from '../../shared/js/config.js';
import { _esc, _escAttr, _refreshLucideIcons } from '../../shared/js/dom-utils.js';

// providers : cloud targets that support server management (from /providers).
// adding    : provider ids the user pressed "+" on but hasn't saved yet.
// expanded  : provider ids whose sidebar login sub-menu is open.
// editing   : the id of the manual entry currently open in the add/edit form
//             (null = the form, when shown, is a fresh "add").
const state = {
  providers: [], adding: new Set(), expanded: new Set(),
  editing: null, formOpen: false, wired: false, busy: false,
};

function _qs(id) { return document.getElementById(id); }
function _userId() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }
function _byId(id) { return state.providers.find(p => p.id === id) || null; }

async function _get(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath('/admin/server-manager' + path + sep + 'requesting_user_id=' + encodeURIComponent(_userId())));
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
  return data;
}

async function _post(path, body) {
  const res = await fetch(apiPath('/admin/server-manager' + path), {
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

// ── Cloud-account connection helpers (per account) ──
function _idComplete(p) {
  return (p.connect_fields || []).filter(f => f.required).every(f => String(f.value || '').trim());
}
function _hasSavedId(p) { return (p.connect_fields || []).some(f => String(f.value || '').trim()); }
function _isConnected(p) { return !!p.has_key && _idComplete(p); }

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
  _qs('cvm-add-entry')?.addEventListener('click', () => _openEntryForm(null));
  // The accounts list, entry form and main body are re-rendered on change, so
  // each is reached through one delegated listener (survives re-renders).
  _qs('cvm-accounts')?.addEventListener('click', _onSideClick);
  _qs('cvm-entry-form')?.addEventListener('click', _onFormClick);
  _qs('cvm-body')?.addEventListener('click', _onBodyClick);
}

function _refreshAll() {
  _loadProviders().then(() => { _renderAccounts(); _loadAll(); });
}

// ── Cloud providers (which clouds can be added at all) ──
async function _loadProviders() {
  try {
    const data = await _get('/providers');
    state.providers = (data.providers || []).filter(p => p.available !== false || p.has_key || _hasSavedId(p));
  } catch (e) { state.providers = []; }
}

// ── Sidebar: cloud-account manager (add control + one row per account) ──
function _renderAccounts() {
  const accounts = _accounts();
  const taken = new Set(accounts.map(p => p.id));

  const addable = state.providers.filter(p => !taken.has(p.id));
  const add = _qs('cvm-add');
  const sel = _qs('cvm-add-provider');
  if (sel) sel.innerHTML = addable.map(p =>
    `<option value="${_escAttr(p.id)}">${_esc(p.display_name)}</option>`).join('');
  if (add) add.hidden = addable.length === 0;

  const wrap = _qs('cvm-accounts'); if (!wrap) return;
  if (!accounts.length) {
    wrap.innerHTML = '<div class="cvm-hint cvm-accounts-empty">No cloud accounts yet — pick a cloud above and press +.</div>';
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
  state.expanded.add(id);
  _renderAccounts();
  _loadAll();
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
  try { await _post('/disconnect', { provider: id }); } catch (e) { /* re-render regardless */ }
  await _loadProviders();
  state.expanded.add(id);
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

// ── Sidebar: the add / edit form for a manual machine or site ──
const KIND_OPTIONS = [
  { v: 'linux', t: 'Linux machine' },
  { v: 'termux', t: 'Termux (Android)' },
  { v: 'windows', t: 'Windows machine' },
  { v: 'mac', t: 'Mac machine' },
  { v: 'other', t: 'Other machine' },
  { v: 'site', t: 'Hosted site / URL' },
];

function _openEntryForm(entry) {
  state.editing = entry ? entry.id : null;
  state.formOpen = true;
  _renderEntryForm(entry || {});
  const form = _qs('cvm-entry-form'); if (form) form.hidden = false;
}

function _closeEntryForm() {
  state.editing = null; state.formOpen = false;
  const form = _qs('cvm-entry-form'); if (form) { form.hidden = true; form.innerHTML = ''; }
}

function _renderEntryForm(e) {
  const form = _qs('cvm-entry-form'); if (!form) return;
  const kind = e.kind || 'linux';
  const opts = KIND_OPTIONS.map(o =>
    '<option value="' + o.v + '"' + (o.v === kind ? ' selected' : '') + '>' + _esc(o.t) + '</option>').join('');
  form.innerHTML =
      '<div class="cvm-field">'
    +   '<label class="cvm-field-label">Type</label>'
    +   '<select class="cvm-connect-input" data-ef="kind">' + opts + '</select>'
    + '</div>'
    + '<div class="cvm-field">'
    +   '<label class="cvm-field-label">Name <span class="cvm-req">*</span></label>'
    +   '<input type="text" class="cvm-connect-input" data-ef="name" value="' + _escAttr(e.name || '') + '" placeholder="My laptop / production site" autocomplete="off">'
    + '</div>'
    + '<div class="cvm-field">'
    +   '<label class="cvm-field-label">Address / URL</label>'
    +   '<input type="text" class="cvm-connect-input" data-ef="address" value="' + _escAttr(e.address || '') + '" placeholder="192.168.1.20:8080 or https://example.com" autocomplete="off" spellcheck="false">'
    + '</div>'
    + '<div class="cvm-field">'
    +   '<label class="cvm-field-label">Code repo</label>'
    +   '<input type="text" class="cvm-connect-input" data-ef="repo" value="' + _escAttr(e.repo || '') + '" placeholder="owner/repo or git URL" autocomplete="off" spellcheck="false">'
    + '</div>'
    + '<div class="cvm-field">'
    +   '<label class="cvm-field-label">Notes</label>'
    +   '<textarea class="cvm-connect-input" data-ef="notes" rows="2" placeholder="Anything worth remembering" autocomplete="off">' + _esc(e.notes || '') + '</textarea>'
    + '</div>'
    + '<div class="cvm-acct-actions">'
    +   '<button class="cvm-btn cvm-btn-primary" data-ef-save><i data-lucide="check"></i> ' + (e.id ? 'Save' : 'Add') + '</button>'
    +   '<button class="cvm-btn" data-ef-cancel><i data-lucide="x"></i> Cancel</button>'
    +   '<span class="cvm-key-status" data-ef-status></span>'
    + '</div>';
  _refreshLucideIcons(form);
}

function _onFormClick(e) {
  if (e.target.closest('[data-ef-cancel]')) { _closeEntryForm(); return; }
  if (e.target.closest('[data-ef-save]')) { _saveEntry(); return; }
}

function _formStatus(text, kind) {
  const el = document.querySelector('[data-ef-status]'); if (!el) return;
  el.textContent = text || '';
  el.className = 'cvm-key-status' + (kind === 'ok' ? ' cvm-key-ok' : kind === 'err' ? ' cvm-key-err' : '');
}

async function _saveEntry() {
  const form = _qs('cvm-entry-form'); if (!form) return;
  const val = (k) => { const el = form.querySelector('[data-ef="' + k + '"]'); return el ? (el.value || '').trim() : ''; };
  const name = val('name');
  if (!name) { _formStatus('Enter a name first.', 'err'); return; }
  _formStatus('Saving…');
  try {
    await _post('/entries', {
      id: state.editing || '', kind: val('kind'), name,
      address: val('address'), repo: val('repo'), notes: val('notes'),
    });
    _closeEntryForm();
    await _loadAll();
  } catch (err) { _formStatus(err.message, 'err'); }
}

// ── Main pane: every source, grouped ──
async function _loadAll() {
  const body = _qs('cvm-body'); if (!body) return;
  _stats('Loading…');
  body.innerHTML = '<div class="cvm-hint cvm-main-hint">Loading your servers…</div>';

  const accounts = _accounts();
  // Cloud groups (per account) and manual entries load in parallel; each source
  // is guarded so one failing never blanks the others. (Linked WebAgent devices
  // moved to the Database & Devices page.)
  const [cloudParts, entData] = await Promise.all([
    Promise.all(accounts.map(_cloudGroup)),
    _get('/entries').catch(() => ({ entries: [] })),
  ]);

  const entries = entData.entries || [];
  const machines = entries.filter(e => e.kind !== 'site');
  const sites = entries.filter(e => e.kind === 'site');

  const sections = [];
  sections.push(cloudParts.join(''));
  sections.push(_entriesGroup('Machines', 'hard-drive', machines));
  sections.push(_entriesGroup('Sites', 'globe', sites));
  const html = sections.filter(Boolean).join('');

  body.innerHTML = html || _emptyNotice();
  _refreshLucideIcons(body);

  const cloudCount = accounts.reduce((n, p) => n + (p._count || 0), 0);
  const bits = [];
  if (accounts.length) bits.push(cloudCount + ' cloud server' + (cloudCount === 1 ? '' : 's'));
  if (entries.length) bits.push(entries.length + ' tracked');
  _stats(bits.join(' · '));
}

function _emptyNotice() {
  return '<div class="cvm-notice"><div class="cvm-notice-title"><i data-lucide="server"></i> Nothing to manage yet</div>'
    + '<div class="cvm-hint">Add a cloud account or a machine / site in the sidebar on the left. Computers running WebAgent '
    + 'against your shared database are listed on the <strong>Database &amp; Devices</strong> page.</div></div>';
}

// One cloud account → its server group (or a connect / error notice).
async function _cloudGroup(p) {
  if (!_isConnected(p)) { p._count = 0; return _groupNotice(p.display_name, p.icon || 'cloud', 'Open this account in the sidebar and enter its key to see its servers.'); }
  try {
    const data = await _get('/instances?provider=' + encodeURIComponent(p.id));
    if (data.needs_connect || data.needs_key) { p._count = 0; return _groupNotice(p.display_name, p.icon || 'cloud', data.detail); }
    if (data.ok === false) { p._count = 0; return _groupError(p.display_name, p.icon || 'cloud', data.detail || 'Could not list servers.'); }
    const project = data.project || p.project || '';
    if (project) p.project = project;
    const list = data.instances || [];
    p._count = list.length;
    const meta = (project ? project + ' · ' : '') + list.length + ' server' + (list.length === 1 ? '' : 's');
    const inner = list.length
      ? list.map(inst => _cloudCard(inst, p.id)).join('')
      : '<div class="cvm-hint cvm-group-empty">No servers in this account yet. Create one from App Settings → Deploy.</div>';
    return '<div class="cvm-group">' + _groupHead(p.display_name, p.icon || 'cloud', meta) + inner + '</div>';
  } catch (e) { p._count = 0; return _groupError(p.display_name, p.icon || 'cloud', e.message); }
}

function _groupHead(name, icon, meta) {
  return '<div class="cvm-group-head">'
    + '<i data-lucide="' + _escAttr(icon || 'cloud') + '"></i>'
    + '<span class="cvm-group-name">' + _esc(name) + '</span>'
    + (meta ? '<span class="cvm-group-meta">' + _esc(meta) + '</span>' : '')
    + '</div>';
}

function _groupNotice(name, icon, detail) {
  return '<div class="cvm-group">' + _groupHead(name, icon, 'not connected')
    + '<div class="cvm-notice cvm-group-notice"><div class="cvm-hint">'
    + _esc(detail || 'Open this account in the sidebar and enter its key to see its servers.')
    + '</div></div></div>';
}

function _groupError(name, icon, msg) {
  return '<div class="cvm-group">' + _groupHead(name, icon, 'error')
    + '<div class="cvm-notice cvm-notice-warn cvm-group-notice"><div class="cvm-hint">'
    + _esc(msg || 'Could not load.') + '</div></div></div>';
}

// ── The shared card renderer (every source normalizes to this) ──
// opts: { id, classes, dot, statusLabel, name, nameMono, badges[], meta[],
//         repo, repoEdit (data value or ''), actions[] }
function _card(o) {
  const badges = (o.badges || []).join('');
  const meta = (o.meta || []).length ? '<div class="cvm-meta">' + o.meta.join('') + '</div>' : '';
  const repoLine = _repoLine(o.repo, o.repoEdit);
  const actions = (o.actions || []).length ? '<div class="cvm-actions">' + o.actions.join('') + '</div>' : '';
  return '<div class="cvm-card' + (o.classes ? ' ' + o.classes : '') + '"' + (o.id ? ' id="' + _escAttr(o.id) + '"' : '') + '>'
    + '<div class="cvm-card-main">'
    +   '<div class="cvm-card-head">'
    +     '<span class="cvm-dot ' + (o.dot || 'cvm-dot-stop') + '" title="' + _escAttr(o.statusLabel || '') + '"></span>'
    +     '<span class="cvm-name' + (o.nameMono === false ? ' cvm-name-plain' : '') + '">' + _esc(o.name || '') + '</span>'
    +     badges
    +     '<span class="cvm-status">' + _esc(o.statusLabel || '') + '</span>'
    +   '</div>'
    +   meta
    +   repoLine
    + '</div>'
    + actions
    + '</div>';
}

// The repo row. `edit` is the data value for the pencil ("ann:<key>") — omitted
// for read-only (manual entries edit their repo through the entry form).
function _repoLine(repo, edit) {
  const text = repo
    ? '<a class="cvm-repo-link" href="' + _escAttr(_repoUrl(repo)) + '" target="_blank" rel="noopener">' + _esc(repo) + '</a>'
    : '<span class="cvm-repo-none">no repo set</span>';
  const pencil = edit
    ? '<button class="cvm-repo-edit" data-repo-edit="' + _escAttr(edit) + '" title="Set the repo this runs"><i data-lucide="pencil"></i></button>'
    : '';
  return '<div class="cvm-repo"><i data-lucide="git-branch"></i>' + text + pencil + '</div>';
}

function _repoUrl(repo) {
  const r = String(repo || '').trim();
  if (/^https?:\/\//i.test(r)) return r;
  if (/^git@github\.com:/i.test(r)) return 'https://github.com/' + r.replace(/^git@github\.com:/i, '').replace(/\.git$/i, '');
  if (/^[\w.-]+\/[\w.-]+$/.test(r)) return 'https://github.com/' + r.replace(/\.git$/i, '');
  return r;
}

function _openUrl(addr) {
  const a = String(addr || '').trim();
  if (!a) return '';
  return /^https?:\/\//i.test(a) ? a : 'http://' + a;
}

// ── Cloud VM card ──
function _cloudCard(inst, providerId) {
  const st = _statusMeta(inst.status);
  const running = (inst.status || '').toUpperCase() === 'RUNNING';
  const stopped = ['TERMINATED', 'SUSPENDED'].includes((inst.status || '').toUpperCase());
  const url = inst.ip ? ('http://' + inst.ip) : '';
  const ref = String(providerId) + '|' + String(inst.zone) + '|' + String(inst.name);

  const badges = [];
  if (inst.is_this_app) badges.push('<span class="cvm-badge cvm-badge-app">This app</span>');
  else if (inst.is_webagent) badges.push('<span class="cvm-badge">WebAgent</span>');

  const meta = [];
  if (inst.machine_type) meta.push(_metaItem('cpu', inst.machine_type));
  if (inst.ip) meta.push(_metaItem('globe', inst.ip));
  if (inst.created) meta.push(_metaItem('calendar', _fmtDate(inst.created)));

  const actions = [];
  if (running && url) actions.push('<a class="cvm-act cvm-act-open" href="' + _escAttr(url) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>');
  if (stopped) actions.push(_actBtn('start', ref, 'play', 'Start'));
  if (running) actions.push(_actBtn('stop', ref, 'square', 'Stop'));
  actions.push(_actBtn('delete', ref, 'trash-2', 'Delete', 'cvm-act-danger'));

  return _card({
    classes: inst.is_this_app ? 'cvm-card-app' : '',
    dot: st.dot, statusLabel: st.label, name: inst.name,
    badges, meta, repo: inst.repo || '', repoEdit: 'ann:' + (inst.annotation_key || ''),
    actions,
  });
}

// ── Manual machines / sites group ──
function _entriesGroup(title, icon, list) {
  if (!list.length) return '';   // hide an empty Machines / Sites section
  const inner = list.map(_entryCard).join('');
  return '<div class="cvm-group">' + _groupHead(title, icon, list.length + ' tracked') + inner + '</div>';
}

function _entryCard(e) {
  const isSite = e.kind === 'site';
  const meta = [];
  meta.push(_metaItem(_kindIcon(e.kind), _kindLabel(e.kind)));
  if (e.address) meta.push(_metaItem(isSite ? 'globe' : 'network', e.address));
  if (e.notes) meta.push(_metaItem('sticky-note', e.notes));

  const actions = [];
  const open = _openUrl(e.address);
  if (open) actions.push('<a class="cvm-act cvm-act-open" href="' + _escAttr(open) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>');
  if (e.address) actions.push('<button class="cvm-act" data-ping="' + _escAttr(e.address) + '" data-card="' + _escAttr('entry-' + e.id) + '"><i data-lucide="activity"></i>Ping</button>');
  actions.push('<button class="cvm-act" data-entry-edit="' + _escAttr(e.id) + '"><i data-lucide="pencil"></i>Edit</button>');
  actions.push('<button class="cvm-act cvm-act-danger" data-entry-remove="' + _escAttr(e.id) + '"><i data-lucide="trash-2"></i>Remove</button>');

  return _card({
    id: 'entry-' + e.id,
    dot: 'cvm-dot-stop', statusLabel: 'Not checked',
    name: e.name, nameMono: false,
    meta, repo: e.repo || '', repoEdit: '',   // manual repo edited via the Edit form
    actions,
  });
}

// ── Small builders ──
function _metaItem(icon, text) {
  return '<span class="cvm-meta-item"><i data-lucide="' + _escAttr(icon) + '"></i>' + _esc(text) + '</span>';
}
function _actBtn(action, ref, icon, label, extra) {
  return '<button class="cvm-act' + (extra ? ' ' + extra : '') + '" data-action="' + action + '" data-ref="' + _escAttr(ref) + '"><i data-lucide="' + icon + '"></i>' + label + '</button>';
}

function _kindLabel(kind) {
  const o = KIND_OPTIONS.find(x => x.v === kind); return o ? o.t : 'Machine';
}
function _kindIcon(kind) {
  switch (kind) {
    case 'termux': return 'smartphone';
    case 'windows': return 'monitor';
    case 'mac': return 'laptop';
    case 'site': return 'globe';
    case 'linux': return 'terminal';
    default: return 'hard-drive';
  }
}
// Google Compute states → friendly label + status-dot class.
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

// ── Body interactions (delegated) ──
function _onBodyClick(e) {
  const action = e.target.closest('[data-action]');
  if (action) { if (state.busy) return; const [provider, zone, name] = (action.dataset.ref || '').split('|'); _doAction(action.dataset.action, provider, zone, name); return; }
  const ping = e.target.closest('[data-ping]');
  if (ping) { _doPing(ping.dataset.ping, ping.dataset.card, ping); return; }
  const edit = e.target.closest('[data-entry-edit]');
  if (edit) { _editEntry(edit.dataset.entryEdit); return; }
  const rm = e.target.closest('[data-entry-remove]');
  if (rm) { _removeEntry(rm.dataset.entryRemove); return; }
  const repo = e.target.closest('[data-repo-edit]');
  if (repo) { _editRepo(repo.dataset.repoEdit); return; }
}

async function _editEntry(id) {
  try {
    const data = await _get('/entries');
    const entry = (data.entries || []).find(e => e.id === id);
    if (entry) _openEntryForm(entry);
  } catch (e) { /* ignore */ }
}

async function _removeEntry(id) {
  if (!window.confirm('Stop tracking this entry?\n\nIt is removed from the Server Manager only — the actual machine or site is not touched.')) return;
  try { await _post('/entries/remove', { id }); await _loadAll(); } catch (e) { alert(e.message); }
}

// Repo edit for cloud VMs + devices (annotation). `data` is "ann:<key>".
async function _editRepo(data) {
  const key = data.slice(4);
  if (!key) return;
  const next = window.prompt('Which code repo does this run?\n\nEnter "owner/repo" or a git URL, or leave blank to clear.');
  if (next === null) return;
  try { await _post('/annotate', { key, repo: next.trim() }); await _loadAll(); }
  catch (e) { alert(e.message); }
}

async function _doPing(address, cardId, btn) {
  const card = cardId ? document.getElementById(cardId) : null;
  const dot = card ? card.querySelector('.cvm-dot') : null;
  const status = card ? card.querySelector('.cvm-status') : null;
  if (dot) dot.className = 'cvm-dot cvm-dot-busy';
  if (status) status.textContent = 'Checking…';
  if (btn) btn.disabled = true;
  try {
    const res = await _post('/ping', { address });
    if (dot) dot.className = 'cvm-dot ' + (res.reachable ? 'cvm-dot-run' : 'cvm-dot-stop');
    if (status) status.textContent = res.detail || (res.reachable ? 'Reachable' : 'Unreachable');
  } catch (e) {
    if (dot) dot.className = 'cvm-dot cvm-dot-stop';
    if (status) status.textContent = e.message;
  } finally { if (btn) btn.disabled = false; }
}

// ── Cloud actions (Start / Stop / Delete) ──
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
  const res = await fetch(apiPath('/admin/server-manager' + path), {
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
