'use strict';

import { app, bindDom } from './state.js';
import { initStorageUi } from './storage.js';
import { initChat } from './chat.js';
import { initSuggestions } from './suggestions.js';
import { initChatActivity } from './chat-activity.js';
import { initReconnect } from './reconnect.js';
import { ensureAttachmentsInit } from './attachments.js';
import { connectAgent } from './agentWs.js';
import { initTabs } from './tabs.js';
import { initLoop } from './loop.js';
import { initLoopVisual } from './loop-logic.js';
import { registerSessionApi, initSessions } from './sessions.js';
import { initAutoAgent } from './autoagent.js';
import { initBilling } from './billing.js';
import { initAccount } from './account.js';
import { relocateAdminToolsContainers } from './files.js';
// The whole Admin Tools panel — the file manager (initFiles) and the sub-panels
// (initDbViewer / initDataManagement / initRemoteAccess / initAppConfig) — is
// initialised lazily on first Admin Tools open: initFiles via startAdminTools(),
// the sub-panels via _ensureAdminInit() in tabs.js. None run at boot. initAgents
// runs via startAgents() on Agents-tab activation.
import { initChatResize } from './chatResize.js';
import { initRecorder } from './recorder.js';
import { initTutorial, startTutorial } from './tutorial.js';
import { fetchAdminStatus, isAdmin, fetchAccessMode } from './left-login.js';
import { installAuthRefresh, ensureFreshToken, startTokenAutoRefresh } from './auth-refresh.js';
import './icons.js'; // auto-renders Lucide icons on DOM mutations
import { randomUUID } from './uuid.js';

bindDom();

// Keep the 24h access token fresh. When it expires while a tab is open (or
// between visits), authenticated features break two ways: the session-messages
// participant check sees no identity and returns `{restricted}`, so switching
// sessions silently bounces you into a new empty one ("I can see the chat but
// can't choose a different session"); and require_db_auth / _require_auth
// routes 401. The already-open agent WebSocket keeps live chat working, which
// is why only a re-login (a fresh token) used to fix it. installAuthRefresh()
// adds a 401 → recall → retry backstop on window.fetch; startTokenAutoRefresh()
// keeps the token valid proactively (timer + on focus). See auth-refresh.js.
installAuthRefresh();
startTokenAutoRefresh();

// ── Anonymous session ─────────────────────────────────────────────────────
// If no auth token exists, create an anonymous session so the visitor can
// chat without signing in.  Works for both the main page and public agent
// URL pages (/{agentId}).
async function _initAnonSession() {
  if (localStorage.getItem('auth_token')) return;

  let browserId = localStorage.getItem('anon_browser_id');
  if (!browserId) {
    browserId = randomUUID();
    localStorage.setItem('anon_browser_id', browserId);
  }

  try {
    // Use the public agent URL endpoint if __agentId is set (creates identity
    // tied to that agent), otherwise use the generic anonymous endpoint.
    const url = window.__agentId
      ? `/api/v1/agents/${window.__agentId}/anon-session`
      : '/api/v1/auth/anonymous';
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ browser_id: browserId }),
    });
    if (!res.ok) return;
    const data = await res.json();
    localStorage.setItem('auth_token', data.access_token || data.token);
    localStorage.setItem('auth_user_id', data.user_id);
    app.currentUserId = data.user_id;
    app.localUserId = data.user_id;
  } catch (e) {
    console.warn('anon session init failed:', e);
  }
}

// Run anon auth, then continue init. For non-anon visitors this resolves immediately.
const _anonReady = _initAnonSession();

// Before the rest of init (which connects the WebSocket and loads the current
// session's messages), make sure the access token is valid. Otherwise the very
// first session load can hit the `{restricted}` participant-check path and
// bounce the user into a fresh empty session. Race a 3s cap so a slow/hung
// recall can't stall the whole app boot — the fetch backstop and the
// keep-fresh timer will catch up if the cap wins. For a still-valid token this
// resolves immediately (a local exp decode, no network).
const _bootReady = _anonReady.then(() => Promise.race([
  ensureFreshToken().catch(() => {}),
  new Promise((res) => setTimeout(res, 3000)),
]));

