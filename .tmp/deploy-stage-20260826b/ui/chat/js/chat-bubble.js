'use strict';

// Chat bubble DOM — bubble creation, Markdown rendering (marked + DOMPurify),
// linkification, relative timestamps. Sets app.addChatBubble / app._renderMarkdownBody.
// Module map for this folder: ui/chat/js/README.md.

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

// Fill a container (bubble or turn-section) with rendered Markdown or linkified text.
function _fillAgentBubble(container, text, highlight) {
  const body = _renderMarkdownBody(text, highlight);
  if (body) {
    container.appendChild(body);
    container.classList.add('md');
    container.__mdSource = text;
    return true;
  }
  container.appendChild(linkifyText(text || ''));
  container.classList.remove('md');
  container.__mdSource = null;
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
  if (!str) return NaN;
  if (typeof str === 'number') return Number.isFinite(str) ? str : NaN;
  const raw = String(str).trim();
  if (!raw) return NaN;
  // SQLite stores UTC as `YYYY-MM-DD HH:MM:SS[.fff]` without a zone.  Remote
  // stores may return a normal ISO value that already has Z or an offset; do not
  // append a second zone suffix to those values.
  const iso = raw.replace(' ', 'T');
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : iso + 'Z';
  const parsed = Date.parse(zoned);
  return Number.isFinite(parsed) ? parsed : NaN;
}

/** Apply an authoritative interactions.created_at value to a rendered bubble. */
function _setBubbleCreatedAt(bubble, createdAt) {
  if (!bubble) return;
  const createdAtMs = _parseCreatedAt(createdAt);
  if (Number.isFinite(createdAtMs)) {
    bubble.setAttribute('data-created-at', createdAtMs);
    bubble.removeAttribute('data-time-error');
  } else {
    bubble.removeAttribute('data-created-at');
    bubble.setAttribute(
      'data-time-error',
      createdAt ? `Invalid database created_at: ${createdAt}` : 'Database created_at is missing',
    );
  }
  // Update time elements in both legacy footers and new gutters
  const times = bubble.querySelectorAll(':scope .bubble-time');
  times.forEach(time => {
    if (Number.isFinite(createdAtMs)) {
      time.setAttribute('data-created-at', createdAtMs);
      time.removeAttribute('data-time-error');
      time.removeAttribute('title');
      time.textContent = _formatRelativeTime(createdAtMs);
    } else if (app.isDebug) {
      const error = bubble.getAttribute('data-time-error');
      time.removeAttribute('data-created-at');
      time.setAttribute('data-time-error', error);
      time.title = error;
      time.textContent = `time error: ${error}`;
    }
  });
  // Live nodes can exist before their durable session_seq is known. Re-run the
  // fallback placement as soon as a useful timestamp arrives; the sequence
  // path below will replace this provisional placement once DB-tail catches up.
  if (bubble.isConnected) _reorderTranscriptCanonical();
}

let _nextRenderOrder = 1;

function _renderOrder(el) {
  if (!el) return Number.MAX_SAFE_INTEGER;
  const existing = Number(el.dataset && el.dataset.renderOrder);
  if (Number.isFinite(existing)) return existing;
  const order = _nextRenderOrder++;
  if (el.dataset) el.dataset.renderOrder = String(order);
  return order;
}

/**
 * Rebuild the live transcript order without letting clock skew override durable
 * interaction order. Sequenced nodes always stay ordered by session_seq. Nodes
 * that have not reached persistence yet are placed into the sequenced timeline
 * by created_at, then move to their authoritative slot when their seq arrives.
 */
