'use strict';

// Per-user receive-only WebSocket subscriber — streams live agent events (tokens,
// tool calls, status) for the signed-in user, with reconnect/backoff. Exposes
// registerSessionSubscriber() so a chat widget can claim a session's live events.

import { app } from './state.js';
import { agentWsUrl, apiPath } from './config.js';
import { logTool } from './toolLog.js';
import { getAuthToken } from './left-login.js';
import { renderAvatar } from './user-avatar.js';
import { getActive } from './accounts.js';
import { handleAttachmentEvent } from './attachments.js';

let reconnectTimer = null;
let reconnectAttempts = 0;
const MAX_RECONNECT_DELAY = 30000;
const INITIAL_RECONNECT_DELAY = 500;

// ── Pending replay buffer ──
// When the WS reconnects (or a fresh page load completes its handshake) the
// server can replay buffered `stream` / `response` / `interrupted` events
// for any of the user's sessions that still have an in-flight run. If those
// events arrive while the UI is on a DIFFERENT session — or before the
// user has navigated anywhere — we used to drop them. Now we stash them
// per-session and `consumeReplayedEventsFor(sid)` lets the chat module
// flush them when the user switches in. Capped per session to avoid leaks
// for runaway runs.
const _pendingReplay = new Map(); // session_id -> Array<event>
const PENDING_REPLAY_CAP = 500;

function _stashReplay(sid, event) {
  if (!sid) return;
  let arr = _pendingReplay.get(sid);
  if (!arr) {
    arr = [];
    _pendingReplay.set(sid, arr);
  }
  if (arr.length >= PENDING_REPLAY_CAP) return; // drop oldest-overflow silently
  arr.push(event);
}

export function consumeReplayedEventsFor(sid) {
  if (!sid) return [];
  const arr = _pendingReplay.get(sid) || [];
  _pendingReplay.delete(sid);
  return arr;
}

// ── Session subscribers (chat widgets) ──
// A floating chat widget (ui/chat-widget/) runs its OWN session alongside the
// main panel. It claims that session's events here so they reach the widget
// instead of being dropped/stashed as "foreign". One handler per session_id;
// registering drains any events already stashed for that session (a reconnect
// replay can land before the widget finishes registering).
const _sessionSubscribers = new Map(); // session_id -> handler(event)

export function registerSessionSubscriber(sessionId, handler) {
  if (!sessionId || typeof handler !== 'function') return () => {};
  _sessionSubscribers.set(sessionId, handler);
  for (const ev of consumeReplayedEventsFor(sessionId)) {
    try { handler(ev); } catch (_) { /* widget render error — non-fatal */ }
  }
  return () => {
    if (_sessionSubscribers.get(sessionId) === handler) _sessionSubscribers.delete(sessionId);
  };
}

function setAgentStatus(state) {
  if (!app.aDot || !app.aStat) return;
  app.aDot.className = 'status-dot ' + state;
  app.aStat.textContent =
    state === 'green' ? 'Agent' : state === 'yellow' ? 'Connecting...' : 'Disconnected';
}

