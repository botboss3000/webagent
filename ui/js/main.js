'use strict';

import { app, bindDom } from './state.js';
import { initDbModeUi } from './dbMode.js';
import { initTerminal, connectTerminal } from './terminal.js';
import { initChat } from './chat.js';
import { initReconnect } from './reconnect.js';
import { ensureAttachmentsInit } from './attachments.js';
import { connectAgent } from './agentWs.js';
import { initTabs } from './tabs.js';
import { initStream } from './stream.js';
import { initLoop } from './loop.js';
import { initLoopVisual } from './loop-visual.js';
import { initDbViewer } from './db/index.js';
import { registerSessionApi, initSessions } from './sessions.js';
import { initSettings } from './settings.js';
import { initConnections } from './connections.js';
import { initOptimizer } from './optimizer.js';
import { initGithub } from './github.js';
import { initAutoAgent } from './autoagent.js';
import { initAgents } from './agents.js';
import { isAuthenticated, showLeftOverlay, hideLeftOverlay } from './left-login.js';

bindDom();

// ── Left panel login gate ─────────────────────────────────────────────────
// If not authenticated, show login overlay covering all left-side tabs
if (!isAuthenticated()) {
  showLeftOverlay();
}
initDbModeUi();
initTerminal();
initChat();
ensureAttachmentsInit();
initReconnect();
registerSessionApi();

try {
  connectTerminal();
  connectAgent();
} catch (e) {
  document.title = 'JS ERROR: ' + e.message;
}

initTabs();
initStream();
initLoop();
initLoopVisual();
initDbViewer();
initGithub();
initAutoAgent();
initAgents();
initSettings();
initConnections();
initOptimizer();
initSessions();

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

// ── Sign Out ────────────────────────────────────────────────────────────
document.getElementById('btn-signout')?.addEventListener('click', () => {
  localStorage.removeItem('auth_token');
  localStorage.removeItem('auth_username');
  localStorage.removeItem('auth_user_id');
  localStorage.removeItem('auth_display_name');
  localStorage.removeItem('remember_token');
  localStorage.removeItem('terminalUserId');
  window.location.reload();
});
