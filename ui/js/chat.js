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
  return bubble;
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
  app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
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

    if (!resp.ok) {
      updateLastBubble('Server error: ' + resp.status, 'error');
      app.isProcessing = false;
      app.chatSend.disabled = false;
      window.__sseActive = false;
      return;
    }

    // Read SSE stream for chat bubble updates
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

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

        // Track highest session_seq the client has seen for this session.
        // Used by agentWs.js on reconnect to ask the server for replay
        // of only-newer events.
        const _sid = event.session_id || app.currentSessionId;
        if (_sid && typeof event.session_seq === 'number') {
          if (!app.lastSessionSeq) app.lastSessionSeq = {};
          const prev = app.lastSessionSeq[_sid] || 0;
          if (event.session_seq > prev) {
            app.lastSessionSeq[_sid] = event.session_seq;
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
          app.isProcessing = false;
          app.chatSend.disabled = false;
          if (typeof app.populateSessionSelect === 'function') {
            app.populateSessionSelect(app.currentUserId);
          }
        } else if (event.type === 'error') {
          updateLastBubble('Error: ' + event.message, 'error');
          app.agentBuffer = '';
          app.isProcessing = false;
          app.chatSend.disabled = false;
        }
        // tool_call / tool_result / pipeline / db handled by WS -> debug panels
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      window.__sseActive = false;
      return;
    }
    if (app.isProcessing) {
      updateLastBubble('Request failed: ' + e.message, 'error');
      app.isProcessing = false;
      app.chatSend.disabled = false;
    }
  } finally {
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
