'use strict';

// Dashboard instance header — the full-width card at the top of the Admin Tools
// Dashboard that names the CURRENT instance (this app) with its live database
// connection + health, and a chevron dropdown (styled like the chat panel's
// session selector) to START A NEW instance or SWITCH to another one.
//
// This is the whole "Local Instances" feature relocated onto the Dashboard — no
// separate admin page. The registry + launch/stop engine is app/local_instances.py;
// its admin API is folded into the Dashboard backend (ui/admin-tools/dashboard/
// server.py → /admin/dashboard/instances/*). This module is imported by
// dashboard.js (the page's entry), which calls initInstancesHeader() once and
// feeds it the metrics snapshot via onDashboardSnapshot() so the DB + health pills
// track the same live data the grid shows. All colours come from the design-system
// palette so the header is correct in dark + light.
// REMOVE-WHEN: the instance-switcher header is dropped from the Dashboard.

import { apiPath } from '../../shared/js/config.js';
import { _esc, _escAttr, _refreshLucideIcons } from '../../shared/js/dom-utils.js';

const API = '/admin/dashboard/instances';

// instances : last-loaded list (hub first). editing : id open in the form (null =
// a fresh "add"). menuOpen / formOpen : dropdown + form visibility. busy : a
// start/stop stream is running. lastHealth : the db_health block from the metrics
// snapshot (used to paint the DB + health pills). wired : listeners bound once.
const state = {
  instances: [], hubPort: 8080, editing: null,
  menuOpen: false, formOpen: false, busy: false, lastHealth: null, wired: false,
};

function _qs(id) { return document.getElementById(id); }
function _userId() { try { return localStorage.getItem('auth_user_id') || ''; } catch (e) { return ''; } }
function _byId(id) { return state.instances.find(i => i.id === id) || null; }
function _current() { return state.instances.find(i => i.builtin) || null; }

async function _get(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath(API + path + sep + 'requesting_user_id=' + encodeURIComponent(_userId())));
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
  return data;
}

async function _post(path, body) {
  const res = await fetch(apiPath(API + path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
  return data;
}

// ── Public entry points (called by dashboard.js) ─────────────────────────────
export function initInstancesHeader() {
  _wire();
  _load();
}

// Fed the composite /metrics snapshot each poll so the header's DB + health pills
// show exactly what the grid's Database card shows — no extra backend round-trip.
export function onDashboardSnapshot(snap) {
  if (snap && snap.db_health) { state.lastHealth = snap.db_health; _paintPills(); }
}

// ── Wiring ───────────────────────────────────────────────────────────────────
function _wire() {
  if (state.wired) return; state.wired = true;
  const trigger = _qs('dash-inst-trigger');
  if (trigger) {
    trigger.addEventListener('click', _toggleMenu);
    trigger.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggleMenu(); }
      if (e.key === 'Escape') _closeMenu();
    });
  }
  _qs('dash-inst-menu')?.addEventListener('click', _onMenuClick);
  _qs('dash-inst-form')?.addEventListener('click', _onFormClick);
  // Close the dropdown on an outside click (mirrors the session selector).
  document.addEventListener('click', (e) => {
    if (!state.menuOpen) return;
    const wrap = _qs('dash-inst');
    if (wrap && !wrap.contains(e.target)) _closeMenu();
  });
}

// ── Load + render ────────────────────────────────────────────────────────────
async function _load() {
  try {
    const data = await _get('');
    state.instances = data.instances || [];
    state.hubPort = data.hub_port || 8080;
  } catch (e) {
    _setCurrentError(e.message);
    return;
  }
  _paintCurrent();
  _paintPills();
  if (state.menuOpen) _renderMenu();
}

// The always-visible card: name + "localhost:<port>" of the current (hub) instance.
function _paintCurrent() {
  const cur = _current();
  const name = _qs('dash-inst-name');
  const sub = _qs('dash-inst-sub');
  if (name) name.textContent = cur ? cur.label : 'This app';
  if (sub) sub.textContent = 'localhost:' + (cur ? cur.port : state.hubPort);
  const dot = _qs('dash-inst-dot');
  if (dot) dot.className = 'dash-inst-dot dash-inst-dot-run';
}

