'use strict';

// Session lifecycle actions — switch / delete / pin / inline-rename a session
// (teardown, loading spinner, load chat, drain replayed events, WS resume).
// Module map for this folder: ui/chat-side-panel/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { icon } from '../../shared/js/icons.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../shared/js/delete-control.js';
import { notifySessionsChanged } from '../../shared/js/session-events.js';
import { loopSessionChanged } from '../../main-panel/agents/agent-loop/js/loop.js';
import { loopVisualSessionChanged } from '../../main-panel/agents/agent-loop/js/loop-logic.js';
import { canvasSessionChanged } from '../../main-panel/canvas/js/canvas.js';
import { chatActivitySessionChanged } from '../../shared/js/chat-activity.js';
import { consumeReplayedEventsFor } from '../../shared/js/agentWs.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { addChatBubble } from './chat-bubble.js';
import { _cacheAppendMessage, _captureSessionFocus } from './chat-message-cache.js';
import { _teardownVirtualScroll, _installVirtualScroll } from './chat-virtual-scroll.js';
import { loadSessionChat } from './session-load.js';
import { _sessionsCache, populateSessionSelect, _renderSessionRows, _setTriggerLabel, _fetchRelatedSessions } from './session-list.js';
import { _refreshAgentAbilities, _agentsCache, _lastSessionPerAgent, _saveLastSessionMap, _agentIconFor, _renderAgentRows, _setAgentTriggerLabel } from './session-agent.js';

// ── Interrupt ──────────────────────────────────────────────────────────────

function interruptSession(sessionId) {
  if (!sessionId) return;
  fetch(apiPath('/api/v1/chat/interrupt'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => { /* best-effort */ });
}

// ── Switch session ─────────────────────────────────────────────────────────

async function switchToSession(sid) {
  if (!sid || sid === app.currentSessionId) { /* closeMenu handled by caller */ return; }
  const targetSess = _sessionsCache.find(s => s.id === sid);
  const targetAgentId = (targetSess && targetSess.agent_id) || app.currentAgentId;
  if (targetAgentId && targetAgentId !== app.currentAgentId) {
    app.currentAgentId = targetAgentId;
    _refreshAgentAbilities(targetAgentId);
    try { localStorage.setItem('selectedAgentId', targetAgentId); } catch (_) {}
  }
  if (targetAgentId) {
    _lastSessionPerAgent.set(targetAgentId, sid);
    _saveLastSessionMap();
  }
  app.abortChatStream?.();
  app.currentSessionId = sid;
  localStorage.setItem('terminalSessionId', app.currentSessionId);

  // Reload execution mode for this session (per-session key)
  if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();

  _teardownVirtualScroll();
  if (app.chatMessages) {
    app.chatMessages.innerHTML =
      '<div class="chat-loading-wrap">' +
        '<div class="chat-loading-spinner">' +
          '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg>' +
        '</div>' +
      '</div>';
  }

  await loadSessionChat(sid);
  loopSessionChanged();
  loopVisualSessionChanged();
  canvasSessionChanged();
  chatActivitySessionChanged();
  _renderSessionRows();
  _setTriggerLabel();

  // Drain replayed events
  try {
    const pending = consumeReplayedEventsFor(sid);
    for (const ev of pending) {
      const key = ev.asst_id || ev.turn_id;
      if (ev.type === 'stream' && typeof app.appendStreamToActiveBubble === 'function') {
        app.appendStreamToActiveBubble(ev.content || '', key);
      } else if (ev.type === 'agent_step_end' && typeof app.finalizeAgentStep === 'function') {
        app.finalizeAgentStep(ev.content || '', key);
      } else if (ev.type === 'response' && typeof app.finalizeAgentResponse === 'function') {
        app.finalizeAgentResponse(ev.content || '', key, true);
      } else if (ev.type === 'interrupted' && typeof app.markAgentInterrupted === 'function') {
        app.markAgentInterrupted(ev.asst_id);
      }
      if (ev.type === 'response' && ev.content) {
        _cacheAppendMessage(sid, { role: 'assistant', content: ev.content, id: ev.asst_id || ev.turn_id });
      }
    }
    // If the WS replayed anything for this session, treat the socket as live so
    // the DB-reconcile poll gives it a head start (WS_SILENCE_MS) before kicking
    // in — avoids the poll racing a healthy same-process stream. If nothing was
    // drained (a genuinely cross-worker run), the poll engages normally.
    if (pending.length) {
      if (!app._lastWsEventAt) app._lastWsEventAt = {};
      app._lastWsEventAt[sid] = Date.now();
    }
  } catch (_pendErr) { /* never let drain break navigation */ }

  try {
    if (app.agentWs && app.agentWs.readyState === WebSocket.OPEN) {
      const lastSeq = (app.lastSessionSeq && app.lastSessionSeq[sid]) || 0;
      app.agentWs.send(JSON.stringify({
        type: 'resume',
        session_id: sid,
        last_session_seq: lastSeq,
      }));
    }
  } catch (_e) { /* socket may not be ready */ }

  // If we switched into a session whose turn is still running (isProcessing is
  // set from the DB `run` object by loadSessionChat), start the DB-reconcile
  // loop so a cross-worker in-flight run keeps streaming here too.
  if (app.isProcessing && typeof app.startReconcileLoop === 'function') {
    app.startReconcileLoop();
  }
}

// ── Delete session ─────────────────────────────────────────────────────────

async function deleteSession(sid) {
  interruptSession(sid);
  try {
    const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=local.db'), { method: 'DELETE', headers: { ...authHeaders() } });
    if (res.ok) {
      // Tell the admin Sessions table (and any other listener) to refresh so a
      // session deleted here also disappears there.
      notifySessionsChanged({ action: 'delete', ids: [sid] });
      if (sid === app.currentSessionId) {
        await populateSessionSelect(app.currentUserId);
        const others = _sessionsCache.filter(s => s.id !== sid);
        if (others.length > 0) {
          await switchToSession(others[0].id);
        } else {
          app.currentSessionId = randomUUID();
          localStorage.setItem('terminalSessionId', app.currentSessionId);
          // Fresh session → reset the execution-mode pill (per-session key, defaults
          // to Ask) so it doesn't inherit the deleted session's leftover mode.
          if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
          _teardownVirtualScroll();
          app.chatMessages.innerHTML = '';
          app.addChatBubble('agent', 'Session deleted. New session created.');
        }
      } else {
        await populateSessionSelect(app.currentUserId);
      }
    }
  } catch (e) {
    console.warn('Failed to delete session:', e);
  }
}

// ── Patch session ──────────────────────────────────────────────────────────

async function patchSession(sid, body) {
  try {
    const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=local.db'), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(body),
    });
    if (res.ok) notifySessionsChanged({ action: 'patch', ids: [sid] });
    return res.ok;
  } catch (e) {
    console.warn('Failed to patch session:', e);
    return false;
  }
}

