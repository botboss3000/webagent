'use strict';

// Session loader — fetch session messages, handle `restricted` (token refresh +
// retry, auto-create fresh session), windowed virtual-scroll render, attach
// persisted tool-call panels, infinite scroll. Sets app.loadSessionChat.
// Module map for this folder: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { getActive } from '../../shared/js/accounts.js';
import { ensureFreshToken } from '../../shared/js/auth-refresh.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { addChatBubble, _renderMarkdownBody, linkifyText, _setBubbleCreatedAt, _setBubbleSessionSeq } from './chat-bubble.js';
import { _restorePersistenceStatus } from './chat-send.js';
import { renderAttachmentElement } from '../../shared/js/attachments.js';
import { storageAdapter } from './storage/storage-adapter.js';
import {
  _addBubbleActions,
  _bubbleAnchorId,
  _getBubbleText,
  _setBubbleModelFromMeta,
} from './chat-bubble-actions.js';
import {
  _stripToolCalls,
  _isSubstantiveAnswer,
  seedStreamingBubble,
  ensureStreamingBubbleForActiveTurn,
  attachToolCallsToLastBubble,
  _setBubbleText,
} from './chat-stream.js';
import {
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
} from './chat-message-cache.js';
import {
  _installVirtualScroll,
  _teardownVirtualScroll,
  _storeBubbleHeight,
  _makePlaceholder,
  _getBubbleHeight,
  _placeholderIds,
} from './chat-virtual-scroll.js';
import { _fetchRelatedSessions } from './session-list.js';
import { startReconcileLoop } from './chat-reconcile.js';
import { agentChatMsg, isDebugMode, chatUiFlag } from '../../shared/js/app-prompts.js';
import { _agentEngineFor } from './session-agent.js';

// ── Open-fetch sizing ───────────────────────────────────────────────────────
// The opening download is kept small (and, when a saved scroll position exists,
// CENTRED on it) so a session reopens fast instead of hauling its whole tail.
// Older/newer rows then page in on scroll. Tool-call BODIES aren't downloaded at
// all here (light mode) — only headings; bodies load on expand.
const _OPEN_NEWEST_LIMIT = 40;   // newest-N slice when opening at the bottom
const _OPEN_AROUND_RADIUS = 25;  // rows each side of the saved anchor
const _OLDER_BATCH = 60;         // scroll-up page size
const _NEWER_BATCH = 40;         // scroll-down page size

// ── Infinite scroll guards ──────────────────────────────────────────────────
let _loadingMoreMessages = false;   // loading OLDER (scroll up)
let _loadingNewerMessages = false;  // loading NEWER (scroll down, anchored open)

// Arm the always-on DB-reconcile watcher for the session now on screen. Seeds
// the poll cursor to the newest seq we just rendered — OR to `floorSeq` (the
// session's true latest, passed when we opened mid-history) so the idle poll
// only ever renders genuinely NEW rows (e.g. an automation writing into this
// session), never re-renders what's already shown and never backfills the gap
// between an anchored window and the tail — then starts the loop (idempotent).
function _armSessionWatch(sessionId, messages, floorSeq) {
  if (!sessionId) return;
  if (!app.lastSessionSeq) app.lastSessionSeq = {};
  if (!app.lastInteractionSeq) app.lastInteractionSeq = {};
  // A full/cache load is an authoritative reset (important after rollback or
  // replacement), so never retain a stale higher DB cursor from an older view.
  let mx = typeof floorSeq === 'number' ? floorSeq : 0;
  if (Array.isArray(messages)) {
    for (const m of messages) {
      if (typeof m.session_seq === 'number' && m.session_seq > mx) mx = m.session_seq;
    }
  }
  app.lastInteractionSeq[sessionId] = mx;
  // A persisted interaction sequence is also a safe lower bound for replay,
  // but the event cursor may legitimately be much higher.
  app.lastSessionSeq[sessionId] = Math.max(app.lastSessionSeq[sessionId] || 0, mx);
  try { startReconcileLoop(); } catch (_) { /* best-effort */ }
}

function _parseDuration(metadataStr) {
  if (!metadataStr) return null;
  try {
    const meta = JSON.parse(metadataStr);
    return meta.duration_ms != null ? meta.duration_ms : null;
  } catch (_) { return null; }
}

// Group tool-result messages by the assistant message that triggered them.
function _buildToolResultsByParent(messages) {
  const map = {};
  for (const msg of messages) {
    if (msg.role === 'tool' && msg.parent_id) {
      if (!map[msg.parent_id]) map[msg.parent_id] = [];
      map[msg.parent_id].push(msg);
    }
  }
  return map;
}

// Build the per-call objects for one assistant message's saved tool_calls.
// `turnNum` > 0 makes each row show a "Turn N" badge when expanded.
// `light`: the payload carries only tool-call NAMES (+ durations) — the heavy
// bodies (arguments, results, LLM output) were never downloaded, so each
// call is stamped with `_needsDetail` + `_detailMsgId`/`_detailIdx` so the panel
// can lazy-fetch them via /session-turn-detail the first time it's opened.
function _buildCallsForMessage(msg, toolResultsByParent, turnNum, light) {
  if (!msg.output) return [];
  let outputData;
  try { outputData = JSON.parse(msg.output); } catch (_) { return []; }
  const toolCalls = outputData.tool_calls;
  if (!toolCalls || toolCalls.length === 0) return [];

  // Remote skeleton — tool names + IDs only, no real data. Build minimal
  // entries with status 'remote_placeholder' so the UI shows headings but
  // the body says "not saved to remote".
  if (outputData._remote_placeholder) {
    return toolCalls.map((tc, i) => ({
      tool: tc.function ? tc.function.name : '(unknown)',
      toolCallId: tc.id || null,
      args: {},
      status: 'remote_placeholder',
      result: null,
      durationMs: null,
      errorType: null,
      turn: turnNum || 0,
      open: false,
      _savedOutput: null,
      _savedToolOutput: null,
      _savedToolMetadata: null,
      _detailMsgId: null,
      _detailIdx: 0,
      _needsDetail: false,
      _remotePlaceholder: true,
    }));
  }

  return toolCalls.map((tc, i) => {
    let args = {};
    try { args = JSON.parse(tc.function.arguments); } catch (_) {}
    const toolName = tc.function.name;
    const results = toolResultsByParent[msg.id] || [];
    // Pair by tool name, then fall back to positional order within this parent's
    // results. Both lists are per-assistant-row and in call order, so results[i]
    // is the i-th call's result — this covers tool rows saved WITHOUT a tool_name
    // (e.g. older Local Claude Code turns, whose engine didn't stamp it).
    const resultEntry = results.find(r => r.tool_name === toolName) || results[i] || null;
    const deleted = resultEntry && resultEntry.status === 'deleted';
    return {
      tool: toolName,
      toolCallId: tc.id || null,
      args: args,
      status: deleted ? 'deleted' : 'done',
      result: resultEntry ? resultEntry.content : null,
      durationMs: resultEntry ? _parseDuration(resultEntry.metadata) : null,
      errorType: null,
      turn: turnNum || 0,
      open: false,
      _savedOutput: msg.output || null,
      _savedToolOutput: resultEntry ? resultEntry.output : null,
      _savedToolMetadata: resultEntry ? resultEntry.metadata : null,
      _detailMsgId: msg.id || null,
      _detailIdx: i,
      _needsDetail: !!light,
      _deleted: deleted,
    };
  });
}

// Reattach tool calls to assistant TEXT bubbles after a reload. Tool-only turns
// are rendered as grouped bubbles during the windowed pass, so they're skipped
// here. Out-of-window messages (placeholders) are skipped to avoid attaching
// their calls to the wrong bubble.
function _attachToolCallsFromMessages(messages, light) {
  if (!app.attachToolCallsToLastBubble) return;
  const toolResultsByParent = _buildToolResultsByParent(messages);

  for (const msg of messages) {
    if (msg.role !== 'assistant') continue;
    let text = msg.content || '';
    text = _stripToolCalls(text);
    if (!text.trim()) continue;          // tool-only / empty → handled elsewhere
    const calls = _buildCallsForMessage(msg, toolResultsByParent, 0, light);
    if (calls.length === 0) continue;
    const targetBubble = msg.id
      ? app.chatMessages.querySelector(`.chat-bubble.agent[data-msg-id="${msg.id}"]`)
      : null;
    if (!targetBubble) continue;         // out of window → don't misattribute
    app.attachToolCallsToLastBubble(calls, targetBubble);
  }
}

