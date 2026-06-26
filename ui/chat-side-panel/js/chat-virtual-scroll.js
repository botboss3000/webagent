'use strict';

// Chat virtual scrolling — swaps off-window bubbles for height-preserving
// placeholders and materialises them on scroll (one-way: placeholder → real).
// Hooks app.addChatBubble. Module map: ui/chat-side-panel/js/README.md.

import { app } from '../../shared/js/state.js';
import { addChatBubble, linkifyText, _renderMarkdownBody } from './chat-bubble.js';
import { _addBubbleActions } from './chat-bubble-actions.js';
import { _messageCache } from './chat-message-cache.js';
import { _stripToolCalls } from './chat-stream.js';

// ── Virtual-scroll state ──────────────────────────────────────────────────
const _bubbleHeights = new Map();
// Set of msgIds currently rendered as placeholders
const _placeholderIds = new Set();
let _virtualScrollHandler = null;
const _VS_BUFFER = 400;
let _origAddChatBubble = null;

// Guard flag for concurrent scroll-triggered loads
let _loadingMoreMessages = false;

function _storeBubbleHeight(bubble) {
  const msgId = bubble.getAttribute('data-msg-id');
  if (!msgId) return;
  const h = bubble.offsetHeight;
  if (h > 0) _bubbleHeights.set(msgId, h);
}

function _getBubbleHeight(msgId) {
  const h = _bubbleHeights.get(msgId);
  return h || 80; // fallback for unmeasured bubbles
}

function _makePlaceholder(msgId, height) {
  const el = document.createElement('div');
  el.className = 'chat-bubble-placeholder';
  el.dataset.msgId = msgId;
  el.style.height = height + 'px';
  return el;
}

function _recyclePlaceholderToBubble(placeholder, msgId) {
  const sessionId = app.currentSessionId;
  if (!sessionId) return;
  const cached = _messageCache.get(sessionId);
  if (!cached) return;
  const msg = cached.messages.find(m => m.id === msgId);
  if (!msg) return;

  _placeholderIds.delete(msgId);

  const _container = app.chatMessages;
  const _phTop = placeholder.offsetTop;
  const _phHeight = placeholder.offsetHeight;
  const _scrollTop = _container ? _container.scrollTop : 0;

  const bubble = document.createElement('div');
  let role = msg.role === 'user' ? 'user' : 'agent';
  let extraClass = null;
  if (msg.role === 'assistant') {
    if (msg.status === 'streaming') extraClass = 'streaming';
    else if (msg.status === 'interrupted') extraClass = 'interrupted';
    else if (msg.status === 'error') extraClass = 'error';
  }
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (msg.id) bubble.setAttribute('data-msg-id', msg.id);

  {
    const createdAtMs = msg.created_at ? new Date(msg.created_at.replace(' ', 'T') + 'Z').getTime() : Date.now();
    bubble.setAttribute('data-created-at', createdAtMs);
  }

  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
    bubble.appendChild(linkifyText(msg.content || ''));
  } else {
    let text = msg.content || '';
    text = _stripToolCalls(text);
    if (text.trim()) {
      const body = _renderMarkdownBody(text, true);
      if (body) {
        bubble.appendChild(body);
        bubble.classList.add('md');
        bubble.__mdSource = text;
      } else {
        bubble.appendChild(linkifyText(text));
      }
    }
    if (extraClass === 'streaming') {
      const stopBtn = document.createElement('button');
      stopBtn.className = 'stop-btn';
      stopBtn.textContent = '\u23F9';
      stopBtn.addEventListener('click', () => {
        if (typeof app._sendStopMessage === 'function') app._sendStopMessage();
      });
      bubble.appendChild(stopBtn);
    }
  }

  if (typeof app._addBubbleActions === 'function') {
    try { app._addBubbleActions(bubble); } catch(_) {}
  }

  placeholder.parentNode.replaceChild(bubble, placeholder);
  _storeBubbleHeight(bubble);

  if (_container && (_phTop + _phHeight) <= _scrollTop) {
    const delta = bubble.offsetHeight - _phHeight;
    if (delta) _container.scrollTop = _scrollTop + delta;
  }
}

function _recycleVisible() {
  const container = app.chatMessages;
  if (!container) return;
  const rect = container.getBoundingClientRect();
  const scrollTop = container.scrollTop;
  const bufferTop = scrollTop - _VS_BUFFER;
  const bufferBottom = scrollTop + rect.height + _VS_BUFFER;

  const placeholders = Array.from(container.children).filter(
    c => c.classList.contains('chat-bubble-placeholder')
  );

  for (const el of placeholders) {
    const msgId = el.getAttribute('data-msg-id');
    if (!msgId) continue;

    const elTop = el.offsetTop;
    const elBottom = elTop + (parseInt(el.style.height, 10) || _getBubbleHeight(msgId));
    const isVisible = elBottom > bufferTop && elTop < bufferBottom;

    if (isVisible) _recyclePlaceholderToBubble(el, msgId);
  }
}

function _hookAddChatBubble() {
  if (_origAddChatBubble) return;
  _origAddChatBubble = app.addChatBubble;
  app.addChatBubble = function(role, text, extraClass, imageUrl, turnId, msgId) {
    const bubble = _origAddChatBubble.call(app, role, text, extraClass, imageUrl, turnId, msgId);
    requestAnimationFrame(() => _storeBubbleHeight(bubble));
    return bubble;
  };
}

function _installVirtualScroll() {
  const container = app.chatMessages;
  if (!container) return;

  _hookAddChatBubble();

  if (_virtualScrollHandler) {
    container.removeEventListener('scroll', _virtualScrollHandler);
  }

  container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));

  let _vsTimer = null;
  _virtualScrollHandler = () => {
    if (_vsTimer) cancelAnimationFrame(_vsTimer);
    _vsTimer = requestAnimationFrame(() => {
      _recycleVisible();
      if (typeof app._maybeLoadMoreOnScrollTop === 'function') {
        app._maybeLoadMoreOnScrollTop(app.currentSessionId);
      }
      if (typeof app._maybeLoadMoreOnScrollBottom === 'function') {
        app._maybeLoadMoreOnScrollBottom(app.currentSessionId);
      }
      _vsTimer = null;
    });
  };
  container.addEventListener('scroll', _virtualScrollHandler);

  _recycleVisible();
}

function _teardownVirtualScroll() {
  if (_virtualScrollHandler && app.chatMessages) {
    app.chatMessages.removeEventListener('scroll', _virtualScrollHandler);
  }
  _virtualScrollHandler = null;
  if (_origAddChatBubble) {
    app.addChatBubble = _origAddChatBubble;
    _origAddChatBubble = null;
  }
  if (app.chatMessages) {
    app.chatMessages.querySelectorAll('.chat-bubble-placeholder').forEach(ph => {
      const msgId = ph.getAttribute('data-msg-id');
      if (msgId) {
        _placeholderIds.delete(msgId);
        _recyclePlaceholderToBubble(ph, msgId);
      }
    });
  }
}

export {
  _placeholderIds,
  _storeBubbleHeight,
  _getBubbleHeight,
  _makePlaceholder,
  _installVirtualScroll,
  _teardownVirtualScroll,
};