'use strict';

import { app } from './state.js';
import { agentWsUrl, apiPath } from './config.js';
import { logTool } from './toolLog.js';

let reconnectTimer = null;
let reconnectAttempts = 0;
const MAX_RECONNECT_DELAY = 30000;
const INITIAL_RECONNECT_DELAY = 500;

export function setAgentStatus(state) {
  if (!app.aDot || !app.aStat) return;
  app.aDot.className = 'status-dot ' + state;
  app.aStat.textContent =
    state === 'green' ? 'Agent' : state === 'yellow' ? 'Connecting...' : 'Disconnected';
}

export function cancelAgentReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectAttempts = 0;
}

function scheduleAgentReconnect() {
  cancelAgentReconnect();
  const delay = Math.min(
    INITIAL_RECONNECT_DELAY * Math.pow(2, reconnectAttempts),
    MAX_RECONNECT_DELAY
  );
  reconnectAttempts++;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectAgent();
  }, delay);
}

export function connectAgent() {
  if (!app.currentUserId) {
    setAgentStatus('red');
    return;
  }

  setAgentStatus('yellow');

  if (app.agentWs) {
    app.agentWs.onclose = null;
    app.agentWs.onerror = null;
    app.agentWs.close();
  }

  app.agentWs = new WebSocket(agentWsUrl());

  app.agentWs.onopen = () => {
    app.agentWs.send(JSON.stringify({
      mode: 'user_subscriber',
      user_id: app.currentUserId,
    }));
  };

  app.agentWs.onmessage = (ev) => {
    let event;
    try {
      event = JSON.parse(ev.data);
    } catch {
      return;
    }

    if (event.type === 'ping') return;

    if (event.type === 'subscribed') {
      setAgentStatus('green');
      reconnectAttempts = 0;
      if (typeof app.populateUserSelect === 'function') {
        app.populateUserSelect();
      }
      return;
    }

    // Forward to tool log panel
    try { logTool(event); } catch(e) { /* not mounted */ }

    // Forward to stream/loop/flow debug panels (handles ALL event types)
    if (app._loopHandler) {
      try { app._loopHandler(event); } catch(e) { /* ignore */ }
    }
    if (app._loopVisualHandler) {
      try { app._loopVisualHandler(event); } catch(e) { /* ignore */ }
    }
    if (app._autoAgentHandler) {
      try { app._autoAgentHandler(event); } catch(e) { /* ignore */ }
    }

    // ── Chat bubble display is handled by SSE in chat.js ──
    // WS does NOT update chat bubbles to avoid race with SSE.
    // The only WS events that affect chat display are:
    //   - "error" (when SSE connection failed and WS still sees error)
    //   - "interrupted" (same)
    // These are processed only if the event belongs to the current session.

    const eventSessionId = event.session_id || event.sessionId || '';

    switch (event.type) {
      case 'error':
        // Only update if SSE isn't actively driving the current session
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (window.__sseActive) break;
        app.updateLastBubble('Error: ' + event.message, 'error');
        app.agentBuffer = '';
        app.isProcessing = false;
        if (app.chatSend) app.chatSend.disabled = false;
        break;

      case 'interrupted':
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (window.__sseActive) break;
        app.updateLastBubble('(interrupted)', 'interrupted');
        app.agentBuffer = '';
        break;

      // All other event types (stream, response, tool_call, tool_result,
      // pipeline, db, attachment) are handled by:
      //   - SSE reader in chat.js (chat bubble updates)
      //   - app._loopHandler / app._loopVisualHandler (debug panels)
      default:
        break;
    }
  };

  app.agentWs.onclose = () => {
    setAgentStatus('red');
    if (app.isProcessing) {
      if (app.updateLastBubble) {
        app.updateLastBubble('Connection lost.', 'error');
      }
      app.isProcessing = false;
      if (app.chatSend) app.chatSend.disabled = false;
    }
    scheduleAgentReconnect();
  };

  app.agentWs.onerror = () => {
    setAgentStatus('red');
  };
}