// Called by the virtual scroller after reconstructing a finalized text bubble.
// Reattach only that bubble's lightweight tool headings; detail bodies remain
// lazy and are fetched on expansion exactly as they are on the initial render.
function _rehydrateVirtualBubble(msgId, bubble) {
  const cached = app.currentSessionId ? _messageCache.get(app.currentSessionId) : null;
  if (!cached || !bubble || !app.attachToolCallsToLastBubble) return;
  const msg = cached.messages.find(m => m.id === msgId);
  if (!msg || msg.role !== 'assistant') return;
  const text = _stripToolCalls(msg.content || '');
  if (!text.trim()) return;
  const calls = _buildCallsForMessage(
    msg,
    _buildToolResultsByParent(cached.messages),
    0,
    cached.light !== false,
  );
  if (calls.length) app.attachToolCallsToLastBubble(calls, bubble);
}

app._rehydrateVirtualBubble = _rehydrateVirtualBubble;

// Synthetic STANDALONE tool rows — written outside the normal assistant
// tool_call pairing, so the parent-lookup renderer skips them. Two kinds:
//   • vision ingestion — process_image (describe path) / route_attachment
//     (unreadable path), parented to the USER turn before the reply exists;
//   • loop-node memory — memory_search (runs before the turn) / memory_save
//     (runs after), tagged metadata.brain on the backend.
// The backend flags every one `_synth_tool` (vision also has the parent's role
// as a fallback signal). Vision renders its own foldable tool-call bubble in
// chronological position. Memory renders a small debug note instead.
const _SYNTH_TOOLS = {
  process_image: true, route_attachment: true,
  memory_search: true, memory_save: true,  // shown as debug notes, not bubbles
  app_control: true,   // App Control point-and-share fingerprint (user hand-off)
};

function _isSynthToolRow(msg, idToRole) {
  if (!msg || msg.role !== 'tool' || !_SYNTH_TOOLS[msg.tool_name]) return false;
  if (msg._synth_tool || msg._vision_synth) return true;   // backend tag (new / legacy)
  // Fallback for legacy payloads with no backend tag: VISION rows are parented to
  // the user turn. Memory rows must NOT use this — a SKIPPED memory_search is also
  // user-parented but is deliberately left untagged so it doesn't render an empty
  // bubble; the tag is the only signal that admits a memory row.
  const visionOnly = msg.tool_name === 'process_image' || msg.tool_name === 'route_attachment';
  return (visionOnly && idToRole) ? (idToRole[msg.parent_id] === 'user') : false;
}

// Build one tool-call panel entry straight from a persisted synthetic tool row.
// The args (image prompt / search query) live in the row's metadata, which is
// never slimmed, so the panel shows args + result on reload with no lazy fetch.
function _buildSynthCall(msg) {
  let meta = {};
  try { if (msg.metadata) meta = JSON.parse(msg.metadata) || {}; } catch (_) {}
  const args = (meta && meta.args) || {};
  return {
    tool: msg.tool_name,
    args: args,
    status: meta.error ? 'error' : 'done',
    result: (msg.content != null && msg.content !== '') ? msg.content : (msg.output || ''),
    durationMs: (typeof meta.duration_ms === 'number') ? meta.duration_ms : null,
    errorType: null,
    turn: 0,
    open: false,
    _savedToolOutput: msg.output || null,
    _savedToolMetadata: msg.metadata || null,
    _detailMsgId: msg.id || null,
    _detailIdx: 0,
    _needsDetail: false,
  };
}

// Segment a flat message list into render units: user/text bubbles stand alone;
// consecutive tool-only assistant turns merge into one grouped tool-call bubble
// (one "Turn N" section per turn when expanded); synthetic standalone tool rows
// (image-processing + loop-node memory) become their own foldable bubble in
// chronological position — memory_search before the reply, memory_save after.
// Shared by the windowed initial render and the append-newer (scroll-down) path.
function _messagePhase(msg, legacyFinalIds) {
  // Persisted terminal status wins over stale metadata left behind when a
  // streaming row crashes before its metadata can be finalized.
  if (msg.status === 'interrupted' || msg.status === 'error') return 'terminal';
  if (msg.status === 'streaming') return 'pending';
  let explicit = String(msg.message_phase || '').toLowerCase();
  if (!explicit && msg.metadata) {
    try {
      const metadata = typeof msg.metadata === 'string'
        ? JSON.parse(msg.metadata) : msg.metadata;
      explicit = String((metadata && metadata.message_phase) || '').toLowerCase();
    } catch (_) {}
  }
  if (explicit) return explicit;
  if (legacyFinalIds && legacyFinalIds.has(msg.id)) return 'final';
  try {
    const out = typeof msg.output === 'string' ? JSON.parse(msg.output) : msg.output;
    if (out && Array.isArray(out.tool_calls) && out.tool_calls.length) return 'progress';
  } catch (_) {}
  return 'progress';
}

function _legacyFinalAssistantIds(messages) {
  const ids = new Set();
  const lastByTurn = new Map();
  let currentTurn = '';
  for (const msg of messages) {
    if (msg.role === 'user') currentTurn = msg.id || currentTurn;
    if (msg.role !== 'assistant') continue;
    if (_messagePhase(msg, null) !== 'progress') continue;
    if (msg.status === 'streaming' || msg.status === 'interrupted' || msg.status === 'error') continue;
    let hasTools = false;
    try {
      const out = typeof msg.output === 'string' ? JSON.parse(msg.output) : msg.output;
      hasTools = !!(out && Array.isArray(out.tool_calls) && out.tool_calls.length);
    } catch (_) {}
    if ((msg.content || '').trim() && !hasTools) {
      lastByTurn.set(msg.turn_id || currentTurn || '__window__', msg.id);
    }
  }
  lastByTurn.forEach(id => { if (id) ids.add(id); });
  return ids;
}

