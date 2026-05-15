'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { addAttachmentsToMessage, renderAttachmentElement } from './attachments.js';

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function addChatBubble(role, text, extraClass, imageUrl) {
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  // Show 'You' label for user, omit for agent (already prefixed with agent name in content)
  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
  }
  bubble.appendChild(document.createTextNode(text));
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
      headers: { 'Content-Type': 'application/json' },
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
  while (last.childNodes.length > 1) last.removeChild(last.lastChild);
  last.appendChild(document.createTextNode(text));
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
  const text = app.chatInput.value.trim();
  if (!text) return;
  app.chatInput.value = '';
  app.chatSend.disabled = true;

  // Advance the poll cursor so auto-poll doesn't re-render this message
  if (window.__chatPollLastAt !== undefined) {
    window.__chatPollLastAt = new Date().toISOString();
  }

  addChatBubble('user', text);
  addChatBubble('agent', '\u2026', 'streaming');
  app.isProcessing = true;

  // Build payload
  const payload = addAttachmentsToMessage({
    message: text,
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
  });
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
      headers: { 'Content-Type': 'application/json' },
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

export function initChat() {
  app.addChatBubble = addChatBubble;
  app.updateLastBubble = updateLastBubble;

  app.chatSend.addEventListener('click', sendMessage);
  app.chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  app.chatInput.addEventListener('input', () => {
    app.chatSend.disabled = !app.chatInput.value.trim();
  });

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

  // ── Auto-fetch new messages for the current session ──
  let lastPollSessionId = null;
  let pollTimer = null;

  async function pollNewMessages() {
    if (!app.currentSessionId) return;
    if (app.isProcessing) return;

    // Detect session switch — reset cursor
    if (lastPollSessionId !== app.currentSessionId) {
      lastPollSessionId = app.currentSessionId;
      window.__chatPollLastAt = null;
    }

    try {
      let url = apiPath(`/api/v1/db/stream/interactions?db=local.db&session_id=${encodeURIComponent(app.currentSessionId)}`);
      if (window.__chatPollLastAt) {
        url += `&since=${encodeURIComponent(window.__chatPollLastAt)}`;
      } else {
        url += '&limit=100';
      }

      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      const all = data.interactions || [];
      if (all.length === 0) return;

      // On first poll, just record timestamp and bail
      if (!window.__chatPollLastAt) {
        for (const msg of all) {
          if (msg.created_at && (!window.__chatPollLastAt || msg.created_at > window.__chatPollLastAt)) {
            window.__chatPollLastAt = msg.created_at;
          }
        }
        return;
      }

      // Update timestamp and render new messages
      const toRender = [];
      for (const msg of all) {
        if (msg.created_at && msg.created_at > window.__chatPollLastAt) {
          window.__chatPollLastAt = msg.created_at;
        }
        if (msg.role === 'user' || msg.role === 'assistant') {
          toRender.push(msg);
        }
      }

      for (const msg of toRender) {
        let text = msg.content || '';
        // Strip [Tool calls: ...] suffix baked in by loop.py for DB persistence
        const toolCallIdx = text.indexOf('\n\n[Tool calls: ');
        if (toolCallIdx !== -1) {
          text = text.slice(0, toolCallIdx);
        }
        addChatBubble(msg.role === 'user' ? 'user' : 'agent', text);
      }
    } catch (e) { /* silent */ }
  }

  // Start polling (every 2 seconds)
  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollNewMessages, 2000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  // Expose for cleanup
  app._chatPollStop = stopPolling;

  // Start polling on init, stop on page unload
  startPolling();
  window.addEventListener('beforeunload', stopPolling);
}

export { escapeHtml };
