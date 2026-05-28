'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { addAttachmentsToMessage, renderAttachmentElement } from './attachments.js';
import { getAccessMode, fetchAccessMode, authHeaders } from './left-login.js';

/** Returns true when the current visitor may use chat under the active access mode. */
function _canChat() {
  const mode = getAccessMode();
  if (mode === 'public_anonymous') return true;
  // public_registered, admin_approval, private — all require sign-in
  return !!localStorage.getItem('auth_token');
}

const _CHAT_LOCK_PLACEHOLDER = 'Sign in to chat — this app does not allow anonymous use.';
let _origChatPlaceholder = '';

// CHAT-PILL-SYNC: this is the web chat's has-text toggle. The same pattern is
// implemented for #agent-builder-bar-row in ui/js/agents.js (_bindAgentBuilderBar)
// and for #autoagent-prompt-row in ui/js/autoagent.js (initAutoAgent). All four
// pills share the .chat-pill* CSS in ui/css/app1.css.
function _updateInputRowState() {
  if (!app.chatInput) return;
  const row = document.getElementById('chat-input-row');
  if (!row) return;
  const hasText = !!app.chatInput.value.trim();
  row.classList.toggle('has-text', hasText);
}

function applyChatGate() {
  if (!app.chatInput || !app.chatSend) return;
  const allowed = _canChat();
  if (!_origChatPlaceholder) _origChatPlaceholder = app.chatInput.placeholder || '';
  if (allowed) {
    app.chatInput.disabled = false;
    app.chatInput.placeholder = _origChatPlaceholder;
    app.chatSend.disabled = !app.chatInput.value.trim();
  } else {
    app.chatInput.disabled = true;
    app.chatInput.value = '';
    app.chatInput.placeholder = _CHAT_LOCK_PLACEHOLDER;
    app.chatSend.disabled = true;
  }
  _updateInputRowState();
}

window.addEventListener('access-mode-loaded',  applyChatGate);
window.addEventListener('access-mode-changed', applyChatGate);

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

const URL_RE = /https?:\/\/[^\s<>"]+/g;

function linkifyText(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  let match;
  URL_RE.lastIndex = 0;
  while ((match = URL_RE.exec(text)) !== null) {
    if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
    const a = document.createElement('a');
    a.href = match[0];
    a.textContent = match[0];
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    frag.appendChild(a);
    last = match.index + match[0].length;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  return frag;
}

// ── session_seq persistence ──
// Live in localStorage so a hard refresh mid-stream still tells the server
// what we've already seen, and the WS replay can pick up from there instead
// of dumping every buffered event back at us (or — worse — none, if the
// in-memory map was lost and we end up filtering replayed events for an
// unknown session).
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

function addChatBubble(role, text, extraClass, imageUrl, turnId) {
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (turnId) bubble.setAttribute('data-turn-id', turnId);
  // Show 'You' label for user, omit for agent (already prefixed with agent name in content)
  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
  }
  bubble.appendChild(linkifyText(text));
  if (imageUrl) {
    const img = document.createElement('img');
    img.src = imageUrl;
    img.style.maxWidth = '100%';
    img.style.maxHeight = '400px';
    img.style.borderRadius = '8px';
    img.style.marginTop = '8px';
    img.style.border = '1px solid #444';
    bubble.appendChild(img);
  }
  if (role === 'agent' && window.__streamAttachments && extraClass === 'has-attachments') {
    for (const att of window.__streamAttachments) {
      const el = renderAttachmentElement(att);
      if (el) bubble.appendChild(el);
    }
    window.__streamAttachments = null;
  }
  if (role === 'agent' && extraClass === 'streaming') {
    const stopBtn = document.createElement('button');
    stopBtn.className = 'stop-btn';
    stopBtn.textContent = '\ud83d\uded1';
    stopBtn.title = 'Stop generation';
    stopBtn.addEventListener('click', sendStopMessage);
    bubble.appendChild(stopBtn);
  }
  app.chatMessages.appendChild(bubble);
  app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
  _addBubbleActions(bubble);
  return bubble;
}

// \u2500\u2500 Per-bubble action row (read-aloud + copy) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// Extracts the readable text from a bubble, excluding the 'You' label,
// the action buttons themselves, and the streaming stop button.
function _getBubbleText(bubble) {
  if (!bubble) return '';
  const clone = bubble.cloneNode(true);
  clone.querySelectorAll('.label, .bubble-actions, .stop-btn').forEach(el => el.remove());
  return clone.textContent.trim();
}

function _setActionIcon(btn, iconName) {
  const i = btn.querySelector('i');
  if (!i) return;
  i.setAttribute('data-lucide', iconName);
  // Reset so lucide can re-render this node (lucide skips nodes already marked .lucide)
  i.classList.remove('lucide');
  i.removeAttribute('stroke');
  while (i.firstChild) i.removeChild(i.firstChild);
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try { window.lucide.createIcons({ nodes: [i] }); } catch (_) {}
  }
}

