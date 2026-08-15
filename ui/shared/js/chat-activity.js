'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';
import { copyText } from './clipboard.js';
import { renderVaultCredentialCard } from '../../vault-credential/vault-credential-card.js';

// When the agent's request_credential tool (Visualizer ability) returns, its
// result carries ui:"vault_credential_form" — pop the secure entry card so the
// user can type the secret straight into the vault (never back through the agent).
// Live events only (historical session loads don't replay through here), so a
// reload won't re-pop a stale card.
function _maybeShowVaultCard(event) {
  if (!event || event.tool !== 'request_credential' || event.error) return;
  try {
    const payload = typeof event.result === 'string' ? JSON.parse(event.result) : event.result;
    if (payload && payload.ui === 'vault_credential_form' && payload.key_id) {
      renderVaultCredentialCard(payload);
    }
  } catch (_) { /* not a card payload — ignore */ }
}

function _maybeShowChatComponent(event) {
  if (!event || event.error) return;
  try {
    const payload = typeof event.result === 'string' ? JSON.parse(event.result) : event.result;
    if (payload && payload.ui === 'chat_component' && payload.component && app.presentChatComponent) {
      app.presentChatComponent(payload.component).catch(() => {});
    }
  } catch (_) { /* not a component payload */ }
}

// ── Chat activity ("thinking") indicator ───────────────────────────────────
// While the agent is working on the CURRENT session's turn, the web-chat pill
// glows ("thinking") and a small bar above it ticks through each interaction as
// a short note — e.g. "Toolcall get_time", "Searching memory", "Writing reply…".
//
// The bar is CLICKABLE: it expands a panel listing every tool call made this
// turn, each row independently expandable to show its full arguments + result.
// Tool calls accumulate across all steps of the turn. When the turn ends, the
// glow + pulse stop; if the turn made any tool calls the bar stays in a resting
// state ("N tool calls") so you can still open it and inspect — until the next
// turn resets it or you switch sessions.
//
// Driven entirely by the per-user WebSocket event stream (see agentWs.js, which
// forwards every current-session event to app._chatActivityHandler). Decoupled
// from the chat bubble pipeline: chat.js just calls app.chatActivityStart()/
// Stop() for instant feedback on send + HTTP-error paths that never produce a
// WebSocket terminal event.

let rootEl = null;   // #chat-activity (container)
let pillEl = null;   // #chat-input-row (the .chat-pill — glows)
let barEl = null;    // #chat-activity-bar (clickable header chip)
let textEl = null;   // .chat-activity-text (the ticking note)
let panelEl = null;  // #chat-activity-panel (tool-call list)

// ── Token bar elements ──
let tokenBarEl = null;   // #chat-token-bar
let tokensInEl = null;   // #chat-tokens-in
let tokensOutEl = null;  // #chat-tokens-out
let tokenSpinnerEl = null; // #chat-token-spinner
let footerLeftEl = null;  // #chat-pill-stats — click target for the context panel (contains tokens/ctx/cost)
let footerRowEl = null;   // #chat-pill-stats — measured to fit the stats (FOOTER-STATS-FIT)
let modelBtnEl = null;   // #chat-model-btn — dedicated "switch model" button below the pill
let modelCtxEl = null;   // #chat-model-ctx (live context tokens / model's max limit)
let costEl = null;       // #chat-cost (session cost so far)
let _footerFitPending = false; // rAF debounce flag for _fitFooterStats
let _lastFitRowWidth = -1;     // last observed row width (ignore height-only RO ticks)
let _modelListPanelEl = null; // model list panel element (hoisted for cross-function access)
let _modelListPanelList = null;
let _contextTokens = 0;  // live assembled context tokens (from context_status events)
let _modelContextLimit = null; // model's context window limit (from current-model-info)
let _costInput = null;   // active model's USD price per 1M input tokens (from current-model-info)
let _costOutput = null;  // active model's USD price per 1M output tokens (from current-model-info)
// Alternate-engine agents (e.g. Local Claude Code) don't bill on the app's model,
// so the session cost is meaningless for them — keep the token/ctx counters but
// hide the price chip. Set from current-model-info's `engine` field.
let _altEngine = false;  // true when the active agent runs on a non-default engine
let _altEngineId = '';   // which engine ('claude_code' | 'codex' | …) — '' when native
let cumulativeIn = 0;
let cumulativeOut = 0;
// LocalStorage cache key for token counters — survives page refreshes.
// Format: JSON {sessionId, in, out} — only shown when sessionId matches.
const _TOKEN_CACHE_KEY = 'chat_token_counters';

function _saveTokenCache() {
  const sid = app.currentSessionId || '';
  if (!sid) return;
  try {
    localStorage.setItem(_TOKEN_CACHE_KEY, JSON.stringify({
      sessionId: sid,
      in: cumulativeIn,
      out: cumulativeOut,
      ts: Date.now(),
    }));
  } catch (_) { /* quota exceeded — non-critical */ }
}

function _loadTokenCache() {
  const sid = app.currentSessionId || '';
  if (!sid) return;
  try {
    const raw = localStorage.getItem(_TOKEN_CACHE_KEY);
    if (!raw) return;
    const cached = JSON.parse(raw);
    if (cached && cached.sessionId === sid && (cached.in > 0 || cached.out > 0)) {
      cumulativeIn = cached.in;
      cumulativeOut = cached.out;
      if (tokensInEl) tokensInEl.textContent = cumulativeIn.toLocaleString();
      if (tokensOutEl) tokensOutEl.textContent = cumulativeOut.toLocaleString();
      _updateTokenBar();
    }
  } catch (_) { /* corrupt cache — ignore */ }
}
// Running session cost in USD — the SUM of each call's locked-in cost (server
// computes cost_usd per call at that call's own model rate). Accumulating per
// call (never totals × current rate) keeps it accurate across model switches.
// Reconciled against /session-cost on session load.
let _sessionCostUsd = 0;
let _streamCharCount = 0;     // chars streamed in current ongoing LLM call
let _pendingOutEstimate = 0;  // estimated output tokens for current streaming call
let _pendingCtxEstimate = 0;  // estimated ctx tokens during thinking ramp (replaced by real data)
let _thinkingRamp = null;     // interval handle for the pre-stream thinking ramp

let active = false;     // a turn is in progress
let resting = false;    // turn ended but tool calls remain to inspect
let expanded = false;   // panel open
let currentNote = '';
let clearTimer = null;  // delayed text-clear after a no-tools fade-out
let endTimer = null;    // delayed stop() after a terminal Error/Stopped note
let watchdog = null;    // safety auto-stop if a turn never reports completion

// POST-TURN HOUSEKEEPING SETTLE ──────────────────────────────────────────────
// A turn's terminal event (`response`/`interrupted`/`error`) is NOT the last
// thing the backend emits: fire-and-forget background steps run AFTER it — the
// memory upsert (memory_save_start/_end), turn hooks, the optimizer, etc. Each
// emits a `*_start` note that re-lights the bar, but the bar's only general way
// to clear an active note is a terminal event or the 3-min watchdog — so a
// post-turn `*_start` with no recognised terminal would hang the chip (e.g.
// "Saving memory") for three minutes. `_turnEnded` marks that we're past the
// turn's terminal event; while set, a re-light uses the short backstop below
// and any matching `*_end`/completion step clears the bar promptly. Reset at the
// next real turn boundary (user_message / new turn / session switch).
let _turnEnded = false;
// Backstop for a post-turn re-light whose completion event never arrives (the
// save errored before emitting `*_end`, or the event was dropped). The matching
// completion step normally clears the bar within ~1s; this only bounds the worst
// case so a post-turn note can't linger anywhere near the 3-min watchdog.
const POST_TURN_SETTLE_MS = 15000;

// One entry per tool call this turn: { tool, args, status, result, durationMs,
// errorType, turn, open }. status: 'running' | 'done' | 'error'.
let toolCalls = [];
// Durable assistant row that owns the current inference turn's tool calls.
// A live tool-only line uses this to acquire the same transcript identity and
// ordering key that the saved projection receives after refresh.
let currentToolAnchor = null;

// SYNTHETIC STANDALONE tool calls fire OUTSIDE a reply bubble's normal tool-call
// accumulation, so they're rendered out-of-band straight onto the transcript as
// their own foldable tool bubble (see _renderSynthToolBubble), mirroring the
// reload renderer (session-load.js _buildSynthCall). Two kinds:
//   • vision ingestion — process_image / route_attachment, emitted during
//     user-turn ingestion (before turn 1, before any reply bubble); the turn
//     reset that brackets ingestion would otherwise wipe the activity list.
//   • loop-node memory — memory_search (before the turn) / memory_save (after);
//     neither is a model-issued tool call, and memory_save fires post-`response`.
//     They render as small debug notes in the transcript instead of bubbles.
// Args arrive differently per tool, so we stash them by tool name until the
// completion event lands: vision via its tool_call event; memory_search via its
// memory_search_start pipeline step; memory_save's args ride its memory_save_end.
const _SYNTH_TOOLS = {
  process_image: true, route_attachment: true,
  memory_search: true, memory_save: true,
  app_control: true,   // App Control point-and-share fingerprint (user hand-off)
};
let _pendingSynthArgs = {};   // toolName → args, awaiting that tool's completion event

// Tracks whether the chat layer has created at least one bubble for the current
// turn's output (text or tool calls). chat-stream.js sets this to true when it
// creates/finds a bubble. Cleared on new-turn boundaries so the first tool_call
// of a new turn knows no bubble exists yet and creates one.
// Exposed on app so chat-stream.js can read/write it cross-module.
app._turnHasBubble = false;

// Append a small debug note to the chat transcript (memory_search / memory_save).
// Shown as a subtle italic line — not a full bubble. Deduplicates consecutive
// identical notes so rapid tool runs don't spam the transcript.
function _appendMemoryNote(message) {
  if (!app.chatMessages) return;
  const last = app.chatMessages.lastElementChild;
  if (last && last.classList.contains('memory-debug-note') && last.textContent === '— ' + message) return;
  const el = document.createElement('div');
  el.className = 'memory-debug-note';
  el.textContent = '— ' + message;
  app.chatMessages.appendChild(el);
}
app._appendMemoryNote = _appendMemoryNote;
export { _appendMemoryNote };

// Current inference-turn number within this exchange. A single user message can
// drive several LLM "turns" (call → tools → call → …); the backend stamps each
// with a `turn_start` pipeline event carrying `turn`. 0 = not yet in a turn.
// Reset only at a true new exchange (user_message) / session switch — NOT in
// _resetForNewTurn, so a mid-turn reattach keeps the number it already learned.
let currentTurn = 0;

// 3 min of total silence on the current session ⇒ assume the run died and drop
// the glow. Real turns emit pipeline/stream events far more often.
const WATCHDOG_MS = 180000;

let _outsideHandler = null;
let _keyHandler = null;

// Clear a pending setTimeout (if any) and return null, so callers can write
// `x = _killTimer(x)` to both cancel and null out a timer handle in one step.
function _killTimer(id) { if (id) clearTimeout(id); return null; }
function _clearTextTimer() { clearTimer = _killTimer(clearTimer); }
function _clearEndTimer()  { endTimer = _killTimer(endTimer); }
function _armWatchdog() {
  watchdog = _killTimer(watchdog);
  watchdog = setTimeout(() => { watchdog = null; stop(); }, WATCHDOG_MS);
}
function _disarmWatchdog() { watchdog = _killTimer(watchdog); }

// Re-light triggered by a POST-turn background step (see `_turnEnded`). Show the
// note, but auto-settle on a short backstop instead of the long in-turn watchdog,
// so the chip can't hang if the step's completion event never arrives. Shares the
// endTimer slot with _endSoon(), so the real `*_end` event (which calls _endSoon)
// simply re-arms it shorter and wins.
function _armPostTurnSettle() {
  endTimer = _killTimer(endTimer);
  endTimer = setTimeout(() => { endTimer = null; stop(); }, POST_TURN_SETTLE_MS);
}

// True for a pipeline step that signals a background op FINISHED (memory_save_end,
// and any future *_end/*_complete/*_done/*_saved step). Used only while _turnEnded
// is set, so in-turn `*_end` steps (e.g. llm_call_end, fired every LLM call) never
// trip it and prematurely clear a live turn.
function _isCompletionStep(step) {
  return typeof step === 'string'
    && /_(end|complete|completed|done|finish|finished|saved)$/.test(step);
}

// Pop the new note in with a quick slide/fade so each interaction "appears".
function _animateText() {
  if (!textEl || typeof textEl.animate !== 'function') return;
  try {
    textEl.animate(
      [
        { opacity: 0, transform: 'translateY(3px)' },
        { opacity: 1, transform: 'translateY(0)' },
      ],
      { duration: 180, easing: 'ease-out' },
    );
  } catch (_) { /* Web Animations API unavailable — non-fatal */ }
}

/** Start a gentle ramp that makes the output counter tick up while the agent
 *  is thinking (no stream content yet). Increments by ~2-4 tokens every ~800ms
 *  so the user sees movement right away.
 */
function _startThinkingRamp() {
  _stopThinkingRamp();
  // If we already have real stream data or real tokens, don't fake it
  if (_streamCharCount > 0 || cumulativeOut > 0) return;
  _setSpinnerDir('out');
  _thinkingRamp = setInterval(() => {
    // Ramp grows slightly faster over time
    const bump = Math.min(8, 2 + Math.floor(_pendingOutEstimate / 50));
    _pendingOutEstimate += bump;
    _updateOutDisplay();
    // Also ramp the ctx counter so it counts up during thinking too
    const ctxBump = Math.min(15, 4 + Math.floor(_pendingCtxEstimate / 60));
    _pendingCtxEstimate += ctxBump;
    _renderCtxIndicator();
  }, 800);
}

function _stopThinkingRamp() {
  if (_thinkingRamp) {
    clearInterval(_thinkingRamp);
    _thinkingRamp = null;
  }
  _pendingCtxEstimate = 0;
  _renderCtxIndicator();
}

function _displayedOut() {
  // What the user sees for output tokens = cumulative real + streaming estimate
  return cumulativeOut + _pendingOutEstimate;
}

function _animateCountUp(el, target, durationMs = 300) {
  if (!el) return;
  const from = parseInt(el.textContent.replace(/,/g, ''), 10) || 0;
  if (from === target) return;
  const diff = target - from;
  const start = performance.now();

  function step(now) {
    const elapsed = now - start;
    const progress = Math.min(elapsed / durationMs, 1);
    // ease-out quadratic
    const eased = 1 - (1 - progress) * (1 - progress);
    const current = Math.round(from + diff * eased);
    el.textContent = current;
    if (progress < 1) {
      requestAnimationFrame(step);
    } else {
      el.textContent = target; // ensure exact final value
      _scheduleFooterFit();    // its width may have grown — re-fit the stats pill
    }
  }

  requestAnimationFrame(step);
}

function _setSpinnerDir(dir) {
  if (!tokenSpinnerEl) return;
  tokenSpinnerEl.classList.remove('spinning-out', 'spinning-in');
  if (dir === 'out') tokenSpinnerEl.classList.add('spinning-out');
  else if (dir === 'in') tokenSpinnerEl.classList.add('spinning-in');
}

function _updateTokenBar() {
  if (tokenBarEl) tokenBarEl.classList.toggle('active', active || cumulativeIn > 0 || cumulativeOut > 0);
}

function _updateOutDisplay() {
  _animateCountUp(tokensOutEl, _displayedOut());
}

