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

/* ── Server health dot ─────────────────────────────────────── */
function setHealthDot(state) {
  const dot = document.getElementById('admin-health-dot');
  if (!dot) return;
  dot.className = 'health-dot';
  if (state === 'green') {
    dot.classList.add('health-dot-green');
    dot.title = 'Server healthy';
  } else if (state === 'red') {
    dot.classList.add('health-dot-red');
    dot.title = 'Server unreachable';
  } else if (state === 'orange') {
    dot.classList.add('health-dot-orange');
    dot.title = 'Server restarting…';
  }
}

let healthPollInterval = null;

function startHealthPoll() {
  if (healthPollInterval) clearInterval(healthPollInterval);
  const poll = () => {
    fetch(apiPath('/health'))
      .then(r => { setHealthDot(r.ok ? 'green' : 'red'); })
      .catch(() => { setHealthDot('red'); });
  };
  poll();
  healthPollInterval = setInterval(poll, 10000);
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
      setHealthDot('orange');
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
            setHealthDot('green');
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

  // Start health polling
  startHealthPoll();
}