function _speakBubble(btn, bubble) {
  if (!('speechSynthesis' in window)) {
    alert('Text-to-speech is not supported in this browser.');
    return;
  }
  const synth = window.speechSynthesis;
  if (btn.dataset.speaking === 'true') {
    try { synth.cancel(); } catch (_) {}
    return;
  }
  const text = _getBubbleText(bubble);
  if (!text) return;
  try { synth.cancel(); } catch (_) {}
  // Reset state on any other action buttons that may still be marked speaking.
  document.querySelectorAll('.bubble-action-btn[data-speaking="true"]').forEach((other) => {
    delete other.dataset.speaking;
    other.title = 'Read aloud';
    _setActionIcon(other, 'volume-2');
  });
  const u = new SpeechSynthesisUtterance(text);
  const restore = () => {
    delete btn.dataset.speaking;
    btn.title = 'Read aloud';
    _setActionIcon(btn, 'volume-2');
  };
  u.onend = restore;
  u.onerror = restore;
  btn.dataset.speaking = 'true';
  btn.title = 'Stop reading';
  _setActionIcon(btn, 'square');
  synth.speak(u);
}

async function _copyBubble(btn, bubble) {
  const text = _getBubbleText(bubble);
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } finally { ta.remove(); }
    }
    const origTitle = btn.title;
    btn.title = 'Copied!';
    btn.classList.add('copied');
    _setActionIcon(btn, 'check');
    setTimeout(() => {
      btn.title = origTitle;
      btn.classList.remove('copied');
      _setActionIcon(btn, 'copy');
    }, 1200);
  } catch (e) {
    console.warn('Copy failed:', e);
  }
}

function _addBubbleActions(bubble) {
  if (!bubble) return;
  // Don't render actions while the bubble is still streaming.
  if (bubble.classList.contains('streaming')) return;
  const txt = _getBubbleText(bubble);
  if (!txt || txt === '\u2026') return;
  // Avoid double-adding.
  if (bubble.querySelector(':scope > .bubble-actions')) return;

  const actions = document.createElement('div');
  actions.className = 'bubble-actions';

  const speakBtn = document.createElement('button');
  speakBtn.type = 'button';
  speakBtn.className = 'bubble-action-btn';
  speakBtn.title = 'Read aloud';
  speakBtn.innerHTML = '<i data-lucide="volume-2" style="width:14px;height:14px;"></i>';
  speakBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _speakBubble(speakBtn, bubble);
  });
  actions.appendChild(speakBtn);

  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'bubble-action-btn';
  copyBtn.title = 'Copy text';
  copyBtn.innerHTML = '<i data-lucide="copy" style="width:14px;height:14px;"></i>';
  copyBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _copyBubble(copyBtn, bubble);
  });
  actions.appendChild(copyBtn);

  bubble.appendChild(actions);
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try {
      window.lucide.createIcons({
        nodes: Array.from(actions.querySelectorAll('[data-lucide]:not(.lucide)')),
      });
    } catch (_) {}
  }
}

