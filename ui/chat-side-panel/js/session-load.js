'use strict';

// Session loader — fetch session messages, handle `restricted` (token refresh +
// retry, auto-create fresh session), windowed virtual-scroll render, attach
// persisted tool-call panels, infinite scroll. Sets app.loadSessionChat.
// Module map for this folder: ui/chat-side-panel/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { getActive } from '../../shared/js/accounts.js';
import { ensureFreshToken } from '../../shared/js/auth-refresh.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { addChatBubble, _renderMarkdownBody, linkifyText, _parseCreatedAt } from './chat-bubble.js';
import { renderAttachmentElement } from '../../shared/js/attachments.js';
import {
  _addBubbleActions,
  _bubbleAnchorId,
  _getBubbleText,
  _setBubbleModelFromMeta,
} from './chat-bubble-actions.js';
import {
  _stripToolCalls,
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
import { agentChatMsg } from '../../shared/js/app-prompts.js';
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
  let mx = app.lastSessionSeq[sessionId] || 0;
  if (typeof floorSeq === 'number' && floorSeq > mx) mx = floorSeq;
  if (Array.isArray(messages)) {
    for (const m of messages) {
      if (typeof m.session_seq === 'number' && m.session_seq > mx) mx = m.session_seq;
    }
  }
  app.lastSessionSeq[sessionId] = mx;
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
// bodies (arguments, results, LLM input/output) were never downloaded, so each
// call is stamped with `_needsDetail` + `_detailMsgId`/`_detailIdx` so the panel
// can lazy-fetch them via /session-turn-detail the first time it's opened.
function _buildCallsForMessage(msg, toolResultsByParent, turnNum, light) {
  if (!msg.output) return [];
  let outputData;
  try { outputData = JSON.parse(msg.output); } catch (_) { return []; }
  const toolCalls = outputData.tool_calls;
  if (!toolCalls || toolCalls.length === 0) return [];
  return toolCalls.map((tc, i) => {
    let args = {};
    try { args = JSON.parse(tc.function.arguments); } catch (_) {}
    const toolName = tc.function.name;
    const results = toolResultsByParent[msg.id] || [];
    const resultEntry = results.find(r => r.tool_name === toolName);
    return {
      tool: toolName,
      args: args,
      status: 'done',
      result: resultEntry ? resultEntry.content : null,
      durationMs: resultEntry ? _parseDuration(resultEntry.metadata) : null,
      errorType: null,
      turn: turnNum || 0,
      open: false,
      _savedInput: msg.input || null,
      _savedOutput: msg.output || null,
      _savedToolOutput: resultEntry ? resultEntry.output : null,
      _savedToolMetadata: resultEntry ? resultEntry.metadata : null,
      _detailMsgId: msg.id || null,
      _detailIdx: i,
      _needsDetail: !!light,
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

// Synthetic STANDALONE tool rows — written outside the normal assistant
// tool_call pairing, so the parent-lookup renderer skips them. Two kinds:
//   • vision ingestion — process_image (describe path) / route_attachment
//     (unreadable path), parented to the USER turn before the reply exists;
//   • loop-node memory — memory_search (runs before the turn) / memory_save
//     (runs after), tagged metadata.brain on the backend.
// The backend flags every one `_synth_tool` (vision also has the parent's role
// as a fallback signal). We render each as its own foldable tool-call bubble in
// chronological position — search before the reply, save after.
const _SYNTH_TOOLS = {
  process_image: true, route_attachment: true,
  memory_search: true, memory_save: true,
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
// Its body ships even in light mode (see db_viewer _slim), so the panel shows
// the saved args (image prompt / search query) + result with no lazy fetch.
function _buildSynthCall(msg) {
  let args = {};
  try { if (msg.input) args = JSON.parse(msg.input) || {}; } catch (_) {}
  let meta = {};
  try { if (msg.metadata) meta = JSON.parse(msg.metadata) || {}; } catch (_) {}
  return {
    tool: msg.tool_name,
    args: args,
    status: meta.error ? 'error' : 'done',
    result: (msg.content != null && msg.content !== '') ? msg.content : (msg.output || ''),
    durationMs: (typeof meta.duration_ms === 'number') ? meta.duration_ms : null,
    errorType: null,
    turn: 0,
    open: false,
    _savedInput: msg.input || null,
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
function _buildRenderables(messages, toolResultsByParent, light) {
  const renderables = [];
  const idToRole = {};
  for (const m of messages) { if (m && m.id) idToRole[m.id] = m.role; }
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
    if (msg.role === 'user') { flushSynth(); flushGroup(); renderables.push(msg); continue; }
    // Synthetic standalone tool rows (vision ingestion, loop-node memory) render
    // as their own bubble — they have no assistant tool_call to attach to.
    if (_isSynthToolRow(msg, idToRole)) {
      flushGroup();
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
    if (msg.status === 'streaming' || text.trim()) {
      flushGroup();
      renderables.push(msg);
      continue;
    }
    // Tool-only turn — accumulate into the current group (skip empty turns).
    const peekCalls = _buildCallsForMessage(msg, toolResultsByParent, 0, light);
    if (peekCalls.length === 0) continue;
    if (!pendingGroup) {
      pendingGroup = { _toolGroup: true, id: 'toolgroup-' + (msg.id || renderables.length), calls: [], turnCount: 0 };
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
    if (msg._toolGroup) { _emitToolGroupBubble(msg); continue; }
    const el = _emitRealBubble(msg);
    if (el && el.nodeType === 1) _addBubbleActions(el);
  }
  _attachToolCallsFromMessages(messages, light);
}

// Fetch a window of session messages. `opts`: { beforeId, afterId, aroundId,
// light }. Light defaults ON for the chat (headings only; bodies load on
// expand). beforeId/afterId page older/newer; aroundId opens centred on a saved
// position.
async function _fetchMessages(sessionId, limit, opts) {
  opts = opts || {};
  const token = localStorage.getItem('auth_token');
  let url = apiPath(`/api/v1/db/session-messages?db=local.db&session_id=${encodeURIComponent(sessionId)}&limit=${limit}`);
  if (opts.light !== false) url += '&light=1';
  if (opts.beforeId) url += `&before_id=${encodeURIComponent(opts.beforeId)}`;
  if (opts.afterId) url += `&after_id=${encodeURIComponent(opts.afterId)}`;
  if (opts.aroundId) url += `&around_id=${encodeURIComponent(opts.aroundId)}`;
  if (token) url += `&token=${encodeURIComponent(token)}`;
  const res = await fetch(url);
  return await res.json();
}

async function _maybeLoadMoreOnScrollTop(sessionId) {
  if (!sessionId) return;
  if (_loadingMoreMessages) return;
  const container = app.chatMessages;
  if (!container) return;
  const cache = _messageCache.get(sessionId);
  if (!cache || !cache.hasMore) return;
  if (container.scrollTop > 150) return;

  _loadingMoreMessages = true;
  try {
    const oldestId = cache.messages.length > 0 ? cache.messages[0].id : null;
    if (!oldestId) { cache.hasMore = false; return; }
    const data = await _fetchMessages(sessionId, _OLDER_BATCH, { beforeId: oldestId, light: true });
    if (!data.messages || data.messages.length === 0) {
      cache.hasMore = false;
      return;
    }
    cache.messages = [...data.messages, ...cache.messages];
    cache.hasMore = !!data.has_more;
    const oldScrollHeight = container.scrollHeight;
    const firstChild = container.firstChild;
    for (const msg of data.messages) {
      if (msg.role === 'user') {
        const bubble = _createBubble('user', msg.content, null, null, null, msg.id, msg.created_at);
        _appendUserAttachments(bubble, msg);
        container.insertBefore(bubble, firstChild);
        if (typeof app._addBubbleActions === 'function' && !bubble.classList.contains('streaming')) {
          try { app._addBubbleActions(bubble); } catch(_) {}
        }
      } else if (msg.role === 'assistant') {
        let text = msg.content || '';
        text = _stripToolCalls(text);
        const hasText = !!text.trim();
        if (!hasText && msg.status !== 'streaming') continue;
        const cls = msg.status === 'streaming' ? 'streaming' :
                    msg.status === 'interrupted' ? 'interrupted' :
                    msg.status === 'error' ? 'error' : null;
        const bubble = _createBubble('agent', text, cls, null, msg.id, null, msg.created_at);
        if (msg.metadata) _setBubbleModelFromMeta(bubble, msg.metadata);
        container.insertBefore(bubble, firstChild);
        if (typeof app._addBubbleActions === 'function' && !bubble.classList.contains('streaming')) {
          try { app._addBubbleActions(bubble); } catch(_) {}
        }
      }
    }
    _adjustScrollForPrepend(container, oldScrollHeight);
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
  const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
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
    cache.messages = [...cache.messages, ...incoming];
    _appendMessagesToTranscript(incoming, cache.light !== false);
    container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
  } catch (e) {
    console.warn('Failed to load newer messages:', e);
  } finally {
    _loadingNewerMessages = false;
  }
}

function _adjustScrollForPrepend(container, oldScrollHeight) {
  const newScrollHeight = container.scrollHeight;
  const delta = newScrollHeight - oldScrollHeight;
  container.scrollTop += delta;
}

function _createBubble(role, text, extraClass, imageUrl, turnId, msgId, createdAt) {
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (turnId) bubble.setAttribute('data-turn-id', turnId);
  if (msgId) bubble.setAttribute('data-msg-id', msgId);
  const createdAtMs = createdAt ? _parseCreatedAt(createdAt) : Date.now();
  bubble.setAttribute('data-created-at', createdAtMs);
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

function _emitRealBubble(msg) {
  if (msg.role === 'user') {
    const _ub = app.addChatBubble('user', msg.content, undefined, undefined, undefined, msg.id, msg.created_at);
    _appendUserAttachments(_ub, msg);
    return _ub;
  }
  let text = msg.content || '';
  text = _stripToolCalls(text);
  const hasText = !!text.trim();
  let el;
  if (msg.status === 'streaming') {
    el = (typeof app.seedStreamingBubble === 'function')
      ? app.seedStreamingBubble(msg.id, text)
      : app.addChatBubble('agent', text || '\u2026', 'streaming', undefined, msg.id, msg.created_at);
  } else if (!hasText) {
    return null;
  } else if (msg.status === 'interrupted') {
    el = app.addChatBubble('agent', text + '\n\n(interrupted)', 'interrupted', undefined, msg.id, msg.created_at);
  } else if (msg.status === 'error') {
    el = app.addChatBubble('agent', text, 'error', undefined, msg.id, msg.created_at);
  } else {
    el = app.addChatBubble('agent', text, undefined, undefined, msg.id, msg.created_at);
  }
  // Stamp the per-turn model tag before the action row is built by the caller.
  if (el && el.nodeType === 1 && msg.metadata) _setBubbleModelFromMeta(el, msg.metadata);
  return el;
}

// Render one grouped tool-only bubble (consecutive tool-only turns merged).
function _emitToolGroupBubble(group) {
  const bubble = app.addChatBubble('agent', '', 'tool-only', undefined, group.id);
  if (bubble && bubble.nodeType === 1 && typeof app.attachToolCallsToLastBubble === 'function') {
    app.attachToolCallsToLastBubble(group.calls, bubble);
  }
  return bubble;
}

// Expose for virtual-scroll
app._maybeLoadMoreOnScrollTop = _maybeLoadMoreOnScrollTop;
app._maybeLoadMoreOnScrollBottom = _maybeLoadMoreOnScrollBottom;

function _renderWelcomeBubble() {
  // Per-agent override (agent Config tab) → app-prompts.json default → built-in.
  // agentChatMsg resolves against the current chat agent (app.currentAgentId).
  // Empty-state placeholder — drops away the moment the user sends (chat-send.js).
  app.addChatBubble('agent', agentChatMsg('welcome_bubble'), 'session-placeholder');
}

// The admin-only Agent/Session diagnostic line no longer renders inside the
// transcript. It now lives in the chat-header "more" (\u22EF) menu \u2014 built + wired in
// ui/chat-side-panel/js/session-init.js (_wireHeaderMoreMenu), gated to admins
// via the `body.is-admin` class. Removed from here so it can't drift off the top
// of the conversation when older messages page in.

function _renderSessionWindowed(messages, sessionId, run, useFocus, manageRunState, light) {
  const container = app.chatMessages;
  if (!container) return;

  // A fresh render never continues a live tool-call group.
  app._activeToolGroupBubble = null;

  // Build the render list. Text/user messages become their own bubbles;
  // consecutive tool-only assistant turns are merged into a single grouped
  // tool-call bubble (one "Turn N" section per turn when expanded).
  const toolResultsByParent = _buildToolResultsByParent(messages);
  const renderables = _buildRenderables(messages, toolResultsByParent, light);

  const activeRun = !!(run && run.active);
  const focus = useFocus ? _sessionFocus.get(sessionId) : null;

  let anchorIdx = renderables.length - 1;
  let atBottom = true;
  if (!activeRun && focus && focus.atBottom === false && focus.anchorMsgId) {
    const fi = renderables.findIndex(m => m.id === focus.anchorMsgId);
    if (fi !== -1) { anchorIdx = fi; atBottom = false; }
  }

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
      container.appendChild(_makePlaceholder(msg.id, _getBubbleHeight(msg.id)));
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

  _attachToolCallsFromMessages(messages, light);

  if (atBottom || !anchorEl) {
    container.scrollTop = container.scrollHeight;
  } else {
    container.scrollTop = Math.max(0, anchorEl.offsetTop - 8);
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
  if (engine && engine !== 'terminal_chat') return;

  // Defer a tick so the (now-hidden) bubble render settles first.
  setTimeout(() => {
    try { app.activateTerminalChat(sessionId, agentId); } catch (_) {}
  }, 50);
}

export async function loadSessionChat(sessionId) {
  // Restore per-session execution mode (Ask/Plan/Auto) when loading any session
  if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
  if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();

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
        _renderWelcomeBubble();
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

      _installVirtualScroll();
      _armSessionWatch(sessionId, cached.messages, cached.maxSeq);
      return;
    }

    // Cache miss — fetch from API. Open CENTRED on the saved scroll position
    // (a small window around it) when there is one; otherwise the newest slice.
    // Either way the tool-call bodies aren't downloaded (light) — only headings.
    const savedFocus = _sessionFocus.get(sessionId);
    const openOpts = { light: true };
    let openLimit = _OPEN_NEWEST_LIMIT;
    if (savedFocus && savedFocus.atBottom === false && savedFocus.anchorMsgId) {
      openOpts.aroundId = savedFocus.anchorMsgId;
      openLimit = _OPEN_AROUND_RADIUS;
    }
    let data = await _fetchMessages(sessionId, openLimit, openOpts);

    if (data.restricted && getActive() && getActive().remember_token) {
      await ensureFreshToken();
      data = await _fetchMessages(sessionId, openLimit, openOpts);
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

    const msgs = data.messages || [];
    const isLight = data.light !== false;
    _messageCache.set(sessionId, {
      messages: [...msgs],
      hasMore: !!data.has_more,
      hasNewer: !!data.has_newer,
      light: isLight,
      maxSeq: data.max_session_seq || 0,
      contextTokens: data.context_tokens || 0,
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

    if (msgs.length === 0) {
      _renderWelcomeBubble();
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
export async function refreshTranscript() {
  const sessionId = app.currentSessionId;
  if (!sessionId) return;
  // Remember where the user was so the reload lands in the same place.
  try { _captureSessionFocus(sessionId); } catch (_) {}
  // Drop the cache so loadSessionChat re-fetches fresh from the DB.
  try { _messageCache.delete(sessionId); } catch (_) {}
  // Force a clean rebuild: tear down the virtual scroll, clear the bubbles, and
  // make loadSessionChat treat this as a fresh load (not a same-session no-op).
  _teardownVirtualScroll();
  if (app.chatMessages) app.chatMessages.innerHTML = '';
  app._lastLoadedSessionId = null;
  await loadSessionChat(sessionId);
}

app.refreshTranscript = refreshTranscript;
