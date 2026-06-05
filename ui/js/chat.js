'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { addAttachmentsToMessage, renderAttachmentElement } from './attachments.js';
import { getAccessMode, fetchAccessMode, authHeaders } from './left-login.js';
import { _cacheAppendMessage } from './sessions.js';
import { fmtArgs, buildToolRow } from './chat-activity.js';

/** Whether the user has explicitly locked auto-scroll (by clicking the
 *  scroll-to-bottom chevron or by being at the bottom). When true, new
 *  content auto-scrolls into view. Set to false when the user scrolls
 *  away from the bottom. */
let _scrollLocked = true;

// ── Debounced helpers to avoid per-keystroke layout / I/O thrash ──
let _draftTimer = null;
let _resizeTimer = null;

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

function _debouncedAutoResizePill(el) {
  if (_resizeTimer) clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => {
    _resizeTimer = null;
    _autoResizePill(el);
  }, 100);
}

/** The scroll-to-bottom chevron button, cached after init. */
let _scrollBtn = null;

/** Set to true while _scrollToBottomIfNear is doing a programmatic scroll,
 *  so the scroll event listener knows not to release the lock. */
let _programmaticScroll = false;

/** Show/hide the scroll-to-bottom chevron based on scroll position.
 *  Releases the scroll lock only on genuine user-initiated scrolls
 *  (not programmatic scrolls from _scrollToBottomIfNear). */
function _updateScrollChevron(el) {
  if (!el || !_scrollBtn) return;
  // If this scroll was triggered by our own programmatic scroll, don't
  // release the lock — just update the chevron and bail.
  if (_programmaticScroll) {
    _programmaticScroll = false;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    _scrollBtn.classList.toggle('visible', !atBottom);
    return;
  }
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  _scrollBtn.classList.toggle('visible', !atBottom);
  if (!atBottom) _scrollLocked = false;
}

/** Scroll to bottom only if the user has locked auto-scroll (clicked the
 *  chevron or is at the bottom). Forces a synchronous reflow (via
 *  offsetHeight) so scrollHeight reflects the latest DOM changes — without
 *  this, new content appended/replaced in the same tick isn't laid out yet
 *  and scrollTop = scrollHeight becomes a no-op. */
function _scrollToBottomIfNear(el) {
  if (!el) return;
  if (!_scrollLocked) {
    // Still update the chevron visibility in case the user scrolled back down
    _updateScrollChevron(el);
    return;
  }
  // Force reflow so scrollHeight includes any content just added/replaced.
  const _ = el.offsetHeight;
  _programmaticScroll = true;
  el.scrollTop = el.scrollHeight;
  // Chevron should be hidden when we're at the bottom
  if (_scrollBtn) _scrollBtn.classList.remove('visible');
}

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

// CHAT-PILL-SYNC: auto-grow any .chat-pill-input textarea between the CSS
// min-height (2 rows = 52px) and max-height (6 rows = 124px). Both live in
// ui/css/app1.css; we read them from getComputedStyle here so a single
// design-tweak in CSS is enough — no JS constant to update in lockstep.
function _autoResizePill(el) {
  if (!el || el.tagName !== 'TEXTAREA') return;
  // Reset to 'auto' so scrollHeight reflects the *current* content height
  // rather than the previously-set inline height (scrollHeight never shrinks
  // past the assigned height otherwise).
  el.style.height = 'auto';
  const cs = getComputedStyle(el);
  const minH = parseFloat(cs.minHeight) || 0;
  const maxH = parseFloat(cs.maxHeight) || 124;
  const next = Math.max(minH, Math.min(el.scrollHeight, maxH));
  el.style.height = next + 'px';
  // overflowY is still toggled even though the native scrollbar is hidden in
  // CSS — keyboard/wheel scrolling needs overflow:auto to function past the
  // 6-row cap.
  el.style.overflowY = el.scrollHeight > maxH ? 'auto' : 'hidden';
  _updateScrollIndicator(el);
}

// Drive the tiny vertical scroll-position dot on the left edge of every
// chat pill. The dot lives in CSS as .chat-pill::before and reads two
// custom properties from the pill itself:
//   --scroll-pct        — 0..1, where in the overflow the textarea is
//   --indicator-opacity — 0 when content fits, 1 when it overflows
// Both are set here off the textarea's scrollTop/scrollHeight; CSS does
// the actual positioning + fade.
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

// Delegated input listener resizes every chat-pill textarea (web chat, agents
// builder, page agent, integration admin) without each module having to wire
// its own handler. Uses a debounced call to avoid per-keystroke layout thrash.
// Programmatic value changes still need to dispatch 'input' or call
// _autoResizePill directly — see _restoreDraft / sendMessage below.
document.addEventListener('input', (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains('chat-pill-input')) {
    _debouncedAutoResizePill(t);
  }
}, true);

// Scroll events don't bubble, so the delegated listener has to use capture.
// Update the indicator dot whenever any pill textarea is scrolled (wheel,
// keyboard, drag, programmatic).
document.addEventListener('scroll', (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains('chat-pill-input')) {
    _updateScrollIndicator(t);
  }
}, true);