function _setCurrentError(msg) {
  const sub = _qs('dash-inst-sub');
  if (sub) sub.textContent = msg || 'Could not load instances';
}

// DB connection + health pills, painted from the metrics snapshot's db_health.
function _paintPills() {
  const h = state.lastHealth || {};
  const db = _qs('dash-inst-db');
  const health = _qs('dash-inst-health');
  if (db) {
    const provider = (h.actual || h.provider || '—');
    const conn = h.degraded ? 'degraded → local'
      : (h.remote ? 'connected' : 'local')
        + (h.hybrid ? ' · hybrid' : '');
    const tone = h.degraded ? 'danger' : (h.ok === false ? 'warning' : 'success');
    db.className = 'dash-inst-pill tone-' + tone;
    db.innerHTML = '<i data-lucide="database" style="width:12px;height:12px;"></i>'
      + '<span>' + _esc(String(provider).toUpperCase()) + ' · ' + _esc(conn) + '</span>';
  }
  if (health) {
    const ok = !h.degraded && h.ok !== false;
    const tone = h.degraded ? 'danger' : (h.ok === false ? 'warning' : 'success');
    const label = h.degraded ? 'Degraded' : (h.ok === false ? 'Checking' : 'Healthy');
    health.className = 'dash-inst-pill tone-' + tone;
    health.innerHTML = '<i data-lucide="' + (ok ? 'heart-pulse' : 'triangle-alert') + '" style="width:12px;height:12px;"></i>'
      + '<span>' + _esc(label) + '</span>';
  }
  _refreshLucideIcons(_qs('dash-inst-pills'));
}

// ── The dropdown menu ────────────────────────────────────────────────────────
function _toggleMenu() { state.menuOpen ? _closeMenu() : _openMenu(); }

function _openMenu() {
  state.menuOpen = true;
  const menu = _qs('dash-inst-menu');
  const trigger = _qs('dash-inst-trigger');
  if (trigger) trigger.setAttribute('aria-expanded', 'true');
  if (menu) menu.hidden = false;
  _qs('dash-inst')?.classList.add('open');
  _renderMenu();
  _load();   // refresh statuses while the menu is open
}

function _closeMenu() {
  state.menuOpen = false;
  const menu = _qs('dash-inst-menu');
  const trigger = _qs('dash-inst-trigger');
  if (trigger) trigger.setAttribute('aria-expanded', 'false');
  if (menu) menu.hidden = true;
  _qs('dash-inst')?.classList.remove('open');
}

function _renderMenu() {
  const menu = _qs('dash-inst-menu');
  if (!menu) return;
  const rows = state.instances.map(_menuRow).join('');
  menu.innerHTML =
    _selfBlock()
    + '<div class="dash-inst-menu-sep"></div>'
    + '<div class="dash-inst-menu-cap">Switch instance</div>'
    + rows
    + '<div class="dash-inst-menu-sep"></div>'
    + '<button type="button" class="dash-inst-add-row" data-add>'
    + '<i data-lucide="plus" style="width:16px;height:16px;"></i><span>Start a new instance</span></button>';
  _refreshLucideIcons(menu);
  _paintPills();   // fill the DB + health pills now that they exist in the menu
}

