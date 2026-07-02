'use strict';

// Message-send pipeline — outbox retry queue (survives refresh), draft persistence,
// chat gate (sign-in check), stop/interrupt, pill textarea auto-resize + scroll dot.
// Sets app.abortChatStream / app.refreshChat. Module map: ui/chat-side-panel/js/README.md.

import { _refreshLucideIcons } from '../../shared/js/dom-utils.js';
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { addAttachmentsToMessage, getPendingAttachments, renderAttachmentElement } from '../../shared/js/attachments.js';
import { getAccessMode, fetchAccessMode, authHeaders } from '../../shared/js/left-login.js';
import { addChatBubble } from './chat-bubble.js';
import { _addBubbleActions, _getBubbleText } from './chat-bubble-actions.js';
import { startReconcileLoop } from './chat-reconcile.js';
import { agentChatMsg } from '../../shared/js/app-prompts.js';

// ── Chat gate ──────────────────────────────────────────────────────────────

function _canChat() {
  // A token is present for every visitor allowed to chat: signed-in members,
  // per-agent anonymous visitors (public agent URLs), and the auto-admin in
  // 'open' mode (localhost). No token ⇒ this app requires sign-in here.
  return !!localStorage.getItem('auth_token');
}

// Both placeholders resolve per-agent: the current agent's Config-tab override
// (metadata.chat_ui) → the app-wide default in app/defaults/app-prompts.json
// (ui_messages.chat.*) → a built-in fallback. agentChatMsg() walks that chain.
function applyChatGate() {
  if (!app.chatInput || !app.chatSend) return;
  const allowed = _canChat();
  if (allowed) {
    if (app._composerLocked) {
      // A blocking built-in command (e.g. /compact) is running synchronously.
      // Keep the pill + Send disabled so no message can be queued mid-compact;
      // the Stop button stays live (it is not touched here) as the escape hatch.
      // Placeholder is left as set by the lock so the "Compacting…" hint persists.
      app.chatInput.disabled = true;
      app.chatSend.disabled = true;
      _updateInputRowState();
      return;
    }
    app.chatInput.disabled = false;
    app.chatInput.placeholder = agentChatMsg('pill_placeholder');
    app.chatSend.disabled = !app.chatInput.value.trim();
  } else {
    app.chatInput.disabled = true;
    app.chatInput.value = '';
    app.chatInput.placeholder = agentChatMsg('pill_locked_placeholder');
    app.chatSend.disabled = true;
  }
  _updateInputRowState();
}
// Exposed so the session modules can re-apply the gate (and thus refresh the
// per-agent placeholder) when the user switches agents or starts a new session.
app.applyChatGate = applyChatGate;

// Built-in commands that run SYNCHRONOUSLY on the server (the /chat/send POST
// blocks until they finish) and so should lock the composer until they reply.
// Mirror of app/api/chat.py _is_compact_command (`/compact` + optional args,
// rejecting `/compactfoo`). The Stop button stays live as the escape hatch.
function _isBlockingCommand(text) {
  return /^\/compact(?:\s+.*)?$/i.test((text || '').trim());
}

// Lock the pill while a blocking built-in runs; Stop stays clickable.
function _lockComposerForCommand(hint) {
  app._composerLocked = true;
  if (app.chatInput) app.chatInput.placeholder = hint || 'Working… click Stop to cancel';
  applyChatGate();
}
function _unlockComposer() {
  if (!app._composerLocked) return;
  app._composerLocked = false;
  applyChatGate();
}
app._unlockComposer = _unlockComposer;

// The "new session" / "welcome" / "switched agent" bubbles are empty-state
// PLACEHOLDERS (marked `.session-placeholder` where they're rendered in
// session-init.js / session-load.js). They stand in for a conversation that
// hasn't started yet — so the instant the user sends their first message they're
// stale and must be cleared, leaving the session to read as a clean start
// (their message, then the reply). A later full DB re-render never re-adds them
// once the session has real turns, so this only matters on the optimistic path.
function _removeSessionPlaceholders() {
  if (!app.chatMessages) return;
  app.chatMessages
    .querySelectorAll(':scope > .chat-bubble.session-placeholder')
    .forEach(el => el.remove());
}