function _reorderTranscriptCanonical() {
  if (!app.chatMessages) return;
  const nodes = Array.from(app.chatMessages.children).filter(el =>
    el.classList && (
      el.classList.contains('chat-bubble')
      || el.classList.contains('chat-bubble-placeholder')
    ),
  );
  if (nodes.length < 2) return;

  const key = el => {
    const seqRaw = el.getAttribute('data-session-seq');
    const createdAtRaw = el.getAttribute('data-created-at');
    const seq = seqRaw == null ? NaN : Number(seqRaw);
    const createdAt = createdAtRaw == null ? NaN : Number(createdAtRaw);
    return {
      el,
      seq: Number.isFinite(seq) ? seq : null,
      createdAt: Number.isFinite(createdAt) ? createdAt : null,
      order: _renderOrder(el),
    };
  };
  const keyed = nodes.map(key);
  const sequenced = keyed.filter(x => x.seq !== null).sort((a, b) =>
    (a.seq - b.seq)
      || ((a.createdAt !== null && b.createdAt !== null) ? a.createdAt - b.createdAt : 0)
      || (a.order - b.order),
  );
  const provisional = keyed.filter(x => x.seq === null).sort((a, b) => {
    if (a.createdAt !== null && b.createdAt !== null && a.createdAt !== b.createdAt) {
      return a.createdAt - b.createdAt;
    }
    if (a.createdAt !== null && b.createdAt === null) return -1;
    if (a.createdAt === null && b.createdAt !== null) return 1;
    return a.order - b.order;
  });

  // Insert provisional nodes into timestamp-derived gaps while leaving the
  // relative order of all durable nodes untouched.
  const gaps = Array.from({ length: sequenced.length + 1 }, () => []);
  for (const item of provisional) {
    let gap = sequenced.length;
    if (item.createdAt !== null) {
      const later = sequenced.findIndex(saved =>
        saved.createdAt !== null && saved.createdAt > item.createdAt,
      );
      if (later !== -1) gap = later;
    }
    gaps[gap].push(item);
  }
  const ordered = [];
  for (let i = 0; i < sequenced.length; i++) {
    ordered.push(...gaps[i], sequenced[i]);
  }
  ordered.push(...gaps[sequenced.length]);

  // Provisional LIVE activity groups carry data-activity-turn-id = the owning
  // USER interaction id. Keep those beside their prompt until persistence gives
  // them a sequence. Persisted segments must remain in durable sequence order:
  // a closer/summary can legitimately divide multiple segments with one owner.
  for (const item of [...ordered]) {
    const ownerId = item.el.dataset && item.el.dataset.activityTurnId;
    if (!ownerId || !item.el.classList.contains('activity-group')) continue;
    if (item.seq !== null || item.el.dataset.activitySegmentId) continue;
    const current = ordered.indexOf(item);
    const owner = ordered.findIndex(candidate => candidate.el.classList.contains('user')
      && (candidate.el.dataset.msgId === ownerId || candidate.el.dataset.turnId === ownerId));
    if (current === -1 || owner === -1 || current === owner + 1) continue;
    ordered.splice(current, 1);
    const relocatedOwner = ordered.findIndex(candidate => candidate.el.classList.contains('user')
      && (candidate.el.dataset.msgId === ownerId || candidate.el.dataset.turnId === ownerId));
    ordered.splice(relocatedOwner + 1, 0, item);
  }

  if (ordered.every((item, index) => nodes[index] === item.el)) return;
  for (const item of ordered) app.chatMessages.appendChild(item.el);
  _regroupBubbles();
}

/** Stamp a durable interaction sequence and keep transcript nodes ordered. */
function _setBubbleSessionSeq(bubble, sessionSeq) {
  if (!bubble || !app.chatMessages || !Number.isFinite(sessionSeq)) return;
  bubble.setAttribute('data-session-seq', String(sessionSeq));
  _renderOrder(bubble);
  _reorderTranscriptCanonical();
}

/** Format an elapsed duration since a given epoch ms into a relative-time label. */
function _formatRelativeTime(createdAtMs) {
  if (!Number.isFinite(createdAtMs)) return 'time unavailable';
  const elapsed = Date.now() - createdAtMs;
  const totalSec = Math.floor(elapsed / 1000);
  if (totalSec < 60) return totalSec + 's';
  const totalMin = Math.floor(totalSec / 60);
  if (totalMin < 60) return totalMin + 'm';
  const hours = Math.floor(totalMin / 60);
  const mins = totalMin % 60;
  return hours + 'h ' + mins + 'm';
}