// The status header at the top of the dropdown — this instance's name + url and
// its live DB connection + health pills (#dash-inst-db/#dash-inst-health, painted
// by _paintPills from the metrics snapshot). This is the "more info about itself"
// the header shows when expanded.
function _selfBlock() {
  const cur = _current();
  const name = cur ? cur.label : 'This app';
  const url = 'localhost:' + (cur ? cur.port : state.hubPort);
  return '<div class="dash-inst-self">'
    + '<span class="dash-inst-dot dash-inst-dot-run"></span>'
    + '<span class="dash-inst-self-id">'
    +   '<span class="dash-inst-self-name">' + _esc(name) + '</span>'
    +   '<span class="dash-inst-self-sub">' + _esc(url) + '</span>'
    + '</span>'
    + '<span class="dash-inst-pills" id="dash-inst-pills">'
    +   '<span class="dash-inst-pill" id="dash-inst-db"><i data-lucide="database" style="width:12px;height:12px;"></i><span>Database…</span></span>'
    +   '<span class="dash-inst-pill" id="dash-inst-health"><i data-lucide="heart-pulse" style="width:12px;height:12px;"></i><span>Health…</span></span>'
    + '</span>'
    + '</div>';
}

function _menuRow(i) {
  const st = _statusMeta(i.status);
  const running = i.status === 'running';
  const isCurrent = !!i.builtin;

  const badges = [];
  if (!i.builtin && i.is_checkout === false) badges.push('<span class="dash-inst-tag warn">Folder missing</span>');
  else if (!i.builtin && i.port_aware === false) badges.push('<span class="dash-inst-tag warn">Needs update</span>');

  // Right-side actions differ by state — mirrors the session row's trailing controls.
  const actions = [];
  if (isCurrent) {
    actions.push('<span class="dash-inst-tag cur">current</span>');
  } else {
    if (running) {
      actions.push(_iconBtn('open', i.id, 'external-link', 'Open in a new tab'));
      actions.push(_iconBtn('stop', i.id, 'square', 'Stop'));
    } else {
      actions.push(_iconBtn('start', i.id, 'play', 'Start'));
    }
    actions.push(_iconBtn('edit', i.id, 'pencil', 'Edit'));
    actions.push(_iconBtn('remove', i.id, 'trash-2', 'Remove', 'danger'));
  }

  // Clicking the row body switches to that instance when it's running (same tab),
  // starts it when it's a stopped registered checkout, and does nothing for the
  // current one. The trailing icon buttons handle the explicit actions.
  const rowAction = isCurrent ? '' : (running ? 'switch' : 'start');
  return '<div class="dash-inst-row' + (isCurrent ? ' is-current' : '') + '"'
    + ' data-row="' + _escAttr(i.id) + '" data-rowaction="' + rowAction + '">'
    + '<span class="dash-inst-row-dot ' + st.dot + '" title="' + _escAttr(st.label) + '"></span>'
    + '<span class="dash-inst-row-main">'
    +   '<span class="dash-inst-row-name">' + _esc(i.label) + badges.join('') + '</span>'
    +   '<span class="dash-inst-row-meta">localhost:' + i.port + ' · ' + _esc(st.label) + '</span>'
    + '</span>'
    + '<span class="dash-inst-row-actions">' + actions.join('') + '</span>'
    + '</div>';
}

function _iconBtn(action, id, icon, title, extra) {
  return '<button type="button" class="dash-inst-ico' + (extra ? ' ' + extra : '') + '"'
    + ' data-action="' + action + '" data-id="' + _escAttr(id) + '" title="' + _escAttr(title) + '"'
    + ' aria-label="' + _escAttr(title) + '"><i data-lucide="' + icon + '" style="width:14px;height:14px;"></i></button>';
}

function _statusMeta(status) {
  switch (status) {
    case 'running': return { label: 'Running', dot: 'dash-inst-dot-run' };
    case 'busy': return { label: 'Port busy', dot: 'dash-inst-dot-busy' };
    default: return { label: 'Stopped', dot: 'dash-inst-dot-stop' };
  }
}

