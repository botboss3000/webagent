'use strict';

/**
 * Deploy — App Configuration → Data Settings → Deployment card.
 *
 * Live one-click deploy of this app onto a cloud target (Google VM today; AWS,
 * a plain Linux box and Docker are drop-in targets to follow). Talks to
 * /admin/deploy/* : lists the discovered targets + their forms, saves the
 * non-secret settings, saves the cloud key into the encrypted vault, tests the
 * connection, and streams the deploy / tear-down as NDJSON into a live log.
 *
 * The cloud key is used for one deploy then auto-discarded (the per-target
 * "Forget keys after deploy" setting, default on). Admin-only; the Deploy and
 * Tear-down actions are confirm-gated (they create / delete billable resources).
 *
 * Colours use design-system variables so the card is correct in dark + light.
 * Mirrors the structure of ui/shared/js/remote-access.js.
 */

import { apiPath } from '../../../shared/js/config.js';
import { isAdmin } from '../../../shared/js/left-login.js';
import { _refreshLucideIcons, _esc, _escAttr } from '../../../shared/js/dom-utils.js';
import { copyText } from '../../../shared/js/clipboard.js';

let _catalog = null;        // last /catalog payload
let _busy = false;          // a deploy / tear-down stream is running

// ── Manual install row state (one QR popover open at a time across all rows) ──
let _qrTimer = null;        // debounce timer for the (server-side) QR refresh
let _qrPop = null;          // the floating QR popover element, or null
let _qrAnchor = null;       // the QR button it was opened from
let _qrDesc = null;         // which manual-row descriptor the open QR belongs to

function _qs(id) { return document.getElementById(id); }
function _userId() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }
function _provider() { return (_qs('ac-deploy-provider') || {}).value || ''; }
function _current() { return (_catalog && (_catalog.providers || []).find(p => p.id === _provider())) || null; }

// ── Shared repo details ──────────────────────────────────────────────────────
// The repo choice lives ONCE in the Repo-details bar (#ac-deploy-repo-*) and is
// carried into EVERY target: the manual command boxes read it (via _manualInputs),
// and the cloud deploy has it injected into its config/creds (see _saveSettings).
// The token + admin password are never stored; only the URL + visibility persist
// (to the reserved "_repo" slot) so the bar pre-fills next time.
function _readSharedRepo() {
  return {
    github_url: (_qs('ac-deploy-repo-url')?.value || '').trim(),
    visibility: _qs('ac-deploy-repo-visibility')?.value || 'public',
    token: (_qs('ac-deploy-repo-token')?.value || '').trim(),
    admin_password: _qs('ac-deploy-repo-admin-pw')?.value || '',
  };
}
// Show the access-token field only when the repo is Private.
function _syncSharedToken() {
  const vis = _qs('ac-deploy-repo-visibility');
  const wrap = _qs('ac-deploy-repo-token-wrap');
  if (wrap) wrap.style.display = (vis && vis.value === 'private') ? '' : 'none';
}

// Cloud config/cred keys now OWNED by the shared Repo-details bar — skipped when
// rendering a cloud target's own forms, then injected on save/deploy. `branch` is
// dropped from the UI entirely (defaults to main, like the manual targets).
const SHARED_CLOUD_KEYS = new Set(['repo_url', 'branch', 'visibility', 'github_token', 'admin_password', 'github_url']);

// The five deploy-target panels revealed by the #ac-deploy-target dropdown. The
// three manual values (termux/windows/macos) match MANUAL_ROWS ids so a manual
// selection re-renders its command box.
const TARGET_PANELS = [
  { target: 'cloud', panel: 'ac-deploy-cloud-panel' },
  { target: 'local', panel: 'ac-deploy-local-panel' },
  { target: 'termux', panel: 'ac-deploy-linux-panel' },
  { target: 'windows', panel: 'ac-deploy-win-panel' },
  { target: 'macos', panel: 'ac-deploy-mac-panel' },
];
function _syncTargetPanel() {
  const t = _qs('ac-deploy-target')?.value || 'cloud';
  TARGET_PANELS.forEach(p => { const el = _qs(p.panel); if (el) el.hidden = (p.target !== t); });
  const desc = MANUAL_ROWS.find(d => d.id === t);   // re-render the newly-shown command
  if (desc) _manualRender(desc);
}

function _setStatus(msg, kind) {
  const el = _qs('ac-deploy-status');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = kind === 'ok' ? 'var(--success)' : kind === 'err' ? 'var(--danger)' : '';
}

async function _get(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath('/admin/deploy' + path + sep + 'requesting_user_id=' + encodeURIComponent(_userId())));
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
  return data;
}