/** Called on each stream chunk to update the live token estimate */
function _onStreamContent(content) {
  if (!content) return;
  // First stream content — stop the thinking ramp, switch to real estimates
  if (_streamCharCount === 0) {
    _stopThinkingRamp();
    _setSpinnerDir('out');
  }
  _streamCharCount += content.length;
  const newEstimate = Math.round(_streamCharCount / 4);
  if (newEstimate !== _pendingOutEstimate) {
    _pendingOutEstimate = newEstimate;
    _updateOutDisplay();
  }
}

function addTokens(inputTokens, outputTokens, callCostUsd) {
  // Real tokens arrived — stop the thinking ramp
  _stopThinkingRamp();
  // Add this single call's cost (priced at its own model's rate) to the running
  // session total. Never recompute from cumulative tokens × the current rate —
  // that would re-price earlier calls when the model is switched mid-session.
  if (typeof callCostUsd === 'number' && callCostUsd > 0) {
    _sessionCostUsd += callCostUsd;
  }
  if (typeof inputTokens === 'number' && inputTokens > 0) {
    cumulativeIn += inputTokens;
    _setSpinnerDir('in');
    _animateCountUp(tokensInEl, cumulativeIn);
  }
  // Real output tokens arrived — always clear the streaming estimate
  if (typeof outputTokens === 'number') {
    _pendingOutEstimate = 0;
    _streamCharCount = 0;
    if (outputTokens > 0) {
      cumulativeOut += outputTokens;
      _setSpinnerDir('out');
      _animateCountUp(tokensOutEl, cumulativeOut);
    }
  }
  _updateTokenBar();
  _renderCost();
  _saveTokenCache();
}

// ── Active-model context indicator (next to in/out counters) ────────────────