// ── Menu interactions (delegated) ────────────────────────────────────────────
function _onMenuClick(e) {
  if (e.target.closest('[data-add]')) { _openForm(null); return; }

  const ico = e.target.closest('[data-action]');
  if (ico) {
    e.stopPropagation();
    const { action, id } = ico.dataset;
    if (state.busy && (action === 'start' || action === 'stop')) return;
    if (action === 'open') _openInNewTab(id);
    else if (action === 'edit') _openForm(_byId(id));
    else if (action === 'remove') _removeInstance(id);
    else if (action === 'start' || action === 'stop') _runAction(action, id);
    return;
  }

  const row = e.target.closest('[data-row]');
  if (row) {
    const id = row.dataset.row;
    const act = row.dataset.rowaction;
    if (act === 'switch') _switchTo(id);
    else if (act === 'start') { if (!state.busy) _runAction('start', id); }
  }
}

// ── Switching (point the browser at another instance) ────────────────────────
function _switchTo(id) {
  const inst = _byId(id);
  if (!inst || inst.builtin) return;
  window.location.href = inst.url;   // same tab — "switch which repo localhost shows"
}
function _openInNewTab(id) {
  const inst = _byId(id);
  if (inst) window.open(inst.url, '_blank', 'noopener');
}

// ── Add / edit form ──────────────────────────────────────────────────────────
function _openForm(inst) {
  state.editing = inst ? inst.id : null;
  state.formOpen = true;
  const form = _qs('dash-inst-form');
  if (!form) return;
  const suggestedPort = (inst && inst.port) || _nextFreePort();
  form.innerHTML =
      '<div class="dash-inst-field">'
    +   '<label class="dash-inst-flabel">Repo folder <span class="dash-inst-req">*</span></label>'
    +   '<input type="text" class="dash-inst-input" data-ff="folder" value="' + _escAttr((inst && inst.folder) || '') + '" placeholder="C:\\path\\to\\other-webagent" autocomplete="off" spellcheck="false">'
    +   '<div class="dash-inst-tip" data-ff-check>Another WebAgent checkout on this machine.</div>'
    + '</div>'
    + '<div class="dash-inst-field">'
    +   '<label class="dash-inst-flabel">Name</label>'
    +   '<input type="text" class="dash-inst-input" data-ff="label" value="' + _escAttr((inst && inst.label) || '') + '" placeholder="Defaults to the folder name" autocomplete="off">'
    + '</div>'
    + '<div class="dash-inst-field">'
    +   '<label class="dash-inst-flabel">Port <span class="dash-inst-req">*</span></label>'
    +   '<input type="number" class="dash-inst-input" data-ff="port" value="' + _escAttr(String(suggestedPort)) + '" min="1024" max="65535" placeholder="8081">'
    +   '<div class="dash-inst-tip">A free port, different from this app’s (' + state.hubPort + ').</div>'
    + '</div>'
    + '<div class="dash-inst-form-actions">'
    +   '<button type="button" class="dash-inst-btn primary" data-ff-save><i data-lucide="check" style="width:14px;height:14px;"></i> ' + (inst ? 'Save' : 'Add') + '</button>'
    +   '<button type="button" class="dash-inst-btn" data-ff-cancel><i data-lucide="x" style="width:14px;height:14px;"></i> Cancel</button>'
    +   '<span class="dash-inst-form-status" data-ff-status></span>'
    + '</div>';
  form.hidden = false;
  _refreshLucideIcons(form);
  form.querySelector('[data-ff="folder"]')?.addEventListener('blur', _checkFolder);
  form.querySelector('[data-ff="folder"]')?.focus();
}

function _closeForm() {
  state.editing = null; state.formOpen = false;
  const form = _qs('dash-inst-form');
  if (form) { form.hidden = true; form.innerHTML = ''; }
}

function _nextFreePort() {
  const used = new Set(state.instances.map(i => i.port));
  let p = 8081;
  while (used.has(p) || p === state.hubPort) p++;
  return p;
}