// ── JS error debugging ────────────────────────────────────────────────────
// Catches unhandled JS errors and module load failures. Shows a visible
// red banner so the issue is obvious even without DevTools open, and also
// surfaces in the page title so it's visible in the browser tab.
//
// Yellow status dots in the header mean the WebSocket is trying to connect
// (state: "Connecting…"). They turn green once connected, red if the server
// is unreachable. Persistent yellow flashing = the connection keeps dropping
// and retrying — usually caused by a JS module error that prevents the page
// from initialising fully (blocking connectAgent() from completing), or a
// server that is down/unreachable.
const _JS_ERRORS = [];

function _showJsErrorBanner(msg, source) {
  _JS_ERRORS.push({ msg, source, ts: new Date().toISOString() });

  // Title prefix — immediately visible in the browser tab
  document.title = '[JS ERR] ' + document.title.replace(/^\[JS ERR\] /, '');

  // Persistent red banner fixed to top of page — create once, stack messages
  let banner = document.getElementById('js-error-debug-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'js-error-debug-banner';
    banner.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:99999',
      'background:rgba(247,36,36,0.92)', 'color:#fff',
      'font-size:12px', 'font-family:monospace',
      'padding:6px 48px 6px 12px',
      'border-bottom:2px solid #c00',
      'display:flex', 'flex-direction:column', 'gap:2px',
    ].join(';');
    const x = document.createElement('button');
    x.textContent = '✕ dismiss';
    x.style.cssText = [
      'position:absolute', 'right:8px', 'top:50%', 'transform:translateY(-50%)',
      'background:none', 'border:1px solid rgba(255,255,255,0.5)', 'color:#fff',
      'cursor:pointer', 'padding:2px 6px', 'border-radius:3px', 'font-size:11px',
    ].join(';');
    x.addEventListener('click', () => banner.remove());
    banner.appendChild(x);
    document.body.appendChild(banner);
  }

  const row = document.createElement('div');
  row.textContent = '⚠ ' + source + ': ' + msg;
  banner.insertBefore(row, banner.firstChild);

  // Structured console output for DevTools inspection
  console.error('[webAgent JS error]', { msg, source, allErrors: _JS_ERRORS });
}

// Catches synchronous errors thrown by scripts (including missing variables,
// type errors, etc.)
window.addEventListener('error', (e) => {
  const src = e.filename ? e.filename.split('/').pop() + ':' + e.lineno : 'unknown';
  _showJsErrorBanner(e.message, src);
});

// Catches async failures — broken dynamic imports, unhandled promise rejections,
// fetch errors that aren't caught, etc.
window.addEventListener('unhandledrejection', (e) => {
  const msg = (e.reason && e.reason.message) ? e.reason.message : String(e.reason);
  _showJsErrorBanner(msg, 'unhandledrejection');
});

// Wrap each init call so one broken module can't silently prevent the rest
// from loading. Any error is surfaced in the banner above.
function _safeInit(name, fn) {
  try {
    fn();
  } catch (e) {
    _showJsErrorBanner(e.message, name);
    console.error('[webAgent] ' + name + ' threw during init:', e);
  }
}

// Admin Tools tab is always visible now — restrictions have been removed.
function _applyAdminToolsVisibility() {
  document.querySelectorAll('.main-tab[data-value="admin-tools"]').forEach(el => {
    el.style.display = '';
  });
  const opt = document.querySelector('#main-tab-select option[value="admin-tools"]');
  if (opt) opt.disabled = false;
}

window.addEventListener('admin-status-loaded', _applyAdminToolsVisibility);

