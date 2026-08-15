'use strict';

// Message-send pipeline — outbox retry queue (survives refresh), draft persistence,
// chat gate (sign-in check), stop/interrupt, pill textarea auto-resize + scroll dot.
// Sets app.abortChatStream / app.refreshChat. Module map: ui/chat/js/README.md.

import { _refreshLucideIcons } from '../../shared/js/dom-utils.js';
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { addAttachmentsToMessage, getPendingAttachments, renderAttachmentElement } from '../../shared/js/attachments.js';
import { getAccessMode, fetchAccessMode, authHeaders } from '../../shared/js/left-login.js';
import { addChatBubble } from './chat-bubble.js';
import { _addBubbleActions, _getBubbleText } from './chat-bubble-actions.js';
import { startReconcileLoop } from './chat-reconcile.js';
import { _transcriptChangedRemotely } from './chat-message-cache.js';
import { agentChatMsg } from '../../shared/js/app-prompts.js';
import { storageAdapter } from './storage/storage-adapter.js';
import browserRouter from './storage/browser-router.js';

// ── Chat gate ──────────────────────────────────────────────────────────────

function _canChat() {
  // A token is present for every visitor allowed to chat: signed-in members,
  // per-agent anonymous visitors (public agent URLs), and the auto-admin in
  // 'open' mode (localhost). No token ⇒ this app requires sign-in here.
  return !!localStorage.getItem('auth_token');
}

