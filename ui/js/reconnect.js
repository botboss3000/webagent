'use strict';

import { reconnectAllTerminals } from './files.js';
import { connectAgent } from './agentWs.js';
import { apiPath } from './config.js';

function setRestartStatus(msg) {
  const el = document.getElementById('restart-status');
  if (!el) return;
  if (msg) {
    el.textContent = msg;
    el.style.display = '';
  } else {
    el.textContent = '';
    el.style.display = 'none';
  }
}

export function initReconnect() {
  const reconnectBtn = document.getElementById('btn-reconnect');
  if (reconnectBtn) {
    reconnectBtn.addEventListener('click', () => {
      reconnectAllTerminals();
      connectAgent();
    });
  }

  const restartBtn = document.getElementById('btn-restart');
  if (restartBtn) {
    restartBtn.addEventListener('click', async () => {
      restartBtn.classList.add('restarting');
      setRestartStatus('Restarting server...');
      try {
        await fetch(apiPath('/api/v1/restart'), { method: 'POST' });
      } catch {
        /* server will go down */
      }
      const poll = setInterval(async () => {
        try {
          const r = await fetch(apiPath('/health'));
          if (r.ok) {
            clearInterval(poll);
            restartBtn.classList.remove('restarting');
            setRestartStatus('Reconnecting...');
            reconnectAllTerminals();
            connectAgent();
            setTimeout(() => setRestartStatus(null), 3000);
          }
        } catch {
          /* server still down */
        }
      }, 2000);
    });
  }
}
