'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { addAttachmentsToMessage, renderAttachmentElement } from './attachments.js';
import { getAccessMode, fetchAccessMode, authHeaders } from './left-login.js';
import { _cacheAppendMessage } from './sessions.js';

/** Scroll to bottom only if the user is already at or near the bottom (within 60px). */
function _scrollToBottomIfNear(el) {
  if (!el) return;
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  if (nearBottom) el.scrollTop = el.scrollHeight;
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
// its own handler. Programmatic value changes still need to dispatch 'input'
// or call _autoResizePill directly — see _restoreDraft / sendMessage below.
document.addEventListener('input', (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains('chat-pill-input')) {
    _autoResizePill(t);
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

// ── chat draft persistence ──
// Keep whatever the user has typed (but not yet sent) in the chat pill so a
// page refresh doesn't lose it. Stored as plain text in localStorage; this
// mirrors the in-memory pill, which is already global across session switches.
const _DRAFT_LS_KEY = 'webagent.chatDraft.v1';
function _saveDraft() {
  try {
    const v = app.chatInput ? app.chatInput.value : '';
    if (v) localStorage.setItem(_DRAFT_LS_KEY, v);
    else localStorage.removeItem(_DRAFT_LS_KEY);
  } catch (_) { /* quota / private mode — non-fatal */ }
}
function _clearDraft() {
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
  if (role === 'agent' && extraClass === 'streaming') {
    const stopBtn = document.createElement('button');
    stopBtn.className = 'stop-btn';
    stopBtn.textContent = '\ud83d\uded1';
    stopBtn.title = 'Stop generation';
    stopBtn.addEventListener('click', sendStopMessage);
    bubble.appendChild(stopBtn);
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
  if (isMd) last.classList.add('md');
  _scrollToBottomIfNear(app.chatMessages);
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
  _autoResizePill(app.chatInput);
  _clearDraft();

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
      addChatBubble('agent', 'Server error: ' + resp.status, 'error');
      app.isProcessing = false;
      app.chatSend.disabled = false;
      if (app.chatActivityStop) app.chatActivityStop();
      return;
    }

    const data = await resp.json().catch(() => ({}));

    // Slash command handled synchronously — render its reply directly.
    if (data.status === 'ok' && data.reply) {
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
    addChatBubble('agent', 'Request failed: ' + ((e && e.message) || e), 'error');
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
  app.autoResizeChatInput = () => _autoResizePill(app.chatInput);
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
  setInterval(_updateContinueBtn, 500);
  _updateContinueBtn();

  // Apply gating immediately with cached value, then re-apply once mode is loaded
  applyChatGate();
  fetchAccessMode().then(() => { applyChatGate(); _restoreDraft(); });

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
      // Publish the input-area height so the bottom fade mask (index.html)
      // covers exactly the input zone.
      if (messagesEl) messagesEl.style.setProperty('--chat-input-h', h + 'px');
      // Reserve that height PLUS the mask's 24px fade ramp, so the newest
      // message rests right at the opaque edge of the fade — fully visible,
      // never tucked under the pill or clipped mid-fade.
      messagesInner.style.paddingBottom = (h + 24) + 'px';
    };
    new ResizeObserver(syncPad).observe(inputArea);
    syncPad();
  }
}

export { escapeHtml };
