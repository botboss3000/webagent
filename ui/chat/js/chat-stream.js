'use strict';

// Live WebSocket streaming into bubbles — accumulates chunks per turn, finalizes
// or interrupts agent steps, attaches tool-call accordion panels to agent bubbles.
// Module map for this folder: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { _refreshLucideIcons } from '../../shared/js/dom-utils.js';
import { copyText } from '../../shared/js/clipboard.js';
import { addChatBubble, _fillAgentBubble, _formatRelativeTime, _applyGrouping, _setBubbleCreatedAt, _setBubbleSessionSeq } from './chat-bubble.js';
import { _addBubbleActions, _getBubbleText, _setBubbleModel } from './chat-bubble-actions.js';
import { _cacheAppendMessage } from './chat-message-cache.js';
import { buildToolRow } from '../../shared/js/chat-activity.js';
import { chatUiFlag } from '../../shared/js/app-prompts.js';

function _stripToolCalls(text) {
  const idx = text.indexOf('\n\n[Tool calls: ');
  return idx !== -1 ? text.slice(0, idx) : text;
}

// C: does this assistant text read like a real answer rather than a short
// transitional line ('Now switching to premium…')? Long text always counts;
// short text counts when it is structurally an answer (markdown heading or
// bullet list). Mirrors the backend _is_substantive_answer in loop.py.
function _isSubstantiveAnswer(text) {
  if (!text) return false;
  const stripped = String(text).replace(/[\s#>_*`\[\]()|]/g, '');
  if (stripped.length >= 200) return true;
  return /^[ \t]*#{1,4}[ \t]+\S|^[ \t]*[-*+][ \t]+\S/m.test(text);
}

function _findAgentBubbleForTurn(turnId) {
  if (!app.chatMessages) return null;
  if (turnId) {
    // Search both data-turn-id (set by WS/live path) and data-msg-id (set by
    // the DB-load / session-load path). A bubble created by one path must be
    // findable by the other, or the WS replay / reconcile poll creates a second
    // copy of the same message.
    return app.chatMessages.querySelector(
      `.chat-bubble.agent[data-turn-id="${CSS.escape(turnId)}"], .chat-bubble.agent[data-msg-id="${CSS.escape(turnId)}"]`,
    );
  }
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  return bubbles[bubbles.length - 1] || null;
}

function _fillStreamingText(container, text) {
  container.textContent = text || '';
  container.classList.remove('md');
  container.__mdSource = text || '';
  return false;
}

function _setBubbleText(bubble, text, extraClass) {
  if (!bubble) return;

  // Same-content guard: skip the DOM destroy/rebuild when a non-streaming
  // bubble already displays this exact text. Without this the reconcile loop
  // and finalize paths destroy every bubble's DOM subtree and rebuild it from
  // scratch (Markdown re-parse, DOMPurify, Lucide re-instantiation) on every
  // tick — visible as flash/re-layout of the entire transcript.
  if (extraClass !== 'streaming' && !bubble.classList.contains('streaming')) {
    const existing = _getBubbleText(bubble) || bubble.__mdSource || '';
    if (existing && existing.trim() === (text || '').trim()) return;
  }

  const directLlmSections = Array.from(
    bubble.querySelectorAll(':scope > .turn-section.llm-section'),
  );
  const activeLlmSection = directLlmSections[directLlmSections.length - 1] || null;

  // A streamed bubble already has the same section structure as a saved bubble.
  // Rewrite only its active LLM section. The previous implementation rendered
  // the new text directly into the bubble and then restored the old LLM section,
  // producing two visible copies and discarding the footer on every chunk.
  if (activeLlmSection) {
    while (activeLlmSection.firstChild) activeLlmSection.removeChild(activeLlmSection.firstChild);
    // During streaming, keep one plain text node. Parsing/sanitizing the entire
    // accumulated Markdown on every token is quadratic; finalization below runs
    // the full Markdown renderer exactly once for the authoritative response.
    const isMd = extraClass === 'streaming'
      ? _fillStreamingText(activeLlmSection, text)
      : _fillAgentBubble(activeLlmSection, text, true);
    activeLlmSection.classList.toggle('md', isMd);
    bubble.__mdSource = text || '';

    const hadPremium = bubble.classList.contains('premium');
    bubble.className = 'chat-bubble agent' + (extraClass ? ' ' + extraClass : '');
    if (isMd) bubble.classList.add('md');
    if (hadPremium) bubble.classList.add('premium');
    _applyGrouping(bubble);
    if (bubble.nextElementSibling) _applyGrouping(bubble.nextElementSibling);

    // Keep the saved-message footer visible while content grows. This is
    // idempotent: existing gutters are reused, then finalized in place.
    _addBubbleActions(bubble);
    if (extraClass !== 'streaming' && typeof app._finalizeBubbleSections === 'function') {
      app._finalizeBubbleSections(bubble);
    }
    return;
  }

  // If this bubble already has tool sections, append a NEW LLM section instead
  // of replacing content (multi-step turn: text → tools → more text).
  const hasToolSection = bubble.querySelector(':scope > .turn-section.tool-section');
  if (hasToolSection && extraClass !== 'streaming' && !bubble.classList.contains('streaming')) {
    const section = document.createElement('div');
    section.className = 'turn-section llm-section';
    const idx = bubble.querySelectorAll(':scope > .turn-section.llm-section').length;
    section.dataset.sectionIdx = String(idx);
    const isMd = _fillAgentBubble(section, text, extraClass !== 'streaming');
    if (isMd) { section.classList.add('md'); bubble.__mdSource = text; }
    bubble.appendChild(section);
    if (typeof app._wireLLMSection === 'function') app._wireLLMSection(section, bubble);
    return;
  }

  // Keep built sections, tool calls, and gutters across content rewrite.
  const keptSections = Array.from(bubble.querySelectorAll(':scope > .turn-section'));
  const keptGutters = Array.from(bubble.querySelectorAll(':scope > .turn-gutter'));
  const keptComponents = Array.from(bubble.querySelectorAll(':scope > .chat-component'));
  const hadPremium = bubble.classList.contains('premium');
  [...keptSections, ...keptGutters, ...keptComponents].forEach(el => el.remove());

  // Remove legacy footer if present
  const legacyFooter = bubble.querySelector(':scope > .bubble-actions');
  if (legacyFooter) legacyFooter.remove();

  while (bubble.firstChild) bubble.removeChild(bubble.firstChild);

  const isMd = extraClass === 'streaming'
    ? _fillStreamingText(bubble, text)
    : _fillAgentBubble(bubble, text, true);
  if (extraClass === 'streaming') {
    bubble.className = 'chat-bubble agent streaming';
  } else if (extraClass) {
    bubble.className = 'chat-bubble agent ' + extraClass;
  } else {
    bubble.className = 'chat-bubble agent';
  }
  if (isMd) bubble.classList.add('md');
  if (hadPremium) bubble.classList.add('premium');
  _applyGrouping(bubble);
  if (bubble.nextElementSibling) _applyGrouping(bubble.nextElementSibling);

  // Restore sections and gutters after the LLM body
  [...keptSections, ...keptGutters].forEach(el => bubble.appendChild(el));
  keptComponents.forEach(el => bubble.appendChild(el));

  // During streaming, skip section wiring — it will be done at finalization.
  if (extraClass !== 'streaming') {
    _addBubbleActions(bubble);
  }
}

const _wsTurnBuffers = new Map();   // turnId → accumulated content string
const _pendingStreamRenders = new Map();
let _streamRenderTimer = null;
const _STREAM_RENDER_INTERVAL_MS = 100;

function _flushStreamRenders() {
  _streamRenderTimer = null;
  const pending = Array.from(_pendingStreamRenders.entries());
  _pendingStreamRenders.clear();
  let shouldFollowTail = false;
  for (const [key, item] of pending) {
    const bubble = _findAgentBubbleForTurn(key) || item.bubble;
    if (!bubble || !bubble.isConnected) continue;
    const text = key ? (_wsTurnBuffers.get(key) || '') : (app.agentBuffer || '');
    _setBubbleText(bubble, text, 'streaming');
    _addBubbleActions(bubble);
    shouldFollowTail = true;
  }
  if (shouldFollowTail && typeof app._scrollToBottomIfNear === 'function') {
    app._scrollToBottomIfNear(
      app._chatScroller || (app.chatMessages && app.chatMessages.parentElement),
    );
  }
}

function _scheduleStreamRender(key, bubble) {
  _pendingStreamRenders.set(key || '', { bubble });
  if (_streamRenderTimer !== null) return;
  _streamRenderTimer = setTimeout(_flushStreamRenders, _STREAM_RENDER_INTERVAL_MS);
}

function _dropPendingStreamRender(key) {
  _pendingStreamRenders.delete(key || '');
  if (_pendingStreamRenders.size === 0 && _streamRenderTimer !== null) {
    clearTimeout(_streamRenderTimer);
    _streamRenderTimer = null;
  }
}

function appendStreamToActiveBubble(textChunk, turnId, createdAt) {
  if (textChunk == null) return;
  let bubble = _findAgentBubbleForTurn(turnId);
  let createdBubble = false;
  if (!bubble) {
    // The assistant interaction id is both the live turn key and the durable
    // message key. Stamping both from creation lets DB reconcile adopt this
    // exact node instead of creating a saved-message twin.
    bubble = addChatBubble(
      'agent', '', 'streaming', undefined,
      turnId || undefined, turnId || undefined, createdAt,
    );
    createdBubble = true;
    if (turnId) _wsTurnBuffers.set(turnId, '');
  }
  if (createdAt) _setBubbleCreatedAt(bubble, createdAt);
  if (bubble && app._activeTurnModel) {
    try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort); } catch (_) {}
  }
  app._turnHasBubble = true;
  if (turnId) {
    const cur = _wsTurnBuffers.get(turnId) || '';
    const next = cur + textChunk;
    _wsTurnBuffers.set(turnId, next);
  } else {
    if (app.agentBuffer === undefined) app.agentBuffer = '';
    app.agentBuffer += textChunk;
  }
  // Paint the first chunk immediately so the durable bubble structure exists
  // for controls/reconcile. Subsequent token events can arrive much faster than
  // layout, so coalesce those paints at a fixed cadence. Finalization renders
  // the complete Markdown once.
  if (createdBubble) {
    const firstText = turnId ? (_wsTurnBuffers.get(turnId) || '') : (app.agentBuffer || '');
    _setBubbleText(bubble, firstText, 'streaming');
    _addBubbleActions(bubble);
    if (typeof app._scrollToBottomIfNear === 'function') {
      app._scrollToBottomIfNear(
        app._chatScroller || (app.chatMessages && app.chatMessages.parentElement),
      );
    }
  } else {
    _scheduleStreamRender(turnId, bubble);
  }
  // Don't re-engage processing state if a stop was requested — the backend is
  // still unwinding but the user chose to stop, so keep isProcessing=false so the
  // activity indicator doesn't revert from "Stopping…" to "Writing reply…".
  if (!app._stopPending) app.isProcessing = true;
  if (turnId && app.currentSessionId) {
    _cacheAppendMessage(app.currentSessionId, {
      role: 'assistant', content: textChunk, id: turnId,
      created_at: createdAt, _streaming: true,
    });
  }
}

