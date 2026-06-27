'use strict';

/**
 * Deploy — App Configuration → App Settings → Deploy card.
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
import { _refreshLucideIcons, _esc } from '../../../shared/js/dom-utils.js';
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

function _setStatus(msg, kind) {
  const el = _qs('ac-deploy-status');
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

// ── Load + render ──────────────────────────────────────────────────────────
async function _load() {
  if (!isAdmin()) return;
  try {
    const res = await fetch(apiPath('/admin/deploy/catalog?requesting_user_id=' + encodeURIComponent(_userId())));
    if (!res.ok) return;          // 403 for non-admins — leave the card quiet
    _catalog = await res.json();
  } catch { return; }
  _renderAll();
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
  _renderProvider();
  _renderManualPrefill();
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
  // Cloud-key form (secret); a "set" secret shows a placeholder, never the value
  if (credHost) {
    credHost.innerHTML = '';
    (p.credential_fields || []).forEach(f => {
      const isSet = !!(p.credentials_set || {})[f.key];
      credHost.appendChild(_buildField(f, '', 'cred', isSet));
    });
  }
  if (keyNote) {
    keyNote.textContent = p.configured
      ? 'A cloud key is saved. Deploying will use it; leave the field blank to keep it.'
      : 'No cloud key saved yet — paste one to enable Deploy.';
  }

  // Tear-down only when there's a recorded server
  const dep = p.deployment || {};
  if (destroyBtn) destroyBtn.style.display = dep.server ? '' : 'none';
  if (result) result.innerHTML = _deploymentLine(dep);
  _setStatus('');
  if (window.lucide) { try { lucide.createIcons(); } catch {} }
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
const _MC_PLACEHOLDER_REPO = 'https://github.com/YOUR-NAME/YOUR-REPO';
const _MC_PLACEHOLDER_TOKEN = 'YOUR_ACCESS_TOKEN';
const _MC_BRANCH = 'main';
const _MC_BAD_URL = "'\";\n\r\\ &|`$(){}<>";
const _MC_BAD_TOKEN = "'\";\n\r\\ &|`$(){}<>@/ ";

function _mcHasBad(s, bad) { for (const c of bad) if (s.indexOf(c) >= 0) return true; return false; }
function _mcStripScheme(u) { return u.replace(/^https?:\/\//, ''); }

// Resolve the clone target from the row inputs (mirror manual_common.resolve_clone).
function _mcResolve(inp) {
  const typed = (inp.github_url || '').trim();
  const placeholderRepo = !typed;
  const repo = (placeholderRepo || _mcHasBad(typed, _MC_BAD_URL)) ? _MC_PLACEHOLDER_REPO : typed;
  const priv = (inp.visibility || 'public') === 'private';
  let cloneUrl = repo, warning = '', placeholderToken = false;
  if (priv) {
    let tok = (inp.token || '').trim();
    if (tok && _mcHasBad(tok, _MC_BAD_TOKEN)) { warning = 'That token contains characters that aren’t valid in a GitHub token.'; tok = ''; }
    if (!tok) { tok = _MC_PLACEHOLDER_TOKEN; placeholderToken = true; }
    cloneUrl = 'https://' + tok + '@' + _mcStripScheme(repo);
  }
  return { repo, cloneUrl, placeholderRepo, placeholderToken, warning };
}

// Linux / Termux — mirror app/deploy/providers/termux.py build_command. ONE
// command for both: install git with whatever package manager is present, clone,
// then hand off to deploy/termux-setup.sh which detects Termux vs plain Linux.
function _buildTermux(inp) {
  const r = _mcResolve(inp);
  const command = 'SUDO=; [ "$(id -u 2>/dev/null)" = 0 ] || SUDO=sudo; '
    + 'if command -v git >/dev/null 2>&1; then :; '
    + 'elif command -v pkg >/dev/null 2>&1; then pkg install -y git; '
    + 'elif command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update && $SUDO apt-get install -y git; '
    + 'elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y git; '
    + 'elif command -v pacman >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm git; fi; '
    + '{ [ -d "$HOME/webagent/.git" ] || git clone --depth 1 --branch ' + _MC_BRANCH + ' ' + r.cloneUrl + ' "$HOME/webagent"; } && '
    + 'bash "$HOME/webagent/deploy/termux-setup.sh"';
  return { command, placeholderRepo: r.placeholderRepo, placeholderToken: r.placeholderToken, warning: r.warning };
}

// Windows — mirror app/deploy/providers/windows.py build_command (a PowerShell
// one-liner: ensure git via winget, clone to %USERPROFILE%\webagent, run the ps1).
function _buildWindows(inp) {
  const r = _mcResolve(inp);
  const command = "$ErrorActionPreference='Stop'; "
    + "$repo='" + r.cloneUrl + "'; $dir=\"$env:USERPROFILE\\webagent\"; "
    + "if(-not(Get-Command git -EA SilentlyContinue)){Write-Host 'Installing Git...'; "
    + "try{winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --silent}catch{}; "
    + "$env:Path=[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')}; "
    + "if(-not(Get-Command git -EA SilentlyContinue)){Write-Host 'Git is required. Install it from https://git-scm.com/download/win then run this again.'; return}; "
    + "if(-not(Test-Path \"$dir\\.git\")){git clone --depth 1 --branch " + _MC_BRANCH + " $repo \"$dir\"}; "
    + "powershell -NoProfile -ExecutionPolicy Bypass -File \"$dir\\deploy\\windows-setup.ps1\"";
  return { command, placeholderRepo: r.placeholderRepo, placeholderToken: r.placeholderToken, warning: r.warning };
}

// macOS — mirror app/deploy/providers/macos.py build_command (a Terminal
// one-liner: ensure git/Command Line Tools, clone to $HOME/webagent, run the sh).
function _buildMac(inp) {
  const r = _mcResolve(inp);
  const command = 'set -e; D="$HOME/webagent"; '
    + 'if ! command -v git >/dev/null 2>&1; then '
    + "echo 'Installing the Command Line Tools (a dialog may appear)...'; "
    + 'xcode-select --install 2>/dev/null || true; '
    + "echo 'If a dialog appeared, finish it, then paste this command again.'; fi; "
    + '{ [ -d "$D/.git" ] || git clone --depth 1 --branch ' + _MC_BRANCH + ' ' + r.cloneUrl + ' "$D"; } && '
    + 'bash "$D/deploy/macos-setup.sh"';
  return { command, placeholderRepo: r.placeholderRepo, placeholderToken: r.placeholderToken, warning: r.warning };
}

const _TERMUX_STEPS = [
  'On a phone: install the free Termux app, then open it. On a Linux computer: open a terminal.',
  'Scan the QR code or paste the command, then press Enter.',
  'The first run takes a few minutes while it installs everything (on a phone it also sets up a small Ubuntu environment).',
  'When it finishes, open http://localhost:8080 on that device, or http://DEVICE-IP:8080 from another device on the same network (the script prints the address).',
];
const _TERMUX_NOTE = 'On a phone the command installs webAgent inside a small Ubuntu environment (the reliable way to run the full app on Android); on a Linux computer it installs straight onto the system. Either way it keeps running in the background and restarts itself if it stops. On a Linux computer it also restarts automatically after a reboot; on a phone, install the free Termux:Boot add-on to start it on boot. On a phone it also installs the Server Manager — type webagent in Termux to inspect, restart or diagnose the install. To stop it later: on a phone paste “proot-distro login ubuntu -- pkill -f run.py”, on Linux paste “pkill -f run.py”.';

const _WIN_STEPS = [
  "Open PowerShell: click Start, type 'PowerShell', and open it.",
  'Paste the command and press Enter. (If Windows offers to install Git, allow it.)',
  'The first run takes a few minutes while it downloads Python and installs everything.',
  'When it finishes, open http://localhost:8080 on this PC, or http://THIS-PC-IP:8080 from another device on the same network.',
];
const _WIN_NOTE = 'webAgent installs into a folder in your user profile and runs in the background as a Scheduled Task named “webAgent” — it starts automatically when you log in and restarts itself if it stops. It also installs the Server Manager — open a new terminal and type webagent to inspect, restart or diagnose it. To stop it later: paste “Stop-ScheduledTask -TaskName webAgent” into PowerShell; to stop it starting on login: “Unregister-ScheduledTask -TaskName webAgent -Confirm:$false”.';

const _MAC_STEPS = [
  "Open Terminal: press Cmd+Space, type 'Terminal', and open it.",
  'Paste the command and press Enter. (The first time, macOS may ask to install the Command Line Tools — allow it, then paste the command again.)',
  'The first run takes a few minutes while it installs everything.',
  'When it finishes, open http://localhost:8080 on this Mac, or http://THIS-MAC-IP:8080 from another device on the same network.',
];
const _MAC_NOTE = 'webAgent installs into a folder in your home directory and runs in the background via launchd — it starts automatically when you log in and restarts itself if it stops. It also installs the Server Manager — type webagent to inspect, restart or diagnose it. To stop it later: paste “launchctl unload ~/Library/LaunchAgents/com.webagent.server.plist”; to start it again: “launchctl load -w ~/Library/LaunchAgents/com.webagent.server.plist”.';

// One descriptor per manual platform: its row + field element ids, command
// builder, and static steps/note. The build functions + step constants above are
// referenced here, so this list must come AFTER them.
const MANUAL_ROWS = [
  { id: 'termux', row: 'ac-deploy-phone-row',
    url: 'ac-tx-url', vis: 'ac-tx-visibility', tokenWrap: 'ac-tx-token-wrap', token: 'ac-tx-token',
    cmd: 'ac-tx-cmd', copy: 'ac-tx-copy', qrBtn: 'ac-tx-qr-btn', status: 'ac-tx-status',
    steps: 'ac-tx-steps', note: 'ac-tx-note',
    build: _buildTermux, stepsText: _TERMUX_STEPS, noteText: _TERMUX_NOTE,
    qrLabel: 'Scan this in Termux on the phone' },
  { id: 'windows', row: 'ac-deploy-win-row',
    url: 'ac-win-url', vis: 'ac-win-visibility', tokenWrap: 'ac-win-token-wrap', token: 'ac-win-token',
    cmd: 'ac-win-cmd', copy: 'ac-win-copy', qrBtn: 'ac-win-qr-btn', status: 'ac-win-status',
    steps: 'ac-win-steps', note: 'ac-win-note',
    build: _buildWindows, stepsText: _WIN_STEPS, noteText: _WIN_NOTE,
    qrLabel: 'Scan to copy the command to another device' },
  { id: 'macos', row: 'ac-deploy-mac-row',
    url: 'ac-mac-url', vis: 'ac-mac-visibility', tokenWrap: 'ac-mac-token-wrap', token: 'ac-mac-token',
    cmd: 'ac-mac-cmd', copy: 'ac-mac-copy', qrBtn: 'ac-mac-qr-btn', status: 'ac-mac-status',
    steps: 'ac-mac-steps', note: 'ac-mac-note',
    build: _buildMac, stepsText: _MAC_STEPS, noteText: _MAC_NOTE,
    qrLabel: 'Scan to copy the command to another device' },
];

// Read one row's three inputs.
function _manualInputs(desc) {
  return {
    github_url: (_qs(desc.url)?.value || '').trim(),
    visibility: _qs(desc.vis)?.value || 'public',
    token: (_qs(desc.token)?.value || '').trim(),
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
    if (url && !url.value) url.value = cfg.github_url || '';
    if (vis && cfg.visibility) vis.value = cfg.visibility;
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

  // A gentle nudge while something's still a placeholder; a real warning in red.
  const status = _qs(desc.status);
  if (status) {
    if (r.warning) { status.textContent = r.warning; status.style.color = 'var(--danger)'; }
    else if (r.placeholderRepo) { status.textContent = 'Enter your repository address above — the command updates as you type.'; status.style.color = ''; }
    else if (r.placeholderToken) { status.textContent = 'Enter your access token above to finish the command.'; status.style.color = ''; }
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
  _post('/admin/deploy/config', { provider: desc.id, config: { github_url: inp.github_url, visibility: inp.visibility } }).catch(() => {});
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
      _qrMessage('Couldn’t reach the server for the QR code. If you just updated webAgent, restart it and try again.');
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

    if (url && !url.dataset.wired) {
      url.dataset.wired = '1';
      url.addEventListener('input', () => _manualRender(desc));         // instant, client-side
      url.addEventListener('change', () => { _manualRender(desc); _manualPersist(desc); });
    }
    if (token && !token.dataset.wired) {
      token.dataset.wired = '1';
      token.addEventListener('input', () => _manualRender(desc));
    }
    if (vis && !vis.dataset.wired) {
      vis.dataset.wired = '1';
      vis.addEventListener('change', () => { _manualSyncToken(desc); _manualRender(desc); _manualPersist(desc); });
    }
    _wireCopy(_qs(desc.copy), _qs(desc.cmd));   // idempotent (guards on its own flag)
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

function _tipBadge(tip) {
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
  if (f.type === 'select') {
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
  if (f.type !== 'checkbox') wrap.appendChild(inp);
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
  document.querySelectorAll('#ac-deploy-' + (kind === 'cfg' ? 'config' : 'creds') + ' [data-key]').forEach(inp => {
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
  if (!window.confirm('Deploy webAgent to ' + (p.display_name || 'the cloud') +
      '?\n\nThis creates a real, billable server on your cloud account.')) return;
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
  if (!window.confirm('Tear down the server "' + dep.server + '"?\n\nThis permanently deletes it.')) return;
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
  _initManualRows();            // the Linux/Termux, Windows + macOS install rows

  // Re-load whenever the App Settings section is shown (wired in nav.js).
  window.__refreshDeploy = _load;
}