// Run once on load so any pre-populated value (draft restore, server-injected
// text) sizes correctly before the user types. Also re-runs on window resize
// because the textarea's content width — and therefore wrap count — shifts.
function _resizeAllPills() {
  document.querySelectorAll('.chat-pill-input').forEach(_autoResizePill);
}
window.addEventListener('load', _resizeAllPills);
window.addEventListener('resize', _resizeAllPills);

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

// ── Markdown rendering for agent messages ──────────────────────────
// Agent replies are written in Markdown (headings, tables, lists, fenced
// code, bold, …). We render them to HTML with `marked`, then SANITIZE the
// result with DOMPurify before inserting it: agent output can echo back
// untrusted text pulled in from the web, email, or tool results, so
// injecting raw HTML would be a genuine XSS vector. If either library is
// unavailable we fall back to plain linkified text — never unsanitized
// HTML. User messages are left literal (linkifyText) on purpose.
function _markdownReady() {
  return !!(window.marked && typeof window.marked.parse === 'function'
         && window.DOMPurify && typeof window.DOMPurify.sanitize === 'function');
}

function _highlightCodeBlocks(root) {
  try {
    if (window.Prism && typeof window.Prism.highlightAllUnder === 'function') {
      window.Prism.highlightAllUnder(root);
    }
  } catch (_) { /* syntax highlighting is best-effort */ }
}

// Build a `<div class="md-body">` of rendered + sanitized markdown, or
// return null when the libraries are missing / there's nothing to render
// (the caller then falls back to linkifyText). Pass highlight=false while a
// message is still streaming so we don't re-tokenize partial code fences on
// every chunk — finalize re-renders with highlighting on.
function _renderMarkdownBody(text, highlight) {
  if (!text || !text.trim() || !_markdownReady()) return null;
  let html;
  try {
    html = window.marked.parse(text, { gfm: true, breaks: true });
  } catch (_) { return null; }
  const body = document.createElement('div');
  body.className = 'md-body';
  body.innerHTML = window.DOMPurify.sanitize(html, { FORBID_ATTR: ['style'] });
  // Open links in a new tab and defuse reverse-tabnabbing.
  body.querySelectorAll('a[href]').forEach((a) => {
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  });
  if (highlight !== false) _highlightCodeBlocks(body);
  return body;
}

