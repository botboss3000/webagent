'use strict';

// Reconnect coordinator — re-establishes the agent WebSocket and all terminals
// after the tab wakes / network returns, and renders restart status. initReconnect().

import { reconnectAllTerminals } from '../../admin-tools/terminal/terminal-view.js';
import { connectAgent } from './agentWs.js';
import { apiPath } from './config.js';
import { setChatHeaderReachable } from './user-panel.js';
import { setOfflineReadOnly } from './offline-reader.js';

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

// Connection state — drives the chat icon aura in the header (#chat-toggle-btn):
// orange pulse while the server is starting/connecting, one green flash when it
// becomes ready (then off), red pulse while unreachable. This replaces the old
// #server-connection-state dot + label in #status-right (removed from index.html).
let _lastConnPhase = null;

function setConnectionState(state, pending = []) {
  const chatBtn = document.getElementById('chat-toggle-btn');
  if (!chatBtn) return;
  chatBtn.classList.remove('chat-aura-connecting', 'chat-aura-red', 'chat-aura-flash');
  if (state === 'connecting' || state === 'starting') {
    chatBtn.classList.add('chat-aura-connecting');
    chatBtn.title = pending.length
      ? `Preparing: ${pending.join(', ')}`
      : 'Preparing background services';
    _lastConnPhase = 'connecting';
  } else if (state === 'unreachable') {
    chatBtn.classList.add('chat-aura-red');
    chatBtn.title = 'Server unreachable';
    _lastConnPhase = 'red';
  } else {
    // Ready — one short flash on the transition out of connecting/unreachable,
    // then the aura turns off until the next state change. Steady-state ready
    // polls never touch the title or re-fire the flash.
    if (_lastConnPhase === 'connecting' || _lastConnPhase === 'red') {
      void chatBtn.offsetWidth; // restart the one-shot animation
      chatBtn.classList.add('chat-aura-flash');
      chatBtn.title = 'Toggle chat panel (long-press for a new session)';
    }
    _lastConnPhase = 'ready';
  }
}

/* ── Server health dot ─────────────────────────────────────── */
function setHealthDot(state) {
  // The core server is usable during optional background initialization. Only a
  // red state disables fresh controls; orange is an honest informational state.
  setChatHeaderReachable(state !== 'red');
  setOfflineReadOnly(state === 'red', { source: 'health' });
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
    dot.title = 'Server connecting…';
  }
}

let healthPollInterval = null;

function startHealthPoll() {
  if (healthPollInterval) clearInterval(healthPollInterval);
  const poll = () => {
    fetch(apiPath('/health'))
      .then(async (r) => {
        if (!r.ok) {
          setConnectionState('unreachable');
          setHealthDot('red');
          return;
        }
        const payload = await r.json().catch(() => ({}));
        const phase = payload.initialization || 'ready';
        const connecting = phase === 'starting' || phase === 'connecting';
        setConnectionState(phase, payload.pending || []);
        // Core HTTP + cached session reads are available while optional workers
        // catch up, so keep normal controls usable; orange is informational.
        setHealthDot(connecting ? 'orange' : 'green');
      })
      .catch(() => {
        setConnectionState('unreachable');
        setHealthDot('red');
      });
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
      setConnectionState('connecting');
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
            setConnectionState('ready');
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
