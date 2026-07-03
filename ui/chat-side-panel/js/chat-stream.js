'use strict';

// Live WebSocket streaming into bubbles — accumulates chunks per turn, finalizes
// or interrupts agent steps, attaches tool-call accordion panels to agent bubbles.
// Module map for this folder: ui/chat-side-panel/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { addChatBubble, _fillAgentBubble, _formatRelativeTime, _applyGrouping } from './chat-bubble.js';
import { _addBubbleActions, _getBubbleText, _setBubbleModel } from './chat-bubble-actions.js';
import { _cacheAppendMessage } from './chat-message-cache.js';
import { buildToolRow } from '../../shared/js/chat-activity.js';

function _stripToolCalls(text) {
  const idx = text.indexOf('\n\n[Tool calls: ');
  return idx !== -1 ? text.slice(0, idx) : text;
}

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

function _setBubbleText(bubble, text, extraClass) {
  if (!bubble) return;
  // Keep an already-built footer across the content rewrite: a streaming bubble
  // is re-set on every chunk, so tearing the action row down and rebuilding it
  // (with fresh Lucide icons) each time would churn the DOM. Detach it, refill
  // the body, then re-append — _addBubbleActions reuses it when it's still there.
  const keptActions = bubble.querySelector(':scope > .bubble-actions');
  if (keptActions) keptActions.remove();
  while (bubble.firstChild) bubble.removeChild(bubble.firstChild);
  const isMd = _fillAgentBubble(bubble, text, extraClass !== 'streaming');
  if (extraClass === 'streaming') {
    bubble.className = 'chat-bubble agent streaming';
  } else if (extraClass) {
    bubble.className = 'chat-bubble agent ' + extraClass;
  } else {
    bubble.className = 'chat-bubble agent';
  }
  if (isMd) bubble.classList.add('md');
  // The className rewrite above wipes any grouped-* state — re-derive it for this
  // bubble and the one below so a streaming/finalizing step keeps joining its run.
  _applyGrouping(bubble);
  if (bubble.nextElementSibling) _applyGrouping(bubble.nextElementSibling);
  if (app.chatMessages) {
    // scroll handled by caller
  }
  if (keptActions) bubble.appendChild(keptActions);
  _addBubbleActions(bubble);
}

const _wsTurnBuffers = new Map();   // turnId → accumulated content string

function appendStreamToActiveBubble(textChunk, turnId) {
  if (textChunk == null) return;
  let bubble = _findAgentBubbleForTurn(turnId);
  if (!bubble) {
    bubble = addChatBubble('agent', '\u2026', 'streaming', undefined, turnId || undefined);
    if (turnId) _wsTurnBuffers.set(turnId, '');
  }
  app._turnHasBubble = true;
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
  if (turnId && app.currentSessionId) {
    _cacheAppendMessage(app.currentSessionId, { role: 'assistant', content: textChunk, id: turnId, _streaming: true });
  }
}

function finalizeAgentResponse(content, turnId, isReplayed) {
  let bubble = _findAgentBubbleForTurn(turnId);
  const fresh = !bubble;
  if (fresh) {
    bubble = addChatBubble('agent', content || '', undefined, undefined, turnId || undefined);
  }
  // Tag the bubble with the model/effort that ran this turn (set in
  // chat-activity.js from the live llm_call_end event) before its footer is
  // built by _setBubbleText → _addBubbleActions.
  if (bubble && app._activeTurnModel) {
    try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort); } catch (_) {}
  }
  if (!fresh) {
    _setBubbleText(bubble, content || '');
  }
  app._turnHasBubble = true;
  if (turnId) _wsTurnBuffers.delete(turnId);
  app.agentBuffer = '';
  app.isProcessing = false;
  if (app.chatSend) app.chatSend.disabled = false;
  if (typeof app.populateSessionSelect === 'function') {
    try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
  }
  if (turnId && app.currentSessionId && content) {
    _cacheAppendMessage(app.currentSessionId, { role: 'assistant', content, id: turnId, _finalized: true });
  }
  if (typeof app.refreshSuggestions === 'function') {
    try { app.refreshSuggestions(); } catch (_) { /* best-effort */ }
  }
  if (typeof app.refreshActiveAbilities === 'function') {
    try { app.refreshActiveAbilities(); } catch (_) { /* best-effort */ }
  }
}

function ensureStreamingBubbleForActiveTurn(turnId) {
  if (!turnId) return;
  let existing = _findAgentBubbleForTurn(turnId);
  if (existing) return;
  addChatBubble('agent', '\u2026', 'streaming', undefined, turnId);
  app.isProcessing = true;
}

function finalizeAgentStep(content, asstId) {
  const text = (content || '').trim();
  let bubble = _findAgentBubbleForTurn(asstId);
  if (!text) {
    if (bubble) {
      // Removing a bubble can orphan its neighbours' join state — re-derive them.
      const prev = bubble.previousElementSibling;
      const next = bubble.nextElementSibling;
      bubble.remove();
      if (next) _applyGrouping(next);
      else if (prev && prev.classList) prev.classList.remove('grouped-open');
    }
    if (asstId) _wsTurnBuffers.delete(asstId);
    return;
  }
  const fresh = !bubble;
  if (fresh) {
    bubble = addChatBubble('agent', text, undefined, undefined, asstId);
  }
  if (bubble && app._activeTurnModel) {
    try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort); } catch (_) {}
  }
  if (!fresh) {
    _setBubbleText(bubble, text);
  }
  app._turnHasBubble = true;
  if (asstId) _wsTurnBuffers.delete(asstId);
}

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

