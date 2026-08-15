'use strict';

// Session lifecycle actions — switch / delete / pin / inline-rename a session
// (teardown, loading spinner, load chat, drain replayed events, WS resume).
// Module map for this folder: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { icon } from '../../shared/js/icons.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../shared/js/delete-control.js';
import { notifySessionsChanged } from '../../shared/js/session-events.js';
import { loopSessionChanged } from '../../main-panel/agents/agent-loop/js/loop.js';
import { loopVisualSessionChanged } from '../../main-panel/agents/agent-loop/js/loop-logic.js';
import { genuiSessionChanged } from '../../main-panel/genui/js/genui.js';
import { chatActivitySessionChanged } from '../../shared/js/chat-activity.js';
import { consumeReplayedEventsFor } from '../../shared/js/agentWs.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { addChatBubble } from './chat-bubble.js';
import { _cacheAppendMessage, _captureSessionFocus } from './chat-message-cache.js';
import { _teardownVirtualScroll, _installVirtualScroll } from './chat-virtual-scroll.js';
import { loadSessionChat } from './session-load.js';
import { _sessionsCache, populateSessionSelect, _renderSessionRows, _setTriggerLabel, _clearSessionHeader, _fetchRelatedSessions } from './session-list.js';
import { _refreshAgentAbilities, _agentsCache, _lastSessionPerAgent, _saveLastSessionMap, _agentIconFor, _renderAgentRows, _setAgentTriggerLabel } from './session-agent.js';
import { storageAdapter } from './storage/storage-adapter.js';

// ── Interrupt ──────────────────────────────────────────────────────────────

