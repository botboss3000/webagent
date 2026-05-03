'use strict';

import { app } from './state.js';
import { connectAgent } from './agentWs.js';

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function addChatBubble(role, text, extraClass, imageUrl) {
  const bubble = document.createElement('div');
  bubble.className =
    'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = role === 'user' ? 'You' : 'Assistant';
  bubble.appendChild(label);
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
  app.chatMessages.appendChild(bubble);
  app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
  return bubble;
}

function updateLastBubble(text, extraClass, imageUrl) {
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  const last = bubbles[bubbles.length - 1];
  if (last) {
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
    if (extraClass) last.className = 'chat-bubble agent ' + extraClass;
    app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
  }
}

function sendMessage() {
  const text = app.chatInput.value.trim();
  if (!text) return;
  app.chatInput.value = '';
  app.chatSend.disabled = true;

  addChatBubble('user', text);

  if (!app.isProcessing) {
    addChatBubble('agent', '…', 'streaming');
    app.isProcessing = true;
    app.chatSend.disabled = true;
  }

  const msg = JSON.stringify({
    message: text,
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
  });

  if (app.agentWs && app.agentWs.readyState === WebSocket.OPEN) {
    app.agentWs.send(msg);
  } else {
    updateLastBubble('Agent disconnected. Reconnecting...', 'error');
    app.isProcessing = false;
    app.chatSend.disabled = false;
    connectAgent();
  }
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

  app.chatClear.addEventListener('click', () => {
    app.chatMessages.innerHTML = '';
    addChatBubble('agent', 'Conversation cleared. Ask me anything.');
  });
}

export { escapeHtml };
