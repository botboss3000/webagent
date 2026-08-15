'use strict';

// Chat virtual scrolling — swaps off-window bubbles for height-preserving
// placeholders and materialises only the window around the viewport.
// Hooks app.addChatBubble. Module map: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import {
  addChatBubble,
  linkifyText,
  _renderMarkdownBody,
  _setBubbleCreatedAt,
  _setBubbleSessionSeq,
} from './chat-bubble.js';
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
  const _scroller = app._chatScroller || (_container && _container.parentElement);
  const _scrollTop = _scroller ? _scroller.scrollTop : 0;

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

  _setBubbleCreatedAt(bubble, msg.created_at);
  _setBubbleSessionSeq(bubble, msg.session_seq);

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

  // Persisted tool bodies are deliberately absent from the light transcript.
  // Restore just this bubble's lightweight headings when it enters the window;
  // expanding a heading still performs the existing lazy detail fetch.
  if (typeof app._rehydrateVirtualBubble === 'function') {
    try { app._rehydrateVirtualBubble(msgId, bubble); } catch (_) {}
  }

  if (_container && (_phTop + _phHeight) <= _scrollTop) {
    const delta = bubble.offsetHeight - _phHeight;
    if (delta) {
      if (_scroller) _scroller.scrollTop = _scrollTop + delta;
    }
  }
}

function _recycleBubbleToPlaceholder(bubble) {
  const msgId = bubble.getAttribute('data-msg-id');
  if (!msgId || !bubble.parentNode) return false;

  // Never evict mutable or interactive state. Finalized transcript bubbles are
  // reproducible from the message cache; a live stream, focused control, open
  // menu, or expanded details panel is not.
  if (bubble.classList.contains('streaming')
      || bubble.classList.contains('session-placeholder')
      || bubble.contains(document.activeElement)
      || bubble.querySelector('details[open], [aria-expanded="true"], .is-open, .open')) {
    return false;
  }

  const sessionId = app.currentSessionId;
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  if (!cached || !cached.messages.some(m => m.id === msgId)) return false;

  _storeBubbleHeight(bubble);
  const placeholder = _makePlaceholder(msgId, _getBubbleHeight(msgId));
  const sessionSeq = bubble.getAttribute('data-session-seq');
  if (sessionSeq != null) placeholder.setAttribute('data-session-seq', sessionSeq);
  bubble.parentNode.replaceChild(placeholder, bubble);
  _placeholderIds.add(msgId);
  return true;
}

function _recycleVisible() {
  const container = app.chatMessages;
  if (!container) return;
  const scroller = app._chatScroller || container.parentElement;
  const rect = scroller.getBoundingClientRect();
  const scrollTop = scroller.scrollTop;
  const bufferTop = scrollTop - _VS_BUFFER;
  const bufferBottom = scrollTop + rect.height + _VS_BUFFER;

  // Evict finalized bubbles that have left the buffered viewport. Without this
  // pass, walking through a long session permanently accumulated every parsed
  // Markdown tree in the DOM even though the initial load was windowed.
  const bubbles = Array.from(container.children).filter(
    c => c.classList.contains('chat-bubble')
  );
  for (const el of bubbles) {
    const elTop = el.offsetTop;
    const elBottom = elTop + el.offsetHeight;
    if (elBottom <= bufferTop || elTop >= bufferBottom) {
      _recycleBubbleToPlaceholder(el);
    }
  }

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
  app.addChatBubble = function(role, text, extraClass, imageUrl, turnId, msgId, createdAt) {
    const bubble = _origAddChatBubble.call(app, role, text, extraClass, imageUrl, turnId, msgId, createdAt);
    requestAnimationFrame(() => _storeBubbleHeight(bubble));
    return bubble;
  };
}

function _installVirtualScroll() {
  const container = app.chatMessages;
  if (!container) return;
  // #chat-messages-inner is the content; #chat-messages is the scrollable parent.
  const scroller = container.parentElement;
  if (!scroller) return;

  _hookAddChatBubble();

  if (_virtualScrollHandler) {
    scroller.removeEventListener('scroll', _virtualScrollHandler);
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
  scroller.addEventListener('scroll', _virtualScrollHandler, { passive: true });

  // Direct (non-debounced) scroll listener — fires on every scroll event so
  // the hard-boundary check catches the moment the user hits the very top or
  // bottom, before browser momentum/overscroll can shift scrollTop away.
  let _hbtListener = () => {
    if (typeof app._checkHardScrollBoundary === 'function') {
      app._checkHardScrollBoundary(app.currentSessionId);
    }
  };
  scroller.addEventListener('scroll', _hbtListener, { passive: true });
  // Store the scrollable parent so other modules can read scrollTop correctly.
  // app.chatMessages (#chat-messages-inner) is the content; its parent
  // (#chat-messages) is the element that actually scrolls.
  app._chatScroller = scroller;
  // Stash for teardown
  container._hbtListener = _hbtListener;

  _recycleVisible();
}

function _teardownVirtualScroll() {
  const container = app.chatMessages;
  const scroller = app._chatScroller || (container && container.parentElement);
  if (_virtualScrollHandler && scroller) {
    scroller.removeEventListener('scroll', _virtualScrollHandler);
  }
  if (scroller && container && container._hbtListener) {
    scroller.removeEventListener('scroll', container._hbtListener);
    container._hbtListener = null;
  }
  _virtualScrollHandler = null;
  if (_origAddChatBubble) {
    app.addChatBubble = _origAddChatBubble;
    _origAddChatBubble = null;
  }
  // Every caller tears down immediately before replacing or hiding this
  // transcript. Do not materialize the entire virtualized history just to throw
  // it away; that was an O(session-size) Markdown parse on every session switch.
  _placeholderIds.clear();
}

export {
  _placeholderIds,
  _storeBubbleHeight,
  _getBubbleHeight,
  _makePlaceholder,
  _installVirtualScroll,
  _teardownVirtualScroll,
};