function finalizeAgentResponse(content, turnId, isReplayed, createdAt,
                               ownerTurnId, interactionSeq) {
  _dropPendingStreamRender(turnId);
  const text = (content || '').trim();
  // Empty response with no existing bubble → nothing to render.
  // Empty response with a bubble but no tool calls → remove the bubble
  // (the agent finished with no output).
  if (!text) {
    if (turnId) _wsTurnBuffers.delete(turnId);
    const bubble = _findAgentBubbleForTurn(turnId);
    if (bubble && !bubble.querySelector('.bubble-tool-calls')) {
      const prev = bubble.previousElementSibling;
      const next = bubble.nextElementSibling;
      if (app._activeToolGroupBubble === bubble) app._activeToolGroupBubble = null;
      bubble.remove();
      if (next && typeof _applyGrouping === 'function') _applyGrouping(next);
      else if (prev && prev.classList) prev.classList.remove('grouped-open');
    }
    app.agentBuffer = '';
    app._activeToolGroupBubble = null;
    app.isProcessing = false;
    if (app.chatSend) app.chatSend.disabled = false;
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
    if (typeof app.refreshSuggestions === 'function') {
      try { app.refreshSuggestions(); } catch (_) { /* best-effort */ }
    }
    if (typeof app.refreshActiveAbilities === 'function') {
      try { app.refreshActiveAbilities(); } catch (_) { /* best-effort */ }
    }
    return;
  }

  let bubble = _findAgentBubbleForTurn(turnId);
  // Id-miss safety net: if the id lookup failed (e.g. the bubble was created
  // by the DB-load path with a different id shape), scan for an existing bubble
  // whose text matches this content. Adopt it in place instead of creating a
  // duplicate. This is the common cause of "2 copies of every message" — the
  // WS replay / reconcile poll finds no bubble by id and creates a second one.
  if (!bubble && turnId && text) {
    const all = app.chatMessages.querySelectorAll('.chat-bubble.agent');
    for (const b of all) {
      // __mdSource is the raw markdown — the authoritative pre-render text.
      // _getBubbleText returns rendered DOM text (stripped of markdown syntax).
      // Check raw first so markdown-formatted content matches correctly.
      const t = (b.__mdSource || _getBubbleText(b) || '').trim();
      if (t && t === text) {
        b.setAttribute('data-turn-id', turnId);
        b.setAttribute('data-msg-id', turnId);
        bubble = b;
        break;
      }
    }
  }
  const fresh = !bubble;
  if (fresh) {
    bubble = addChatBubble(
      'agent', content || '', undefined, undefined,
      turnId || undefined, turnId || undefined, createdAt,
    );
  }
  if (createdAt) _setBubbleCreatedAt(bubble, createdAt);
  if (bubble && Number.isFinite(Number(interactionSeq))) {
    _setBubbleSessionSeq(bubble, Number(interactionSeq));
  }
  // Tag the bubble with the model/effort that ran this turn (set in
  // chat-activity.js from the live llm_call_end event) before its footer is
  // built by _setBubbleText → _addBubbleActions.
  if (bubble && app._activeTurnModel) {
    try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort); } catch (_) {}
  }
  if (!fresh) {
    _setBubbleText(bubble, content || '');
  } else if (bubble && typeof app._addBubbleActions === 'function') {
    app._addBubbleActions(bubble);
  }
  if (typeof app.attachPendingChatComponents === 'function') app.attachPendingChatComponents(bubble);
  app._turnHasBubble = true;
  if (turnId) _wsTurnBuffers.delete(turnId);
  app.agentBuffer = '';
  app._activeToolGroupBubble = null;
  app.isProcessing = false;
  if (app.chatSend) app.chatSend.disabled = false;
  if (typeof app.populateSessionSelect === 'function') {
    try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
  }
  if (turnId && app.currentSessionId && content) {
    _cacheAppendMessage(app.currentSessionId, {
      role: 'assistant', content, id: turnId,
      created_at: createdAt, _finalized: true,
    });
  }
  if (typeof app.refreshSuggestions === 'function') {
    try { app.refreshSuggestions(); } catch (_) { /* best-effort */ }
  }
  if (typeof app.refreshActiveAbilities === 'function') {
    try { app.refreshActiveAbilities(); } catch (_) { /* best-effort */ }
  }
}

