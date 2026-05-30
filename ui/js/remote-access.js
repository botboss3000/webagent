'use strict';

/**
 * Remote Access — App Configuration → App Settings → Remote Access card.
 *
 * Lets an admin reach this server from a phone / other device. Talks to
 * /admin/remote-access/* : shows the same-network address (+ QR), starts/stops
 * the app-managed tunnels (ngrok / cloudflared), surfaces Tailscale + manual
 * port-forward status, and manages the signpost "phone bookmark" that always
 * forwards to the PC's current address.
 *
 * Colours use design-system variables so the card is correct in dark + light.
 */

import { apiPath } from './config.js';
import { isAdmin } from './left-login.js';

let _status = null;

const METHOD_META = {
  same_network: {
    blurb: 'No remote tunnel — only devices on the same Wi-Fi can reach this PC (using the address above).',
    fields: [],
  },
  ngrok: {
    blurb: 'App-managed outbound tunnel. With a reserved domain the address is stable (a paid ngrok feature); without one it changes each restart and the phone bookmark below keeps up. Run ngrok’s one-time authtoken setup first (field below).',
    fields: [
      { key: 'domain', label: 'Reserved domain (optional — stable address)', placeholder: 'your-name.ngrok.app', full: true },
      { key: 'region', label: 'Region (optional)', placeholder: 'us, eu, ap, au, sa, jp, in' },
      { key: 'bin_path', label: 'ngrok path (optional)', placeholder: 'blank = find on PATH' },
      { key: '__authtoken', label: 'ngrok authtoken (saved into ngrok, one-time)', placeholder: 'paste token, then Save token', action: 'authtoken', full: true },
    ],
  },
  cloudflare: {
    blurb: 'App-managed Cloudflare tunnel. A named tunnel gives a fixed https hostname on a domain you own (set it up once in Cloudflare). Tick Quick for a throwaway trycloudflare.com address instead.',
    fields: [
      { key: 'tunnel', label: 'Named tunnel', placeholder: 'my-pc' },
      { key: 'hostname', label: 'Fixed hostname', placeholder: 'pc.webagent.live' },
      { key: 'bin_path', label: 'cloudflared path (optional)', placeholder: 'blank = find on PATH' },
      { key: 'quick', label: 'Quick tunnel (random trycloudflare.com address)', type: 'checkbox', full: true },
    ],
  },
  tailscale: {
    blurb: 'Private mesh — only your own enrolled devices can reach this PC (most secure). Install Tailscale on this PC and your phone, sign both into the same account, then Start to register the address.',
    fields: [],
  },
  manual: {
    blurb: 'You run your own port-forward / dynamic-DNS. Enter the public address it exposes; Test checks whether it answers.',
    fields: [
      { key: 'public_url', label: 'Your public address', placeholder: 'https://home.example.com', full: true },
    ],
  },
};

function _qs(id) { return document.getElementById(id); }
function _userId() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }

function _setStatus(msg, kind) {
  const el = _qs('ac-ra-status');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = kind === 'ok' ? 'var(--success)' : kind === 'err' ? 'var(--danger)' : '';
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

function _copy(text) {
  if (!text || text === '—') return;
  const done = () => _setStatus('Copied', 'ok');
  try {
    navigator.clipboard.writeText(text).then(done).catch(_fallbackCopy.bind(null, text, done));
  } catch {
    _fallbackCopy(text, done);
  }
}
function _fallbackCopy(text, done) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    done && done();
  } catch { _setStatus('Copy failed — select the text manually', 'err'); }
}

function _injectQr(containerId, svg) {
  const box = _qs(containerId);
  if (!box) return;
  if (!svg) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.innerHTML = svg;
  // QR must read as dark-on-light to scan; functional white plate in both themes.
  box.style.display = 'inline-block';
  box.style.background = '#ffffff';
  box.style.padding = '8px';
  box.style.borderRadius = '8px';
  const el = box.querySelector('svg');
  if (el) { el.style.width = '152px'; el.style.height = '152px'; el.style.display = 'block'; }
}

async function _load() {
  if (!isAdmin()) return;
  try {
    const res = await fetch(apiPath('/admin/remote-access/status?requesting_user_id=' + encodeURIComponent(_userId())));
    if (!res.ok) return; // 403 for non-admins, etc. — leave the card quiet
    _status = await res.json();
  } catch { return; }
  _renderAll();
}

