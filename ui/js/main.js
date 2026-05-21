'use strict';

import { app, bindDom } from './state.js';
import { initStorageUi } from './storage.js';
import { initTerminal, connectTerminal } from './terminal.js';
import { initChat } from './chat.js';
import { initReconnect } from './reconnect.js';
import { ensureAttachmentsInit } from './attachments.js';
import { connectAgent } from './agentWs.js';
import { initTabs } from './tabs.js';
import { initStream } from './stream.js';
import { initLoop } from './loop.js';
import { initLoopVisual } from './loop-logic.js';
import { initDbViewer } from './db/index.js';
import { registerSessionApi, initSessions } from './sessions.js';
import { initGithub } from './github.js';
import { initAutoAgent } from './autoagent.js';
import { initAgents } from './agents.js';
import { initAppConfig } from './app-config.js';
import { initAccount } from './account.js';
import { initChatResize } from './chatResize.js';
import { isAuthenticated, showLeftOverlay, hideLeftOverlay } from './left-login.js';
import './icons.js'; // auto-renders Lucide icons on DOM mutations

bindDom();

// ── Anonymous session for public agent URLs ──────────────────────────────
// If the server injected __agentId and no auth token exists, create an
// anonymous session so the visitor can chat without logging in.
async function _initAnonSession() {
  if (!window.__agentId) return;
  if (localStorage.getItem('auth_token')) return;

  let browserId = localStorage.getItem('anon_browser_id');
  if (!browserId) {
    browserId = crypto.randomUUID();
    localStorage.setItem('anon_browser_id', browserId);
  }

  try {
    const res = await fetch(`/api/v1/agents/${window.__agentId}/anon-session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ browser_id: browserId }),
    });
    if (!res.ok) return;
    const data = await res.json();
    localStorage.setItem('auth_token', data.token);
    localStorage.setItem('auth_user_id', data.user_id);
    app.currentUserId = data.user_id;
    app.localUserId = data.user_id;
  } catch (e) {
    console.warn('anon session init failed:', e);
  }
}

// Run anon auth, then continue init. For non-anon visitors this resolves immediately.
const _anonReady = _initAnonSession();

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

// Wait for anon session (if needed) before running the rest of init,
// so auth_token + user_id are available for all downstream modules.
_anonReady.then(() => {
  // ── Left panel login gate ───────────────────────────────────────────
  if (!isAuthenticated()) {
    showLeftOverlay();
  }
  initStorageUi();
  initTerminal();
  initChat();
  ensureAttachmentsInit();
  initReconnect();
  registerSessionApi();

  try {
    connectTerminal();
    connectAgent();
  } catch (e) {
    _showJsErrorBanner(e.message, 'connectAgent/connectTerminal');
  }

  _safeInit('initTabs',        initTabs);
  _safeInit('initStream',      initStream);
  _safeInit('initLoop',        initLoop);
  _safeInit('initLoopVisual',  initLoopVisual);
  _safeInit('initDbViewer',    initDbViewer);
  _safeInit('initGithub',      initGithub);
  _safeInit('initAutoAgent',   initAutoAgent);
  _safeInit('initAgents',      initAgents);
  _safeInit('initAppConfig',   initAppConfig);
  _safeInit('initAccount',     initAccount);
  _safeInit('initSessions',    initSessions);
  _safeInit('initChatResize',  initChatResize);
});

// ── Visibility change: reconnect when user returns to this tab ──
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    const termOk = app.termWs && app.termWs.readyState === WebSocket.OPEN;
    const agentOk = app.agentWs && app.agentWs.readyState === WebSocket.OPEN;
    if (!termOk) connectTerminal();
    if (!agentOk) connectAgent();
  }
});

// ── Fallback poll every 10s in case visibility change misses something ──
setInterval(() => {
  if (!app.termWs || app.termWs.readyState > 1) connectTerminal();
  if (!app.agentWs || app.agentWs.readyState > 1) connectAgent();
}, 10000);