function ensureStreamingBubbleForActiveTurn(turnId, createdAt) {
  if (!turnId) return;
  // If a tool-only group already exists and is in the DOM, don't create a
  // streaming bubble — the tool group is already visible and will absorb
  // further tool calls from the WS stream.
  if (app._activeToolGroupBubble && app._activeToolGroupBubble.isConnected) {
    if (!app._stopPending) app.isProcessing = true;
    return;
  }
  let existing = _findAgentBubbleForTurn(turnId);
  if (existing) {
    _setBubbleCreatedAt(existing, createdAt);
    if (!app._stopPending) app.isProcessing = true;
    return;
  }
  // No existing bubble and no tool group — the turn hasn't produced any
  // visible content yet.  Do NOT create an empty streaming placeholder;
  // an empty bubble can stay orphaned if no text or tool calls ever arrive
  // before the run finishes (a split that disappears on refresh).  The WS
  // stream will create a bubble naturally when content arrives via
  // appendStreamToActiveBubble, or tool calls via attachToolCallsToLastBubble.
  // Just mark the processing state so the "thinking" indicator is lit.
  if (!app._stopPending) app.isProcessing = true;
}

function finalizeAgentStep(content, asstId, createdAt, ownerTurnId, interactionSeq) {
  _dropPendingStreamRender(asstId);
  const draft = asstId ? _findAgentBubbleForTurn(asstId) : null;
  if (app._showMidTurn === false) {
    if (draft && draft.classList.contains('streaming')) draft.remove();
    if (asstId) _wsTurnBuffers.delete(asstId);
    return;
  }
  const promote = chatUiFlag('classify_main_messages', true)
    && _isSubstantiveAnswer(content || '');

  if (!promote) {
    attachProgressToActivity(
      content, asstId, createdAt, ownerTurnId, interactionSeq,
    );
    app._turnHasBubble = true;
    return;
  }

  // C: substantive mid-turn text renders as its own normal bubble (the same
  // path finalizeAgentResponse uses) instead of being buried in the
  // tools/updates panel.
  const text = (content || '').trim();
  let bubble = _findAgentBubbleForTurn(asstId);
  // Id-miss safety net: same guard as finalizeAgentResponse — scan for an
  // existing bubble whose text matches before creating a duplicate.
  if (!bubble && asstId && text) {
    const all = app.chatMessages.querySelectorAll('.chat-bubble.agent');
    for (const b of all) {
      const t = (b.__mdSource || _getBubbleText(b) || '').trim();
      if (t && t === text) {
        b.setAttribute('data-turn-id', asstId);
        b.setAttribute('data-msg-id', asstId);
        bubble = b;
        break;
      }
    }
  }
  if (!text) {
    if (bubble) {
      // Never remove a bubble that already has tool calls attached — the WS
      // event stream has already promoted it to a tool-only bubble and removing
      // it would orphan those calls, causing subsequent turns to start a separate
      // group (a split that disappears on refresh). Also, during an active
      // exchange (app.isProcessing) the streaming placeholder is still waiting
      // for tool calls — the WS will promote it to tool-only at the turn boundary.
      if (bubble.querySelector('.bubble-tool-calls') || app.isProcessing) {
        if (asstId) _wsTurnBuffers.delete(asstId);
        return;
      }
      // Removing a bubble can orphan its neighbours' join state — re-derive them.
      const prev = bubble.previousElementSibling;
      const next = bubble.nextElementSibling;
      // If this bubble was the tool-group container, clear the pointer so the
      // next turn's streaming placeholder (created by ensureStreamingBubbleForActiveTurn)
      // doesn't find a stale .isConnected === false reference and creates a proper
      // replacement group instead of fragmenting into separate bubbles.
      if (app._activeToolGroupBubble === bubble) app._activeToolGroupBubble = null;
      bubble.remove();
      if (next) _applyGrouping(next);
      else if (prev && prev.classList) prev.classList.remove('grouped-open');
    }
    if (asstId) _wsTurnBuffers.delete(asstId);
    return;
  }
  const fresh = !bubble;
  if (fresh) {
    bubble = addChatBubble(
      'agent', text, undefined, undefined,
      asstId || undefined, asstId || undefined, createdAt,
    );
  }
  if (createdAt) _setBubbleCreatedAt(bubble, createdAt);
  if (bubble && app._activeTurnModel) {
    try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort); } catch (_) {}
  }
  if (!fresh) {
    _setBubbleText(bubble, text);
  } else if (bubble && typeof app._addBubbleActions === 'function') {
    app._addBubbleActions(bubble);
  }
  app._turnHasBubble = true;
  if (asstId) _wsTurnBuffers.delete(asstId);
}

