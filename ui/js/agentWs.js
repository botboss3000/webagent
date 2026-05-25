'use strict';

import { app } from './state.js';
import { agentWsUrl, apiPath } from './config.js';
import { logTool } from './toolLog.js';
import { getAuthToken } from './left-login.js';

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
    // Send the JWT alongside the user_id so the server can prove the caller
    // is who they claim to be — without this the per-user broadcast
    // registration would accept any user_id and leak cross-tenant events.
    // Also send the per-session "last session_seq" map so the server can
    // replay any events we missed during the WS gap before going live.
    const resume = (app.lastSessionSeq && typeof app.lastSessionSeq === 'object')
      ? app.lastSessionSeq
      : {};
    app.agentWs.send(JSON.stringify({
      mode: 'user_subscriber',
      user_id: app.currentUserId,
      token: getAuthToken() || '',
      resume,
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
      // Server tells us which sessions still have an in-flight run buffered.
      // Stash it so the chat module can decide to "reattach" the bubble for
      // the active session even if the user navigated away and back.
      if (Array.isArray(event.active_sessions)) {
        app._activeServerSessions = event.active_sessions;
        if (typeof app.onActiveServerSessions === 'function') {
          try { app.onActiveServerSessions(event.active_sessions); } catch(_) {}
        }
      }
      return;
    }

    // Track the highest session_seq we've ever seen per session, so the
    // next WS reconnect can ask the server for only-newer events.
    const _sid = event.session_id || event.sessionId || '';
    if (_sid && typeof event.session_seq === 'number') {
      if (!app.lastSessionSeq) app.lastSessionSeq = {};
      const prev = app.lastSessionSeq[_sid] || 0;
      if (event.session_seq > prev) {
        app.lastSessionSeq[_sid] = event.session_seq;
      }
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
    // WS does NOT update chat bubbles for user-typed turns to avoid races
    // with SSE. The only WS events that affect chat display are:
    //   - "error" / "interrupted" — only when SSE isn't driving (error path)
    //   - "user_message" / "response" from EVENT-TRIGGERED runs — no SSE is
    //     running for those, so WS is the ONLY way the chat bubble can
    //     refresh in real time. Guarded on `!window.__sseActive` so we
    //     never collide with an in-flight user-typed turn.

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

      case 'user_message':
        // Event-triggered runs inject a synthetic user message describing
        // the event. Render it as a chat bubble so the user sees WHAT
        // triggered the assistant reply that's about to stream in.
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (window.__sseActive) break;
        if (event.source === 'event_trigger' && typeof app.addChatBubble === 'function') {
          app.addChatBubble('user', event.content || '', 'event-trigger');
        }
        break;

      case 'stream':
        // Live (or replayed) assistant text for the current session, but
        // ONLY when SSE isn't already driving the bubble. This is the
        // path that lets a refresh / session-switch mid-stream reattach
        // to the in-flight run. Route to the bubble for this event's
        // turn_id so we don't accidentally append to an old turn's bubble.
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (window.__sseActive) break;
        if (typeof app.appendStreamToActiveBubble === 'function') {
          try { app.appendStreamToActiveBubble(event.content || '', event.turn_id); } catch(_) {}
        }
        break;

      case 'response':
        // Final assistant reply from an event-triggered run OR replayed
        // final from an in-flight run we reconnected to. SSE is not
        // running for these, so the WS is the only path to a live update.
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (window.__sseActive) break;
        if (typeof app.finalizeAgentResponse === 'function') {
          try { app.finalizeAgentResponse(event.content || '', event.turn_id, !!event.replayed); } catch(_) {}
        } else if (typeof app.addChatBubble === 'function') {
          app.addChatBubble('agent', event.content || '');
        }
        break;

      case 'automation_updated':
        // Backend tool (event_subscribe / event_unsubscribe / etc.) wrote
        // to agent_automations or agent_event_subscriptions. Tell the
        // Automation tab to re-fetch if it's currently mounted for the
        // affected agent.
        if (typeof app.refreshAutomationTab === 'function') {
          try { app.refreshAutomationTab(event.agent_id); } catch (_) { /* ignore */ }
        }
        break;

      // All other event types (stream, tool_call, tool_result, pipeline,
      // db, attachment) are handled by:
      //   - SSE reader in chat.js (chat bubble updates for user turns)
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