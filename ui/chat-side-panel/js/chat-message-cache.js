'use strict';

import { app } from '../../shared/js/state.js';

// ── Message cache + infinite-scroll state ──────────────────────────────────
// Keyed by sessionId. Each entry: { messages: [...], hasMore: bool, loadedAll: bool }
const _messageCache = new Map();
const _CACHE_TTL_MS = 60000; // 60 seconds

// ── Saved focus per session (for fast windowed switching) ──────────────────
const _sessionFocus = new Map(); // sessionId -> { atBottom: bool, anchorMsgId?: string }
const _FOCUS_LS_KEY = 'sessionFocus.v1';
const _FOCUS_MAX = 50;
const _WINDOW_RADIUS = 20;
const _loadingSessions = new Set();

/** Load persisted per-session focus from localStorage into _sessionFocus. */
function _loadSessionFocus() {
  try {
    const raw = localStorage.getItem(_FOCUS_LS_KEY);
    if (!raw) return;
    const obj = JSON.parse(raw);
    if (obj && typeof obj === 'object') {
      for (const [k, v] of Object.entries(obj)) {
        if (v && typeof v === 'object') _sessionFocus.set(k, v);
      }
    }
  } catch (_) { /* corrupt/blocked storage — ignore */ }
}

/** Persist _sessionFocus to localStorage, keeping only the most-recent entries. */
function _persistSessionFocus() {
  try {
    const entries = Array.from(_sessionFocus.entries()).slice(-_FOCUS_MAX);
    _sessionFocus.clear();
    const obj = {};
    for (const [k, v] of entries) { _sessionFocus.set(k, v); obj[k] = v; }
    localStorage.setItem(_FOCUS_LS_KEY, JSON.stringify(obj));
  } catch (_) { /* quota/blocked — non-fatal */ }
}

/**
 * Record where the user is currently looking in the on-screen transcript for
 * `sessionId`. Stores either { atBottom: true } or { atBottom: false, anchorMsgId }.
 */
function _captureSessionFocus(sessionId) {
  if (!sessionId) return;
  const container = app.chatMessages;
  if (!container) return;
  const nearBottom =
    (container.scrollHeight - container.scrollTop - container.clientHeight) < 60;
  let focus = { atBottom: true };
  if (!nearBottom) {
    const st = container.scrollTop;
    let anchorMsgId = null;
    for (const el of Array.from(container.children)) {
      if (!el.classList) continue;
      const isBubble = el.classList.contains('chat-bubble');
      const isPlaceholder = el.classList.contains('chat-bubble-placeholder');
      if (!isBubble && !isPlaceholder) continue;
      const id = el.getAttribute('data-msg-id');
      if (!id) continue;
      const top = el.offsetTop;
      const h = isPlaceholder ? (parseInt(el.style.height, 10) || 0) : el.offsetHeight;
      if (top + h > st + 4) { anchorMsgId = id; break; }
    }
    if (anchorMsgId) focus = { atBottom: false, anchorMsgId };
  }
  _sessionFocus.delete(sessionId);
  _sessionFocus.set(sessionId, focus);
  _persistSessionFocus();
}

/**
 * Append or update a message in the in-memory cache for a session (called from WS paths).
 */
function _cacheAppendMessage(sessionId, msg) {
  const cached = _messageCache.get(sessionId);
  if (!cached) return;

  if (msg._streaming && msg.id) {
    const existing = cached.messages.find(m => m.id === msg.id && m.role === 'assistant');
    if (existing) {
      existing.content = (existing.content || '') + msg.content;
    } else {
      cached.messages.push({ role: 'assistant', content: msg.content, id: msg.id, status: 'streaming' });
    }
  } else if (msg._finalized && msg.id) {
    const existing = cached.messages.find(m => m.id === msg.id && m.role === 'assistant');
    if (existing) {
      existing.content = msg.content;
      delete existing.status;
    } else {
      cached.messages.push({ role: 'assistant', content: msg.content, id: msg.id });
    }
  } else {
    if (msg.id && cached.messages.some(m => m.id === msg.id)) return;
    cached.messages.push(msg);
  }

  cached.loadedAt = Date.now();
  if (typeof app.setContextFromMessages === 'function') {
    app.setContextFromMessages(cached.messages);
  }
}

export {
  _messageCache,
  _CACHE_TTL_MS,
  _sessionFocus,
  _loadSessionFocus,
  _persistSessionFocus,
  _captureSessionFocus,
  _cacheAppendMessage,
};