// CHAT-PILL-SYNC: same pattern as agent-builder-bar and genui
function _updateInputRowState() {
  if (!app.chatInput) return;
  const row = document.getElementById('chat-input-row');
  if (!row) return;
  const hasText = !!app.chatInput.value.trim();
  row.classList.toggle('has-text', hasText);
}

// ── session_seq persistence ──
const _LAST_SEQ_LS_KEY = 'webagent.lastSessionSeq.v1';
function _persistLastSessionSeq() {
  try {
    localStorage.setItem(_LAST_SEQ_LS_KEY, JSON.stringify(app.lastSessionSeq || {}));
  } catch (_) { /* quota / private mode — non-fatal */ }
}
function _loadLastSessionSeq() {
  try {
    const raw = localStorage.getItem(_LAST_SEQ_LS_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') {
      app.lastSessionSeq = parsed;
    }
  } catch (_) { /* corrupt — drop silently */ }
}
_loadLastSessionSeq();

// ── Outgoing message queue (outbox) ─────────────────────────────────
const _OUTBOX_LS_KEY = 'webagent.pendingMessages.v1';
let _outboxIdCounter = 0;

// A queued message that can never be delivered must not live forever: any entry
// older than this is dropped on the next read, so a permanently-stuck "pending"
// bubble self-evicts instead of haunting every future session. Long enough to
// survive a server restart / brief offline spell, short enough to not linger.
// (OUTBOX-MAX-AGE)
const _OUTBOX_MAX_AGE_MS = 6 * 60 * 60 * 1000; // 6 hours

function _readOutbox() {
  let q;
  try {
    const raw = localStorage.getItem(_OUTBOX_LS_KEY);
    q = raw ? JSON.parse(raw) : [];
  } catch (_) { return []; }
  if (!Array.isArray(q) || q.length === 0) return Array.isArray(q) ? q : [];
  // Self-heal: prune provably-stale entries and persist the result so a dead
  // message clears out of localStorage on its own. Undated entries are kept.
  const cutoff = Date.now() - _OUTBOX_MAX_AGE_MS;
  const fresh = q.filter(e => {
    const t = e && e.timestamp ? Date.parse(e.timestamp) : NaN;
    return isNaN(t) || t >= cutoff;
  });
  if (fresh.length !== q.length) {
    try {
      if (fresh.length === 0) localStorage.removeItem(_OUTBOX_LS_KEY);
      else localStorage.setItem(_OUTBOX_LS_KEY, JSON.stringify(fresh));
    } catch (_) { /* quota / private mode — non-fatal */ }
  }
  return fresh;
}

function _writeOutbox(queue) {
  try {
    if (!queue || queue.length === 0) {
      localStorage.removeItem(_OUTBOX_LS_KEY);
    } else {
      localStorage.setItem(_OUTBOX_LS_KEY, JSON.stringify(queue));
    }
  } catch (_) { /* quota / private mode — non-fatal */ }
}

function _addToOutbox(entry) {
  const q = _readOutbox();
  q.push(entry);
  _writeOutbox(q);
  _startOutboxPoll();
}

function _removeFromOutbox(id) {
  const q = _readOutbox().filter(e => e.id !== id);
  _writeOutbox(q);
  if (q.length === 0) _stopOutboxPoll();
}

function _outboxHasPending() {
  const q = _readOutbox();
  return q.length > 0;
}

