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
import { _refreshLucideIcons, _esc } from './dom-utils.js';

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

/** Render the Same-network addresses as a uniform stacked list, one row per
 *  URL: <code>address</code> + Copy + QR. The QR button opens a popup panel
 *  (rather than showing a big code inline) so the list stays compact. */
function _renderSameNetwork(sn) {
  const host = _qs('ac-ra-sn-urls');
  if (!host) return;
  host.innerHTML = '';

  // Prefer per-URL entries (each carries its own qr_svg); fall back to the flat
  // urls list (only the primary has a QR in that older shape).
  let entries = Array.isArray(sn.entries) && sn.entries.length ? sn.entries : null;
  if (!entries) {
    entries = (sn.urls || []).map(u => ({
      url: u,
      qr_svg: u === sn.primary_url ? sn.qr_svg : null,
    }));
  }

  if (!entries.length) {
    host.innerHTML = '<div class="ac-hint">No same-network address detected.</div>';
    return;
  }

  entries.forEach(e => {
    const row = document.createElement('div');
    row.className = 'ac-ra-sn-row';

    const code = document.createElement('code');
    code.className = 'ac-ra-code';
    code.textContent = e.url;
    row.appendChild(code);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'ac-btn ac-ra-icon-btn';
    copyBtn.type = 'button';
    copyBtn.title = 'Copy address';
    copyBtn.setAttribute('aria-label', 'Copy address');
    copyBtn.innerHTML = '<i data-lucide="copy"></i>';
    copyBtn.addEventListener('click', () => _copy(e.url));
    row.appendChild(copyBtn);

    const qrBtn = document.createElement('button');
    qrBtn.className = 'ac-btn ac-ra-icon-btn';
    qrBtn.type = 'button';
    qrBtn.title = 'Show QR code';
    qrBtn.setAttribute('aria-label', 'Show QR code');
    qrBtn.innerHTML = '<i data-lucide="qr-code"></i>';
    qrBtn.addEventListener('click', () => _toggleQrPopup(e.url, e.qr_svg, qrBtn));
    row.appendChild(qrBtn);

    host.appendChild(row);
  });

  // Swap the [data-lucide] placeholders for real SVGs (scoped to this host).
  _refreshLucideIcons(host);
}

let _qrPopup = null;       // the floating panel element, or null
let _qrPopupAnchor = null; // the QR button it was opened from
function _closeQrPopup() {
  if (!_qrPopup) return;
  document.removeEventListener('keydown', _qrPopup._onKey, true);
  document.removeEventListener('mousedown', _qrPopup._onDocClick, true);
  window.removeEventListener('resize', _qrPopup._onReflow, true);
  window.removeEventListener('scroll', _qrPopup._onReflow, true);
  _qrPopup.remove();
  _qrPopup = null;
  _qrPopupAnchor = null;
}

/** Click the QR button: open the popover, or close it if it's already open for
 *  this same button (so the button toggles). */
function _toggleQrPopup(url, svg, anchor) {
  if (_qrPopup && _qrPopupAnchor === anchor) { _closeQrPopup(); return; }
  _showQrPopup(url, svg, anchor);
}

/** A small popover (like a clickable hover-card) anchored to the QR button,
 *  showing one address's QR on a dark-on-white plate so it scans in either
 *  theme. Click anywhere outside or press Escape to dismiss. */
function _showQrPopup(url, svg, anchor) {
  _closeQrPopup();
  if (!svg) { _setStatus('QR unavailable on this server (qrcode package missing)', 'err'); return; }

  const panel = document.createElement('div');
  panel.className = 'ac-ra-qr-pop';

  const plate = document.createElement('div');
  plate.className = 'ac-ra-qr-plate';
  plate.innerHTML = svg;
  // The QR scales to fill the plate width; the plate stretches to the panel,
  // which is sized by the (single-line) URL — so the QR is at least as wide as
  // the address. The square viewBox keeps it square at height:auto.
  const el = plate.querySelector('svg');
  if (el) { el.style.width = '100%'; el.style.height = 'auto'; el.style.display = 'block'; }
  panel.appendChild(plate);

  const label = document.createElement('div');
  label.className = 'ac-ra-qr-pop-url';
  label.textContent = url;
  panel.appendChild(label);

  document.body.appendChild(panel);
  _qrPopup = panel;
  _qrPopupAnchor = anchor;

  const place = () => _placeQrPopup(panel, anchor);
  place();

  // Dismiss on outside click, Escape, or any scroll/resize that would unmoor it.
  const onDocClick = ev => {
    if (panel.contains(ev.target) || anchor.contains(ev.target)) return;
    _closeQrPopup();
  };
  const onKey = ev => { if (ev.key === 'Escape') _closeQrPopup(); };
  const onReflow = () => { if (_qrPopup) place(); };
  document.addEventListener('mousedown', onDocClick, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', onReflow, true);
  window.addEventListener('scroll', onReflow, true);
  panel._onDocClick = onDocClick;
  panel._onKey = onKey;
  panel._onReflow = onReflow;
}

/** Position the popover just below its anchor button, right-aligned to it, and
 *  clamped inside the viewport (flips above the button if it would overflow). */
function _placeQrPopup(panel, anchor) {
  const a = anchor.getBoundingClientRect();
  const pw = panel.offsetWidth, ph = panel.offsetHeight;
  const gap = 6, margin = 8;
  let left = a.right - pw;                       // right edges aligned
  let top = a.bottom + gap;                      // below the button
  if (top + ph + margin > window.innerHeight) {  // no room below → flip above
    top = a.top - ph - gap;
  }
  left = Math.max(margin, Math.min(left, window.innerWidth - pw - margin));
  top = Math.max(margin, top);
  panel.style.left = Math.round(left) + 'px';
  panel.style.top = Math.round(top) + 'px';
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

  // Same network — every LAN address stacked, uniform style, each with its
  // own copy + QR-popup button.
  _renderSameNetwork(s.same_network || {});

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
  _qs('ac-ra-phone-copy')?.addEventListener('click', () => _copy((_qs('ac-ra-phone-url') || {}).textContent));

  // Re-load whenever the App Settings section is shown (wired in app-config.js).
  window.__refreshRemoteAccess = _load;
  // Data fetching deferred to startRemoteAccess() — runs only when the
  // Remote Access card is visible.
}

/** Fetch remote access status. Called when the Remote Access card becomes
 *  visible (from startAdminTools in files.js). */
export function startRemoteAccess() {
  _load();
}