function cancelAgentReconnect() {
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
    // Derive the handshake user_id from the SAME source as the token (the active
    // account) so the pair can never desync — e.g. after an account switch or a
    // token refresh that updated auth_token but not app.currentUserId (a boot-
    // time snapshot). A mismatched pair is rejected by the server with "token
    // subject does not match user_id". Fall back to app.currentUserId for flows
    // with no tracked account (open mode, public per-agent anon sessions).
    const _active = getActive();
    const _uid = (_active && _active.user_id) || app.currentUserId;
    app.agentWs.send(JSON.stringify({
      mode: 'user_subscriber',
      user_id: _uid,
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
      // Lightweight refresh on WS (re)connect — don't re-fetch agents/sessions,
      // initSessions() already loaded them eagerly. Just update the user avatar
      // in case the account changed in another tab.
      const slot = document.getElementById('top-user-avatar-slot');
      if (slot) {
        const active = getActive();
        const acct = active || { display_name: app.currentUserId || 'None', username: app.currentUserId || '' };
        slot.innerHTML = '';
        slot.appendChild(renderAvatar(acct, 'sm'));
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
        // Mirror to localStorage so a hard refresh doesn't lose the cursor
        // and the server can replay-from-correct-seq on next handshake.
        try {
          localStorage.setItem(
            'webagent.lastSessionSeq.v1',
            JSON.stringify(app.lastSessionSeq),
          );
        } catch (_) { /* quota / private mode — non-fatal */ }
      }
    }

    // Track WebSocket liveness per session: when did we last receive a chat
    // event for this session? The DB-reconcile poll (chat-reconcile.js) only
    // engages after the WS has been silent for a couple seconds, so when the WS
    // is delivering (same-process) the poll never fires, and when it can't
    // (browser and agent on different workers/instances) the poll takes over.
    if (_sid && (event.type === 'stream' || event.type === 'response'
                 || event.type === 'agent_step_end' || event.type === 'interrupted')) {
      if (!app._lastWsEventAt) app._lastWsEventAt = {};
      app._lastWsEventAt[_sid] = Date.now();
    }

    // The Loop / Flow debug panels and the chat-pill "thinking" glow reflect the
    // session you're VIEWING, so only feed them events for the active session.
    // Events now carry session_id; untagged ones pass through defensively.
    const _forCurrent = !_sid || _sid === app.currentSessionId;

    // Forward to tool log panel (global — intentionally shows all tool activity)
    try { logTool(event); } catch(e) { /* not mounted */ }

    // Forward to stream/loop/flow debug panels (handles ALL event types)
    if (_forCurrent && app._loopHandler) {
      try { app._loopHandler(event); } catch(e) { /* ignore */ }
    }
    if (_forCurrent && app._loopVisualHandler) {
      try { app._loopVisualHandler(event); } catch(e) { /* ignore */ }
    }
    // Gen UI has its own per-visualizer-session logic — leave unguarded.
    if (app._genuiHandler) {
      try { app._genuiHandler(event); } catch(e) { /* ignore */ }
    }
    // Drive the chat pill "thinking" glow + activity-note ticker (current session).
    if (_forCurrent && app._chatActivityHandler) {
      try { app._chatActivityHandler(event); } catch(e) { /* ignore */ }
    }

    // ── Session subscribers (chat widgets) get first claim ──
    // If a widget owns this event's session, hand the event over. When the
    // widget's session is ALSO the main panel's current session, fall through
    // so the panel renders too. session_title always falls through — it
    // updates the global session-name cache, not just the panel.
    // Note ui_command stays gated to the CURRENT session below, so a widget
    // task can never rearrange the user's screen.
    const _claimed = _sid ? _sessionSubscribers.get(_sid) : null;
    if (_claimed) {
      try { _claimed(event); } catch (_) { /* widget render error — non-fatal */ }
      if (event.type !== 'session_title' && _sid !== app.currentSessionId) return;
    }

    // ── Chat bubble display — the WS is the sole real-time renderer ──
    // Every agent turn emits stream, agent_step_end, and response events
    // through this socket. The DB is the source of truth for cold loads
    // (refresh / device switch); the WS is the live accelerant. Events
    // for foreign sessions are stashed via _stashReplay so the session
    // loader can drain them when the user navigates in.

    const eventSessionId = event.session_id || event.sessionId || '';

    switch (event.type) {
      case 'error':
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        app.updateLastBubble('Error: ' + event.message, 'error');
        app.agentBuffer = '';
        app.isProcessing = false;
        if (app.chatSend) app.chatSend.disabled = false;
        break;

      case 'interrupted':
        // Terminal for the step it names. Marks that step's bubble interrupted
        // (keeping its partial text). A new message may have already started a
        // replacement run, which re-engages on its own.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        if (typeof app.markAgentInterrupted === 'function') {
          try { app.markAgentInterrupted(event.asst_id); } catch(_) {}
        } else {
          app.updateLastBubble('(interrupted)', 'interrupted');
          app.isProcessing = false;
          if (app.chatSend) app.chatSend.disabled = false;
        }
        break;

      case 'resumed':
        // The self-healing layer re-ignited a run that had stopped involuntarily
        // (server restart / frozen / zombie). Clear any stale "interrupted" or
        // "Connection lost" state and re-engage the thinking indicator — the
        // resumed turn streams in as a fresh bubble right after this.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        app.isProcessing = true;
        if (app.chatSend) app.chatSend.disabled = true;
        break;

      case 'user_message': {
        // Every user message (typed by this user, sent from another device, or
        // injected by an event-triggered run) is broadcast here so ALL devices
        // viewing the session render it live — the same way agent messages do.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        if (!app.chatMessages) break;
        const mid = event.id || event.interaction_id || '';
        const cont = event.content || '';
        // Already shown (by interaction id)? Dedup.
        if (mid && app.chatMessages.querySelector(
              `.chat-bubble.user[data-msg-id="${CSS.escape(String(mid))}"]`)) break;
        // Adopt the sender's own optimistic bubble (rendered locally before the
        // id was known) so we don't double it. Match by text on an untagged
        // user bubble; tag it with the id instead of adding a new one.
        if (mid) {
          const candidates = app.chatMessages.querySelectorAll('.chat-bubble.user:not([data-msg-id])');
          for (let i = candidates.length - 1; i >= 0; i--) {
            const b = candidates[i];
            const t = (b.querySelector('.bubble-body')?.textContent || '').trim();
            if (t === cont.trim()) { b.setAttribute('data-msg-id', String(mid)); break; }
          }
          if (app.chatMessages.querySelector(
                `.chat-bubble.user[data-msg-id="${CSS.escape(String(mid))}"]`)) break;
        }
        if (typeof app.addChatBubble === 'function') {
          const cls = event.source === 'event_trigger' ? 'event-trigger' : undefined;
          app.addChatBubble('user', cont, cls, undefined, undefined, mid || undefined);
        }
        break;
      }

      case 'stream':
        // Live (or replayed) assistant text for the current session.
        // Routes to the bubble for this event's asst_id so each step
        // renders as its own bubble; falls back to turn_id for legacy
        // events. Lets a refresh / session-switch mid-stream reattach
        // to an in-flight run.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          console.debug('DEBUG-TAG:agentWs-stream-skipped', { eventSessionId, currentSessionId: app.currentSessionId, replayed: event.replayed });
          break;
        }
        console.debug('DEBUG-TAG:agentWs-stream-render', { content: (event.content || '').slice(0, 40), asst_id: event.asst_id, turn_id: event.turn_id, hasAppender: typeof app.appendStreamToActiveBubble });
        if (typeof app.appendStreamToActiveBubble === 'function') {
          try { app.appendStreamToActiveBubble(event.content || '', event.asst_id || event.turn_id); } catch(_) {}
        } else {
          console.warn('DEBUG-TAG:agentWs-stream-no-appender');
        }
        break;

      case 'agent_step_end':
        // An intermediate assistant step (text before tool calls) finished —
        // finalize its own bubble so the user sees every step, not just the last.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        if (typeof app.finalizeAgentStep === 'function') {
          try { app.finalizeAgentStep(event.content || '', event.asst_id || event.turn_id); } catch(_) {}
        }
        break;

      case 'response':
        // Final assistant reply — the turn completed without more tool
        // calls. Can arrive for user-typed turns, event-triggered runs,
        // or replayed from a reconnect mid-stream.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          console.debug('DEBUG-TAG:agentWs-response-skipped', { eventSessionId, currentSessionId: app.currentSessionId, replayed: event.replayed });
          break;
        }
        console.debug('DEBUG-TAG:agentWs-response-render', { content: (event.content || '').slice(0, 40), asst_id: event.asst_id, turn_id: event.turn_id, hasFinalizer: typeof app.finalizeAgentResponse });
        if (typeof app.finalizeAgentResponse === 'function') {
          try { app.finalizeAgentResponse(event.content || '', event.asst_id || event.turn_id, !!event.replayed); } catch(_) {}
        } else if (typeof app.addChatBubble === 'function') {
          app.addChatBubble('agent', event.content || '');
        } else {
          console.warn('DEBUG-TAG:agentWs-response-no-finalizer');
        }
        break;

      case 'execution_mode': {
        // The agent switched Ask/Plan/Auto mid-conversation (set_execution_mode
        // tool — typically Plan→Auto once the user approved a plan). Update the
        // pill for the session being viewed so the user SEES it flip, and so the
        // next message carries the new mode. Don't honor on replay (a stale
        // reconnect switch shouldn't silently change the user's mode).
        if (event.replayed) break;
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (typeof app.setExecutionMode === 'function') {
          try { app.setExecutionMode(event.mode); } catch (_) { /* ignore */ }
        }
        break;
      }

      case 'repo_change_notice':
        // Passive heads-up from the Git Control "Notify me about repo changes"
        // watcher (plugins/abilities/Administrator/git_control). The working tree
        // newly diverged from the last commit (uncommitted edits and/or unpushed
        // commits). No agent run happened — it's an FYI. The notice is also
        // persisted into the session, so if it's not the one being viewed it
        // shows when the user opens that chat. Render live only when it targets
        // the session currently on screen, and never on replay (a stale
        // reconnect shouldn't re-announce an old change).
        if (event.replayed) break;
        if (eventSessionId && eventSessionId === app.currentSessionId
            && typeof app.addChatBubble === 'function') {
          // role 'agent' so it renders on the assistant side (matches how the
          // persisted role='assistant' row is re-rendered on session load).
          try { app.addChatBubble('agent', event.message || 'Repo changes detected.', 'git-watch-notice'); }
          catch (_) { /* render error — the persisted row still covers it */ }
        }
        break;

      case 'agent_created':
      case 'agent_trashed':
      case 'agent_deleted':
      case 'agent_restored':
      case 'agent_status':
        // An agent was created / trashed / permanently deleted / restored in
        // another tab or device, or one started/stopped a run. Let the Agents
        // page sync its grid (and per-card status dot) without a manual refresh.
        if (typeof app.onAgentLifecycleEvent === 'function') {
          try { app.onAgentLifecycleEvent(event); } catch (_) { /* ignore */ }
        }
        break;

      case 'ui_command': {
        // Agent-driven screen control (the "App Control" ability). The agent's
        // set_app_view tool emits this to rearrange the viewer's own screen:
        // switch the main view, show/hide the chat panel, resize it. Only obey
        // commands for the session the user is currently viewing, and never on
        // replay — a stale view-switch on reconnect would yank the user around.
        if (event.replayed) break;
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (event.action === 'set_view') {
          if (event.view && typeof window.__setMainTab === 'function') {
            try { window.__setMainTab(event.view); } catch (_) { /* ignore */ }
          }
          if (typeof event.show_chat === 'boolean' && typeof window.__applyChatVisible === 'function') {
            try { window.__applyChatVisible(event.show_chat); } catch (_) { /* ignore */ }
          }
          if (typeof event.chat_width === 'number' && typeof window.__setChatPanelWidth === 'function') {
            try { window.__setChatPanelWidth(event.chat_width); } catch (_) { /* ignore */ }
          }
        }
        break;
      }

      case 'tunnel_state': {
        // Terminal-tunnel mode toggled (user drives a terminal through chat).
        // The chat module mounts/unmounts the tunnel banner + embedded terminal.
        // Honor on replay too, so a reconnect re-mounts an active tunnel.
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (typeof app.onTunnelState === 'function') {
          try { app.onTunnelState(event); } catch (_) { /* ignore */ }
        }
        break;
      }

      case 'terminal_chat_state':
      case 'terminal_stream':
      case 'terminal_step_end': {
        // Terminal Chat engine — live terminal view replaces chat bubbles.
        // The engine (plugins/engines/terminal_chat/) emits these events;
        // the frontend module (chat-terminal-engine.js) handles the xterm.js
        // mount/unmount and data piping.
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (typeof app._terminalChatHandler === 'function') {
          try { app._terminalChatHandler(event); } catch (_) { /* ignore */ }
        }
        break;
      }

      case 'session_title': {
        // Auto-generated session name (Session Namer ability,
        // plugins/abilities/Core/session_titler/). status
        // 'generating' lights the header spinner; 'done' swaps in the new name.
        // Not gated on the active session — the cache is updated for any session
        // so the sidebar reflects the new name on its next render too.
        if (typeof app.applySessionTitle === 'function') {
          try { app.applySessionTitle(event); } catch (_) { /* ignore */ }
        }
        break;
      }

      case 'attachment':
        // Image/file attachment reference from the server (both user and agent).
        handleAttachmentEvent(event);
        break;

      // All other event types (tool_call, tool_result, pipeline, db) are handled by app._loopHandler etc.
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
    // Drop the thinking glow — a live turn (if any) keeps running server-side
    // and re-lights via replay on reconnect.
    if (app.chatActivityStop) { try { app.chatActivityStop(); } catch (_) {} }
    scheduleAgentReconnect();
  };

  app.agentWs.onerror = () => {
    setAgentStatus('red');
  };
}