async function sendStopMessage() {
  addChatBubble('user', '\ud83d\uded1 Stop');

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

function updateLastBubble(text, extraClass, imageUrl) {
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  const last = bubbles[bubbles.length - 1];
  if (!last) return;
  while (last.firstChild) last.removeChild(last.firstChild);
  last.appendChild(linkifyText(text));
  if (imageUrl) {
    const img = document.createElement('img');
    img.src = imageUrl;
    img.style.maxWidth = '100%';
    img.style.maxHeight = '400px';
    img.style.borderRadius = '8px';
    img.style.marginTop = '8px';
    img.style.border = '1px solid #444';
    last.appendChild(img);
  }
  if (window.__streamAttachments && extraClass === 'has-attachments') {
    for (const att of window.__streamAttachments) {
      const el = renderAttachmentElement(att);
      if (el) last.appendChild(el);
    }
    window.__streamAttachments = null;
  }
  if (extraClass === 'streaming') {
    const stopBtn = document.createElement('button');
    stopBtn.className = 'stop-btn';
    stopBtn.textContent = '\ud83d\uded1';
    stopBtn.title = 'Stop generation';
    stopBtn.addEventListener('click', sendStopMessage);
    last.appendChild(stopBtn);
  }
  if (extraClass) last.className = 'chat-bubble agent ' + extraClass;
  else last.classList.remove('streaming');
  app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
  _addBubbleActions(last);
}

async function sendMessage() {
  if (!_canChat()) { applyChatGate(); return; }
  const text = app.chatInput.value.trim();
  if (!text) return;

  // No agent selected — open the new-agent modal instead of sending. Keep the
  // typed text so the user can resend after picking a template + creating.
  if (!app.currentAgentId) {
    const sel = document.getElementById('main-tab-select');
    if (sel) {
      sel.value = 'agents';
      sel.dispatchEvent(new Event('change'));
    }
    setTimeout(() => {
      const newBtn = document.getElementById('btn-new-agent');
      if (newBtn) newBtn.click();
    }, 50);
    return;
  }

  app.chatInput.value = '';
  app.chatSend.disabled = true;
  _updateInputRowState();

  // Advance the poll cursor so auto-poll doesn't re-render this message
  if (window.__chatPollLastAt !== undefined) {
    window.__chatPollLastAt = new Date().toISOString();
  }

  addChatBubble('user', text);
  addChatBubble('agent', '\u2026', 'streaming');
  app.isProcessing = true;

  // Build payload
  const base = {
    message: text,
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
  };
  if (app.currentAgentId) base.agent_id = app.currentAgentId;
  const payload = addAttachmentsToMessage(base);
  if (app.clearPendingAttachments) app.clearPendingAttachments();

  // Reset any previous SSE reader
  if (app._sseAbortController) {
    app._sseAbortController.abort();
  }
  app._sseAbortController = new AbortController();

  // Signal to WS handler that SSE is the active display source
  window.__sseActive = true;

  // Diagnostics for "Request failed: …" / "Error in input stream" investigations.
  const _sseDiag = {
    startedAt: performance.now(),
    bytes: 0,
    eventCount: 0,
    lastEventType: null,
    lastEventAt: null,
    headersReceivedAt: null,
  };

  // Whether we observed a terminal event (response / error / interrupted).
  // If the reader closes without one, we force-unlock the UI in the
  // post-loop guard — otherwise the chat input stays disabled forever
  // and looks like a "disconnect". See the SSE-stuck-state bug.
  let _sseTerminalSeen = false;

  // Idle watchdog: if no bytes arrive for SSE_IDLE_MS we abort the fetch
  // so the user gets a real error instead of an indefinite `…`. Reset on
  // each chunk; cleared in `finally`.
  const SSE_IDLE_MS = 60000;
  let _sseIdleTimer = null;
  const _armIdleTimer = () => {
    if (_sseIdleTimer) clearTimeout(_sseIdleTimer);
    _sseIdleTimer = setTimeout(() => {
      _sseIdleTimer = null;
      if (app._sseAbortController) {
        try { app._sseAbortController.abort('idle-timeout'); } catch (_) {}
      }
    }, SSE_IDLE_MS);
  };

  try {
    // POST to SSE streaming endpoint — read the response stream.
    // This is the primary source of chat bubble updates.
    // WS is a secondary/backup and will skip if __sseActive is true.
    const resp = await fetch(apiPath('/api/v1/chat/stream'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
      signal: app._sseAbortController.signal,
    });
    _sseDiag.headersReceivedAt = performance.now();

    if (!resp.ok) {
      updateLastBubble('Server error: ' + resp.status, 'error');
      _sseTerminalSeen = true;
      app.isProcessing = false;
      app.chatSend.disabled = false;
      window.__sseActive = false;
      return;
    }

    // Read SSE stream for chat bubble updates
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    _armIdleTimer();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) _sseDiag.bytes += value.byteLength;
      _armIdleTimer();

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let event;
        try {
          event = JSON.parse(line.slice(6));
        } catch {
          continue;
        }
        _sseDiag.eventCount += 1;
        _sseDiag.lastEventType = event.type;
        _sseDiag.lastEventAt = performance.now();

        // Track highest session_seq the client has seen for this session.
        // Used by agentWs.js on reconnect to ask the server for replay
        // of only-newer events.
        const _sid = event.session_id || app.currentSessionId;
        if (_sid && typeof event.session_seq === 'number') {
          if (!app.lastSessionSeq) app.lastSessionSeq = {};
          const prev = app.lastSessionSeq[_sid] || 0;
          if (event.session_seq > prev) {
            app.lastSessionSeq[_sid] = event.session_seq;
            _persistLastSessionSeq();
          }
        }

        // Update bubble from SSE events
        if (event.type === 'stream') {
          if (app.agentBuffer === undefined) app.agentBuffer = '';
          app.agentBuffer += event.content;
          updateLastBubble(app.agentBuffer, 'streaming');
        } else if (event.type === 'response') {
          app.agentBuffer = '';
          updateLastBubble(event.content);
          _sseTerminalSeen = true;
          app.isProcessing = false;
          app.chatSend.disabled = false;
          if (typeof app.populateSessionSelect === 'function') {
            app.populateSessionSelect(app.currentUserId);
          }
        } else if (event.type === 'error') {
          updateLastBubble('Error: ' + event.message, 'error');
          app.agentBuffer = '';
          _sseTerminalSeen = true;
          app.isProcessing = false;
          app.chatSend.disabled = false;
        } else if (event.type === 'interrupted') {
          // Without this branch the bubble sits on `…` forever — `interrupted`
          // events are terminal but were previously dropped by this switch.
          const msg = event.message ? '(interrupted: ' + event.message + ')' : '(interrupted)';
          updateLastBubble(app.agentBuffer ? app.agentBuffer + '\n\n' + msg : msg, 'interrupted');
          app.agentBuffer = '';
          _sseTerminalSeen = true;
          app.isProcessing = false;
          app.chatSend.disabled = false;
        }
        // tool_call / tool_result / pipeline / db handled by WS -> debug panels
      }
    }

    // Reader closed cleanly but server never sent a terminal event.
    // Force-unlock the UI so the user can keep typing.
    if (!_sseTerminalSeen && app.isProcessing) {
      console.warn('[chat/stream] reader closed without terminal event', {
        bytes: _sseDiag.bytes, events: _sseDiag.eventCount,
        lastEventType: _sseDiag.lastEventType,
      });
      const stalled = app.agentBuffer
        ? app.agentBuffer + '\n\n(stream ended unexpectedly)'
        : '(stream ended unexpectedly — please retry)';
      updateLastBubble(stalled, 'error');
      app.agentBuffer = '';
      app.isProcessing = false;
      app.chatSend.disabled = false;
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      // Two ways we land here:
      //   1. The idle watchdog fired (no bytes for SSE_IDLE_MS) — surface
      //      a clear error so the user isn't stuck staring at `…`.
      //   2. abortChatStream() / a new sendMessage aborted — silent.
      const idleAbort = e.message === 'idle-timeout' || (e.reason === 'idle-timeout');
      if (idleAbort && app.isProcessing) {
        const stalled = app.agentBuffer
          ? app.agentBuffer + '\n\n(no response for ' + Math.round(SSE_IDLE_MS / 1000) + 's — please retry)'
          : '(no response for ' + Math.round(SSE_IDLE_MS / 1000) + 's — please retry)';
        updateLastBubble(stalled, 'error');
        app.agentBuffer = '';
        app.isProcessing = false;
        app.chatSend.disabled = false;
      }
      window.__sseActive = false;
      return;
    }
    const now = performance.now();
    const diag = {
      errorName: e && e.name,
      errorMessage: e && e.message,
      msSinceStart: Math.round(now - _sseDiag.startedAt),
      msSinceHeaders: _sseDiag.headersReceivedAt
        ? Math.round(now - _sseDiag.headersReceivedAt) : null,
      msSinceLastEvent: _sseDiag.lastEventAt
        ? Math.round(now - _sseDiag.lastEventAt) : null,
      bytesReceived: _sseDiag.bytes,
      eventCount: _sseDiag.eventCount,
      lastEventType: _sseDiag.lastEventType,
      online: navigator.onLine,
      visibility: document.visibilityState,
    };
    console.warn('[chat/stream] failed', diag, e);
    if (app.isProcessing) {
      updateLastBubble(
        'Request failed: ' + e.message
          + ' (after ' + diag.msSinceStart + 'ms, '
          + diag.eventCount + ' events, last='
          + (diag.lastEventType || 'none') + ')',
        'error',
      );
      app.isProcessing = false;
      app.chatSend.disabled = false;
    }
  } finally {
    if (_sseIdleTimer) {
      clearTimeout(_sseIdleTimer);
      _sseIdleTimer = null;
    }
    window.__sseActive = false;
  }
}