function _buildPhasedRenderables(messages, toolResultsByParent, light) {
  const renderables = [];
  const legacyFinalIds = _legacyFinalAssistantIds(messages);
  const idToRole = {};
  for (const msg of messages) if (msg && msg.id) idToRole[msg.id] = msg.role;
  let pending = null;
  let pendingSynth = null;
  let currentUserTurn = '';

  const flushActivity = () => {
    if (pending && pending.entries.length) renderables.push(pending);
    pending = null;
  };
  const flushSynth = () => {
    if (pendingSynth && pendingSynth.calls.length) renderables.push(pendingSynth);
    pendingSynth = null;
  };
  const ensureActivity = msg => {
    const owner = msg.turn_id || currentUserTurn || msg.parent_id || '';
    if (!pending || pending.ownerTurnId !== owner) {
      flushActivity();
      pending = {
        _activityGroup: true,
        _toolGroup: true,
        id: 'activity-' + (owner || 'turn') + '-' + (msg.id || renderables.length),
        ownerTurnId: owner,
        entries: [],
        calls: [],
        created_at: msg.created_at,
        session_seq: msg.interaction_seq != null ? msg.interaction_seq : msg.session_seq,
        firstInteractionId: msg.id,
      };
    }
    return pending;
  };

  for (const msg of messages) {
    if (msg.role === 'system') {
      flushSynth();
      flushActivity();
      const src = msg.source || '';
      if (!src.startsWith('system:debug:') || isDebugMode()) renderables.push(msg);
      continue;
    }
    if (msg.role === 'user') {
      flushSynth();
      flushActivity();
      currentUserTurn = msg.id || currentUserTurn;
      renderables.push(msg);
      continue;
    }
    if (_isSynthToolRow(msg, idToRole)) {
      flushActivity();
      if (msg.tool_name === 'memory_search' || msg.tool_name === 'memory_save') {
        renderables.push({
          _memoryNote: true,
          message: msg.tool_name === 'memory_search' ? 'Memory searched' : 'Memory saved',
        });
        flushSynth();
        continue;
      }
      if (!pendingSynth) {
        pendingSynth = { _toolGroup: true, id: 'synth-' + (msg.id || renderables.length), calls: [] };
      }
      pendingSynth.calls.push(_buildSynthCall(msg));
      continue;
    }
    if (msg.role !== 'assistant') continue;
    flushSynth();

    const text = _stripToolCalls(msg.content || '').trim();
    const phase = _messagePhase(msg, legacyFinalIds);
    if (phase === 'pending') {
      flushActivity();
      if (text) renderables.push(msg);
      continue;
    }
    if (phase === 'main' || phase === 'final') {
      flushActivity();
      if (text) renderables.push(msg);
      continue;
    }
    // C: substantive mid-turn text is a real answer — surface it as a normal
    // bubble instead of burying it in the tools/updates panel (respecting the
    // show_mid_turn_messages toggle).
    if (phase === 'progress'
        && text
        && chatUiFlag('show_mid_turn_messages', true)
        && chatUiFlag('classify_main_messages', true)
        && _isSubstantiveAnswer(text)) {
      flushActivity();
      renderables.push(msg);
      continue;
    }

    const group = ensureActivity(msg);
    if (text && chatUiFlag('show_mid_turn_messages', true)) {
      group.entries.push({ kind: 'progress', id: msg.id, content: text, createdAt: msg.created_at });
    }
    const calls = _buildCallsForMessage(msg, toolResultsByParent, 0, light);
    group.entries.push(...calls);
    group.calls.push(...calls);
    if (phase === 'terminal') {
      group.entries.push({
        kind: 'terminal',
        id: 'terminal-' + (msg.id || group.entries.length),
        label: msg.status === 'error' ? 'Error' : 'Stopped',
        content: '',
      });
      flushActivity();
    }
  }
  flushSynth();
  flushActivity();
  return renderables;
}
function _buildRenderables(messages, toolResultsByParent, light) {
  return _buildPhasedRenderables(messages, toolResultsByParent, light);
  const renderables = [];
  const idToRole = {};
  for (const m of messages) { if (m && m.id) idToRole[m.id] = m.role; }

  // When show_mid_turn_messages is off, only the LAST text-bearing assistant
  // message per contiguous run (before a user/system boundary) renders.
  let finalIds = null;
  if (!chatUiFlag('show_mid_turn_messages', true)) {
    finalIds = new Set();
    let saw = false;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === 'user' || m.role === 'system') { saw = false; continue; }
      if (m.role !== 'assistant') continue;
      if ((m.content || '').trim() && !saw) {
        finalIds.add(m.id);
        saw = true;
      }
    }
  }

  let pendingGroup = null;
  let pendingSynth = null;
  const flushGroup = () => {
    if (pendingGroup && pendingGroup.calls.length) renderables.push(pendingGroup);
    pendingGroup = null;
  };
  const flushSynth = () => {
    if (pendingSynth && pendingSynth.calls.length) renderables.push(pendingSynth);
    pendingSynth = null;
  };
  for (const msg of messages) {
    if (msg.role === 'system') {
      // System messages with a "system:debug:" source are debug-only — skip
      // them unless the chat_ui.json debug toggle is enabled for this agent.
      const src = msg.source || '';
      if (src.startsWith('system:debug:') && !isDebugMode()) continue;
      flushSynth(); flushGroup(); renderables.push(msg); continue;
    }
    if (msg.role === 'user') { flushSynth(); flushGroup(); renderables.push(msg); continue; }
    // Synthetic standalone tool rows (vision ingestion, loop-node memory) render
    // as their own bubble — they have no assistant tool_call to attach to.
    // Memory rows render as a small debug note instead.
    if (_isSynthToolRow(msg, idToRole)) {
      flushGroup();
      if (msg.tool_name === 'memory_search' || msg.tool_name === 'memory_save') {
        renderables.push({
          _memoryNote: true,
          message: msg.tool_name === 'memory_search' ? 'Memory searched' : 'Memory saved',
        });
        flushSynth();
        continue;
      }
      if (!pendingSynth) {
        pendingSynth = { _toolGroup: true, id: 'synth-' + (msg.id || renderables.length), calls: [] };
      }
      pendingSynth.calls.push(_buildSynthCall(msg));
      continue;
    }
    if (msg.role !== 'assistant') continue;   // other tool results handled via parent lookup
    flushSynth();                             // assistant content ends the synth group
    let text = msg.content || '';
    text = _stripToolCalls(text);
    if (msg.status === 'streaming') {
      flushGroup();
      // Only push streaming messages that have visible text. Empty streaming
      // rows from stale runs would otherwise render as empty bubbles; active
      // runs get a fresh placeholder from ensureStreamingBubbleForActiveTurn.
      if (text.trim()) renderables.push(msg);
      continue;
    }
    if (text.trim()) {
      flushGroup();
      if (finalIds && !finalIds.has(msg.id)) continue;
      renderables.push(msg);
      continue;
    }
    // Tool-only turn — accumulate into the current group (skip empty turns).
    const peekCalls = _buildCallsForMessage(msg, toolResultsByParent, 0, light);
    if (peekCalls.length === 0) continue;
    if (!pendingGroup) {
      pendingGroup = { _toolGroup: true, id: 'toolgroup-' + (msg.id || renderables.length), calls: [], turnCount: 0, created_at: msg.created_at };
    }
    pendingGroup.turnCount += 1;
    for (const c of peekCalls) c.turn = pendingGroup.turnCount;
    pendingGroup.calls.push(...peekCalls);
  }
  flushSynth();
  flushGroup();
  return renderables;
}

// Append a batch of newer messages to the bottom of the live transcript (the
// scroll-down / forward-pagination path). Renders real bubbles + grouped
// tool-only bubbles + reattaches tool-call panels, mirroring the initial pass.
function _appendMessagesToTranscript(messages, light) {
  const container = app.chatMessages;
  if (!container || !messages.length) return;
  const toolResultsByParent = _buildToolResultsByParent(messages);
  const renderables = _buildRenderables(messages, toolResultsByParent, light);
  for (const msg of renderables) {
    // Reconcile, forward pagination, and an outbox acknowledgement can all race
    // to present the same persisted row. Treat the interaction UUID as the DOM
    // primary key so every append path is idempotent.
    if (msg.id && !msg._activityGroup) {
      const key = CSS.escape(String(msg.id));
      const existing = container.querySelector(
        `[data-msg-id="${key}"], [data-turn-id="${key}"]`,
      );
      if (existing) continue;
    }
    if (msg._memoryNote) {
      if (typeof app._appendMemoryNote === 'function') app._appendMemoryNote(msg.message);
      continue;
    }
    if (msg._toolGroup) { _emitToolGroupBubble(msg); continue; }
    const el = _emitRealBubble(msg);
    if (el && el.nodeType === 1) _addBubbleActions(el);
  }
  _attachToolCallsFromMessages(messages, light);
}

app._appendMessagesToTranscript = _appendMessagesToTranscript;
// Prepend through the same renderer as initial/forward loads so grouped
// tool-only turns cannot diverge. Render at the tail, collect the new nodes,
// then move the ordered block before the previous first child.
function _prependMessagesToTranscript(messages, light) {
  const container = app.chatMessages;
  if (!container || !messages.length) return;
  // Historical rendering must not inherit the live/current assistant merge
  // pointer. Otherwise the first older final on a page can be appended as a
  // section of the newest final already on screen.
  app._agentTurnBubble = null;
  _appendMessagesToTranscript(messages, light);
  // _setBubbleSessionSeq performs canonical placement while each node renders.
  // Do not infer node identity from its post-sort array position.
}

function _reprojectCachedTranscript(sessionId, cache, preservePrependViewport) {
  const container = app.chatMessages;
  if (!container || !cache) return;
  const scroller = app._chatScroller || container.parentElement;
  const oldHeight = scroller ? scroller.scrollHeight : 0;
  const oldTop = scroller ? scroller.scrollTop : 0;
  cache.messages = _sortMessagesCanonical(cache.messages || []);
  _renderSessionWindowed(
    cache.messages, sessionId, null, false, false, cache.light !== false,
  );
  if (preservePrependViewport && scroller) {
    scroller.scrollTop = oldTop + Math.max(0, scroller.scrollHeight - oldHeight);
  }
}