function _renderAll() {
  if (!_status) return;
  const s = _status;

  const badge = _qs('ac-ra-active-badge');
  if (badge) {
    const running = s.runtime && s.runtime.running ? ' · running' : '';
    badge.textContent = (s.active_method || 'same_network').replace('_', ' ') + running;
  }

  const env = _qs('ac-ra-env-banner');
  if (env) env.hidden = !s.env_locked;

  // Same network
  const sn = s.same_network || {};
  const snUrl = _qs('ac-ra-sn-url');
  if (snUrl) snUrl.textContent = sn.primary_url || '—';
  _injectQr('ac-ra-sn-qr', sn.qr_svg);
  const others = _qs('ac-ra-sn-others');
  if (others) {
    const extra = (sn.urls || []).filter(u => u !== sn.primary_url);
    others.textContent = extra.length ? 'Also: ' + extra.join('  ') : '';
  }

  // Method
  const sel = _qs('ac-ra-method');
  if (sel) sel.value = s.active_method || 'same_network';
  _renderMethodPanel(s.active_method || 'same_network');

  // Auto-start
  const auto = _qs('ac-ra-autostart');
  if (auto) auto.checked = !!s.auto_start;

  // Runtime line
  _renderRuntime();

  // Phone bookmark + signpost
  const purl = _qs('ac-ra-phone-url');
  if (purl) purl.textContent = s.phone_url || '—';
  _injectQr('ac-ra-phone-qr', s.phone_qr_svg);
  const sp = s.signpost || {};
  if (_qs('ac-ra-sp-server')) _qs('ac-ra-sp-server').value = sp.server_url || '';
  if (_qs('ac-ra-sp-role')) _qs('ac-ra-sp-role').value = sp.role || 'client';
  if (_qs('ac-ra-sp-enabled')) _qs('ac-ra-sp-enabled').checked = sp.enabled !== false;
}

function _renderMethodPanel(method) {
  const meta = METHOD_META[method] || METHOD_META.same_network;
  const blurb = _qs('ac-ra-blurb');
  const fields = _qs('ac-ra-fields');
  if (blurb) blurb.textContent = meta.blurb;
  if (!fields) return;
  fields.innerHTML = '';

  const cfg = ((_status.methods || {})[method] || {}).config || {};

  // Tailscale: show detected device status instead of inputs.
  if (method === 'tailscale') {
    const ts = (_status.methods || {}).tailscale || {};
    const div = document.createElement('div');
    div.style.gridColumn = '1 / -1';
    div.className = 'ac-hint';
    if (!ts.available) div.textContent = 'Tailscale is not installed on this PC.';
    else if (!ts.ip) div.textContent = 'Tailscale installed but not connected. Run "tailscale up" and sign in.';
    else div.textContent = 'This device: ' + (ts.name || ts.ip) + '  →  ' + (ts.url || ('http://' + ts.ip));
    fields.appendChild(div);
    return;
  }

  meta.fields.forEach(f => {
    const wrap = document.createElement('div');
    if (f.full) wrap.style.gridColumn = '1 / -1';
    const lab = document.createElement('label');
    lab.className = 'ac-label';
    lab.textContent = f.label;
    wrap.appendChild(lab);

    let inp;
    if (f.type === 'checkbox') {
      inp = document.createElement('input');
      inp.type = 'checkbox';
      inp.checked = !!cfg[f.key];
      inp.dataset.type = 'checkbox';
    } else {
      inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'ac-input';
      inp.placeholder = f.placeholder || '';
      inp.value = f.key.startsWith('__') ? '' : (cfg[f.key] == null ? '' : String(cfg[f.key]));
    }
    inp.id = 'ac-ra-fld-' + f.key;
    inp.dataset.key = f.key;

    if (f.action === 'authtoken') {
      const row = document.createElement('div');
      row.style.display = 'flex';
      row.style.gap = '6px';
      row.style.alignItems = 'center';
      inp.style.flex = '1';
      row.appendChild(inp);
      const btn = document.createElement('button');
      btn.className = 'ac-btn';
      btn.type = 'button';
      btn.textContent = 'Save token';
      btn.addEventListener('click', () => _saveAuthtoken(inp.value.trim()));
      row.appendChild(btn);
      wrap.appendChild(row);
    } else {
      wrap.appendChild(inp);
    }
    fields.appendChild(wrap);
  });
}

function _renderRuntime() {
  const row = _qs('ac-ra-public-row');
  if (!row) return;
  const rt = _status.runtime || {};
  const method = _status.active_method;
  let url = rt.public_url || '';
  if (!url) {
    const m = (_status.methods || {})[method] || {};
    url = m.url || (m.config && m.config.public_url) || (m.config && m.config.hostname) || '';
  }
  if (rt.running && url) {
    row.innerHTML = 'Live at <strong style="color:var(--success);">' + _esc(url) + '</strong>';
  } else if (rt.error) {
    row.innerHTML = '<span style="color:var(--danger);">' + _esc(rt.error) + '</span>';
  } else if (url) {
    row.textContent = 'Address: ' + url;
  } else {
    row.textContent = '';
  }
}