function openChatExpand() {
  const modal = document.getElementById('chat-expand-modal');
  const editor = document.getElementById('chat-expand-editor');
  const sendBtn = document.getElementById('chat-expand-send');
  editor.value = app.chatInput.value;
  sendBtn.disabled = !editor.value.trim();
  modal.classList.add('open');
  setTimeout(() => { editor.focus(); }, 100);
}

function closeChatExpand() {
  document.getElementById('chat-expand-modal').classList.remove('open');
}

function sendFromExpand() {
  const editor = document.getElementById('chat-expand-editor');
  const text = editor.value;
  if (!text.trim()) return;
  app.chatInput.value = text;
  closeChatExpand();
  sendMessage();
}

export function abortChatStream() {
  if (app._sseAbortController) {
    app._sseAbortController.abort();
    app._sseAbortController = null;
  }
  window.__sseActive = false;
  app.agentBuffer = '';
  app.isProcessing = false;
  if (app.chatSend) app.chatSend.disabled = false;
}

/**
 * Find the agent bubble for a given turn_id. Returns null if none exists.
 * Falls back to last agent bubble when no turn_id is supplied (legacy path).
 */
function _findAgentBubbleForTurn(turnId) {
  if (!app.chatMessages) return null;
  if (turnId) {
    return app.chatMessages.querySelector(
      `.chat-bubble.agent[data-turn-id="${CSS.escape(turnId)}"]`,
    );
  }
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  return bubbles[bubbles.length - 1] || null;
}