// ── Toggle pin ─────────────────────────────────────────────────────────────

async function togglePin(sid) {
  const sess = _sessionsCache.find(s => s.id === sid);
  if (!sess) return;
  const newPinned = !sess.pinned;
  const ok = await patchSession(sid, { pinned: newPinned });
  if (ok) {
    sess.pinned = newPinned;
    await populateSessionSelect(app.currentUserId);
  }
}

// ── Toggle hidden ──────────────────────────────────────────────────────────

async function toggleHidden(sid) {
  const sess = _sessionsCache.find(s => s.id === sid);
  if (!sess) return;
  const newHidden = !sess.hidden;
  const ok = await patchSession(sid, { hidden: newHidden });
  if (ok) {
    sess.hidden = newHidden;
    await populateSessionSelect(app.currentUserId);
  }
}

// ── Rename (dropdown row and header) ───────────────────────────────────────

function startRename(sid, row) {
  const titleEl = row.querySelector('.session-row-title');
  if (!titleEl) return;
  const sess = _sessionsCache.find(s => s.id === sid);
  const current = (sess && sess.title) || '';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'session-row-title-input';
  input.value = current;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const newTitle = input.value.trim();
    if (commit && newTitle && newTitle !== current) {
      const ok = await patchSession(sid, { title: newTitle });
      if (ok && sess) sess.title = newTitle;
    }
    await populateSessionSelect(app.currentUserId);
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  const _docHandler = (e) => {
    if (input && !input.contains(e.target)) {
      document.removeEventListener('pointerdown', _docHandler, true);
      input.blur();
    }
  };
  document.addEventListener('pointerdown', _docHandler, true);
}

function _headerRenameSession() {
  const labelEl = document.getElementById('session-dropdown-label');
  if (!labelEl) return;
  const sid = app.currentSessionId;
  if (!sid) return;
  const sess = _sessionsCache.find(s => s.id === sid);
  const current = (sess && sess.title) || 'New Session';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'session-row-title-input';
  input.value = current;
  input.style.width = '140px';
  labelEl.replaceWith(input);
  input.style.pointerEvents = 'auto';
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const newTitle = input.value.trim();
    if (commit && newTitle && newTitle !== current) {
      const ok = await patchSession(sid, { title: newTitle });
      if (ok && sess) sess.title = newTitle;
    }
    _setTriggerLabel();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  const _docHandler = (e) => {
    if (input && !input.contains(e.target)) {
      document.removeEventListener('pointerdown', _docHandler, true);
      input.blur();
    }
  };
  document.addEventListener('pointerdown', _docHandler, true);
}

export {
  switchToSession,
  interruptSession,
  patchSession,
  deleteSession,
  togglePin,
  toggleHidden,
  startRename,
  _headerRenameSession,
};