async function _retryEntry(entry) {
  try {
    const allUserBodies = document.querySelectorAll('.chat-bubble.user:not(.pending) .bubble-body');
    let alreadyDelivered = false;
    for (const b of allUserBodies) {
      if (b.textContent.trim() === entry.text) {
        alreadyDelivered = true;
        break;
      }
    }
    if (alreadyDelivered) {
      _removeFromOutbox(entry.id);
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => el.remove());
      return true;
    }

    const payload = {
      message: entry.text,
      session_id: entry.session_id || app.currentSessionId,
      user_id: entry.user_id || app.currentUserId,
      execution_mode: app.executionMode || 'ask',
      // Idempotency key: the outbox entry id rides along on every retry so the
      // server can recognise a re-send of an already-accepted message and skip
      // the duplicate insert / second run (see app/api/chat.py _find_interaction_by_cmid).
      client_msg_id: entry.id,
    };
    if (entry.agent_id || app.currentAgentId) {
      payload.agent_id = entry.agent_id || app.currentAgentId;
    }
    if (entry.app_control) payload.app_control = entry.app_control;
    if (entry.target_device) payload.target_device = entry.target_device;
    const resp = await fetch(apiPath('/api/v1/chat/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
    });
    if (resp.ok) {
      _removeFromOutbox(entry.id);
      const data = await resp.json().catch(() => ({}));
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => {
          el.className = 'chat-bubble user';
          el.removeAttribute('data-pending-id');
          const label = el.querySelector('.label');
          if (label) label.textContent = 'You';
          el.querySelectorAll('.bubble-actions').forEach(a => a.remove());
          if (data && data.turn_id) {
            el.setAttribute('data-msg-id', data.turn_id);
            _addBubbleActions(el);
          }
        });
      if (typeof app.populateSessionSelect === 'function') {
        app.populateSessionSelect(app.currentUserId);
      }
      return true;
    }
    // A client-error status (bad request / forbidden / gone / not found) won't
    // change on retry — this exact message can never be delivered, so drop it
    // rather than wedge the queue forever. Transient failures (5xx, 408/429,
    // network throw below) fall through and keep retrying. (OUTBOX-GIVE-UP)
    if ([400, 401, 403, 404, 409, 410, 422].includes(resp.status)) {
      _removeFromOutbox(entry.id);
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => el.remove());
      return true; // resolved by dropping — stop hammering a dead request
    }
  } catch (_) { /* server still down */ }
  return false;
}

async function _flushOutbox() {
  const queue = _readOutbox();
  if (queue.length === 0) return 0;
  if (!app.currentUserId) return 0;
  let ok = 0;
  for (const entry of queue) {
    if (await _retryEntry(entry)) ok++;
  }
  return ok;
}

function _renderPendingBubble(entry) {
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble user pending';
  bubble.setAttribute('data-pending-id', entry.id);
  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = 'You (pending)';
  bubble.appendChild(label);
  const body = document.createElement('div');
  body.className = 'bubble-body';
  body.textContent = entry.text;
  bubble.appendChild(body);
  _appendPendingActions(bubble, entry);
  _refreshLucideIcons(bubble);
  if (app.chatMessages) {
    app.chatMessages.appendChild(bubble);
    // scroll handled by caller or chat-ui
  }
  return bubble;
}

function _convertBubbleToPending(bubble, entry) {
  let label = bubble.querySelector('.label');
  if (!label) {
    label = document.createElement('span');
    label.className = 'label';
    bubble.insertBefore(label, bubble.firstChild);
  }
  label.textContent = 'You (pending)';
  bubble.className = 'chat-bubble user pending';
  bubble.setAttribute('data-pending-id', entry.id);
  bubble.querySelectorAll('.bubble-actions').forEach(el => el.remove());
  bubble.removeAttribute('data-msg-id');
  bubble.removeAttribute('data-turn-id');
  _appendPendingActions(bubble, entry);
  _refreshLucideIcons(bubble);
}