/** Refresh every `.bubble-time` element in the chat panel. Called on a 1s interval. */
function _refreshAllBubbleTimes() {
  document.querySelectorAll('.chat-bubble .bubble-time').forEach(el => {
    const ts = el.getAttribute('data-created-at');
    if (ts) {
      el.textContent = _formatRelativeTime(Number(ts));
    } else if (app.isDebug && el.hasAttribute('data-time-error')) {
      el.textContent = `time error: ${el.getAttribute('data-time-error')}`;
    }
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
  // Only plain agent prose joins — tool pills, errors, deleted, and the
  // closer 'Closer' lane stand alone (the close-out must read as its own
  // separate bubble, not a continuation of the agent's reply).
  return !bubble.classList.contains('tool-only')
      && !bubble.classList.contains('error')
      && !bubble.classList.contains('interrupted')
      && !bubble.classList.contains('deleted')
      && !bubble.classList.contains('summary-bubble');
}

function _bubblesJoin(a, b) {
  if (!_bubbleGroupable(a) || !_bubbleGroupable(b)) return false;
  const ta = Number(a.getAttribute('data-created-at')) || 0;
  const tb = Number(b.getAttribute('data-created-at')) || 0;
  if (!ta || !tb) return false;
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
  const extraClasses = new Set(String(extraClass || '').split(/\s+/).filter(Boolean));
  const isToolOnly = extraClasses.has('tool-only');
  const isStreaming = extraClasses.has('streaming');
  // Any real text/user bubble ends the current tool-only group, so the next
  // tool-only turn starts a fresh grouped bubble instead of merging.
  if (!isToolOnly && !isStreaming) app._activeToolGroupBubble = null;

  // ── Agent turn merging ──────────────────────────────────────────────────
  // Consecutive agent text messages merge into ONE bubble per turn. A user
  // message closes the turn; the next agent text starts a fresh bubble.
  // Error / interrupted / placeholder / summary bubbles do NOT participate
  // in merging (the closer close-out is its own separate bubble).
  if (role === 'user') {
    app._agentTurnBubble = null;
  }

  const isMergeable = role === 'agent'
    && !isToolOnly
    && extraClass !== 'error'
    && extraClass !== 'interrupted'
    && extraClass !== 'session-placeholder'
    && extraClass !== 'summary-bubble'
    && text && text.trim();

  if (isMergeable && app._agentTurnBubble && app._agentTurnBubble.isConnected) {
    // Append a new LLM section to the open agent turn bubble.
    const target = app._agentTurnBubble;
    const idx = target.querySelectorAll(':scope > .turn-section.llm-section').length;
    const section = document.createElement('div');
    section.className = 'turn-section llm-section';
    section.dataset.sectionIdx = String(idx);
    if (turnId) section.dataset.turnId = turnId;
    if (msgId) section.dataset.msgId = msgId;
    _fillAgentBubble(section, text, extraClass !== 'streaming');
    target.appendChild(section);
    if (section.__mdSource) target.__mdSource = section.__mdSource;
    if (turnId) target.setAttribute('data-turn-id', turnId);
    if (msgId) target.setAttribute('data-msg-id', msgId);
    if (createdAt) _setBubbleCreatedAt(target, createdAt);
    // Wire the PREVIOUS LLM section (the one before this new one) so its gutter exists
    const allLLM = target.querySelectorAll(':scope > .turn-section.llm-section');
    if (allLLM.length >= 2 && typeof app._wireLLMSection === 'function') {
      app._wireLLMSection(allLLM[allLLM.length - 2], target);
    }
    return target;
  }

  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (turnId) bubble.setAttribute('data-turn-id', turnId);
  if (msgId) bubble.setAttribute('data-msg-id', msgId);
  {
    _setBubbleCreatedAt(bubble, createdAt);
  }
  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
  }
  // Normal agent responses retain their lane label. The production Closer is
  // content-only: no development sender heading above the final output.
  if (role === 'agent' && !isToolOnly && extraClass !== 'session-placeholder'
      && extraClass !== 'summary-bubble') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'Agent';
    bubble.appendChild(label);
  }
  if (role === 'info') {
    const body = document.createElement('div');
    body.className = 'bubble-body';
    body.appendChild(linkifyText(text));
    bubble.appendChild(body);
  } else if (role === 'agent' && extraClass !== 'error') {
    // Wrap agent content in an LLM turn-section
    const section = document.createElement('div');
    section.className = 'turn-section llm-section';
    section.dataset.sectionIdx = '0';
    if (turnId) section.dataset.turnId = turnId;
    if (msgId) section.dataset.msgId = msgId;
    _fillAgentBubble(section, text, extraClass !== 'streaming');
    bubble.appendChild(section);
    // Also track mdSource on the bubble for copy/speak helpers
    if (section.__mdSource) bubble.__mdSource = section.__mdSource;
  } else if (role === 'agent' && extraClass === 'error') {
    const body = document.createElement('div');
    body.className = 'bubble-body';
    body.appendChild(linkifyText(text));
    bubble.appendChild(body);
  } else if (role === 'user') {
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
    const target = bubble.querySelector(':scope > .turn-section .md-body, :scope > .bubble-body, :scope > .md-body') || bubble;
    target.appendChild(img);
  }
  if (role === 'agent' && window.__streamAttachments && extraClass === 'has-attachments') {
    for (const att of window.__streamAttachments) {
      const el = renderAttachmentElement(att);
      if (el) bubble.appendChild(el);
    }
    window.__streamAttachments = null;
  }

  // Track this as the open agent turn bubble for merging subsequent messages.
  // (streaming bubbles start empty but should still anchor the turn.)
  // The summary bubble never becomes the merge anchor — it is a standalone lane.
  if (role === 'agent' && !isToolOnly && extraClass !== 'error'
      && extraClass !== 'interrupted' && extraClass !== 'summary-bubble') {
    app._agentTurnBubble = bubble;
  }

  app.chatMessages.appendChild(bubble);
  _renderOrder(bubble);
  _reorderTranscriptCanonical();
  _applyGrouping(bubble);
  // Follow-the-tail: when auto-scroll is armed (chevron-locked / at the
  // bottom), snap to the latest message as new content lands. Manual scroll
  // releases the lock via _updateScrollChevron, and _scrollToBottomIfNear
  // no-ops while it's released. (Session-load's windowed render re-positions
  // afterwards, so it never fights an anchored restore.)
  if (typeof app._scrollToBottomIfNear === 'function') {
    app._scrollToBottomIfNear(app._chatScroller || (app.chatMessages && app.chatMessages.parentElement));
  }
  return bubble;
}

