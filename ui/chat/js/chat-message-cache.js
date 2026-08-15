'use strict';

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { storageAdapter } from './storage/storage-adapter.js';

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
  const scroller = app._chatScroller || container.parentElement;
  if (!scroller) return;
  const nearBottom =
    (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight) < 60;
  let focus = { atBottom: true };
  if (!nearBottom) {
    const st = scroller.scrollTop;
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
      if (typeof msg.session_seq === 'number') existing.session_seq = msg.session_seq;
      if (msg.created_at && !existing.created_at) existing.created_at = msg.created_at;
    } else {
      cached.messages.push({
        role: 'assistant', content: msg.content, id: msg.id,
        created_at: msg.created_at, status: 'streaming',
      });
    }
  } else if (msg._finalized && msg.id) {
    const existing = cached.messages.find(m => m.id === msg.id && m.role === 'assistant');
    if (existing) {
      existing.content = msg.content;
      if (typeof msg.session_seq === 'number') existing.session_seq = msg.session_seq;
      if (msg.created_at && !existing.created_at) existing.created_at = msg.created_at;
      delete existing.status;
    } else {
      cached.messages.push({
        role: 'assistant', content: msg.content, id: msg.id,
        created_at: msg.created_at,
      });
    }
  } else {
    const existing = msg.id ? cached.messages.find(m => m.id === msg.id) : null;
    if (existing) Object.assign(existing, msg);
    else cached.messages.push(msg);
  }

  // Live WS events and DB-tail polls can interleave. Keep the cache in the same
  // authoritative order as a cold transcript load instead of preserving arrival
  // order. Legacy rows without a sequence predate sequenced rows.
  if (typeof msg.session_seq === 'number') _sortMessagesCanonical(cached.messages);

  if (typeof msg.session_seq === 'number') {
    cached.maxSeq = Math.max(Number(cached.maxSeq || 0), msg.session_seq);
  }

  cached.loadedAt = Date.now();
  if (typeof app.setContextFromMessages === 'function') {
    app.setContextFromMessages(cached.messages);
  }
}

app._cacheAppendMessage = _cacheAppendMessage;

function _sortMessagesCanonical(messages) {
  if (!Array.isArray(messages)) return messages;
  const decorated = messages.map((message, index) => ({ message, index }));
  const compareTime = (a, b) => {
    const at = String(a.message.created_at || '');
    const bt = String(b.message.created_at || '');
    if (at !== bt) return at < bt ? -1 : 1;
    return a.index - b.index;
  };
  const sequenced = decorated.filter(item =>
    Number.isFinite(item.message.session_seq),
  ).sort((a, b) => {
    const as = Number.isFinite(a.message.session_seq) ? a.message.session_seq : null;
    const bs = Number.isFinite(b.message.session_seq) ? b.message.session_seq : null;
    if (as !== null && bs !== null && as !== bs) return as - bs;
    return compareTime(a, b);
  });
  const provisional = decorated.filter(item =>
    !Number.isFinite(item.message.session_seq),
  ).sort(compareTime);
  // Missing legacy sequences are timestamp-positioned into the immutable
  // sequenced ledger; they never override the relative order of durable rows.
  for (const item of provisional) {
    const itemTime = String(item.message.created_at || '');
    const at = itemTime
      ? sequenced.findIndex(saved =>
        String(saved.message.created_at || '') > itemTime)
      : -1;
    sequenced.splice(at === -1 ? sequenced.length : at, 0, item);
  }
  messages.splice(0, messages.length, ...sequenced.map(x => x.message));
  return messages;
}

// ── Transcript manifests — remote-change detection ─────────────────────────
// The server maintains a per-session transcript manifest (authority_revision +
// content_hash, see app/db/session_manifest.py) and validates it against the
// browser's last-known values via /session-messages?manifest_only=1
// (returns `not_modified: true` when identical). Every refresh path consults
// this FIRST: if the server says nothing changed, the caller MUST NOT touch
// the screen — no cache drop, no teardown, no re-render. The rubber-band pull
// animation is driven by applyRubberBand and is unaffected by the skip.
const _sessionManifest = new Map(); // sessionId -> { revision, hash }
const _MANIFEST_LS_KEY = 'sessionManifest.v1';
const _MANIFEST_MAX = 50;
let _manifestsLoaded = false;

function _loadSessionManifests() {
  if (_manifestsLoaded) return;
  _manifestsLoaded = true;
  try {
    const raw = localStorage.getItem(_MANIFEST_LS_KEY);
    if (!raw) return;
    const obj = JSON.parse(raw);
    if (obj && typeof obj === 'object') {
      for (const [k, v] of Object.entries(obj)) {
        if (v && typeof v === 'object' && typeof v.revision === 'number' && typeof v.hash === 'string') {
          _sessionManifest.set(k, { revision: v.revision, hash: v.hash });
        }
      }
    }
  } catch (_) { /* corrupt/blocked storage — ignore */ }
}

function _persistSessionManifests() {
  try {
    const entries = Array.from(_sessionManifest.entries()).slice(-_MANIFEST_MAX);
    _sessionManifest.clear();
    const obj = {};
    for (const [k, v] of entries) { _sessionManifest.set(k, v); obj[k] = v; }
    localStorage.setItem(_MANIFEST_LS_KEY, JSON.stringify(obj));
  } catch (_) { /* quota/blocked — non-fatal */ }
}

/** Remember the server's manifest for a session after any fetch/validation. */
function _rememberSessionManifest(sessionId, manifest) {
  if (!sessionId || !manifest) return;
  const rev = Number(manifest.authority_revision);
  const hash = String(manifest.content_hash || '');
  if (!Number.isFinite(rev) || !hash) return;
  _loadSessionManifests();
  _sessionManifest.set(sessionId, { revision: rev, hash });
  _persistSessionManifests();
}

/**
 * Ask the REMOTE server whether the session transcript changed since the last
 * render. Returns:
 *   false — the server explicitly says NOT modified → the caller must do
 *           nothing to the screen.
 *   true  — changed, unknown (no manifest recorded yet), or the check failed
 *           (network/parse/browser-only mode) → the caller should refresh.
 * Only an explicit `not_modified: true` skips the refresh; every uncertain
 * path stays conservative and refreshes, preserving today's behavior.
 */
async function _transcriptChangedRemotely(sessionId) {
  if (!sessionId) return true;
  _loadSessionManifests();
  const known = _sessionManifest.get(sessionId);
  if (!known) return true; // never validated — a refresh is the first sync
  if (storageAdapter.isBrowser) return true; // no remote authority — refresh locally
  try {
    const token = localStorage.getItem('auth_token') || '';
    let url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sessionId)}&limit=1&manifest_only=true&known_revision=${known.revision}&known_hash=${encodeURIComponent(known.hash)}`);
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(url);
    if (!res.ok) return true;
    const data = await res.json();
    // Always record whatever the server reports so the next check starts from
    // the new baseline (keeps the store in sync with server truth).
    if (data.manifest) _rememberSessionManifest(sessionId, data.manifest);
    return data.not_modified !== true;
  } catch (_) {
    return true; // network/parse failure — conservative: proceed with refresh
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
  _sortMessagesCanonical,
  _rememberSessionManifest,
  _transcriptChangedRemotely,
};