async function _post(path, body) {
  const res = await fetch(apiPath(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
  return data;
}

// ── Load + render ──────────────────────────────────────────────────────────
async function _load() {
  if (!isAdmin()) return;
  try {
    const res = await fetch(apiPath('/admin/deploy/catalog?requesting_user_id=' + encodeURIComponent(_userId())));
    if (!res.ok) return;          // 403 for non-admins — leave the card quiet
    _catalog = await res.json();
  } catch { return; }
  _renderAll();
  _loadInstances();               // the local-deployments list (hub + siblings)
}

function _renderAll() {
  if (!_catalog) return;
  const sel = _qs('ac-deploy-provider');
  const providers = _catalog.providers || [];
  // The cloud-deploy dropdown lists only CLOUD targets. "Manual" targets
  // (Linux/Termux, Windows, macOS) each get their own dedicated row below —
  // see _initManualRows / MANUAL_ROWS.
  const cloud = providers.filter(p => !p.manual);

  const badge = _qs('ac-deploy-badge');
  if (badge) badge.textContent = cloud.length
    ? (cloud.length + (cloud.length === 1 ? ' target' : ' targets')) : 'no targets';

  if (sel) {
    const want = (cloud.some(p => p.id === _catalog.active_provider)
      ? _catalog.active_provider : (cloud[0] && cloud[0].id)) || '';
    sel.innerHTML = cloud.map(p =>
      `<option value="${_esc(p.id)}"${p.available ? '' : ' disabled'}>` +
      `${_esc(p.display_name)}${p.available ? '' : ' — unavailable'}</option>`).join('');
    sel.value = want;
  }
  _prefillSharedRepo();
  _syncSharedToken();
  _renderProvider();
  _renderManualPrefill();
  _syncTargetPanel();
}

// Pre-fill the shared Repo-details bar from the reserved "_repo" slot (URL +
// visibility only; the token + admin password are never persisted). Only fills a
// blank field, so it never clobbers something the admin is mid-typing.
function _prefillSharedRepo() {
  const repo = (_catalog && _catalog.shared_repo) || {};
  const url = _qs('ac-deploy-repo-url');
  const vis = _qs('ac-deploy-repo-visibility');
  if (url && !url.value && repo.github_url) url.value = repo.github_url;
  if (vis && repo.visibility) vis.value = repo.visibility;
}

function _renderProvider() {
  _hideTip();                    // drop any help bubble from the old form
  const p = _current();
  const blurb = _qs('ac-deploy-blurb');
  const requires = _qs('ac-deploy-requires');
  const cfgHost = _qs('ac-deploy-config');
  const credHost = _qs('ac-deploy-creds');
  const keyNote = _qs('ac-deploy-key-note');
  const result = _qs('ac-deploy-result');
  const destroyBtn = _qs('ac-deploy-destroy');
  if (!p) {
    if (cfgHost) cfgHost.innerHTML = '';
    if (credHost) credHost.innerHTML = '';
    return;
  }

  if (blurb) blurb.textContent = p.summary || '';
  if (requires) {
    requires.innerHTML = (p.requires && p.requires.length)
      ? 'You’ll need: ' + p.requires.map(_esc).join(' · ')
      : '';
  }
  if (!p.available && blurb) {
    blurb.innerHTML = '<span style="color:var(--danger);">' + _esc(p.unavailable_reason || 'This target is unavailable here.') + '</span>';
  }

  // Settings form (non-secret)
  if (cfgHost) {
    cfgHost.innerHTML = '';
    (p.config_fields || []).forEach(f => cfgHost.appendChild(_buildField(f, (p.config || {})[f.key], 'cfg')));
  }
  // The optional admin password gets its OWN slot (rendered ABOVE the cloud key),
  // not the generic cloud-key list — it isn't a cloud credential, it's a deploy
  // setting. It's still a secret (rides the vault, discarded after deploy), so it's
  // gathered as a 'cred'. Rendered here from the same credential_fields descriptor.
  const adminHost = _qs('ac-deploy-admin');
  if (adminHost) {
    adminHost.innerHTML = '';
    const af = (p.credential_fields || []).find(f => f.key === 'admin_password');
    if (af) {
      const isSet = !!(p.credentials_set || {})[af.key];
      adminHost.appendChild(_buildField(af, '', 'cred', isSet));
    }
  }
  // Cloud-key form (secret); a "set" secret shows a placeholder, never the value.
  // admin_password is skipped here — it lives in its own slot above.
  if (credHost) {
    credHost.innerHTML = '';
    (p.credential_fields || []).forEach(f => {
      if (f.key === 'admin_password') return;
      const isSet = !!(p.credentials_set || {})[f.key];
      credHost.appendChild(_buildField(f, '', 'cred', isSet));
    });
  }
  if (keyNote) {
    keyNote.textContent = p.configured
      ? 'A cloud key is saved. Deploying will use it; leave the field blank to keep it.'
      : 'No cloud key saved yet — paste one to enable Deploy.';
  }
  _renderServers(p);            // the "Saved servers" picker (SSH target only)

  // Tear-down only when there's a recorded server
  const dep = p.deployment || {};
  if (destroyBtn) destroyBtn.style.display = dep.server ? '' : 'none';
  if (result) result.innerHTML = _deploymentLine(dep);
  _setStatus('');
  if (window.lucide) { try { lucide.createIcons(); } catch {} }
}

// ── Saved servers (profile-aware targets, e.g. the SSH one) ──────────────────
// A dropdown of named servers whose address + login are kept (secrets encrypted
// in the vault). Picking one LOADS it into the form (the deploy runtime then acts
// on it); "Save server" stores the current form as a named server; "Delete"
// removes one. Every Google VM you create is auto-added server-side. The whole
// block is hidden for targets that aren't profile-aware (p.saved_servers false).
function _renderServers(p) {
  const wrap = _qs('ac-deploy-servers-wrap');
  const sel = _qs('ac-deploy-servers');
  if (!wrap || !sel) return;
  if (!p || !p.saved_servers) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';

  const servers = p.servers || [];
  const active = p.active_server || '';
  const opts = ['<option value="">＋ New server…</option>'];
  servers.forEach(s => {
    const tag = s.host ? ' — ' + s.host : '';
    const src = s.source === 'google_vm' ? ' (Google VM)' : '';
    opts.push('<option value="' + _escAttr(s.id) + '">' + _esc((s.label || s.host || 'Server') + tag + src) + '</option>');
  });
  sel.innerHTML = opts.join('');
  sel.value = active;

  const cur = servers.find(s => s.id === active);
  const labelInp = _qs('ac-deploy-server-label');
  if (labelInp) labelInp.value = cur ? (cur.label || '') : '';
  const delBtn = _qs('ac-deploy-server-delete');
  if (delBtn) delBtn.style.display = active ? '' : 'none';
}

// Pick a saved server (or "New server…") → load it into the working form.
async function _onServerSelect() {
  if (!isAdmin()) return;
  const server_id = (_qs('ac-deploy-servers') || {}).value || '';
  _setStatus(server_id ? 'Loading server…' : 'Cleared — enter a new server below.');
  try {
    await _post('/admin/deploy/servers/select', { provider: _provider(), server_id });
    await _load();                 // re-render the form filled from the loaded server
    _setStatus('');
  } catch (e) { _setStatus(e.message, 'err'); }
}

// Save the current form as a server (updates the selected one, or creates a new
// one when "New server…" is selected). Secrets left blank keep the stored login.
async function _onServerSave() {
  if (!isAdmin()) return;
  const server_id = (_qs('ac-deploy-servers') || {}).value || '';
  const label = (_qs('ac-deploy-server-label')?.value || '').trim();
  const values = { ..._gather('cfg'), ..._gather('cred') };
  if (!String(values.host || '').trim()) { _setStatus('Enter the server address first.', 'err'); return; }
  _setStatus('Saving server…');
  try {
    await _post('/admin/deploy/servers/save', { provider: _provider(), server_id, label, values });
    await _load();
    _setStatus('Server saved.', 'ok');
  } catch (e) { _setStatus(e.message, 'err'); }
}

// Remove a saved server + its stored login (the machine itself is untouched).
async function _onServerDelete() {
  if (!isAdmin()) return;
  const server_id = (_qs('ac-deploy-servers') || {}).value || '';
  if (!server_id) return;
  const cur = ((_current() || {}).servers || []).find(s => s.id === server_id);
  if (!window.confirm('Delete saved server “' + ((cur && cur.label) || server_id) + '”?\n\n'
      + 'This removes it from the list and forgets its stored login. The server itself is not touched.')) return;
  _setStatus('Deleting…');
  try {
    await _post('/admin/deploy/servers/delete', { provider: _provider(), server_id });
    await _load();
    _setStatus('Deleted.', 'ok');
  } catch (e) { _setStatus(e.message, 'err'); }
}

// ── Current deployment + other local deployments ─────────────────────────────
// The list at the top of the Deploy card: this app (the hub) plus any registered
// sibling WebAgent checkouts on this machine, each shown as an expandable bar with
// a live status dot and a "Port N · folder" line. The hub bar expands to a port
// editor (change + relaunch this app); a sibling bar to Open / Start / Stop / edit
// / Remove. Backed by /admin/deploy/instances/* (app/local_instances.py) — the same
// registry the Dashboard's instance header uses. The rows are BUILT here (count
// varies), so their expand + actions are wired by delegation, not _wireBootRow.
let _insts = [];                 // last-loaded instances (hub first)
let _instHubPort = 8080;         // this app's current port
let _instBusy = false;           // a start / stop / relaunch is running
const _instExpanded = new Set(); // ids whose bar is expanded (survives re-render)

async function _loadInstances() {
  const host = _qs('ac-deploy-instances');
  if (!host || !isAdmin()) return;
  try {
    const data = await _get('/instances');
    _insts = data.instances || [];
    _instHubPort = data.hub_port || 8080;
  } catch (e) {
    host.innerHTML = '<div class="ac-hint" style="padding:10px 14px;color:var(--danger);">' + _esc(e.message) + '</div>';
    return;
  }
  _renderInstances();
  _seedRegisterPort();
}

function _instDotClass(status) {
  return status === 'running' ? 'ac-deploy-dot-run'
    : status === 'busy' ? 'ac-deploy-dot-busy' : 'ac-deploy-dot-stop';
}
function _instStatusLabel(status) {
  return status === 'running' ? 'Running' : status === 'busy' ? 'Port busy' : 'Stopped';
}

function _renderInstances() {
  const host = _qs('ac-deploy-instances');
  if (!host) return;
  host.innerHTML = _insts.map(_instBar).join('') || '';
  _refreshLucideIcons(host);
}

// One instance = an expandable `.ac-row`. Collapsed: status dot, name + badges, a
// right-hand status word, and the "Port N · folder" line. Expanded body differs
// for the hub (port editor) vs a sibling (actions + inline edit).
function _instBar(i) {
  const expanded = _instExpanded.has(i.id);
  const badges = [];
  if (i.builtin) badges.push('<span class="ac-deploy-badge ac-deploy-badge-app">This app</span>');
  if (!i.builtin && i.is_checkout === false) badges.push('<span class="ac-deploy-badge ac-deploy-badge-warn">Folder missing</span>');
  else if (!i.builtin && i.port_aware === false) badges.push('<span class="ac-deploy-badge ac-deploy-badge-warn">Needs update</span>');

  const meta = '<span class="ac-deploy-meta-port">Port ' + _esc(String(i.port)) + '</span>'
    + '<span class="ac-deploy-meta-folder">' + _esc(i.folder) + '</span>';

  return '<div class="ac-row ac-deploy-inst' + (expanded ? ' expanded' : '') + '" data-inst="' + _escAttr(i.id) + '">'
    + '<div class="ac-ability-row">'
    +   '<span class="ac-ability-icon"><span class="ac-deploy-dot ' + _instDotClass(i.status) + '" title="' + _escAttr(_instStatusLabel(i.status)) + '"></span></span>'
    +   '<div class="ac-ability-label">'
    +     '<div class="ac-ability-name">' + _esc(i.label) + badges.join('')
    +       '<span class="ac-deploy-inst-status">' + _esc(_instStatusLabel(i.status)) + '</span>'
    +     '</div>'
    +     '<div class="ac-ability-desc">' + meta + '</div>'
    +   '</div>'
    +   '<span class="ac-row-chevron"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg></span>'
    + '</div>'
    + '<div class="ac-ability-body">' + (i.builtin ? _hubBody(i) : _siblingBody(i)) + '</div>'
    + '</div>';
}

// The hub's body: change this app's own port (persist + relaunch).
function _hubBody(i) {
  return '<label class="ac-label">Change this app’s port</label>'
    + '<input class="ac-input" type="number" min="1024" max="65535" data-hub-port value="' + _escAttr(String(i.port)) + '">'
    + '<div class="ac-hint" style="font-size:11px;margin-top:4px;">Each deployment needs its own port. Changing this <strong>restarts WebAgent</strong> on the new port — this connection drops and you reopen it at the new address.</div>'
    + '<div class="ac-ra-actions" style="margin-top:10px;">'
    +   '<button class="ac-btn ac-deploy-go-btn" type="button" data-hub-save>Change &amp; relaunch</button>'
    +   '<span class="ac-hint" data-hub-status style="margin-left:6px;"></span>'
    + '</div>';
}

// A sibling's body: Open (running) / Start / Stop / Remove, plus inline edit of its
// name / folder / port.
function _siblingBody(i) {
  const acts = [];
  if (i.status === 'running') acts.push('<a class="ac-btn" href="' + _escAttr(i.url) + '" target="_blank" rel="noopener" data-open><i data-lucide="external-link"></i> Open</a>');
  if (i.status === 'running') acts.push('<button class="ac-btn" type="button" data-stop><i data-lucide="square"></i> Stop</button>');
  else acts.push('<button class="ac-btn ac-deploy-go-btn" type="button" data-start><i data-lucide="play"></i> Start</button>');
  acts.push('<button class="ac-btn ac-deploy-danger-btn" type="button" data-remove><i data-lucide="trash-2"></i> Remove</button>');

  let warn = '';
  if (i.is_checkout === false) warn = '<div class="ac-hint" style="color:var(--danger);font-size:11.5px;">This folder is no longer a WebAgent checkout — fix the path below.</div>';
  else if (i.port_aware === false) warn = '<div class="ac-hint" style="color:var(--warning, var(--danger));font-size:11.5px;">This checkout is too old to run on a custom port — update it (git pull) before starting.</div>';

  return '<div class="ac-ra-actions" style="margin-bottom:8px;">' + acts.join('') + '</div>'
    + warn
    + '<label class="ac-label">Name</label>'
    + '<input class="ac-input" type="text" data-edit-label value="' + _escAttr(i.label) + '">'
    + '<label class="ac-label" style="margin-top:8px;">Repo folder</label>'
    + '<input class="ac-input" type="text" data-edit-folder value="' + _escAttr(i.folder) + '" spellcheck="false">'
    + '<label class="ac-label" style="margin-top:8px;">Port</label>'
    + '<input class="ac-input" type="number" min="1024" max="65535" data-edit-port value="' + _escAttr(String(i.port)) + '">'
    + '<div class="ac-ra-actions" style="margin-top:10px;">'
    +   '<button class="ac-btn" type="button" data-inst-save>Save changes</button>'
    +   '<span class="ac-hint" data-inst-status style="margin-left:6px;"></span>'
    + '</div>';
}

// Delegated clicks across the whole instances list: action buttons first, then a
// head-click toggles that bar (ignoring clicks on controls inside it).
function _onInstClick(e) {
  const row = e.target.closest('.ac-deploy-inst');
  if (!row) return;
  const id = row.dataset.inst;
  if (e.target.closest('[data-open]')) return;                          // native <a> handles it
  if (e.target.closest('[data-start]')) { _instAction('start', id); return; }
  if (e.target.closest('[data-stop]')) { _instAction('stop', id); return; }
  if (e.target.closest('[data-remove]')) { _instRemove(id); return; }
  if (e.target.closest('[data-inst-save]')) { _instSave(id, row); return; }
  if (e.target.closest('[data-hub-save]')) { _hubSave(row); return; }
  const head = e.target.closest('.ac-ability-row');
  if (head && head.parentElement === row) {
    if (e.target.closest('input, textarea, select, button, a, label')) return;
    row.classList.toggle('expanded');
    if (row.classList.contains('expanded')) _instExpanded.add(id); else _instExpanded.delete(id);
  }
}

// Start / Stop a sibling (streams NDJSON into the shared deploy log).
async function _instAction(action, id) {
  if (_instBusy) return;
  const inst = _insts.find(x => x.id === id);
  if (!inst) return;
  if (action === 'stop' && !window.confirm('Stop “' + inst.label + '”?\n\nIts server (port ' + inst.port + ') is shut down; anyone using it is disconnected.')) return;
  _instBusy = true;
  _logReset();
  _logLine((action === 'stop' ? 'Stopping ' : 'Starting ') + inst.label + '…');
  try {
    await _stream('/admin/deploy/instances/' + action, { id }, (result) => {
      _logLine(result.message || (result.ok ? 'Done.' : 'Failed.'), result.ok ? 'ok' : 'err');
    });
  } catch (e) { _logLine(e.message, 'err'); }
  finally { _instBusy = false; await _loadInstances(); }
}

async function _instRemove(id) {
  const inst = _insts.find(x => x.id === id);
  if (!inst) return;
  const running = inst.status === 'running';
  const msg = running
    ? 'Remove “' + inst.label + '” from the list?\n\nIt is still RUNNING — removing only forgets it here; it keeps running until you Stop it.'
    : 'Remove “' + inst.label + '” from the list?\n\nThe repo folder on disk is not touched.';
  if (!window.confirm(msg)) return;
  try { await _post('/admin/deploy/instances/remove', { id }); await _loadInstances(); }
  catch (e) { window.alert(e.message); }
}

async function _instSave(id, row) {
  const status = row.querySelector('[data-inst-status]');
  const label = row.querySelector('[data-edit-label]')?.value || '';
  const folder = (row.querySelector('[data-edit-folder]')?.value || '').trim();
  const port = parseInt(row.querySelector('[data-edit-port]')?.value || '', 10);
  if (status) { status.textContent = 'Saving…'; status.style.color = ''; }
  try {
    await _post('/admin/deploy/instances/update', { id, label, folder, port: port || 0 });
    await _loadInstances();
  } catch (e) { if (status) { status.textContent = e.message; status.style.color = 'var(--danger)'; } }
}

// Change THIS app's port: persist it + relaunch. The server exits right after
// responding, so we replace the body with a "reopen here" link.
async function _hubSave(row) {
  const inp = row.querySelector('[data-hub-port]');
  const status = row.querySelector('[data-hub-status]');
  const port = parseInt(inp?.value || '', 10);
  const setStatus = (m, err) => { if (status) { status.textContent = m || ''; status.style.color = err ? 'var(--danger)' : ''; } };
  if (!port) return setStatus('Enter a port.', true);
  if (port === _instHubPort) return setStatus('That’s already the current port.', true);
  if (!window.confirm('Change this app’s port to ' + port + ' and restart now?\n\n'
      + 'This connection will drop and WebAgent will reopen at http://localhost:' + port + '/')) return;
  setStatus('Saving & restarting…');
  try {
    const r = await _post('/admin/deploy/instances/set-hub-port', { port });
    const url = r.url || ('http://localhost:' + port + '/');
    const body = row.querySelector('.ac-ability-body');
    if (body) {
      body.innerHTML = '<div class="ac-hint" style="line-height:1.6;">'
        + 'WebAgent is restarting on port <strong>' + _esc(String(r.port || port)) + '</strong>. '
        + (r.auto_restart === false
            ? 'Start it again from your server window, then open: '
            : 'It’ll be back in a few seconds — reopen it here: ')
        + '<a href="' + _esc(url) + '" class="ac-deploy-current-link">' + _esc(url) + '</a></div>';
    }
  } catch (e) { setStatus(e.message, true); }
}

// ── Register a local checkout (the add form) ─────────────────────────────────
function _nextFreePort() {
  const used = new Set(_insts.map(i => i.port));
  used.add(_instHubPort);
  let p = 8081;
  while (used.has(p)) p++;
  return p;
}
function _seedRegisterPort() {
  const el = _qs('ac-inst-port');
  if (el && !el.value) el.value = String(_nextFreePort());
}

async function _checkRegisterFolder() {
  const tip = _qs('ac-inst-folder-tip');
  const folder = (_qs('ac-inst-folder')?.value || '').trim();
  if (!folder || !tip) return;
  tip.style.color = '';
  tip.textContent = 'Checking…';
  try {
    const res = await _post('/admin/deploy/instances/validate', { folder });
    if (!res.is_webagent) { tip.style.color = 'var(--danger)'; tip.textContent = res.message; return; }
    if (!res.port_aware) { tip.style.color = 'var(--warning, var(--danger))'; tip.textContent = res.message; return; }
    tip.style.color = 'var(--success)';
    tip.textContent = 'Looks good — a WebAgent checkout ready to run on its own port.';
  } catch (e) { tip.style.color = 'var(--danger)'; tip.textContent = e.message; }
}

async function _registerAdd() {
  const status = _qs('ac-inst-add-status');
  const folder = (_qs('ac-inst-folder')?.value || '').trim();
  const label = _qs('ac-inst-label')?.value || '';
  const port = parseInt(_qs('ac-inst-port')?.value || '', 10);
  const setStatus = (m, kind) => { if (status) { status.textContent = m || ''; status.style.color = kind === 'err' ? 'var(--danger)' : kind === 'ok' ? 'var(--success)' : ''; } };
  if (!folder) return setStatus('Enter the repo folder first.', 'err');
  if (!port) return setStatus('Enter a port.', 'err');
  setStatus('Adding…');
  try {
    await _post('/admin/deploy/instances/add', { label, folder, port });
    if (_qs('ac-inst-folder')) _qs('ac-inst-folder').value = '';
    if (_qs('ac-inst-label')) _qs('ac-inst-label').value = '';
    if (_qs('ac-inst-port')) _qs('ac-inst-port').value = '';
    setStatus('Added.', 'ok');
    _qs('ac-deploy-register-row')?.classList.remove('expanded');
    await _loadInstances();
  } catch (e) { setStatus(e.message, 'err'); }
}

function _initInstances() {
  const host = _qs('ac-deploy-instances');
  if (host && !host.dataset.wired) { host.dataset.wired = '1'; host.addEventListener('click', _onInstClick); }
  const folder = _qs('ac-inst-folder');
  if (folder && !folder.dataset.wired) { folder.dataset.wired = '1'; folder.addEventListener('blur', _checkRegisterFolder); }
  const add = _qs('ac-inst-add');
  if (add && !add.dataset.wired) { add.dataset.wired = '1'; add.addEventListener('click', _registerAdd); }
}

function _deploymentLine(dep) {
  if (!dep || !dep.server) return '';
  const url = dep.public_url || (dep.ip ? ('http://' + dep.ip) : '');
  const state = dep.state === 'installing' ? ' (installing…)' : '';
  let html = 'Last server: <strong>' + _esc(dep.server) + '</strong>' + _esc(state);
  if (url) html += ' — <a href="' + _esc(url) + '" target="_blank" rel="noopener" style="color:var(--accent);">' + _esc(url) + '</a>';
  return html;
}

// Wire a "Copy" button to copy a code element's text. Re-run safe (the button
// carries a `wired` flag so re-renders don't stack listeners). Shared by the
// phone-install row's command box.
function _wireCopy(btn, code) {
  if (!btn || !code || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  btn.addEventListener('click', async () => {
    const text = code.textContent || '';
    try {
      // Use the shared clipboard helper, NOT navigator.clipboard directly: on a
      // phone this panel is reached over http://<device-ip>:8080 (the address the
      // install steps tell you to open), a NON-secure context where
      // navigator.clipboard is undefined — copyText falls back to an execCommand
      // copy there so the button actually works on phones.
      await copyText(text);
      _flashCopied(btn);
    } catch {
      // Last resort: select the text so it can be copied by hand.
      const range = document.createRange();
      range.selectNodeContents(code);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  });
}

// Briefly flash a green check on an icon-only button, then restore its icon.
// We swap the inner Lucide <i>/<svg> rather than setting btn.textContent — the
// latter would delete the icon SVG, leaving the button blank after one copy.
function _flashCopied(btn) {
  if (btn.dataset.flashing) return;
  btn.dataset.flashing = '1';
  btn.innerHTML = '<i data-lucide="check"></i>';
  btn.style.color = 'var(--success)';
  _refreshLucideIcons(btn);
  setTimeout(() => {
    btn.innerHTML = '<i data-lucide="copy"></i>';
    btn.style.color = '';
    _refreshLucideIcons(btn);
    delete btn.dataset.flashing;
  }, 1500);
}

// ── Manual install rows (Linux/Termux, Windows, macOS) ───────────────────────
// Each is its OWN dedicated row below the cloud-deploy row (NOT a cloud provider
// in the dropdown): GitHub URL + public/private (+ token for private) → the one
// command to paste into a terminal / PowerShell, shown LIVE, plus a QR of that
// command. Backed by POST /admin/deploy/command (no cloud account, nothing
// billable; the token is used to build the command and never stored). The
// non-secret URL + visibility persist so each row pre-fills.
//
// All three rows share ONE set of render / QR / copy / tips functions, driven by
// a per-platform descriptor (MANUAL_ROWS): only the element ids, the command
// builder, and the static steps/note text differ. Adding a platform = a drop-in
// app/deploy/providers/<id>.py + one descriptor here + one row in the HTML.

// ── Shared command-building primitives (mirror app/deploy/manual_common.py) ──
// The command is built RIGHT HERE in the browser so the box is NEVER empty and
// updates the instant a field changes — no server round-trip to depend on (an
// un-restarted / unreachable server must not leave the box blank). Each builder
// below is BYTE-IDENTICAL to its provider's `build_command`. The QR is still made
// server-side, on demand (see _fetchQr).
// Blank repository → install the STANDARD WebAgent repository, so the command is
// always ready to run as is (the admin only types a URL for their own fork).
const _MC_DEFAULT_REPO = 'https://github.com/botboss3000/webagent';
const _MC_PLACEHOLDER_TOKEN = 'YOUR_ACCESS_TOKEN';
const _MC_BRANCH = 'main';
// Default install folder per platform (where the repo is cloned + run from when
// the admin doesn't choose one). POSIX expands $HOME / Windows expands
// $env:USERPROFILE inside the double quotes the command wraps them in.
const _MC_DEFAULT_DIR_POSIX = '$HOME/webagent';
const _MC_DEFAULT_DIR_WINDOWS = '$env:USERPROFILE\\webagent';
const _MC_BAD_URL = "'\";\n\r\\ &|`$(){}<>";
const _MC_BAD_TOKEN = "'\";\n\r\\ &|`$(){}<>@/ ";
// Looser than _MC_BAD_URL: a real path legitimately has spaces, \\, :, $, % — only
// reject shell metacharacters. Mirror of manual_common.BAD_DIR.
const _MC_BAD_DIR = "\"'" + "`" + ";\n\r|&<>(){}*?";

function _mcHasBad(s, bad) { for (const c of bad) if (s.indexOf(c) >= 0) return true; return false; }
function _mcStripScheme(u) { return u.replace(/^https?:\/\//, ''); }

// Resolve the install folder from the row input (mirror manual_common.resolve_dir).
function _mcResolveDir(dir, def) {
  const d = (dir || '').trim();
  if (!d) return def;
  if (_mcHasBad(d, _MC_BAD_DIR)) return def;
  return d;
}

// Resolve the clone target from the row inputs (mirror manual_common.resolve_clone).
function _mcResolve(inp) {
  const typed = (inp.github_url || '').trim();
  let defaultRepo = !typed, warning = '', repo;
  if (defaultRepo) { repo = _MC_DEFAULT_REPO; }
  else if (_mcHasBad(typed, _MC_BAD_URL)) {
    repo = _MC_DEFAULT_REPO; defaultRepo = true;
    warning = 'That repository address isn’t a valid URL — using the standard WebAgent repository instead.';
  } else { repo = typed; }
  const priv = (inp.visibility || 'public') === 'private';
  let cloneUrl = repo, placeholderToken = false;
  if (priv) {
    let tok = (inp.token || '').trim();
    if (tok && _mcHasBad(tok, _MC_BAD_TOKEN)) { warning = 'That token contains characters that aren’t valid in a GitHub token.'; tok = ''; }
    if (!tok) { tok = _MC_PLACEHOLDER_TOKEN; placeholderToken = true; }
    cloneUrl = 'https://' + tok + '@' + _mcStripScheme(repo);
  }
  return { repo, cloneUrl, defaultRepo, placeholderToken, warning };
}

// The optional pre-set admin password (mirror manual_common.resolve_admin). Typed
// → carried into the setup script (WA_ADMIN_PW → BOOTSTRAP_ADMIN_PASSWORD in .env).
// Blank → no prefix: the first visitor sets the password on the setup page. An
// unsafe password falls back to first-visitor with a warning rather than splicing
// something that would break the command's quoting.
const _MC_BAD_PW = "'\"" + "`" + "$\\\n\r";
function _mcResolveAdmin(pw) {
  const p = (pw || '').trim();
  if (!p) return { prewire: false, password: '', warning: '' };
  if (_mcHasBad(p, _MC_BAD_PW)) return { prewire: false, password: '',
    warning: 'That password contains characters that can’t be placed in the command safely — use letters, digits and simple punctuation, or leave it blank to let the first visitor set it instead.' };
  const warning = p.length < 6 ? 'The admin password should be at least 6 characters.' : '';
  return { prewire: true, password: p, warning };
}

// Build the shell prefix that carries the pre-set password into the setup script;
// empty when blank. Byte-mirror of the providers' `admin_prefix`.
// POSIX (Termux/macOS): a leading `WA_ADMIN_PW='val' ` env assignment.
function _mcAdminPrefixPosix(a) {
  return a.prewire ? ("WA_ADMIN_PW='" + a.password + "' ") : '';
}
// Windows: a `$env:WA_ADMIN_PW='val'; ` assignment the child powershell inherits.
function _mcAdminPrefixWin(a) {
  return a.prewire ? ("$env:WA_ADMIN_PW='" + a.password + "'; ") : '';
}

// Linux / Termux — mirror app/deploy/providers/termux.py build_command. ONE
// command for both: install git with whatever package manager is present; if the
// folder already holds a clone, re-point its origin at the chosen repo (a graceful
// update — the setup script then pulls), else clone fresh; then hand off to
// deploy/termux-setup.sh which detects Termux vs plain Linux.
function _buildTermux(inp) {
  const r = _mcResolve(inp);
  const a = _mcResolveAdmin(inp.admin_password);
  const directory = _mcResolveDir(inp.install_dir, _MC_DEFAULT_DIR_POSIX);
  const command = 'SUDO=; [ "$(id -u 2>/dev/null)" = 0 ] || SUDO=sudo; '
    + 'D="' + directory + '"; '
    + 'if command -v git >/dev/null 2>&1; then :; '
    + 'elif command -v pkg >/dev/null 2>&1; then pkg install -y git; '
    + 'elif command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update && $SUDO apt-get install -y git; '
    + 'elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y git; '
    + 'elif command -v pacman >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm git; fi; '
    + '{ if [ -d "$D/.git" ]; then git -C "$D" remote set-url origin ' + r.cloneUrl + '; '
    + 'else git clone --depth 1 --branch ' + _MC_BRANCH + ' ' + r.cloneUrl + ' "$D"; fi; } && '
    + _mcAdminPrefixPosix(a) + 'bash "$D/deploy/termux-setup.sh"';
  return { command, directory, defaultRepo: r.defaultRepo, placeholderToken: r.placeholderToken,
    warning: [r.warning, a.warning].filter(Boolean).join(' '), prewire: a.prewire };
}

// Windows — mirror app/deploy/providers/windows.py build_command (a PowerShell
// one-liner: ensure git via winget; re-point an existing clone or clone fresh;
// run the ps1).
function _buildWindows(inp) {
  const r = _mcResolve(inp);
  const a = _mcResolveAdmin(inp.admin_password);
  const directory = _mcResolveDir(inp.install_dir, _MC_DEFAULT_DIR_WINDOWS);
  const command = "$ErrorActionPreference='Stop'; "
    + "$repo='" + r.cloneUrl + "'; $dir=\"" + directory + "\"; "
    + "if(-not(Get-Command git -EA SilentlyContinue)){Write-Host 'Installing Git...'; "
    + "try{winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --silent}catch{}; "
    + "$env:Path=[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')}; "
    + "if(-not(Get-Command git -EA SilentlyContinue)){Write-Host 'Git is required. Install it from https://git-scm.com/download/win then run this again.'; return}; "
    + "if(Test-Path \"$dir\\.git\"){git -C \"$dir\" remote set-url origin $repo}else{git clone --depth 1 --branch " + _MC_BRANCH + " $repo \"$dir\"}; "
    + _mcAdminPrefixWin(a)
    + "powershell -NoProfile -ExecutionPolicy Bypass -File \"$dir\\deploy\\windows-setup.ps1\"";
  return { command, directory, defaultRepo: r.defaultRepo, placeholderToken: r.placeholderToken,
    warning: [r.warning, a.warning].filter(Boolean).join(' '), prewire: a.prewire };
}

// macOS — mirror app/deploy/providers/macos.py build_command (a Terminal
// one-liner: ensure git/Command Line Tools; re-point an existing clone or clone
// fresh; run the sh).
function _buildMac(inp) {
  const r = _mcResolve(inp);
  const a = _mcResolveAdmin(inp.admin_password);
  const directory = _mcResolveDir(inp.install_dir, _MC_DEFAULT_DIR_POSIX);
  const command = 'set -e; D="' + directory + '"; '
    + 'if ! command -v git >/dev/null 2>&1; then '
    + "echo 'Installing the Command Line Tools (a dialog may appear)...'; "
    + 'xcode-select --install 2>/dev/null || true; '
    + "echo 'If a dialog appeared, finish it, then paste this command again.'; fi; "
    + '{ if [ -d "$D/.git" ]; then git -C "$D" remote set-url origin ' + r.cloneUrl + '; '
    + 'else git clone --depth 1 --branch ' + _MC_BRANCH + ' ' + r.cloneUrl + ' "$D"; fi; } && '
    + _mcAdminPrefixPosix(a) + 'bash "$D/deploy/macos-setup.sh"';
  return { command, directory, defaultRepo: r.defaultRepo, placeholderToken: r.placeholderToken,
    warning: [r.warning, a.warning].filter(Boolean).join(' '), prewire: a.prewire };
}

const _TERMUX_STEPS = [
  'On a phone: install the free Termux app, then open it. On a Linux computer: open a terminal.',
  'Scan the QR code or paste the command, then press Enter.',
  'The first run takes a few minutes while it installs everything (on a phone it also sets up a small Ubuntu environment).',
  'When it finishes, open http://localhost:8080 on that device, or http://DEVICE-IP:8080 from another device on the same network (the script prints the address).',
];
const _TERMUX_NOTE = 'On a phone the command installs WebAgent inside a small Ubuntu environment (the reliable way to run the full app on Android); on a Linux computer it installs straight onto the system. Either way it keeps running in the background and restarts itself if it stops. On a Linux computer it also restarts automatically after a reboot; on a phone, install the free Termux:Boot add-on to start it on boot. On a phone it also installs the Server Manager — type webagent in Termux to inspect, restart or diagnose the install. To stop it later: on a phone paste “proot-distro login ubuntu -- pkill -f run.py”, on Linux paste “pkill -f run.py”.';

const _WIN_STEPS = [
  "Open PowerShell: click Start, type 'PowerShell', and open it.",
  'Paste the command and press Enter. (If Windows offers to install Git, allow it.)',
  'The first run takes a few minutes while it downloads Python and installs everything.',
  'When it finishes, open http://localhost:8080 on this PC, or http://THIS-PC-IP:8080 from another device on the same network.',
];
const _WIN_NOTE = 'WebAgent installs into a folder in your user profile and runs in the background as a Scheduled Task named “WebAgent” — it starts automatically when you log in and restarts itself if it stops. It also installs the Server Manager — open a new terminal and type webagent to inspect, restart or diagnose it. To stop it later: paste “Stop-ScheduledTask -TaskName WebAgent” into PowerShell; to stop it starting on login: “Unregister-ScheduledTask -TaskName WebAgent -Confirm:$false”.';

const _MAC_STEPS = [
  "Open Terminal: press Cmd+Space, type 'Terminal', and open it.",
  'Paste the command and press Enter. (The first time, macOS may ask to install the Command Line Tools — allow it, then paste the command again.)',
  'The first run takes a few minutes while it installs everything.',
  'When it finishes, open http://localhost:8080 on this Mac, or http://THIS-MAC-IP:8080 from another device on the same network.',
];
const _MAC_NOTE = 'WebAgent installs into a folder in your home directory and runs in the background via launchd — it starts automatically when you log in and restarts itself if it stops. It also installs the Server Manager — type webagent to inspect, restart or diagnose it. To stop it later: paste “launchctl unload ~/Library/LaunchAgents/com.webagent.server.plist”; to start it again: “launchctl load -w ~/Library/LaunchAgents/com.webagent.server.plist”.';

// ── Run-only commands (start the server when it's ALREADY installed) ──────────
// No repo URL / token, nothing to clone or rebuild. Each is BYTE-IDENTICAL to its
// provider's run command (termux/windows/macos.py) so the copy box matches what
// the backend would build. They detect however the server was installed and start
// it the matching way:
//   • Linux/Termux — Termux → start_server_termux.sh (proot keep-alive); a systemd
//     Linux box → systemctl start webagent; otherwise the nohup keep-alive loop.
//   • Windows — the “WebAgent” Scheduled Task if present, else the keep-alive ps1.
//   • macOS — (re)load the launchd agent, falling back to a kickstart if loaded.
// Termux/Windows reference the install folder (so they follow a custom location);
// macOS uses the launchd agent, which is folder-independent.
function _runTermux(dir) {
  return 'if [ -n "$TERMUX_VERSION" ] || [ -d /data/data/com.termux ]; then '
    + 'bash "' + dir + '/start_server_termux.sh"; '
    + 'elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files webagent.service >/dev/null 2>&1; then '
    + 'sudo systemctl start webagent; '
    + 'else bash "' + dir + '/deploy/start_server_linux.sh" "' + dir + '"; fi';
}
function _runWindows(dir) {
  return 'if(Get-ScheduledTask -TaskName WebAgent -EA SilentlyContinue){Start-ScheduledTask -TaskName WebAgent}'
    + 'else{powershell -NoProfile -ExecutionPolicy Bypass -File "' + dir + '\\deploy\\start_server_windows.ps1"}';
}
function _runMac() {
  return 'launchctl load -w "$HOME/Library/LaunchAgents/com.webagent.server.plist" 2>/dev/null '
    + '|| launchctl kickstart -k "gui/$(id -u)/com.webagent.server"';
}

// One descriptor per manual platform: its row + field element ids, command
// builder, and static steps/note. The build functions + step constants above are
// referenced here, so this list must come AFTER them.
const MANUAL_ROWS = [
  { id: 'termux', row: 'ac-deploy-phone-row',
    url: 'ac-tx-url', vis: 'ac-tx-visibility', tokenWrap: 'ac-tx-token-wrap', token: 'ac-tx-token',
    dir: 'ac-tx-dir', defaultDir: _MC_DEFAULT_DIR_POSIX, adminPw: 'ac-tx-admin-pw',
    cmd: 'ac-tx-cmd', copy: 'ac-tx-copy', qrBtn: 'ac-tx-qr-btn', status: 'ac-tx-status',
    run: 'ac-tx-run', runCopy: 'ac-tx-run-copy', runBuild: _runTermux,
    steps: 'ac-tx-steps', note: 'ac-tx-note',
    build: _buildTermux, stepsText: _TERMUX_STEPS, noteText: _TERMUX_NOTE,
    qrLabel: 'Scan this in Termux on the phone' },
  { id: 'windows', row: 'ac-deploy-win-row',
    url: 'ac-win-url', vis: 'ac-win-visibility', tokenWrap: 'ac-win-token-wrap', token: 'ac-win-token',
    dir: 'ac-win-dir', defaultDir: _MC_DEFAULT_DIR_WINDOWS, adminPw: 'ac-win-admin-pw',
    cmd: 'ac-win-cmd', copy: 'ac-win-copy', qrBtn: 'ac-win-qr-btn', status: 'ac-win-status',
    run: 'ac-win-run', runCopy: 'ac-win-run-copy', runBuild: _runWindows,
    steps: 'ac-win-steps', note: 'ac-win-note',
    build: _buildWindows, stepsText: _WIN_STEPS, noteText: _WIN_NOTE,
    qrLabel: 'Scan to copy the command to another device' },
  { id: 'macos', row: 'ac-deploy-mac-row',
    url: 'ac-mac-url', vis: 'ac-mac-visibility', tokenWrap: 'ac-mac-token-wrap', token: 'ac-mac-token',
    dir: 'ac-mac-dir', defaultDir: _MC_DEFAULT_DIR_POSIX, adminPw: 'ac-mac-admin-pw',
    cmd: 'ac-mac-cmd', copy: 'ac-mac-copy', qrBtn: 'ac-mac-qr-btn', status: 'ac-mac-status',
    run: 'ac-mac-run', runCopy: 'ac-mac-run-copy', runBuild: _runMac,
    steps: 'ac-mac-steps', note: 'ac-mac-note',
    build: _buildMac, stepsText: _MAC_STEPS, noteText: _MAC_NOTE,
    qrLabel: 'Scan to copy the command to another device' },
];

// Read one row's inputs (repo URL + visibility + token + optional install folder
// + the optional pre-set admin password).
function _manualInputs(desc) {
  return {
    github_url: (_qs(desc.url)?.value || '').trim(),
    visibility: _qs(desc.vis)?.value || 'public',
    token: (_qs(desc.token)?.value || '').trim(),
    install_dir: (_qs(desc.dir)?.value || '').trim(),
    admin_password: _qs(desc.adminPw)?.value || '',
  };
}

// Show/hide the token field for one row (only when its repo is Private).
function _manualSyncToken(desc) {
  const vis = _qs(desc.vis);
  const wrap = _qs(desc.tokenWrap);
  if (wrap) wrap.style.display = (vis && vis.value === 'private') ? '' : 'none';
}

// Pre-fill every manual row from its saved (non-secret) config, then paint each.
function _renderManualPrefill() {
  MANUAL_ROWS.forEach(desc => {
    const t = (_catalog && (_catalog.providers || []).find(p => p.id === desc.id)) || null;
    const cfg = (t && t.config) || {};
    const url = _qs(desc.url);
    const vis = _qs(desc.vis);
    const dir = _qs(desc.dir);
    if (url && !url.value) url.value = cfg.github_url || '';
    if (vis && cfg.visibility) vis.value = cfg.visibility;
    if (dir && !dir.value) dir.value = cfg.install_dir || '';
    if (dir && !dir.placeholder) dir.placeholder = desc.defaultDir;
    _manualSyncToken(desc);
    _refreshLucideIcons(_qs(desc.row));   // Copy / QR button icons
    _manualRender(desc);                  // show the command straight away
  });
}

// Build one row's command from its current inputs and paint it into the
// always-visible box (instant, no network). If this row's QR popover is open,
// refresh its code too.
function _manualRender(desc) {
  const r = desc.build(_manualInputs(desc));
  const code = _qs(desc.cmd);
  if (code) code.textContent = r.command;

  // The run-only (already-installed) command follows the chosen install folder
  // (Termux/Windows); macOS ignores it. Paint it from the same resolved folder.
  const runCode = _qs(desc.run);
  if (runCode && desc.runBuild) runCode.textContent = desc.runBuild(r.directory);

  // A real warning in red; otherwise a gentle note about which repo/folder is in
  // use, or a nudge to finish a private repo's token.
  const status = _qs(desc.status);
  if (status) {
    if (r.warning) { status.textContent = r.warning; status.style.color = 'var(--danger)'; }
    else if (r.placeholderToken) { status.textContent = 'Enter your access token above to finish the command.'; status.style.color = ''; }
    else if (r.defaultRepo) { status.textContent = 'Installing the standard WebAgent repository — enter an address above only to install your own fork.'; status.style.color = ''; }
    else { status.textContent = ''; status.style.color = ''; }
  }

  // Steps + note never change — fill them once.
  const steps = _qs(desc.steps);
  if (steps && !steps.dataset.filled) {
    steps.innerHTML = desc.stepsText.map(s => '<li>' + _esc(s) + '</li>').join('');
    steps.dataset.filled = '1';
  }
  const note = _qs(desc.note);
  if (note && !note.dataset.filled) {
    note.hidden = false; note.textContent = desc.noteText; note.dataset.filled = '1';
  }

  if (_qrPop && _qrDesc === desc) _fetchQr(desc);   // keep an open QR in sync
}

// Save one row's non-secret choices so it pre-fills next time (the token is never
// sent here). Fire-and-forget; a missing endpoint just means no pre-fill.
function _manualPersist(desc) {
  if (!isAdmin()) return;
  const inp = _manualInputs(desc);
  // Only the non-secret repo/visibility/folder are remembered; the admin PASSWORD
  // is never sent here (it only ever lives in the shown command).
  _post('/admin/deploy/config', { provider: desc.id, config: { github_url: inp.github_url, visibility: inp.visibility, install_dir: inp.install_dir } }).catch(() => {});
}

// ── QR popover (mirrors Remote Access → Same network) ──
// A small click-toggled card anchored to a row's QR button, showing that row's
// current command as a scannable code on a white plate (so it reads in either
// theme). Only one is open at a time (across all rows). The QR itself is generated
// server-side on demand; while the card is open, changing any field re-fetches it
// so it stays in sync with the live command.
function _closeQr() {
  if (!_qrPop) return;
  document.removeEventListener('keydown', _qrPop._onKey, true);
  document.removeEventListener('mousedown', _qrPop._onDoc, true);
  window.removeEventListener('resize', _qrPop._onReflow, true);
  window.removeEventListener('scroll', _qrPop._onReflow, true);
  _qrPop.remove();
  _qrPop = null;
  _qrAnchor = null;
  _qrDesc = null;
}

function _toggleQr(desc, anchor) {
  if (_qrPop && _qrAnchor === anchor) { _closeQr(); return; }
  _showQr(desc, anchor);
}

// Put a status message (loading / error) on the QR plate.
function _qrMessage(msg) {
  if (!_qrPop) return;
  const plate = _qrPop.querySelector('.ac-ra-qr-plate');
  if (plate) plate.innerHTML = '<div class="ac-hint" style="padding:22px 12px;text-align:center;color:#555;">' + _esc(msg) + '</div>';
}

function _setQrPlate(svg) {
  if (!_qrPop) return;
  const plate = _qrPop.querySelector('.ac-ra-qr-plate');
  if (!plate) return;
  plate.innerHTML = svg;
  const el = plate.querySelector('svg');
  if (el) { el.style.width = '100%'; el.style.height = 'auto'; el.style.display = 'block'; }
}

// Ask the server for a QR of one row's current command (debounced). Degrades to a
// clear message if the server can't make one (e.g. not yet restarted).
function _fetchQr(desc) {
  clearTimeout(_qrTimer);
  _qrTimer = setTimeout(async () => {
    if (!_qrPop) return;
    let r;
    try {
      r = await _post('/admin/deploy/command', { provider: desc.id, ..._manualInputs(desc), persist: false });
    } catch {
      _qrMessage('Couldn’t reach the server for the QR code. If you just updated WebAgent, restart it and try again.');
      return;
    }
    if (!_qrPop) return;
    if (r && r.qr_svg) _setQrPlate(r.qr_svg);
    else _qrMessage('QR codes need the “qrcode” package installed on the server.');
  }, 120);
}

function _showQr(desc, anchor) {
  _closeQr();
  const panel = document.createElement('div');
  panel.className = 'ac-ra-qr-pop ac-tx-qr-pop';

  const plate = document.createElement('div');
  plate.className = 'ac-ra-qr-plate';
  panel.appendChild(plate);

  const label = document.createElement('div');
  label.className = 'ac-ra-qr-pop-url';
  label.style.whiteSpace = 'normal';
  label.textContent = desc.qrLabel;
  panel.appendChild(label);

  document.body.appendChild(panel);
  _qrPop = panel;
  _qrAnchor = anchor;
  _qrDesc = desc;
  _qrMessage('Generating…');
  _fetchQr(desc);

  const place = () => _placeQr(panel, anchor);
  place();
  const onDoc = ev => { if (panel.contains(ev.target) || anchor.contains(ev.target)) return; _closeQr(); };
  const onKey = ev => { if (ev.key === 'Escape') _closeQr(); };
  const onReflow = () => { if (_qrPop) place(); };
  document.addEventListener('mousedown', onDoc, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', onReflow, true);
  window.addEventListener('scroll', onReflow, true);
  panel._onDoc = onDoc;
  panel._onKey = onKey;
  panel._onReflow = onReflow;
}

function _placeQr(panel, anchor) {
  const a = anchor.getBoundingClientRect();
  const pw = panel.offsetWidth, ph = panel.offsetHeight;
  const gap = 6, margin = 8;
  let left = a.right - pw;                       // right edges aligned
  let top = a.bottom + gap;                      // below the button
  if (top + ph + margin > window.innerHeight) top = a.top - ph - gap;   // flip up
  left = Math.max(margin, Math.min(left, window.innerWidth - pw - margin));
  top = Math.max(margin, top);
  panel.style.left = Math.round(left) + 'px';
  panel.style.top = Math.round(top) + 'px';
}

// Turn every `data-tip` label across ALL manual rows into a circled "?" help
// badge, the same affordance the cloud row's fields get (via _buildField).
// Bespoke here because the manual rows are hand-written markup, not built from
// field descriptors. Idempotent — a `wired` flag stops re-runs from stacking.
function _wireManualTips() {
  const sel = MANUAL_ROWS.map(d => '#' + d.row + ' .ac-label[data-tip]').join(', ');
  document.querySelectorAll(sel).forEach(lab => {
    if (lab.dataset.tipWired) return;
    lab.dataset.tipWired = '1';
    const badge = _tipBadge(lab.dataset.tip);
    if (badge) lab.appendChild(badge);
  });
}

function _initManualRows() {
  MANUAL_ROWS.forEach(desc => {
    const url = _qs(desc.url);
    const vis = _qs(desc.vis);
    const token = _qs(desc.token);
    const dir = _qs(desc.dir);

    if (url && !url.dataset.wired) {
      url.dataset.wired = '1';
      url.addEventListener('input', () => _manualRender(desc));         // instant, client-side
      url.addEventListener('change', () => { _manualRender(desc); _manualPersist(desc); });
    }
    if (dir && !dir.dataset.wired) {
      dir.dataset.wired = '1';
      dir.addEventListener('input', () => _manualRender(desc));
      dir.addEventListener('change', () => { _manualRender(desc); _manualPersist(desc); });
    }
    if (token && !token.dataset.wired) {
      token.dataset.wired = '1';
      token.addEventListener('input', () => _manualRender(desc));
    }
    if (vis && !vis.dataset.wired) {
      vis.dataset.wired = '1';
      vis.addEventListener('change', () => { _manualSyncToken(desc); _manualRender(desc); _manualPersist(desc); });
    }
    const adminPw = _qs(desc.adminPw);
    if (adminPw && !adminPw.dataset.wired) {
      adminPw.dataset.wired = '1';
      adminPw.addEventListener('input', () => _manualRender(desc));   // never persisted
    }
    _wireCopy(_qs(desc.copy), _qs(desc.cmd));         // idempotent (guards on its own flag)
    _wireCopy(_qs(desc.runCopy), _qs(desc.run));      // the run-only command's Copy
    const qrBtn = _qs(desc.qrBtn);
    if (qrBtn && !qrBtn.dataset.wired) {
      qrBtn.dataset.wired = '1';
      qrBtn.addEventListener('click', () => _toggleQr(desc, qrBtn));
    }
  });
  _wireManualTips();             // circled "?" help badges on every row's labels
}

// ── Field-help popover ───────────────────────────────────────────────────────
// A small circled "?" beside a label. CLICK / TAP opens a real help bubble
// (hover also previews it on a mouse). The native `title=` tooltip was hover-
// only — it did nothing on click and nothing at all on touch. One shared
// floating bubble is appended to <body> so it can't be clipped by the panel's
// scroll box. The help text lives on the backend field descriptor (f.tip) so
// every cloud target ships its own field guidance.
let _tipPop = null;        // the shared floating bubble element
let _tipAnchor = null;     // badge it's currently pointing at
let _tipPinned = false;    // true when opened by click/tap (stays until dismissed)

function _ensureTipPop() {
  if (_tipPop) return _tipPop;
  _tipPop = document.createElement('div');
  _tipPop.className = 'ac-tip-pop';
  _tipPop.setAttribute('role', 'tooltip');
  _tipPop.hidden = true;
  document.body.appendChild(_tipPop);
  // Dismiss a pinned bubble on outside click, Escape, scroll or resize.
  document.addEventListener('click', (e) => {
    if (!_tipPinned) return;
    if (e.target === _tipAnchor) return;
    _hideTip();
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _hideTip(); });
  window.addEventListener('scroll', () => { if (!_tipPop.hidden) _hideTip(); }, true);
  window.addEventListener('resize', () => { if (!_tipPop.hidden) _hideTip(); });
  return _tipPop;
}

function _hideTip() {
  if (_tipAnchor) _tipAnchor.classList.remove('is-open');
  _tipPinned = false;
  _tipAnchor = null;
  if (_tipPop) { _tipPop.hidden = true; _tipPop.classList.remove('show'); }
}

function _showTip(anchor, pinned) {
  const tip = anchor.dataset.tip || '';
  if (!tip) return;
  const pop = _ensureTipPop();
  pop.textContent = tip;
  pop.hidden = false;                       // make it measurable before placing
  _tipAnchor = anchor;
  _tipPinned = pinned || _tipPinned;
  // Light up the badge when pinned by a click, so the click visibly "lands"
  // even if a hover had already previewed the bubble.
  document.querySelectorAll('.ac-field-tip.is-open').forEach(el => { if (el !== anchor) el.classList.remove('is-open'); });
  anchor.classList.toggle('is-open', _tipPinned);
  const margin = 8;
  const r = anchor.getBoundingClientRect();
  const pr = pop.getBoundingClientRect();
  let left = r.left + r.width / 2 - pr.width / 2;
  let top = r.bottom + 8;
  if (left < margin) left = margin;
  if (left + pr.width > window.innerWidth - margin) left = window.innerWidth - margin - pr.width;
  if (top + pr.height > window.innerHeight - margin) top = r.top - pr.height - 8;   // flip up
  pop.style.left = Math.round(Math.max(margin, left)) + 'px';
  pop.style.top = Math.round(Math.max(margin, top)) + 'px';
  pop.classList.add('show');
}

function _toggleTip(b) {
  if (_tipPinned && _tipAnchor === b) _hideTip();
  else _showTip(b, true);
}

// Exported so the sibling App Access → Social sign-in panel (social-auth.js) can
// reuse the exact same "?" help badge + shared floating bubble, rather than
// duplicating the popover system. The returned badge closes over this module's
// single shared bubble, so all field tips across Data Settings share one popover.
export function _tipBadge(tip) {
  if (!tip) return null;
  const b = document.createElement('span');
  b.className = 'ac-field-tip';
  b.textContent = '?';
  b.dataset.tip = tip;
  b.tabIndex = 0;
  b.setAttribute('role', 'button');
  b.setAttribute('aria-label', 'Help: ' + tip);
  // preventDefault+stopPropagation so a badge inside a checkbox <label> doesn't
  // toggle the checkbox, and so the outside-click dismiss doesn't fight us.
  b.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); _toggleTip(b); });
  b.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggleTip(b); }
  });
  b.addEventListener('mouseenter', () => { if (!_tipPinned) _showTip(b, false); });
  b.addEventListener('mouseleave', () => { if (!_tipPinned) _hideTip(); });
  b.addEventListener('blur', () => { if (!_tipPinned) _hideTip(); });
  return b;
}

// Build one labelled input for a field descriptor {key,label,type,tip,...}.
function _buildField(f, value, kind, isSet) {
  const wrap = document.createElement('div');
  if (f.type === 'textarea' || f.full || f.hint || f.tip) wrap.style.gridColumn = '1 / -1';

  if (f.type !== 'checkbox') {
    const lab = document.createElement('label');
    lab.className = 'ac-label';
    lab.textContent = f.label + (f.required ? ' *' : '');
    const tip = _tipBadge(f.tip);
    if (tip) lab.appendChild(tip);
    wrap.appendChild(lab);
  }

  let inp;
  let noteEl = null;             // live "who is this for" note under a select
  if (f.type === 'select' && f.custom) {
    // A curated dropdown PLUS a "Custom…" entry that reveals a text box, so a value
    // outside the list can still be typed (e.g. any Google Cloud zone). The visible
    // <select> is only a DRIVER: the hidden-until-needed TEXT box carries the
    // data-key and holds the real value, so _gather always reads the true string —
    // a picked option is mirrored into it, a custom one is typed straight into it.
    const CUSTOM = '__custom__';
    const sel = document.createElement('select');
    sel.className = 'ac-input';
    const known = new Set();
    const notes = {};
    (f.options || []).forEach(o => {
      const opt = document.createElement('option');
      if (typeof o === 'string') { opt.value = o; opt.textContent = o; known.add(o); }
      else {
        opt.value = o.value; opt.textContent = o.label || o.value; known.add(o.value);
        if (o.note) notes[o.value] = o.note;
      }
      sel.appendChild(opt);
    });
    const customOpt = document.createElement('option');
    customOpt.value = CUSTOM;
    customOpt.textContent = f.custom_label || 'Custom…';
    sel.appendChild(customOpt);

    const txt = document.createElement('input');
    txt.type = 'text';
    txt.className = 'ac-input';
    txt.style.marginTop = '6px';
    if (f.custom_placeholder) txt.placeholder = f.custom_placeholder;
    txt.dataset.key = f.key;                 // the real field lives here
    txt.dataset.kind = kind;

    const cur = (value != null ? value : (f.default != null ? f.default : ''));
    txt.value = cur;
    const isKnown = known.has(cur);
    sel.value = isKnown ? cur : CUSTOM;       // unlisted saved value → "Custom…"
    txt.style.display = isKnown ? 'none' : '';

    if (Object.keys(notes).length) {
      noteEl = document.createElement('div');
      noteEl.className = 'ac-field-note';
    }
    const sync = () => {
      if (sel.value === CUSTOM) {
        txt.style.display = '';               // reveal the box, keep any typed value
        try { txt.focus(); } catch {}
      } else {
        txt.value = sel.value;                // the picked zone becomes the value
        txt.style.display = 'none';
      }
      if (noteEl) noteEl.textContent = notes[sel.value] || '';
    };
    sel.addEventListener('change', sync);
    if (noteEl) noteEl.textContent = notes[sel.value] || '';

    wrap.appendChild(sel);
    wrap.appendChild(txt);
    inp = null;                               // appended above; skip the generic add
  } else if (f.type === 'select') {
    inp = document.createElement('select');
    inp.className = 'ac-input';
    const notes = {};
    (f.options || []).forEach(o => {
      const opt = document.createElement('option');
      if (typeof o === 'string') { opt.value = o; opt.textContent = o; }
      else {
        opt.value = o.value; opt.textContent = o.label || o.value;
        if (o.note) notes[o.value] = o.note;
      }
      inp.appendChild(opt);
    });
    inp.value = (value != null ? value : (f.default != null ? f.default : ''));
    if (Object.keys(notes).length) {
      noteEl = document.createElement('div');
      noteEl.className = 'ac-field-note';
      const sync = () => { noteEl.textContent = notes[inp.value] || ''; };
      inp.addEventListener('change', sync);
      sync();
    }
  } else if (f.type === 'checkbox') {
    const row = document.createElement('label');
    row.style.display = 'flex';
    row.style.alignItems = 'center';
    row.style.gap = '8px';
    inp = document.createElement('input');
    inp.type = 'checkbox';
    inp.checked = (value != null ? !!value : !!f.default);
    inp.dataset.type = 'checkbox';
    row.appendChild(inp);
    const span = document.createElement('span');
    span.className = 'ac-label';
    span.style.margin = '0';
    span.textContent = f.label;
    row.appendChild(span);
    const tip = _tipBadge(f.tip);
    if (tip) row.appendChild(tip);
    wrap.appendChild(row);
  } else if (f.type === 'textarea') {
    inp = document.createElement('textarea');
    inp.className = 'ac-input';
    inp.rows = 4;
    inp.style.fontFamily = 'var(--font-mono)';
    inp.value = (value != null ? value : '');
    if (isSet) inp.placeholder = '••• saved — leave blank to keep';
    else if (f.placeholder) inp.placeholder = f.placeholder;
  } else {
    inp = document.createElement('input');
    inp.type = (f.type === 'password' || kind === 'cred') ? 'password' : (f.type === 'number' ? 'number' : 'text');
    inp.className = 'ac-input';
    inp.value = (value != null ? value : (f.default != null && kind === 'cfg' ? f.default : ''));
    if (isSet) inp.placeholder = '••• saved — leave blank to keep';
    else if (f.placeholder) inp.placeholder = f.placeholder;
  }

  if (inp) { inp.dataset.key = f.key; inp.dataset.kind = kind; }
  if (inp && f.type !== 'checkbox') wrap.appendChild(inp);
  if (noteEl) wrap.appendChild(noteEl);

  if (f.hint) {
    const h = document.createElement('div');
    h.className = 'ac-hint';
    h.style.fontSize = '11px';
    h.style.marginTop = '3px';
    h.textContent = f.hint;
    wrap.appendChild(h);
  }
  return wrap;
}

function _gather(kind) {
  const out = {};
  // Secrets live in two hosts: the cloud-key list AND the standalone admin-password
  // slot (rendered separately, above the key). Config reads only the settings host.
  const sel = kind === 'cfg'
    ? '#ac-deploy-config [data-key]'
    : '#ac-deploy-creds [data-key], #ac-deploy-admin [data-key]';
  document.querySelectorAll(sel).forEach(inp => {
    const key = inp.dataset.key;
    if (!key) return;
    if (inp.dataset.type === 'checkbox') out[key] = !!inp.checked;
    else out[key] = (inp.value != null ? inp.value : '').trim ? inp.value.trim() : inp.value;
  });
  return out;
}

// ── Log + streaming ────────────────────────────────────────────────────────
function _logReset() {
  const log = _qs('ac-deploy-log');
  if (log) { log.hidden = false; log.textContent = ''; }
}
function _logLine(text, level) {
  const log = _qs('ac-deploy-log');
  if (!log) return;
  const mark = level === 'ok' ? '✓ ' : level === 'err' ? '✗ ' : level === 'warn' ? '! ' : '· ';
  log.textContent += mark + text + '\n';
  log.scrollTop = log.scrollHeight;
}

async function _stream(path, body, onDone) {
  const res = await fetch(apiPath(path), {
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

// ── Actions ────────────────────────────────────────────────────────────────
async function _onProviderChange() {
  _renderProvider();
  if (!isAdmin()) return;
  try { await _post('/admin/deploy/select', { provider: _provider() }); } catch {}
}

async function _saveSettings() {
  if (!isAdmin()) return false;
  _setStatus('Saving…');
  try {
    await _post('/admin/deploy/config', { provider: _provider(), config: _gather('cfg') });
    const creds = _gather('cred');
    // Only POST credentials when the admin typed something (blank = keep stored).
    if (Object.values(creds).some(v => String(v || '').trim())) {
      await _post('/admin/deploy/credentials', { provider: _provider(), values: creds });
    }
    _setStatus('Saved.', 'ok');
    await _load();
    return true;
  } catch (e) { _setStatus(e.message, 'err'); return false; }
}

async function _test() {
  if (!isAdmin()) return;
  await _saveSettings();
  _setStatus('Testing…');
  try {
    const r = await _post('/admin/deploy/test', { provider: _provider() });
    _setStatus(r.detail || (r.ok ? 'Connected.' : 'Failed'), r.ok ? 'ok' : 'err');
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _deploy() {
  if (!isAdmin() || _busy) return;
  const p = _current();
  if (!p) return;
  // Persist whatever's typed first, so the deploy uses the latest settings/key.
  const saved = await _saveSettings();
  if (!saved) return;
  // Word the confirm to match the target: a cloud target CREATES a billable
  // server; an "existing server" target (creates_server === false) just installs
  // onto a machine the admin already owns.
  const confirmMsg = (p.creates_server === false)
    ? ('Install WebAgent onto ' + (p.display_name || 'the server') + '?\n\n'
        + 'This connects to the server you specified and installs WebAgent on it. '
        + 'It replaces any existing WebAgent install there.')
    : ('Deploy WebAgent to ' + (p.display_name || 'the cloud') + '?\n\n'
        + 'This creates a real, billable server on your cloud account.');
  if (!window.confirm(confirmMsg)) return;
  _busy = true;
  _setStatus('Deploying…');
  _logReset();
  _logLine('Starting deploy to ' + (p.display_name || _provider()) + '…');
  try {
    await _stream('/admin/deploy/deploy', { provider: _provider() }, (result) => {
      if (result.ok) {
        _logLine(result.message || 'Done.', 'ok');
        _setStatus('Deployed.', 'ok');
      } else {
        _logLine(result.message || 'Failed.', 'err');
        _setStatus(result.message || 'Failed', 'err');
      }
    });
    await _load();
  } catch (e) { _logLine(e.message, 'err'); _setStatus(e.message, 'err'); }
  finally { _busy = false; }
}

async function _destroy() {
  if (!isAdmin() || _busy) return;
  const p = _current();
  const dep = (p && p.deployment) || {};
  if (!dep.server) return;
  // A cloud target's tear-down DELETES the machine; an "existing server" target
  // (creates_server === false) only stops the app and leaves the machine untouched.
  const destroyMsg = (p && p.creates_server === false)
    ? ('Stop WebAgent on "' + dep.server + '"?\n\n'
        + 'This stops and disables the WebAgent service on that server. The machine '
        + 'itself and its files are left untouched.')
    : ('Tear down the server "' + dep.server + '"?\n\nThis permanently deletes it.');
  if (!window.confirm(destroyMsg)) return;
  _busy = true;
  _setStatus('Tearing down…');
  _logReset();
  try {
    await _stream('/admin/deploy/destroy', { provider: _provider() }, (result) => {
      _logLine(result.message || (result.ok ? 'Done.' : 'Failed.'), result.ok ? 'ok' : 'err');
      _setStatus(result.ok ? 'Torn down.' : (result.message || 'Failed'), result.ok ? 'ok' : 'err');
    });
    await _load();
  } catch (e) { _logLine(e.message, 'err'); _setStatus(e.message, 'err'); }
  finally { _busy = false; }
}

// ── Init ───────────────────────────────────────────────────────────────────
export function initDeploy() {
  const sel = _qs('ac-deploy-provider');
  if (sel && !sel.dataset.wired) {
    sel.dataset.wired = '1';
    sel.addEventListener('change', _onProviderChange);
  }
  _qs('ac-deploy-test')?.addEventListener('click', _test);
  _qs('ac-deploy-save')?.addEventListener('click', _saveSettings);
  _qs('ac-deploy-go')?.addEventListener('click', _deploy);
  _qs('ac-deploy-destroy')?.addEventListener('click', _destroy);
  // Saved-servers picker (shown only for the profile-aware SSH target).
  _qs('ac-deploy-servers')?.addEventListener('change', _onServerSelect);
  _qs('ac-deploy-server-save')?.addEventListener('click', _onServerSave);
  _qs('ac-deploy-server-delete')?.addEventListener('click', _onServerDelete);
  _initManualRows();            // the Linux/Termux, Windows + macOS install rows
  _initInstances();             // the local-deployments list + register-a-checkout form
  _loadInstances();             // paint the hub + siblings straight away

  // Re-load whenever the Data Settings section is shown (wired in nav.js).
  window.__refreshDeploy = _load;
}