function _appendPendingActions(bubble, entry) {
  // Standard footer (collapse / speaker / copy) — same as every other bubble.
  _addBubbleActions(bubble);
  let actions = bubble.querySelector(':scope > .bubble-actions');
  if (!actions) {
    actions = document.createElement('div');
    bubble.appendChild(actions);
  }
  actions.classList.add('bubble-actions', 'pending-actions');
  const retryBtn = document.createElement('button');
  retryBtn.type = 'button';
  retryBtn.className = 'bubble-action-btn pending-retry';
  retryBtn.innerHTML = '<i data-lucide="refresh-cw" style="width:14px;height:14px;"></i>';
  retryBtn.title = 'Retry sending';
  retryBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    retryBtn.disabled = true;
    retryBtn.innerHTML = '<span style="font-size:12px;">\u21BB</span>';
    const ok = await _retryEntry(entry);
    if (!ok) {
      retryBtn.disabled = false;
      retryBtn.innerHTML = '<i data-lucide="refresh-cw" style="width:14px;height:14px;"></i>';
      if (window.lucide && typeof window.lucide.createIcons === 'function') {
        try { window.lucide.createIcons({ nodes: [retryBtn.querySelector('[data-lucide]')] }); } catch (_) {}
      }
    }
  });
  actions.appendChild(retryBtn);
  const dismissBtn = document.createElement('button');
  dismissBtn.type = 'button';
  dismissBtn.className = 'bubble-action-btn pending-dismiss';
  dismissBtn.innerHTML = '<i data-lucide="x" style="width:14px;height:14px;"></i>';
  dismissBtn.title = 'Dismiss (remove from queue)';
  dismissBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _removeFromOutbox(entry.id);
    bubble.remove();
  });
  actions.appendChild(dismissBtn);
}

let _outboxPollTimer = null;
function _startOutboxPoll() {
  if (_outboxPollTimer) return;
  _outboxPollTimer = setInterval(async () => {
    const n = await _flushOutbox();
    if (n > 0) {
      const remaining = _readOutbox();
      if (remaining.length === 0) {
        document.querySelectorAll('.chat-bubble.user.pending').forEach(el => el.remove());
      } else {
        _renderPendingBubbles();
      }
    }
  }, 5000);
}
function _stopOutboxPoll() {
  if (_outboxPollTimer) {
    clearInterval(_outboxPollTimer);
    _outboxPollTimer = null;
  }
}

function _renderPendingBubbles() {
  document.querySelectorAll('.chat-bubble.user.pending').forEach(el => el.remove());
  const queue = _readOutbox();
  for (const entry of queue) {
    _renderPendingBubble(entry);
  }
}

// ── chat draft persistence ──
const _DRAFT_LS_KEY = 'webagent.chatDraft.v1';
function _saveDraft() {
  _debouncedSaveDraft();
}
function _clearDraft() {
  if (_draftTimer) { clearTimeout(_draftTimer); _draftTimer = null; }
  try { localStorage.removeItem(_DRAFT_LS_KEY); } catch (_) { /* non-fatal */ }
}
function _restoreDraft() {
  try {
    const v = localStorage.getItem(_DRAFT_LS_KEY);
    if (!v || !app.chatInput) return;
    if (app.chatInput.value) return;
    if (!_canChat()) return;
    app.chatInput.value = v;
    _updateInputRowState();
    if (app.chatSend) app.chatSend.disabled = !v.trim();
    if (app.autoResizeChatInput) app.autoResizeChatInput();
  } catch (_) { /* non-fatal */ }
}

let _draftTimer = null;
function _debouncedSaveDraft() {
  if (_draftTimer) clearTimeout(_draftTimer);
  _draftTimer = setTimeout(() => {
    _draftTimer = null;
    try {
      const v = app.chatInput ? app.chatInput.value : '';
      if (v) localStorage.setItem(_DRAFT_LS_KEY, v);
      else localStorage.removeItem(_DRAFT_LS_KEY);
    } catch (_) { /* quota / private mode — non-fatal */ }
  }, 150);
}

// ── Send / Stop / Abort ────────────────────────────────────────────────────

async function sendStopMessage() {
  addChatBubble('user', 'Stop');

  // Stop is the escape hatch from a composer-locking command (e.g. /compact):
  // hand control back to the user immediately, even if the server is still busy.
  _unlockComposer();

  try {
    await fetch(apiPath('/api/v1/chat/interrupt'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ session_id: app.currentSessionId }),
    });
  } catch (e) {
    addChatBubble('agent', 'Cannot stop: ' + e.message, 'error');
  }
}