// Both placeholders resolve per-agent: the current agent's Config-tab override
// (metadata.chat_ui) → the app-wide default in app/defaults/app-prompts.json
// (chat_ui.chat_common.messages) → a built-in fallback. agentChatMsg() walks that chain.
function applyChatGate() {
  _syncBillingPanel();
  if (!app.chatInput || !app.chatSend) return;
  const billingBlock = app._billingBlocked;
  const billingBlockedHere = billingBlock
    && (!billingBlock.agentId || billingBlock.agentId === app.currentAgentId);
  if (billingBlockedHere) {
    app.chatInput.disabled = true;
    app.chatInput.value = '';
    app.chatInput.placeholder = 'Your trial has ended — pick an option above.';
    app.chatSend.disabled = true;
    _updateInputRowState();
    return;
  }
  const allowed = _canChat();
  if (allowed) {
    if (app._composerLocked) {
      // A blocking built-in command (e.g. /compact) is running synchronously.
      // Keep the pill USABLE so the user can type and queue a message; it will
      // be sent automatically when the command finishes. The Stop button stays
      // live as the escape hatch. Placeholder hints that compaction is underway.
      app.chatInput.disabled = false;
      app.chatInput.placeholder = app.chatInput.placeholder || 'Compacting context… type to queue';
      app.chatSend.disabled = !app.chatInput.value.trim();
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

window.addEventListener('webagent-billing-blocked', (event) => {
  const detail = event?.detail || {};
  if (detail.reason !== 'trial_expired') return;
  app._billingBlocked = {
    agentId: detail.agentId || app.currentAgentId || null,
    reason: detail.reason,
    accepted: detail.accepted || null,
  };
  applyChatGate();
});

// ── Trial-ended billing panel ─────────────────────────────────────────────
// When a user's trial is exhausted the composer is disabled and this panel
// appears above the pill: a "Buy credits" button (opens the buy-credits modal,
// which offers the agent's accepted processors — Bitcoin in this deployment)
// and a "Continue using for free" button that expands info about bringing your
// own LLM. Hidden whenever the current agent is not trial-blocked.
function _syncBillingPanel() {
  const panel = document.getElementById('chat-billing-panel');
  if (!panel) return;
  const billingBlock = app._billingBlocked;
  const blockedHere = billingBlock
    && billingBlock.reason === 'trial_expired'
    && (!billingBlock.agentId || billingBlock.agentId === app.currentAgentId);
  if (!blockedHere) {
    panel.hidden = true;
    panel.innerHTML = '';
    return;
  }
  const agentKey = billingBlock.agentId || '';
  if (panel.dataset.renderedFor === agentKey && panel.innerHTML) return;
  panel.dataset.renderedFor = agentKey;
  panel.innerHTML = '';
  panel.hidden = false;

  const card = _bEl('div', { class: 'billing-panel-card' });
  card.appendChild(_bEl('div', { class: 'billing-panel-title' }, 'Your trial has ended'));
  card.appendChild(_bEl('div', { class: 'billing-panel-desc' },
    'This agent’s free trial is over. Pick how you’d like to keep chatting.'));

  card.appendChild(_bEl('button', {
    type: 'button',
    class: 'billing-panel-btn billing-panel-btn-primary',
    onclick: () => window.AppBilling.openBuyCreditsModal({
      agentId: billingBlock.agentId,
      reason: 'trial_expired',
      accepted: billingBlock.accepted,
    }),
  }, 'Buy credits'));

  card.appendChild(_bEl('button', {
    type: 'button',
    class: 'billing-panel-btn',
    onclick: () => { info.hidden = !info.hidden; },
  }, 'Continue using for free'));

  const info = _bEl('div', { class: 'billing-panel-info', hidden: true });
  info.appendChild(_bEl('div', { class: 'billing-panel-info-title' }, 'Use your own LLM — free'));
  info.appendChild(_bEl('div', { class: 'billing-panel-info-text' },
    'You can keep chatting with this agent for free by bringing your own LLM. ' +
    'Connect your own model provider (OpenAI, Anthropic, or any OpenAI-compatible API) ' +
    'with your own key, and your messages run on your key instead of this agent’s credits. ' +
    'Use the model picker in the chat footer to switch to your own model.'));
  info.appendChild(_bEl('div', { class: 'billing-panel-info-text', style: { marginTop: '8px' } },
    'Once your key is connected — or after buying credits — press “Check again” below.'));
  card.appendChild(info);

  // Re-evaluate access without sending a message. The composer is disabled while
  // blocked, so this is the recovery path after the user buys credits (balance
  // > 0) or connects their own LLM key (own-llm allow). On success the block is
  // cleared and the gate re-applied, re-enabling the composer.
  const checkBtn = _bEl('button', {
    type: 'button',
    class: 'billing-panel-btn billing-panel-btn-ghost',
  }, 'Check again');
  checkBtn.addEventListener('click', async () => {
    checkBtn.disabled = true;
    checkBtn.textContent = 'Checking…';
    try {
      const decision = await window.AppBilling.checkAccess(billingBlock.agentId);
      if (decision && decision.allow) {
        app._billingBlocked = null;
      } else {
        // Still blocked: re-assert the current block so the panel stays put.
        app._billingBlocked = billingBlock;
      }
    } catch (_) {
      app._billingBlocked = billingBlock; // couldn't check — keep blocking
    } finally {
      checkBtn.disabled = false;
      checkBtn.textContent = 'Check again';
      applyChatGate();
    }
  });
  card.appendChild(checkBtn);

  panel.appendChild(card);
}

// Minimal DOM builder for the billing panel (kept local — chat-send.js does
// not import the shared _el helper).
function _bEl(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'hidden') { if (v) e.hidden = true; }
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (v === false || v == null) continue;
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return e;
}

// Built-in commands that run SYNCHRONOUSLY on the server (the /chat/send POST
// blocks until they finish) and so should lock the composer until they reply.
// Mirror of app/api/chat.py _is_compact_command (`/compact` + optional args,
// rejecting `/compactfoo`). Also covers /optimize and other slash commands.
// The Stop button stays live as the escape hatch.
function _isBlockingCommand(text) {
  return /^\/compact(?:\s+.*)?$/i.test((text || '').trim());
}

// Slash commands whose result is persisted as a system interaction — the
// reconcile loop will render the permanent info bubble; skip the transient one.
function _isSystemSlashCommand(text) {
  return /^\/[a-zA-Z]/.test((text || '').trim());
}

// Message queued while a blocking command (e.g. /compact) is running — sent
// automatically when the command finishes. Cleared on Stop/abort.
let _queuedMessage = null;

// A message sent while a blocking command (e.g. /compact) is running is
// surfaced as a "You (queued…)" bubble — the same pending look used for
// gate-queued messages — instead of an announcement in the pill placeholder.
// The queue holds at most one message, so a newer send replaces the older
// bubble (mirrors _queuedMessage being overwritten).
function _renderQueuedBubble(text) {
  if (!app.chatMessages) return;
  app.chatMessages.querySelectorAll(':scope > .chat-bubble.user[data-blocked-queued]')
    .forEach(el => el.remove());
  const bubble = addChatBubble('user', text);
  if (!bubble) return;
  bubble.setAttribute('data-blocked-queued', '1');
  bubble.classList.add('pending', 'saving');
  const label = bubble.querySelector('.label');
  if (label) label.textContent = 'You (queued\u2026)';
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
  // Any bubble queued behind the blocking command is stale the moment the
  // composer unlocks: either the message is sent below (the normal send
  // pipeline renders a fresh bubble) or the user stopped/aborted and the
  // queue was cleared. Drop it either way — never leave a phantom "queued"
  // bubble in the transcript.
  if (app.chatMessages) {
    app.chatMessages.querySelectorAll(':scope > .chat-bubble.user[data-blocked-queued]')
      .forEach(el => el.remove());
  }
  // If a message was queued during the blocking command, send it now.
  if (_queuedMessage) {
    const msg = _queuedMessage;
    _queuedMessage = null;
    app.chatInput.value = msg;
    sendMessage();
  }
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
const _PERSISTENCE_RECEIPTS_LS_KEY = 'webagent.messagePersistenceReceipts.v1';
let _outboxIdCounter = 0;
// Queue status travels over the websocket independently of the send response.
// Keep the small amount of state needed to reconnect the two paths: an event
// can beat the acknowledgement, a reconcile can replace the optimistic node,
// or an ephemeral/GenUI send may not have created a normal user bubble at all.
const _gateQueueBySession = new Map();
const _recentOutgoingBySession = new Map();

function _rememberOutgoingMessage(sessionId, text, bubble = null) {
  if (!sessionId) return;
  _recentOutgoingBySession.set(String(sessionId), { text: String(text || ''), bubble });
}

function _queuedStateFor(sessionId, turnId) {
  const state = _gateQueueBySession.get(String(sessionId || ''));
  return state && (!turnId || !state.turnId || String(state.turnId) === String(turnId))
    ? state : null;
}
// Entries currently being POSTed by this page. The recovery poll must not retry
// a request merely because its first acknowledgement is taking longer than the
// five-second poll interval.
const _outboxInFlight = new Set();

function _recordPersistence(id, state, detail) {
  try {
    const all = JSON.parse(localStorage.getItem(_PERSISTENCE_RECEIPTS_LS_KEY) || '{}');
    const receipt = all[id] || { events: [] };
    receipt.events.push({ state, detail, at: new Date().toISOString() });
    // Retain a compact audit trail for recent messages without allowing this
    // browser-side recovery aid to grow without bound.
    receipt.events = receipt.events.slice(-12);
    all[id] = receipt;
    const ids = Object.keys(all);
    if (ids.length > 100) {
      ids.sort((a, b) => Date.parse(all[a].events?.at(-1)?.at || 0) - Date.parse(all[b].events?.at(-1)?.at || 0));
      ids.slice(0, ids.length - 100).forEach(old => delete all[old]);
    }
    localStorage.setItem(_PERSISTENCE_RECEIPTS_LS_KEY, JSON.stringify(all));
  } catch (_) { /* localStorage is a recovery aid, never a send blocker */ }
}

function _persistenceDetails(id) {
  try {
    const all = JSON.parse(localStorage.getItem(_PERSISTENCE_RECEIPTS_LS_KEY) || '{}');
    return all[id]?.events || [];
  } catch (_) { return []; }
}

function _readOutbox() {
  let q;
  try {
    const raw = localStorage.getItem(_OUTBOX_LS_KEY);
    q = raw ? JSON.parse(raw) : [];
  } catch (_) { return []; }
  if (!Array.isArray(q) || q.length === 0) return Array.isArray(q) ? q : [];
  // Never silently discard unsaved user text. The explicit dismiss control is
  // the only removal path other than a durable server acknowledgement.
  return q;
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
  _recordPersistence(entry.id, 'queued', 'Saved in this browser recovery queue; waiting for database confirmation.');
  _startOutboxPoll();
}

function _removeFromOutbox(id) {
  const q = _readOutbox().filter(e => e.id !== id);
  _writeOutbox(q);
  if (q.length === 0) _stopOutboxPoll();
}

function _updateOutboxEntry(id, patch) {
  _writeOutbox(_readOutbox().map(e => e.id === id ? { ...e, ...patch } : e));
}

function _outboxHasPending() {
  const q = _readOutbox();
  return q.length > 0;
}

async function _retryEntry(entry, manual = false) {
  if (entry.manual_only && !manual) return false;
  if (_outboxInFlight.has(entry.id)) return false;
  _outboxInFlight.add(entry.id);
  try {
    _recordPersistence(entry.id, 'retrying', manual ? 'Manual database-save retry requested.' : 'Automatic database-save retry started.');
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
    if (resp.status === 402) {
      // Billing denial is permanent until account state changes. Retrying it
      // would reopen the paywall on every recovery poll.
      _removeFromOutbox(entry.id);
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => el.remove());
      return false;
    }
    if (resp.ok) {
      _removeFromOutbox(entry.id);
      const data = await resp.json().catch(() => ({}));
      _recordPersistence(entry.id, 'saved', `Database acknowledged the message${data.turn_id ? ` as ${data.turn_id}` : ''}.`);
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => {
          _markBubbleSaved(el, data && data.turn_id, entry.id);
          if (data && data.turn_id) {
            el.setAttribute('data-msg-id', data.turn_id);
          }
        });
      if (typeof app.populateSessionSelect === 'function') {
        app.populateSessionSelect(app.currentUserId);
      }
      return true;
    }
    document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
      .forEach(el => _convertBubbleToPending(el, entry, `Save failed (${resp.status}) — retry`));
    _recordPersistence(entry.id, 'error', `Database save returned HTTP ${resp.status}.`);
    // Permanent client errors are paused for manual retry; the message is never
    // silently discarded from the browser recovery journal.
    if ([400, 401, 403, 404, 409, 410, 422].includes(resp.status)) {
      _updateOutboxEntry(entry.id, { manual_only: true });
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => _convertBubbleToPending(el, entry, `Save failed (${resp.status}) — retry`));
    }
  } catch (error) {
    _recordPersistence(entry.id, 'error', `Database save could not be reached: ${error?.message || 'network error'}.`);
  } finally {
    _outboxInFlight.delete(entry.id);
  }
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
  // Pending/retry bubbles are debug UI — hidden when debug is off
  if (!app.isDebug) return;
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble user pending${entry.manual_only ? ' save-error' : ''}`;
  bubble.setAttribute('data-pending-id', entry.id);
  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = entry.manual_only ? 'You (save failed — retry)' : 'You (saving / retrying)';
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

function _convertBubbleToPending(bubble, entry, labelText = 'You (save failed — retry)') {
  // Pending/retry bubbles are debug UI — silently remove the bubble when debug is off.
  // The outbox still tracks the entry and retries in the background.
  if (!app.isDebug) {
    if (bubble) bubble.remove();
    return;
  }
  let label = bubble.querySelector('.label');
  if (!label) {
    label = document.createElement('span');
    label.className = 'label';
    bubble.insertBefore(label, bubble.firstChild);
  }
  label.textContent = labelText;
  bubble.className = 'chat-bubble user pending save-error';
  bubble.setAttribute('data-pending-id', entry.id);
  bubble.querySelectorAll('.bubble-actions').forEach(el => el.remove());
  bubble.removeAttribute('data-msg-id');
  bubble.removeAttribute('data-turn-id');
  _appendPendingActions(bubble, entry);
  _refreshLucideIcons(bubble);
}

function _markBubbleSaving(bubble, entry) {
  if (!bubble) return;
  if (!app.isDebug) return;
  let label = bubble.querySelector('.label');
  if (!label) {
    label = document.createElement('span');
    label.className = 'label';
    bubble.insertBefore(label, bubble.firstChild);
  }
  label.textContent = 'You (saving…)';
  bubble.classList.add('pending', 'saving');
  bubble.setAttribute('data-pending-id', entry.id);
}

function _attachPersistenceDetails(bubble, messageId) {
  if (!bubble || !messageId) return;
  const label = bubble.querySelector('.label');
  if (!label) return;
  label.classList.add('persistence-status');
  label.tabIndex = 0;
  label.setAttribute('role', 'button');
  label.title = 'Show message persistence details';
  const existing = bubble.querySelector('.persistence-details');
  if (existing) existing.remove();
  const details = document.createElement('div');
  details.className = 'persistence-details';
  const events = _persistenceDetails(messageId);
  details.textContent = events.length
    ? events.map(e => `${new Date(e.at).toLocaleTimeString()}: ${e.detail}`).join('\n')
    : 'No browser-side persistence details are available for this message.';
  bubble.appendChild(details);
  const toggle = () => details.classList.toggle('open');
  label.onclick = toggle;
  label.onkeydown = (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggle();
    }
  };
}

function _markBubbleSaved(bubble, turnId, messageId = turnId) {
  if (!bubble) return;
  if (!app.isDebug) {
    bubble.className = 'chat-bubble user saved';
    bubble.removeAttribute('data-pending-id');
    bubble.querySelectorAll('.label').forEach(el => el.remove());
    bubble.querySelectorAll('.bubble-actions').forEach(el => el.remove());
    if (turnId) bubble.setAttribute('data-msg-id', turnId);
    _addBubbleActions(bubble);
    _restoreGateQueueBubble(bubble, turnId);
    return;
  }
  bubble.className = 'chat-bubble user saved';
  bubble.removeAttribute('data-pending-id');
  const label = bubble.querySelector('.label');
  if (label) label.textContent = 'You (saved)';
  bubble.querySelectorAll('.bubble-actions').forEach(el => el.remove());
  if (turnId) bubble.setAttribute('data-msg-id', turnId);
  _addBubbleActions(bubble);
  _attachPersistenceDetails(bubble, messageId);
  _restoreGateQueueBubble(bubble, turnId);
}

function _restorePersistenceStatus(bubble, messageId) {
  if (!bubble || !messageId || _persistenceDetails(messageId).length === 0) return;
  if (!app.isDebug) return;
  bubble.classList.add('saved');
  const label = bubble.querySelector('.label');
  if (label) label.textContent = 'You (saved)';
  _attachPersistenceDetails(bubble, messageId);
}

function _appendPendingActions(bubble, entry) {
  if (!app.isDebug) return;
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
  retryBtn.title = 'Retry database save';
  retryBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    retryBtn.disabled = true;
    retryBtn.innerHTML = '<span style="font-size:12px;">\u21BB</span>';
    const ok = await _retryEntry(entry, true);
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
  dismissBtn.title = 'Dismiss (discard this unsaved message)';
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
        document.querySelectorAll('.chat-bubble.user.pending')
          .forEach(el => { if (!el.hasAttribute('data-blocked-queued')) el.remove(); });
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
  if (!app.isDebug) return;
  document.querySelectorAll('.chat-bubble.user.pending')
    .forEach(el => { if (!el.hasAttribute('data-blocked-queued')) el.remove(); });
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
  // Stop is the escape hatch from a composer-locking command (e.g. /compact):
  // hand control back to the user immediately, even if the server is still busy.
  // Clear any queued message — the user chose to abort.
  _queuedMessage = null;
  _unlockComposer();

  // Flag that a stop was requested so late-arriving WS stream events don't
  // re-light the activity indicator or re-engage processing state.
  app._stopPending = true;

  // Immediate UI feedback: show "Stopping…" in the activity bar (the chip
  // above the chat pill) so the user knows their stop request was received.
  // The server will later emit an `interrupted` event which replaces it with
  // "Stopped" and fades the bar.
  if (typeof app.chatActivityStart === 'function') {
    app.chatActivityStart('Stopping…');
  }

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

  // If a blocking command (e.g. /compact) is already running, queue this message
  // instead of interrupting it. It will be sent automatically when the command
  // finishes. The Stop button clears the queue.
  if (app._composerLocked && !_isBlockingCommand(text)) {
    _queuedMessage = text;
    app.chatInput.value = '';
    app.chatSend.disabled = true;
    _updateInputRowState();
    _autoResizePill(app.chatInput);
    _clearDraft();
    // Surface the queue on the message BUBBLE (same look as gate-queued
    // messages) rather than on the pill — the pill keeps its plain
    // "compacting" hint and never claims the message is queued.
    _renderQueuedBubble(text);
    return;
  }

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
  _outboxInFlight.add(outboxEntry.id);
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
  // ── GenUI-originated send (page field/button): the genui bridge set a one-shot
  // label override. Render a green notice with the label instead of the raw
  // prompt in a "You" bubble — the raw prompt is still the message the agent
  // receives; it only surfaces in the panel under the debug toggle.
  const _genuiLabel = app._genuiLabelOverride || '';
  try { app._genuiLabelOverride = null; } catch (_) {}
  const _isGenuiSend = !!_genuiLabel;
  let _userBubble;
  if (_isGenuiSend) {
    // Leave the bubble UNTAGGED so the WS user_message echo / reconcile can
    // adopt it by text and stamp the server interaction id (mirrors the normal
    // optimistic user-bubble flow) — tagging it with the client id early would
    // defeat that adoption and produce a duplicate notice.
    _userBubble = addChatBubble('info', _genuiLabel, 'system-genui', undefined, undefined, undefined, outboxEntry.timestamp);
    if (app.isDebug && (text || '').trim()) {
      addChatBubble('info', 'Raw prompt:\n' + text, 'system-debug', undefined, undefined, undefined, outboxEntry.timestamp);
    }
  } else {
    _userBubble = addChatBubble('user', text, undefined, undefined, undefined, undefined, outboxEntry.timestamp);
    _markBubbleSaving(_userBubble, outboxEntry);
  }
  // This is deliberately independent of outbox persistence. A gate-status
  // websocket event may arrive before the send POST resolves (or after a
  // renderer has briefly replaced this node), so retain the actual message as
  // a recovery source for the queued-state renderer.
  _rememberOutgoingMessage(outboxEntry.session_id, text, _userBubble);
  // ── Render pending attachments into the user bubble ──
  const _pendingAtts = getPendingAttachments();
  if (!_isGenuiSend && _pendingAtts.length > 0 && _userBubble) {
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
  // Target device for this turn (same value stored on the outbox entry above). An
  // instance id routes the turn to that device's worker; '' runs locally. This was
  // missing from the primary send payload, so a targeted turn only ever dispatched
  // on an outbox RETRY — the first (normal) send always ran locally. (see app/api/chat.py)
  if (app.targetDevice) base.target_device = app.targetDevice;
  const payload = await addAttachmentsToMessage(base);
  if (app.clearPendingAttachments) app.clearPendingAttachments();

  // ── Browser mode send path (SSE stream, no server-side persistence) ──
  if (storageAdapter.isBrowser) {
    _outboxInFlight.delete(outboxEntry.id);
    _removeFromOutbox(outboxEntry.id);
    _recordPersistence(outboxEntry.id, 'saved', 'Browser mode — local storage.');
    if (!_isGenuiSend) _markBubbleSaved(_userBubble, outboxEntry.id, outboxEntry.id);

    const _isSlash = _isSystemSlashCommand(text);
    let _assistantBubble = null;
    try {
      await browserRouter.sendMessage(app.currentSessionId, text, {
        onStream: (chunk) => {
          if (_isSlash) return;  // skip streaming for slash commands — handled by onResponse
          if (!_assistantBubble) {
            _assistantBubble = addChatBubble('agent', chunk);
            if (_assistantBubble) _addBubbleActions(_assistantBubble);
          } else {
            const textEl = _assistantBubble.querySelector('.chat-bubble-text');
            if (textEl) textEl.textContent += chunk;
          }
        },
        onResponse: (content) => {
          if (_isSlash) {
            addChatBubble('info', content);
          } else if (_assistantBubble) {
            const textEl = _assistantBubble.querySelector('.chat-bubble-text');
            if (textEl) textEl.textContent = content;
          } else {
            _assistantBubble = addChatBubble('agent', content);
            if (_assistantBubble) _addBubbleActions(_assistantBubble);
          }
          app.isProcessing = false;
          _unlockComposer();
          app.chatSend.disabled = false;
          if (app.chatActivityStop) app.chatActivityStop();
          if (typeof app.populateSessionSelect === 'function') {
            app.populateSessionSelect(app.currentUserId);
          }
        },
        onError: (err) => {
          // Only surface agent errors in debug mode.
          if (app.isDebug) {
            addChatBubble('agent', '\u274C ' + err, 'error');
          }
          app.isProcessing = false;
          _unlockComposer();
          app.chatSend.disabled = false;
          if (app.chatActivityStop) app.chatActivityStop();
        },
      });
    } catch (e) {
      addChatBubble('agent', '\u274C ' + (e.message || e), 'error');
      app.isProcessing = false;
      _unlockComposer();
      app.chatSend.disabled = false;
      if (app.chatActivityStop) app.chatActivityStop();
    }
    if (app._healingTimer) { clearTimeout(app._healingTimer); app._healingTimer = null; }
    return;
  }

  try {
    const resp = await fetch(apiPath('/api/v1/chat/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
    });
    _outboxInFlight.delete(outboxEntry.id);

    if (!resp.ok) {
      if (resp.status === 402) {
        // The global billing interceptor has already recorded the terminal trial
        // state. Discard the rejected optimistic turn and its retry journal entry.
        _removeFromOutbox(outboxEntry.id);
        if (_userBubble) _userBubble.remove();
        app.isProcessing = false;
        _queuedMessage = null;
        _unlockComposer();
        applyChatGate();
        if (app._healingTimer) { clearTimeout(app._healingTimer); app._healingTimer = null; }
        if (app.chatActivityStop) app.chatActivityStop();
        return;
      }
      _recordPersistence(outboxEntry.id, 'error', `Database save returned HTTP ${resp.status}; retained for retry.`);
      // GenUI label notices are informational — keep them as-is; the outbox
      // still tracks + retries the entry in the background.
      if (_userBubble && !_isGenuiSend) _convertBubbleToPending(_userBubble, outboxEntry, `Save failed (${resp.status}) — retry`);
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
      _recordPersistence(outboxEntry.id, 'saved', 'Database acknowledged the message and returned a reply.');
      if (!_isGenuiSend) _markBubbleSaved(_userBubble, data.turn_id, outboxEntry.id);
      // Synchronous reply path. For blocking/system commands (e.g. /compact,
      // /optimize), the result is also persisted as a role='system' interaction
      // and the reconcile loop will render it as an info bubble — don't double-
      // render it here as an agent bubble.
      if (_isSystemSlashCommand(text)) {
        app.isProcessing = false;
        _unlockComposer();
        app.chatSend.disabled = false;
        if (app.chatActivityStop) app.chatActivityStop();
        if (typeof app.populateSessionSelect === 'function') {
          app.populateSessionSelect(app.currentUserId);
        }
        return;
      }
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

    if (_userBubble) {
      _recordPersistence(outboxEntry.id, 'saved', data.turn_id
        ? `Database acknowledged the message as ${data.turn_id}.`
        : 'Database acknowledged the message.');
      if (!_isGenuiSend) _markBubbleSaved(_userBubble, data.turn_id, outboxEntry.id);
    }
    if (typeof app.populateSessionSelect === 'function') {
      app.populateSessionSelect(app.currentUserId);
    }
  } catch (e) {
    _outboxInFlight.delete(outboxEntry.id);
    console.warn('[chat/send] failed', e);
    _recordPersistence(outboxEntry.id, 'error', `Database save could not be reached: ${e?.message || 'network error'}.`);
    if (_userBubble && !_isGenuiSend) _convertBubbleToPending(_userBubble, outboxEntry);
    app.isProcessing = false;
    _unlockComposer();
    app.chatSend.disabled = false;
    if (app.chatActivityStop) app.chatActivityStop();
  }
}

function abortChatStream() {
  app.agentBuffer = '';
  app.isProcessing = false;
  app._stopPending = false;
  _queuedMessage = null;
  _unlockComposer();
  if (app.chatSend) app.chatSend.disabled = false;
}

// Exposed on `app` so session modules can call it without importing this module
app.abortChatStream = abortChatStream;

app.refreshChat = async () => {
  if (!app.currentSessionId) return;
  // Remote-change guard (same rule as refreshTranscript): ask the server whether
  // the transcript changed. If NOT modified, skip the full DB resync entirely —
  // no cache drop, no re-render. The healing timer's real job is recovering a
  // wedged composer, so if the UI is still stuck "processing" we still clear
  // that state (without touching the transcript), and the ~0.8s reconcile loop
  // remains the primary recovery path for late-arriving rows.
  try {
    const changed = await _transcriptChangedRemotely(app.currentSessionId);
    if (!changed) {
      if (app.isProcessing) {
        app.isProcessing = false;
        _unlockComposer();
        if (app.chatActivityStop) app.chatActivityStop();
      }
      return;
    }
  } catch (_) { /* helper swallows errors — proceed conservatively */ }
  // Force a clean load: set _lastLoadedSessionId to null so loadSessionChat
  // clears the existing bubbles before re-rendering. Without this the 60s
  // healing timer appends duplicate bubbles on every fire for a long-running
  // session (optimizer runs, multi-tool turns) — the same-session guard in
  // loadSessionChat skips the clear, so _renderSessionWindowed adds fresh
  // bubbles without removing the old ones.
  app._lastLoadedSessionId = null;
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
  // During pill rebuild (applyChatPillLayout), elements are reparented and
  // clientWidth/clientHeight are transient — skip to avoid writing garbage
  // dimensions (e.g. ~20px) into --chat-pill-current-width.
  if (app.__rebuildingPill) return;
  const cs = getComputedStyle(el);
  const minH = parseFloat(cs.minHeight) || 32;
  const maxH = parseFloat(cs.maxHeight) || 160;

  const pill = el.closest('.chat-pill');
  const isMainPill = pill && pill.id === 'chat-input-row';

  if (!isMainPill) {
    // ── Vertical resize (non-main pills: 1-line, search, builder, etc.) ──
    if (!el.value) {
      el.style.height = minH ? minH + 'px' : 'auto';
      el.style.overflowY = 'hidden';
      _updateScrollIndicator(el);
      return;
    }
    el.style.height = '1px';
    const scrollH = el.scrollHeight;
    const padT = parseFloat(cs.paddingTop) || 0;
    const padB = parseFloat(cs.paddingBottom) || 0;
    const lnH  = parseFloat(cs.lineHeight) || 22;
    const oneLineH = padT + lnH + padB;
    const targetH = scrollH <= oneLineH
      ? minH
      : Math.max(minH, Math.min(scrollH, maxH));
    el.style.height = targetH + 'px';
    el.style.overflowY = scrollH > maxH ? 'auto' : 'hidden';
    _updateScrollIndicator(el);
    return;
  }

  // ── Main composer (#chat-input-row): hybrid horizontal + vertical ──

  // Compute bounds
  const inputArea = pill.closest('#chat-input-area');
  const padX = 24; // 12px left + 12px right on #chat-input-area
  const availableW = inputArea ? inputArea.clientWidth - padX : 600;
  const configuredMaxW = parseFloat(
    getComputedStyle(pill).getPropertyValue('--chat-surface-max-width')
  ) || availableW;
  const maxPillW = Math.min(availableW, configuredMaxW);
  const configuredPillW = parseFloat(
    getComputedStyle(pill).getPropertyValue('--chat-pill-width')
  ) || 500;
  const restingPillW = Math.min(configuredPillW, maxPillW);

  if (!el.value) {
    inputArea?.style.setProperty('--chat-pill-current-width', restingPillW + 'px');
    // Empty: clear overrides so the pill and control bands share the configured
    // resting width again.
    pill.style.width = '';
    pill.style.minWidth = '';
    pill.style.maxWidth = '';
    el.style.whiteSpace = '';
    el.style.overflowWrap = '';
    el.style.wordBreak = '';
    el.style.height = minH + 'px';
    el.style.overflowY = 'hidden';
    return;
  }

  // ── Phase 1: measure text content width ──
  const pillOverhead = pill.clientWidth - el.clientWidth;

  // scrollWidth bottoms out at clientWidth, so narrow glyphs can accumulate
  // without changing it. Measure the rendered line itself instead.
  const canvas = _autoResizePill._measureCanvas
    || (_autoResizePill._measureCanvas = document.createElement('canvas'));
  const ctx = canvas.getContext('2d');
  ctx.font = cs.font;
  const letterSpacing = parseFloat(cs.letterSpacing) || 0;
  const lines = el.value.split('\n');
  const naturalW = Math.max(0, ...lines.map(line => {
    const measured = ctx.measureText(line.replace(/\t/g, '    ')).width;
    return measured + Math.max(0, line.length - 1) * letterSpacing;
  })) + (parseFloat(cs.paddingLeft) || 0)
      + (parseFloat(cs.paddingRight) || 0)
      + 16; // keep the caret ahead of the textarea's native clipping edge

  const targetPillW = Math.max(restingPillW, Math.min(naturalW + pillOverhead, maxPillW));
  inputArea?.style.setProperty('--chat-pill-current-width', targetPillW + 'px');

  // ── Phase 2: apply width + height atomically ──
  const savedPillT = pill.style.transition;
  const savedElT = el.style.transition;
  pill.style.transition = 'none';
  el.style.transition = 'none';

  // Only set an explicit width while expanded; at rest the shared CSS variable
  // keeps the pill and surrounding control bands aligned.
  if (targetPillW > restingPillW) {
    pill.style.width = targetPillW + 'px';
    pill.style.maxWidth = 'none';
  } else {
    // Within resting bounds → clear overrides, same as footer
    pill.style.width = '';
    pill.style.maxWidth = '';
  }

  // ── Phase 3: toggle wrapping on/off ──
  // When the pill is at max width and content still overflows, enable text
  // wrapping (pre-wrap). Otherwise keep pre (no-wrap) so the pill widens
  // before any line break.
  const atMaxWidth = targetPillW >= maxPillW;
  const contentOverflows = naturalW + pillOverhead > maxPillW;
  if (atMaxWidth && contentOverflows) {
    el.style.whiteSpace = 'pre-wrap';
    el.style.overflowWrap = 'break-word';
    el.style.wordBreak = 'break-word';
  } else {
    el.style.whiteSpace = '';
    el.style.overflowWrap = '';
    el.style.wordBreak = '';
  }

  // A textarea scrolls horizontally to reveal the caret before this input
  // handler gets a chance to widen the pill. Once the content fits again,
  // discard that stale offset or the leading text remains visibly clipped.
  if (!contentOverflows) el.scrollLeft = 0;

  // Measure height at the new width.
  el.style.height = '1px';
  const scrollH = el.scrollHeight;
  const padT = parseFloat(cs.paddingTop) || 8;
  const padB = parseFloat(cs.paddingBottom) || 2;
  const lnH  = parseFloat(cs.lineHeight) || 22;
  const oneLineH = padT + lnH + padB;

  const targetH = scrollH <= oneLineH
    ? minH
    : Math.max(minH, Math.min(scrollH, maxH));
  el.style.height = targetH + 'px';
  el.style.overflowY = scrollH > maxH ? 'auto' : 'hidden';

  pill.offsetHeight; // force reflow
  pill.style.transition = savedPillT;
  el.style.transition = savedElT;
}

function _updateScrollIndicator(_el) {
  // Removed — scroll indicator dot inside the pill is no longer used.
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

// ── Gate-queue bubble styling ──────────────────────────────────────────
// Called from agentWs.js when agent_status: queued fires — the session cap
// was reached and this session is waiting in the FIFO queue. Apply the
// same pending (dashed-border + ⏳) look used for outbox-retry messages.

function _findGateQueuedBubble(turnId) {
  if (!app.chatMessages) return null;
  let bubble = null;
  if (turnId) {
    bubble = app.chatMessages.querySelector(
      `.chat-bubble.user[data-turn-id="${CSS.escape(String(turnId))}"], .chat-bubble.user[data-msg-id="${CSS.escape(String(turnId))}"]`
    );
  }
  return bubble;
}

function _applyGateQueuedStyle(bubble, state) {
  if (!bubble || !state) return null;
  // Don't re-mark a bubble that's already in save-error state
  if (bubble.classList.contains('save-error')) return null;
  bubble.classList.add('pending', 'saving');
  bubble.setAttribute('data-gate-queued', String(state.queuePosition || '1'));
  let label = bubble.querySelector('.label');
  if (!label) {
    label = document.createElement('span');
    label.className = 'label';
    bubble.insertBefore(label, bubble.firstChild);
  }
  label.textContent = 'You (queued' + (state.queuePosition ? ' #' + state.queuePosition : '') + '\u2026)';
  _addForceRunButton(bubble, state.turnId);
  return bubble;
}

function _restoreGateQueueBubble(bubble, turnId) {
  const state = _queuedStateFor(app.currentSessionId, turnId);
  if (state) _applyGateQueuedStyle(bubble, state);
}

function markBubbleQueued(turnId, queuePosition, sessionId = app.currentSessionId) {
  const sid = String(sessionId || app.currentSessionId || '');
  if (!sid) return;
  const remembered = _recentOutgoingBySession.get(sid);
  const state = {
    turnId: turnId || null,
    queuePosition: Number.isFinite(queuePosition) ? queuePosition : null,
    text: remembered?.text || '',
  };
  _gateQueueBySession.set(sid, state);

  // Do not touch a different session's transcript. Its title-bar queue state
  // is updated by the dropdown cache; when this session is opened, a normal
  // persisted user message can still be styled by a later status event.
  if (sid !== String(app.currentSessionId || '')) return;

  let bubble = _findGateQueuedBubble(turnId);
  // Prefer the bubble registered by this send over an unqualified DOM scan:
  // an ephemeral/GenUI turn can otherwise accidentally mark an older user
  // bubble while its own node is still being reconciled.
  if (!bubble && remembered?.bubble?.isConnected
      && remembered.bubble.classList.contains('user')) {
    bubble = remembered.bubble;
  }
  // Last-resort rendering is intentional: GenUI and ephemeral messages may
  // have no durable user node at the instant the queue event arrives. Showing
  // the queued turn is more useful than silently dropping its only feedback.
  if (!bubble && app.chatMessages && state.text) {
    bubble = addChatBubble('user', state.text);
  }
  _applyGateQueuedStyle(bubble, state);
}

// "Force run" button on a queued bubble: bypasses the session gate so this
// turn starts immediately instead of waiting for a slot to free.
function _addForceRunButton(bubble, turnId) {
  if (!bubble || bubble.querySelector('.force-run-btn')) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn force-run-btn';
  btn.innerHTML = '<i data-lucide="zap" style="width:13px;height:13px;"></i> Force run';
  btn.title = 'Bypass the session queue and run this message now';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    btn.disabled = true;
    try {
      const resp = await fetch(apiPath('/api/v1/chat/force-run'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          user_id: app.currentUserId,
          session_id: app.currentSessionId,
        }),
      });
      if (!resp.ok) {
        btn.disabled = false;
        btn.title = 'Force run failed — try again';
        return;
      }
      // Success: clear the queued styling immediately. The run wakes and
      // emits agent_status: running (via run_state_begin), which also calls
      // clearBubbleQueued — idempotent, so no double-handling.
      clearBubbleQueued(turnId);
      // Patch the dropdown row back to running state right away.
      if (typeof app.onSessionGateForce === 'function') {
        try { app.onSessionGateForce(); } catch (_) { /* ignore */ }
      }
    } catch (_) {
      btn.disabled = false;
      btn.title = 'Force run failed — try again';
    }
  });
  // Place into the existing actions footer if present, else make a small one.
  let actions = bubble.querySelector(':scope > .bubble-actions');
  if (!actions) {
    actions = document.createElement('div');
    actions.className = 'bubble-actions';
    bubble.appendChild(actions);
  }
  actions.appendChild(btn);
  _refreshLucideIcons(bubble);
}

// Clear the queued styling when the run starts (agent_status: running) or the
// user forces the run. Clears every gate-queued bubble (safe: at most one per
// session) and removes the force button.
function clearBubbleQueued(turnId, sessionId = app.currentSessionId) {
  const sid = String(sessionId || app.currentSessionId || '');
  const state = _gateQueueBySession.get(sid);
  if (state && (!turnId || !state.turnId || String(state.turnId) === String(turnId))) {
    _gateQueueBySession.delete(sid);
  }
  if (!app.chatMessages) return;
  const marked = app.chatMessages.querySelectorAll('[data-gate-queued]');
  marked.forEach((bubble) => {
    if (turnId && bubble.getAttribute('data-msg-id') && bubble.getAttribute('data-msg-id') !== String(turnId)) return;
    bubble.classList.remove('pending', 'saving');
    bubble.removeAttribute('data-gate-queued');
    bubble.querySelectorAll('.force-run-btn').forEach((el) => el.remove());
    const label = bubble.querySelector('.label');
    if (label && /queued/.test(label.textContent || '')) {
      label.textContent = 'You';
    }
  });
}

app.markBubbleQueued = markBubbleQueued;
app.clearBubbleQueued = clearBubbleQueued;

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
  _restorePersistenceStatus,
};