function _fmtCtxNum(n) {
  if (!n || typeof n !== 'number') return '';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}K`;
  return `${n}`;
}

/** Best-effort context window for a Claude Code model id, by family. The local
 *  `claude` CLI isn't in the app's model catalog, so we map instead of looking up:
 *  current Opus/Sonnet run a 1M window, Haiku 200K. Mirrors the backend's
 *  _claude_model_window so a live-reported model resolves the same as the load-time
 *  resolve. Returns null for an unknown id (footer just omits the max). */
function _claudeCtxWindow(model) {
  const m = (model || '').toLowerCase();
  if (!m) return null;
  if (m.includes('haiku')) return 200_000;
  if (m.includes('opus') || m.includes('sonnet') || m.includes('fable') || m.includes('mythos')) return 1_000_000;
  return null;
}

/** Fetch the active model's context window / max output and show it in the
 *  footer. Resolves the user's effective model server-side (one call). Silent
 *  on failure — the indicator just stays hidden. */
async function refreshModelContext() {
  if (!modelCtxEl) return;
  try {
    const headers = { ...authHeaders() };
    const aid = app.currentAgentId || '';
    const sid = app.currentSessionId || '';
    const qs = new URLSearchParams();
    if (aid) qs.set('agent_id', aid);
    if (sid) qs.set('session_id', sid);
    const url = apiPath('/admin/settings/current-model-info')
      + (qs.toString() ? `?${qs.toString()}` : '');
    const res = await fetch(url, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    _modelContextLimit = d.context || null;
    _currentModelName = d.model || '';
    _costInput = (typeof d.cost_input === 'number') ? d.cost_input : null;
    _costOutput = (typeof d.cost_output === 'number') ? d.cost_output : null;
    // Non-default engines (Local Claude Code, …) don't run on this model — drop
    // the price chip; the token/ctx counters stay (they reflect the real run).
    _altEngine = !!(d.engine && d.engine !== 'default');
    _altEngineId = _altEngine ? String(d.engine) : '';
    if (_altEngine) {
      // Adopt the configured harness model + effort so the changer/panel labels
      // match before the panel ever opens (and even when no panel was opened).
      _engineCurModel = d.model || '';
      const eff = (d.reasoning_effort && d.reasoning_effort !== 'default') ? String(d.reasoning_effort) : '';
      _engineCurEffort = eff;
    }
    _renderCtxIndicator();
    _renderCost();
    _renderModelBtn();
  } catch (e) {
    _altEngine = false;   // unknown engine ⇒ assume normal billing, don't strand the price hidden
    _altEngineId = '';
    modelCtxEl.style.display = 'none';
    modelCtxEl.innerHTML = '';
    _renderModelBtn();
    _scheduleFooterFit();
  }
}

/** Label the dedicated model button with the active slot's three-letter abbreviation
 *  (STD, PRM, VIS, IMG). The full model name is only visible inside the picker panel. */
function _renderModelBtn() {
  if (!modelBtnEl) return;
  // Alternate engine: show the harness's own model choice instead of slot labels.
  if (_altEngine) {
    const catalog = _engineCatalogNow();
    let label = 'Model';
    let title = 'Click to switch model for this chat';
    if (catalog) {
      const m = _engineCurModel || '';
      if (m) {
        const hit = catalog.models.find(x => x.v === m)
          || (catalog.lane === 'claude_code' ? catalog.models.find(x => x.v === _claudeAliasOf(m)) : null);
        label = hit ? hit.label : (m.length > 16 ? '…' + m.slice(-15) : m);
        title = `Model: ${m} — click to switch model for this chat`;
      } else {
        label = 'Default';
      }
    }
    modelBtnEl.textContent = label;
    modelBtnEl.title = title;
    return;
  }
  const slot = _allSlots.find(s => {
    const ref = s.type === 'role' ? `role:${s.role}` : `custom:${s.position}`;
    return ref === _currentSlotRef;
  });
  const name = (slot && slot.model) || '';
  let label = '';
  if (!slot || !name) {
    label = 'Model';
  } else if (slot.type === 'role') {
    const labels = {standard: 'Std', premium: 'Prem', image_in: 'Vis', image_out: 'Img'};
    label = labels[slot.role] || slot.role;
  } else {
    label = 'C' + slot.position;
  }
  modelBtnEl.textContent = label;
  modelBtnEl.title = name
    ? `Model: ${name} — click to switch model for this chat`
    : 'Click to switch model for this chat';
}

/** Format a USD amount for the footer: 2 decimals at/above $1, 4 below so
 *  sub-cent sessions still read as a non-zero number. */
function _fmtCost(usd) {
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  return `$${usd.toFixed(4)}`;
}

/** Render the session-cost chip from the running per-call total (_sessionCostUsd).
 *  Each call was priced at its own model's rate the moment it finished, so the
 *  total stays accurate across mid-session model switches — unlike the old
 *  "cumulative tokens × current model rate", which re-priced earlier calls.
 *  Always shown — displays "$0" before any cost accrues (e.g. all models so far
 *  were free or had no published price). Resets on session switch; reconciled
 *  from /session-cost on load. */
function _renderCost() {
  if (!costEl) return;
  // Alternate-engine agents (e.g. Local Claude Code) aren't billed on the app's
  // model, so a price here would be misleading — hide it, keep the counters.
  if (_altEngine) {
    costEl.style.display = 'none';
    costEl.innerHTML = '';
    _scheduleFooterFit();
    return;
  }
  const total = _sessionCostUsd;
  costEl.innerHTML = (total > 0) ? `${_fmtCost(total)}` : '$0';
  costEl.title = 'Session cost so far — each call billed at the model it ran on, '
    + 'summed across any model switches'
    + (_currentModelName ? ` (now: ${_currentModelName})` : '');
  costEl.style.display = '';
  _scheduleFooterFit();
}

/** Estimate context tokens from an array of message objects (chars/4 heuristic). */
function _estimateContextFromMessages(messages) {
  if (!messages || !messages.length) return 0;
  let totalChars = 0;
  for (const m of messages) {
    const c = m.content;
    if (typeof c === 'string') totalChars += c.length;
    else if (c) totalChars += String(c).length;
    // Also count tool_calls payload if present
    if (m.tool_calls) {
      try { totalChars += JSON.stringify(m.tool_calls).length; }
      catch (_) { totalChars += String(m.tool_calls).length; }
    }
  }
  return Math.max(0, Math.round(totalChars / 4));
}

/** Set the live context from an array of messages (called on session load). */
function setContextFromMessages(messages) {
  _contextTokens = _estimateContextFromMessages(messages);
  _pendingCtxEstimate = 0; // clear any stale ramp from a previous session
  _renderCtxIndicator();
  // Restore cached counters instantly; session-load then applies the one
  // authoritative ledger payload without issuing another aggregate request.
  _loadTokenCache();
}

function setSessionUsage(usage) {
  if (!usage || typeof usage !== 'object') return;
  cumulativeIn = Number(usage.input_tokens) || 0;
  cumulativeOut = Number(usage.output_tokens) || 0;
  _sessionCostUsd = Number(usage.total_cost_usd) || 0;
  _animateCountUp(tokensInEl, cumulativeIn);
  _animateCountUp(tokensOutEl, cumulativeOut);
  _updateTokenBar();
  _renderCost();
  _saveTokenCache();
}

/** Set the ctx indicator from a precomputed whole-session token estimate. Used
 *  by the loader after setContextFromMessages: the open fetch is now a small
 *  window, so counting only its messages would under-report — the server sends
 *  a whole-session total, which this applies on top. */
function setContextTokens(tokens) {
  if (typeof tokens !== 'number' || tokens < 0) return;
  _contextTokens = tokens;
  _pendingCtxEstimate = 0; // clear the thinking ramp when real data arrives
  _renderCtxIndicator();
}

/** What the user sees for ctx tokens = real context + thinking ramp estimate */
function _displayedCtx() {
  return (_contextTokens || 0) + _pendingCtxEstimate;
}

/** Render the ctx indicator: live context tokens / model's max context limit */
function _renderCtxIndicator() {
  if (!modelCtxEl) return;
  const ctx = _displayedCtx();
  const max = _modelContextLimit;
  if (!max && !_contextTokens && !_pendingCtxEstimate) { modelCtxEl.style.display = 'none'; modelCtxEl.innerHTML = ''; _scheduleFooterFit(); return; }
  modelCtxEl.innerHTML = `<span class="chat-token-label">ctx</span> ${_fmtCtxNum(ctx)} <span class="chat-ctx-sep">/</span> ${_fmtCtxNum(max)}`;
  const ctxSlot = _allSlots.find(s => {
    const ref = s.type === 'role' ? 'role:' + s.role : 'custom:' + s.position;
    return ref === _currentSlotRef;
  });
  modelCtxEl.title = `${(ctxSlot && ctxSlot.model) || '??'} — ctx ${(_contextTokens || 0).toLocaleString()} / max ${max ? max.toLocaleString() : '?'} — click to switch model`;
  modelCtxEl.style.display = '';
  // Keep the open Claude panel's context section in step with live updates.
  if (_claudePanelEl && _claudePanelEl.style.display !== 'none') _renderClaudeContext();
  _scheduleFooterFit();
}

// ── Footer stats fit (FOOTER-STATS-FIT) ─────────────────────────────────────
// Priority ladder for the left stats pill, LEAST → MOST useful. Each entry is a
// class on #chat-pill-stats that hides one piece (see app1.css). _fitFooterStats
// adds them one at a time, lowest first, only as far as needed to stop the pill's
// content from overflowing the space the right-side buttons leave it. To change
// what survives a squeeze, reorder this array — the CSS keys off these names.
const _FOOTER_SHED = [
  'fs-hide-tok-labels', // 1. "in" / "out" labels
  'fs-hide-ctx-label',  // 2. "ctx" label
  'fs-hide-cost',       // 3. session cost (least useful feature)
  'fs-hide-tokbar',     // 4. in/out token bar
  'fs-hide-ctx',        // 5. ctx readout (last to go; also hides the empty pill)
];

/** Shed footer stats in priority order until the left pill fits the space left
 *  by the right-side buttons. Measures REAL rendered widths (locale-formatted
 *  numbers, the active font) rather than guessing pixel breakpoints, so each
 *  element is always either fully shown or fully hidden — never half-clipped
 *  under the buttons (the bug the old @container thresholds caused). */
function _fitFooterStats() {
  if (!footerRowEl || !footerLeftEl) return;
  // Start from a clean slate so a widening panel restores what it can.
  for (const c of _FOOTER_SHED) footerRowEl.classList.remove(c);
  if (!footerRowEl.clientWidth) return;       // row hidden / not laid out yet
  // The left pill is a flex item with min-width:0, so it shrinks to the space
  // the (flex-shrink:0) right buttons leave it; its nowrap children then spill
  // past that box → scrollWidth exceeds clientWidth. Shed until it fits.
  let i = 0;
  while (footerLeftEl.scrollWidth > footerLeftEl.clientWidth + 1 && i < _FOOTER_SHED.length) {
    footerRowEl.classList.add(_FOOTER_SHED[i]);
    i++;
  }
}

/** rAF-debounced wrapper: content renders and resizes can fire many times per
 *  frame; collapse them into a single fit pass. */
function _scheduleFooterFit() {
  if (_footerFitPending) return;
  _footerFitPending = true;
  requestAnimationFrame(() => { _footerFitPending = false; _fitFooterStats(); });
}

// ── Context panel (click ctx indicator) ─────────────────────────────────────
// The stats strip's click target (#chat-pill-stats) opens this floating panel.
// Model selection and model info now live in the model-changer UI
// (#chat-model-changer → .chat-model-list-panel), so this panel only carries
// per-chat context-management (compaction) controls.

let _modelPickerEl = null;       // the floating dropdown container
let _compactEl = null;           // context-compaction section (sliders + "Compact now")
let _allSlots = [];              // fetched slot list: [{type, role?, position?, model, provider, context, description, ...}]
let _allEffortMap = {};          // per-slot effort: {"role:standard": "high", "custom:2": "low"}
let _currentSlotRef = '';        // the currently-selected slot ref (e.g. "role:text", "custom:2")
let _currentModelName = '';      // the resolved model name (fallback display — set by refreshModelContext and WS)

function _buildModelPicker() {
  if (_modelPickerEl) return;
  const picker = document.createElement('div');
  picker.className = 'chat-model-picker';
  // ── Context-compaction section ──
  // Two sliders tune THIS chat's compaction — "compact at % full" (when older turns
  // start folding into summaries) and "keep verbatim %" (how much recent history
  // stays word-for-word) — saved as a per-session override. "Compact now" folds the
  // conversation immediately by sending /compact through the normal pipeline.
  const compact = document.createElement('div');
  compact.className = 'cmp-compact';
  compact.innerHTML =
      `<div class="cmp-cc-head">Context compaction</div>`
    + `<div class="cmp-cc-row">`
    +   `<div class="cmp-cc-top"><span class="cmp-cc-label">Compact at</span>`
    +     `<span class="cmp-cc-val" data-val="threshold">85%</span></div>`
    +   `<input type="range" class="cmp-cc-slider" data-key="threshold" min="10" max="99" step="1" value="85">`
    +   `<div class="cmp-cc-hint">How full this chat gets before older turns are auto-summarized.</div>`
    + `</div>`
    + `<div class="cmp-cc-row">`
    +   `<div class="cmp-cc-top"><span class="cmp-cc-label">Keep verbatim</span>`
    +     `<span class="cmp-cc-val" data-val="tail">30%</span></div>`
    +   `<input type="range" class="cmp-cc-slider" data-key="tail" min="5" max="90" step="1" value="30">`
    +   `<div class="cmp-cc-hint">Share of the window always kept word-for-word (newest turns).</div>`
    + `</div>`
    + `<button type="button" class="cmp-cc-btn">`
    +   `<span class="cmp-cc-btn-label">Compact now</span>`
    +   `<span class="cmp-cc-btn-status"></span></button>`
    + `<div class="cmp-cc-note">Applies to this chat only — takes effect next turn.</div>`;
  picker.appendChild(compact);
  document.body.appendChild(picker);
  _modelPickerEl = picker;
  _compactEl = compact;

  // Slider "input" updates the live % label; "change" (on release) saves it.
  compact.querySelectorAll('.cmp-cc-slider').forEach(sl => {
    sl.addEventListener('input', () => _onCompactSlide(sl));
    sl.addEventListener('change', () => _onCompactCommit(sl));
    // Don't let a click inside the picker bubble to the document close handler.
    sl.addEventListener('click', (e) => e.stopPropagation());
  });
  const compactBtn = compact.querySelector('.cmp-cc-btn');
  if (compactBtn) compactBtn.addEventListener('click', (e) => { e.stopPropagation(); _runCompactNow(compactBtn); });

  // Close on click outside
  document.addEventListener('click', (e) => {
    if (_modelPickerEl && !_modelPickerEl.contains(e.target)
        && e.target !== modelBtnEl && !(modelBtnEl && modelBtnEl.contains(e.target))
        && !(footerLeftEl && footerLeftEl.contains(e.target))) {
      _modelPickerEl.style.display = 'none';
    }
  });

  // Close on Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _modelPickerEl && _modelPickerEl.style.display !== 'none') {
      _modelPickerEl.style.display = 'none';
    }
  });
}

function _escHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

/** Build the curated model list for the footer's model list panel (model
 *  changer): ALL of the agent's Text-capable models — the union of the app
 *  default(s) + the agent's OWN saved roster, resolved server-side by
 *  /admin/settings/agent-models exactly as the run resolves it. Falls back to
 *  the admin defaults (/multi-providers) when there's no agent/session. NOT the
 *  full provider catalog. */
async function _fetchModelsForPicker() {
  const headers = { ...authHeaders() };

  const aid = app.currentAgentId || '';
  const sid = app.currentSessionId || '';
  const modelMap = new Map();
  let activeSlotData = null;

  // Per-agent candidate list — now returns slot structure.
  if (aid || sid) {
    try {
      const qs = new URLSearchParams();
      if (aid) qs.set('agent_id', aid);
      if (sid) qs.set('session_id', sid);
      const res = await fetch(apiPath('/admin/settings/agent-models?' + qs.toString()), { headers });
      if (res.ok) {
        const data = await res.json();
        const slots = data.slots || [];
        _allSlots = slots.map(s => ({ ...s, context: null }));
        _allEffortMap = data.model_effort || {};
        activeSlotData = data.active_slot || null;
      }
    } catch (_) { /* best-effort */ }
  }

  // Fallback: app-default models when no per-agent list.
  if (!_allSlots.length) {
    try {
      const res = await fetch(apiPath('/admin/settings/multi-providers'), { headers });
      if (res.ok) {
        const data = await res.json();
        (data.providers || []).forEach(p => {
          if (p && p.model && p.enabled !== false && !modelMap.has(p.model)) {
            modelMap.set(p.model, { id: p.model, context: null });
          }
        });
        // Build a flat slot-less model list (legacy path).
        _allSlots = Array.from(modelMap.values()).map((m, i) => ({
          type: 'custom', position: i + 1, model: m.id, provider: '', context: null,
        }));
      }
    } catch (_) { /* best-effort */ }
  }

  // Set current slot ref from the active slot data, or default to first slot.
  if (activeSlotData) {
    _currentSlotRef = activeSlotData.type === 'role'
      ? 'role:' + (activeSlotData.role || 'standard')
      : 'custom:' + (activeSlotData.custom_position || 1);
  } else if (_allSlots.length) {
    const first = _allSlots[0];
    _currentSlotRef = first.type === 'role' ? 'role:' + first.role : 'custom:' + first.position;
  } else {
    _currentSlotRef = '';
  }
}

/** Reconcile the running session-cost chip AND the IN/OUT token counters against
 *  the authoritative backend (sum of every call's locked-in usage for this session).
 *  Called on session load so a reload / session switch shows the true accumulated
 *  cost and tokens, not zero. Best-effort: leaves live accumulators untouched on
 *  failure. */
async function _fetchSessionCost() {
  const sid = app.currentSessionId || '';
  if (!sid) { _sessionCostUsd = 0; _renderCost(); return; }
  const headers = { ...authHeaders() };
  try {
    const res = await fetch(apiPath(`/admin/settings/session-cost?session_id=${encodeURIComponent(sid)}`), { headers });
    if (res.ok) {
      const d = await res.json();
      if (d && typeof d.total_cost_usd === 'number') {
        _sessionCostUsd = d.total_cost_usd;
        _renderCost();
      }
      // Also seed the IN/OUT token counters from the per-model breakdown
      if (d && d.by_model) {
        let totalIn = 0;
        let totalOut = 0;
        for (const m of Object.values(d.by_model)) {
          totalIn += m.input || 0;
          totalOut += m.output || 0;
        }
        if (totalIn > 0 || totalOut > 0) {
          cumulativeIn = totalIn;
          cumulativeOut = totalOut;
          _pendingCtxEstimate = 0;
          if (tokensInEl) tokensInEl.textContent = totalIn.toLocaleString();
          if (tokensOutEl) tokensOutEl.textContent = totalOut.toLocaleString();
          _updateTokenBar();
          _saveTokenCache();
        }
      }
    }
  } catch (_) { /* best-effort */ }
}

/** Persist the chosen model. Returns true on a confirmed save, false otherwise.
 *  Scope, in order of preference:
 *    • In a chat session  → saved on the SESSION (sessions.metadata.llm_config),
 *      so this conversation remembers its own model without touching the agent
 *      default or other sessions. Resolved app-default → agent → session.
 *    • Agent but no session → overrides the agent's llm_config.model.
 *    • Neither → updates the global provider config's model. */
async function _selectModel(slotRef) {
  try {
    const headers = { 'Content-Type': 'application/json', ...authHeaders() };
    const sid = app.currentSessionId || '';
    const aid = app.currentAgentId || '';

    // Resolve slot ref to selection_type + role/position.
    const slot = _allSlots.find(s => {
      const ref = s.type === 'role' ? 'role:' + s.role : 'custom:' + s.position;
      return ref === slotRef;
    });
    if (!slot) return false;
    const selectionType = slot.type;
    const role = slot.type === 'role' ? slot.role : '';
    const customPos = slot.type === 'custom' ? slot.position : 0;

    const payload = {
      user_id: app.currentUserId || '',
      session_id: sid,
      selection_type: selectionType,
      role: role,
      custom_position: customPos,
    };

    if (sid) {
      const postRes = await fetch(apiPath('/api/v1/chat/session-model'), {
        method: 'POST', headers,
        body: JSON.stringify(payload),
      });
      if (postRes.ok) {
        // Session endpoint succeeded — model saved on the server session.
      } else {
        // Session endpoint failed (e.g. session doesn't exist on server yet —
        // common before the first message is sent). Store the pending selection
        // in sessionStorage so it can be applied once the session row exists.
        // Never fall back to writing agent metadata — model selections are
        // per-session and must not bleed into other chats.
        try {
          sessionStorage.setItem('ws:pendingModelSel', JSON.stringify(payload));
        } catch (_) { /* best-effort */ }
      }
    } else if (aid) {
      // No session — save as agent's llm_config override.
      const uq = '?user_id=' + encodeURIComponent(app.currentUserId || '');
      const getRes = await fetch(apiPath('/api/v1/agents/' + encodeURIComponent(aid) + uq), { headers });
      if (!getRes.ok) return false;
      const agent = await getRes.json();
      const existing = (agent.agent && agent.agent.llm_config) || {};
      const llmCfg = { ...existing, ...payload, use_default: false };
      const putRes = await fetch(apiPath('/api/v1/agents/' + encodeURIComponent(aid)), {
        method: 'PUT', headers,
        body: JSON.stringify({ user_id: app.currentUserId || '', llm_config: llmCfg }),
      });
      if (!putRes.ok) return false;
    } else {
      return false;
    }
    _currentSlotRef = slotRef;
    refreshModelContext();
    return true;
  } catch (e) {
    console.warn('Failed to switch model:', e);
    return false;
  }
}

// ── Context-compaction panel (bottom of the model picker) ────────────────────

// Map a slider's data-key to the label + save field it drives.
const _CC_FIELDS = {
  threshold: { valSel: 'threshold', field: 'compact_threshold_pct' },
  tail: { valSel: 'tail', field: 'tail_fraction_pct' },
};

function _ccValEl(key) {
  return _compactEl ? _compactEl.querySelector(`.cmp-cc-val[data-val="${key}"]`) : null;
}

// Live label update while dragging (no save yet).
function _onCompactSlide(slider) {
  const key = slider.dataset.key;
  const el = _ccValEl(key);
  if (el) el.textContent = slider.value + '%';
}

// Persist on release. Flash the label to confirm the save landed.
async function _onCompactCommit(slider) {
  const meta = _CC_FIELDS[slider.dataset.key];
  if (!meta) return;
  const el = _ccValEl(slider.dataset.key);
  const ok = await _saveCompaction(meta.field, parseInt(slider.value, 10));
  if (el) {
    el.classList.remove('cmp-cc-saved', 'cmp-cc-failed');
    // Force a reflow so re-adding the class re-triggers the flash animation.
    void el.offsetWidth;
    el.classList.add(ok ? 'cmp-cc-saved' : 'cmp-cc-failed');
  }
}

/** POST one compaction field (a whole percent) for this session. Returns true on a
 *  confirmed save; no-op (false) outside a chat session. */
async function _saveCompaction(field, pct) {
  const sid = app.currentSessionId || '';
  if (!sid) return false;
  try {
    const headers = { 'Content-Type': 'application/json', ...authHeaders() };
    const res = await fetch(apiPath('/api/v1/chat/session-compaction'), {
      method: 'POST', headers,
      body: JSON.stringify({
        user_id: app.currentUserId || '',
        session_id: sid,
        [field]: pct,
      }),
    });
    return res.ok;
  } catch (e) {
    console.warn('Failed to save compaction setting:', e);
    return false;
  }
}

/** Load the effective compaction settings for the current chat and reflect them on
 *  the two sliders + labels. Best-effort — leaves the defaults on any failure. */
async function _loadCompactionSettings() {
  if (!_compactEl) return;
  const sid = app.currentSessionId || '';
  if (!sid) return;
  try {
    const qs = new URLSearchParams({ session_id: sid });
    if (app.currentAgentId) qs.set('agent_id', app.currentAgentId);
    if (app.currentUserId) qs.set('user_id', app.currentUserId);
    const res = await fetch(apiPath('/api/v1/chat/session-compaction?' + qs.toString()),
      { headers: { ...authHeaders() } });
    if (!res.ok) return;
    const d = await res.json();
    const set = (key, pct) => {
      const sl = _compactEl.querySelector(`.cmp-cc-slider[data-key="${key}"]`);
      const el = _ccValEl(key);
      if (sl && typeof pct === 'number') sl.value = String(pct);
      if (el && typeof pct === 'number') el.textContent = pct + '%';
    };
    set('threshold', d.compact_threshold_pct);
    set('tail', d.tail_fraction_pct);
  } catch (_) { /* best-effort — sliders keep their defaults */ }
}

/** Fire "/compact" through the normal send pipeline (it locks the composer + shows
 *  the activity chip exactly like typing it) and close the picker. */
function _runCompactNow(btn) {
  const statusEl = btn ? btn.querySelector('.cmp-cc-btn-status') : null;
  const sid = app.currentSessionId || '';
  if (!sid || typeof app.sendChatMessage !== 'function' || !app.chatInput) {
    if (statusEl) statusEl.textContent = 'unavailable';
    return;
  }
  if (_modelPickerEl) _modelPickerEl.style.display = 'none';
  app.chatInput.value = '/compact';
  try { app.sendChatMessage(); }
  catch (e) { console.warn('Compact-now send failed:', e); }
}

// Toggle the context panel open/close when clicking the footer left area
function _toggleModelPicker() {
  if (_modelPickerEl && _modelPickerEl.style.display !== 'none') {
    _modelPickerEl.style.display = 'none';
    return;
  }
  // Close the model list panel if open
  if (_modelListPanelEl) _modelListPanelEl.style.display = 'none';
  _buildModelPicker();

  // Vertical anchor: pin the panel's bottom edge just above the footer row, so it
  // grows upward from there (prefer the dedicated model button, then the ctx chip).
  const vAnchor = modelBtnEl
    || (modelCtxEl && modelCtxEl.style.display !== 'none' ? modelCtxEl : null)
    || document.getElementById('chat-pill-stats')
    || document.getElementById('chat-input-row');
  // Horizontal anchor: align the panel's left edge to the chat pill's left edge.
  const pill = document.getElementById('chat-input-row');
  const hAnchor = pill || vAnchor;
  if (!vAnchor || !hAnchor) return;
  const vRect = vAnchor.getBoundingClientRect();
  const hRect = hAnchor.getBoundingClientRect();
  _modelPickerEl.style.top = 'auto';
  _modelPickerEl.style.bottom = (window.innerHeight - vRect.top + 6) + 'px';
  _modelPickerEl.style.left = Math.max(4, Math.min(hRect.left, window.innerWidth - 290)) + 'px';
  // Show it after a microtask so the document click handler (set in _buildModelPicker)
  // doesn't fire on this same click event and immediately close it.
  setTimeout(() => {
    _modelPickerEl.style.display = 'block';
  }, 0);

  // Pull this chat's effective compaction settings into the sliders.
  _loadCompactionSettings();
}

// ── Claude Code panel (replaces the model picker for claude_code agents) ──────
// The app's OpenRouter model list is meaningless for a Local Claude Code agent —
// it runs the machine's `claude`, not the app LLM. So for those agents the footer
// click opens THIS panel instead: real context used / window, the subscription's
// 5-hour + weekly rolling-limit usage (best-effort — hidden when the undocumented
// endpoint can't be reached), and one-tap model selection (Default/Opus/Sonnet/
// Haiku) saved onto the agent's claude_code config (the engine reads it next turn).

const _CLAUDE_USAGE_URL = '/api/v1/claude-code/usage';
let _claudePanelEl = null;
let _claudeModelSaving = false;
let _engineCurModel = '';   // engine model currently configured on the agent (for changer sync)
let _engineCurEffort = '';  // engine effort currently configured on the agent
let _engineCatalogCache = null;  // live catalog fetched from the backend (CLI), or null
let _engineCatalogCacheTs = 0;   // when the live cache was fetched
const _ENGINE_CATALOG_TTL_MS = 5 * 60 * 1000; // re-fetch after 5 min so a long-lived page doesn't pin stale models

// STATIC fallback catalog — used only when the engine's own live catalog can't
// be fetched (see _getEngineCatalog). For Codex the live source is `codex debug
// models` (per-model efforts + defaults); for Claude the CLI exposes no catalog
// command, so this curated list IS the source (blank value = the CLI's default).
const _ENGINE_CATALOG = {
  claude_code: {
    lane: 'claude_code',
    label: 'Claude',
    models: [
      { v: '', label: 'Default', sub: "Claude's own default" },
      { v: 'fable', label: 'Fable', sub: 'Latest flagship' },
      { v: 'opus', label: 'Opus', sub: 'Most capable' },
      { v: 'sonnet', label: 'Sonnet', sub: 'Balanced' },
      { v: 'haiku', label: 'Haiku', sub: 'Fastest' },
    ],
    // claude --effort <level> (from `claude --help`)
    efforts: ['', 'low', 'medium', 'high', 'xhigh', 'max'],
  },
  codex: {
    lane: 'codex_code',
    label: 'Codex',
    models: [
      { v: '', label: 'Default', sub: "Codex's own default" },
      { v: 'gpt-5.6-sol', label: 'GPT-5.6-Sol', sub: 'Latest frontier agentic coding model.' },
      { v: 'gpt-5.6-terra', label: 'GPT-5.6-Terra', sub: 'Balanced agentic coding model for everyday work.' },
      { v: 'gpt-5.6-luna', label: 'GPT-5.6-Luna', sub: 'Fast and affordable agentic coding model.' },
      { v: 'gpt-5.5', label: 'GPT-5.5', sub: 'Frontier model for complex coding, research, and real-world work.' },
      { v: 'gpt-5.4', label: 'GPT-5.4', sub: 'Strong model for everyday coding.' },
      { v: 'gpt-5.4-mini', label: 'GPT-5.4-Mini', sub: 'Small, fast, and cost-efficient model for simpler coding tasks.' },
    ],
    // codex model_reasoning_effort (config override) — conservative union that
    // every listed model supports; the LIVE catalog narrows this per model.
    efforts: ['', 'low', 'medium', 'high', 'xhigh'],
  },
};