async function _checkFolder() {
  const form = _qs('dash-inst-form'); if (!form) return;
  const tip = form.querySelector('[data-ff-check]');
  const folder = (form.querySelector('[data-ff="folder"]')?.value || '').trim();
  if (!folder || !tip) return;
  tip.className = 'dash-inst-tip';
  tip.textContent = 'Checking…';
  try {
    const res = await _post('/validate', { folder });
    if (!res.is_webagent) { tip.className = 'dash-inst-tip err'; tip.textContent = res.message; return; }
    if (!res.port_aware) { tip.className = 'dash-inst-tip warn'; tip.textContent = res.message; return; }
    tip.className = 'dash-inst-tip ok'; tip.textContent = 'Looks good — a WebAgent checkout ready to run on its own port.';
  } catch (e) { tip.className = 'dash-inst-tip err'; tip.textContent = e.message; }
}

function _onFormClick(e) {
  if (e.target.closest('[data-ff-cancel]')) { _closeForm(); return; }
  if (e.target.closest('[data-ff-save]')) { _saveForm(); return; }
}

function _formStatus(text, kind) {
  const el = document.querySelector('#dash-inst-form [data-ff-status]'); if (!el) return;
  el.textContent = text || '';
  el.className = 'dash-inst-form-status' + (kind === 'ok' ? ' ok' : kind === 'err' ? ' err' : '');
}

async function _saveForm() {
  const form = _qs('dash-inst-form'); if (!form) return;
  const val = (k) => { const el = form.querySelector('[data-ff="' + k + '"]'); return el ? (el.value || '').trim() : ''; };
  const folder = val('folder');
  const port = parseInt(val('port'), 10);
  if (!folder) { _formStatus('Enter the repo folder first.', 'err'); return; }
  if (!port) { _formStatus('Enter a port.', 'err'); return; }
  _formStatus('Saving…');
  try {
    if (state.editing) await _post('/update', { id: state.editing, label: val('label'), folder, port });
    else await _post('/add', { label: val('label'), folder, port });
    _closeForm();
    await _load();
    if (state.menuOpen) _renderMenu();
  } catch (e) { _formStatus(e.message, 'err'); }
}

// ── Remove ───────────────────────────────────────────────────────────────────
async function _removeInstance(id) {
  const inst = _byId(id);
  const running = inst && inst.status === 'running';
  const msg = running
    ? 'Remove this instance from the list?\n\nIt is still RUNNING — removing only forgets it here; it keeps running until you Stop it or close its window.'
    : 'Remove this instance from the list?\n\nThe repo folder on disk is not touched.';
  if (!window.confirm(msg)) return;
  try { await _post('/remove', { id }); await _load(); if (state.menuOpen) _renderMenu(); }
  catch (e) { window.alert(e.message); }
}

// ── Start / Stop (NDJSON stream to the live log) ─────────────────────────────
async function _runAction(action, id) {
  const inst = _byId(id); if (!inst) return;
  if (action === 'stop' && !window.confirm('Stop “' + inst.label + '”?\n\nIts server process (port ' + inst.port + ') is shut down. Anyone using it is disconnected.')) return;
  state.busy = true;
  _logReset();
  _logLine((action === 'stop' ? 'Stopping ' : 'Starting ') + inst.label + '…');
  try {
    await _stream('/' + action, { id }, (result) => {
      _logLine(result.message || (result.ok ? 'Done.' : 'Failed.'), result.ok ? 'ok' : 'err');
    });
  } catch (e) { _logLine(e.message, 'err'); }
  finally {
    state.busy = false;
    await _load();
    if (state.menuOpen) _renderMenu();
  }
}

function _logReset() { const log = _qs('dash-inst-log'); if (log) { log.hidden = false; log.textContent = ''; } }
function _logLine(text, level) {
  const log = _qs('dash-inst-log'); if (!log) return;
  const mark = level === 'ok' ? '✓ ' : level === 'err' ? '✗ ' : level === 'warn' ? '! ' : '· ';
  log.textContent += mark + text + '\n';
  log.scrollTop = log.scrollHeight;
}

async function _stream(path, body, onDone) {
  const res = await fetch(apiPath(API + path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch (e) {}
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
      try { ev = JSON.parse(line); } catch (e) { continue; }
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