function markAgentInterrupted(asstId, createdAt, interactionSeq, ownerTurnId, terminalLabel) {
  _dropPendingStreamRender(asstId);
  const draft = asstId ? _findAgentBubbleForTurn(asstId) : null;
  const partial = (
    (asstId && _wsTurnBuffers.get(asstId))
    || (draft && (_getBubbleText(draft) || draft.__mdSource))
    || ''
  ).trim();
  const entries = [];
  if (partial && app._showMidTurn !== false) {
    entries.push({ kind: 'progress', id: asstId, content: partial, createdAt });
  }
  entries.push({
    kind: 'terminal',
    id: 'stopped-' + (asstId || ownerTurnId || Date.now()),
    label: terminalLabel || 'Stopped',
    content: '',
  });
  attachActivityEntries(entries, null, {
    id: asstId,
    createdAt,
    turnId: ownerTurnId,
    interactionSeq: Number(interactionSeq),
  }, ownerTurnId);
  if (draft) draft.remove();
  if (asstId) _wsTurnBuffers.delete(asstId);
  app.agentBuffer = '';
  app.isProcessing = false;
  return;
  // Replace the streaming bubble with a clean "Stopped" confirmation.
  // The user asked for a mock agent message indicating the stop completed,
  // rather than appending "(interrupted)" to whatever partial text was there.
  let bubble = asstId ? _findAgentBubbleForTurn(asstId) : null;
  if (!bubble && app.chatMessages) {
    const streaming = app.chatMessages.querySelectorAll('.chat-bubble.agent.streaming');
    bubble = streaming[streaming.length - 1] || null;
  }
  if (bubble) {
    _setBubbleText(bubble, 'Stopped', 'interrupted');
  } else {
    // No streaming bubble to replace — create a fresh one (e.g. the stop
    // arrived before any agent text was emitted).
    bubble = addChatBubble(
      'agent', 'Stopped', 'interrupted', undefined,
      asstId || undefined, asstId || undefined, createdAt,
    );
  }
  // A stop can arrive before the first stream chunk. Keep the fallback bubble
  // addressable by the durable assistant interaction so DB-tail can update and
  // reposition this same node instead of leaving an orphan at the session end.
  if (bubble && asstId) {
    bubble.setAttribute('data-turn-id', String(asstId));
    bubble.setAttribute('data-msg-id', String(asstId));
  }
  // Preserve the earlier timestamp already carried by a real streaming bubble.
  if (bubble && !bubble.hasAttribute('data-created-at') && createdAt) {
    _setBubbleCreatedAt(bubble, createdAt);
  }
  if (bubble && Number.isFinite(sessionSeq)) _setBubbleSessionSeq(bubble, sessionSeq);
  if (asstId) _wsTurnBuffers.delete(asstId);
  app.agentBuffer = '';
  app.isProcessing = false;
  if (app.chatSend) app.chatSend.disabled = !((app.chatInput && app.chatInput.value.trim()));
}

function seedStreamingBubble(turnId, content, createdAt) {
  if (!turnId) return;
  let bubble = _findAgentBubbleForTurn(turnId);
  const text = content || '';
  // If there's no real text and a tool-only group already exists, don't create
  // a streaming bubble that would break the group — the tool calls are visible.
  if (!text && app._activeToolGroupBubble && app._activeToolGroupBubble.isConnected) return;
  if (!bubble) {
    bubble = addChatBubble('agent', text, 'streaming', undefined, turnId, turnId, createdAt);
    // addChatBubble doesn't build a footer; add it now so a row recovered from
    // the DB by the reconcile loop shows its footer on the FIRST tick, not the
    // next one (the \u2026 placeholder is skipped inside _addBubbleActions).
    _addBubbleActions(bubble);
  } else {
    _setBubbleCreatedAt(bubble, createdAt);
    _setBubbleText(bubble, text, 'streaming');
  }
  app._turnHasBubble = true;
  // Empty streaming bubble = tool-only turn — mark it so subsequent turns merge.
  if (!text) app._activeToolGroupBubble = bubble;
  _wsTurnBuffers.set(turnId, content || '');
  if (!app._stopPending) app.isProcessing = true;
}

// "N tool calls" label on the collapsible head, recomputed whenever rows change.
function _updateToolCallsHead(head, panel) {
  const rows = Array.from(panel.children);
  const n = rows.filter(row => !row.classList.contains('ca-progress-row')
    && !row.classList.contains('ca-terminal-row')).length;
  const updates = rows.filter(row => row.classList.contains('ca-progress-row')).length;
  head.innerHTML = (n === 1 ? '1 tool call' : n + ' tool calls')
    + (updates ? ' / ' + updates + (updates === 1 ? ' update' : ' updates') : '')
    + ' <span class="bubble-tool-calls-chevron" aria-hidden="true">\u203A</span>';
}

// ── Tool-calls heading delete button ────────────────────────────────────────
// Two-click confirm that recycles ALL assistant interactions whose tool children
// appear in this container (collected from container.__calls._detailMsgId).

// Collect unique parent interaction IDs represented in this tool-calls container.
function _toolCallsInteractionIds(container) {
  const ids = new Set();
  const calls = container && container.__calls;
  if (Array.isArray(calls)) {
    calls.forEach(c => { if (c && c._detailMsgId) ids.add(c._detailMsgId); });
  }
  // Fallback: use the bubble's id
  if (ids.size === 0) {
    const bubble = container && container.closest('.chat-bubble');
    const id = bubble && (bubble.getAttribute('data-msg-id') || bubble.getAttribute('data-turn-id'));
    if (id) ids.add(id);
  }
  return [...ids];
}

async function _deleteToolCallsInteractions(container, permanent) {
  const ids = _toolCallsInteractionIds(container);
  if (!ids.length) return false;
  const sid = app.currentSessionId;
  if (!sid) return false;
  const uid = app.currentUserId || '';
  let ok = true;
  for (const id of ids) {
    try {
      const url = apiPath('/api/v1/db/interaction?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(id)
        + '&include_children=true'
        + (permanent ? '&permanent=true' : '')
        + (uid ? '&user_id=' + encodeURIComponent(uid) : ''));
      const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
      if (!resp.ok) ok = false;
    } catch (_) { ok = false; }
  }
  return ok;
}

async function _restoreToolCallsInteractions(container) {
  const ids = _toolCallsInteractionIds(container);
  if (!ids.length) return false;
  const sid = app.currentSessionId;
  if (!sid) return false;
  const uid = app.currentUserId || '';
  let ok = true;
  for (const id of ids) {
    try {
      const url = apiPath('/api/v1/db/interaction/restore?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(id)
        + '&include_children=true'
        + (uid ? '&user_id=' + encodeURIComponent(uid) : ''));
      const resp = await fetch(url, { method: 'POST', headers: { ...authHeaders() } });
      if (!resp.ok) ok = false;
    } catch (_) { ok = false; }
  }
  return ok;
}