// Fill an agent bubble's content: rendered markdown when possible, else
// plain linkified text. Tags the bubble `.md` and stashes the raw markdown
// source on it (so Copy yields the verbatim source, not flattened text).
// Returns true when markdown was rendered.
function _fillAgentBubble(bubble, text, highlight) {
  const body = _renderMarkdownBody(text, highlight);
  if (body) {
    bubble.appendChild(body);
    bubble.classList.add('md');
    bubble.__mdSource = text;
    return true;
  }
  bubble.appendChild(linkifyText(text || ''));
  bubble.classList.remove('md');
  bubble.__mdSource = null;
  return false;
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

// ── Outgoing message queue (outbox) ─────────────────────────────────
// Holds messages the user has sent but that haven't been confirmed by the
// server yet (network error / server reloading). Survives page refresh.
// Key invariant: NEVER render a bubble from the retry path — the WebSocket
// replay (+ dedup) is the only source of truth for what appears in chat.
// The outbox only adds pending-style bubbles when the user is sitting on
// the page during a failure, and removes them on retry success.
const _OUTBOX_LS_KEY = 'webagent.pendingMessages.v1';
let _outboxIdCounter = 0;

function _readOutbox() {
  try {
    const raw = localStorage.getItem(_OUTBOX_LS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (_) { return []; }
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

// Silently retry a single pending message against the server.
// NEVER renders bubbles on its own — converts existing pending DOM bubbles
// back to normal on success, or removes them if WS already caught up.
// Returns true if the server accepted it (entry removed from outbox).
async function _retryEntry(entry) {
  try {
    // Dedup: if a normal (non-pending) user bubble with this text already
    // exists in the DOM, the WS replay already caught it — just clean up
    // the outbox entry and remove any stale pending bubble.
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
      execution_mode: app.executionMode || 'write',
    };
    if (entry.agent_id || app.currentAgentId) {
      payload.agent_id = entry.agent_id || app.currentAgentId;
    }
    const resp = await fetch(apiPath('/api/v1/chat/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
    });
    if (resp.ok) {
      _removeFromOutbox(entry.id);
      const data = await resp.json().catch(() => ({}));
      // Convert any pending DOM bubble back to a normal user bubble
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
  } catch (_) { /* server still down */ }
  return false;
}

// Retry ALL pending messages silently. Returns count of successes.
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

// Render a single pending bubble — only called from sendMessage's catch
// (i.e. the user is on the page right now and the send just failed).
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
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try { window.lucide.createIcons({ nodes: Array.from(bubble.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch (_) {}
  }
  if (app.chatMessages) {
    app.chatMessages.appendChild(bubble);
    _scrollToBottomIfNear(app.chatMessages);
  }
  return bubble;
}

// Convert an existing normal user bubble INTO a pending bubble in-place.
// This avoids the "two bubbles" problem (one normal + one pending).
function _convertBubbleToPending(bubble, entry) {
  // Label: change "You" to "You (pending)"
  let label = bubble.querySelector('.label');
  if (!label) {
    label = document.createElement('span');
    label.className = 'label';
    bubble.insertBefore(label, bubble.firstChild);
  }
  label.textContent = 'You (pending)';
  // Class
  bubble.className = 'chat-bubble user pending';
  bubble.setAttribute('data-pending-id', entry.id);
  // Remove any existing actions so we re-append fresh ones
  bubble.querySelectorAll('.bubble-actions').forEach(el => el.remove());
  // Remove any existing data-msg-id so WS dedup won't skip it but the
  // outbox retry path won't create a duplicate.
  bubble.removeAttribute('data-msg-id');
  bubble.removeAttribute('data-turn-id');
  _appendPendingActions(bubble, entry);
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try { window.lucide.createIcons({ nodes: Array.from(bubble.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch (_) {}
  }
}

// Shared: appends retry + dismiss buttons to a pending bubble
function _appendPendingActions(bubble, entry) {
  const actions = document.createElement('div');
  actions.className = 'bubble-actions pending-actions';
  // Retry button
  const retryBtn = document.createElement('button');
  retryBtn.type = 'button';
  retryBtn.className = 'bubble-action-btn pending-retry';
  retryBtn.innerHTML = '<i data-lucide="refresh-cw" style="width:14px;height:14px;"></i>';
  retryBtn.title = 'Retry sending';
  retryBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    retryBtn.disabled = true;
    retryBtn.innerHTML = '<span style="font-size:12px;">↻</span>';
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
  // Dismiss button
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
  bubble.appendChild(actions);
}

// Periodic auto-retry — polls every 5s while the queue is non-empty.
let _outboxPollTimer = null;
function _startOutboxPoll() {
  if (_outboxPollTimer) return;
  _outboxPollTimer = setInterval(async () => {
    const n = await _flushOutbox();
    if (n > 0) {
      // Re-render remaining pending bubbles if any are still stuck
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

// Re-render any pending outbox entries as bubbles (used on init when the
// server is still down and the flush couldn't deliver them).
function _renderPendingBubbles() {
  document.querySelectorAll('.chat-bubble.user.pending').forEach(el => el.remove());
  const queue = _readOutbox();
  for (const entry of queue) {
    _renderPendingBubble(entry);
  }
}

// ── chat draft persistence ──
// Keep whatever the user has typed (but not yet sent) in the chat pill so a
// page refresh doesn't lose it. Stored as plain text in localStorage; this
// mirrors the in-memory pill, which is already global across session switches.
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
    if (app.chatInput.value) return;   // don't clobber anything already present
    if (!_canChat()) return;           // locked apps keep the pill empty
    app.chatInput.value = v;
    _updateInputRowState();
    if (app.chatSend) app.chatSend.disabled = !v.trim();
    if (app.autoResizeChatInput) app.autoResizeChatInput();
  } catch (_) { /* non-fatal */ }
}

function addChatBubble(role, text, extraClass, imageUrl, turnId, msgId) {
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (turnId) bubble.setAttribute('data-turn-id', turnId);
  // Stable per-interaction id — used to dedup the same message arriving from
  // multiple sources (local render + WS broadcast + DB reload) so a message
  // never appears twice. Critical for live multi-device viewing.
  if (msgId) bubble.setAttribute('data-msg-id', msgId);
  // Show 'You' label for user, omit for agent (already prefixed with agent name in content)
  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
  }
  if (role === 'agent' && extraClass !== 'error') {
    _fillAgentBubble(bubble, text, extraClass !== 'streaming');
  } else {
    const body = document.createElement('div');
    body.className = 'bubble-body';
    body.appendChild(linkifyText(text));
    bubble.appendChild(body);
  }
  if (imageUrl) {
    const img = document.createElement('img');
    img.src = imageUrl;
    img.style.maxWidth = '100%';
    img.style.maxHeight = '400px';
    img.style.borderRadius = '8px';
    img.style.marginTop = '8px';
    img.style.border = '1px solid #444';
    // Attach image to the body container if it exists, otherwise to bubble
    const target = bubble.querySelector(':scope > .bubble-body, :scope > .md-body') || bubble;
    target.appendChild(img);
  }
  if (role === 'agent' && window.__streamAttachments && extraClass === 'has-attachments') {
    for (const att of window.__streamAttachments) {
      const el = renderAttachmentElement(att);
      if (el) bubble.appendChild(el);
    }
    window.__streamAttachments = null;
  }
  app.chatMessages.appendChild(bubble);
  _scrollToBottomIfNear(app.chatMessages);
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

// Text to put on the clipboard when Copy is pressed. For markdown-rendered
// agent bubbles, prefer the raw markdown source (tables, code fences, etc.
// stay intact) over the flattened visible text. Read-aloud still uses the
// flattened _getBubbleText so it doesn't speak the markdown punctuation.
function _getBubbleCopyText(bubble) {
  if (bubble && typeof bubble.__mdSource === 'string' && bubble.__mdSource.trim()) {
    return bubble.__mdSource;
  }
  return _getBubbleText(bubble);
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
  const text = _getBubbleCopyText(bubble);
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

// ── Per-turn delete (two-click confirm, mirrors the session-dropdown UX) ──
// Clicking the trash button on ANY bubble deletes the WHOLE turn that bubble
// belongs to — the user message plus every agent step, tool call and memory
// write that descended from it — which strips that turn from the history the
// agent rebuilds each turn (i.e. prunes its context). First click arms the
// button (trash → ⚠️); a second click commits. The turn is resolved server-side
// from the bubble's interaction id by walking the parent chain to the root, so
// the frontend only needs any one id from the turn.
function _renderActionIcons(container) {
  if (container && window.lucide && typeof window.lucide.createIcons === 'function') {
    try {
      window.lucide.createIcons({
        nodes: Array.from(container.querySelectorAll('[data-lucide]:not(.lucide)')),
      });
    } catch (_) {}
  }
}

// Either id is a real interaction row id the backend can resolve to a turn:
// data-msg-id on DB-reloaded bubbles + live user bubbles; data-turn-id on live
// agent step/response bubbles (it holds the assistant row's id).
function _bubbleAnchorId(bubble) {
  return bubble && (bubble.getAttribute('data-msg-id') || bubble.getAttribute('data-turn-id'));
}

// Lucide replaces the <i data-lucide> placeholder with an <svg>, so the shared
// _setActionIcon (which looks for an <i>) can't re-swap an already-rendered
// button. Reset innerHTML to a fresh placeholder and re-render instead.
function _setDeleteIcon(btn, name) {
  btn.innerHTML = '<i data-lucide="' + name + '" style="width:14px;height:14px;"></i>';
  _renderActionIcons(btn);
}

function _resetBubbleDeleteBtn(btn) {
  if (!btn) return;
  btn.dataset.state = 'trash';
  btn.classList.remove('warning');
  btn.title = 'Delete this turn';
  _setDeleteIcon(btn, 'trash-2');
}

function _resetAllBubbleDeleteBtns(except) {
  document.querySelectorAll('.bubble-delete-btn[data-state="warning"]').forEach((b) => {
    if (b !== except) _resetBubbleDeleteBtn(b);
  });
}

function _handleBubbleDeleteClick(btn, bubble) {
  if (btn.dataset.state === 'warning') {
    btn.dataset.state = 'deleting';
    _deleteTurn(bubble, btn);
    return;
  }
  // First click: arm this button, disarm any other pending one.
  _resetAllBubbleDeleteBtns(btn);
  btn.dataset.state = 'warning';
  btn.classList.add('warning');
  btn.title = 'Click again to delete this message and its whole turn';
  _setDeleteIcon(btn, 'alert-triangle');
}

async function _deleteTurn(bubble, btn) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) { _resetBubbleDeleteBtn(btn); return; }   // not persisted yet — no-op
  const sid = app.currentSessionId;
  // Pass the active client identity so the backend can authorize tokenless local
  // users (and TUI/launcher-created sessions) by user_id, the same way the
  // session list does — without it, those deletes 403 and the button silently
  // resets. The session's owner/participant list is still the real gate.
  const uid = app.currentUserId || '';
  const url = apiPath('/api/v1/db/turn?session_id=' + encodeURIComponent(sid)
    + '&interaction_id=' + encodeURIComponent(anchor)
    + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
    + '&db=local.db');
  try {
    const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !Array.isArray(data.deleted_ids)) {
      console.warn('Delete turn failed:', resp.status, data);
      _resetBubbleDeleteBtn(btn);
      // Don't fail silently — the click registered but the server rejected it.
      if (resp.status === 404 || resp.status === 405) {
        alert('Couldn’t delete this turn: the server doesn’t have this feature yet. Restart the app server and try again.');
      } else {
        alert('Couldn’t delete this turn (server responded ' + resp.status + ').');
      }
      return;
    }
    // Remove every bubble of the turn. Bubbles carry the deleted id in either
    // data-msg-id (reloaded / user) or data-turn-id (live agent steps).
    const ids = new Set(data.deleted_ids.map(String));
    app.chatMessages.querySelectorAll('.chat-bubble').forEach((b) => {
      const mid = b.getAttribute('data-msg-id');
      const tid = b.getAttribute('data-turn-id');
      if ((mid && ids.has(String(mid))) || (tid && ids.has(String(tid)))) b.remove();
    });
    // Defensive: the clicked bubble itself, in case it lacked a matching id.
    if (bubble && bubble.isConnected) bubble.remove();
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
  } catch (e) {
    console.warn('Delete turn error:', e);
    _resetBubbleDeleteBtn(btn);
    alert('Couldn’t delete this turn — no response from the server. Is the app server running?');
  }
}

function _makeBubbleDeleteBtn(bubble) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-delete-btn';
  btn.dataset.state = 'trash';
  btn.title = 'Delete this turn';
  btn.innerHTML = '<i data-lucide="trash-2" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _handleBubbleDeleteClick(btn, bubble);
  });
  return btn;
}

// Clicking anywhere that isn't an armed delete button disarms them all, so a
// half-confirmed delete never lingers (same intent as the session dropdown
// resetting its buttons when the menu opens/closes).
document.addEventListener('click', (e) => {
  if (!e.target.closest || !e.target.closest('.bubble-delete-btn')) {
    _resetAllBubbleDeleteBtns(null);
  }
}, true);

function _toggleBubbleCollapse(btn, bubble) {
  const isCollapsed = bubble.classList.toggle('collapsed');
  btn.title = isCollapsed ? 'Expand message' : 'Collapse message';
  _setActionIcon(btn, isCollapsed ? 'chevron-right' : 'chevron-down');
}

function _addBubbleActions(bubble) {
  if (!bubble) return;
  // Don't render actions while the bubble is still streaming.
  if (bubble.classList.contains('streaming')) return;
  const txt = _getBubbleText(bubble);
  if (!txt || txt === '\u2026') return;
  const anchor = _bubbleAnchorId(bubble);
  // Idempotent: if the row already exists, just backfill the delete button once
  // an anchor id is available (a freshly-sent user bubble gets its id only after
  // the turn persists). System placeholders never get one — no id to delete.
  const existingActions = bubble.querySelector(':scope > .bubble-actions');
  if (existingActions) {
    if (anchor && !existingActions.querySelector('.bubble-delete-btn')) {
      existingActions.appendChild(_makeBubbleDeleteBtn(bubble));
      _renderActionIcons(existingActions);
    }
    return;
  }

  const actions = document.createElement('div');
  actions.className = 'bubble-actions';

  // Collapse / expand button
  const collapseBtn = document.createElement('button');
  collapseBtn.type = 'button';
  collapseBtn.className = 'bubble-action-btn bubble-collapse-btn';
  collapseBtn.title = 'Collapse message';
  collapseBtn.innerHTML = '<i data-lucide="chevron-down" style="width:14px;height:14px;"></i>';
  collapseBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _toggleBubbleCollapse(collapseBtn, bubble);
  });
  actions.appendChild(collapseBtn);

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

  // Delete (per-turn). Only when we have an id to anchor the turn on, so it
  // never appears on transient system placeholders.
  if (anchor) actions.appendChild(_makeBubbleDeleteBtn(bubble));

  bubble.appendChild(actions);
  _renderActionIcons(actions);
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
  const isMd = _fillAgentBubble(last, text, extraClass !== 'streaming');
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
  if (extraClass) last.className = 'chat-bubble agent ' + extraClass;
  else last.classList.remove('streaming');
  if (isMd) last.classList.add('md');
  _scrollToBottomIfNear(app.chatMessages);
  _addBubbleActions(last);
}

async function sendMessage() {
  if (!_canChat()) { applyChatGate(); return; }
  const text = app.chatInput.value.trim();
  if (!text) return;

  // No agent yet — don't push the user into a "create an agent" flow. Spin up
  // (or reuse) their shared webAgent in a fresh session and carry on sending.
  if (!app.currentAgentId) {
    if (!app.currentUserId) { applyChatGate(); return; }
    try {
      if (typeof app.startWebagentSession === 'function') {
        await app.startWebagentSession();
      }
    } catch (e) {
      addChatBubble('agent', '❌ Could not start webAgent: ' + (e.message || e), 'error');
      return;
    }
    // startWebagentSession may not have resolved an id (edge cases) — bail
    // rather than send an agent-less message the backend would reject.
    if (!app.currentAgentId) return;
  }

  // ── Save to outbox BEFORE clearing the input ──────────────────────
  const outboxEntry = {
    id: 'msg_' + Date.now() + '_' + (++_outboxIdCounter),
    text: text,
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
    agent_id: app.currentAgentId,
    timestamp: new Date().toISOString(),
  };
  _addToOutbox(outboxEntry);

  app.chatInput.value = '';
  app.chatSend.disabled = true;
  _updateInputRowState();
  _autoResizePill(app.chatInput);
  _clearDraft();
  // Clear stale suggestion chips the moment the user sends.
  if (typeof app.clearSuggestions === 'function') {
    try { app.clearSuggestions(); } catch (_) { /* best-effort */ }
  }

  // Advance the poll cursor so auto-poll doesn't re-render this message
  if (window.__chatPollLastAt !== undefined) {
    window.__chatPollLastAt = new Date().toISOString();
  }

  const _userBubble = addChatBubble('user', text);
  app.isProcessing = true;
  // Light the pill immediately — WebSocket events refine the note from here.
  if (app.chatActivityStart) app.chatActivityStart('Sending…');

  // Build payload
  const base = {
    message: text,
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
    execution_mode: app.executionMode || 'ask',
  };
  if (app.currentAgentId) base.agent_id = app.currentAgentId;
  const payload = addAttachmentsToMessage(base);
  if (app.clearPendingAttachments) app.clearPendingAttachments();

  // Fire-and-forget: persist the message + start the run server-side, then
  // return. ALL output (including for THIS device) arrives via the per-user
  // WebSocket and the DB — the run survives leaving, closing the browser, or
  // switching sessions/devices. We never hold a connection open for the answer.
  try {
    const resp = await fetch(apiPath('/api/v1/chat/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      // Server responded with an error — keep in outbox for retry.
      // Convert the normal user bubble to a pending bubble (no duplicates).
      if (_userBubble) _convertBubbleToPending(_userBubble, outboxEntry);
      app.isProcessing = false;
      app.chatSend.disabled = false;
      if (app.chatActivityStop) app.chatActivityStop();
      return;
    }

    const data = await resp.json().catch(() => ({}));

    // Slash command handled synchronously — render its reply directly.
    if (data.status === 'ok' && data.reply) {
      _removeFromOutbox(outboxEntry.id);
      addChatBubble('agent', data.reply);
      app.isProcessing = false;
      app.chatSend.disabled = false;
      if (app.chatActivityStop) app.chatActivityStop();
      if (typeof app.populateSessionSelect === 'function') {
        app.populateSessionSelect(app.currentUserId);
      }
      return;
    }

    // status is 'running' (fresh) or 'replacing' (interrupted a prior run to
    // start this one). Either way the run is going server-side.
    _removeFromOutbox(outboxEntry.id);

    // Tag our local user bubble with its interaction id so the user_message
    // broadcast we just triggered dedups against it (other devices, which have
    // no such bubble, will render it live).
    if (data.turn_id && _userBubble) {
      _userBubble.setAttribute('data-msg-id', data.turn_id);
      // Now that the bubble has a real id, backfill its delete button.
      _addBubbleActions(_userBubble);
    }
    if (typeof app.populateSessionSelect === 'function') {
      app.populateSessionSelect(app.currentUserId);
    }
    // No placeholder here: the agent's bubble(s) are created by the first WS
    // `stream` event, keyed by per-step assistant id, so EVERY step shows as its
    // own bubble. The input stays usable — sending again interrupts this run and
    // starts a new one. If the WS is down, the visibility/focus refresh + WS
    // reconnect-replay catch up from the DB.
  } catch (e) {
    console.warn('[chat/send] failed', e);
    // Network error (server down, reloading) — keep in outbox for retry.
    // Convert the normal user bubble to a pending bubble (no duplicates).
    if (_userBubble) _convertBubbleToPending(_userBubble, outboxEntry);
    app.isProcessing = false;
    app.chatSend.disabled = false;
    if (app.chatActivityStop) app.chatActivityStop();
  }
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

// Exposed on `app` so modules that need to abort the stream (e.g. sessions.js
// when switching session/agent) can call it WITHOUT importing chat.js — that
// import created a chat.js <-> sessions.js cycle. chat.js is loaded at boot
// (main.js), so app.abortChatStream is always set before any switch happens.
app.abortChatStream = abortChatStream;

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
  const isMd = _fillAgentBubble(bubble, text, extraClass !== 'streaming');
  if (extraClass === 'streaming') {
    bubble.className = 'chat-bubble agent streaming';
  } else if (extraClass) {
    bubble.className = 'chat-bubble agent ' + extraClass;
  } else {
    bubble.className = 'chat-bubble agent';
  }
  // className was reassigned above (wiping .md); restore it when markdown rendered.
  if (isMd) bubble.classList.add('md');
  if (app.chatMessages) _scrollToBottomIfNear(app.chatMessages);
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
  // Keep cache fresh — update the cached assistant message content
  if (turnId && app.currentSessionId) {
    _cacheAppendMessage(app.currentSessionId, { role: 'assistant', content: textChunk, id: turnId, _streaming: true });
  }
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
  // Keep cache fresh — finalize the cached assistant message
  if (turnId && app.currentSessionId && content) {
    _cacheAppendMessage(app.currentSessionId, { role: 'assistant', content, id: turnId, _finalized: true });
  }
  // The agent just finished — offer fresh suggested replies in the pill chips.
  if (typeof app.refreshSuggestions === 'function') {
    try { app.refreshSuggestions(); } catch (_) { /* best-effort */ }
  }
  // The turn may have loaded a skill — refresh the active-skill chips.
  if (typeof app.refreshActiveSkills === 'function') {
    try { app.refreshActiveSkills(); } catch (_) { /* best-effort */ }
  }
}

/**
 * Attach a tool-call panel to the last agent bubble in the chat.
 *
 * Called by chat-activity.js's stop() when a turn ends with tool calls.
 * Renders a clickable chip ("N tool calls") inside the bubble; clicking it
 * expands an accordion list of every tool call made during that turn, each
 * independently expandable to show arguments + result. Reuses the same row
 * rendering as the live activity panel so the UX is identical.
 */
function attachToolCallsToLastBubble(calls) {
  if (!calls || calls.length === 0) return;
  if (!app.chatMessages) return;
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  const last = bubbles[bubbles.length - 1];
  if (!last) return;

  // Don't double-attach if already present.
  if (last.querySelector('.bubble-tool-calls')) return;

  const container = document.createElement('div');
  container.className = 'bubble-tool-calls';

  // ── Header chip (clickable) ──
  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'bubble-tool-calls-head';
  head.setAttribute('aria-expanded', 'false');
  const n = calls.length;
  head.innerHTML = '<span class="bubble-tool-calls-icon" aria-hidden="true">⚙</span> '
    + (n === 1 ? '1 tool call' : n + ' tool calls')
    + ' <span class="bubble-tool-calls-chevron" aria-hidden="true">›</span>';
  container.appendChild(head);

  // ── Expandable panel ──
  const panel = document.createElement('div');
  panel.className = 'bubble-tool-calls-panel';
  panel.hidden = true;

  // Render each tool call row (reusing the same accordion logic)
  calls.forEach((entry, i) => {
    // Clone the entry so we can toggle .open independently per bubble
    const rowEntry = { ...entry, open: false };
    const row = buildToolRow(rowEntry, i);
    // Wire accordion toggle
    const rowHead = row.querySelector('.ca-tool-head');
    if (rowHead) {
      rowHead.addEventListener('click', (e) => {
        e.stopPropagation();
        rowEntry.open = !rowEntry.open;
        row.classList.toggle('open', rowEntry.open);
        rowHead.setAttribute('aria-expanded', rowEntry.open ? 'true' : 'false');
      });
    }
    panel.appendChild(row);
  });
  container.appendChild(panel);

  // ── Toggle panel on header click ──
  let panelOpen = false;
  head.addEventListener('click', (e) => {
    e.stopPropagation();
    panelOpen = !panelOpen;
    panel.hidden = !panelOpen;
    head.setAttribute('aria-expanded', panelOpen ? 'true' : 'false');
    container.classList.toggle('open', panelOpen);
    // When open, hide the head chip so it's one compact panel
    head.style.display = panelOpen ? 'none' : '';
  });

  last.insertBefore(container, last.querySelector('.bubble-actions'));
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

/**
 * Finalize an INTERMEDIATE agent step (an assistant message that precedes tool
 * calls). Keeps its bubble with the text and drops the streaming spinner, but
 * does NOT unlock the input — the turn continues with more steps. Empty steps
 * (tool-call-only, no text) render nothing.
 */
function finalizeAgentStep(content, asstId) {
  const text = (content || '').trim();
  let bubble = _findAgentBubbleForTurn(asstId);
  if (!text) {
    if (bubble) bubble.remove();
    if (asstId) _wsTurnBuffers.delete(asstId);
    return;
  }
  if (!bubble) {
    bubble = addChatBubble('agent', text, undefined, undefined, asstId);
  } else {
    _setBubbleText(bubble, text);
  }
  if (asstId) _wsTurnBuffers.delete(asstId);
}

/**
 * Mark an agent bubble interrupted, keeping whatever partial text it has. If we
 * know the step's id, target that bubble; otherwise fall back to the last
 * still-streaming agent bubble. Unlocks the input (a follow-up message may have
 * already started a replacement run, which will re-engage on its own).
 */
function markAgentInterrupted(asstId) {
  let bubble = asstId ? _findAgentBubbleForTurn(asstId) : null;
  if (!bubble && app.chatMessages) {
    const streaming = app.chatMessages.querySelectorAll('.chat-bubble.agent.streaming');
    bubble = streaming[streaming.length - 1] || null;
  }
  if (bubble) {
    const cur = _getBubbleText(bubble);
    _setBubbleText(bubble, cur ? cur + '\n\n(interrupted)' : '(interrupted)', 'interrupted');
  }
  if (asstId) _wsTurnBuffers.delete(asstId);
  app.agentBuffer = '';
  app.isProcessing = false;
  if (app.chatSend) app.chatSend.disabled = !((app.chatInput && app.chatInput.value.trim()));
}

/**
 * Seed a streaming bubble from the DB partial when (re)loading a session that
 * has a run in progress. Renders whatever text the server has persisted so far
 * (cold-device / second-device view) AND primes `_wsTurnBuffers` so that the
 * subsequent WS replay — which only resends chunks NEWER than the resume floor —
 * appends onto this partial instead of replacing it. The final `response`
 * event later overwrites the bubble with the complete text, self-healing any
 * small gap between the persisted partial and the live stream.
 */
function seedStreamingBubble(turnId, content) {
  if (!turnId) return;
  let bubble = _findAgentBubbleForTurn(turnId);
  const text = content || '…';
  if (!bubble) {
    bubble = addChatBubble('agent', text, 'streaming', undefined, turnId);
  } else {
    _setBubbleText(bubble, text, 'streaming');
  }
  _wsTurnBuffers.set(turnId, content || '');
  app.isProcessing = true;
}

export function initChat() {
  app.addChatBubble = addChatBubble;
  app.updateLastBubble = updateLastBubble;
  app.appendStreamToActiveBubble = appendStreamToActiveBubble;
  app.finalizeAgentResponse = finalizeAgentResponse;
  app.ensureStreamingBubbleForActiveTurn = ensureStreamingBubbleForActiveTurn;
  app.seedStreamingBubble = seedStreamingBubble;
  app.finalizeAgentStep = finalizeAgentStep;
  app.markAgentInterrupted = markAgentInterrupted;
  app.attachToolCallsToLastBubble = attachToolCallsToLastBubble;
  app.autoResizeChatInput = () => _autoResizePill(app.chatInput);
  // Helper: focus the chat input. Used when switching/starting sessions.
  app.focusChatInput = () => { if (app.chatInput) app.chatInput.focus(); };
  // Expose for virtual-scroll recycling in sessions.js
  app._linkifyText = linkifyText;
  app._renderMarkdownBody = _renderMarkdownBody;
  app._addBubbleActions = _addBubbleActions;
  app._sendStopMessage = sendStopMessage;

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
    _saveDraft();
  });

  // ── Execution mode toggle (single cycle button) ────────────────────
  const MODE_CYCLE = ['read', 'write', 'auto'];
  const MODE_LABELS = { read: 'Read', write: 'Write', auto: 'Auto' };
  app.executionMode = 'write'; // default
  const modeBtn = document.getElementById('chat-mode-btn');
  if (modeBtn) {
    // Restore saved preference
    try {
      const saved = localStorage.getItem('chat_execution_mode');
      if (saved && MODE_CYCLE.includes(saved)) app.executionMode = saved;
    } catch (_) {}
    modeBtn.textContent = MODE_LABELS[app.executionMode] || 'Write';
    modeBtn.addEventListener('click', () => {
      const idx = MODE_CYCLE.indexOf(app.executionMode);
      app.executionMode = MODE_CYCLE[(idx + 1) % MODE_CYCLE.length];
      modeBtn.textContent = MODE_LABELS[app.executionMode];
      try { localStorage.setItem('chat_execution_mode', app.executionMode); } catch (_) {}
    });
  }

  // ── Continue button ──────────────────────────────────────────────
  // Sends a "continue" message — a shortcut for the common case where
  // the agent asks the user to continue after a tool result.
  const continueBtn = document.getElementById('chat-continue-btn');
  if (continueBtn) {
    continueBtn.addEventListener('click', () => {
      if (!_canChat()) { applyChatGate(); return; }
      if (!app.currentAgentId) return;
      app.chatInput.value = 'continue';
      sendMessage();
    });
  }

  // Show the continue button when the agent is idle and selected;
  // hide it while processing or when no agent is active.
  // Poll every 500ms to stay in sync with isProcessing / currentAgentId
  // changes from any module (agents.js, sessions.js, autoagent.js).
  function _updateContinueBtn() {
    if (!continueBtn) return;
    const show = !app.isProcessing && !!app.currentAgentId;
    continueBtn.style.display = show ? 'flex' : 'none';
  }
  // ── Stop button (above the pill, left of continue) ──────────────
  const stopBtn = document.getElementById('chat-stop-btn');
  if (stopBtn) {
    stopBtn.addEventListener('click', sendStopMessage);
    // Extend the continue-btn poll to also update stop button visibility.
    // We wrap the original so the interval picks up the combined function.
    const origContinue = _updateContinueBtn;
    _updateContinueBtn = function() {
      origContinue();
      stopBtn.style.display = app.isProcessing ? 'flex' : 'none';
    };
  }
  setInterval(_updateContinueBtn, 500);
  _updateContinueBtn();

  // Apply gating immediately with cached value, then re-apply once mode is loaded
  applyChatGate();
  fetchAccessMode().then(() => {
    applyChatGate();
    _restoreDraft();
    // Pending messages from a previous browsing session (before a refresh)
    // show up as pending bubbles but are NOT force-flushed. The periodic
    // poll + _retryEntry's dedup logic will handle them automatically.
    if (_outboxHasPending()) {
      _renderPendingBubbles();
      _startOutboxPoll();
    }
  });

  // Restore any unsent draft from a previous page load so a refresh keeps it.
  // Runs again above once the access mode resolves, in case this first attempt
  // was gated out before we knew the visitor was allowed to chat.
  _restoreDraft();

  // Reserve space at the bottom of the scrollable message list equal to the floating
  // input area's height so the last message clears the absolutely-positioned input.
  // Tracks the textarea as it grows with multi-line input.
  const inputArea = document.getElementById('chat-input-area');
  const messagesInner = document.getElementById('chat-messages-inner');
  const messagesEl = document.getElementById('chat-messages');
  if (inputArea && messagesInner && typeof ResizeObserver !== 'undefined') {
    const syncPad = () => {
      const h = inputArea.offsetHeight;
      // Publish the input-area height (consumed by index.html for layout sizing).
      if (messagesEl) messagesEl.style.setProperty('--chat-input-h', h + 'px');
      // Reserve the input-area height plus a 24px gap so the newest message
      // rests just above the floating pill at scroll-bottom — fully visible,
      // never tucked under it. Older bubbles scroll behind the glass pill.
      messagesInner.style.paddingBottom = (h + 24) + 'px';
    };
    new ResizeObserver(syncPad).observe(inputArea);
    syncPad();
  }

  // ── Scroll-to-bottom chevron ──────────────────────────────────────
  _scrollBtn = document.getElementById('chat-scroll-bottom-btn');
  if (messagesInner && _scrollBtn) {
    // Scroll listener: update chevron visibility + release scroll lock
    messagesInner.addEventListener('scroll', () => {
      _updateScrollChevron(messagesInner);
    }, { passive: true });

    // Click: scroll to bottom, lock auto-scroll, hide chevron
    _scrollBtn.addEventListener('click', () => {
      _scrollLocked = true;
      _programmaticScroll = true;
      messagesInner.scrollTop = messagesInner.scrollHeight;
      _scrollBtn.classList.remove('visible');
    });

    // Initial state: locked at bottom, chevron hidden
    _scrollLocked = true;
    _scrollBtn.classList.remove('visible');
  }

  // ── Mobile focus-on-first-tap ────────────────────────────────────
  // Mobile browsers (Chrome, Firefox, Safari) block programmatic .focus()
  // unless it runs inside a user-gesture handler.  We capture the FIRST
  // touch/click on the page and re-focus the chat input so the cursor and
  // keyboard appear without requiring a second tap.
  let _focused = false;
  const _firstTap = () => {
    if (_focused) return;
    _focused = true;
    if (app.chatInput) app.chatInput.focus();
  };
  document.addEventListener('touchstart', _firstTap, { once: true, passive: true });
  document.addEventListener('click', _firstTap, { once: true, passive: true });
}

export { escapeHtml };