function addRecoveryNotice(recovery = {}) {
  if (!app.chatMessages) return null;
  const bubble = document.createElement('details');
  bubble.className = 'chat-bubble agent recovery-notice';
  bubble.setAttribute('data-created-at', Date.now());
  const summary = document.createElement('summary');
  summary.textContent = 'Recovered and resumed';
  bubble.appendChild(summary);
  const details = document.createElement('dl');
  const fields = [
    ['Stopped because', recovery.stop_cause],
    ['Issue', recovery.issue],
    ['Recovery', recovery.trigger],
    ['Attempt', recovery.attempt ? `${recovery.attempt}${recovery.max_attempts ? ` of ${recovery.max_attempts}` : ''}` : '—'],
    ['Stopped at', recovery.stopped_at],
    ['Recovered at', recovery.recovered_at],
  ];
  for (const [label, value] of fields) {
    if (!value) continue;
    const term = document.createElement('dt');
    term.textContent = label;
    const description = document.createElement('dd');
    description.textContent = String(value);
    details.append(term, description);
  }
  bubble.appendChild(details);
  app.chatMessages.appendChild(bubble);
  _applyGrouping(bubble);
  return bubble;
}

function updateLastBubble(text, extraClass, imageUrl) {
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  const last = bubbles[bubbles.length - 1];
  if (!last) return;
  while (last.firstChild) last.removeChild(last.firstChild);
  // Wrap in LLM section
  const section = document.createElement('div');
  section.className = 'turn-section llm-section';
  section.dataset.sectionIdx = '0';
  const isMd = _fillAgentBubble(section, text, extraClass !== 'streaming');
  last.appendChild(section);
  if (section.__mdSource) last.__mdSource = section.__mdSource;
  if (imageUrl) {
    const img = document.createElement('img');
    img.src = imageUrl;
    img.style.maxWidth = '100%';
    img.style.maxHeight = '400px';
    img.style.borderRadius = '8px';
    img.style.marginTop = '8px';
    img.style.border = '1px solid var(--border)';
    section.appendChild(img);
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
app.addRecoveryNotice = addRecoveryNotice;
app.updateLastBubble = updateLastBubble;
app._linkifyText = linkifyText;
app._renderMarkdownBody = _renderMarkdownBody;
app._regroupBubbles = _regroupBubbles;
app._setBubbleCreatedAt = _setBubbleCreatedAt;
app._setBubbleSessionSeq = _setBubbleSessionSeq;
app._reorderTranscriptCanonical = _reorderTranscriptCanonical;

export {
  addChatBubble,
  addRecoveryNotice,
  updateLastBubble,
  _fillAgentBubble,
  linkifyText,
  _esc as escapeHtml,
  _renderMarkdownBody,
  _formatRelativeTime,
  _refreshAllBubbleTimes,
  _parseCreatedAt,
  _setBubbleCreatedAt,
  _setBubbleSessionSeq,
  _reorderTranscriptCanonical,
  _regroupBubbles,
  _applyGrouping,
};