function _makeToolCallsDeleteBtn(container) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-tool-calls-delete';
  btn.title = 'Delete all tool calls in this group';
  btn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
  let _armedTimer = null;

  function _reset() {
    if (_armedTimer) { clearTimeout(_armedTimer); _armedTimer = null; }
    btn.dataset.state = '';
    btn.title = 'Delete all tool calls in this group';
    btn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
    btn.classList.remove('warning');
  }

  btn.addEventListener('click', async (e) => {
    e.stopPropagation(); // don't toggle the panel
    if (btn.dataset.state !== 'warning') {
      document.querySelectorAll('.bubble-tool-calls-delete[data-state="warning"]').forEach(b => {
        b.dataset.state = '';
        b.title = 'Delete all tool calls in this group';
        b.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
        b.classList.remove('warning');
      });
      btn.dataset.state = 'warning';
      btn.title = 'Click again to delete all tool calls';
      btn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      btn.classList.add('warning');
      if (_armedTimer) clearTimeout(_armedTimer);
      _armedTimer = setTimeout(_reset, 4000);
      return;
    }
    if (_armedTimer) { clearTimeout(_armedTimer); _armedTimer = null; }

    const ok = await _deleteToolCallsInteractions(container, false);
    if (!ok) { _reset(); return; }
    // Mark the tool-calls container + all rows as deleted
    container.classList.add('deleted');
    container.querySelectorAll('.ca-tool-row').forEach(row => row.classList.add('deleted'));
    // Replace delete btn with restore + permanent-delete
    _injectToolCallsDeletedActions(container, btn);
  });
  return btn;
}

// Replace the delete button with restore + permanent-delete in a deleted tool-calls heading.
function _injectToolCallsDeletedActions(container, oldBtn) {
  if (!container || !oldBtn) return;
  const head = container.querySelector('.bubble-tool-calls-head');
  if (!head) return;
  oldBtn.remove();

  const wrap = document.createElement('span');
  wrap.style.cssText = 'display:inline-flex;gap:3px;margin-left:auto;flex:0 0 auto;';

  const restoreBtn = document.createElement('button');
  restoreBtn.type = 'button';
  restoreBtn.className = 'bubble-tool-calls-delete';
  restoreBtn.title = 'Restore all tool calls';
  restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
  restoreBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (restoreBtn.dataset.state !== 'warning') {
      restoreBtn.dataset.state = 'warning';
      restoreBtn.title = 'Click again to restore';
      restoreBtn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      restoreBtn.classList.add('warning');
      const t = setTimeout(() => {
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore all tool calls';
        restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
        restoreBtn.classList.remove('warning');
      }, 4000);
      restoreBtn._timer = t;
      return;
    }
    if (restoreBtn._timer) { clearTimeout(restoreBtn._timer); restoreBtn._timer = null; }
    const ok = await _restoreToolCallsInteractions(container);
    if (ok) {
      container.classList.remove('deleted');
      container.querySelectorAll('.ca-tool-row').forEach(row => row.classList.remove('deleted'));
      wrap.remove();
      const newBtn = _makeToolCallsDeleteBtn(container);
      head.appendChild(newBtn);
      _refreshLucideIcons(head);
    } else {
      restoreBtn.dataset.state = '';
      restoreBtn.title = 'Restore all tool calls';
      restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
      restoreBtn.classList.remove('warning');
    }
  });
  wrap.appendChild(restoreBtn);

  const permBtn = document.createElement('button');
  permBtn.type = 'button';
  permBtn.className = 'bubble-tool-calls-delete';
  permBtn.title = 'Permanently delete all tool calls';
  permBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
  permBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (permBtn.dataset.state !== 'warning') {
      permBtn.dataset.state = 'warning';
      permBtn.title = 'Click again to permanently erase';
      permBtn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      permBtn.classList.add('warning');
      const t = setTimeout(() => {
        permBtn.dataset.state = '';
        permBtn.title = 'Permanently delete all tool calls';
        permBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
        permBtn.classList.remove('warning');
      }, 4000);
      permBtn._timer = t;
      return;
    }
    if (permBtn._timer) { clearTimeout(permBtn._timer); permBtn._timer = null; }
    const ok = await _deleteToolCallsInteractions(container, true);
    if (ok) container.remove();
  });
  wrap.appendChild(permBtn);

  head.appendChild(wrap);
}

// Append tool-call rows to an existing panel (used for both first render and
// merging later turns into the same grouped bubble). `entry.open` is honoured so
// a lazy-detail rebuild can restore which rows were expanded.
function _buildActivityEntryRow(entry) {
  const row = document.createElement('div');
  const terminal = entry.kind === 'terminal';
  row.className = 'ca-tool-row ' + (terminal ? 'ca-terminal-row' : 'ca-progress-row');
  if (entry.id) row.dataset.activityEntryId = String(entry.id);

  const head = document.createElement('div');
  head.className = 'ca-activity-entry-head';
  const icon = document.createElement('span');
  icon.className = 'ca-activity-entry-icon';
  icon.textContent = terminal ? '?' : '?';
  const label = document.createElement('span');
  label.className = 'ca-activity-entry-label';
  label.textContent = terminal ? (entry.label || 'Stopped') : 'Agent update';
  head.append(icon, label);
  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ca-activity-entry-body';
  if (terminal && !(entry.content || '').trim()) {
    body.textContent = entry.label || 'Stopped';
  } else {
    _fillAgentBubble(body, entry.content || '', false);
  }
  row.appendChild(body);

  // Updates are assistant messages in their own right. Give each one the
  // response-style footer, rather than placing a footer above the disclosure.
  if (!terminal) {
    const footer = document.createElement('div');
    footer.className = 'turn-gutter ca-activity-entry-footer';
    const createdAt = entry.createdAt || entry.created_at;
    const createdAtMs = typeof createdAt === 'number' ? createdAt : Date.parse(
      String(createdAt || '').replace(' ', 'T')
        + (/(?:Z|[+-]\d{2}:?\d{2})$/i.test(String(createdAt || '')) ? '' : 'Z'),
    );
    if (Number.isFinite(createdAtMs)) {
      const time = document.createElement('span');
      time.className = 'turn-gutter-time bubble-time';
      time.setAttribute('data-created-at', String(createdAtMs));
      time.textContent = _formatRelativeTime(createdAtMs);
      footer.appendChild(time);
    }
    const addButton = (title, iconName, action) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'turn-gutter-btn';
      button.title = title;
      button.innerHTML = '<i data-lucide="' + iconName + '" style="width:14px;height:14px;"></i>';
      button.addEventListener('click', event => { event.stopPropagation(); action(button); });
      footer.appendChild(button);
    };
    addButton('Collapse update', 'chevron-down', button => {
      const collapsed = row.classList.toggle('collapsed');
      body.hidden = collapsed;
      button.title = collapsed ? 'Expand update' : 'Collapse update';
      button.innerHTML = '<i data-lucide="' + (collapsed ? 'chevron-right' : 'chevron-down') + '" style="width:14px;height:14px;"></i>';
      _refreshLucideIcons(button);
    });
    addButton('Read aloud', 'volume-2', () => {
      if (!('speechSynthesis' in window)) return;
      try {
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(new SpeechSynthesisUtterance((entry.content || '').trim()));
      } catch (_) {}
    });
    addButton('Copy text', 'copy', async button => {
      try {
        await copyText((entry.content || '').trim());
        button.title = 'Copied!';
        setTimeout(() => { button.title = 'Copy text'; }, 1200);
      } catch (_) {}
    });
    row.appendChild(footer);
    _refreshLucideIcons(footer);
  }
  return row;
}