async function sendMessage() {
  if (!_canChat()) { applyChatGate(); return; }
  const text = app.chatInput.value.trim();
  if (!text) return;

  // Terminal tunnel
  if (app.tunnel && app.tunnel.active) {
    app.chatInput.value = '';
    app.chatSend.disabled = true;
    _updateInputRowState();
    _autoResizePill(app.chatInput);
    _clearDraft();
    if (typeof app.sendTunnelLine === 'function') {
      try { await app.sendTunnelLine(text); } catch (_) { /* terminal reflects state */ }
    }
    return;
  }

  // Terminal Chat — pill input goes straight to the PTY, no agent.
  if (app.terminalChat && app.terminalChat.active) {
    app.chatInput.value = '';
    app.chatSend.disabled = true;
    _updateInputRowState();
    _autoResizePill(app.chatInput);
    _clearDraft();
    try {
      // A PTY's Enter key is carriage return (\r), NOT line feed (\n) — the same
      // byte the xterm keyboard and the agent's terminal_send both use. Sending
      // \n makes PowerShell's PSReadLine treat it as a line continuation (the
      // ">>" prompt) instead of executing, so the command never runs. Normalise
      // any in-textarea newlines (multi-line paste) to \r too, and end with \r.
      const ptyInput = text.replace(/\r?\n/g, '\r') + '\r';
      await fetch(apiPath('/api/v1/terminal/sessions/' + encodeURIComponent(app.terminalChat.tsid) + '/write'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ input: ptyInput }),
      });
    } catch (_) { /* terminal reflects state */ }
    return;
  }

  if (!app.currentAgentId) {
    if (!app.currentUserId) { applyChatGate(); return; }
    try {
      if (typeof app.startWebagentSession === 'function') {
        await app.startWebagentSession();
      }
    } catch (e) {
      addChatBubble('agent', '\u274C Could not start WebAgent: ' + (e.message || e), 'error');
      return;
    }
    if (!app.currentAgentId) return;
  }

  // App Control point-and-share stages a fingerprint of the clicked element on
  // app.pendingAppControl; it rides along with THIS send only (consumed + cleared
  // now), so the backend can render it as a foldable app_control tool chip and fold
  // it to the agent without bloating the visible message. See app-control-point.js.
  const _appControl = app.pendingAppControl || null;
  app.pendingAppControl = null;

  const outboxEntry = {
    id: 'msg_' + Date.now() + '_' + (++_outboxIdCounter),
    text: text,
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
    agent_id: app.currentAgentId,
    timestamp: new Date().toISOString(),
  };
  if (_appControl) outboxEntry.app_control = _appControl;
  // Target device for this turn (the chat pill). '' = run locally; an instance
  // id routes the turn to that device's worker (see chat-ui.js / app/devices/).
  if (app.targetDevice) outboxEntry.target_device = app.targetDevice;
  _addToOutbox(outboxEntry);

  app.chatInput.value = '';
  app.chatSend.disabled = true;
  _updateInputRowState();
  _autoResizePill(app.chatInput);
  _clearDraft();
  if (typeof app.clearSuggestions === 'function') {
    try { app.clearSuggestions(); } catch (_) { /* best-effort */ }
  }

  window.__chatPollLastAt = new Date().toISOString();

  // First real message — clear the empty-state placeholder so it doesn't linger
  // above the conversation.
  _removeSessionPlaceholders();
  const _userBubble = addChatBubble('user', text);
  // ── Render pending attachments into the user bubble ──
  const _pendingAtts = getPendingAttachments();
  if (_pendingAtts.length > 0 && _userBubble) {
    for (const att of _pendingAtts) {
      const el = renderAttachmentElement(att);
      if (el) _userBubble.appendChild(el);
    }
  }
  app.isProcessing = true;
  // Synchronous built-ins (e.g. /compact) block the send POST until done — lock
  // the pill so nothing can be queued mid-run; Stop remains live to bail out.
  if (_isBlockingCommand(text)) {
    _lockComposerForCommand('Compacting context… click Stop to cancel');
  }
  // Prime WS-liveness from "now" so the DB-reconcile poll counts its silence
  // window from send. If the WS delivers (same-process) it keeps this fresh and
  // the poll stays dormant; if it can't (browser + agent on different workers)
  // the poll engages after WS_SILENCE_MS and streams the reply from the DB.
  if (app.currentSessionId) {
    if (!app._lastWsEventAt) app._lastWsEventAt = {};
    app._lastWsEventAt[app.currentSessionId] = Date.now();
  }
  startReconcileLoop();
  // Last-resort backstop: if a turn is somehow still "processing" after 60s
  // (neither the WS nor the reconcile poll cleared it), force a full DB resync.
  // The ~0.8s reconcile loop is the primary recovery path; this is the floor.
  if (app._healingTimer) clearTimeout(app._healingTimer);
  app._healingTimer = setTimeout(() => {
    if (app.isProcessing) {
      console.warn('Self-healing: turn still processing after 60s, forcing DB sync.');
      if (typeof app.refreshChat === 'function') app.refreshChat();
    }
  }, 60000);
  if (app.chatActivityStart) app.chatActivityStart('Sending\u2026');

  const base = {
    message: text,
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
    execution_mode: app.executionMode || 'ask',
    // Idempotency key (same id stored on the outbox entry above): if this send
    // isn't confirmed and the outbox retries it, the server dedupes on this id
    // instead of inserting the message twice. (see app/api/chat.py)
    client_msg_id: outboxEntry.id,
  };
  if (app.currentAgentId) base.agent_id = app.currentAgentId;
  if (_appControl) base.app_control = _appControl;
  const payload = addAttachmentsToMessage(base);
  if (app.clearPendingAttachments) app.clearPendingAttachments();

  try {
    const resp = await fetch(apiPath('/api/v1/chat/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      if (_userBubble) _convertBubbleToPending(_userBubble, outboxEntry);
      app.isProcessing = false;
      _unlockComposer();
      if (app._healingTimer) { clearTimeout(app._healingTimer); app._healingTimer = null; }
      app.chatSend.disabled = false;
      if (app.chatActivityStop) app.chatActivityStop();
      return;
    }

    const data = await resp.json().catch(() => ({}));

    if (data.status === 'ok' && data.reply) {
      _removeFromOutbox(outboxEntry.id);
      // Buffered/synchronous reply path: addChatBubble doesn't build a footer,
      // so add it here (live-streamed replies get theirs via finalizeAgentResponse).
      const _replyBubble = addChatBubble('agent', data.reply);
      if (_replyBubble) _addBubbleActions(_replyBubble);
      app.isProcessing = false;
      _unlockComposer();
      app.chatSend.disabled = false;
      if (app.chatActivityStop) app.chatActivityStop();
      if (typeof app.populateSessionSelect === 'function') {
        app.populateSessionSelect(app.currentUserId);
      }
      return;
    }

    _removeFromOutbox(outboxEntry.id);

    if (data.turn_id && _userBubble) {
      _userBubble.setAttribute('data-msg-id', data.turn_id);
      _addBubbleActions(_userBubble);
    }
    if (typeof app.populateSessionSelect === 'function') {
      app.populateSessionSelect(app.currentUserId);
    }
  } catch (e) {
    console.warn('[chat/send] failed', e);
    if (_userBubble) _convertBubbleToPending(_userBubble, outboxEntry);
    app.isProcessing = false;
    _unlockComposer();
    app.chatSend.disabled = false;
    if (app.chatActivityStop) app.chatActivityStop();
  }
}

