'use strict';

// App boot orchestrator — imports every top-level module and runs the boot
// sequence (bindDom → chat → sessions → tabs → loop, billing, account, …).
// Admin Tools and the Agents page init lazily on first open (see note below),
// not at boot.

import { app, bindDom } from './state.js';
import { initStorageUi } from './storage.js';
import { initChat } from '../../chat-side-panel/js/chat-ui.js';
import { initSuggestions } from '../../chat-side-panel/js/suggestions.js';
import { initAbilities } from '../../chat-side-panel/js/abilities.js';
import { initChatActivity } from './chat-activity.js';
import { initReconnect } from './reconnect.js';
import { ensureAttachmentsInit } from './attachments.js';
import { connectAgent } from './agentWs.js';
import { initTabs } from './tabs.js';
import { initLoop } from '../../main-panel/agents/agent-loop/js/loop.js';
import { initLoopVisual } from '../../main-panel/agents/agent-loop/js/loop-logic.js';
import { registerSessionApi, initSessions } from '../../chat-side-panel/js/session-init.js';
import { initCanvas } from '../../main-panel/canvas/js/canvas.js';
import { initBilling } from './billing.js';
import { initAccount } from './account.js';
import { relocateAdminToolsContainers } from './files.js';
// The whole Admin Tools panel — the file manager (initFiles) and the sub-panels
// (initDbViewer / initDataManagement / initRemoteAccess / initAppConfig) — is
// initialised lazily on first Admin Tools open: initFiles via startAdminTools(),
// the sub-panels via _ensureAdminInit() in tabs.js. None run at boot. initAgents
// runs via startAgents() on Agents-tab activation.
import { initChatResize } from './chatResize.js';
import { initRecorder } from '../../chat-side-panel/js/recorder.js';
import { initDebugConsole } from './debugConsole.js';
import { initTutorial, startTutorial } from '../../tutorials/js/tutorial.js';
import { fetchAdminStatus, isAdmin, fetchAccessMode, getAccessMode, isAuthenticated, showLeftOverlay, authHeaders } from './left-login.js';
import { installAuthRefresh, ensureFreshToken, startTokenAutoRefresh } from './auth-refresh.js';
import './icons.js'; // auto-renders Lucide icons on DOM mutations
import { randomUUID } from './uuid.js';
import { initClipboard } from './clipboard.js';
import { loadUiMessages } from './app-prompts.js';
import { initAppControlPoint } from './app-control-point.js';
import { mountSignInForm } from './page-access-gate.js';

bindDom();

// Pull the editable chat UI strings (welcome bubble, system bubbles, composer
// placeholders) from app/defaults/app-prompts.json as early as possible so they
// drive the first chat render. Fire now; the boot path awaits it below. Has its
// own fallbacks, so this can never blank a string or stall boot. See app-prompts.js.
const _uiMessagesReady = loadUiMessages();

// Guarantee Ctrl/Cmd + C / X / V / A work in every input, textarea,
// contenteditable and text selection across the whole app (terminals keep
// their own handling). Installed first so it sits ahead of every panel's key
// handlers. See clipboard.js for the why.
initClipboard();

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
// Resolve a boot-time identity when no auth token exists yet. The behaviour
// depends on the app's access mode:
//   • open              → auto-login as the default admin (localhost-only,
//                         enforced server-side). Local-use convenience.
//   • any mode, but on a public agent URL (/{agentId}) → try a per-agent
//                         anonymous session. The agent's OWN user_mode decides
//                         whether anonymous is allowed (anonymous permissions
//                         are controlled per-agent, not at the app level), so
//                         this is attempted regardless of the app access mode.
//   • otherwise         → leave unauthenticated; the visitor signs in / registers.
// Redirects to the first-run setup wizard if the app isn't set up yet (no admin).
// Probe whether the token currently in storage is accepted by the server.
// Used by open mode to decide between keeping a valid session and minting a
// fresh admin token. Hits /auth/me (which the 401-refresh wrapper deliberately
// ignores, so a stale token surfaces a real 401 here instead of being retried).
async function _sessionTokenValid() {
  const token = localStorage.getItem('auth_token');
  if (!token) return false;
  try {
    const res = await fetch('/api/v1/auth/me', {
      headers: authHeaders(),
    });
    return res.ok;
  } catch (_) {
    return false;
  }
}