function _appendToolRows(panel, calls) {
  const start = panel.children.length;
  calls.forEach((entry, i) => {
    const rowEntry = { ...entry, open: !!entry.open };
    if (entry && (entry.kind === 'progress' || entry.kind === 'terminal')) {
      panel.appendChild(_buildActivityEntryRow(entry));
      return;
    }
    const row = buildToolRow(rowEntry, start + i);
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
}

// Add a batch to the one shared tool-call disclosure owned by a bubble. Live
// inference turns arrive separately, but the persisted projection groups their
// calls into one summary after refresh. Reusing the bubble's existing container
// keeps both paths structurally identical while the response is still running.
function _appendCallsToToolContainer(container, calls) {
  if (!container || !calls || calls.length === 0) return false;
  const panel = container.querySelector('.bubble-tool-calls-panel');
  const head = container.querySelector('.bubble-tool-calls-head');
  if (!panel || !head) return false;
  const knownEntries = new Set(
    Array.from(panel.querySelectorAll('[data-activity-entry-id]'))
      .map(row => row.dataset.activityEntryId),
  );
  const knownTools = new Set(
    (container.__calls || []).map(c => c && c.toolCallId).filter(Boolean),
  );
  const fresh = calls.filter(entry => {
    if (!entry) return false;
    if (entry.id && knownEntries.has(String(entry.id))) return false;
    if (entry.toolCallId && knownTools.has(String(entry.toolCallId))) return false;
    return true;
  });
  if (!fresh.length) return true;
  _appendToolRows(panel, fresh);
  _updateToolCallsHead(head, panel);
  if (Array.isArray(container.__calls)) container.__calls.push(...fresh);
  else container.__calls = fresh.slice();
  if (fresh.some(c => c && c._needsDetail)) container.__detailLoaded = false;
  if (container.classList.contains('open')) {
    panel.style.maxHeight = Math.min(panel.scrollHeight, 250) + 'px';
  }
  return true;
}

// ── Lazy tool-call bodies ───────────────────────────────────────────────────
// A transcript loaded from history arrives "light": each call has its heading
// (tool name + duration) but NOT its body (arguments, result, LLM output)
// — those were never downloaded. The first time a panel is opened, fetch the
// full bodies for just that panel's turns and rebuild its rows in place.

// Rebuild a panel's rows from container.__calls, preserving which were expanded.
function _rebuildToolPanel(container) {
  const panel = container.querySelector('.bubble-tool-calls-panel');
  const head = container.querySelector('.bubble-tool-calls-head');
  if (!panel) return;
  const openIdx = new Set();
  Array.from(panel.children).forEach((row, i) => {
    if (row.classList && row.classList.contains('open')) openIdx.add(i);
  });
  const calls = container.__calls || [];
  calls.forEach((c, i) => { if (c) c.open = openIdx.has(i); });
  while (panel.firstChild) panel.removeChild(panel.firstChild);
  _appendToolRows(panel, calls);
  if (head) _updateToolCallsHead(head, panel);
}

async function _ensureToolDetail(container) {
  if (!container || container.__detailLoaded || container.__detailLoading) return;
  const calls = container.__calls;
  if (!Array.isArray(calls) || !calls.length) { container.__detailLoaded = true; return; }
  if (!calls.some(c => c && c._needsDetail)) { container.__detailLoaded = true; return; }
  const ids = [...new Set(calls.map(c => c && c._detailMsgId).filter(Boolean))];
  if (!ids.length) { container.__detailLoaded = true; return; }

  container.__detailLoading = true;
  try {
    const details = {};
    // Phase 0: keep sensitive tool arguments/results server-authoritative.
    // A policy-aware, bounded cache can be reintroduced after its retention,
    // logout purge, encryption, and invalidation contract is defined.
    if (ids.length) {
      const sid = app.currentSessionId;
      const token = localStorage.getItem('auth_token');
      let url = apiPath(`/api/v1/db/session-turn-detail?db=user.db&session_id=${encodeURIComponent(sid)}&ids=${encodeURIComponent(ids.join(','))}`);
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Tool detail fetch failed: ${res.status}`);
      const data = await res.json();
      const fetched = (data && data.details) || {};
      for (const [id, detail] of Object.entries(fetched)) {
        details[id] = detail;
      }
    }

    // ── Apply to call objects ──
    let changed = false;
    for (const c of calls) {
      if (!c || !c._needsDetail) continue;
      const d = details[c._detailMsgId];
      if (!d) continue;
      // Arguments + full LLM output come from the assistant's saved output.
      let outObj = null;
      if (d.output) { try { outObj = JSON.parse(d.output); } catch (_) {} }
      const tc = outObj && Array.isArray(outObj.tool_calls) ? outObj.tool_calls[c._detailIdx] : null;
      if (tc && tc.function) { try { c.args = JSON.parse(tc.function.arguments); } catch (_) { c.args = {}; } }
      c._savedOutput = d.output || null;
      const tools = Array.isArray(d.tools) ? d.tools : [];
      const tr = tools.find(t => t.tool_name === c.tool) || tools[c._detailIdx] || null;
      if (tr) {
        if (tr.content != null) c.result = tr.content;
        c._savedToolOutput = tr.output || null;
        c._savedToolMetadata = tr.metadata || c._savedToolMetadata || null;
      }
      c._needsDetail = false;
      changed = true;
    }
    container.__detailLoaded = true;
    if (changed) _rebuildToolPanel(container);
  } catch (_) {
    // Leave the headings in place; a later open will retry.
  } finally {
    container.__detailLoading = false;
  }
}

// Build the "N tool calls" heading + collapsible panel for a bubble.
// Panel is closed by default (max-height: 0); clicking measures + animates
// rows in/out one at a time (~1s total). Individual rows expand independently.
function _buildToolCallsContainer(calls) {
  const container = document.createElement('div');
  container.className = 'bubble-tool-calls';
  // Keep the call objects so the bodies can be lazy-filled on first open.
  container.__calls = Array.isArray(calls) ? calls.slice() : [];

  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'bubble-tool-calls-head';
  head.setAttribute('aria-expanded', 'false');
  container.appendChild(head);

  const panel = document.createElement('div');
  panel.className = 'bubble-tool-calls-panel';
  container.appendChild(panel);

  _appendToolRows(panel, calls);
  _updateToolCallsHead(head, panel);

  let panelOpen = false;
  let _animating = false;

  head.addEventListener('click', (e) => {
    e.stopPropagation();
    if (_animating) return;
    panelOpen = !panelOpen;

    const rows = panel.querySelectorAll('.ca-tool-row');
    const stagger = rows.length > 0 ? Math.min(0.12, 1.0 / rows.length) : 0.08;
    const staggerTotalMs = rows.length * stagger * 1000;

    if (panelOpen) {
      // ── OPEN ──
      panel.style.maxHeight = Math.min(panel.scrollHeight, 250) + 'px';
      // Force layout so the transition fires from 0 → measured
      panel.offsetHeight;
      panel.classList.add('open');
      panel.classList.add('opening');
      head.setAttribute('aria-expanded', 'true');
      container.classList.add('open');
      rows.forEach((row, i) => {
        row.style.animationDelay = (i * stagger).toFixed(3) + 's';
      });
      _animating = true;
      const totalMs = staggerTotalMs + 280;
      setTimeout(() => {
        panel.classList.remove('opening');
        rows.forEach(row => { row.style.animationDelay = ''; });
        _animating = false;
      }, totalMs);
      _ensureToolDetail(container);
    } else {
      // ── CLOSE ──
      // Lock current height so the transition starts from real value
      panel.style.maxHeight = panel.offsetHeight + 'px';
      panel.offsetHeight;
      panel.classList.remove('open');
      panel.classList.add('closing');
      head.setAttribute('aria-expanded', 'false');
      container.classList.remove('open');
      Array.from(rows).reverse().forEach((row, i) => {
        row.style.animationDelay = (i * stagger).toFixed(3) + 's';
      });
      _animating = true;
      const totalMs = staggerTotalMs + 280;
      setTimeout(() => {
        panel.classList.remove('closing');
        panel.style.maxHeight = '0px';
        // After the height transition finishes, clean up
        setTimeout(() => {
          panel.style.maxHeight = '';
          rows.forEach(row => { row.style.animationDelay = ''; });
          _animating = false;
        }, 750);
      }, totalMs);
    }
  });

  return container;
}

function _addToolCallsToBubble(bubble, calls) {
  // A response may span several streamed inference turns. They all belong to
  // the same agent bubble, so extend its existing disclosure instead of adding
  // one "N tool calls" line per turn.
  const existing = bubble.querySelector(':scope > .turn-section.tool-section .bubble-tool-calls');
  if (existing && _appendCallsToToolContainer(existing, calls)) return;

  // Wrap tool calls in a turn-section and append to the bubble, before any
  // existing footer (which will be removed by _addBubbleActions).
  const section = document.createElement('div');
  section.className = 'turn-section tool-section';
  const container = _buildToolCallsContainer(calls);
  section.appendChild(container);
  bubble.appendChild(section);

  // Tool-only groups are disclosures, not empty LLM responses. Remove their
  // placeholder section and any normal response gutter it created.
  if (bubble.classList.contains('tool-only')) {
    bubble.querySelectorAll(':scope > .turn-section.llm-section').forEach(llm => {
      if (!llm.textContent.trim()) {
        const gutter = llm.nextElementSibling;
        if (gutter && gutter.classList.contains('turn-gutter')) gutter.remove();
        llm.remove();
      }
    });
  }

  // Wire the preceding LLM section if it hasn't been wired yet
  const llmSections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
  const lastLLM = llmSections[llmSections.length - 1];
  if (lastLLM && typeof app._wireLLMSection === 'function') {
    app._wireLLMSection(lastLLM, bubble);
  }
}

function _activityGroupForTurn(ownerTurnId) {
  if (!app.chatMessages) return null;
  const active = app._activeToolGroupBubble;
  if (!ownerTurnId && active && active.isConnected) return active;
  if (active && active.isConnected
      && active.dataset.activityTurnId === String(ownerTurnId || '')) return active;
  if (ownerTurnId) {
    const groups = app.chatMessages.querySelectorAll(
      `.chat-bubble.activity-group[data-activity-turn-id="${CSS.escape(String(ownerTurnId))}"]`,
    );
    // Live state can be reset between inference steps. Recover the latest
    // disclosure owned by this user turn instead of creating a second one.
    if (groups.length) return groups[groups.length - 1];
  }
  return null;
}

function _coalesceLiveActivityGroups() {
  if (!app.chatMessages) return null;
  const users = app.chatMessages.querySelectorAll(':scope > .chat-bubble.user');
  const boundary = users.length ? users[users.length - 1] : null;
  if (!boundary) {
    const active = app._activeToolGroupBubble;
    return active && active.isConnected ? active : null;
  }
  const groups = [];
  let node = boundary.nextElementSibling;
  while (node) {
    if (node.classList && node.classList.contains('activity-group')) groups.push(node);
    node = node.nextElementSibling;
  }
  if (!groups.length) return null;

  // The first disclosure owns the chronological position. Fold every later
  // live disclosure into it in DOM order, then remove the redundant shells.
  const primary = groups[0];
  for (const extra of groups.slice(1)) {
    const calls = Array.from(extra.querySelectorAll('.bubble-tool-calls'))
      .flatMap(container => Array.isArray(container.__calls) ? container.__calls : []);
    if (calls.length) {
      const container = primary.querySelector('.bubble-tool-calls');
      _appendCallsToToolContainer(container, calls);
    }
    extra.remove();
  }
  const boundaryId = boundary && (
    boundary.dataset.msgId || boundary.dataset.turnId || boundary.dataset.renderOrder
  );
  if (boundaryId) primary.dataset.activityBoundaryId = String(boundaryId);
  app._activeToolGroupBubble = primary;
  return primary;
}

function attachActivityEntries(entries, targetBubble, anchor, ownerTurnId) {
  if (!entries || !entries.length || !app.chatMessages) return null;
  const owner = ownerTurnId || (anchor && anchor.turnId) || '';
  const anchorId = anchor && (anchor.id || anchor.asstId);
  const activityId = (anchor && anchor.activityGroupId)
    || ('activity-' + (anchorId || owner || ''));
  const persistedSegment = !!(anchor && anchor.activityGroupId);
  let bubble = targetBubble;
  if (!bubble && !persistedSegment) bubble = _coalesceLiveActivityGroups();
  if (!bubble && activityId !== 'activity-') {
    const key = CSS.escape(String(activityId));
    bubble = app.chatMessages.querySelector(
      '.chat-bubble.activity-group[data-msg-id="' + key + '"], '
        + '.chat-bubble.activity-group[data-turn-id="' + key + '"]',
    );
  }
  // Persisted projection supplies an explicit segment id. In that mode, never
  // reuse the merely-active group: the same user turn may have been split by a
  // recovery/system marker and each contiguous segment must retain its place.
  if (!bubble && !persistedSegment) {
    bubble = _activityGroupForTurn(owner);
  }
  if (!bubble) {
    const newActivityId = activityId === 'activity-' ? 'activity-' + Date.now() : activityId;
    bubble = addChatBubble(
      'agent', '', 'tool-only activity-group', undefined,
      newActivityId, newActivityId, anchor && anchor.createdAt,
    );
  }
  bubble.classList.add('activity-group', 'tool-only');
  if (owner) bubble.dataset.activityTurnId = String(owner);
  app._activeToolGroupBubble = bubble;
  _addToolCallsToBubble(bubble, entries);
  _anchorToolBubble(bubble, anchor);
  return bubble;
}

function attachProgressToActivity(content, asstId, createdAt, ownerTurnId, interactionSeq) {
  const text = (content || '').trim();
  const draft = asstId ? _findAgentBubbleForTurn(asstId) : null;
  if (!text) {
    if (draft && draft.classList.contains('streaming')) draft.remove();
    return null;
  }
  const anchor = {
    id: asstId,
    createdAt,
    turnId: ownerTurnId,
    interactionSeq: Number(interactionSeq),
  };
  const carried = draft
    ? Array.from(draft.querySelectorAll('.bubble-tool-calls'))
      .flatMap(container => Array.isArray(container.__calls) ? container.__calls : [])
    : [];
  const entries = [{ kind: 'progress', id: asstId, content: text, createdAt }, ...carried];
  if (draft && app._activeToolGroupBubble === draft) {
    app._activeToolGroupBubble = null;
  }
  const group = attachActivityEntries(
    entries,
    null,
    anchor,
    ownerTurnId,
  );
  if (draft && draft !== group) draft.remove();
  if (asstId) _wsTurnBuffers.delete(asstId);
  return group;
}
function _anchorToolBubble(bubble, anchor) {
  if (!bubble || !anchor) return;
  const anchorId = anchor.id || anchor.asstId;
  if (anchorId && !bubble.hasAttribute('data-msg-id')) {
    bubble.setAttribute('data-msg-id', String(anchorId));
  }
  if (anchor.createdAt && !bubble.hasAttribute('data-created-at')) {
    _setBubbleCreatedAt(bubble, anchor.createdAt);
  }
  const durableSeq = Number.isFinite(anchor.interactionSeq)
    ? anchor.interactionSeq : anchor.sessionSeq;
  if (Number.isFinite(durableSeq)) {
    const existing = bubble.hasAttribute('data-session-seq')
      ? Number(bubble.getAttribute('data-session-seq'))
      : NaN;
    // A disclosure may combine consecutive tool-only assistant rows. Anchor
    // the combined line at the earliest durable row represented by the group.
    _setBubbleSessionSeq(
      bubble,
      Number.isFinite(existing) ? Math.min(existing, durableSeq) : durableSeq,
    );
  }
}

function attachToolCallsToLastBubble(calls, targetBubble, anchor) {
  if (!calls || calls.length === 0) return;
  if (!app.chatMessages) return;

  // When show_tool_calls is OFF, suppress the tool-call accordion entirely.
  if (!chatUiFlag('show_tool_calls', true)) return;
  // Durable live runs are grouped by their owning user turn, never by the
  // visually last bubble. This is the primary guard against late tool events
  // attaching to a previous or resumed response.
  if (!targetBubble && anchor && anchor.turnId) {
    attachActivityEntries(
      calls,
      _activityGroupForTurn(anchor.turnId),
      anchor,
      anchor.turnId,
    );
    return;
  }

  // 1. If a tool-only group already exists in the same bubble, append rows to it.
  if (!targetBubble && app._activeToolGroupBubble && app._activeToolGroupBubble.isConnected) {
    const section = app._activeToolGroupBubble.querySelector(':scope > .turn-section.tool-section');
    const container = section ? section.querySelector('.bubble-tool-calls') : app._activeToolGroupBubble.querySelector('.bubble-tool-calls');
    if (_appendCallsToToolContainer(container, calls)) {
      _anchorToolBubble(app._activeToolGroupBubble, anchor);
      return;
    }
  }

  let last = targetBubble || null;
  if (!last) {
    const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
    last = bubbles[bubbles.length - 1];
  }

  // 2. Determine if this is a tool-only turn (no target, and either no last
  //    bubble, no text was written yet, or the last bubble is a streaming
  //    placeholder with no content).
  const isToolOnlyTurn = !targetBubble && (
    !last
    || !app._turnHasBubble
    || (last.classList.contains('streaming') && !_getBubbleText(last))
  );

  if (!last && !isToolOnlyTurn) return;

  if (isToolOnlyTurn) {
    // A terminal marker belongs after the calls that were in flight when the
    // user stopped the run. Never absorb those calls into the "Stopped"/error
    // bubble; create or reuse their own durably anchored disclosure before it.
    if (last && (last.classList.contains('interrupted') || last.classList.contains('error'))) {
      last = null;
    }
    // Merge into an existing tool-call bubble group (if one is already live).
    const group = app._activeToolGroupBubble;
    if (group && group.isConnected) {
      const section = group.querySelector(':scope > .turn-section.tool-section');
      const container = section ? section.querySelector('.bubble-tool-calls') : group.querySelector('.bubble-tool-calls');
      if (_appendCallsToToolContainer(container, calls)) {
        _anchorToolBubble(group, anchor);
        return;
      }
    }
    // No existing group and no last bubble to attach to — create a fresh
    // tool-only bubble so the calls are visible during streaming (required
    // when the exchange's first turn is tool-only with no prior agent bubble).
    if (!last) {
      const anchorId = anchor && (anchor.id || anchor.asstId);
      last = addChatBubble(
        'agent', '', 'tool-only', undefined,
        anchorId || ('tool-turn-' + Date.now()), anchorId || undefined,
        anchor && anchor.createdAt,
      );
      app._turnHasBubble = true;
      app._activeToolGroupBubble = last;
      _addToolCallsToBubble(last, calls);
      _anchorToolBubble(last, anchor);
      return;
    }
    // Append a tool section to the last bubble
    if (last.classList.contains('streaming')) last.classList.remove('streaming');
    app._activeToolGroupBubble = last;
    _addToolCallsToBubble(last, calls);
    _anchorToolBubble(last, anchor);
    return;
  }

  // 3. This turn has text — append tool section to the same bubble.
  _addToolCallsToBubble(last, calls);
  _anchorToolBubble(last, anchor);
}

// Expose on app immediately for session-load.js
app.appendStreamToActiveBubble = appendStreamToActiveBubble;
app.finalizeAgentResponse = finalizeAgentResponse;
app.ensureStreamingBubbleForActiveTurn = ensureStreamingBubbleForActiveTurn;
app.seedStreamingBubble = seedStreamingBubble;
app.finalizeAgentStep = finalizeAgentStep;
app.markAgentInterrupted = markAgentInterrupted;
app.attachToolCallsToLastBubble = attachToolCallsToLastBubble;
app.attachActivityEntries = attachActivityEntries;
app.attachProgressToActivity = attachProgressToActivity;

export {
  _stripToolCalls,
  _isSubstantiveAnswer,
  appendStreamToActiveBubble,
  finalizeAgentResponse,
  ensureStreamingBubbleForActiveTurn,
  finalizeAgentStep,
  markAgentInterrupted,
  seedStreamingBubble,
  attachToolCallsToLastBubble,
  _findAgentBubbleForTurn,
  attachActivityEntries,
  attachProgressToActivity,
  _wsTurnBuffers,
  _setBubbleText,
};