const _ENGINE_EFFORT_LABELS = {
  minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High',
  xhigh: 'X-High', max: 'Max', ultra: 'Ultra',
};

/** The static (curated) catalog for the agent's engine, or null when not an
 *  alternate engine. */
function _engineCatalog() {
  return _ENGINE_CATALOG[_altEngineId] || null;
}

/** The catalog used for immediate, synchronous label rendering: the live cache
 *  when it has loaded, otherwise the static curated list. */
function _engineCatalogNow() {
  return _engineCatalogCache || _engineCatalog();
}

/** Fetch the LIVE engine catalog from the backend (which reads it from the local
 *  CLI, e.g. `codex debug models`). Falls back to the static curated catalog on
 *  any failure. Cached ~5 min in the page (the backend caches the CLI query too,
 *  ~15 min). ``force`` bypasses the page cache and asks the backend to re-query
 *  the CLI (what the Config tab's "Query CLI for latest model options" button
 *  does — the footer picks up the fresh list on its next open). */
async function _getEngineCatalog(force) {
  const staticCat = _engineCatalog();
  if (!_altEngineId || !staticCat) return staticCat;
  const fresh = _engineCatalogCache && (Date.now() - _engineCatalogCacheTs) < _ENGINE_CATALOG_TTL_MS;
  if (fresh && !force) return _engineCatalogCache;
  try {
    const url = apiPath('/api/v1/engines/model-catalog?engine=' + encodeURIComponent(_altEngineId))
      + (force ? '&force=1' : '');
    const res = await fetch(url, { headers: { ...authHeaders() } });
    if (res.ok) {
      const d = await res.json();
      const cat = (d && d.source === 'cli' && d.catalog
        && Array.isArray(d.catalog.models) && d.catalog.models.length) ? d.catalog : null;
      if (cat) {
        _engineCatalogCache = {
          lane: cat.lane || staticCat.lane,
          label: cat.label || staticCat.label,
          models: cat.models.map(m => ({
            v: m.v,
            label: m.label || m.v,
            sub: m.sub || '',
            efforts: (Array.isArray(m.efforts) && m.efforts.length) ? m.efforts : null,
            default_effort: m.default_effort || '',
          })),
          efforts: null, // per-model efforts always win when the CLI provides them
        };
        _engineCatalogCacheTs = Date.now();
        return _engineCatalogCache;
      }
    }
  } catch (_) { /* best-effort — fall back to curated */ }
  return _engineCatalogCache || staticCat;
}

/** Effort options for a given model in a catalog: the model's own supported
 *  levels when present, else the engine-level list. Always includes the
 *  "Default" (CLI default) option first. */
function _effortOptions(cat, model) {
  const out = [{ v: '', label: 'Default' }];
  if (!cat) return out;
  let levels = null;
  if (model) {
    const mm = cat.models.find(x => x.v === model);
    if (mm && Array.isArray(mm.efforts) && mm.efforts.length) levels = mm.efforts;
  }
  if (!levels && Array.isArray(cat.efforts)) {
    levels = cat.efforts.filter(v => v !== '').map(v => ({ v, label: _ENGINE_EFFORT_LABELS[v] || v }));
  }
  if (levels) {
    for (const e of levels) {
      if (e && e.v) out.push({ v: e.v, label: e.label || _ENGINE_EFFORT_LABELS[e.v] || e.v });
    }
  }
  return out;
}

/** Which preset row a configured model id maps to ('' = Default, null = a custom
 *  id that matches no preset so nothing is ticked). */
function _claudeAliasOf(model) {
  const m = (model || '').toLowerCase();
  if (!m) return '';
  if (m.includes('fable')) return 'fable';
  if (m.includes('opus')) return 'opus';
  if (m.includes('sonnet')) return 'sonnet';
  if (m.includes('haiku')) return 'haiku';
  return null;
}

/** Format a reset timestamp (ISO string or epoch s/ms) to a short local label;
 *  echoes the raw value back if it can't be parsed. */
function _fmtReset(v) {
  let d = null;
  if (typeof v === 'number') d = new Date(v > 1e12 ? v : v * 1000);
  else if (typeof v === 'string') { const t = Date.parse(v); if (!isNaN(t)) d = new Date(t); }
  if (!d || isNaN(d.getTime())) return typeof v === 'string' ? v : '';
  try { return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); }
  catch (_) { return d.toLocaleString(); }
}

function _buildClaudePanel() {
  if (_claudePanelEl) return;
  const p = document.createElement('div');
  // Reuse the model-picker container chrome (position/border/shadow) + own body.
  p.className = 'chat-model-picker chat-claude-panel';
  p.innerHTML = `
    <div class="ccp-sec ccp-context">
      <div class="ccp-head">Context</div>
      <div class="ccp-ctx-row"><span class="ccp-ctx-num">—</span><span class="ccp-ctx-max"></span></div>
      <div class="ccp-bar"><div class="ccp-bar-fill"></div></div>
    </div>
    <div class="ccp-sec ccp-usage" style="display:none">
      <div class="ccp-head">Plan usage</div>
      <div class="ccp-usage-body"></div>
    </div>
    <div class="ccp-sec ccp-models">
      <div class="ccp-head">Model</div>
      <div class="ccp-model-list"></div>
    </div>
    <div class="ccp-sec ccp-effort">
      <div class="ccp-head">Effort</div>
      <div class="ccp-effort-btns"></div>
    </div>`;
  document.body.appendChild(p);
  _claudePanelEl = p;

  document.addEventListener('click', (e) => {
    if (_claudePanelEl && _claudePanelEl.style.display !== 'none'
        && !_claudePanelEl.contains(e.target)
        && e.target !== modelBtnEl && !(modelBtnEl && modelBtnEl.contains(e.target))
        && !(footerLeftEl && footerLeftEl.contains(e.target))
        && e.target !== modelCtxEl && !(modelCtxEl && modelCtxEl.contains(e.target))) {
      _claudePanelEl.style.display = 'none';
    }
  });
}

/** Fill the context section from the live footer state (real tokens / window). */
function _renderClaudeContext() {
  if (!_claudePanelEl) return;
  const ctx = _displayedCtx();
  const max = _modelContextLimit;
  const numEl = _claudePanelEl.querySelector('.ccp-ctx-num');
  const maxEl = _claudePanelEl.querySelector('.ccp-ctx-max');
  const fill = _claudePanelEl.querySelector('.ccp-context .ccp-bar-fill');
  if (numEl) numEl.textContent = ctx ? _fmtCtxNum(ctx) : '—';
  if (maxEl) maxEl.textContent = max ? ' / ' + _fmtCtxNum(max) : '';
  if (fill) fill.style.width = ((ctx && max) ? Math.max(0, Math.min(100, ctx / max * 100)) : 0) + '%';
}

function _usageRowHtml(label, w) {
  if (!w) return '';
  const pct = (typeof w.percent === 'number') ? w.percent : null;
  const pctStr = (pct != null) ? `${Math.round(pct)}%` : '—';
  const fillW = (pct != null) ? Math.max(0, Math.min(100, pct)) : 0;
  const warn = (pct != null && pct >= 80) ? ' warn' : '';
  const reset = w.resets_at ? _fmtReset(w.resets_at) : '';
  return `<div class="ccp-u-row">
      <div class="ccp-u-top"><span class="ccp-u-label">${_escHtml(label)}</span><span class="ccp-u-pct">${pctStr}</span></div>
      <div class="ccp-bar"><div class="ccp-bar-fill${warn}" style="width:${fillW}%"></div></div>
      ${reset ? `<div class="ccp-u-reset">resets ${_escHtml(reset)}</div>` : ''}
    </div>`;
}

/** Best-effort plan usage. Hides the whole section if the endpoint is unavailable
 *  (undocumented — see plugins/engines/claude_code/claude_code_login.py get_plan_usage). */
async function _loadClaudeUsage() {
  const sec = _claudePanelEl && _claudePanelEl.querySelector('.ccp-usage');
  const body = _claudePanelEl && _claudePanelEl.querySelector('.ccp-usage-body');
  if (!sec || !body) return;
  try {
    const res = await fetch(_CLAUDE_USAGE_URL, { headers: { ...authHeaders() } });
    const d = res.ok ? await res.json() : null;
    const rows = d && d.available
      ? _usageRowHtml('5-hour', d.five_hour) + _usageRowHtml('Weekly', d.weekly) : '';
    if (rows) { body.innerHTML = rows; sec.style.display = ''; }
    else sec.style.display = 'none';
  } catch (_) {
    sec.style.display = 'none';
  }
}

/** Load the engine's model rows + effort buttons, ticking whatever the agent is
 *  configured to use (from its claude_code/codex_code metadata lane). Uses the
 *  LIVE catalog from the CLI when available (per-model effort levels), else the
 *  curated static list. */