async function _initBootAuth() {
  // Resolve the app's access mode FIRST. The entire identity of the app — who
  // is signed in, and whether Admin Tools are available — hinges on it, so it
  // must be settled before we decide what to do with any token already in
  // storage. (Previously a leftover `auth_token` short-circuited this on the
  // very first line, so in "open" mode a stale/expired/foreign token left the
  // app half-authenticated: the avatar showed the admin as "signed in", but the
  // server rejected the token so the admin-status probe returned false and the
  // Admin Tools tab never appeared. Only a manual logout reset it. Open mode
  // now re-establishes a valid admin session at boot instead.)
  await fetchAccessMode();
  const mode = getAccessMode();

  const haveToken = !!localStorage.getItem('auth_token');

  // In every mode EXCEPT open, an existing token means we're already signed in
  // and the keep-fresh timer + 401 backstop maintain it from here. Two cases
  // re-establish identity instead of trusting a stored token blindly:
  //   • open mode — the default admin is the active local user, so we re-validate
  //     (and re-mint if needed) even when a token is present (handled below).
  //   • Open Registration guests (anon_* token) — a guest has no remember-token
  //     to drive the 401 refresh, so a stale guest token would silently break.
  //     Fall through to re-mint one below against the SAME browser-keyed identity
  //     (their sessions survive). A real signed-in member token is trusted as-is.
  if (mode !== 'open' && haveToken) {
    const _uid = localStorage.getItem('auth_user_id') || '';
    const isGuest = mode === 'public_registered' && _uid.indexOf('anon_') === 0;
    if (!isGuest) return;
  }

  // Check if app has been initialized (admin exists). On a fresh install (no
  // admin yet) bounce straight to the first-run setup wizard — same redirect
  // the login page does — so opening the app at its root address "just works"
  // instead of dropping the visitor on an empty, unusable shell. Guarded so we
  // never loop if we're already on setup.html.
  try {
    const statusRes = await fetch('/api/v1/auth/setup-status');
    if (statusRes.ok) {
      const statusData = await statusRes.json();
      if (!statusData.initialized) {
        if (!location.pathname.endsWith('/setup.html')) {
          window.location.href = '/setup.html';
        }
        return;  // app not set up yet
      }
    }
  } catch (e) {
    // If setup-status fails, assume uninitialized and skip session creation.
    return;
  }

  const _store = (token, userId) => {
    if (!token) return;
    localStorage.setItem('auth_token', token);
    localStorage.setItem('auth_user_id', userId);
    app.currentUserId = userId;
    app.localUserId = userId;
  };

  // ── Open mode: the default admin is the active local user (localhost only). ──
  // Auto-sign-in as the bootstrap admin unless we ALREADY hold a server-valid
  // session. Keeping a valid session means a deliberately-switched account (via
  // the multi-account picker) survives a reload; an invalid/expired/foreign
  // token is replaced with a fresh admin token so the app self-heals instead of
  // showing a signed-in-but-powerless state.
  if (mode === 'open') {
    if (haveToken && await _sessionTokenValid()) return;
    try {
      const res = await fetch('/api/v1/auth/open-login', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        // Store through the multi-account manager so the header avatar, the
        // account dropdown, and the token-refresh logic all read the SAME
        // source of truth. (Open-login previously wrote only the legacy keys,
        // leaving getActive() blank and the account UI inconsistent with the
        // token actually being used.) upsertAccount() mirrors the legacy keys
        // too, so downstream code that reads `auth_token` keeps working.
        const { upsertAccount } = await import('./accounts.js');
        upsertAccount({
          user_id: data.user_id,
          username: data.username,
          display_name: data.display_name,
          access_token: data.access_token,
        });
        app.currentUserId = data.user_id;
        app.localUserId = data.user_id;
      }
    } catch (e) {
      console.warn('open-mode auto-login failed:', e);
    }
    // Fallback for open mode over Cloudflare Tunnel etc.: if open-login
    // failed (non-localhost request) we still need a userId for the
    // WebSocket handshake — the server skips JWT verification in open
    // mode, so we can trust the stored/local value.
    if (!app.currentUserId) {
      app.currentUserId = localStorage.getItem('auth_user_id') || 'admin';
      app.localUserId = app.currentUserId;
    }
    return;
  }

  // ── Public agent URL: per-agent anonymous, gated by the agent itself. ──
  if (window.__agentId) {
    let browserId = localStorage.getItem('anon_browser_id');
    if (!browserId) {
      browserId = randomUUID();
      localStorage.setItem('anon_browser_id', browserId);
    }
    try {
      const res = await fetch(`/api/v1/agents/${window.__agentId}/anon-session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ browser_id: browserId }),
      });
      if (res.ok) {
        const data = await res.json();
        _store(data.access_token || data.token, data.user_id);
      }
      // A 403 here means the agent requires a registered/authorized account —
      // leave unauthenticated so the sign-in UI takes over.
    } catch (e) {
      console.warn('anon session init failed:', e);
    }
    return;
  }

  // ── Open Registration: anonymous visitors are first-class guests. ──
  // The app is open to anonymous use, with what each anon may do decided
  // per-agent (the agent's user_mode), not at the app level. Hand the visitor a
  // persistent anonymous identity (a web_public guest keyed to this browser) so
  // the WebSocket handshake + chat gate accept them and their sessions survive a
  // reload. A real signed-in member returned early above; this path is only for
  // anonymous visitors and stale-guest re-mints.
  if (mode === 'public_registered') {
    let browserId = localStorage.getItem('anon_browser_id');
    if (!browserId) {
      browserId = randomUUID();
      localStorage.setItem('anon_browser_id', browserId);
    }
    try {
      const res = await fetch('/api/v1/auth/guest-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ browser_id: browserId }),
      });
      if (res.ok) {
        const data = await res.json();
        _store(data.access_token || data.token, data.user_id);
      }
      // A non-OK response leaves the visitor unauthenticated; the app-access
      // gate stays open in this mode (browsing works) and the header sign-in
      // form is available. The fallback below keeps any prior guest id usable.
    } catch (e) {
      console.warn('guest session init failed:', e);
    }
    // If guest-login was unreachable but we still hold a prior guest id, keep
    // using it so an offline blip doesn't drop the WebSocket identity.
    if (!app.currentUserId) {
      const priorUid = localStorage.getItem('auth_user_id');
      if (priorUid) { app.currentUserId = priorUid; app.localUserId = priorUid; }
    }
    return;
  }

  // Otherwise (main page, admin_approval): require sign-in.
}

// ── App-wide access gate ───────────────────────────────────────────────────
// Show the full-screen Restricted Access lock (hiding every page AND the chat
// panel — body.app-restricted, styled in ui/shared/css/index.css) whenever the
// visitor isn't signed in / authorized to use this instance. A valid auth token
// is the authorization signal: in "private" (admin-approval) mode a guest has no
// token and an unapproved account can't get one, so both are locked out of every
// page; "open" mode auto-logs-in (token minted) so the owner is never locked;
// after sign-in user-panel.js reloads the page, which re-runs this gate.
//
// Public agent URLs (/{agentId}) run their OWN per-agent anonymous access flow,
// so they are intentionally never gated here.
let _restrictedFormMounted = false;
function _applyAppRestricted(restricted) {
  if (document.body) document.body.classList.toggle('app-restricted', restricted);
  // When the whole-app lock fires (Private mode, no token), swap the static lock
  // card for the real login form so the visitor signs in right here instead of
  // hunting for the corner-avatar dropdown. Mounted once, the first time we lock.
  if (restricted && !_restrictedFormMounted) {
    const overlay = document.getElementById('app-restricted-overlay');
    if (overlay) {
      mountSignInForm(overlay, {
        title: 'Sign in to continue',
        subtext: 'This app is private. Sign in with your account to continue.',
      });
      _restrictedFormMounted = true;
    }
  }
}
function _evaluateAppAccess() {
  if (window.__agentId) { _applyAppRestricted(false); return; }
  // 'open' and 'public_registered' (Open Registration) are open to anonymous
  // visitors — the app is usable without a member sign-in (open mode auto-logs-in
  // the admin; Open Registration hands anons a guest token). Per-page visibility
  // (App Settings) and the Admin Tools gate decide what an anon can actually see,
  // so the whole-app lock must NOT fire here. Only 'admin_approval' (Private)
  // walls the entire app behind sign-in.
  const mode = getAccessMode();
  if (mode === 'open' || mode === 'public_registered') { _applyAppRestricted(false); return; }
  _applyAppRestricted(!isAuthenticated());
}
// Fallback wiring for the static lock card's Sign-in button → opens the corner
// account menu. Normally unused: _applyAppRestricted swaps that card for the
// inline login form (page-access-gate.mountSignInForm) the moment the lock fires,
// removing this button. It still wires here so a pre-JS / unmounted card stays
// functional (the header avatar stays visible in the restricted layout).
(function _initAppRestrictedControls() {
  const btn = document.getElementById('app-restricted-signin');
  if (btn) btn.addEventListener('click', (e) => {
    // Stop this click from reaching the account menu's outside-click handler,
    // and open on the next tick — otherwise the same click that opens the menu
    // immediately registers as a click-outside and closes it again.
    e.stopPropagation();
    setTimeout(() => { try { showLeftOverlay(); } catch (_) {} }, 0);
  });
})();

// Run boot auth, then continue init. For visitors who must sign in this resolves immediately.
const _anonReady = _initBootAuth();
// Evaluate access once boot auth settles (so "open" mode's auto-login token is
// already in place and the owner is never momentarily locked).
_anonReady.then(_evaluateAppAccess).catch(() => _evaluateAppAccess());

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

// Benign browser notifications that fire as "error" events but are NOT real
// errors — they have no functional impact and are not actionable. The
// "ResizeObserver loop…" pair is the canonical one: the browser raises it
// whenever a ResizeObserver callback itself changes layout in the same frame
// (e.g. the file-editor's resizable panes settling when the File Explorer
// opens). Suppress them so they don't trip the red banner or pollute logs.db.
const _BENIGN_ERROR_PATTERNS = [
  /ResizeObserver loop completed with undelivered notifications/i,
  /ResizeObserver loop limit exceeded/i,
];
function _isBenignError(msg) {
  const s = String(msg || '');
  return _BENIGN_ERROR_PATTERNS.some((re) => re.test(s));
}

function _showJsErrorBanner(msg, source, errorEvent) {
  if (_isBenignError(msg)) return;  // non-actionable browser noise — drop it
  _JS_ERRORS.push({ msg, source, ts: new Date().toISOString() });

  // Push the error to the server's diagnostic log (fire-and-forget). Route
  // through the shared pre-module funnel in index.html (window.__reportJsError)
  // so this richer handler and the early catcher collapse to a single logs.db
  // row (deduped by message). Fall back to a direct POST only if the funnel was
  // stripped out of the page shell.
  try {
    var _payload = { message: String(msg).slice(0, 4000), source: source || '' };
    if (errorEvent) {
      var _stackSrc = errorEvent.error || errorEvent.reason;
      if (_stackSrc && _stackSrc.stack) _payload.stack = String(_stackSrc.stack).slice(0, 8000);
      if (typeof errorEvent.lineno === 'number') _payload.source = (source || '') + ':' + errorEvent.lineno + ':' + (errorEvent.colno || 0);
    }
    if (typeof window.__reportJsError === 'function') {
      window.__reportJsError(_payload);
    } else {
      if (typeof app !== 'undefined' && app) {
        if (app.currentUserId) _payload.user_id = app.currentUserId;
        if (app.currentSessionId) _payload.session_id = app.currentSessionId;
        if (app.currentAgentId) _payload.agent_id = app.currentAgentId;
      }
      _payload.url = location.pathname + location.search;
      fetch('/api/v1/log/browser-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(_payload),
      }).catch(function(){});  // best-effort
    }
  } catch (_) {}

  // Title prefix — immediately visible in the browser tab
  document.title = '[JS ERR] ' + document.title.replace(/^\[JS ERR\] /, '');

  // Persistent red banner fixed to top of page — create once, stack messages.
  // KEEP (intentional): hard-coded alarm-red / white here, NOT design-system
  // tokens. This banner surfaces catastrophic JS errors and must render loud and
  // legible even when the stylesheet/theme pipeline failed to load (often the very
  // cause of the error), so it deliberately does not depend on theme variables.
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
  _showJsErrorBanner(e.message, src, e);
});

// Catches async failures — broken dynamic imports, unhandled promise rejections,
// fetch errors that aren't caught, etc.
window.addEventListener('unhandledrejection', (e) => {
  const msg = (e.reason && e.reason.message) ? e.reason.message : String(e.reason);
  _showJsErrorBanner(msg, 'unhandledrejection', e);
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

// Admin Tools is admin-only. Visibility is driven by the `is-admin` body class
// (CSS hides #admin-tools-group + the select option for everyone else — see
// design-system.css). Hidden by default; revealed only once the server confirms
// admin via fetchAdminStatus(). The select option is also disabled for non-admins
// so the mobile dropdown can't select a page the user can't use.
function _applyAdminToolsVisibility() {
  const admin = isAdmin();
  document.body.classList.toggle('is-admin', admin);
  const opt = document.querySelector('#main-tab-select option[value="admin-tools"]');
  if (opt) opt.disabled = !admin;
}

window.addEventListener('admin-status-loaded', _applyAdminToolsVisibility);

// Wait for anon session (if needed) AND a valid access token before running
// the rest of init, so auth_token + user_id are available — and current — for
// all downstream modules.
_bootReady.then(async () => {
  // Make sure the editable chat strings are in before the first session render
  // (the welcome bubble reads them). Capped so a hung config fetch can't stall
  // boot; chatMsg() falls back to its built-in defaults either way.
  await Promise.race([
    _uiMessagesReady,
    new Promise((res) => setTimeout(res, 2000)),
  ]);

  // Hide Admin Tools immediately (synchronous check covers admin
  // bootstrap); fetchAdminStatus() will re-check against the server profile
  // and dispatch 'admin-status-loaded' to refine the visibility.
  _applyAdminToolsVisibility();
  fetchAdminStatus();

  // Fire profile + app settings early so initAgents() can use cached results
  // instead of fetching them sequentially later. These are fire-and-forget;
  // they store results on window.__agentsProfileData / __agentsAppSettingsData.
  if (app.currentUserId) {
    fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) window.__agentsProfileData = d; })
      .catch(() => {});
    fetch('/admin/settings/app', { headers: authHeaders() })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) window.__agentsAppSettingsData = d; })
      .catch(() => {});
  }

  _safeInit('initStorageUi',        initStorageUi);
  _safeInit('initChat',             initChat);
  _safeInit('initSuggestions',      initSuggestions);
  _safeInit('initAbilities',        initAbilities);
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
  //  • initCanvas — registers the live page/visualizer WebSocket handler
  //    (app._canvasHandler), which must receive events even when the
  //    Canvas tab isn't showing.
  //  • initBilling / initAccount — cheap, no tab-gated heavy work.
  _safeInit('initCanvas',   initCanvas);
  _safeInit('initBilling',     initBilling);
  _safeInit('initAccount',     initAccount);
  _safeInit('initSessions',    initSessions);
  _safeInit('initChatResize',  initChatResize);
  // Right-click anywhere → point at a UI element and send it to chat (the
  // user-facing half of the App Control ability). Self-gates on the ability.
  _safeInit('initAppControlPoint', initAppControlPoint);
  _safeInit('initRecorder',    initRecorder);
  _safeInit('initDebugConsole', initDebugConsole);
  _safeInit('initTutorial',    initTutorial);
  // Defer startTutorial() so it doesn't block the critical path — it fetches
  // tutorial state from the server but isn't needed for first paint.
  setTimeout(() => { try { startTutorial(); } catch (_) {} }, 0);

  // Boot complete: the app shell + active tab + chat panel are mounted, so the
  // page is now in its intended view. Lift the black boot curtain — unless a
  // splash has claimed it (then the splash hands off when it's faded in). Two
  // rAFs so the freshly mounted DOM paints before we start fading.
  try {
    if (window.__boot && window.__boot.appReady) {
      requestAnimationFrame(() => requestAnimationFrame(() => window.__boot.appReady()));
    }
  } catch (_) {}
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
