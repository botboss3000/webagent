'use strict';

import { app, bindDom } from './state.js';
import { initDbModeUi } from './dbMode.js';
import { initTerminal, connectTerminal } from './terminal.js';
import { initChat } from './chat.js';
import { initReconnect } from './reconnect.js';
import { connectAgent } from './agentWs.js';
import { initTabs } from './tabs.js';
import { initStream } from './stream.js';
import { initLoop } from './loop.js';
import { initDbViewer } from './db/index.js';
import { registerSessionApi, initSessions } from './sessions.js';
import { initSettings } from './settings.js';

bindDom();
initDbModeUi();
initTerminal();
initChat();
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
initDbViewer();
initSettings();
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
