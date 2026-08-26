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
import { _addBubbleActions, _getBubbleText, _setBubbleModel, _makeBubbleMoreBtn, _makeBubbleDeleteBtn } from './chat-bubble-actions.js';
import { _cacheAppendMessage, persistModeNoticeToCache } from './chat-message-cache.js';
import { buildToolRow } from '../../shared/js/chat-activity.js';
import { chatUiFlag } from '../../shared/js/app-prompts.js';
import { isMessageTypeVisible } from '../../shared/js/chat-visibility.js';

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

  // A FINAL (non-streaming) agent message landing in a bubble that already
  // carries a tool grouping is that grouping's closer — stand its collapsed
  // preview down (see _syncToolCallPreviews). Must run BEFORE the same-content
  // guard: streaming already painted the same text, so the guard would return
  // and the marker would never be set. In fold mode the closer is a response
  // row inside the grouping instead, so this marker is not set — the preview
  // keeps showing the latest row (including the final response).
  if (extraClass !== 'streaming' && !bubble.classList.contains('streaming')) {
    if ((text || '').trim() && bubble.querySelector(':scope > .turn-section.tool-section')) {
      bubble.querySelectorAll('.bubble-tool-calls').forEach(c => { c.__closedByCloser = true; });
    }
  }

  // Same-content guard: skip the DOM destroy/rebuild when a non-streaming
  // bubble already displays this exact text. Without this the reconcile loop
  // and finalize paths destroy every bubble's DOM subtree and rebuild it from
  // scratch (Markdown re-parse, DOMPurify, Lucide re-instantiation) on every
  // tick — visible as flash/re-layout of the entire transcript.
  if (extraClass !== 'streaming' && !bubble.classList.contains('streaming')) {
    const existing = _getBubbleText(bubble) || bubble.__mdSource || '';
    if (existing && existing.trim() === (text || '').trim()) return;
  }
  // Any agent text landing after the latest tool grouping is the closer
  // message — stand down the collapsed preview (see _syncToolCallPreviews).
  _syncToolCallPreviews();

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

// A folded chat renders one durable run bubble, not one bubble per inference
// step.  Keep the most recent partial LLM output in that bubble while it is
// streaming.  It is deliberately an ephemeral entry: the completed step below
// replaces it with its durable progress/response row instead of counting the
// same text twice.
const _LIVE_PREVIEW_PREFIX = 'live-preview:';
const _livePreviewId = turnId => _LIVE_PREVIEW_PREFIX + String(turnId || 'legacy');
const _streamPreviewedTurns = new Set();

function _activityContainerForBubble(bubble) {
  return bubble && bubble.querySelector(':scope > .turn-section.tool-section .bubble-tool-calls');
}

function _removeLivePreview(turnId) {
  const id = _livePreviewId(turnId);
  _streamPreviewedTurns.delete(turnId || '');
  if (!app.chatMessages) return;
  app.chatMessages.querySelectorAll('.bubble-tool-calls').forEach(container => {
    const panel = container.querySelector('.bubble-tool-calls-panel');
    const head = container.querySelector('.bubble-tool-calls-head');
    const row = panel && panel.querySelector(
      '[data-activity-entry-id="' + CSS.escape(id) + '"]',
    );
    if (!row) return;
    row.remove();
    if (Array.isArray(container.__calls)) {
      container.__calls = container.__calls.filter(entry => entry && entry.id !== id);
    }
    if (head && panel) _updateToolCallsHead(head, panel);
  });
}

function _upsertLivePreview(content, turnId, createdAt, ownerTurnId, interactionSeq) {
  const text = String(content || '');
  if (!text || !app.chatMessages || !chatUiFlag('fold_main_messages', false)) return;
  const id = _livePreviewId(turnId);
  const anchor = {
    id,
    createdAt,
    turnId: ownerTurnId || turnId,
    interactionSeq: Number.isFinite(Number(interactionSeq)) ? Number(interactionSeq) : undefined,
  };
  let bubble = _activityGroupForTurn(anchor.turnId);
  // Never append a live preview to a previous run's active bubble.  The only
  // safe fallback is an unscoped fresh bubble (used by legacy events without
  // a server turn id), which will be coalesced once its durable anchor arrives.
  if (!bubble || !bubble.isConnected) {
    bubble = attachActivityEntries(
      [{ kind: 'progress', id, content: text, createdAt, livePreview: true }],
      null, anchor, anchor.turnId,
    );
    return;
  }
  const container = _activityContainerForBubble(bubble);
  const panel = container && container.querySelector('.bubble-tool-calls-panel');
  const head = container && container.querySelector('.bubble-tool-calls-head');
  const existing = panel && panel.querySelector(
    '[data-activity-entry-id="' + CSS.escape(id) + '"]',
  );
  const entry = { kind: 'progress', id, content: text, createdAt, livePreview: true };
  if (!existing || !panel || !head) {
    attachActivityEntries([entry], bubble, anchor, anchor.turnId);
    return;
  }
  const replacement = _buildActivityEntryRow(entry);
  existing.replaceWith(replacement);
  if (Array.isArray(container.__calls)) {
    const idx = container.__calls.findIndex(item => item && item.id === id);
    if (idx >= 0) container.__calls[idx] = entry;
  }
  _updateToolCallsHead(head, panel);
}

function _flushStreamRenders() {
  _streamRenderTimer = null;
  const pending = Array.from(_pendingStreamRenders.entries());
  _pendingStreamRenders.clear();
  let shouldFollowTail = false;
  for (const [key, item] of pending) {
    const text = key ? (_wsTurnBuffers.get(key) || '') : (app.agentBuffer || '');
    if (item.foldPreview) {
      _upsertLivePreview(text, key, item.createdAt, item.ownerTurnId, item.interactionSeq);
      shouldFollowTail = true;
      continue;
    }
    const bubble = _findAgentBubbleForTurn(key) || item.bubble;
    if (!bubble || !bubble.isConnected) continue;
    _setBubbleText(bubble, text, 'streaming');
    _addBubbleActions(bubble);
    if (app._activeTurnModel) {
      try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort); } catch (_) {}
    }
    shouldFollowTail = true;
  }
  if (shouldFollowTail && typeof app._scrollToBottomIfNear === 'function') {
    app._scrollToBottomIfNear(
      app._chatScroller || (app.chatMessages && app.chatMessages.parentElement),
    );
  }
}