function abortChatStream() {
  app.agentBuffer = '';
  app.isProcessing = false;
  _unlockComposer();
  if (app.chatSend) app.chatSend.disabled = false;
}

// Exposed on `app` so session modules can call it without importing this module
app.abortChatStream = abortChatStream;

app.refreshChat = async () => {
  if (!app.currentSessionId) return;
  console.log('Self-healing: Fetching latest messages from DB...');
  try {
    if (typeof app.loadSessionChat === 'function') {
      await app.loadSessionChat(app.currentSessionId);
    }
    app.isProcessing = false;
    _unlockComposer();
    if (app.chatActivityStop) app.chatActivityStop();
  } catch (e) {
    console.error('Self-healing: DB sync failed', e);
  }
};

// Needed by chat-ui.js for auto-resize
function _autoResizePill(el) {
  if (!el || el.tagName !== 'TEXTAREA') return;
  const cs = getComputedStyle(el);
  const minH = parseFloat(cs.minHeight) || 0;
  const maxH = parseFloat(cs.maxHeight) || 124;
  // Empty field → always rest at one line. A textarea's scrollHeight INCLUDES its
  // wrapped placeholder, so measuring it here would grow an empty pill to fit a
  // long placeholder — and on a narrow screen (or the page-assistant's long
  // typewriter hints) that wraps to 2–3 lines, making the single-line pill "stack"
  // taller on window resize. Pin to min-height and skip the measure when there's
  // no typed value; only real content grows the pill.
  if (!el.value) {
    el.style.height = minH ? minH + 'px' : 'auto';
    el.style.overflowY = 'hidden';
    _updateScrollIndicator(el);
    return;
  }
  el.style.height = 'auto';
  const next = Math.max(minH, Math.min(el.scrollHeight, maxH));
  el.style.height = next + 'px';
  el.style.overflowY = el.scrollHeight > maxH ? 'auto' : 'hidden';
  _updateScrollIndicator(el);
}