function _esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function _gatherConfig(method) {
  const cfg = {};
  document.querySelectorAll('#ac-ra-fields [data-key]').forEach(inp => {
    const key = inp.dataset.key;
    if (!key || key.startsWith('__')) return; // __authtoken is saved separately
    if (inp.dataset.type === 'checkbox') cfg[key] = !!inp.checked;
    else { const v = inp.value.trim(); cfg[key] = v; }
  });
  return cfg;
}

// ── Actions ──
async function _onMethodChange() {
  const sel = _qs('ac-ra-method');
  if (!sel) return;
  _renderMethodPanel(sel.value);
  if (!isAdmin()) return;
  try {
    await _post('/admin/remote-access/method', { method: sel.value });
    if (_status) _status.active_method = sel.value;
    _renderRuntime();
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _saveConfig() {
  if (!isAdmin()) return;
  const method = (_qs('ac-ra-method') || {}).value || 'same_network';
  _setStatus('Saving…');
  try {
    await _post('/admin/remote-access/config', { method, config: _gatherConfig(method) });
    _setStatus('Saved.', 'ok');
    await _load();
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _saveAuthtoken(token) {
  if (!isAdmin()) return;
  if (!token) { _setStatus('Paste a token first', 'err'); return; }
  _setStatus('Saving authtoken…');
  try {
    const r = await _post('/admin/remote-access/ngrok-authtoken', { authtoken: token });
    _setStatus(r.detail || (r.ok ? 'Saved.' : 'Failed'), r.ok ? 'ok' : 'err');
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _start() {
  if (!isAdmin()) return;
  const method = (_qs('ac-ra-method') || {}).value || 'same_network';
  _setStatus('Starting ' + method.replace('_', ' ') + '…');
  try {
    const r = await _post('/admin/remote-access/start', { method });
    if (r.ok) _setStatus(r.public_url ? ('Live at ' + r.public_url) : 'Started.', 'ok');
    else _setStatus((r.error || 'Failed') + (r.output ? (' — ' + r.output) : ''), 'err');
    await _load();
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _stop() {
  if (!isAdmin()) return;
  _setStatus('Stopping…');
  try { await _post('/admin/remote-access/stop', {}); _setStatus('Stopped.', 'ok'); await _load(); }
  catch (e) { _setStatus(e.message, 'err'); }
}

async function _test() {
  const method = (_qs('ac-ra-method') || {}).value || 'same_network';
  _setStatus('Testing…');
  try {
    const r = await _post('/admin/remote-access/test', { method });
    _setStatus(r.detail || (r.ok ? 'OK' : 'Failed'), r.ok ? 'ok' : 'err');
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _regenKey() {
  if (!isAdmin()) return;
  _setStatus('Generating new key…');
  try { await _post('/admin/remote-access/regenerate-key', {}); _setStatus('New bookmark key generated.', 'ok'); await _load(); }
  catch (e) { _setStatus(e.message, 'err'); }
}

async function _saveSignpost() {
  if (!isAdmin()) return;
  const config = {
    enabled: !!(_qs('ac-ra-sp-enabled') || {}).checked,
    role: (_qs('ac-ra-sp-role') || {}).value || 'client',
    server_url: ((_qs('ac-ra-sp-server') || {}).value || '').trim(),
  };
  _setStatus('Saving signpost…');
  try { await _post('/admin/remote-access/config', { method: 'signpost', config }); _setStatus('Signpost saved.', 'ok'); await _load(); }
  catch (e) { _setStatus(e.message, 'err'); }
}

async function _toggleAutoStart() {
  if (!isAdmin()) return;
  try { await _post('/admin/remote-access/auto-start', { auto_start: !!(_qs('ac-ra-autostart') || {}).checked }); }
  catch (e) { _setStatus(e.message, 'err'); }
}

export function initRemoteAccess() {
  const sel = _qs('ac-ra-method');
  if (sel && !sel.dataset.wired) {
    sel.dataset.wired = '1';
    sel.addEventListener('change', _onMethodChange);
  }
  _qs('ac-ra-save-cfg')?.addEventListener('click', _saveConfig);
  _qs('ac-ra-start')?.addEventListener('click', _start);
  _qs('ac-ra-stop')?.addEventListener('click', _stop);
  _qs('ac-ra-test')?.addEventListener('click', _test);
  _qs('ac-ra-regen')?.addEventListener('click', _regenKey);
  _qs('ac-ra-sp-save')?.addEventListener('click', _saveSignpost);
  _qs('ac-ra-autostart')?.addEventListener('change', _toggleAutoStart);
  _qs('ac-ra-sn-copy')?.addEventListener('click', () => _copy((_qs('ac-ra-sn-url') || {}).textContent));
  _qs('ac-ra-phone-copy')?.addEventListener('click', () => _copy((_qs('ac-ra-phone-url') || {}).textContent));

  // Re-load whenever the App Settings section is shown (wired in app-config.js).
  window.__refreshRemoteAccess = _load;

  // Initial load (no-op for non-admins).
  _load();
}