function _scheduleStreamRender(key, bubble, options = {}) {
  _pendingStreamRenders.set(key || '', { bubble, ...options });
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

function appendStreamToActiveBubble(textChunk, turnId, createdAt, ownerTurnId, interactionSeq) {
  if (textChunk == null) return;
  // Hidden 'main' lane (user filtered agent replies) — still accumulate the
  // buffer/cache and keep processing state, but never paint the bubble. A
  // visibility re-toggle re-renders the transcript from the cache.
  const hidden = !isMessageTypeVisible('main');
  // Folding mode (fold_main_messages): main replies are folded into the
  // tools/updates group at finalize, so a streaming draft bubble is never
  // painted — text still accumulates in _wsTurnBuffers and lands in the
  // group as a response row when the turn finalizes.
  const fold = !hidden && chatUiFlag('fold_main_messages', false);
  const noPaint = hidden || fold;
  let bubble = noPaint ? null : _findAgentBubbleForTurn(turnId);
  let createdBubble = false;
  if (!bubble && !noPaint) {
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
  if (fold) {
    // Show the first real LLM text promptly, then coalesce token bursts at the
    // same 10fps cadence as a normal streaming bubble.  This preserves a
    // stable live preview without reflowing the transcript on every token.
    const firstPreview = !_streamPreviewedTurns.has(turnId || '');
    if (firstPreview) {
      _streamPreviewedTurns.add(turnId || '');
      _upsertLivePreview(
        turnId ? (_wsTurnBuffers.get(turnId) || '') : (app.agentBuffer || ''),
        turnId, createdAt, ownerTurnId, interactionSeq,
      );
    } else {
      _scheduleStreamRender(turnId, null, {
        foldPreview: true, createdAt, ownerTurnId, interactionSeq,
      });
    }
  } else if (createdBubble) {
    const firstText = turnId ? (_wsTurnBuffers.get(turnId) || '') : (app.agentBuffer || '');
    _setBubbleText(bubble, firstText, 'streaming');
    _addBubbleActions(bubble);
    if (app._activeTurnModel) {
      try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort); } catch (_) {}
    }
    if (typeof app._scrollToBottomIfNear === 'function') {
      app._scrollToBottomIfNear(
        app._chatScroller || (app.chatMessages && app.chatMessages.parentElement),
      );
    }
  } else if (!noPaint) {
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
  _removeLivePreview(turnId);
  const text = (content || '').trim();
  // Hidden 'main' lane: skip all bubble DOM work but keep the state cleanup
  // below (buffers, isProcessing, send re-enable) so the UI never sticks.
  const hidden = !isMessageTypeVisible('main');
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

  // Folding mode: the final reply becomes a 'response' row inside the same
  // tools/updates group (head label: "N tool calls / M updates / K responses")
  // instead of its own main bubble. attachActivityEntries reuses the live
  // group for this turn or creates one; entry ids make it idempotent.
  const fold = !hidden && chatUiFlag('fold_main_messages', false);
  if (fold) {
    attachActivityEntries(
      [{ kind: 'response', id: turnId, content: text, createdAt }],
      null,
      {
        id: turnId,
        createdAt,
        turnId: ownerTurnId,
        interactionSeq: Number.isFinite(Number(interactionSeq))
          ? Number(interactionSeq) : undefined,
      },
      ownerTurnId,
    );
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
      try { app.refreshSuggestions(); } catch (_) {}
    }
    if (typeof app.refreshActiveAbilities === 'function') {
      try { app.refreshActiveAbilities(); } catch (_) {}
    }
    return;
  }

  let bubble = hidden ? null : _findAgentBubbleForTurn(turnId);
  // Id-miss safety net: if the id lookup failed (e.g. the bubble was created
  // by the DB-load path with a different id shape), scan for an existing bubble
  // whose text matches this content. Adopt it in place instead of creating a
  // duplicate. This is the common cause of "2 copies of every message" — the
  // WS replay / reconcile poll finds no bubble by id and creates a second one.
  if (!bubble && turnId && text && !hidden) {
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
  if (!hidden && fresh) {
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
  if (bubble && typeof app.attachPendingChatComponents === 'function') app.attachPendingChatComponents(bubble);
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
  _removeLivePreview(asstId);
  // Hidden 'progress' lane: drop any streaming draft so it can't linger, then
  // skip the step-bubble render (a re-toggle re-renders from the cache).
  if (!isMessageTypeVisible('progress')) {
    const _d = asstId ? _findAgentBubbleForTurn(asstId) : null;
    if (_d && _d.classList.contains('streaming')) _d.remove();
    if (asstId) _wsTurnBuffers.delete(asstId);
    return;
  }
  const draft = asstId ? _findAgentBubbleForTurn(asstId) : null;
  if (app._showMidTurn === false) {
    if (draft && draft.classList.contains('streaming')) draft.remove();
    if (asstId) _wsTurnBuffers.delete(asstId);
    return;
  }
  const promote = chatUiFlag('classify_main_messages', true)
    && _isSubstantiveAnswer(content || '');

  // Folding mode: substantive mid-turn text folds into the tools/updates
  // group as a response row instead of promoting to its own bubble.
  if (promote && isMessageTypeVisible('main') && chatUiFlag('fold_main_messages', false)) {
    if (draft && draft.classList.contains('streaming')) draft.remove();
    attachActivityEntries(
      [{ kind: 'response', id: asstId, content: (content || '').trim(), createdAt }],
      null,
      {
        id: asstId,
        createdAt,
        turnId: ownerTurnId,
        interactionSeq: Number.isFinite(Number(interactionSeq))
          ? Number(interactionSeq) : undefined,
      },
      ownerTurnId,
    );
    app._turnHasBubble = true;
    if (asstId) _wsTurnBuffers.delete(asstId);
    return;
  }

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

function markAgentInterrupted(asstId, createdAt, interactionSeq, ownerTurnId, statusLabel) {
  // The transcript "Stopped" message is the terminal trigger: the moment it
  // renders (live WS event OR DB reconcile fallback), release the stop-pin so
  // "Stopping…" can never outlive the turn it belongs to.
  app._stopPending = false;
  _dropPendingStreamRender(asstId);
  _removeLivePreview(asstId);
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
    kind: 'system',
    id: 'stopped-' + (asstId || ownerTurnId || Date.now()),
    label: statusLabel || 'Stopped',
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

function notifyExecutionMode(mode, anchor) {
  // Live "Ask mode enabled" / "Plan mode enabled" / "Auto mode enabled" notice
  // in the transcript — the mode-switch twin of the "Stopped" system row. A
  // warning-themed row rendered through the same activity machinery, so an
  // agent-driven switch (set_execution_mode tool mid-run) lands between the
  // tool calls of the turn that flipped it (anchor.turnId), while a user pill
  // click appends at the live activity group.
  const labels = {
    ask: 'Ask mode enabled',
    plan: 'Plan mode enabled',
    auto: 'Auto mode enabled',
    wkspc: 'Workspace mode enabled',
  };
  try {
    const agents = window.__agentsSharedData && window.__agentsSharedData.agents;
    const active = Array.isArray(agents) ? agents.find(a => a && a.id === app.currentAgentId) : null;
    (active?.execution_modes || []).forEach(item => {
      if (item?.id) labels[item.id] = `${item.label || item.id} mode enabled`;
    });
  } catch (_) { /* fall back to the mode id */ }
  const label = labels[mode]
    || ((mode ? String(mode) : '') + ' mode enabled');
  const entry = {
    kind: 'system',
    notice: 'mode',
    // status- prefix: the notice is a live-only row, not a persisted
    // interaction — the footer must not offer a delete button for it. Stable
    // per (mode, turn) so repeated flips within one turn don't stack rows.
    id: 'status-mode-' + (mode || 'x') + '-' + ((anchor && anchor.turnId) || Date.now()),
    label,
    content: '',
  };
  attachActivityEntries([entry], null, anchor || null, anchor && anchor.turnId);

  // Durable twin: in browser-authority mode the whole transcript lives in
  // IndexedDB, so the notice must be written there too or a reload loses it
  // (server/hybrid modes persist via the server system:mode row instead).
  // Best-effort and async — never blocks the live notice above.
  try {
    persistModeNoticeToCache(app.currentSessionId, {
      id: entry.id,
      role: 'system',
      content: label,
      source: 'system:mode',
      metadata: JSON.stringify({ mode, reason: (anchor && anchor.reason) || '' }),
      turn_id: (anchor && anchor.turnId) || null,
      status: 'complete',
    });
  } catch (_) { /* non-fatal */ }
}

function notifyModelSwitch(label, anchor) {
  // Live "Switched to <model>" / "Reverted to the default model" notice in the
  // transcript — the model-switch twin of the mode notice, rendered through the
  // same activity machinery. An agent-driven switch (set_model /
  // use_premium_model / reset_to_default mid-run) lands between that turn's
  // tool calls (anchor.turnId); a user footer-picker selection appends at the
  // live activity group.
  const text = String(label || '').trim() || 'Model switched';
  const entry = {
    kind: 'system',
    notice: 'model',
    // status- prefix: the notice is a live-only row, not a persisted
    // interaction — the footer must not offer a delete button for it. Stable
    // per turn so repeated flips within one turn don't stack rows.
    id: 'status-model-' + ((anchor && anchor.turnId) || Date.now()),
    label: text,
    content: '',
  };
  attachActivityEntries([entry], null, anchor || null, anchor && anchor.turnId);

  // Durable twin (browser-authority mode): the whole transcript lives in
  // IndexedDB, so the notice must be written there too or a reload loses it
  // (server/hybrid modes persist via the server system:model row instead).
  // Best-effort and async — never blocks the live notice above.
  try {
    persistModeNoticeToCache(app.currentSessionId, {
      id: entry.id,
      role: 'system',
      content: text,
      source: 'system:model',
      metadata: JSON.stringify({
        initiator: (anchor && anchor.initiator) || '',
        tool: (anchor && anchor.tool) || '',
        model: (anchor && anchor.model) || '',
        slot: (anchor && anchor.slot) || '',
      }),
      turn_id: (anchor && anchor.turnId) || null,
      status: 'complete',
    });
  } catch (_) { /* non-fatal */ }
}

function seedStreamingBubble(turnId, content, createdAt) {
  if (!turnId) return;
  // Folding mode: main text is folded into the tools/updates group at
  // finalize, so a recovered streaming row never paints a draft bubble.
  if (isMessageTypeVisible('main') && chatUiFlag('fold_main_messages', false)) {
    _wsTurnBuffers.set(turnId, content || '');
    app._turnHasBubble = true;
    if (!app._stopPending) app.isProcessing = true;
    return;
  }
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
    && !row.classList.contains('ca-system-row')
    && !row.classList.contains('ca-response-row')).length;
  const updates = rows.filter(row => row.classList.contains('ca-progress-row')).length;
  const liveUpdates = rows.filter(row => row.classList.contains('ca-live-preview-row')).length;
  const responses = rows.filter(row => row.classList.contains('ca-response-row')).length;
  const modeChanges = rows.filter(row => row.classList.contains('ca-mode-row')).length;
  let label = (n === 1 ? '1 tool call' : n + ' tool calls')
    + (updates ? ' / ' + updates + (updates === 1
      ? (liveUpdates ? ' live update' : ' update') : ' updates') : '');
  if (responses) label += ' / ' + responses + (responses === 1 ? ' response' : ' responses');
  if (modeChanges) label += ' / ' + modeChanges
    + (modeChanges === 1 ? ' mode change' : ' mode changes');
  head.innerHTML = '<span class="bubble-tool-calls-label">' + label + '</span>'
    + ' <span class="bubble-tool-calls-chevron" aria-hidden="true">\u203A</span>';
  _syncToolCallPreviews();
}

// ── Collapsed preview: ONE at a time ─────────────────────────────────────────
// Only the MOST RECENT tool-call grouping in the transcript may show a
// preview, and only while no final closer message has landed after it. When a
// newer grouping appears (or the closer renders), the older preview is
// removed — so at any moment there is either exactly one preview or the final
// answer. Re-run on every head/row change and whenever agent text lands.

// Monotonic build stamp: "latest" is the newest-BUILT container, not merely
// the last one in DOM order (append/coalesce order can lag construction).
let _previewGroupSeq = 0;

function _syncToolCallPreviews() {
  if (!app.chatMessages) return;
  const containers = Array.from(app.chatMessages.querySelectorAll('.bubble-tool-calls'));
  let latest = null;
  for (const c of containers) {
    if (!latest || (c.__previewSeq || 0) > (latest.__previewSeq || 0)) latest = c;
  }
  containers.forEach(container => {
    const head = container.querySelector('.bubble-tool-calls-head');
    const panel = container.querySelector('.bubble-tool-calls-panel');
    if (!head || !panel) return;
    _setPreviewVisible(head, panel, container === latest
      && !container.__closedByCloser
      && !_hasCloserAfter(container));
  });
}

function _setPreviewVisible(head, panel, show) {
  const container = head.parentNode;
  let prevEl = container.querySelector('.bubble-tool-calls-preview');
  const source = show ? _latestActivityRow(panel) : null;
  if (!source) {
    if (prevEl) prevEl.remove();
    return;
  }
  // Reuse the EXACT row builder used for the panel entries — same classes,
  // same markdown-rendered body, same footer gutter (time + actions) — so the
  // preview is pixel-identical to the real update/response inside the panel.
  const entry = source.__entry;
  if (!entry) { if (prevEl) prevEl.remove(); return; }
  // Keep the existing preview while it mirrors the SAME source row — preserves
  // the user's scroll position inside the 5-line box across unrelated syncs.
  if (prevEl && prevEl.__sourceRow === source) return;

  const fresh = _buildActivityEntryRow(entry);
  fresh.classList.add('bubble-tool-calls-preview');
  fresh.__sourceRow = source;
  container.insertBefore(fresh, panel);
  if (prevEl) prevEl.remove();
}

// True when a REAL final agent message (the closer) renders AFTER this
// grouping's bubble — it stands the preview down. Tool-only groups, streaming
// drafts, interrupted/error markers, recovery notices and placeholder text are
// NOT closers; only a substantive final agent bubble counts.
function _hasCloserAfter(container) {
  if (!app.chatMessages) return false;
  const groupBubble = container.closest('.chat-bubble');
  if (!groupBubble || !groupBubble.isConnected) return false;
  const agents = Array.from(app.chatMessages.querySelectorAll(':scope > .chat-bubble.agent'));
  let seenGroup = false;
  for (const b of agents) {
    if (b === groupBubble) { seenGroup = true; continue; }
    if (!seenGroup) continue;
    if (b.classList.contains('tool-only')) continue;
    if (b.classList.contains('streaming')) continue;
    if (b.classList.contains('interrupted')
        || b.classList.contains('error')
        || b.classList.contains('recovery-notice')) continue;
    if (b.querySelector('.bubble-tool-calls')) continue;
    const text = (b.__mdSource || _getBubbleText(b) || '').trim();
    if (!text) continue;
    // Placeholder/punctuation-only drafts ("…", "-", ">", etc.) are not a closer.
    if (/^[\s\u2026\u00b7.\-*/_~`#>|\[\]()]{1,12}$/.test(text)) continue;
    return true;
  }
  return false;
}

// The newest update/response row (progress or response) in the panel, or null.
// Scans backwards so the most recently appended entry wins. Only LLM OUTPUT
// rows are considered — tool-call and system rows are skipped entirely (a
// grouping with no updates/responses shows no preview).
function _latestActivityRow(panel) {
  const rows = Array.from(panel.children);
  for (let i = rows.length - 1; i >= 0; i--) {
    const row = rows[i];
    if (row.classList.contains('ca-progress-row')
        || row.classList.contains('ca-response-row')) return row;
  }
  return null;
}

// Scroll a row into view inside the panel only (never the transcript
// scroller): pins the row's bottom to the panel's visible bottom edge.
function _scrollPanelToRow(panel, row) {
  if (!panel || !row || !row.parentNode) return;
  const pr = panel.getBoundingClientRect();
  const rr = row.getBoundingClientRect();
  const relTop = rr.top - pr.top + panel.scrollTop;
  const relBottom = relTop + rr.height;
  if (relTop < panel.scrollTop || relBottom > panel.scrollTop + pr.height) {
    panel.scrollTop = relBottom - pr.height;
  }
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
  const statusRow = entry.kind === 'system';
  const responseRow = entry.kind === 'response';
  const noticeRow = statusRow && (entry.notice === 'mode' || entry.notice === 'model');
  row.className = 'ca-tool-row '
    + (statusRow ? 'ca-system-row' : responseRow ? 'ca-response-row' : 'ca-progress-row')
    + (noticeRow ? (entry.notice === 'model' ? ' ca-model-row' : ' ca-mode-row') : '');
  if (entry.livePreview) row.classList.add('ca-live-preview-row');
  if (entry.id) row.dataset.activityEntryId = String(entry.id);
  // Keep the source entry on the row so the collapsed preview can rebuild the
  // EXACT same row (markdown body + footer gutter) via this same builder.
  row.__entry = entry;

  // Updates, responses, and execution-mode changes are content-only. Other
  // operational notices retain their heading because it carries independent
  // meaning beyond the body text.
  if (statusRow && entry.notice !== 'mode') {
    const head = document.createElement('div');
    head.className = 'ca-activity-entry-head';
    const icon = document.createElement('span');
    icon.className = 'ca-activity-entry-icon';
    icon.textContent = noticeRow ? '\u26A0' : '?';
    const label = document.createElement('span');
    label.className = 'ca-activity-entry-label';
    label.textContent = entry.label || 'Stopped';
    head.append(icon, label);
    row.appendChild(head);
  }

  const body = document.createElement('div');
  body.className = 'ca-activity-entry-body';
  if (statusRow && !(entry.content || '').trim()) {
    body.textContent = entry.label || 'Stopped';
  } else {
    _fillAgentBubble(body, entry.content || '', false);
  }
  row.appendChild(body);

  if (entry.notice === 'mode') {
    row.dataset.modeNoticeLabel = entry.label || '';
    return row;
  }

  // Updates are assistant messages in their own right. Give each one the SAME
  // footer as every other bubble — time, collapse, speak, copy, delete (when
  // the row has a real interaction id), and the ⋮ context menu with the
  // per-message context readout. The footer is marked always-visible
  // ('notice-gutter') so the global click-to-close handler never hides it:
  // clicking the update body must not react.
  const realId = entry.id && !String(entry.id).startsWith('status-') ? String(entry.id) : '';
  if (realId) row.dataset.msgId = realId;

  const footer = document.createElement('div');
  footer.className = 'turn-gutter ca-activity-entry-footer notice-gutter';
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
  if (realId) footer.appendChild(_makeBubbleDeleteBtn(row));
  footer.appendChild(_makeBubbleMoreBtn(footer, row));
  row.appendChild(footer);
  _refreshLucideIcons(footer);
  return row;
}

function _appendToolRows(panel, calls) {
  const start = panel.children.length;
  calls.forEach((entry, i) => {
    const rowEntry = entry;
    rowEntry.open = !!rowEntry.open;
    if (entry && (entry.kind === 'progress' || entry.kind === 'system'
        || entry.kind === 'response')) {
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
        if (rowEntry.open && rowEntry._needsDetail) {
          _ensureToolCallDetail(panel.closest('.bubble-tool-calls'), rowEntry);
        }
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
  const knownModeNotices = new Set(
    Array.from(panel.querySelectorAll('.ca-mode-row'))
      .map(row => (row.dataset.modeNoticeLabel || '').trim())
      .filter(Boolean),
  );
  const fresh = calls.filter(entry => {
    if (!entry) return false;
    if (entry.id && knownEntries.has(String(entry.id))) return false;
    if (entry.toolCallId && knownTools.has(String(entry.toolCallId))) return false;
    if (entry.notice === 'mode' && knownModeNotices.has(String(entry.label || '').trim())) {
      return false;
    }
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
    // Follow the tail: while the grouping is open, keep showing the latest.
    _scrollPanelToRow(panel, panel.lastElementChild);
  }
  _pinMirrorNoteToBottom(container.closest('.chat-bubble'));
  return true;
}

function _comparableOutput(text) {
  return String(text || '').replace(/\r\n?/g, '\n').trim();
}

// Once a durable Closer arrives, the folded response is only a preview of the
// same output. Keep the activity disclosure and its inspectable panel row, but
// remove the duplicate collapsed preview. Restrict by final interaction id when
// available so two identical short replies in different turns cannot interfere.
function suppressMatchingResponsePreview(content, finalAsstId) {
  if (!app.chatMessages) return false;
  const expected = _comparableOutput(content);
  if (!expected) return false;
  const expectedId = String(finalAsstId || '');
  let suppressed = false;
  app.chatMessages.querySelectorAll('.bubble-tool-calls').forEach(container => {
    const panel = container.querySelector('.bubble-tool-calls-panel');
    const rows = panel ? Array.from(panel.querySelectorAll(':scope > .ca-response-row')) : [];
    const match = rows.find(row => {
      const entry = row.__entry || {};
      if (expectedId && String(entry.id || '') !== expectedId) return false;
      return _comparableOutput(entry.content) === expected;
    });
    if (!match) return;
    container.__closedByCloser = true;
    const head = container.querySelector('.bubble-tool-calls-head');
    if (head && panel) _setPreviewVisible(head, panel, false);
    suppressed = true;
  });
  return suppressed;
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
  if (container.classList.contains('open')) {
    panel.style.maxHeight = Math.min(panel.scrollHeight, 250) + 'px';
    _scrollPanelToRow(panel, panel.lastElementChild);
  }
}

async function _ensureToolCallDetail(container, call) {
  if (!container || !call || !call._needsDetail || call._detailLoading) return;
  if (!call._detailMsgId && !call._detailToolId) return;
  call._detailLoading = true;
  try {
    const sid = app.currentSessionId;
    const params = new URLSearchParams({ db: 'user.db', session_id: sid });
    if (call._detailToolId) params.set('tool_id', call._detailToolId);
    else {
      params.set('assistant_id', call._detailMsgId);
      params.set('tool_index', String(call._detailIdx || 0));
    }
    if (app.currentUserId) params.set('user_id', app.currentUserId);
    const res = await fetch(apiPath(`/api/v1/db/session-tool-detail?${params}`), {
      headers: authHeaders(),
    });
    if (!res.ok) throw new Error(`Tool detail fetch failed: ${res.status}`);
    const data = await res.json();
    const detail = data && data.detail;
    if (!detail) return;
    const rawArgs = detail.arguments;
    if (typeof rawArgs === 'string') {
      try { call.args = JSON.parse(rawArgs); } catch (_) { call.args = {}; }
    } else call.args = rawArgs || {};
    call.result = detail.content != null ? detail.content : null;
    call._savedToolOutput = detail.output || null;
    call._savedToolMetadata = detail.metadata || call._savedToolMetadata || null;
    call._needsDetail = false;
    _rebuildToolPanel(container);
  } catch (_) {
    // Leave the heading in place; the next row expansion retries.
  } finally {
    call._detailLoading = false;
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
  // Build stamp — the newest-built container owns the collapsed preview.
  container.__previewSeq = ++_previewGroupSeq;

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
    // The collapsed preview is the complete current message, NOT a button.
    // Only the counts heading toggles the full event-history panel.
    if (e.target && e.target.closest && e.target.closest('.bubble-tool-calls-preview')) return;
    panelOpen = !panelOpen;
    // Opening always lands on the very latest item.
    const scrollTarget = panel.lastElementChild || null;

    const rows = panel.querySelectorAll('.ca-tool-row');
    const stagger = rows.length > 0 ? Math.min(0.12, 1.0 / rows.length) : 0.08;
    const staggerTotalMs = rows.length * stagger * 1000;

    if (panelOpen) {
      // ── OPEN ──
      // A fully closed panel is display:none so its hidden rows cannot size the
      // collapsed preview. Put it back in layout at height zero, measure it,
      // then animate to the capped open height.
      panel.style.maxHeight = '0px';
      panel.classList.add('open');
      panel.classList.add('opening');
      panel.offsetHeight;
      panel.style.maxHeight = Math.min(panel.scrollHeight, 250) + 'px';
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
        _scrollPanelToRow(panel, scrollTarget);
      }, totalMs);
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
  if (existing && _appendCallsToToolContainer(existing, calls)) {
    _pinMirrorNoteToBottom(bubble);
    return;
  }

  // Wrap tool calls in a turn-section and append to the bubble, before any
  // existing footer (which will be removed by _addBubbleActions).
  const section = document.createElement('div');
  section.className = 'turn-section tool-section';
  const container = _buildToolCallsContainer(calls);
  section.appendChild(container);
  bubble.appendChild(section);
  _pinMirrorNoteToBottom(bubble);
  // The new container is now the most recent grouping — it takes the preview
  // and any older grouping's preview is removed.
  _syncToolCallPreviews();

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
  if (ownerTurnId) {
    const groups = Array.from(app.chatMessages.querySelectorAll(
      `.chat-bubble.activity-group[data-activity-turn-id="${CSS.escape(String(ownerTurnId))}"]`,
    )).filter(group => !group.dataset.activitySegmentId);
    if (active && active.isConnected
        && active.dataset.activityTurnId === String(ownerTurnId)
        && !groups.includes(active)) groups.push(active);
    // A run owns exactly one activity bubble. Reconcile can briefly materialise
    // more than one shell when backend step ids or persisted segment ids differ;
    // fold all of them into the earliest shell before returning it.
    if (groups.length) return _coalesceActivityGroups(groups);
  }
  return null;
}

// The run buffer's turn id is the owning USER interaction id. Use that durable
// relationship directly instead of letting a provisional timestamp/render
// order place the activity group before its prompt.
function positionActivityGroupAfterOwner(ownerTurnId, bubble) {
  if (!app.chatMessages || !ownerTurnId) return bubble || null;
  const owner = String(ownerTurnId);
  const user = app.chatMessages.querySelector(
    `.chat-bubble.user[data-msg-id="${CSS.escape(owner)}"], `
      + `.chat-bubble.user[data-turn-id="${CSS.escape(owner)}"]`,
  );
  const group = bubble || _activityGroupForTurn(owner);
  if (!user || !group || !group.isConnected || group === user.nextElementSibling) return group;
  user.insertAdjacentElement('afterend', group);
  return group;
}

function _coalesceActivityGroups(groups) {
  const connected = Array.from(groups || []).filter(group => group && group.isConnected);
  if (!connected.length) return null;
  const primary = connected[0];
  for (const extra of connected.slice(1)) {
    const entries = Array.from(extra.querySelectorAll('.bubble-tool-calls'))
      .flatMap(container => Array.isArray(container.__calls) ? container.__calls : []);
    if (entries.length) {
      const container = primary.querySelector('.bubble-tool-calls');
      if (!container) {
        _addToolCallsToBubble(primary, entries);
      } else {
        _appendCallsToToolContainer(container, entries);
      }
    }
    const note = _mirrorNoteRow(extra);
    if (note && !_mirrorNoteRow(primary)) primary.appendChild(note);
    extra.remove();
  }
  app._activeToolGroupBubble = primary;
  return primary;
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
  const primary = _coalesceActivityGroups(groups);
  const boundaryId = boundary && (
    boundary.dataset.msgId || boundary.dataset.turnId || boundary.dataset.renderOrder
  );
  if (boundaryId) primary.dataset.activityBoundaryId = String(boundaryId);
  app._activeToolGroupBubble = primary;
  return primary;
}

function _persistedActivityGroupForOwner(owner) {
  if (!owner || !app.chatMessages) return null;
  const groups = Array.from(app.chatMessages.querySelectorAll(
    '.chat-bubble.activity-group',
  )).filter(group => group.parentElement === app.chatMessages);
  for (let i = groups.length - 1; i >= 0; i--) {
    const candidate = groups[i];
    if (candidate.dataset.activityTurnId !== String(owner)) continue;
    let node = candidate.nextElementSibling;
    let blocked = false;
    while (node) {
      const userBoundary = node.classList?.contains('user');
      const boundaryId = userBoundary
        ? (node.dataset.msgId || node.dataset.turnId || '') : '';
      if ((userBoundary && boundaryId !== String(owner))
          || node.classList?.contains('summary-bubble')) {
        blocked = true;
        break;
      }
      node = node.nextElementSibling;
    }
    if (!blocked) return candidate;
  }
  return null;
}

function attachActivityEntries(entries, targetBubble, anchor, ownerTurnId) {
  if (!entries || !entries.length || !app.chatMessages) return null;
  const owner = ownerTurnId || (anchor && anchor.turnId) || '';
  const anchorId = anchor && (anchor.id || anchor.asstId);
  const activityId = (anchor && anchor.activityGroupId)
    || ('activity-' + (anchorId || owner || ''));
  const persistedSegment = !!(anchor && anchor.activityGroupId);
  let bubble = targetBubble;
  // Persisted history already carries a boundary-safe segment id. Resolve that
  // exact shell before considering live owner-turn coalescing: one user turn can
  // contain several persisted phases separated by closer/summary rows.
  if (!bubble && activityId !== 'activity-') {
    const key = CSS.escape(String(activityId));
    bubble = app.chatMessages.querySelector(
      '.chat-bubble.activity-group[data-msg-id="' + key + '"], '
        + '.chat-bubble.activity-group[data-turn-id="' + key + '"]',
    );
  }
  if (!bubble && persistedSegment && anchorId) {
    // Reconcile can receive a row just after its live WS projection was drawn.
    // Adopt that provisional shell by its durable entry id, then stamp the
    // boundary-safe persisted segment id. Creating a second shell here leaves
    // one zero-tool "live" copy beside the canonical persisted disclosure.
    const entryKey = CSS.escape(String(anchorId));
    const liveEntry = app.chatMessages.querySelector(
      '.chat-bubble.activity-group:not([data-activity-segment-id]) '
        + `[data-activity-entry-id="${entryKey}"]`,
    );
    const liveBubble = liveEntry && liveEntry.closest('.chat-bubble.activity-group');
    if (liveBubble) {
      bubble = liveBubble;
      bubble.dataset.msgId = String(activityId);
      bubble.dataset.turnId = String(activityId);
    }
  }
  // Persisted reconciliation often arrives in small batches. Keep consecutive
  // batches owned by the same run in one disclosure, but never reuse a group
  // once a closer/summary or a new user row has closed that phase.
  if (!bubble && persistedSegment) bubble = _persistedActivityGroupForOwner(owner);
  if (!bubble && !persistedSegment) bubble = _activityGroupForTurn(owner);
  if (!bubble && !persistedSegment) bubble = _coalesceLiveActivityGroups();
  if (!bubble) {
    const newActivityId = activityId === 'activity-' ? 'activity-' + Date.now() : activityId;
    bubble = addChatBubble(
      'agent', '', 'tool-only activity-group', undefined,
      newActivityId, newActivityId, anchor && anchor.createdAt,
    );
  }
  bubble.classList.add('activity-group', 'tool-only');
  if (owner) bubble.dataset.activityTurnId = String(owner);
  if (persistedSegment && !bubble.dataset.activitySegmentId) {
    bubble.dataset.activitySegmentId = String(activityId);
  }
  // Live path: stamp the group with the running turn's model so each update
  // row's ⋮ menu can show it. Persisted projections are deliberately NOT
  // stamped here — app._activeTurnModel describes the CURRENT run, which would
  // mislabel older update rows (their menu reads per-message metadata instead).
  if (!persistedSegment && app._activeTurnModel) {
    try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort); } catch (_) {}
  }
  app._activeToolGroupBubble = bubble;
  _addToolCallsToBubble(bubble, entries);
  _anchorToolBubble(bubble, anchor);
  if (persistedSegment) {
    const durableEntrySeqs = entries
      .map(entry => Number(entry && entry.interactionSeq))
      .filter(Number.isFinite);
    if (durableEntrySeqs.length) {
      // The persisted segment itself, not its owning user/turn, determines its
      // durable slot. This matters when one user request contains multiple run
      // phases: stamping both groups with the owner's first sequence reverses
      // later phases on reload and lets their timestamps disagree with the DB.
      const currentSeq = Number(bubble.dataset.sessionSeq);
      const entrySeq = Math.min(...durableEntrySeqs);
      _setBubbleSessionSeq(
        bubble,
        Number.isFinite(currentSeq) ? Math.min(currentSeq, entrySeq) : entrySeq,
      );
    }
  }
  // Durable persisted rows are positioned solely by session_seq. Moving each
  // segment directly after its user would jump later phases across their closer.
  if (!persistedSegment) positionActivityGroupAfterOwner(owner, bubble);
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

// ── Live-activity mirror (mirror_activity_in_transcript) ────────────────────
// The progress text shown above the composer pill (activity bar) is mirrored
// into the transcript as an agent bubble that also carries this turn's tool
// calls and update rows. The bar is untouched — this is a deliberate duplicate.
// The mirror bubble IS the live activity group (app._activeToolGroupBubble),
// so the standard tool-attach paths (attachToolCallsToLastBubble /
// attachActivityEntries) land this turn's calls and updates inside it, and
// every row opens exactly like the other tool-call disclosures. The note row
// disappears at turn end; the bubble persists only if it holds calls/updates.

const _LIVE_ACTIVITY_CLS = 'live-activity';

function _mirrorBubble(sessionId) {
  if (!app.chatMessages) return null;
  const owner = sessionId || app.currentSessionId || '';
  const bubbles = app.chatMessages.querySelectorAll(
    ':scope > .chat-bubble.agent.activity-group.' + _LIVE_ACTIVITY_CLS,
  );
  // Prefer a bubble stamped for this session; never reuse another session's.
  for (let i = bubbles.length - 1; i >= 0; i--) {
    const b = bubbles[i];
    const sid = b.dataset.sessionId || '';
    if (owner && sid && sid !== owner) continue;
    return b;
  }
  return null;
}

function _mirrorNoteRow(bubble) {
  return bubble ? bubble.querySelector(':scope > .live-activity-note') : null;
}

// The transcript mirror is a live ticker, not durable turn history. Keep its
// one row after every LLM/tool section so it is always the newest visible item
// in the current agent output bubble.
function _pinMirrorNoteToBottom(bubble) {
  const note = _mirrorNoteRow(bubble);
  if (note && note !== bubble.lastElementChild) bubble.appendChild(note);
  return note;
}

function _retireMirrorBubble(bubble) {
  if (!bubble) return;
  const note = _mirrorNoteRow(bubble);
  if (note) note.remove();
  const hasOutput = Array.from(
    bubble.querySelectorAll(':scope > .turn-section.llm-section'),
  ).some(section => section.textContent.trim());
  if (!bubble.querySelector('.bubble-tool-calls') && !hasOutput) {
    if (app._activeToolGroupBubble === bubble) app._activeToolGroupBubble = null;
    bubble.remove();
    return;
  }
  bubble.classList.remove(_LIVE_ACTIVITY_CLS);
}

function mirrorActivityNote(text, sessionId, ownerTurnId) {
  // Config flag + progress-lane visibility gate the mirror, same as the bar's
  // rows (hide the progress lane → no mirror; re-show → it returns).
  if (!chatUiFlag('mirror_activity_in_transcript', false)) return;
  if (!isMessageTypeVisible('progress')) return;
  if (!app.chatMessages) return;
  // Session-scope: only paint the note into the transcript of the session the
  // note belongs to. A run in another session must never bleed into the view
  // on screen (covers unreset switch paths like the Sessions admin page).
  const owner = sessionId || app.currentSessionId || '';
  if (owner && app.currentSessionId && owner !== app.currentSessionId) return;
  const txt = (text || '').trim();
  if (!txt) return;

  // Prefer the current turn's activity/output bubble. If tools caused the
  // active pointer to move, migrate the singleton note instead of leaving a
  // stale progress bubble behind.
  let bubble = _activityGroupForTurn(ownerTurnId);
  if (!bubble) bubble = _mirrorBubble(owner);
  if (bubble) {
    const sid = bubble.dataset.sessionId || '';
    if (owner && sid && sid !== owner) bubble = null;
  }
  if (!bubble) {
    bubble = addChatBubble(
      'agent', '', 'tool-only activity-group ' + _LIVE_ACTIVITY_CLS, undefined,
      undefined, undefined, Date.now(),
    );
    if (owner) bubble.dataset.sessionId = owner;
    // This bubble is the live tool group for the current turn — tool calls and
    // update rows will attach here via the standard paths.
    app._activeToolGroupBubble = bubble;
  }
  bubble.classList.add(_LIVE_ACTIVITY_CLS);
  if (owner) bubble.dataset.sessionId = owner;
  app._activeToolGroupBubble = bubble;
  // The activity ticker often appears before the first assistant row is
  // durable.  Stamp it with the user-turn id as soon as we know it so the
  // streamed preview and subsequent tool calls adopt this exact bubble rather
  // than creating a visually identical sibling.
  if (ownerTurnId) bubble.dataset.activityTurnId = String(ownerTurnId);

  let note = _mirrorNoteRow(bubble);
  // There is exactly one live transcript progress row. Retire every older
  // mirror, including shells created before the active activity group changed.
  const mirrors = Array.from(app.chatMessages.querySelectorAll(
    ':scope > .chat-bubble.agent .live-activity-note',
  ));
  for (const oldNote of mirrors) {
    const oldBubble = oldNote.closest('.chat-bubble');
    if (oldBubble === bubble) continue;
    _retireMirrorBubble(oldBubble);
  }
  if (!note) {
    note = document.createElement('div');
    note.className = 'live-activity-note';
    note.title = 'Click to open this turn\'s tool calls';
    const label = document.createElement('span');
    label.className = 'live-activity-label';
    note.appendChild(label);
    note.addEventListener('click', (e) => {
      e.stopPropagation();
      // A run-level coalesce may migrate this singleton note from a redundant
      // shell into the surviving bubble. Resolve its owner at click time.
      const ownerBubble = note.closest('.chat-bubble');
      const container = ownerBubble && ownerBubble.querySelector('.bubble-tool-calls');
      const head = container && container.querySelector('.bubble-tool-calls-head');
      if (head) head.click();
    });
    bubble.appendChild(note);
  }
  const label = note.querySelector('.live-activity-label');
  if (label && label.textContent !== txt) label.textContent = txt;
  _pinMirrorNoteToBottom(bubble);
  // Keep the live group anchored to its owning turn.  Appending it to the
  // transcript tail here fights the canonical transcript reorder whenever a
  // later optimistic/durable row exists, making the two nodes oscillate.
  positionActivityGroupAfterOwner(ownerTurnId, bubble);
  if (typeof app._scrollToBottomIfNear === 'function') {
    try {
      app._scrollToBottomIfNear(
        app._chatScroller || (app.chatMessages && app.chatMessages.parentElement),
      );
    } catch (_) {}
  }
}

function mirrorActivityEnd(sessionId) {
  if (!app.chatMessages) return;
  const owner = sessionId || app.currentSessionId || '';
  const bubbles = app.chatMessages.querySelectorAll(
    ':scope > .chat-bubble.agent.activity-group.' + _LIVE_ACTIVITY_CLS,
  );
  for (const b of bubbles) {
    const sid = b.dataset.sessionId || '';
    // A bubble stamped for a DIFFERENT session is stale — remove it outright so
    // a leftover mirror can never linger in another view. (On a session switch
    // the re-render wipes the container anyway; this is defense in depth.)
    if (owner && sid && sid !== owner) { b.remove(); continue; }
    const note = _mirrorNoteRow(b);
    if (note) note.remove();
    // Keep the bubble only when it actually carries tool calls / update rows —
    // otherwise it was a bare note ticker (the bar clears too) and should go.
    if (!b.querySelector('.bubble-tool-calls')) {
      if (app._activeToolGroupBubble === b) app._activeToolGroupBubble = null;
      b.remove();
    } else {
      b.classList.remove(_LIVE_ACTIVITY_CLS);
    }
  }
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
  // Per-type visibility: hide the tool-call accordion when the user filtered
  // the 'tool' lane (storage stays complete; a re-toggle re-renders).
  if (!isMessageTypeVisible('tool')) return;
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
    // A status marker belongs after the calls that were in flight when the
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
app.notifyExecutionMode = notifyExecutionMode;
app.notifyModelSwitch = notifyModelSwitch;
app.attachToolCallsToLastBubble = attachToolCallsToLastBubble;
app.attachActivityEntries = attachActivityEntries;
app.attachProgressToActivity = attachProgressToActivity;
app.mirrorActivityNote = mirrorActivityNote;
app.mirrorActivityEnd = mirrorActivityEnd;
app.positionActivityGroupAfterOwner = positionActivityGroupAfterOwner;
app.suppressMatchingResponsePreview = suppressMatchingResponsePreview;

export {
  _stripToolCalls,
  _isSubstantiveAnswer,
  appendStreamToActiveBubble,
  finalizeAgentResponse,
  ensureStreamingBubbleForActiveTurn,
  finalizeAgentStep,
  markAgentInterrupted,
  notifyExecutionMode,
  notifyModelSwitch,
  seedStreamingBubble,
  attachToolCallsToLastBubble,
  _findAgentBubbleForTurn,
  attachActivityEntries,
  attachProgressToActivity,
  mirrorActivityNote,
  mirrorActivityEnd,
  positionActivityGroupAfterOwner,
  suppressMatchingResponsePreview,
  _wsTurnBuffers,
  _setBubbleText,
};
