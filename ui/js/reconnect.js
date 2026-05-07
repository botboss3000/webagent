'use strict';

import { app } from './state.js';
import { connectTerminal } from './terminal.js';
import { connectAgent } from './agentWs.js';

export function initReconnect() {
  const reconnectBtn = document.getElementById('btn-reconnect');
  if (reconnectBtn) {
    reconnectBtn.addEventListener('click', () => {
      connectTerminal();
      connectAgent();
      app.addChatBubble('agent', 'Reconnecting...');
    });
  }

  document.getElementById('btn-restart').addEventListener('click', async () => {
    const btn = document.getElementById('btn-restart');
    btn.classList.add('restarting');
    btn.textContent = '⏻ Restarting...';
    app.addChatBubble('agent', 'Restarting server...');
    try {
      await fetch('/api/v1/restart', { method: 'POST' });
    } catch {
      /* server will go down */
    }
    const poll = setInterval(async () => {
      try {
        const r = await fetch('/health');
        if (r.ok) {
          clearInterval(poll);
          btn.classList.remove('restarting');
          btn.textContent = '⏻ Restart';
          app.addChatBubble('agent', 'Server restarted. Reconnecting...');
          connectTerminal();
          connectAgent();
        }
      } catch {
        /* server still down */
      }
    }, 2000);
  });
}