// Fetch a window of session messages. `opts`: { beforeId, afterId, aroundId,
// light }. Light defaults ON for the chat (headings only; bodies load on
// expand). beforeId/afterId page older/newer; aroundId opens centred on a saved
// position.
async function _fetchMessages(sessionId, limit, opts) {
  opts = opts || {};
  // Hybrid + browser + normal all route through the adapter, which
  // handles IndexedDB caching transparently based on the active mode.
  if (storageAdapter.isBrowser || storageAdapter.isHybrid) {
    return storageAdapter.getInteractions(sessionId, limit, opts);
  }
  const token = localStorage.getItem('auth_token');
  let url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sessionId)}&limit=${limit}`);
  if (opts.light !== false) url += '&light=1';
  if (opts.beforeId) url += `&before_id=${encodeURIComponent(opts.beforeId)}`;
  if (opts.afterId) url += `&after_id=${encodeURIComponent(opts.afterId)}`;
  if (opts.aroundId) url += `&around_id=${encodeURIComponent(opts.aroundId)}`;
  if (opts.atStart) url += '&at_start=1';
  if (opts.nearestUserBeforeId) url += `&nearest_user_before_id=${encodeURIComponent(opts.nearestUserBeforeId)}`;
  if (token) url += `&token=${encodeURIComponent(token)}`;
  const res = await fetch(url);
  return await res.json();
}

async function _extendTailToOwningUser(sessionId, data, light) {
  if (!data || !Array.isArray(data.messages) || !data.messages.length) return data;
  let messages = _sortMessagesCanonical([...data.messages]);
  const firstOwned = messages.find(msg => msg && msg.turn_id);
  const ownerTurnId = firstOwned && firstOwned.turn_id;
  if (!ownerTurnId) return data;
  const ownsBoundary = () => messages.some(msg =>
    msg && msg.role === 'user'
      && (String(msg.id) === String(ownerTurnId)
        || String(msg.turn_id || '') === String(ownerTurnId)),
  );
  const seen = new Set(messages.map(msg => msg && msg.id).filter(Boolean));
  let hasMore = !!data.has_more;
  // A tool-heavy turn can exceed one page. Bound the expansion to prevent a
  // malformed legacy turn from turning session open into an unbounded fetch.
  for (let page = 0; hasMore && !ownsBoundary() && page < 8; page += 1) {
    const oldestId = messages.length ? messages[0].id : null;
    if (!oldestId) break;
    const older = await _fetchMessages(
      sessionId, _OLDER_BATCH, { beforeId: oldestId, light },
    );
    const incoming = (older.messages || []).filter(msg => msg.id && !seen.has(msg.id));
    if (!incoming.length) {
      hasMore = false;
      break;
    }
    incoming.forEach(msg => seen.add(msg.id));
    messages = _sortMessagesCanonical([...incoming, ...messages]);
    hasMore = !!older.has_more;
  }
  data.messages = messages;
  data.has_more = hasMore;
  return data;
}

async function _maybeLoadMoreOnScrollTop(sessionId) {
  if (!sessionId) return;
  if (_loadingMoreMessages) return;
  const container = app.chatMessages;
  if (!container) return;
  const cache = _messageCache.get(sessionId);
  if (!cache || !cache.hasMore) return;
  const scroller = app._chatScroller || container.parentElement;
  if (scroller.scrollTop > 150) return;

  _loadingMoreMessages = true;
  try {
    const oldestId = cache.messages.length > 0 ? cache.messages[0].id : null;
    if (!oldestId) { cache.hasMore = false; return; }
    const data = await _fetchMessages(sessionId, _OLDER_BATCH, { beforeId: oldestId, light: true });
    if (!data.messages || data.messages.length === 0) {
      cache.hasMore = false;
      return;
    }
    const seen = new Set(cache.messages.map(m => m.id));
    const incoming = data.messages.filter(m => m.id && !seen.has(m.id));
    cache.hasMore = !!data.has_more;
    if (incoming.length === 0) return;
    cache.messages = _sortMessagesCanonical([...incoming, ...cache.messages]);
    _reprojectCachedTranscript(sessionId, cache, true);
    container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
  } catch (e) {
    console.warn('Failed to load earlier messages:', e);
  } finally {
    _loadingMoreMessages = false;
  }
}

// Forward pagination: when a session was opened CENTRED on a saved position
// (so the newest messages weren't downloaded), scrolling to the bottom pages
// the newer rows in until caught up with the tail. The live reconcile cursor is
// already seeded at the true latest seq, so this never double-renders with it.
async function _maybeLoadMoreOnScrollBottom(sessionId) {
  if (!sessionId) return;
  if (_loadingNewerMessages) return;
  const container = app.chatMessages;
  if (!container) return;
  const cache = _messageCache.get(sessionId);
  if (!cache || !cache.hasNewer) return;
  const scroller = app._chatScroller || container.parentElement;
  const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  if (distanceFromBottom > 150) return;

  _loadingNewerMessages = true;
  try {
    const newestId = cache.messages.length > 0 ? cache.messages[cache.messages.length - 1].id : null;
    if (!newestId) { cache.hasNewer = false; return; }
    const data = await _fetchMessages(sessionId, _NEWER_BATCH, { afterId: newestId, light: true });
    const seen = new Set(cache.messages.map(m => m.id));
    const incoming = (data.messages || []).filter(m => m.id && !seen.has(m.id));
    cache.hasNewer = !!data.has_newer;
    if (incoming.length === 0) return;
    cache.messages = _sortMessagesCanonical([...cache.messages, ...incoming]);
    _reprojectCachedTranscript(sessionId, cache, false);
    container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
  } catch (e) {
    console.warn('Failed to load newer messages:', e);
  } finally {
    _loadingNewerMessages = false;
  }
}

// ── Jump-nav repositioning ─────────────────────────────────────────────────
// Reposition the transcript window to a DB-truthful target: the true session
// start (`{atStart:true}`) or a specific anchor message (`{anchorId}`),
// fetched from the DB when it isn't already in the cache. Reuses
// _renderSessionWindowed's anchor machinery and re-arms the reconcile cursor
// at the session's true tail so the mid-history gap is never backfilled.
let _repositionInFlight = false;
async function _repositionTranscript(sessionId, target) {
  if (!sessionId || !target || _repositionInFlight) return false;
  const container = app.chatMessages;
  if (!container) return false;

  _repositionInFlight = true;
  try {
    const cached = _messageCache.get(sessionId);

    // Fast path — the target is already materialised in the cache (or the whole
    // transcript is loaded for a jump-to-start): re-render without a fetch.
    if (cached && cached.messages.length) {
      const inCache = target.atStart
        ? !cached.hasMore
        : cached.messages.some(m => m.id === target.anchorId);
      if (inCache) {
        _renderSessionWindowed(
          cached.messages, sessionId, null, false, false, cached.light !== false, target,
        );
        _installVirtualScroll();
        _armSessionWatch(sessionId, cached.messages, cached.maxSeq);
        container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
        return true;
      }
    }

    // Target is outside the loaded window — fetch a window around it.
    let data;
    if (target.atStart) {
      data = await _fetchMessages(sessionId, _OLDER_BATCH, { atStart: true, light: true });
    } else {
      data = await _fetchMessages(sessionId, _OPEN_AROUND_RADIUS, { aroundId: target.anchorId, light: true });
    }
    if (!data || !Array.isArray(data.messages) || !data.messages.length) return false;

    const msgs = _sortMessagesCanonical(data.messages);
    _messageCache.set(sessionId, {
      messages: msgs,
      hasMore: !!data.has_more,
      hasNewer: !!data.has_newer,
      light: data.light !== false,
      maxSeq: data.max_session_seq || 0,
      contextTokens: data.context_tokens || 0,
      manifest: data.manifest || null,
      loadedAt: Date.now(),
    });
    if (typeof app.setContextFromMessages === 'function') app.setContextFromMessages(msgs);
    _renderSessionWindowed(msgs, sessionId, data.run || null, false, false, data.light !== false, target);
    _installVirtualScroll();
    _armSessionWatch(sessionId, msgs, data.max_session_seq);
    if (typeof app.seedReconcileSig === 'function') app.seedReconcileSig(sessionId, msgs);
    container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
    return true;
  } catch (e) {
    console.warn('Failed to reposition transcript:', e);
    return false;
  } finally {
    _repositionInFlight = false;
  }
}

// Single-chevron jump: find the most recent USER message strictly before the
// message currently at the top of the viewport — against the DB, not just the
// loaded window. The DOM-only fast path (_prevUserTarget) lives in chat-ui.js;
// this is the cache-scan + DB-probe fallback. Returns true when a jump happened.
async function _stepToPrevUserMessage(sessionId) {
  if (!sessionId || _repositionInFlight) return false;
  const container = app.chatMessages;
  if (!container) return false;
  const cached = _messageCache.get(sessionId);
  if (!cached || !cached.messages.length) return false;

  // Anchor = the message at the top of the viewport (first child whose bottom
  // edge is below the scroll line), mirroring _captureSessionFocus.
  const scroller = app._chatScroller || container.parentElement;
  const st = scroller ? scroller.scrollTop : 0;
  let anchorId = null;
  for (const el of Array.from(container.children)) {
    if (!el.classList) continue;
    const isBubble = el.classList.contains('chat-bubble');
    const isPlaceholder = el.classList.contains('chat-bubble-placeholder');
    if (!isBubble && !isPlaceholder) continue;
    const id = el.getAttribute('data-msg-id');
    if (!id) continue;
    const top = el.offsetTop;
    const h = isPlaceholder ? (parseInt(el.style.height, 10) || 0) : el.offsetHeight;
    if (top + h > st + 4) { anchorId = id; break; }
  }
  if (!anchorId) anchorId = cached.messages[0].id;

  const anchorIdx = cached.messages.findIndex(m => m.id === anchorId);
  const scanEnd = anchorIdx === -1 ? cached.messages.length : anchorIdx;

  // Cache scan: most recent user message strictly before the anchor.
  for (let i = scanEnd - 1; i >= 0; i--) {
    const m = cached.messages[i];
    if (m && m.role === 'user') {
      return app._repositionTranscript(sessionId, { anchorId: m.id });
    }
  }

  // Cache exhausted — ask the DB for the nearest user row before the oldest
  // loaded message, then centre a window on it.
  if (!cached.hasMore) return false;
  try {
    const oldestId = cached.messages[0].id;
    const probe = await _fetchMessages(sessionId, 1, { nearestUserBeforeId: oldestId, light: true });
    const userMsg = (probe && probe.messages || []).find(m => m && m.role === 'user');
    if (!userMsg || !userMsg.id) return false;
    return app._repositionTranscript(sessionId, { anchorId: userMsg.id });
  } catch (e) {
    console.warn('Failed to find previous user message:', e);
    return false;
  }
}

// Double-chevron-down jump: go to the TRUE latest message, not just the bottom
// of the loaded window. When the session was opened mid-history (hasNewer), the
// newest slice is fetched from the DB and rendered at the bottom; otherwise the
// loaded tail already is the latest, so it's a pure scroll.
async function _repositionToBottom(sessionId) {
  if (!sessionId || _repositionInFlight) return false;
  const container = app.chatMessages;
  if (!container) return false;
  const scroller = app._chatScroller || container.parentElement;

  _repositionInFlight = true;
  try {
    const cached = _messageCache.get(sessionId);
    // Fast path — we already hold the true tail (no newer rows beyond the
    // window): just scroll the loaded transcript to its bottom.
    if (cached && cached.messages.length && !cached.hasNewer) {
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
      return true;
    }
    // The session was opened centred mid-history (or the cache is empty) —
    // fetch the true newest window from the DB and render at the bottom.
    let data = await _fetchMessages(sessionId, _OPEN_NEWEST_LIMIT, { light: true });
    if (!data || !Array.isArray(data.messages) || !data.messages.length) return false;
    // Same coherence pass as a normal bottom-open: don't present an orphaned
    // tail that begins halfway through a tool-heavy turn.
    data = await _extendTailToOwningUser(sessionId, data, true);
    const msgs = _sortMessagesCanonical(data.messages || []);
    _messageCache.set(sessionId, {
      messages: msgs,
      hasMore: !!data.has_more,
      hasNewer: !!data.has_newer,
      light: data.light !== false,
      maxSeq: data.max_session_seq || 0,
      contextTokens: data.context_tokens || 0,
      manifest: data.manifest || null,
      loadedAt: Date.now(),
    });
    if (typeof app.setContextFromMessages === 'function') app.setContextFromMessages(msgs);
    // No anchor override + no focus → the window renders at the bottom.
    _renderSessionWindowed(msgs, sessionId, data.run || null, false, false, data.light !== false);
    _installVirtualScroll();
    _armSessionWatch(sessionId, msgs, data.max_session_seq);
    if (typeof app.seedReconcileSig === 'function') app.seedReconcileSig(sessionId, msgs);
    container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
    return true;
  } catch (e) {
    console.warn('Failed to reposition to bottom:', e);
    return false;
  } finally {
    _repositionInFlight = false;
  }
}

function _adjustScrollForPrepend(container, oldScrollHeight) {
  const scroller = app._chatScroller || (container && container.parentElement);
  if (!scroller) return;
  const newScrollHeight = scroller.scrollHeight;
  const delta = newScrollHeight - oldScrollHeight;
  scroller.scrollTop += delta;
}

function _createBubble(role, text, extraClass, imageUrl, turnId, msgId, createdAt) {
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (turnId) bubble.setAttribute('data-turn-id', turnId);
  if (msgId) bubble.setAttribute('data-msg-id', msgId);
  _setBubbleCreatedAt(bubble, createdAt);
  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
  }
  bubble.appendChild(document.createTextNode(text || ''));
  requestAnimationFrame(() => _storeBubbleHeight(bubble));
  return bubble;
}

// Re-render a reloaded user turn's pasted images/files into its bubble. The
// live bubble gets these from the send flow (chat-send.js); a cold reload has
// only the stored row, so /session-messages now ships a resolved `attachments`
// array we render here with the same component the live path uses.
function _appendUserAttachments(bubble, msg) {
  if (!bubble || bubble.nodeType !== 1) return;
  const atts = msg && msg.attachments;
  if (!Array.isArray(atts) || atts.length === 0) return;
  for (const att of atts) {
    const el = renderAttachmentElement(att);
    if (el) bubble.appendChild(el);
  }
}

// Extract the friendly label for a GenUI-originated user row (page field/button
// sends). Returns null for normal user messages. Handles metadata as either a
// JSON string (DB endpoints) or an object (live WS path).
function _genuiLabelFromMeta(metaRaw) {
  if (!metaRaw) return null;
  let m = metaRaw;
  if (typeof m === 'string') { try { m = JSON.parse(m); } catch (_) { return null; } }
  if (!m || typeof m !== 'object' || !m.genui) return null;
  return String(m.genui_label || '').trim() || null;
}

function _emitRealBubble(msg) {
  if (msg.role === 'system') {
    const src = msg.source || '';
    const extraClass = src.startsWith('system:debug:') ? 'system-debug'
      : src === 'system:error' ? 'system-error'
      : src === 'system:retry' ? 'system-retry'
      : undefined;
    const el = app.addChatBubble('info', msg.content || '', extraClass, undefined, undefined, msg.id, msg.created_at);
    _setBubbleSessionSeq(el, msg.session_seq);
    return el;
  }
  if (msg.role === 'user') {
    // ── GenUI-originated page sends (field/button prompts) ──
    // The row is a real user turn (the agent received the raw prompt), but the
    // chat panel shows the page's friendly label as a green notice instead of a
    // "You" bubble. The raw prompt only surfaces under the debug toggle.
    const _genuiLabel = _genuiLabelFromMeta(msg.metadata);
    if (_genuiLabel) {
      const _g = app.addChatBubble('info', _genuiLabel, 'system-genui', undefined, undefined, msg.id, msg.created_at);
      if (isDebugMode() && (msg.content || '').trim()) {
        app.addChatBubble('info', 'Raw prompt:\n' + msg.content, 'system-debug', undefined, undefined, undefined, msg.created_at);
      }
      _setBubbleSessionSeq(_g, msg.session_seq);
      return _g;
    }
    const _ub = app.addChatBubble('user', msg.content, undefined, undefined, undefined, msg.id, msg.created_at);
    try {
      const _meta = msg.metadata ? JSON.parse(msg.metadata) : {};
      if (_meta?.cmid) _restorePersistenceStatus(_ub, _meta.cmid);
    } catch (_) { /* legacy or malformed metadata has no persistence receipt */ }
    _appendUserAttachments(_ub, msg);
    _setBubbleSessionSeq(_ub, msg.session_seq);
    return _ub;
  }
  let text = msg.content || '';
  text = _stripToolCalls(text);
  const hasText = !!text.trim();
  let el;
  if (msg.status === 'streaming') {
    el = (typeof app.seedStreamingBubble === 'function')
      ? app.seedStreamingBubble(msg.id, text, msg.created_at)
      : app.addChatBubble('agent', text || '', 'streaming', undefined, msg.id, msg.id, msg.created_at);
  } else if (!hasText) {
    return null;
  } else if (msg.status === 'interrupted') {
    el = app.addChatBubble('agent', text + '\n\n(interrupted)', 'interrupted', undefined, msg.id, msg.id, msg.created_at);
  } else if (msg.status === 'error') {
    el = app.addChatBubble('agent', text, 'error', undefined, msg.id, msg.id, msg.created_at);
  } else {
    el = app.addChatBubble('agent', text, undefined, undefined, msg.id, msg.id, msg.created_at);
  }
  // Stamp the per-turn model tag before the action row is built by the caller.
  if (el && el.nodeType === 1 && msg.metadata) _setBubbleModelFromMeta(el, msg.metadata);
  if (el && msg.status === 'deleted') {
    el.classList.add('deleted');
    if (typeof app._injectDeletedActions === 'function') {
      app._injectDeletedActions(el, msg.id);
    }
  }
  _setBubbleSessionSeq(el, msg.session_seq);
  return el;
}

// Render tool calls as a section INSIDE the preceding agent bubble,
// merging consecutive tool-only turns into the same turn bubble.
function _emitToolGroupBubble(group) {
  if (group._activityGroup && typeof app.attachActivityEntries === 'function') {
    const owner = group.ownerTurnId || '';
    return app.attachActivityEntries(
      group.entries || group.calls || [],
      null,
      {
        id: group.firstInteractionId,
        activityGroupId: group.id,
        createdAt: group.created_at,
        turnId: owner,
        interactionSeq: group.session_seq == null ? null : Number(group.session_seq),
      },
      owner,
    );
  }
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  let targetBubble = bubbles[bubbles.length - 1];

  // If no preceding bubble exists, or the last bubble is a user bubble or
  // a deleted/error bubble, create a fresh agent bubble for the tool group.
  if (!targetBubble || targetBubble.classList.contains('user')
      || targetBubble.classList.contains('error')
      || targetBubble.classList.contains('deleted')
      || targetBubble.classList.contains('interrupted')) {
    targetBubble = app.addChatBubble('agent', '', undefined, undefined, group.id, undefined, group.created_at);
  }

  if (targetBubble && targetBubble.nodeType === 1 && typeof app.attachToolCallsToLastBubble === 'function') {
    app.attachToolCallsToLastBubble(group.calls, targetBubble);
  }
  return targetBubble;
}

// Expose for virtual-scroll
app._maybeLoadMoreOnScrollTop = _maybeLoadMoreOnScrollTop;
app._maybeLoadMoreOnScrollBottom = _maybeLoadMoreOnScrollBottom;
// Expose the DB-truthful jump-nav helpers for chat-ui.js (single/double chevrons).
app._repositionTranscript = _repositionTranscript;
app._stepToPrevUserMessage = _stepToPrevUserMessage;
app._repositionToBottom = _repositionToBottom;
app._hasOlderMessages = function(sessionId) {
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  return !!(cached && cached.hasMore);
};
app._hasNewerMessages = function(sessionId) {
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  return !!(cached && cached.hasNewer);
};
/** Sync predicate: is the jump target already materialised in the cache? Used
 *  by chat-ui.js to show the busy spinner only when a reposition will actually
 *  hit the DB. Targets: {atStart:true}, {atBottom:true}, or {anchorId}. */
app._jumpTargetInCache = function(sessionId, target) {
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  if (!cached || !cached.messages.length) return false;
  if (target && target.atStart) return !cached.hasMore;
  if (target && target.atBottom) return !cached.hasNewer;
  if (target && target.anchorId) return cached.messages.some(m => m.id === target.anchorId);
  return false;
};

/**
 * Hard-boundary trigger: when the user scrolls to the absolute top or bottom
 * of the messages container, force-load older/newer messages directly from
 * the API. Fully self-contained — bypasses all proximity gates, caching
 * checks, and state flags in _maybeLoadMoreOnScrollTop/_maybeLoadMoreOnScrollBottom.
 * Each direction has its own loading guard to prevent concurrent fetches.
 */
let _hbtLoadingOlder = false;
let _hbtLoadingNewer = false;
app._checkHardScrollBoundary = async function(sessionId) {
  if (!sessionId) return;
  const container = app.chatMessages;
  if (!container) return;
  const scroller = app._chatScroller || container.parentElement;
  const st = scroller.scrollTop;
  const sh = scroller.scrollHeight;
  const ch = scroller.clientHeight;
  const cache = _messageCache.get(sessionId);
  if (!cache) return;

  const atTop = st <= 0;
  // Don't fire the hard-boundary while the rubber band is actively pulling
  // — the pull-to-refresh gesture handles loading instead.
  const rbActive = typeof app._rbPullActive !== 'undefined' && app._rbPullActive;
  const atBottom = (sh - st - ch) <= 10;

  // ── Top boundary ──
  if (atTop && cache.hasMore && !rbActive && !_hbtLoadingOlder && !_loadingMoreMessages) {
    _hbtLoadingOlder = true;
    try {
      const oldestId = cache.messages.length > 0 ? cache.messages[0].id : null;
      if (!oldestId) { cache.hasMore = false; return; }
      cache.hasMore = false;
      const data = await _fetchMessages(sessionId, 60, { beforeId: oldestId, light: true });
      const msgs = data && data.messages;
      if (!msgs || msgs.length === 0) return;
      const seen = new Set(cache.messages.map(m => m.id));
      const incoming = msgs.filter(m => m.id && !seen.has(m.id));
      cache.hasMore = !!data.has_more;
      if (incoming.length === 0) return;
      cache.messages = [...incoming, ...cache.messages];
      const scroller2 = app._chatScroller || container.parentElement;
      const oldScrollHeight = scroller2.scrollHeight;
      _prependMessagesToTranscript(incoming, true);
      _adjustScrollForPrepend(container, oldScrollHeight);
      container.querySelectorAll('.chat-bubble').forEach(b => {
        if (typeof _storeBubbleHeight === 'function') _storeBubbleHeight(b);
      });
    } catch (e) {
      console.warn('Hard-boundary load (older) failed:', e);
    } finally {
      _hbtLoadingOlder = false;
    }
    return;
  }

  // ── Bottom boundary ──
  if (atBottom && cache.hasNewer && !_hbtLoadingNewer && !_loadingNewerMessages) {
    _hbtLoadingNewer = true;
    try {
      const newestId = cache.messages.length > 0 ? cache.messages[cache.messages.length - 1].id : null;
      if (!newestId) { cache.hasNewer = false; return; }
      cache.hasNewer = false;
      const data = await _fetchMessages(sessionId, 40, { afterId: newestId, light: true });
      const msgs = data && data.messages;
      if (!msgs || msgs.length === 0) return;
      const seen = new Set(cache.messages.map(m => m.id));
      const incoming = msgs.filter(m => m.id && !seen.has(m.id));
      cache.hasNewer = !!data.has_newer;
      if (incoming.length === 0) return;
      cache.messages = [...cache.messages, ...incoming];
      if (typeof _appendMessagesToTranscript === 'function') {
        _appendMessagesToTranscript(incoming, true);
      } else {
        for (const msg of incoming) {
          if (msg.role === 'user') {
            const bubble = _createBubble('user', msg.content, null, null, null, msg.id, msg.created_at);
            container.appendChild(bubble);
          } else if (msg.role === 'assistant') {
            let text = msg.content || '';
            text = _stripToolCalls(text);
            if (!text.trim()) continue;
            const bubble = _createBubble('agent', text, null, null, msg.id, null, msg.created_at);
            container.appendChild(bubble);
          }
        }
      }
      container.querySelectorAll('.chat-bubble').forEach(b => {
        if (typeof _storeBubbleHeight === 'function') _storeBubbleHeight(b);
      });
    } catch (e) {
      console.warn('Hard-boundary load (newer) failed:', e);
    } finally {
      _hbtLoadingNewer = false;
    }
  }
};

function _renderWelcomeBubble() {
  // Per-agent override (agent Config tab) → app-prompts.json default → built-in.
  // agentChatMsg resolves against the current chat agent (app.currentAgentId).
  // Empty-state placeholder — drops away the moment the user sends (chat-send.js).
  app.addChatBubble('agent', agentChatMsg('welcome_bubble'), 'session-placeholder');
}

// The admin-only Agent/Session diagnostic line no longer renders inside the
// transcript. It now lives in the chat-header "more" (\u22EF) menu \u2014 built + wired in
// ui/chat/js/session-init.js (_wireHeaderMoreMenu), gated to admins
// via the `body.is-admin` class. Removed from here so it can't drift off the top
// of the conversation when older messages page in.

function _renderSessionWindowed(messages, sessionId, run, useFocus, manageRunState, light, anchorOverride) {
  const container = app.chatMessages;
  if (!container) return;

  // This is a full-window render, not an append. It is also used for cache hits
  // and same-session refreshes, so replace the prior projection every time.
  // Previously those paths appended a second copy whose assistant rows had
  // unstable DOM ids, making virtualization appear to "re-render" messages as
  // the user scrolled through overlapping copies.
  container.replaceChildren();
  _placeholderIds.clear();

  // A fresh render never continues a live tool-call group.
  app._activeToolGroupBubble = null;
  app._agentTurnBubble = null;

  // Build the render list. Text/user messages become their own bubbles;
  // consecutive tool-only assistant turns are merged into a single grouped
  // tool-call bubble (one "Turn N" section per turn when expanded).
  const toolResultsByParent = _buildToolResultsByParent(messages);
  const renderables = _buildRenderables(messages, toolResultsByParent, light);

  const activeRun = !!(run && run.active);
  const focus = useFocus ? _sessionFocus.get(sessionId) : null;

  let anchorIdx = renderables.length - 1;
  let atBottom = true;
  if (anchorOverride) {
    // Explicit jump-nav target (session start or a specific anchor id) wins
    // over any saved focus. Not the bottom — anchor the window around it.
    atBottom = false;
    const fi = anchorOverride.atStart
      ? 0
      : renderables.findIndex(m => m.id === anchorOverride.anchorId);
    anchorIdx = fi === -1 ? renderables.length - 1 : fi;
  } else if (!activeRun && focus && focus.atBottom === false && focus.anchorMsgId) {
    const fi = renderables.findIndex(m => m.id === focus.anchorMsgId);
    if (fi !== -1) { anchorIdx = fi; atBottom = false; }
  }
  // A restored scrolled-up position must not be yanked to the bottom by a
  // previously-armed auto-scroll lock (e.g. a chevron click in the previous
  // session): release the lock so the next live append keeps the user where
  // they are, with the down-chevron offered instead.
  if (!atBottom && '_scrollLocked' in app) app._scrollLocked = false;

  let realStart, realEnd;
  const _WINDOW_RADIUS = 20;
  if (atBottom) {
    realEnd = renderables.length - 1;
    realStart = Math.max(0, renderables.length - _WINDOW_RADIUS * 2);
  } else {
    realStart = Math.max(0, anchorIdx - _WINDOW_RADIUS);
    realEnd = Math.min(renderables.length - 1, anchorIdx + _WINDOW_RADIUS);
  }

  let anchorEl = null;
  for (let i = 0; i < renderables.length; i++) {
    const msg = renderables[i];
    if (msg._toolGroup) {
      // Grouped tool-only bubble — always rendered real (small, not placeholdered).
      const el = _emitToolGroupBubble(msg);
      if (i === anchorIdx && el && el.nodeType === 1) anchorEl = el;
      continue;
    }
    if (i >= realStart && i <= realEnd) {
      const el = _emitRealBubble(msg);
      if (el && el.nodeType === 1) _addBubbleActions(el);
      if (i === anchorIdx && el && el.nodeType === 1) anchorEl = el;
    } else if (msg.id) {
      _placeholderIds.add(msg.id);
      const placeholder = _makePlaceholder(msg.id, _getBubbleHeight(msg.id));
      _setBubbleSessionSeq(placeholder, msg.session_seq);
      container.appendChild(placeholder);
    } else {
      const el = _emitRealBubble(msg);
      if (el && el.nodeType === 1) _addBubbleActions(el);
    }
  }

  if (manageRunState) {
    if (activeRun) {
      app.isProcessing = true;
      const seeded = renderables.some(m => m.status === 'streaming');
      if (!seeded && run.assistant_interaction_id
          && typeof app.ensureStreamingBubbleForActiveTurn === 'function') {
        app.ensureStreamingBubbleForActiveTurn(run.assistant_interaction_id);
      }
      if (!app.lastSessionSeq) app.lastSessionSeq = {};
      const floor = typeof run.latest_session_seq === 'number' ? run.latest_session_seq : 0;
      app.lastSessionSeq[sessionId] = Math.max(app.lastSessionSeq[sessionId] || 0, floor);
      // Re-light the live "in-process" indicator from the durable snapshot.
      if (typeof app.chatActivityRestore === 'function') {
        try { app.chatActivityRestore(run.current_op || null); } catch (_) {}
      }
    } else {
      app.isProcessing = false;
    }
  }

  // Activity groups already own their persisted tool rows. Attach tool calls to
  // any assistant TEXT bubble that carries them (final/main answers from the
  // cleanup path, and C-promoted substantive mid-turn messages) — reload must
  // show the same collapsed tool section the live stream does.
  _attachToolCallsFromMessages(messages, light);

  if (atBottom || !anchorEl) {
    const scroller3 = app._chatScroller || container.parentElement;
    scroller3.scrollTop = scroller3.scrollHeight;
  } else {
    container.scrollTop = Math.max(0, anchorEl.offsetTop - 8);
    // Also sync the scroller
    const scroller4 = app._chatScroller || (container && container.parentElement);
    if (scroller4) scroller4.scrollTop = Math.max(0, anchorEl.offsetTop - 8);
  }

  // Swipe-committed load: fade the freshly rendered transcript in over the
  // loading spinner. Flag set by session-swipe.js _commitSwipe, consumed here.
  if (app._swipeFadeIn) {
    app._swipeFadeIn = false;
    container.classList.remove('chat-messages-fadein');
    void container.offsetWidth; // restart the animation if it ran before
    container.classList.add('chat-messages-fadein');
  }
}

// ── Terminal Chat auto-activation ──
// When the loaded session's agent runs the terminal_chat engine, replace the
// chat bubbles with a live xterm.js terminal the MOMENT the session is selected
// — no message needed. Must run on EVERY load path (empty, cached, restricted,
// fresh-with-messages), so it lives here and is called near the top of
// loadSessionChat, BEFORE the early returns. (A brand-new terminal session has
// zero messages, so the old end-of-function placement was unreachable for it —
// the terminal only appeared after the first sent message lit the engine's WS
// event.) Always tears down a previous session's terminal first, so switching to
// a normal agent restores the chat bubbles.
export function _syncTerminalChat(sessionId) {
  if (typeof app.activateTerminalChat !== 'function'
      || typeof app.deactivateTerminalChat !== 'function') return;

  const agentId = app.currentAgentId;
  // Tear down a previous session's terminal before (maybe) mounting a new one.
  try { app.deactivateTerminalChat(); } catch (_) {}
  if (!agentId || !sessionId) return;

  // Only the terminal_chat engine gets a terminal. '' = the agent isn't in the
  // dropdown cache yet (cold boot) — attempt anyway; the backend ignores the
  // request for non-terminal agents, so a normal agent never mounts one.
  const engine = _agentEngineFor(agentId);
  // Do not probe when this agent is absent from the cache: stale/cold state
  // must not activate terminal chat for an ordinary session.
  if (engine !== 'terminal_chat') return;

  // Defer a tick so the (now-hidden) bubble render settles first.
  setTimeout(() => {
    try { app.activateTerminalChat(sessionId, agentId); } catch (_) {}
  }, 50);
}

export async function loadSessionChat(sessionId) {
  if (typeof app.loadChatComponents === 'function') {
    try { app.loadChatComponents(sessionId); } catch (_) { /* optional UI */ }
  }
  // Lift any "Session not found" lock from a previously-open deleted session so
  // the composer works again on this live one (no-op when nothing was locked).
  if (typeof app.clearSessionNotFound === 'function') app.clearSessionNotFound();
  // Restore per-session execution mode (Ask/Plan/Auto) when loading any session
  if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
  if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
  if (typeof app.reloadFooterExpanded === 'function') app.reloadFooterExpanded();

  if (typeof app.refreshActiveAbilities === 'function') {
    try { app.refreshActiveAbilities(); } catch (_) { /* best-effort */ }
  }
  if (typeof app.refreshTunnelForSession === 'function') {
    try { app.refreshTunnelForSession(); } catch (_) { /* best-effort */ }
  }

  const isSwitch = sessionId !== app._lastLoadedSessionId;
  if (isSwitch && app._lastLoadedSessionId) {
    _captureSessionFocus(app._lastLoadedSessionId);
  }

  // The on-screen session ALWAYS has priority: flag the load as in-flight and
  // cancel any background neighbour pre-load immediately, so a warm-up fetch can
  // never make the session the user is opening wait behind it.
  app._sessionLoadInFlight = true;
  try { if (typeof app.abortNeighborWarm === 'function') app.abortNeighborWarm(); } catch (_) {}

  // Mount/tear down the live terminal up front so it survives every early return
  // below (empty session, cache hit, restricted) — not just the full-fetch path.
  _syncTerminalChat(sessionId);

  try {
    const cached = _messageCache.get(sessionId);
    const now = Date.now();
    if (cached && (now - cached.loadedAt) < _CACHE_TTL_MS) {
      if (sessionId !== app._lastLoadedSessionId) {
        _teardownVirtualScroll();
        app.chatMessages.innerHTML = '';
      }
      app._lastLoadedSessionId = sessionId;

      const oldBtn = document.getElementById(`load-earlier-${sessionId}`);
      if (oldBtn) oldBtn.remove();

      if (cached.messages.length === 0) {
        _installVirtualScroll();
        _armSessionWatch(sessionId, [], cached.maxSeq);
        return;
      }

      _renderSessionWindowed(cached.messages, sessionId, null, isSwitch, false, cached.light !== false);

      if (typeof app.setContextFromMessages === 'function') {
        app.setContextFromMessages(cached.messages);
      }
      if (cached.contextTokens && typeof app.setContextTokens === 'function') {
        app.setContextTokens(cached.contextTokens);
      }
      // Record the manifest from the cache so a later refresh check is cheap.
      if (cached.manifest) _rememberSessionManifest(sessionId, cached.manifest);

      _installVirtualScroll();
      _armSessionWatch(sessionId, cached.messages, cached.maxSeq);
      if (typeof app.seedReconcileSig === 'function') {
        app.seedReconcileSig(sessionId, cached.messages);
      }
      return;
    }

    // Cache miss — fetch from API. Open CENTRED on the saved scroll position
    // (a small window around it) when there is one; otherwise the newest slice.
    const savedFocus = _sessionFocus.get(sessionId);
    const openOpts = { light: true };
    let openLimit = _OPEN_NEWEST_LIMIT;
    if (savedFocus && savedFocus.atBottom === false && savedFocus.anchorMsgId) {
      openOpts.aroundId = savedFocus.anchorMsgId;
      openLimit = _OPEN_AROUND_RADIUS;
    }
    let data = await _fetchMessages(sessionId, openLimit, openOpts);

    // Stale-focus guard: when the saved anchor is far from the session's true
    // tail (e.g. turns arrived AFTER the focus was captured, like during crash
    // recovery), discard the stale position and re-fetch the newest messages.
    if (openOpts.aroundId && data && data.has_newer && data.max_session_seq) {
      const msgs = data.messages || [];
      let maxWindowSeq = 0;
      for (const m of msgs) {
        if (typeof m.session_seq === 'number' && m.session_seq > maxWindowSeq) {
          maxWindowSeq = m.session_seq;
        }
      }
      if (maxWindowSeq > 0 && (data.max_session_seq - maxWindowSeq) > _OPEN_AROUND_RADIUS * 2) {
        // More new messages exist beyond the window than the window itself -
        // the saved scroll position is stale. Discard it and load the tail.
        _sessionFocus.delete(sessionId);
        _persistSessionFocus();
        delete openOpts.aroundId;
        openLimit = _OPEN_NEWEST_LIMIT;
        data = await _fetchMessages(sessionId, openLimit, openOpts);
      }
    }

    if (data.restricted && getActive() && getActive().remember_token) {
      await ensureFreshToken();
      data = await _fetchMessages(sessionId, openLimit, openOpts);
    }

    // A newest-N row window can begin halfway through a tool-heavy turn. Load
    // backward through that turn's user boundary so refresh never presents an
    // orphaned tail that appears to be an incomplete conversation.
    if (!openOpts.aroundId && !data.restricted) {
      data = await _extendTailToOwningUser(sessionId, data, true);
    }

    // The server records the mode every message runs in, making the SESSION the
    // source of truth. Apply it so the pill matches what the server will do —
    // correct across devices/reloads, not just in the browser that last toggled
    // it. This overrides the localStorage-seeded reloadExecutionMode() above.
    // (setExecutionMode persists to the per-session key, so the cached-load path
    // stays right too.) currentSessionId is the loaded id by now (switchToSession
    // sets it before loadSessionChat), so the key is written for the right session.
    if (!data.restricted && data.execution_mode && typeof app.setExecutionMode === 'function') {
      try { app.setExecutionMode(data.execution_mode); } catch (_) { /* best-effort */ }
    }

    // Remote Control executor is likewise a SESSION property (metadata), so the
    // server is the source of truth across devices — apply it on load, overriding
    // the localStorage seed from reloadTargetDevice() above. Null ⇒ run locally.
    if (!data.restricted && typeof app.setTargetDevice === 'function') {
      const _re = data.remote_executor;
      try {
        if (_re && _re.instance_id) {
          // Session has an explicitly stored executor → it wins (across devices).
          app.setTargetDevice(_re.instance_id, _re.label || '');
        } else {
          // No stored executor on the server. If the user already has a local
          // choice (from localStorage, loaded by reloadTargetDevice above),
          // keep it — don't clear it. Otherwise fall back to the agent's
          // configured default device (applied only if it's online; else stays
          // local). The agent default is non-persisting and online-checked.
          if (!app.targetDevice) {
            if (typeof app.applyAgentDefaultTarget === 'function') {
              app.applyAgentDefaultTarget(sessionId);
            }
          }
        }
      } catch (_) { /* best-effort */ }
    }

    if (sessionId !== app._lastLoadedSessionId) {
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
    }
    app._lastLoadedSessionId = sessionId;

    if (data.restricted) {
      app.currentSessionId = randomUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      // This fresh fallback session replaces the restricted one AFTER the earlier
      // reloadExecutionMode() ran for the old id — re-sync the pill for the new id
      // (per-session key, defaults to Ask) so it can't carry a stale mode.
      if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
      if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
      if (typeof app.reloadFooterExpanded === 'function') app.reloadFooterExpanded();
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
      // Empty-state placeholder — drops away the moment the user sends (chat-send.js).
      app.addChatBubble('agent', agentChatMsg('new_session_bubble'), 'session-placeholder');
      if (app.currentUserId && typeof app.populateSessionSelect === 'function') {
        app.populateSessionSelect(app.currentUserId);
      }
      _messageCache.delete(sessionId);
      if (typeof app.setContextFromMessages === 'function') {
        app.setContextFromMessages([]);
      }
      return;
    }

    const msgs = _sortMessagesCanonical(data.messages || []);
    const isLight = data.light !== false;
    // Record the server's transcript manifest so a later refresh can ask
    // "did anything change?" without re-downloading the transcript.
    _rememberSessionManifest(sessionId, data.manifest);
    _messageCache.set(sessionId, {
      messages: [...msgs],
      hasMore: !!data.has_more,
      hasNewer: !!data.has_newer,
      light: isLight,
      maxSeq: data.max_session_seq || 0,
      contextTokens: data.context_tokens || 0,
      manifest: data.manifest || null,
      loadedAt: Date.now(),
    });

    if (typeof app.setContextFromMessages === 'function') {
      app.setContextFromMessages(msgs);
    }
    // Prefer the server's whole-session token estimate — the small open window
    // alone would under-report the ctx indicator.
    if (data.context_tokens && typeof app.setContextTokens === 'function') {
      app.setContextTokens(data.context_tokens);
    }
    if (data.usage && typeof app.setSessionUsage === 'function') {
      app.setSessionUsage(data.usage);
    }

    if (msgs.length === 0) {
      if (typeof app.setContextFromMessages === 'function') {
        app.setContextFromMessages([]);
      }
      _armSessionWatch(sessionId, [], data.max_session_seq);
      return;
    }

    const run = data.run || null;
    _renderSessionWindowed(msgs, sessionId, run, isSwitch, true, isLight);

    if (app.chatSend) app.chatSend.disabled = !((app.chatInput && app.chatInput.value.trim()));

    _installVirtualScroll();
    _armSessionWatch(sessionId, msgs, data.max_session_seq);
    if (typeof app.seedReconcileSig === 'function') {
      app.seedReconcileSig(sessionId, msgs);
    }

    try { _fetchRelatedSessions(); } catch (_) {}
  } catch (e) {
    console.warn('Failed to load session messages:', e);
  } finally {
    app._sessionLoadInFlight = false;
    // Active session has settled — warm the next/prev pinned neighbours, but only
    // when the browser is idle (the warmer self-gates on isProcessing / load state).
    try { if (typeof app.warmNeighborSessions === 'function') app.warmNeighborSessions(); } catch (_) {}
  }
  // (Terminal Chat is activated up front by _syncTerminalChat near the top of
  // this function, so it survives every early return above.)
}

app.loadSessionChat = loadSessionChat;

// Hard-refresh the transcript area for the CURRENT session: drop the cached
// messages, clear the rendered bubbles, and re-fetch + re-render from the DB.
// Resets ONLY the chat transcript — it never reloads the page. Wired to the
// refresh button beside the agent name in the chat header (chat-side-panel.html
// → session-init.js). Differs from app.reloadCurrentSession (which reuses the
// cache and is a no-op clear on the already-loaded session): this forces a
// clean rebuild from the database.
export async function refreshTranscript(force) {
  const sessionId = app.currentSessionId;
  if (!sessionId) return;
  // Remote-change guard: ask the server whether the transcript actually changed
  // since we last rendered it (manifest_only validation against the stored
  // revision/hash). If it is NOT modified, do NOTHING — no cache drop, no
  // virtual-scroll teardown, no re-render. The user's screen must not update,
  // and the rubber-band pull animation (applyRubberBand) is independent of this
  // function, so it keeps its effect either way. Only an explicit server-side
  // change (or an unknown/unreachable state) proceeds to a real refresh.
  // force=true (an explicit user gesture, e.g. the bottom pull-to-refresh)
  // skips the guard: the user asked for a refresh, so always re-sync from the DB.
  if (!force) {
    try {
      const changed = await _transcriptChangedRemotely(sessionId);
      if (!changed) return;
    } catch (_) { /* helper swallows errors — proceed conservatively */ }
  }
  // Remember where the user was so the reload lands in the same place.
  try { _captureSessionFocus(sessionId); } catch (_) {}
  // Drop the cache so loadSessionChat re-fetches fresh from the DB.
  try { _messageCache.delete(sessionId); } catch (_) {}
  // Tear down the virtual-scroll hooks so the re-render starts clean, but leave
  // the existing bubbles in place — _renderSessionWindowed does a synchronous
  // replaceChildren() right before it populates, so the user sees no white flash.
  _teardownVirtualScroll();
  // Keep _lastLoadedSessionId set so loadSessionChat treats this as a same-session
  // operation and skips its own clear / re-teardown.
  await loadSessionChat(sessionId);
}

app.refreshTranscript = refreshTranscript;

// Narrow test/debug surface for exercising pagination as a projection problem
// without reaching through scroll event timing.
export {
  _buildPhasedRenderables,
  _prependMessagesToTranscript,
  _reprojectCachedTranscript,
  _renderSessionWindowed,
};