// Wait for anon session (if needed) AND a valid access token before running
// the rest of init, so auth_token + user_id are available — and current — for
// all downstream modules.
_bootReady.then(() => {
  // Hide Admin Tools immediately (synchronous check covers admin_default
  // bootstrap); fetchAdminStatus() will re-check against the server profile
  // and dispatch 'admin-status-loaded' to refine the visibility.
  _applyAdminToolsVisibility();
  fetchAdminStatus();

  // Fire profile + app settings early so initAgents() can use cached results
  // instead of fetching them sequentially later. These are fire-and-forget;
  // they store results on window.__agentsProfileData / __agentsAppSettingsData.
  if (app.currentUserId) {
    fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(app.currentUserId)}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) window.__agentsProfileData = d; })
      .catch(() => {});
    const _token = localStorage.getItem('auth_token');
    fetch('/admin/settings/app', { headers: _token ? { Authorization: `Bearer ${_token}` } : {} })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) window.__agentsAppSettingsData = d; })
      .catch(() => {});
  }

  _safeInit('initStorageUi',        initStorageUi);
  _safeInit('initChat',             initChat);
  _safeInit('initSuggestions',      initSuggestions);
  _safeInit('initChatActivity',     initChatActivity);
  _safeInit('ensureAttachmentsInit', ensureAttachmentsInit);
  _safeInit('initReconnect',        initReconnect);
  _safeInit('registerSessionApi',   registerSessionApi);

  try {
    connectAgent();
  } catch (e) {
    _showJsErrorBanner(e.message, 'connectAgent');
  }

  _safeInit('initTabs',        initTabs);
  _safeInit('initLoop',        initLoop);
  _safeInit('initLoopVisual',  initLoopVisual);
  // Move parked Admin Tools markup (App Config + Database viewer) into the
  // admin-tools layout. Stays eager (cheap DOM reparent) so the markup is in
  // its final position before the now-lazy db viewer / app config init bind
  // to it on first Admin Tools open.
  _safeInit('relocateAdminToolsContainers', relocateAdminToolsContainers);
  // ── Lazy main-panel init ──────────────────────────────────────────────
  // The entire Admin Tools panel — file manager (initFiles, via
  // startAdminTools) and the sub-panels (Database viewer, Data Management,
  // Remote Access, App Config, via _ensureAdminInit() in tabs.js) — is now
  // built only on first Admin Tools open, not at boot. This avoids building
  // DOM/handlers for a panel that's rarely opened, especially on mobile where
  // only one panel is ever visible. initAgents is covered by startAgents()
  // when the Agents tab activates. Kept eager below:
  //  • initAutoAgent — registers the live page/visualizer WebSocket handler
  //    (app._autoAgentHandler), which must receive events even when the
  //    Pages tab isn't showing.
  //  • initBilling / initAccount — cheap, no tab-gated heavy work.
  _safeInit('initAutoAgent',   initAutoAgent);
  _safeInit('initBilling',     initBilling);
  _safeInit('initAccount',     initAccount);
  _safeInit('initSessions',    initSessions);
  _safeInit('initChatResize',  initChatResize);
  _safeInit('initRecorder',    initRecorder);
  _safeInit('initTutorial',    initTutorial);
  // Defer startTutorial() so it doesn't block the critical path — it fetches
  // tutorial state from the server but isn't needed for first paint.
  setTimeout(() => { try { startTutorial(); } catch (_) {} }, 0);
});

// ── Visibility change: reconnect when user returns to this tab ──
// Terminal tabs manage their own per-instance WebSocket reconnect inside
// createTerminalInstance — only the agent WS is global.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    const agentOk = app.agentWs && app.agentWs.readyState === WebSocket.OPEN;
    if (!agentOk) {
      connectAgent();
      // Dead-WS fallback: pull the current session straight from the DB so an
      // in-progress (or just-finished) answer shows even before the socket is
      // back. The DB is the source of truth; live events are an accelerant.
      if (typeof app.reloadCurrentSession === 'function') {
        try { app.reloadCurrentSession(); } catch (_) {}
      }
    }
  }
});

// ── Fallback poll every 10s in case visibility change misses something ──
setInterval(() => {
  if (!app.agentWs || app.agentWs.readyState > 1) connectAgent();
}, 10000);

// ── Token was silently refreshed (expired JWT recalled via remember_token) ──
// The agent WebSocket authenticates with the token only at connect time, so a
// socket that dropped/failed while the token was stale must reconnect to pick
// up the fresh one. An already-open socket is left alone (don't disturb a
// working live-chat stream). See auth-refresh.js.
window.addEventListener('webagent-token-refreshed', () => {
  const open = app.agentWs && app.agentWs.readyState === WebSocket.OPEN;
  if (!open) {
    try { connectAgent(); } catch (_) {}
  }
});