/**
 * Update a specific bubble's text content (preserving turn_id attribute).
 * Reuses updateLastBubble's effect but on an arbitrary bubble.
 */
function _setBubbleText(bubble, text, extraClass) {
  if (!bubble) return;
  // Keep the data-turn-id while clearing children.
  while (bubble.firstChild) bubble.removeChild(bubble.firstChild);
  bubble.appendChild(linkifyText(text));
  if (extraClass === 'streaming') {
    const stopBtn = document.createElement('button');
    stopBtn.className = 'stop-btn';
    stopBtn.textContent = '🛑';
    stopBtn.title = 'Stop generation';
    stopBtn.addEventListener('click', sendStopMessage);
    bubble.appendChild(stopBtn);
    bubble.className = 'chat-bubble agent streaming';
  } else if (extraClass) {
    bubble.className = 'chat-bubble agent ' + extraClass;
  } else {
    bubble.className = 'chat-bubble agent';
  }
  if (app.chatMessages) app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
  _addBubbleActions(bubble);
}

// Per-turn in-progress accumulator used by replayed/live WS stream chunks.
// Keyed by turn_id (so concurrent turns from event-triggered runs don't collide).
// For legacy events with no turn_id, falls back to the global app.agentBuffer.
const _wsTurnBuffers = new Map();   // turnId → accumulated content string

/**
 * Append a stream chunk into the agent bubble for this turn.
 *
 * Used by the WS path when the SSE reader isn't driving (refresh
 * mid-stream, session switch back into an in-flight run). Looks up the
 * bubble by turn_id; if none exists, creates one tagged with that turn_id.
 *
 * Idempotent for the live path. For replays, the server resends chunks
 * from the buffer — caller may pass the same chunk multiple times across
 * reconnects. We keep the latest accumulated text per turn in
 * `_wsTurnBuffers` so re-renders show the full text rather than tail.
 */
