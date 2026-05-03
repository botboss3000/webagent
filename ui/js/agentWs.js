'use strict';

import { app } from './state.js';
import { agentWsUrl } from './config.js';
import { logTool } from './toolLog.js';

let reconnectTimer = null;
let reconnectAttempts = 0;
const MAX_RECONNECT_DELAY = 30000; // 30s max
const INITIAL_RECONNECT_DELAY = 500; // 500ms first retry

export function setAgentStatus(state) {
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
  setAgentStatus('yellow');
  if (app.agentWs) {
    app.agentWs.onclose = null;
    app.agentWs.onerror = null;
    app.agentWs.close();
  }

  app.agentWs = new WebSocket(agentWsUrl());
  app.agentBuffer = '';

  app.agentWs.onopen = () => {
    setAgentStatus('green');
    reconnectAttempts = 0; // Reset backoff on successful connection
    if (typeof app.populateUserSelect === 'function') {
      app.populateUserSelect();
    }
  };

  app.agentWs.onmessage = (ev) => {
    let event;
    try {
      event = JSON.parse(ev.data);
    } catch {
      return;
    }

    // Ignore heartbeat pings from server
    if (event.type === 'ping') return;

    logTool(event);

    switch (event.type) {
      case 'stream':
        app.agentBuffer += event.content;
        app.updateLastBubble(app.agentBuffer, 'streaming');
        break;

      case 'response':
        app.agentBuffer = '';
        app.updateLastBubble(event.content, '', app.lastScreenshotUri);
        app.lastScreenshotUri = '';
        app.isProcessing = false;
        app.chatSend.disabled = false;
        if (app.chatInput.value.trim()) app.chatSend.disabled = false;
        break;

      case 'error':
        app.updateLastBubble('Error: ' + event.message, 'error');
        app.agentBuffer = '';
        app.isProcessing = false;
        app.chatSend.disabled = false;
        break;

      case 'interrupted':
        // Agent task was cancelled due to new user message (steering/interruption).
        // Mark the current streaming bubble and reset the buffer so the new
        // response starts fresh.
        app.updateLastBubble('(interrupted)', 'interrupted');
        app.agentBuffer = '';
        break;

      case 'interrupt_ack':
        // Acknowledgment that streaming stopped. No UI action needed.
        break;

      case 'tool_call':
      case 'tool_result':
        break;

      default:
        break;
    }
  };

  app.agentWs.onclose = () => {
    setAgentStatus('red');
    if (app.isProcessing) {
      app.updateLastBubble('Connection lost while processing.', 'error');
      app.isProcessing = false;
      app.chatSend.disabled = false;
    }
    // Schedule reconnection with exponential backoff
    scheduleAgentReconnect();
  };

  app.agentWs.onerror = () => {
    setAgentStatus('red');
    // onclose will fire after onerror, which triggers scheduleAgentReconnect
  };
}