function seedStreamingBubble(turnId, content) {
  if (!turnId) return;
  let bubble = _findAgentBubbleForTurn(turnId);
  const text = content || '\u2026';
  if (!bubble) {
    bubble = addChatBubble('agent', text, 'streaming', undefined, turnId);
    // addChatBubble doesn't build a footer; add it now so a row recovered from
    // the DB by the reconcile loop shows its footer on the FIRST tick, not the
    // next one (the \u2026 placeholder is skipped inside _addBubbleActions).
    _addBubbleActions(bubble);
  } else {
    _setBubbleText(bubble, text, 'streaming');
  }
  app._turnHasBubble = true;
  _wsTurnBuffers.set(turnId, content || '');
  app.isProcessing = true;
}

// "N tool calls" label on the collapsible head, recomputed whenever rows change.
function _updateToolCallsHead(head, panel) {
  const n = panel.children.length;
  head.innerHTML = '<span class="bubble-tool-calls-icon" aria-hidden="true">\u2699</span> '
    + (n === 1 ? '1 tool call' : n + ' tool calls')
    + ' <span class="bubble-tool-calls-chevron" aria-hidden="true">\u203A</span>';
}

// Append tool-call rows to an existing panel (used for both first render and
// merging later turns into the same grouped bubble). `entry.open` is honoured so
// a lazy-detail rebuild can restore which rows were expanded.
function _appendToolRows(panel, calls) {
  const start = panel.children.length;
  calls.forEach((entry, i) => {
    const rowEntry = { ...entry, open: !!entry.open };
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
    const sid = app.currentSessionId;
    const token = localStorage.getItem('auth_token');
    let url = apiPath(`/api/v1/db/session-turn-detail?db=local.db&session_id=${encodeURIComponent(sid)}&ids=${encodeURIComponent(ids.join(','))}`);
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(url);
    const data = await res.json();
    const details = (data && data.details) || {};
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
      // Tool result — match by name (first), mirroring the history build.
      const tools = Array.isArray(d.tools) ? d.tools : [];
      const tr = tools.find(t => t.tool_name === c.tool);
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

// Build the collapsible "N tool calls" widget for a bubble.
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
  panel.hidden = true;
  container.appendChild(panel);

  _appendToolRows(panel, calls);
  _updateToolCallsHead(head, panel);

  let panelOpen = false;
  head.addEventListener('click', (e) => {
    e.stopPropagation();
    panelOpen = !panelOpen;
    panel.hidden = !panelOpen;
    head.setAttribute('aria-expanded', panelOpen ? 'true' : 'false');
    container.classList.toggle('open', panelOpen);
    head.style.display = panelOpen ? 'none' : '';
    if (panelOpen) _ensureToolDetail(container);
  });

  return container;
}

function _addToolCallsToBubble(bubble, calls) {
  const container = _buildToolCallsContainer(calls);
  bubble.insertBefore(container, bubble.querySelector('.bubble-actions'));
}

function attachToolCallsToLastBubble(calls, targetBubble) {
  if (!calls || calls.length === 0) return;
  if (!app.chatMessages) return;
  let last = targetBubble || null;
  if (!last) {
    const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
    last = bubbles[bubbles.length - 1];
  }

  // Tool-only turn \u2014 the agent made tool calls with no text this turn.
  const isToolOnlyTurn = !targetBubble && (!last || !app._turnHasBubble);
  if (isToolOnlyTurn) {
    // Merge consecutive tool-only turns into ONE grouped bubble. The group is
    // broken whenever a text/user bubble is added (addChatBubble clears the ref).
    const group = app._activeToolGroupBubble;
    if (group && group.isConnected) {
      const container = group.querySelector('.bubble-tool-calls');
      if (container) {
        const panel = container.querySelector('.bubble-tool-calls-panel');
        const head = container.querySelector('.bubble-tool-calls-head');
        _appendToolRows(panel, calls);
        _updateToolCallsHead(head, panel);
        // Keep the lazy-detail call list in sync; a merged-in turn may carry its
        // own un-fetched bodies, so re-arm the fetch on next open.
        if (Array.isArray(container.__calls)) container.__calls.push(...calls);
        else container.__calls = calls.slice();
        if (calls.some(c => c && c._needsDetail)) container.__detailLoaded = false;
        return;
      }
    }
    // Start a new grouped bubble so calls aren't misattributed to the PREVIOUS
    // exchange's bubble.
    last = addChatBubble('agent', '', 'tool-only', undefined, 'tool-turn-' + Date.now());
    app._turnHasBubble = true;
    app._activeToolGroupBubble = last;
    _addToolCallsToBubble(last, calls);
    return;
  }

  // This turn produced a text bubble (or an explicit target on reload) \u2014 hang
  // the tool calls off it.
  if (last.querySelector('.bubble-tool-calls')) return;
  _addToolCallsToBubble(last, calls);
}

// Expose on app immediately for session-load.js
app.appendStreamToActiveBubble = appendStreamToActiveBubble;
app.finalizeAgentResponse = finalizeAgentResponse;
app.ensureStreamingBubbleForActiveTurn = ensureStreamingBubbleForActiveTurn;
app.seedStreamingBubble = seedStreamingBubble;
app.finalizeAgentStep = finalizeAgentStep;
app.markAgentInterrupted = markAgentInterrupted;
app.attachToolCallsToLastBubble = attachToolCallsToLastBubble;

export {
  _stripToolCalls,
  appendStreamToActiveBubble,
  finalizeAgentResponse,
  ensureStreamingBubbleForActiveTurn,
  finalizeAgentStep,
  markAgentInterrupted,
  seedStreamingBubble,
  attachToolCallsToLastBubble,
  _findAgentBubbleForTurn,
  _wsTurnBuffers,
  _setBubbleText,
};