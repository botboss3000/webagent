'use strict';

// Chat bubble DOM — bubble creation, Markdown rendering (marked + DOMPurify),
// linkification, relative timestamps. Sets app.addChatBubble / app._renderMarkdownBody.
// Module map for this folder: ui/chat-side-panel/js/README.md.

import { _refreshLucideIcons, _esc } from '../../shared/js/dom-utils.js';
import { app } from '../../shared/js/state.js';
import { renderAttachmentElement } from '../../shared/js/attachments.js';

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

function _renderMarkdownBody(text, highlight) {
  if (!text || !text.trim() || !_markdownReady()) return null;
  let html;
  try {
    html = window.marked.parse(text, { gfm: true, breaks: true });
  } catch (_) { return null; }
  const body = document.createElement('div');
  body.className = 'md-body';
  body.innerHTML = window.DOMPurify.sanitize(html, { FORBID_ATTR: ['style'] });
  body.querySelectorAll('a[href]').forEach((a) => {
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  });
  if (highlight !== false) _highlightCodeBlocks(body);
  return body;
}

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

/** Format a UTC timestamp string (e.g. "2024-01-15 10:30:00") to an epoch ms. */
function _parseCreatedAt(str) {
  if (!str) return Date.now();
  return new Date(str.replace(' ', 'T') + 'Z').getTime();
}

/** Format an elapsed duration since a given epoch ms into a relative-time label. */
function _formatRelativeTime(createdAtMs) {
  const elapsed = Date.now() - createdAtMs;
  const totalMin = Math.floor(elapsed / 60000);
  if (totalMin < 1) return '0m';
  if (totalMin < 60) return totalMin + 'm';
  const hours = Math.floor(totalMin / 60);
  const mins = totalMin % 60;
  return hours + 'h ' + mins + 'm';
}

/** Refresh every `.bubble-time` element in the chat panel. Called on a 60s interval. */
function _refreshAllBubbleTimes() {
  document.querySelectorAll('.chat-bubble .bubble-time').forEach(el => {
    const ts = el.getAttribute('data-created-at');
    if (ts) el.textContent = _formatRelativeTime(Number(ts));
  });
}

// ── Consecutive-bubble grouping ──────────────────────────────────────────────
// Back-to-back agent text bubbles "join up" into one visual stack (tight gap +
// squared touching corners) so a single multi-part reply reads as one block.
// They stay separate when a long time has passed between them (the agent picking
// the thread back up later), or when a non-text bubble (tool-only, error) breaks
// the run. Tuning lives here; the look is driven by the .grouped-* classes in
// app1.css. Applied incrementally as each bubble is appended (bubbles arrive in
// order on both live stream and session load).
const GROUP_GAP_MS = 3 * 60 * 1000; // join only if <3 min apart, else separate

function _bubbleGroupable(bubble) {
  if (!bubble || !bubble.classList || !bubble.classList.contains('agent')) return false;
  // Only plain agent prose joins — tool pills and errors stand alone.
  return !bubble.classList.contains('tool-only')
      && !bubble.classList.contains('error');
}

function _bubblesJoin(a, b) {
  if (!_bubbleGroupable(a) || !_bubbleGroupable(b)) return false;
  const ta = Number(a.getAttribute('data-created-at')) || 0;
  const tb = Number(b.getAttribute('data-created-at')) || 0;
  if (!ta || !tb) return true; // missing timing → assume same burst, join
  return Math.abs(tb - ta) <= GROUP_GAP_MS;
}

// Re-evaluate a bubble's join state against the one directly above it.
function _applyGrouping(bubble) {
  if (!bubble) return;
  const prev = bubble.previousElementSibling;
  const join = !!(prev && prev.classList && prev.classList.contains('chat-bubble') && _bubblesJoin(prev, bubble));
  bubble.classList.toggle('grouped-cont', join);    // joined to the bubble above
  if (prev && prev.classList && prev.classList.contains('chat-bubble')) {
    prev.classList.toggle('grouped-open', join);    // joined to the bubble below
  }
}

// Full rescan — for callers that rebuild the list out of band (reconcile, etc.).
function _regroupBubbles() {
  if (!app.chatMessages) return;
  const bubbles = app.chatMessages.querySelectorAll(':scope > .chat-bubble');
  for (const b of bubbles) _applyGrouping(b);
}

function addChatBubble(role, text, extraClass, imageUrl, turnId, msgId, createdAt) {
  // Any real text/user bubble ends the current tool-only group, so the next
  // tool-only turn starts a fresh grouped bubble instead of merging.
  if (extraClass !== 'tool-only') app._activeToolGroupBubble = null;
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (turnId) bubble.setAttribute('data-turn-id', turnId);
  if (msgId) bubble.setAttribute('data-msg-id', msgId);
  {
    const createdAtMs = createdAt ? _parseCreatedAt(createdAt) : Date.now();
    bubble.setAttribute('data-created-at', createdAtMs);
  }
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
    img.style.border = '1px solid var(--border)';
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
  _applyGrouping(bubble);
  // scroll handled by caller or _scrollToBottomIfNear from chat-ui
  return bubble;
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
    img.style.border = '1px solid var(--border)';
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
  // The class rewrite above wipes any grouped-* state — re-derive it (e.g. a
  // streaming bubble that just turned into an error must drop out of its group).
  _applyGrouping(last);
  // scroll handled by caller
}

// Expose on app immediately so session-load.js / stream modules can use them
app.addChatBubble = addChatBubble;
app.updateLastBubble = updateLastBubble;
app._linkifyText = linkifyText;
app._renderMarkdownBody = _renderMarkdownBody;
app._regroupBubbles = _regroupBubbles;

export {
  addChatBubble,
  updateLastBubble,
  _fillAgentBubble,
  linkifyText,
  _esc as escapeHtml,
  _renderMarkdownBody,
  _formatRelativeTime,
  _refreshAllBubbleTimes,
  _parseCreatedAt,
  _regroupBubbles,
  _applyGrouping,
};