function appendStreamToActiveBubble(textChunk, turnId) {
  if (textChunk == null) return;
  let bubble = _findAgentBubbleForTurn(turnId);
  if (!bubble) {
    bubble = addChatBubble('agent', '…', 'streaming', undefined, turnId || undefined);
    if (turnId) _wsTurnBuffers.set(turnId, '');
  }
  if (turnId) {
    const cur = _wsTurnBuffers.get(turnId) || '';
    const next = cur + textChunk;
    _wsTurnBuffers.set(turnId, next);
    _setBubbleText(bubble, next, 'streaming');
  } else {
    if (app.agentBuffer === undefined) app.agentBuffer = '';
    app.agentBuffer += textChunk;
    _setBubbleText(bubble, app.agentBuffer, 'streaming');
  }
  app.isProcessing = true;
}

/**
 * Finalize the agent bubble for this turn with the full response text.
 *
 * Used by the WS path on `response` events when SSE isn't driving:
 *   - event-triggered runs (no SSE was ever started)
 *   - replayed final response after refresh / session reattach
 */
function finalizeAgentResponse(content, turnId, isReplayed) {
  let bubble = _findAgentBubbleForTurn(turnId);
  if (!bubble) {
    bubble = addChatBubble('agent', content || '', undefined, undefined, turnId || undefined);
  } else {
    _setBubbleText(bubble, content || '');
  }
  if (turnId) _wsTurnBuffers.delete(turnId);
  app.agentBuffer = '';
  app.isProcessing = false;
  if (app.chatSend) app.chatSend.disabled = false;
  if (typeof app.populateSessionSelect === 'function') {
    try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
  }
}

/**
 * On reconnect / session reattach: if the server reported this session has
 * an active turn buffered, render a placeholder streaming bubble so the
 * user sees feedback immediately. The first replayed `stream` chunk hydrates
 * it with the real (in-progress) text.
 */
function ensureStreamingBubbleForActiveTurn(turnId) {
  if (!turnId) return;
  let existing = _findAgentBubbleForTurn(turnId);
  if (existing) return;
  addChatBubble('agent', '…', 'streaming', undefined, turnId);
  app.isProcessing = true;
}

export function initChat() {
  app.addChatBubble = addChatBubble;
  app.updateLastBubble = updateLastBubble;
  app.appendStreamToActiveBubble = appendStreamToActiveBubble;
  app.finalizeAgentResponse = finalizeAgentResponse;
  app.ensureStreamingBubbleForActiveTurn = ensureStreamingBubbleForActiveTurn;

  app.chatSend.addEventListener('click', sendMessage);
  app.chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  app.chatInput.addEventListener('input', () => {
    if (!_canChat()) { app.chatSend.disabled = true; _updateInputRowState(); return; }
    app.chatSend.disabled = !app.chatInput.value.trim();
    _updateInputRowState();
  });

  // Apply gating immediately with cached value, then re-apply once mode is loaded
  applyChatGate();
  fetchAccessMode().then(applyChatGate);

  // ── Expand button ──
  document.getElementById('chat-expand-btn').addEventListener('click', openChatExpand);

  // ── Expand modal events ──
  const expandEditor = document.getElementById('chat-expand-editor');
  const expandSend = document.getElementById('chat-expand-send');
  expandSend.addEventListener('click', sendFromExpand);
  expandEditor.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      sendFromExpand();
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeChatExpand();
    }
  });
  expandEditor.addEventListener('input', () => {
    expandSend.disabled = !expandEditor.value.trim();
  });
  document.getElementById('chat-expand-close').addEventListener('click', closeChatExpand);
  document.getElementById('chat-expand-backdrop').addEventListener('click', closeChatExpand);

  // Reserve space at the bottom of the scrollable message list equal to the floating
  // input area's height so the last message clears the absolutely-positioned input.
  // Tracks the textarea as it grows with multi-line input.
  const inputArea = document.getElementById('chat-input-area');
  const messagesInner = document.getElementById('chat-messages-inner');
  if (inputArea && messagesInner && typeof ResizeObserver !== 'undefined') {
    const syncPad = () => {
      messagesInner.style.paddingBottom = inputArea.offsetHeight + 'px';
    };
    new ResizeObserver(syncPad).observe(inputArea);
    syncPad();
  }
}

export { escapeHtml };