async function _loadEngineModels() {
  const list = _claudePanelEl && _claudePanelEl.querySelector('.ccp-model-list');
  const effBox = _claudePanelEl && _claudePanelEl.querySelector('.ccp-effort-btns');
  if (!list) return;
  const catalog = (await _getEngineCatalog()) || _engineCatalog();
  if (!catalog) return;
  const aid = app.currentAgentId || '';
  let current = '';
  let effort = '';
  try {
    const uq = `?user_id=${encodeURIComponent(app.currentUserId || '')}`;
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(aid)}${uq}`, { headers: { ...authHeaders() } });
    if (res.ok) {
      const a = await res.json();
      const lane = (a.agent || a)[catalog.lane] || {};
      current = String(lane.model || '').trim();
      effort = String(lane.effort || '').trim();
    }
  } catch (_) { /* best-effort */ }
  _engineCurModel = current;
  _engineCurEffort = effort;
  // Model rows: Claude ticks via alias (a stored full id like claude-opus-4-5
  // still matches the Opus preset); Codex ticks on exact id.
  const isClaude = catalog.lane === 'claude_code';
  const activeAlias = isClaude ? _claudeAliasOf(current) : current;
  list.innerHTML = '';
  catalog.models.forEach(m => {
    const row = document.createElement('div');
    row.className = 'cmp-item ccp-model-row' + (m.v === activeAlias ? ' cmp-selected' : '');
    row.innerHTML = `<span class="ccp-m-main"><span class="ccp-m-label">${_escHtml(m.label)}</span>`
      + `<span class="ccp-m-sub">${_escHtml(m.sub)}</span></span><span class="cmp-item-status"></span>`;
    row.addEventListener('click', () => _onPickEngineModel(row, m.v));
    list.appendChild(row);
  });
  // Effort buttons — per-model levels when the live catalog provides them.
  _renderEngineEffortButtons(catalog, current, effort);
}

/** Render the effort row for the currently-selected model. */
function _renderEngineEffortButtons(catalog, model, effort) {
  const effBox = _claudePanelEl && _claudePanelEl.querySelector('.ccp-effort-btns');
  if (!effBox || !catalog) return;
  const isClaude = catalog.lane === 'claude_code';
  const options = _effortOptions(catalog, model);
  effBox.innerHTML = '';
  options.forEach(opt => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'cmp-eff-btn' + (opt.v === effort ? ' cmp-eff-on' : '');
    b.textContent = opt.label;
    b.title = opt.v
      ? `Reasoning effort ${opt.v}${isClaude ? ' (claude --effort)' : ' (codex model_reasoning_effort)'}`
      : "Use the CLI's own default effort";
    b.addEventListener('click', () => _onPickEngineEffort(b, opt.v));
    effBox.appendChild(b);
  });
}

/** Save one field (model / effort) onto the engine's metadata lane. */
async function _saveEngineField(field, value) {
  const aid = app.currentAgentId || '';
  const catalog = _engineCatalog();
  if (!aid || !catalog) return false;
  try {
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(aid)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: app.currentUserId || '', [catalog.lane]: { [field]: value } }),
    });
    return res.ok;
  } catch (_) { return false; }
}

async function _onPickEngineModel(row, value) {
  if (_claudeModelSaving) return;
  _claudeModelSaving = true;
  const status = row.querySelector('.cmp-item-status');
  if (status) status.innerHTML = '<span class="cmp-spinner"></span>';
  const [ok] = await Promise.all([_saveEngineField('model', value), new Promise(r => setTimeout(r, 280))]);
  if (status) status.innerHTML = ok
    ? '<span class="cmp-check" title="Saved">✓</span>'
    : '<span class="cmp-cross" title="Save failed">✕</span>';
  if (ok) {
    _engineCurModel = value;
    _claudePanelEl.querySelectorAll('.ccp-model-row.cmp-selected').forEach(el => el.classList.remove('cmp-selected'));
    row.classList.add('cmp-selected');
    refreshModelContext();   // footer name + window follow the new model
    // Per-model effort levels may differ — re-render the effort row for it.
    const cat = _engineCatalogNow();
    _renderEngineEffortButtons(cat, value, _engineCurEffort);
    setTimeout(() => { if (status) status.innerHTML = ''; }, 900);
  }
  _claudeModelSaving = false;
}

async function _onPickEngineEffort(btn, level) {
  if (_claudeModelSaving) return;
  _claudeModelSaving = true;
  btn.classList.add('cmp-eff-saving');
  const [ok] = await Promise.all([_saveEngineField('effort', level), new Promise(r => setTimeout(r, 280))]);
  btn.classList.remove('cmp-eff-saving');
  if (ok) {
    _engineCurEffort = level;
    _claudePanelEl.querySelectorAll('.cmp-eff-btn.cmp-eff-on').forEach(el => el.classList.remove('cmp-eff-on'));
    btn.classList.add('cmp-eff-on');
  }
  _claudeModelSaving = false;
}

function _toggleEnginePanel() {
  if (_claudePanelEl && _claudePanelEl.style.display !== 'none') {
    _claudePanelEl.style.display = 'none';
    return;
  }
  _buildClaudePanel();
  // Anchor identically to the model picker: bottom-pinned above the footer,
  // left-aligned to the chat pill.
  const vAnchor = modelBtnEl
    || (modelCtxEl && modelCtxEl.style.display !== 'none' ? modelCtxEl : null)
    || document.getElementById('chat-pill-stats')
    || document.getElementById('chat-input-row');
  const pill = document.getElementById('chat-input-row');
  const hAnchor = pill || vAnchor;
  if (!vAnchor || !hAnchor) return;
  const vRect = vAnchor.getBoundingClientRect();
  const hRect = hAnchor.getBoundingClientRect();
  _claudePanelEl.style.top = 'auto';
  _claudePanelEl.style.bottom = (window.innerHeight - vRect.top + 6) + 'px';
  _claudePanelEl.style.left = Math.max(4, Math.min(hRect.left, window.innerWidth - 290)) + 'px';
  setTimeout(() => { _claudePanelEl.style.display = 'block'; }, 0);
  _renderClaudeContext();
  _loadEngineModels();
  // Plan-usage section is Claude-specific; skip for other engines.
  const catalog = _engineCatalog();
  if (catalog && catalog.lane === 'claude_code') _loadClaudeUsage();
}

function resetTokens() {
  cumulativeIn = 0;
  cumulativeOut = 0;
  _sessionCostUsd = 0;
  _pendingOutEstimate = 0;
  _streamCharCount = 0;
  _pendingCtxEstimate = 0;
  _setSpinnerDir(null);
  if (tokensInEl) tokensInEl.textContent = '0';
  if (tokensOutEl) tokensOutEl.textContent = '0';
  _updateTokenBar();
  _renderCost();
  try { localStorage.removeItem(_TOKEN_CACHE_KEY); } catch (_) {}
}

// ── Live per-stage elapsed timer ────────────────────────────────────────────
// Each note (Building tools / Loading history / Searching memory / Preparing /
// Thinking… / Writing reply…) shows the seconds spent ON THAT STAGE, ticking
// live, so the user can SEE which step is slow — not just that "memory" ran.
// `currentNote` stays the bare label (so setNote's dedupe still works); the
// element shows label + suffix, refreshed on a light interval.
let _stageTimer = null;   // setInterval handle ticking the elapsed suffix
let _stageStart = 0;      // performance.now() when the current stage began

// Terminal/one-shot notes that shouldn't carry a running clock.
const _NO_CLOCK_NOTES = new Set(['Error', 'Stopped', 'Blocked for safety']);

function _clearStageTimer() {
  if (_stageTimer) { clearInterval(_stageTimer); _stageTimer = null; }
  _stageStart = 0;
}

function _renderNote() {
  if (!textEl) return;
  let txt = currentNote || '';
  if (_stageStart && txt) {
    const secs = (performance.now() - _stageStart) / 1000;
    if (secs >= 0.3) txt += '  ·  ' + secs.toFixed(1) + 's';
  }
  textEl.textContent = txt;
}

function setNote(text) {
  // Dedupe identical notes — this also tames the per-chunk spam from `stream`
  // events, which would otherwise re-fire on every token.
  if (!text || text === currentNote) return;
  currentNote = text;
  // Begin a fresh stage clock (unless this is a terminal note), then refresh on
  // a light interval so the elapsed seconds tick up live next to the label.
  _clearStageTimer();
  if (active && !_NO_CLOCK_NOTES.has(text)) {
    _stageStart = performance.now();
    _stageTimer = setInterval(_renderNote, 120);
  }
  _renderNote();
  _animateText();
}

// "Turn N: " prefix for the in-loop notes (tool calls), blank before turn 1.
function _turnPrefix() {
  return currentTurn > 0 ? 'Turn ' + currentTurn + ': ' : '';
}

// Show/hide the chevron + clickability based on whether there's anything to open.
function _updateBarAffordance() {
  if (!barEl) return;
  const has = toolCalls.length > 0;
  barEl.classList.toggle('has-tools', has);
  if (!has && expanded) closePanel();
}

// Wipe this turn's tool list + panel (called at every new-turn boundary).
function _resetForNewTurn() {
  toolCalls = [];
  currentToolAnchor = null;
  app._turnHasBubble = false;
  closePanel();
  renderPanel();
  _updateBarAffordance();
}

// Switch the indicator into its live "working" look.
function _activate() {
  active = true;
  // Expose turn-active state globally so background pollers (session list /
  // related-sessions refresh in session-init.js) can PAUSE while a turn is
  // streaming — their heavy, un-offloaded DB endpoints otherwise jam the
  // server's event loop and starve the live reply. Cleared in stop().
  try { window.__agentTurnActive = true; } catch (_) {}
  resting = false;
  // Cancel any pending text-blank scheduled by a just-finished turn's stop(). A
  // re-activation within that 260ms window would otherwise have its fresh note
  // wiped, leaving the bar visible with no text — i.e. a lone idle dot.
  _clearTextTimer();
  if (rootEl) { rootEl.classList.remove('resting'); rootEl.classList.add('visible'); }
  if (pillEl) pillEl.classList.add('thinking');
  _startThinkingRamp();
  _updateTokenBar();
}

function start(initialNote) {
  _clearTextTimer();
  _clearEndTimer();
  _turnEnded = false;   // user just sent — a fresh turn is beginning
  if (!active) { _resetForNewTurn(); _activate(); }
  _armWatchdog();
  setNote(initialNote || 'Thinking…');
}

function stop() {
  _disarmWatchdog();
  _clearEndTimer();
  _stopThinkingRamp();
  _clearStageTimer();
  if (!active && !resting) return;
  active = false;
  try { window.__agentTurnActive = false; } catch (_) {}
  if (pillEl) pillEl.classList.remove('thinking');
  _setSpinnerDir(null);

  // A terminal response proves the agent has progressed past every tool call in
  // this inference turn.  A dropped WebSocket `tool_result` must therefore not
  // leave a permanent "running…" card in the transcript.  Keep the call for
  // auditability, but settle its presentation; a reload can still hydrate the
  // full persisted result from the session record.
  for (const call of toolCalls) {
    if (call.status !== 'running') continue;
    call.status = 'done';
    call.result = 'Completed — this browser did not receive the live tool result.';
    call.durationMs = null;
  }

  // Attach any remaining tool calls from the last turn (if not already
  // attached by a turn_start boundary). Each turn's calls are attached
  // incrementally as turns progress, so this only catches the final turn.
  if (toolCalls.length > 0 && app.attachToolCallsToLastBubble) {
    const calls = toolCalls.slice();
    app.attachToolCallsToLastBubble(calls, undefined, currentToolAnchor);
  }

  // Always fade the activity bar out — tool calls now live on the bubble.
  resting = false;
  currentNote = '';
  if (rootEl) rootEl.classList.remove('visible', 'resting');
  closePanel();
  _clearTextTimer();
  clearTimer = setTimeout(() => { if (textEl) textEl.textContent = ''; }, 260);
  _updateBarAffordance();
  _updateTokenBar();
}

// Show a final note (Error / Stopped) briefly, then settle.
function _endSoon() {
  _disarmWatchdog();
  _clearEndTimer();
  endTimer = setTimeout(() => { endTimer = null; stop(); }, 900);
}

// Called when the viewed session changes — a glow/list left over from the
// previous session must not linger on the shared pill.
export function chatActivitySessionChanged() {
  _disarmWatchdog();
  _stopThinkingRamp();
  _clearTextTimer();
  _clearEndTimer();
  active = false;
  try { window.__agentTurnActive = false; } catch (_) {}
  resting = false;
  _turnEnded = false;
  toolCalls = [];
  currentToolAnchor = null;
  currentTurn = 0;
  currentNote = '';
  app._activeToolGroupBubble = null;
  app._turnHasBubble = false;
  closePanel();
  renderPanel();
  _updateBarAffordance();
  if (pillEl) pillEl.classList.remove('thinking');
  if (rootEl) rootEl.classList.remove('visible', 'resting');
  if (textEl) textEl.textContent = '';
  resetTokens();
  // Re-fetch model slot data for the current session so the model selector
  // (#chat-model-btn) reflects the new session's active model, not the
  // previous session's. _fetchModelsForPicker populates _allSlots and
  // _currentSlotRef, and the wrapped version auto-calls _syncModelChanger.
  // We also call _renderModelBtn() to update the dedicated button text.
  _fetchModelsForPicker().then(() => _renderModelBtn());
}

// Re-light the live indicator after a page refresh, from the durable run-state
// snapshot the server persists for the in-flight operation (see run_state_set_op
// / session_runs.current_op). `op` is the JSON string (or parsed object) from
// run.current_op; null/absent means "active but between tools" → generic note.
// Live WS events then take over and update it as the turn continues.
function chatActivityRestore(op) {
  let data = op;
  if (typeof op === 'string') {
    try { data = JSON.parse(op); } catch (_) { data = null; }
  }
  _turnEnded = false;   // restoring an in-flight turn — not post-turn
  _activate();
  if (data && typeof data.turn === 'number' && data.turn > 0) currentTurn = data.turn;
  if (data && data.tool) addToolCall(data.tool, data.args || {});
  setNote((data && data.note) || 'Working…');
  _armWatchdog();
}

// ── Tool-call accumulation ──────────────────────────────────────────────────

function addToolCall(tool, args, toolCallId) {
  toolCalls.push({
    tool: tool || 'tool',
    toolCallId: toolCallId || null,
    args: args || {},
    status: 'running',
    result: null,
    durationMs: null,
    errorType: null,
    turn: currentTurn,
    open: false,
  });
  _updateBarAffordance();
  if (expanded) renderPanel();
}

function resolveToolResult(tool, result, durationMs, isError, errorType, toolCallId) {
  // Pair with the oldest still-running call of the same name (no call-id in the
  // event stream, so order is the best we have).
  let entry = null;
  if (toolCallId) {
    entry = toolCalls.find(call =>
      call.toolCallId === toolCallId && call.status === 'running',
    ) || null;
  }
  for (let i = 0; !entry && i < toolCalls.length; i++) {
    if (toolCalls[i].tool === tool && toolCalls[i].status === 'running') { entry = toolCalls[i]; break; }
  }
  if (!entry) {
    // A result with no matching open call (e.g. mid-turn reattach) — record it.
    entry = { tool: tool || 'tool', args: {}, status: 'running', result: null, durationMs: null, errorType: null, turn: currentTurn, open: false };
    toolCalls.push(entry);
  }
  entry.status = isError ? 'error' : 'done';
  entry.result = result == null ? '' : String(result);
  entry.durationMs = (typeof durationMs === 'number') ? durationMs : null;
  entry.errorType = errorType || null;
  _updateBarAffordance();
  if (expanded) renderPanel();
}

// Render a completed synthetic tool call (vision ingestion / loop-node memory)
// straight onto the transcript as its own foldable tool-only bubble, in the spot
// it happened — memory_search/vision after the user message before the reply,
// memory_save after the reply. Bypasses the activity-bar turn accounting entirely
// so it can't be wiped by an ingestion-phase or post-turn reset. No-ops safely if
// the chat bubble layer isn't present (e.g. a non-chat host of this module).
// `info`: { args, result, error, durationMs, errorType, id }. Missing args fall
// back to whatever was stashed in _pendingSynthArgs for this tool.
function _renderSynthToolBubble(toolName, info) {
  if (!app.chatMessages || typeof app.addChatBubble !== 'function'
      || typeof app.attachToolCallsToLastBubble !== 'function') return;
  info = info || {};
  const call = {
    tool: toolName,
    args: info.args || _pendingSynthArgs[toolName] || {},
    status: info.error ? 'error' : 'done',
    result: info.result == null ? '' : String(info.result),
    durationMs: (typeof info.durationMs === 'number') ? info.durationMs : null,
    errorType: info.errorType || null,
    turn: 0,
    open: false,
  };
  delete _pendingSynthArgs[toolName];
  try {
    // Dedicated tool-only bubble with an explicit target, so it doesn't merge
    // into (or get attributed to) any prior turn's tool group.
    const bubble = app.addChatBubble('agent', '', 'tool-only', undefined,
      'synth-live-' + toolName + '-' + (info.id || ''));
    app.attachToolCallsToLastBubble([call], bubble);
    // This out-of-band bubble must not absorb the agent's later tool-only turns.
    app._activeToolGroupBubble = null;
  } catch (_) { /* best-effort live render; reload shows it from the persisted row */ }
}

// ── Panel rendering ─────────────────────────────────────────────────────────

function _fmtArgs(args) {
  if (args == null) return '(none)';
  try {
    if (typeof args === 'object' && Object.keys(args).length === 0) return '(none)';
    return JSON.stringify(args, null, 2);
  } catch (_) {
    return String(args);
  }
}

function _makeCopyBtn(text, label) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ca-tool-copy-btn';
  btn.title = 'Copy ' + label;
  btn.textContent = 'copy';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    copyText(text).then(() => {
      btn.textContent = 'copied';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = 'copy'; btn.classList.remove('copied'); }, 1500);
    }).catch(() => {});
  });
  return btn;
}

// Delete button for a tool-call row. Works in two contexts:
//   • Bubble accordion — resolves the DB interaction_id from the parent
//     `.chat-bubble`'s `data-msg-id` (available live OR after reload).
//   • Activity-bar panel — requires `_detailMsgId` (only present after reload).
// Clicking removes the tool call from the DB plus its DOM row.
function _makeToolDeleteBtn(entry, row) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ca-tool-delete-btn';
  btn.title = 'Delete this tool call from context';
  btn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';

  // Auto-reset timer so the armed state expires after 4s.
  let _armedTimer = null;

  function _resetToolDeleteBtn() {
    if (_armedTimer) { clearTimeout(_armedTimer); _armedTimer = null; }
    btn.dataset.state = '';
    btn.title = 'Delete this tool call from context';
    btn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
    btn.classList.remove('warning');
  }

  function _resetAllToolDeleteBtns() {
    document.querySelectorAll('.ca-tool-delete-btn[data-state="warning"]').forEach(b => {
      b.dataset.state = '';
      b.title = 'Delete this tool call from context';
      b.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
      b.classList.remove('warning');
    });
  }

  btn.addEventListener('click', async (e) => {
    e.stopPropagation();

    // Two-click confirm with auto-timeout
    if (btn.dataset.state !== 'warning') {
      _resetAllToolDeleteBtns();
      btn.dataset.state = 'warning';
      btn.title = 'Click again to remove this tool call';
      btn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      btn.classList.add('warning');
      if (_armedTimer) clearTimeout(_armedTimer);
      _armedTimer = setTimeout(_resetToolDeleteBtn, 4000);
      return;
    }

    // ── Second click: confirm the delete ──
    if (_armedTimer) { clearTimeout(_armedTimer); _armedTimer = null; }

    // Mark the row as deleted (strikethrough)
    row.classList.add('deleted');
    btn.style.display = 'none';

    const sid = app.currentSessionId;
    if (!sid) return;

    const bubble = row.closest('.chat-bubble');
    const interactionId = entry._detailMsgId
      || (bubble && (bubble.dataset.msgId || bubble.dataset.turnId))
      || '';
    const idx = (entry._detailIdx != null) ? entry._detailIdx : parseInt(row.dataset.i, 10);
    if (!interactionId || idx == null || isNaN(idx)) return;

    const uid = app.currentUserId || '';
    try {
      const url = apiPath('/api/v1/db/tool-call?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(interactionId)
        + '&tool_call_idx=' + encodeURIComponent(idx)
        + (uid ? '&user_id=' + encodeURIComponent(uid) : ''));
      const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        console.warn('Delete tool call failed:', resp.status, errData);
        alert('Could not delete this tool call (server responded ' + resp.status + ').');
        row.classList.remove('deleted');
        btn.style.display = '';
        _resetToolDeleteBtn();
        return;
      }

      // Inject restore + permanent-delete buttons into the row head
      _injectToolDeletedActions(row, interactionId, idx);

    } catch (e) {
      console.warn('Delete tool call error:', e);
      alert('Could not delete this tool call — no response from the server.');
      row.classList.remove('deleted');
      btn.style.display = '';
      _resetToolDeleteBtn();
    }
  });
  return btn;
}

/**
 * Inject restore + permanent-delete buttons into a deleted tool call row's head.
 * Both use the two-click hazard confirm pattern.
 */
function _injectToolDeletedActions(row, interactionId, idx) {
  if (!row) return;
  const head = row.querySelector('.ca-tool-head');
  if (!head) return;

  const btnRow = document.createElement('span');
  btnRow.style.cssText = 'display:inline-flex;gap:3px;margin-left:auto;flex:0 0 auto;';

  const restoreBtn = document.createElement('button');
  restoreBtn.type = 'button';
  restoreBtn.className = 'ca-tool-delete-btn';
  restoreBtn.title = 'Restore this tool call';
  restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
  restoreBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (restoreBtn.dataset.state !== 'warning') {
      restoreBtn.dataset.state = 'warning';
      restoreBtn.title = 'Click again to restore';
      restoreBtn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      restoreBtn.classList.add('warning');
      const timer = setTimeout(() => {
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore this tool call';
        restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
        restoreBtn.classList.remove('warning');
      }, 4000);
      restoreBtn._timer = timer;
      return;
    }
    if (restoreBtn._timer) { clearTimeout(restoreBtn._timer); restoreBtn._timer = null; }
    const sid = app.currentSessionId;
    if (!sid || !interactionId || idx == null) return;
    const uid = app.currentUserId || '';
    try {
      const url = apiPath('/api/v1/db/tool-call/restore?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(interactionId)
        + '&tool_call_idx=' + encodeURIComponent(idx)
        + (uid ? '&user_id=' + encodeURIComponent(uid) : ''));
      const resp = await fetch(url, { method: 'POST', headers: { ...authHeaders() } });
      if (resp.ok) {
        row.classList.remove('deleted');
        btnRow.remove();
      } else {
        alert('Could not restore this tool call (server responded ' + resp.status + ').');
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore this tool call';
        restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
        restoreBtn.classList.remove('warning');
      }
    } catch (e) {
      alert('Could not restore this tool call — no response from the server.');
    }
  });
  btnRow.appendChild(restoreBtn);

  const permDeleteBtn = document.createElement('button');
  permDeleteBtn.type = 'button';
  permDeleteBtn.className = 'ca-tool-delete-btn';
  permDeleteBtn.title = 'Permanently delete this tool call';
  permDeleteBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
  permDeleteBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (permDeleteBtn.dataset.state !== 'warning') {
      permDeleteBtn.dataset.state = 'warning';
      permDeleteBtn.title = 'Click again to permanently erase';
      permDeleteBtn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      permDeleteBtn.classList.add('warning');
      const timer = setTimeout(() => {
        permDeleteBtn.dataset.state = '';
        permDeleteBtn.title = 'Permanently delete this tool call';
        permDeleteBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
        permDeleteBtn.classList.remove('warning');
      }, 4000);
      permDeleteBtn._timer = timer;
      return;
    }
    if (permDeleteBtn._timer) { clearTimeout(permDeleteBtn._timer); permDeleteBtn._timer = null; }
    const sid = app.currentSessionId;
    if (!sid || !interactionId || idx == null) return;
    const uid = app.currentUserId || '';
    try {
      const url = apiPath('/api/v1/db/tool-call?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(interactionId)
        + '&tool_call_idx=' + encodeURIComponent(idx)
        + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
        + '&permanent=true');
      const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
      if (resp.ok) {
        row.remove();
      } else {
        alert('Could not permanently delete this tool call (server responded ' + resp.status + ').');
        permDeleteBtn.dataset.state = '';
        permDeleteBtn.title = 'Permanently delete this tool call';
        permDeleteBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
        permDeleteBtn.classList.remove('warning');
      }
    } catch (e) {
      alert('Could not permanently delete this tool call — no response from the server.');
    }
  });
  btnRow.appendChild(permDeleteBtn);

  // Insert after the meta span, before the caret
  const caret = head.querySelector('.ca-tool-caret');
  if (caret) head.insertBefore(btnRow, caret);
  else head.appendChild(btnRow);
}

function _buildRow(entry, idx) {
  const row = document.createElement('div');
  row.className = 'ca-tool-row' + (entry.open ? ' open' : '') + (entry._deleted ? ' deleted' : '');
  row.dataset.i = String(idx);
  // Tag with the parent interaction id so per-message delete can selectively mark rows.
  if (entry._detailMsgId) row.dataset.detailMsgId = entry._detailMsgId;

  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'ca-tool-head';
  head.setAttribute('aria-expanded', entry.open ? 'true' : 'false');

  const status = document.createElement('span');
  status.className = 'ca-tool-status ca-status-' + (entry._deleted ? 'deleted' : entry.status);
  status.textContent = entry._deleted ? '⊘' : entry.status === 'done' ? '✓' : entry.status === 'error' ? '✕' : entry.status === 'remote_placeholder' ? '⊘' : '';
  head.appendChild(status);

  if (entry.turn > 0) {
    const turn = document.createElement('span');
    turn.className = 'ca-tool-turn';
    turn.textContent = 'Turn ' + entry.turn;
    head.appendChild(turn);
  }

  const name = document.createElement('span');
  name.className = 'ca-tool-name';
  name.textContent = entry.tool;
  head.appendChild(name);

  const meta = document.createElement('span');
  meta.className = 'ca-tool-meta';
  if (entry._deleted) meta.textContent = 'deleted';
  else if (entry.status === 'running') meta.textContent = 'running…';
  else if (entry.status === 'error') meta.textContent = entry.errorType || 'error';
  else if (entry.status === 'remote_placeholder') meta.textContent = 'remote device';
  else meta.textContent = (entry.durationMs != null) ? entry.durationMs + 'ms' : 'done';
  head.appendChild(meta);

  // Copy-all button for the whole tool call (skip for remote placeholders and deleted)
  if (entry.status !== 'remote_placeholder' && !entry._deleted) {
    const fullText = 'Tool: ' + entry.tool + '\nArguments:\n' + _fmtArgs(entry.args) + '\nResult:\n' + (entry.result || '(empty)');
    const copyAllBtn = _makeCopyBtn(fullText, 'tool call');
    head.appendChild(copyAllBtn);
  }

  // Delete button — shown on live rows. Deleted rows get restore + permanent delete instead.
  if (entry._deleted) {
    // Will be injected below after the row is fully built
  } else if (entry.status !== 'remote_placeholder') {
    const delBtn = _makeToolDeleteBtn(entry, row);
    head.appendChild(delBtn);
  }

  const caret = document.createElement('span');
  caret.className = 'ca-tool-caret';
  caret.setAttribute('aria-hidden', 'true');
  caret.textContent = '›';
  head.appendChild(caret);

  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ca-tool-body';

  // ── Saved output (full LLM response with tool calls) ──
  if (entry._savedOutput) {
    const outputLbl = document.createElement('div');
    outputLbl.className = 'ca-tool-label';
    outputLbl.textContent = 'LLM Output';
    body.appendChild(outputLbl);
    const outputWrap = document.createElement('div');
    outputWrap.className = 'ca-tool-pre-wrap';
    const outputPre = document.createElement('pre');
    outputPre.className = 'ca-tool-pre';
    let outputText = entry._savedOutput;
    try { outputText = JSON.stringify(JSON.parse(entry._savedOutput), null, 2); } catch (_) {}
    outputPre.textContent = outputText;
    outputWrap.appendChild(outputPre);
    outputWrap.appendChild(_makeCopyBtn(outputText, 'LLM output'));
    body.appendChild(outputWrap);
  }

  // ── Saved tool output (tool result from DB) ──
  if (entry._savedToolOutput) {
    const toolOutLbl = document.createElement('div');
    toolOutLbl.className = 'ca-tool-label';
    toolOutLbl.textContent = 'Tool Output (saved)';
    body.appendChild(toolOutLbl);
    const toolOutWrap = document.createElement('div');
    toolOutWrap.className = 'ca-tool-pre-wrap';
    const toolOutPre = document.createElement('pre');
    toolOutPre.className = 'ca-tool-pre';
    let toolOutText = entry._savedToolOutput;
    try { toolOutText = JSON.stringify(JSON.parse(entry._savedToolOutput), null, 2); } catch (_) {}
    toolOutPre.textContent = toolOutText;
    toolOutWrap.appendChild(toolOutPre);
    toolOutWrap.appendChild(_makeCopyBtn(toolOutText, 'tool output'));
    body.appendChild(toolOutWrap);
  }

  // ── Saved tool metadata ──
  if (entry._savedToolMetadata) {
    const metaLbl = document.createElement('div');
    metaLbl.className = 'ca-tool-label';
    metaLbl.textContent = 'Tool Metadata';
    body.appendChild(metaLbl);
    const metaWrap = document.createElement('div');
    metaWrap.className = 'ca-tool-pre-wrap';
    const metaPre = document.createElement('pre');
    metaPre.className = 'ca-tool-pre';
    let metaText = entry._savedToolMetadata;
    try { metaText = JSON.stringify(JSON.parse(entry._savedToolMetadata), null, 2); } catch (_) {}
    metaPre.textContent = metaText;
    metaWrap.appendChild(metaPre);
    metaWrap.appendChild(_makeCopyBtn(metaText, 'tool metadata'));
    body.appendChild(metaWrap);
  }

  const argLbl = document.createElement('div');
  argLbl.className = 'ca-tool-label';
  argLbl.textContent = 'Arguments';
  body.appendChild(argLbl);
  const argWrap = document.createElement('div');
  argWrap.className = 'ca-tool-pre-wrap';
  const argPre = document.createElement('pre');
  argPre.className = 'ca-tool-pre';
  argPre.textContent = _fmtArgs(entry.args);
  argWrap.appendChild(argPre);
  argWrap.appendChild(_makeCopyBtn(_fmtArgs(entry.args), 'arguments'));
  body.appendChild(argWrap);

  const resLbl = document.createElement('div');
  resLbl.className = 'ca-tool-label';
  resLbl.textContent = 'Result';
  body.appendChild(resLbl);

  if (entry.status === 'running') {
    const waiting = document.createElement('pre');
    waiting.className = 'ca-tool-pre';
    waiting.textContent = 'Waiting…';
    body.appendChild(waiting);
  } else if (typeof entry.result === 'string' && entry.result.startsWith('/screenshots/')) {
    const img = document.createElement('img');
    img.className = 'ca-tool-img';
    img.src = entry.result;
    img.alt = entry.tool + ' result';
    body.appendChild(img);
  } else {
    const resWrap = document.createElement('div');
    resWrap.className = 'ca-tool-pre-wrap';
    const resPre = document.createElement('pre');
    resPre.className = 'ca-tool-pre';
    resPre.textContent = (entry.result && entry.result.length) ? entry.result : '(empty)';
    resWrap.appendChild(resPre);
    resWrap.appendChild(_makeCopyBtn((entry.result && entry.result.length) ? entry.result : '(empty)', 'result'));
    body.appendChild(resWrap);
  }

  row.appendChild(body);

  // For deleted entries on reload, inject restore + permanent-delete buttons
  if (entry._deleted && entry._detailMsgId != null && entry._detailIdx != null) {
    _injectToolDeletedActions(row, entry._detailMsgId, entry._detailIdx);
  }

  return row;
}

function renderPanel() {
  if (!panelEl) return;
  panelEl.textContent = '';
  if (toolCalls.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'ca-tool-head';
    empty.style.cursor = 'default';
    empty.textContent = 'No tool calls this turn.';
    panelEl.appendChild(empty);
    return;
  }
  toolCalls.forEach((entry, i) => panelEl.appendChild(_buildRow(entry, i)));
}

// ── Panel open / close ──────────────────────────────────────────────────────

function openPanel() {
  if (expanded || !panelEl) return;
  expanded = true;
  renderPanel();
  panelEl.hidden = false;
  if (rootEl) rootEl.classList.add('expanded');
  if (barEl) barEl.setAttribute('aria-expanded', 'true');
  // Hide suggested replies while the tool-call panel is open
  const suggestRow = document.getElementById('chat-suggest-row');
  if (suggestRow) suggestRow.style.display = 'none';

  _outsideHandler = (e) => {
    // Panel is a sibling of the bar now (both direct children of rootEl).
    // Close only if the click is outside BOTH the bar and the panel.
    const inside = (barEl && barEl.contains(e.target)) || (panelEl && panelEl.contains(e.target));
    if (!inside) closePanel();
  };
  _keyHandler = (e) => { if (e.key === 'Escape') closePanel(); };
  // Defer so the click that opened the panel doesn't immediately close it.
  setTimeout(() => {
    document.addEventListener('mousedown', _outsideHandler);
    document.addEventListener('keydown', _keyHandler);
  }, 0);
}

function closePanel() {
  if (!expanded) return;
  expanded = false;
  if (panelEl) panelEl.hidden = true;
  if (rootEl) rootEl.classList.remove('expanded');
  if (barEl) barEl.setAttribute('aria-expanded', 'false');
  // Restore suggested replies when panel closes
  const suggestRow = document.getElementById('chat-suggest-row');
  if (suggestRow) suggestRow.style.display = '';
  if (_outsideHandler) { document.removeEventListener('mousedown', _outsideHandler); _outsideHandler = null; }
  if (_keyHandler) { document.removeEventListener('keydown', _keyHandler); _keyHandler = null; }
}

function togglePanel() {
  if (toolCalls.length === 0) return; // nothing to show
  if (expanded) closePanel();
  else openPanel();
}

// ── Event → note mapping ────────────────────────────────────────────────────

// Map a non-tool live event to a short note. Returns null to leave the note
// as-is (so internal/no-op steps don't blank out a meaningful note mid-turn).
function eventToNote(event) {
  const type = event.type;
  if (type === 'stream') return 'Writing reply…';
  if (type === 'pipeline') {
    switch (event.step) {
      case 'load_context':              return 'Loading context';
      case 'prep_tools':                return 'Building tools';
      case 'prep_history':              return 'Loading history';
      case 'memory_search_start':       return 'Searching memory';
      case 'build_prompt':              return 'Preparing';
      case 'attachment':                return 'Reading attachment';
      case 'attachment_describe_start': return 'Looking at image';
      case 'load_tools':                return 'Building tools';
      case 'data_src_loaded':           return 'Loading data';
      case 'turn_start':
      case 'llm_call_start':            return 'Thinking…';
      case 'guardrail_blocked':         return 'Blocked for safety';
      case 'agent_delegation':          return 'Handing off';
      case 'memory_save_start':         return 'Saving memory';
      default:                          return null;
    }
  }
  return null;
}

// Ensure the indicator is live and update the note. Resting counts as "not
// active", so a meaningful event begins a fresh turn (resetting the tool list).
function _ensureActive(note) {
  _clearEndTimer();
  if (!active) { _resetForNewTurn(); _activate(); }
  // A re-light AFTER the turn's terminal event is background housekeeping (memory
  // save, turn hooks, …). Settle it on the short backstop, never the in-turn
  // watchdog, so it can't hang the chip; the step's `*_end` clears it sooner.
  if (_turnEnded) _armPostTurnSettle();
  else _armWatchdog();
  setNote(note);
}

function handleEvent(event) {
  if (!event || event.type === 'ping') return;

  // Defensive: agentWs.js already only forwards current-session events, but an
  // untagged event from elsewhere shouldn't hijack the indicator.
  const sid = event.session_id || event.sessionId || '';
  if (sid && app.currentSessionId && sid !== app.currentSessionId) return;

  // Track the inference-turn number — turn_start (and several other in-loop
  // events) carry `turn`. tool_call/tool_result don't, so they inherit the
  // last value seen this exchange.
  // When the turn number INCREASES, it means the previous turn's tool calls
  // are complete — attach them to the last agent bubble before resetting.
  if (typeof event.turn === 'number' && event.turn > 0 && event.turn !== currentTurn) {
    const prevTurn = currentTurn;
    currentTurn = event.turn;
    _turnEnded = false;   // a numbered turn is live — leave post-turn settle mode
    // If we had tool calls from the previous turn, attach them now.
    if (prevTurn > 0 && toolCalls.length > 0 && app.attachToolCallsToLastBubble) {
      const calls = toolCalls.slice();
      app.attachToolCallsToLastBubble(calls, undefined, currentToolAnchor);
    }
    _resetForNewTurn();
  }

  const type = event.type;

  // The db event is emitted after the assistant row containing this turn's
  // structured tool calls is persisted. Its sequence belongs to the durable
  // interaction (unlike tool_call/tool_result event sequence numbers), so it
  // is the authoritative anchor for the live combined tool-call line.
  if (type === 'db' && event.role === 'assistant' && event.id
      && (event.op === 'insert_interaction' || event.interaction_seq != null)) {
    currentToolAnchor = {
      id: event.id,
      interactionSeq: Number(
        event.interaction_seq != null ? event.interaction_seq : event.session_seq,
      ),
      turnId: event.turn_id || null,
      createdAt: event.created_at || event.emit_time,
    };
    if (!app._interactionAnchors) app._interactionAnchors = new Map();
    app._interactionAnchors.set(String(event.id), currentToolAnchor);
    return;
  }

  // Terminal events end the turn. Mark _turnEnded so anything the backend emits
  // AFTER this (background memory save, turn hooks, …) is treated as post-turn
  // housekeeping that self-clears, rather than re-lighting the bar indefinitely.
  // Also clear the stop-pending flag so the next turn starts clean.
  if (type === 'response')    { _turnEnded = true; app._stopPending = false; stop(); return; }
  if (type === 'error')       { _turnEnded = true; app._stopPending = false; if (active) setNote('Error');   _endSoon(); return; }
  if (type === 'interrupted') { _turnEnded = true; app._stopPending = false; if (active) setNote('Stopped'); _endSoon(); return; }

  // In-turn signals can never be post-turn housekeeping. If one arrives while we
  // still think the turn ended — e.g. an event/automation run that reused the
  // prior turn number and emitted no fresh user_message — we're actually in a new
  // live turn, so drop settle mode and let it claim the full in-turn watchdog.
  if (type === 'stream'
      || (type === 'pipeline' && (event.step === 'turn_start' || event.step === 'llm_call_start'))) {
    // When a stop was requested (user clicked Stop), the backend may still stream
    // a few more tokens before it checks the interrupt flag. Don't let those
    // late-arriving events re-light the indicator or turn "Stopping…" back into
    // "Thinking…" / "Writing reply…".
    if (!app._stopPending) _turnEnded = false;
  }

  // A user message is the authoritative new-turn boundary (covers fresh sends,
  // interrupt-and-replace, and event-triggered runs) — always reset the list.
  if (type === 'user_message') {
    _clearEndTimer();
    _resetForNewTurn();
    app._activeToolGroupBubble = null;
    _turnEnded = false;       // fresh exchange — out of post-turn settle mode
    app._stopPending = false; // never carry a stale stop into a new turn
    app._turnHasBubble = false;   // new exchange — needs its own fresh bubble
    currentTurn = 0;          // new exchange — turn_start will set it to 1
    _activate();
    _armWatchdog();
    setNote('Thinking…');
    return;
  }

  if (type === 'tool_call') {
    // Vision ingestion calls hold their args here and render out-of-band when the
    // result lands (see _renderSynthToolBubble), so they don't get wiped by the
    // turn-boundary resets that bracket ingestion. (Memory tools have no tool_call
    // event — their args ride memory_search_start / memory_save_end instead.)
    if (_SYNTH_TOOLS[event.tool]) {
      _pendingSynthArgs[event.tool] = event.args || {};
      _ensureActive(_turnPrefix() + 'Reading ' + (event.tool || 'image'));
      return;
    }
    _ensureActive(_turnPrefix() + 'Toolcall ' + (event.tool || 'tool'));
    addToolCall(event.tool, event.args, event.tool_call_id);
    return;
  }
  if (type === 'tool_result') {
    // Synthetic tools that DO emit a tool_result (vision, memory_search) render
    // their own standalone bubble; args come from whatever was stashed at start.
    // Memory tools show as a small debug note in the transcript instead.
    if (_SYNTH_TOOLS[event.tool]) {
      _ensureActive(_turnPrefix() + (event.error ? 'Error ' : 'Done ') + (event.tool || 'image'));
      if (event.tool !== 'memory_search' && event.tool !== 'memory_save') {
        _renderSynthToolBubble(event.tool, {
          args: _pendingSynthArgs[event.tool], result: event.result,
          error: !!event.error, durationMs: event.duration_ms,
          errorType: event.error_type, id: event.id,
        });
      } else if (event.tool === 'memory_search') {
        _appendMemoryNote('Memory searched');
      }
      return;
    }
    _ensureActive(_turnPrefix() + (event.error ? 'Error ' : 'Done ') + (event.tool || 'tool'));
    resolveToolResult(event.tool, event.result, event.duration_ms, !!event.error,
                      event.error_type, event.tool_call_id);
    _maybeShowVaultCard(event);
    _maybeShowChatComponent(event);
    return;
  }

  // Live estimate: while the LLM streams content, count output tokens in real-time
  if (type === 'stream' && typeof event.content === 'string') {
    _onStreamContent(event.content);
  }

  // The resolved model is known before the first token. Seed the live bubble's
  // footer here instead of waiting for llm_call_end after streaming finishes.
  if (type === 'pipeline' && event.step === 'llm_call_start') {
    if (event.model) app._activeTurnModel = event.model;
    app._activeTurnEffort = event.effort || null;
  }

  // Capture token usage from pipeline llm_call_end events
  if (type === 'pipeline' && event.step === 'llm_call_end') {
    if (typeof event.input_tokens === 'number' || typeof event.output_tokens === 'number') {
      addTokens(event.input_tokens || 0, event.output_tokens || 0, event.cost_usd);
    }
    // This is the provider's exact prompt-token count for the call that just
    // completed. It supersedes the pre-call estimate emitted for compaction.
    if (typeof event.input_tokens === 'number') {
      _contextTokens = event.input_tokens;
      _pendingCtxEstimate = 0;
      _renderCtxIndicator();
    }
    // Remember which model/effort ran this turn so the bubble footer can tag it
    // the moment the live answer finalizes (chat-stream.js reads these).
    if (event.model) app._activeTurnModel = event.model;
    app._activeTurnEffort = event.effort || null;
    // Fire the premium glow LIVE on the streaming bubble as soon as llm_call_end
    // reports the effort — don't wait for finalize. This is the earliest point the
    // model/effort are known; the streaming bubble is already in the DOM.
    if (event.effort) {
      const eff = String(event.effort).trim().toLowerCase();
      const streaming = document.querySelector('.chat-bubble.agent.streaming');
      if (streaming) streaming.classList.toggle('premium', eff === 'high');
    }
    // Alternate-engine agents (Local Claude Code) don't resolve through the app's
    // model — the run itself reports the real model it used. Adopt it for the
    // footer name + context window so they describe the actual Claude model,
    // not a placeholder (the load-time resolve only knew the *configured* model,
    // which is blank when the CLI picks Claude's own default).
    if (_altEngine && event.model) {
      _currentModelName = event.model;
      const win = _claudeCtxWindow(event.model);
      if (win) _modelContextLimit = win;
      _renderCtxIndicator();
      _renderModelBtn();
    }
  }

  // Live context size from pipeline context_status events
  if (type === 'pipeline' && event.step === 'context_status') {
    if (typeof event.tokens === 'number') {
      _contextTokens = event.tokens;
      _pendingCtxEstimate = 0; // real data replaces the thinking ramp
      _renderCtxIndicator();
    }
  }

  // Loop-node memory tools have no normal tool_call event, so they ride the
  // pipeline lifecycle steps. memory_search stashes its query from the start
  // step (the activity bar shows "Searching memory"). memory_save has no
  // tool_result at all. Neither renders a bubble in the chat transcript —
  // they're hidden. The completion-settle handler below clears the
  // "Saving memory" chip.
  if (type === 'pipeline') {
    if (event.step === 'memory_search_start') {
      _pendingSynthArgs['memory_search'] = { query: event.query, limit: event.limit };
    } else if (event.step === 'memory_save_end') {
      _appendMemoryNote('Memory saved');
    }
  }

  // Post-turn background steps (memory save, …) finish with an `*_end`/completion
  // pipeline step. Once the turn's terminal event has fired, treat that as a
  // settle signal so the bar clears the moment the work is actually done, instead
  // of lingering on its `*_start` note. Guarded by _turnEnded so in-turn `*_end`
  // steps (llm_call_end fires every LLM call) can't clear a live turn.
  if (_turnEnded && type === 'pipeline' && _isCompletionStep(event.step)) {
    if (active) _endSoon();   // brief flash of the note, then fade
    return;
  }

  const note = eventToNote(event);
  if (note == null) {
    // A post-turn sign of life must NOT re-arm the long in-turn watchdog — that
    // would re-pin a stuck note for 3 min. Keep the short settle ticking instead.
    if (active) { if (_turnEnded) _armPostTurnSettle(); else _armWatchdog(); }
    return;
  }
  // When a stop was requested, let the backend's activity events still tick over
  // (watchdog, token counters) but don't replace "Stopping…" with "Writing reply…"
  // or any other mid-stream note — only terminal events clear the stop state.
  if (app._stopPending) return;
  _ensureActive(note);
}

// ── Exported helpers for chat.js (bubble-attached tool panels) ──────────────
// Reuse the same accordion row rendering so bubble panels look identical to the
// live activity panel.

export { _buildRow as buildToolRow };

export function initChatActivity() {
  rootEl = document.getElementById('chat-above-pill');
  pillEl = document.getElementById('chat-input-row');
  barEl = document.getElementById('chat-activity-bar');
  textEl = barEl ? barEl.querySelector('.chat-activity-text') : null;
  panelEl = document.getElementById('chat-activity-panel');
  tokenBarEl = document.getElementById('chat-token-bar');
  tokensInEl = document.getElementById('chat-tokens-in');
  tokensOutEl = document.getElementById('chat-tokens-out');
  tokenSpinnerEl = document.getElementById('chat-token-spinner');
  // Stats are now inside the pill (cell 2,1) — no more separate footer-left pill.
  footerLeftEl = document.getElementById('chat-pill-stats');
  // The stats row is now inside #chat-pill-stats, not a separate #chat-footer-row.
  // FOOTER-STATS-FIT still works on this element for shedding content.
  footerRowEl = document.getElementById('chat-pill-stats');
  modelBtnEl = document.getElementById('chat-model-btn');
  modelCtxEl = document.getElementById('chat-model-ctx');
  costEl = document.getElementById('chat-cost');
  // Always show the session cost (starts at $0), not gated on a non-zero total.
  _renderCost();

  // FOOTER-STATS-FIT: re-fit when the footer row's width changes (panel resize,
  // chat-pill drag, window resize). Content-driven width changes (token counts,
  // ctx, cost) call _scheduleFooterFit from their render paths instead, since
  // those don't change the row's own box and so don't trip the observer. The
  // badge module (abilities.js) calls app.fitFooterStats when its count widens.
  if (footerRowEl && typeof ResizeObserver !== 'undefined') {
    try {
      // Width-only guard: the final shed step collapses the left pill, which can
      // change the row's HEIGHT — re-firing the observer and (without this) the
      // fit, on a loop. The row's width is parent-driven and unaffected by our
      // class toggles, so keying off width alone is both sufficient and safe.
      new ResizeObserver(entries => {
        const w = Math.round(entries[0].contentRect.width);
        if (w === _lastFitRowWidth) return;
        _lastFitRowWidth = w;
        _scheduleFooterFit();
      }).observe(footerRowEl);
    } catch (_) { /* ResizeObserver unavailable — render-path + badge hooks still fit */ }
  }
  _scheduleFooterFit();

  // The agent Config tab's "Query CLI for latest model options" button broadcasts
  // this after a successful force re-query — drop our page cache so the next
  // footer open shows the freshly queried list immediately.
  window.addEventListener('engine-catalog-refreshed', () => {
    _engineCatalogCache = null;
    _engineCatalogCacheTs = 0;
  });

  // ── Model changer (footer row chevron buttons) ──────────────────────────
  // MUST be wired BEFORE refreshModelContext() so the wrapped version captures
  // the first model-resolve call. Also hooks the WebSocket handler and the
  // async model-list fetch to stay in sync.
  const _changerEl = document.getElementById('chat-model-changer');
  const _changerName = _changerEl ? _changerEl.querySelector('.cmc-name') : null;
  const _toastEl = document.getElementById('cmc-toast');

  /** Truncate a model id from the front — show the tail with a leading `…`. */
  function _shortTail(id, maxLen = 18) {
    if (!id) return '';
    const base = id.includes('/') ? id.split('/').pop() : id;
    if (base.length <= maxLen) return base;
    return '…' + base.slice(-(maxLen - 1));
  }

  function _syncModelChanger() {
      if (!_changerEl || !_changerName) return;
      // Alternate engine (Claude/Codex): show the harness's real model choice.
      if (_altEngine) {
        const catalog = _engineCatalogNow();
        if (!catalog) { _changerName.textContent = 'Model'; return; }
        _changerEl.classList.remove('saving');
        _changerEl.style.display = '';
        const m = _engineCurModel || '';
        let label = 'Default';
        if (m) {
          const hit = catalog.models.find(x => x.v === m)
            || (catalog.lane === 'claude_code' ? catalog.models.find(x => x.v === _claudeAliasOf(m)) : null);
          label = hit ? hit.label : _shortTail(m);
        }
        _changerName.textContent = label;
        return;
      }
      // Still loading — show spinner, not a misleading "Default" label.
      if (!_allSlots.length) {
        _changerEl.style.display = '';
        _changerEl.classList.add('saving');
        return;
      }
      _changerEl.classList.remove('saving');
      const slot = _allSlots.find(s => {
        const ref = s.type === 'role' ? 'role:' + s.role : 'custom:' + s.position;
        return ref === _currentSlotRef;
      });
      const model = (slot && slot.model) || '';
      if (!model) {
        _changerEl.style.display = '';
        _changerName.textContent = 'Default';
        return;
      }
      _changerEl.style.display = '';
      // Show only the three-letter slot abbreviation (no model name)
      let label = '';
      if (slot.type === 'role') {
        const labels = {standard: 'Std', premium: 'Prem', image_in: 'Vis', image_out: 'Img'};
        label = labels[slot.role] || slot.role;
      } else {
        label = 'C' + slot.position;
      }
      _changerName.textContent = label;
    }

  // Sync when model changes via picker selection (_selectModel at ~line 1023)
  const _origSelect = _selectModel;
  _selectModel = function(modelId) {
    const p = _origSelect.apply(this, arguments);
    if (p && p.then) p.then(ok => { if (ok) _syncModelChanger(); });
    return p;
  };

  function _showModelToast(text) {
    if (!_toastEl || !_changerEl) return;
    const rect = _changerEl.getBoundingClientRect();
    _toastEl.textContent = text;
    _toastEl.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
    _toastEl.style.left = Math.max(4, Math.min(rect.left + rect.width / 2 - 80, window.innerWidth - 170)) + 'px';
    _toastEl.className = 'cmc-toast';
    _toastEl.style.display = 'block';
    void _toastEl.offsetWidth;
    _toastEl.classList.add('visible');
    clearTimeout(_toastEl._hideTimer);
    _toastEl._hideTimer = setTimeout(() => {
      _toastEl.classList.remove('visible');
      _toastEl.classList.add('hiding');
      setTimeout(() => { _toastEl.style.display = 'none'; _toastEl.className = 'cmc-toast'; }, 200);
    }, 2000);
  }

  /** Switch the active model slot and show saving → checked feedback on the
   *  changer button.  Returns the promise from _selectModel so callers can chain
   *  model-list syncing after the API confirms success. */
  async function _saveModelWithFeedback(slotRef) {
    if (!_changerEl) return;
    // Resolve the slot so we can show the label *after* the check animation.
    const slot = _allSlots.find(s => {
      const ref = s.type === 'role' ? 'role:' + s.role : 'custom:' + s.position;
      return ref === slotRef;
    });
    const nextLabel = slot
      ? (slot.type === 'role'
          ? ({standard: 'Std', premium: 'Prem', image_in: 'Vis', image_out: 'Img'}[slot.role] || slot.role)
          : 'C' + slot.position)
      : '';
    const nextModel = (slot && slot.model) || '';

    _changerEl.classList.add('saving');
    const ok = await _selectModel(slotRef);
    if (ok) {
      _changerEl.classList.remove('saving');
      _changerEl.classList.add('checked');
      // Update the label to the new model while the check is shown — the check
      // covers the label anyway, so this is invisible. When .checked is removed
      // below the new label is already in place.
      _syncModelChanger();
      _showModelToast(_shortTail(nextModel));
      setTimeout(() => { _changerEl.classList.remove('checked'); }, 700);
    } else {
      _changerEl.classList.remove('saving');
    }
    return ok;
  }

  function _cycleModel(dir) {
    if (!_changerEl) return;
    // Alternate engine: cycle the harness's own model catalog.
    if (_altEngine) { _cycleEngineModel(dir); return; }
    _changerEl.classList.add('saving');
    // Always re-fetch the model list from the server before cycling, so the
    // chevrons always reflect the current agent config — reorders, additions,
    // and removals from the Config tab are picked up immediately, never cycling
    // onto a stale or removed model.
    _fetchModelsForPicker().then(() => _doCycle(dir, _allSlots));
  }
  function _doCycle(dir, list) {
    if (!list.length) { _changerEl.classList.remove('saving'); return; }
    const cur = _currentSlotRef || '';
    let idx = list.findIndex(s => {
      const ref = s.type === 'role' ? 'role:' + s.role : 'custom:' + s.position;
      return ref === cur;
    });
    if (idx === -1) idx = 0;
    idx = (idx + dir + list.length) % list.length;
    const next = list[idx];
    if (!next) { _changerEl.classList.remove('saving'); return; }
    const nextRef = next.type === 'role' ? 'role:' + next.role : 'custom:' + next.position;
    if (nextRef === cur) { _changerEl.classList.remove('saving'); return; }
    _saveModelWithFeedback(nextRef).then(ok => {
      if (!ok) _changerEl.classList.remove('saving');
    });
  }

  /** Cycle the alternate-engine model catalog (chevron arrows). Re-reads the
   *  agent's saved config + live CLI catalog first so the cycle never lands on
   *  a stale value. */
  async function _cycleEngineModel(dir) {
    const catalog = (await _getEngineCatalog()) || _engineCatalog();
    if (!catalog || !_changerEl) return;
    try {
      const uq = `?user_id=${encodeURIComponent(app.currentUserId || '')}`;
      const res = await fetch(`/api/v1/agents/${encodeURIComponent(app.currentAgentId || '')}${uq}`, { headers: { ...authHeaders() } });
      if (res.ok) {
        const a = await res.json();
        const lane = (a.agent || a)[catalog.lane] || {};
        _engineCurModel = String(lane.model || '').trim();
        _engineCurEffort = String(lane.effort || '').trim();
      }
    } catch (_) { /* best-effort */ }
    const cur = _engineCurModel || '';
    let idx = catalog.models.findIndex(m => m.v === cur);
    if (idx === -1) idx = 0;
    idx = (idx + dir + catalog.models.length) % catalog.models.length;
    const next = catalog.models[idx];
    if (next.v === cur) return;
    _changerEl.classList.add('saving');
    const ok = await _saveEngineField('model', next.v);
    if (ok) {
      _engineCurModel = next.v;
      _changerEl.classList.remove('saving');
      _changerEl.classList.add('checked');
      _syncModelChanger();
      _showModelToast(next.v || 'Default');
      setTimeout(() => _changerEl.classList.remove('checked'), 700);
    } else {
      _changerEl.classList.remove('saving');
    }
  }

  // ── Model list panel (click the changer label, opens a role-highlighted list) ──
  function _buildModelListPanel() {
    if (_modelListPanelEl) return;
    const panel = document.createElement('div');
    panel.className = 'chat-skill-picker chat-model-list-panel';
    panel.style.display = 'none';
    const header = document.createElement('div');
    header.className = 'csp-header';
    header.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>Models';
    panel.appendChild(header);
    const list = document.createElement('div');
    list.className = 'csp-list';
    panel.appendChild(list);
    document.body.appendChild(panel);
    _modelListPanelEl = panel;
    _modelListPanelList = list;
    document.addEventListener('click', (e) => {
      if (_modelListPanelEl && _modelListPanelEl.style.display !== 'none'
          && !_modelListPanelEl.contains(e.target)
          && e.target !== _changerEl
          && !(_changerEl && _changerEl.contains(e.target))) {
        _modelListPanelEl.style.display = 'none';
      }
    });
    panel.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { _modelListPanelEl.style.display = 'none'; }
    });
  }
  function _toggleModelListPanel() {
    // Alternate engines pick from the harness's own catalog — open that panel.
    if (_altEngine) { _toggleEnginePanel(); return; }
    if (!_modelListPanelEl) _buildModelListPanel();
    if (_modelListPanelEl.style.display !== 'none') {
      _modelListPanelEl.style.display = 'none';
      return;
    }
    // Close the old model picker if open
    if (_modelPickerEl) _modelPickerEl.style.display = 'none';
    const footer = document.getElementById('chat-footer-row') || document.getElementById('chat-input-row');
    if (!footer) return;
    const rect = footer.getBoundingClientRect();
    _modelListPanelEl.style.top = 'auto';
    _modelListPanelEl.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
    _modelListPanelEl.style.left = 'auto';
    _modelListPanelEl.style.right = Math.max(4, Math.min(window.innerWidth - rect.right, window.innerWidth - 290)) + 'px';
    setTimeout(() => { _modelListPanelEl.style.display = 'block'; }, 0);
    // Show loading state, fetch models, then render (same pattern as _toggleModelPicker)
    _modelListPanelList.innerHTML = '<div class="csp-empty">Loading models…</div>';
    _fetchModelsForPicker().then(() => {
      _renderModelListPanel();
    });
  }
  function _getModelRole(slot) {
    const slotRef = slot.type === 'role' ? 'role:' + slot.role : 'custom:' + slot.position;
    const isActive = slotRef === _currentSlotRef;
    if (isActive) return 'active';
    // Role slots get named badges; custom are 'plain'.
    if (slot.type === 'role') return slot.role === 'premium' ? 'premium' : 'multi';
    return 'plain';
  }

  // ── Per-slot effort selector ──────────────────────────────────────────────
  // When the user clicks a model row in the list panel, an effort row expands
  // below it with minimal/low/medium/high buttons instead of selecting
  // immediately. Clicking an effort button selects the model + saves effort.

  let _openEffortSlotRef = '';     // which slot currently has its effort row open

  async function _saveEffort(slotRef, effort) {
    try {
      const headers = { 'Content-Type': 'application/json', ...authHeaders() };
      const sid = app.currentSessionId || '';
      const payload = {
        user_id: app.currentUserId || '',
        session_id: sid,
        slot_ref: slotRef,
        reasoning_effort: effort,
      };
      if (sid) {
        await fetch(apiPath('/api/v1/chat/session-model-effort'), {
          method: 'POST', headers,
          body: JSON.stringify(payload),
        });
      }
      _allEffortMap[slotRef] = effort;
    } catch (_) { /* best-effort */ }
  }

  function _openEffortRow(item, slotRef) {
    _closeEffortRow(); // only one open at a time
    _openEffortSlotRef = slotRef;

    const row = document.createElement('div');
    row.className = 'cml-effort';
    row.dataset.slotRef = slotRef;

    const curEffort = (_allEffortMap[slotRef] || '').toLowerCase();
    const levels = ['minimal', 'low', 'medium', 'high'];

    levels.forEach(lvl => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cml-eff-btn' + (lvl === curEffort ? ' cml-eff-on' : '');
      btn.textContent = lvl;
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        _saveEffort(slotRef, lvl).then(() => {
          _modelListPanelEl.style.display = 'none';
          _closeEffortRow();
          _saveModelWithFeedback(slotRef);
        });
      });
      row.appendChild(btn);
    });

    item.insertAdjacentElement('afterend', row);
  }

  function _closeEffortRow() {
    const open = _modelListPanelList && _modelListPanelList.querySelector('.cml-effort');
    if (open) open.remove();
    _openEffortSlotRef = '';
  }

  function _renderModelListPanel() {
    if (!_modelListPanelList) return;
    _closeEffortRow();
    const list = _allSlots || [];
    _modelListPanelList.innerHTML = '';
    if (!list.length) {
      _modelListPanelList.innerHTML = '<div class="csp-empty">No models configured</div>';
      return;
    }
    list.forEach((s, i) => {
      const slotRef = s.type === 'role' ? 'role:' + s.role : 'custom:' + s.position;
      const role = _getModelRole(s);
      const baseClass = 'cml-item cml-' + role;
      const item = document.createElement('div');
      item.className = baseClass + (role === 'active' ? ' csp-active' : '');
      item.dataset.slotRef = slotRef;
      const num = document.createElement('span');
      num.className = 'cml-num';
      num.textContent = (i + 1) + ':';
      item.appendChild(num);
      const nameSpan = document.createElement('span');
      nameSpan.className = 'cml-name';
      nameSpan.textContent = _shortTail(s.model || '');
      item.appendChild(nameSpan);
      // Slot type badge
      const badge = document.createElement('span');
      badge.className = 'cml-badge cml-badge-' + role;
      if (role === 'active') badge.textContent = 'active';
      else if (s.type === 'role') {
        const labels = {standard: 'Standard', premium: 'Premium', image_in: 'Vision', image_out: 'Image'};
        badge.textContent = labels[s.role] || s.role;
      }
      if (badge.textContent) item.appendChild(badge);
      item.title = (s.model || '');
      item.addEventListener('click', () => {
        // If effort row is already open for this slot, close it & select.
        if (_openEffortSlotRef === slotRef) {
          _closeEffortRow();
          _modelListPanelEl.style.display = 'none';
          _saveModelWithFeedback(slotRef);
        } else {
          _openEffortRow(item, slotRef);
        }
      });
      _modelListPanelList.appendChild(item);
    });
  }
  // Sync panel + changer when models change
  const _origFetchForPanel = _fetchModelsForPicker;
  _fetchModelsForPicker = function() {
    const p = _origFetchForPanel.apply(this, arguments);
    if (p && p.then) p.then(() => {
      _syncModelChanger();
      if (_modelListPanelEl && _modelListPanelEl.style.display !== 'none') _renderModelListPanel();
    });
    return p;
  };

  if (_changerEl) {
    const leftChev = _changerEl.querySelector('.cmc-chev-left');
    const rightChev = _changerEl.querySelector('.cmc-chev-right');
    if (leftChev) leftChev.addEventListener('click', (e) => { e.stopPropagation(); _cycleModel(-1); });
    if (rightChev) rightChev.addEventListener('click', (e) => { e.stopPropagation(); _cycleModel(1); });
    // Click the main label to open the new model list panel (chevrons have stopPropagation)
    _changerEl.addEventListener('click', _toggleModelListPanel);
    _syncModelChanger();
    // Wrap refreshModelContext to sync changer after model resolves
    const _origRefresh = refreshModelContext;
    refreshModelContext = function() {
      const p = _origRefresh.apply(this, arguments);
      if (p && p.then) p.then(() => _syncModelChanger());
      else _syncModelChanger();
      return p;
    };
  }

  if (barEl) barEl.addEventListener('click', togglePanel);

  // Show the active model's context window / max output next to the counters.
  // Re-callable (exposed below) so Settings can refresh it after a model change.
  refreshModelContext();

  // Click footer left (token bar + ctx) to open the context panel. Alternate
  // engines (Local Claude Code / Codex) get their own panel (context + plan
  // usage + the harness's real model/effort choices), since the app's model
  // list doesn't apply to them.
  function _onFooterClick(e) {
    e.stopPropagation();
    if (_altEngine) _toggleEnginePanel();
    else _toggleModelPicker();
  }
  if (footerLeftEl) {
    footerLeftEl.style.cursor = 'pointer';
    footerLeftEl.addEventListener('click', _onFooterClick);
  }
  // Also wire the ctx element directly as a reliable fallback
  if (modelCtxEl) {
    modelCtxEl.style.cursor = 'pointer';
    modelCtxEl.addEventListener('click', _onFooterClick);
  }
  // Dedicated "switch model" button below the pill (clearer affordance than the
  // ctx chip) — opens the same picker.
  if (modelBtnEl) modelBtnEl.addEventListener('click', _onFooterClick);
  // Delegated accordion toggle: one listener survives panel re-renders.
  if (panelEl) {
    panelEl.addEventListener('click', (e) => {
      const head = e.target.closest('.ca-tool-head');
      if (!head) return;
      const row = head.closest('.ca-tool-row');
      if (!row || row.dataset.i == null) return;
      const idx = parseInt(row.dataset.i, 10);
      const entry = toolCalls[idx];
      if (!entry) return;
      entry.open = !entry.open;
      row.classList.toggle('open', entry.open);
      head.setAttribute('aria-expanded', entry.open ? 'true' : 'false');
    });
  }

  // Imperative hooks for chat.js (instant feedback on send / HTTP-error paths).
  app.chatActivityStart = start;
  app.chatActivityStop = stop;
  app.chatActivityRestore = chatActivityRestore;
  // Let Settings re-pull the footer context indicator after a model change.
  app.refreshModelContext = refreshModelContext;
  // Let the abilities badge (abilities.js) re-fit the stats when its count
  // widens — that lives in the right-side group and eats the stats' space.
  app.fitFooterStats = _scheduleFooterFit;

  // Receive every current-session agent event from the per-user WebSocket.
  app._chatActivityHandler = handleEvent;
  // Wrap to sync model changer when model name arrives via WS
  const _origHandler = app._chatActivityHandler;
  app._chatActivityHandler = function(event) {
    const ret = _origHandler.apply(this, arguments);
    if (event && event.model) {
      Promise.resolve().then(() => _syncModelChanger());
    }
    return ret;
  };
  // Let sessions.js update the ctx indicator after loading message history.
  app.setContextFromMessages = setContextFromMessages;
  app.setContextTokens = setContextTokens;
  app.setSessionUsage = setSessionUsage;
}