function interruptSession(sessionId) {
  if (!sessionId) return;
  if (storageAdapter.isBrowser) return; // no server-side run
  fetch(apiPath('/api/v1/chat/interrupt'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => { /* best-effort */ });
}

// ── Switch session ─────────────────────────────────────────────────────────

// The loading spinner shown in the chat area while a session's messages are
// fetched. Shared with session-swipe.js so the swipe handoff can pre-render
// the exact same markup behind the arrow+text panel (single source of truth).
export function loadingSpinnerMarkup() {
  return '<div class="chat-loading-wrap">' +
    '<div class="chat-loading-spinner">' +
      '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg>' +
    '</div>' +
  '</div>';
}

async function switchToSession(sid) {
  if (!sid || sid === app.currentSessionId) {
    app._swipeFadeIn = false; // a swipe that lands on the same session gets no fade
    /* closeMenu handled by caller */ return;
  }
  const targetSess = _sessionsCache.find(s => s.id === sid);
  const targetAgentId = (targetSess && targetSess.agent_id) || app.currentAgentId;
  if (targetAgentId && targetAgentId !== app.currentAgentId) {
    app.currentAgentId = targetAgentId;
    _refreshAgentAbilities(targetAgentId);
    try { localStorage.setItem('selectedAgentId', targetAgentId); } catch (_) {}
  }
  // Rebuild header for the new agent's per-agent chat_ui
  try {
    const { reapplyChatControlsConfig } = await import('../../chat-controls/chat-controls-config.js');
    reapplyChatControlsConfig();
  } catch (_) {}
  if (targetAgentId) {
    _lastSessionPerAgent.set(targetAgentId, sid);
    _saveLastSessionMap();
  }
  app.abortChatStream?.();
  app.currentSessionId = sid;
  localStorage.setItem('terminalSessionId', app.currentSessionId);

  // Blank the header NOW (synchronously — no fetches) so it never shows the
  // old session's identity while the new one loads. Each element re-renders
  // in parallel from its own loader: mode/target from localStorage below,
  // label/agent-name/status/chips when loadSessionChat's fetch lands.
  _clearSessionHeader();

  // Reload execution mode for this session (per-session key)
  if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
  if (typeof app.reloadThinking === 'function') app.reloadThinking();
  if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
  if (typeof app.reloadFooterExpanded === 'function') app.reloadFooterExpanded();

  _teardownVirtualScroll();
  if (app.chatMessages) {
    app.chatMessages.innerHTML = loadingSpinnerMarkup();
  }

  await loadSessionChat(sid);
  loopSessionChanged();
  loopVisualSessionChanged();
  genuiSessionChanged();
  chatActivitySessionChanged();
  _renderSessionRows();
  _setTriggerLabel();

  // Drain replayed events
  try {
    const pending = consumeReplayedEventsFor(sid);
    for (const ev of pending) {
      if (ev.type === 'db' && ev.role === 'assistant' && ev.id
          && (ev.op === 'insert_interaction' || ev.interaction_seq != null)) {
        if (!app._interactionAnchors) app._interactionAnchors = new Map();
        app._interactionAnchors.set(String(ev.id), {
          id: ev.id,
          interactionSeq: Number(ev.interaction_seq ?? ev.session_seq),
          turnId: ev.turn_id || null,
          createdAt: ev.created_at || ev.emit_time,
        });
        continue;
      }
      const key = ev.asst_id || ev.turn_id;
      const anchor = key && app._interactionAnchors
        ? app._interactionAnchors.get(String(key)) : null;
      const interactionSeq = Number(
        ev.interaction_seq ?? (anchor && anchor.interactionSeq),
      );
      const ownerTurnId = ev.turn_id || (anchor && anchor.turnId) || '';
      if (ev.type === 'stream' && typeof app.appendStreamToActiveBubble === 'function') {
        app.appendStreamToActiveBubble(ev.content || '', key, ev.created_at || ev.emit_time);
      } else if (ev.type === 'agent_step_end' && typeof app.finalizeAgentStep === 'function') {
        app.finalizeAgentStep(ev.content || '', key, ev.created_at || ev.emit_time, ownerTurnId, interactionSeq);
      } else if (ev.type === 'response' && typeof app.finalizeAgentResponse === 'function') {
        app.finalizeAgentResponse(ev.content || '', key, true, ev.created_at || ev.emit_time, ownerTurnId, interactionSeq);
      } else if (ev.type === 'interrupted' && typeof app.markAgentInterrupted === 'function') {
        app.markAgentInterrupted(ev.asst_id, ev.created_at || ev.emit_time, interactionSeq, ownerTurnId);
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
// Returns { ok: boolean, error?: string }. Retries up to `retries` times with
// exponential backoff so transient blips don't silently lose the button state.
// The caller (session-init.js) shows the error in the session title on failure.

async function deleteSession(sid, { retries = 1 } = {}) {
  interruptSession(sid);
  if (storageAdapter.isBrowser) {
    const result = await storageAdapter.deleteSession(sid);
    if (!result.ok) return result;
    notifySessionsChanged({ action: 'delete', ids: [sid] });
    await populateSessionSelect(app.currentUserId);
    const _stubIdx = _sessionsCache.findIndex(s => s.id === sid && s.created_at === null);
    if (_stubIdx !== -1) _sessionsCache.splice(_stubIdx, 1);
    if (sid === app.currentSessionId) {
      const others = _sessionsCache.filter(s => s.id !== sid);
      if (others.length > 0) {
        await switchToSession(others[0].id);
      } else {
        app.currentSessionId = randomUUID();
        localStorage.setItem('terminalSessionId', app.currentSessionId);
        // Fresh session — blank the header the same way a switch does.
        _clearSessionHeader();
        if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
        if (typeof app.reloadThinking === 'function') app.reloadThinking();
        if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
        if (typeof app.reloadFooterExpanded === 'function') app.reloadFooterExpanded();
        await loadSessionChat(app.currentSessionId);
      }
    }
    return { ok: true };
  }
  let lastErr = '';
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=user.db'), { method: 'DELETE', headers: { ...authHeaders() } });
      if (res.ok) {
        // Tell the admin Sessions table (and any other listener) to refresh so a
        // session deleted here also disappears there.
        notifySessionsChanged({ action: 'delete', ids: [sid] });
        // Re-fetch the session list and VERIFY the session is actually gone.
        // The API can return 200 even when the delete didn't take effect (e.g.
        // phantom session with no `sessions` row, or a hybrid sync that silently
        // fails). If the session is still in the cache after refresh, the delete
        // did NOT persist — return an error so the caller shows it in the UI.
        await populateSessionSelect(app.currentUserId);
        // poplateSessionSelect stubs the currentSessionId if it's missing from
        // the server list (line ~580 of session-list.js). When deleting the
        // current session that stub has id === sid, which would make the
        // verification below think the delete failed. Remove it so the
        // switching logic runs and the next session takes over.
        const _stubIdx = _sessionsCache.findIndex(s => s.id === sid && s.created_at === null);
        if (_stubIdx !== -1) _sessionsCache.splice(_stubIdx, 1);
        const stillPresent = _sessionsCache.some(s => s.id === sid);
        if (stillPresent) {
          lastErr = 'Session still exists after delete';
          // Fall through to retry logic below (or return error after exhausting).
          // This acts as an extra "attempt" using verification rather than a
          // raw HTTP call — avoids an infinite retry loop on persistent failures.
          if (attempt < retries) {
            await new Promise(r => setTimeout(r, 400 * (attempt + 1)));
            continue;
          }
          console.warn('Failed to delete session (session still present after refresh):', sid);
          return { ok: false, error: lastErr };
        }
        if (sid === app.currentSessionId) {
          const others = _sessionsCache.filter(s => s.id !== sid);
          if (others.length > 0) {
            await switchToSession(others[0].id);
          } else {
            app.currentSessionId = randomUUID();
            localStorage.setItem('terminalSessionId', app.currentSessionId);
            // Fresh session → reset the execution-mode pill (per-session key, defaults
            // to Ask) so it doesn't inherit the deleted session's leftover mode.
            if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
            if (typeof app.reloadThinking === 'function') app.reloadThinking();
            if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
            if (typeof app.reloadFooterExpanded === 'function') app.reloadFooterExpanded();
            await loadSessionChat(app.currentSessionId);
          }
        }
        return { ok: true };
      }
      lastErr = `Server error (${res.status})`;
      const body = await res.text().catch(() => '');
      if (body) lastErr = body.substring(0, 120);
    } catch (e) {
      lastErr = e.message || 'Network error';
    }
    if (attempt < retries) await new Promise(r => setTimeout(r, 400 * (attempt + 1)));
  }
  console.warn('Failed to delete session after retries:', lastErr);
  return { ok: false, error: lastErr };
}

// ── Patch session ──────────────────────────────────────────────────────────

async function patchSession(sid, body) {
  if (storageAdapter.isBrowser) {
    await storageAdapter.patchSession(sid, body);
    notifySessionsChanged({ action: 'patch', ids: [sid] });
    return true;
  }
  try {
    const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=user.db'), {
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

// ── Auto rename (Session Namer app function) ────────────────────────────────
// Calls POST /sessions/{id}/auto-title so the background Session Namer re-titles
// the session on demand — forced past any lock or special prefix (optimizer-/
// closer-/slash-), sampling more of the conversation than the 3-turn background
// hook. The server returns the new name and emits session_title WS events so the
// header spinner + live rename work exactly like a real turn. In browser-storage
// mode there is no server: fall back to a client-side title from the first user
// message (same rule as the server's fallback naming).

async function autoRenameSession(sid) {
  if (!sid) return { ok: false, error: 'No session' };
  console.log('[auto-rename] autoRenameSession called for', sid);
  if (storageAdapter.isBrowser) {
    let title = '';
    try {
      const msgs = await storageAdapter.getInteractions(sid, 6, {});
      const firstUser = (msgs || []).find(
        m => m && m.role === 'user' && typeof m.content === 'string' && m.content.trim()
      );
      if (firstUser && firstUser.content) {
        title = firstUser.content.trim().split(/\s+/).slice(0, 6)
          .join(' ').replace(/[.,!?;:]+$/, '').slice(0, 60);
      }
    } catch (_) { /* fall through */ }
    if (!title) return { ok: false, error: 'No messages to name from' };
    const ok = await patchSession(sid, { title });
    if (ok) {
      const sess = _sessionsCache.find(s => s.id === sid);
      if (sess) sess.title = title;
      await populateSessionSelect(app.currentUserId);
    }
    return { ok: !!ok, title };
  }
  try {
    const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '/auto-title?db=user.db'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
    });
    if (!res.ok) {
      let detail = 'Auto rename failed';
      try { const j = await res.json(); if (j && j.detail) detail = String(j.detail); } catch (_) {}
      console.warn('[auto-rename] server returned', res.status, detail);
      return { ok: false, error: detail };
    }
    const data = await res.json();
    console.log('[auto-rename] server response:', data);
    if (data && data.status === 'ok' && data.title) {
      const sess = _sessionsCache.find(s => s.id === sid);
      if (sess) sess.title = data.title;
      notifySessionsChanged({ action: 'patch', ids: [sid] });
      await populateSessionSelect(app.currentUserId);
    }
    return { ok: !!(data && data.status === 'ok'), title: data && data.title, status: data && data.status, message: data && data.message };
  } catch (e) {
    console.warn('Auto rename failed:', e);
    return { ok: false, error: e.message || 'Network error' };
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
  autoRenameSession,
};