function _updateScrollIndicator(el) {
  if (!el) return;
  const pill = el.closest('.chat-pill');
  if (!pill) return;
  const overflow = el.scrollHeight - el.clientHeight;
  if (overflow <= 1) {
    pill.style.setProperty('--indicator-opacity', '0');
    pill.style.setProperty('--scroll-pct', '0');
    return;
  }
  const pct = Math.max(0, Math.min(1, el.scrollTop / overflow));
  pill.style.setProperty('--scroll-pct', pct.toFixed(4));
  pill.style.setProperty('--indicator-opacity', '1');
}

// ── Prewarm ──────────────────────────────────────────────────────────────────
// While the user is typing (or the moment they focus the pill), ask the server
// to build the read-only prep for the NEXT send — the agent's tool set, the chat
// history, the attached data sources. On a remote DB those reads cost seconds;
// doing them during the typing window means the send can skip them. Best-effort:
// throttled per session, fire-and-forget, and a failure just means the turn
// builds the prep live as before. Backend: POST /api/v1/chat/prewarm.
const _PREWARM_THROTTLE_MS = 15000;
let _lastPrewarmAt = Object.create(null);

function _prewarm() {
  try {
    if (!_canChat()) return;
    if (!app.currentUserId || !app.currentSessionId || !app.currentAgentId) return;
    // Tunnels / Terminal Chat don't run the normal agent turn — nothing to warm.
    if (app.tunnel && app.tunnel.active) return;
    if (app.terminalChat && app.terminalChat.active) return;
    const sid = app.currentSessionId;
    const now = Date.now();
    if (_lastPrewarmAt[sid] && (now - _lastPrewarmAt[sid]) < _PREWARM_THROTTLE_MS) return;
    _lastPrewarmAt[sid] = now;
    fetch(apiPath('/api/v1/chat/prewarm'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        user_id: app.currentUserId,
        session_id: sid,
        agent_id: app.currentAgentId,
      }),
      keepalive: true,
    }).catch(() => { /* best-effort: a miss just builds the prep live on send */ });
  } catch (_) { /* never let prewarm interfere with typing */ }
}

export {
  sendMessage,
  sendStopMessage,
  _prewarm,
  abortChatStream,
  applyChatGate,
  _canChat,
  _autoResizePill,
  _updateScrollIndicator,
  _updateInputRowState,
  _saveDraft,
  _clearDraft,
  _restoreDraft,
  _renderPendingBubbles,
  _startOutboxPoll,
  _outboxHasPending,
};