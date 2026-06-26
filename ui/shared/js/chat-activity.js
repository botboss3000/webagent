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
let footerLeftEl = null;  // #chat-footer-left — click target for model picker
let footerRowEl = null;   // #chat-footer-row — measured to fit the stats (FOOTER-STATS-FIT)
let modelBtnEl = null;   // #chat-model-btn — dedicated "switch model" button below the pill
let modelCtxEl = null;   // #chat-model-ctx (live context tokens / model's max limit)
let costEl = null;       // #chat-cost (session cost so far)
let _footerFitPending = false; // rAF debounce flag for _fitFooterStats
let _lastFitRowWidth = -1;     // last observed row width (ignore height-only RO ticks)
let _contextTokens = 0;  // live assembled context tokens (from context_status events)
let _modelContextLimit = null; // model's context window limit (from current-model-info)
let _costInput = null;   // active model's USD price per 1M input tokens (from current-model-info)
let _costOutput = null;  // active model's USD price per 1M output tokens (from current-model-info)
// Alternate-engine agents (e.g. Local Claude Code) don't bill on the app's model,
// so the session cost is meaningless for them — keep the token/ctx counters but
// hide the price chip. Set from current-model-info's `engine` field.
let _altEngine = false;  // true when the active agent runs on a non-default engine
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

// SYNTHETIC STANDALONE tool calls fire OUTSIDE a reply bubble's normal tool-call
// accumulation, so they're rendered out-of-band straight onto the transcript as
// their own foldable tool bubble (see _renderSynthToolBubble), mirroring the
// reload renderer (session-load.js _buildSynthCall). Two kinds:
//   • vision ingestion — process_image / route_attachment, emitted during
//     user-turn ingestion (before turn 1, before any reply bubble); the turn
//     resets that bracket ingestion would otherwise wipe the activity list.
//   • loop-node memory — memory_search (before the turn) / memory_save (after);
//     neither is a model-issued tool call, and memory_save fires post-`response`.
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
    _renderCtxIndicator();
    _renderCost();
    _renderModelBtn();
  } catch (e) {
    _altEngine = false;   // unknown engine ⇒ assume normal billing, don't strand the price hidden
    modelCtxEl.style.display = 'none';
    modelCtxEl.innerHTML = '';
    _renderModelBtn();
    _scheduleFooterFit();
  }
}

/** Label the dedicated model button with the active model's short name (the part
 *  after the last "/", e.g. "deepseek/deepseek-v4-flash" → "deepseek-v4-flash").
 *  Falls back to "Model" when none is resolved. */
function _renderModelBtn() {
  if (!modelBtnEl) return;
  const name = _currentModelName || '';
  const short = name.includes('/') ? name.slice(name.lastIndexOf('/') + 1) : name;
  modelBtnEl.textContent = short || 'Model';
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
  // Restore token counters from localStorage cache instantly, then reconcile
  // with the authoritative backend total.
  _loadTokenCache();
  _fetchSessionCost();
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
  modelCtxEl.title = `${_currentModelName || '??'} — ctx ${(_contextTokens || 0).toLocaleString()} / max ${max ? max.toLocaleString() : '?'} — click to switch model`;
  modelCtxEl.style.display = '';
  // Keep the open Claude panel's context section in step with live updates.
  if (_claudePanelEl && _claudePanelEl.style.display !== 'none') _renderClaudeContext();
  _scheduleFooterFit();
}

// ── Footer stats fit (FOOTER-STATS-FIT) ─────────────────────────────────────
// Priority ladder for the left stats pill, LEAST → MOST useful. Each entry is a
// class on #chat-footer-row that hides one piece (see app1.css). _fitFooterStats
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

// ── Model picker (click ctx indicator) ───────────────────────────────────────

let _modelPickerEl = null;       // the floating dropdown container
let _modelPickerInput = null;    // search input inside the picker
let _modelPickerList = null;     // scrollable list inside the picker
let _modelPickerDetail = null;   // detail footer (description + cost)
let _allAvailableModels = [];    // fetched model list
let _currentModelName = '';      // the currently-selected model id (for highlight)
let _sessionModelUsage = {};     // { [modelId]: {input, output, total} } for this session
let _modelEffortMap = {};        // { [modelId]: 'minimal'|'low'|'medium'|'high' } per-session reasoning effort

// Reasoning-effort scale offered per model row. 'default' = send no hint (the
// model's own default). The rest map to the provider's normalised
// reasoning.effort levels; an unsupported level is dropped server-side. Keep in
// sync with REASONING_EFFORT_LEVELS in app/admin/settings.py.
const _EFFORT_LEVELS = [
  { v: 'default', l: 'Def', t: 'Default — let the model decide (no reasoning hint)' },
  { v: 'minimal', l: 'Min', t: 'Minimal reasoning — fastest, cheapest' },
  { v: 'low', l: 'Low', t: 'Low reasoning effort' },
  { v: 'medium', l: 'Med', t: 'Medium reasoning effort' },
  { v: 'high', l: 'High', t: 'High reasoning effort — deepest thinking, slowest' },
];

function _buildModelPicker() {
  if (_modelPickerEl) return;
  const picker = document.createElement('div');
  picker.className = 'chat-model-picker';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'cmp-search';
  input.placeholder = 'Search models…';
  input.autocomplete = 'off';
  const list = document.createElement('div');
  list.className = 'cmp-list';
  const detail = document.createElement('div');
  detail.className = 'cmp-detail';
  picker.appendChild(input);
  picker.appendChild(list);
  picker.appendChild(detail);
  document.body.appendChild(picker);
  _modelPickerEl = picker;
  _modelPickerInput = input;
  _modelPickerList = list;
  _modelPickerDetail = detail;

  // Search filtering
  input.addEventListener('input', () => _renderModelPickerList(input.value.toLowerCase()));

  // Close on click outside
  document.addEventListener('click', (e) => {
    if (_modelPickerEl && !_modelPickerEl.contains(e.target)
        && e.target !== footerLeftEl && !(footerLeftEl && footerLeftEl.contains(e.target))
        && e.target !== modelBtnEl && !(modelBtnEl && modelBtnEl.contains(e.target))) {
      _modelPickerEl.style.display = 'none';
    }
  });

  // Close on Escape
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { _modelPickerEl.style.display = 'none'; input.blur(); }
    if (e.key === 'Enter' && input.value.trim()) {
      // Select first visible item
      const first = _modelPickerList.querySelector('.cmp-item');
      if (first) first.click();
    }
  });
}

function _escHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

/** Compact token count: 0 / 950 / 12.3K / 1.05M. */
function _fmtTok(n) {
  if (!n || typeof n !== 'number') return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return `${n}`;
}

/** Format a per-1M-token USD cost into a chip label, or null if unknown. */
function _fmtCostChip(label, v) {
  if (v === 0) return { text: `${label} free`, free: true };
  if (typeof v === 'number') return { text: `${label} $${v}/1M`, free: false };
  return null;
}

/** Render the detail footer (description + cost) for one model. */
function _renderModelDetail(m) {
  if (!_modelPickerDetail) return;
  if (!m) { _modelPickerDetail.innerHTML = ''; return; }
  const desc = m.description
    ? `<div class="cmp-detail-desc">${_escHtml(m.description)}</div>`
    : `<div class="cmp-detail-desc cmp-detail-muted">No description available.</div>`;
  const chips = [
    _fmtCostChip('in', m.cost_input),
    _fmtCostChip('out', m.cost_output),
  ].filter(Boolean)
    .map(c => `<span class="cmp-cost-chip${c.free ? ' cmp-cost-free' : ''}">${_escHtml(c.text)}</span>`)
    .join('');
  const cost = chips
    ? `<div class="cmp-detail-cost">${chips}</div>`
    : `<div class="cmp-detail-cost"><span class="cmp-detail-muted">Cost unavailable</span></div>`;
  _modelPickerDetail.innerHTML =
    `<div class="cmp-detail-name">${_escHtml(m.id)}</div>${desc}${cost}`;
}

// Click a row → save with a spinner, then confirm with a green check (or a red
// cross on failure). The picker stays open showing the result, then closes.
let _pickerSaving = false;
async function _onPickModel(item, modelId) {
  if (_pickerSaving) return;                       // ignore clicks mid-save
  _pickerSaving = true;
  const status = item.querySelector('.cmp-item-status');
  item.classList.add('cmp-saving');
  if (status) status.innerHTML = '<span class="cmp-spinner"></span>';

  // Keep the spinner visible for a beat even on an instant (localhost) save, so
  // the save→confirm transition reads as intentional rather than a flash.
  const [ok] = await Promise.all([
    _selectModel(modelId),
    new Promise(r => setTimeout(r, 320)),
  ]);

  item.classList.remove('cmp-saving');
  if (status) status.innerHTML = ok
    ? '<span class="cmp-check" title="Saved">✓</span>'
    : '<span class="cmp-cross" title="Save failed">✕</span>';

  if (ok) {
    _modelPickerList.querySelectorAll('.cmp-item.cmp-selected')
      .forEach(el => el.classList.remove('cmp-selected'));
    item.classList.add('cmp-selected');
    setTimeout(() => {
      if (_modelPickerEl) _modelPickerEl.style.display = 'none';
      if (_modelPickerInput) _modelPickerInput.value = '';
    }, 650);
  }
  _pickerSaving = false;
}

// Click an effort chip → save it for that model (per session), then reflect the
// new selection on the row. Independent of the model-select flow.
let _effortSaving = false;
async function _onPickEffort(item, modelId, level) {
  if (_effortSaving) return;
  _effortSaving = true;
  const row = item.querySelector('.cmp-effort');
  if (row) row.classList.add('cmp-eff-saving');
  const ok = await _saveModelEffort(modelId, level);
  if (ok) {
    if (level && level !== 'default') _modelEffortMap[modelId] = level;
    else delete _modelEffortMap[modelId];
    // Re-highlight the chosen chip on this row.
    item.querySelectorAll('.cmp-eff-btn').forEach(b =>
      b.classList.toggle('cmp-eff-on', b.dataset.effort === (level || 'default')));
    // If this is the active model, refresh the footer (effort can affect nothing
    // visible yet, but keeps state consistent).
    if (modelId === _currentModelName) refreshModelContext();
  }
  if (row) row.classList.remove('cmp-eff-saving');
  _effortSaving = false;
}

/** Persist a per-model reasoning-effort level for this session. Returns true on a
 *  confirmed save. No-op (false) outside a chat session. */
async function _saveModelEffort(modelId, level) {
  const sid = app.currentSessionId || '';
  if (!sid) return false;
  try {
    const headers = { 'Content-Type': 'application/json', ...authHeaders() };
    const res = await fetch(apiPath('/api/v1/chat/session-model-effort'), {
      method: 'POST', headers,
      body: JSON.stringify({
        user_id: app.currentUserId || '',
        session_id: sid,
        model: modelId,
        reasoning_effort: level || 'default',
      }),
    });
    return res.ok;
  } catch (e) {
    console.warn('Failed to set model effort:', e);
    return false;
  }
}

function _renderModelPickerList(filter) {
  if (!_modelPickerList) return;
  const filtered = filter
    ? _allAvailableModels.filter(m => m.id.toLowerCase().includes(filter) || (m.name || '').toLowerCase().includes(filter))
    : _allAvailableModels;
  _modelPickerList.innerHTML = '';
  if (!filtered.length) {
    _modelPickerList.innerHTML = '<div class="cmp-empty">No models match</div>';
    _renderModelDetail(null);
    return;
  }
  filtered.slice(0, 200).forEach(m => {
    const item = document.createElement('div');
    item.className = 'cmp-item' + (m.id === _currentModelName ? ' cmp-selected' : '');
    item.dataset.modelId = m.id;
    const ctxStr = m.context ? _fmtCtxNum(m.context) : '';
    const u = _sessionModelUsage[m.id];
    const tokStr = `${_fmtTok(u ? u.total : 0)} tok`;
    // This model's own cost in the current session (priced at its own rate).
    const costStr = (u && typeof u.cost_usd === 'number' && u.cost_usd > 0)
      ? _fmtCost(u.cost_usd) : '';
    // Effort selector — one chip per level. Shown unless the model is KNOWN not
    // to support reasoning (reasoning === false); unknown/true both show it (an
    // unsupported hint is harmless, dropped server-side). Reflects this model's
    // saved level (default when none).
    const curLevel = (_modelEffortMap[m.id] || 'default');
    const effortHtml = (m.reasoning === false) ? '' :
      `<div class="cmp-effort" title="Reasoning effort for this model">`
      + _EFFORT_LEVELS.map(e =>
          `<button type="button" class="cmp-eff-btn${e.v === curLevel ? ' cmp-eff-on' : ''}"`
          + ` data-effort="${e.v}" title="${_escHtml(e.t)}">${e.l}</button>`).join('')
      + `</div>`;
    item.innerHTML = `<span class="cmp-item-id">${_escHtml(m.id)}</span>`
      + `<span class="cmp-item-meta">`
      + `<span class="cmp-item-tok" title="Tokens used by this model in the current session">${tokStr}</span>`
      + (costStr ? `<span class="cmp-item-cost" title="This model's cost in the current session">${costStr}</span>` : '')
      + (ctxStr ? `<span class="cmp-item-ctx">ctx ${ctxStr}</span>` : '')
      + `<span class="cmp-item-status"></span>`
      + `</span>`
      + effortHtml;
    item.addEventListener('click', (e) => {
      // Effort chips manage their own click; don't switch model when one is hit.
      if (e.target.closest('.cmp-effort')) return;
      _onPickModel(item, m.id);
    });
    item.addEventListener('mouseenter', () => _renderModelDetail(m));
    item.querySelectorAll('.cmp-eff-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        _onPickEffort(item, m.id, btn.dataset.effort);
      });
    });
    _modelPickerList.appendChild(item);
  });
  // Default the detail to the selected model (or the first row).
  const def = filtered.find(m => m.id === _currentModelName) || filtered[0];
  _renderModelDetail(def);
}

/** Build the curated model list for the footer picker: ALL of the agent's
 *  Text-capable models — the union of the app default(s) + the agent's OWN saved
 *  roster, resolved server-side by /admin/settings/agent-models exactly as the
 *  run resolves it. Falls back to the admin defaults (/multi-providers) when
 *  there's no agent/session. NOT the full provider catalog. */
async function _fetchModelsForPicker() {
  const headers = { ...authHeaders() };

  const aid = app.currentAgentId || '';
  const sid = app.currentSessionId || '';
  const ids = new Set();

  // Per-agent candidate list (app default(s) + the agent's own roster, Text-only).
  if (aid || sid) {
    try {
      const qs = new URLSearchParams();
      if (aid) qs.set('agent_id', aid);
      if (sid) qs.set('session_id', sid);
      const res = await fetch(apiPath('/admin/settings/agent-models?' + qs.toString()), { headers });
      if (res.ok) {
        const data = await res.json();
        (data.models || []).forEach(m => { if (m && m.id) ids.add(m.id); });
        _modelEffortMap = (data.model_effort && typeof data.model_effort === 'object')
          ? data.model_effort : {};
      }
    } catch (_) { /* best-effort */ }
  }

  // Fallback: the app-default saved models (Text-ticked) when no per-agent list.
  if (!ids.size) {
    try {
      const res = await fetch(apiPath('/admin/settings/multi-providers'), { headers });
      if (res.ok) {
        const data = await res.json();
        (data.providers || []).forEach(p => {
          if (p && p.model && p.enabled !== false) ids.add(p.model);
        });
      }
    } catch (_) { /* best-effort */ }
  }

  _allAvailableModels = Array.from(ids)
    .sort((a, b) => a.localeCompare(b))
    .map(id => ({ id, name: id, context: null }));
}

/** Fill in each curated model's context window, description and cost from the
 *  lightweight per-model metadata endpoint (best-effort, in parallel). The list
 *  renders before this resolves, so missing data just shows fewer chips. */
async function _enrichPickerContext() {
  const headers = { ...authHeaders() };
  await Promise.all(_allAvailableModels.map(async (m) => {
    if (m._enriched) return;
    try {
      const res = await fetch(apiPath(`/admin/settings/model-info?model=${encodeURIComponent(m.id)}`), { headers });
      if (res.ok) {
        const d = await res.json();
        const info = (d && d.info) || {};
        if (info.context) m.context = info.context;
        if (info.description) m.description = info.description;
        if (info.cost_input != null) m.cost_input = info.cost_input;
        if (info.cost_output != null) m.cost_output = info.cost_output;
        if (info.reasoning != null) m.reasoning = info.reasoning;
        m._enriched = true;
      }
    } catch (_) { /* best-effort */ }
  }));
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

/** Fetch this session's per-model token totals (best-effort). */
async function _fetchSessionModelUsage() {
  _sessionModelUsage = {};
  const sid = app.currentSessionId || '';
  if (!sid) return;
  const headers = { ...authHeaders() };
  try {
    const res = await fetch(apiPath(`/admin/settings/session-model-usage?session_id=${encodeURIComponent(sid)}`), { headers });
    if (res.ok) {
      const d = await res.json();
      _sessionModelUsage = (d && d.usage) || {};
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
async function _selectModel(modelId) {
  try {
    const headers = { 'Content-Type': 'application/json', ...authHeaders() };
    const aid = app.currentAgentId || '';
    const sid = app.currentSessionId || '';
    if (sid) {
      // Chat session — save as a per-session override (takes effect next turn).
      const postRes = await fetch(apiPath('/api/v1/chat/session-model'), {
        method: 'POST', headers,
        body: JSON.stringify({
          user_id: app.currentUserId || '',
          session_id: sid,
          model: modelId,
        }),
      });
      if (!postRes.ok) return false;
    } else if (aid) {
      // Agent context — save as agent's llm_config override. Read the EXPOSED
      // top-level `llm_config` (the API hides `metadata`), so we preserve any
      // provider/base_url/api_key the agent already set and only swap the model.
      // GET /agents/{id} requires user_id as a query param (else 422).
      const uq = `?user_id=${encodeURIComponent(app.currentUserId || '')}`;
      const getRes = await fetch(apiPath(`/api/v1/agents/${encodeURIComponent(aid)}${uq}`), { headers });
      if (!getRes.ok) return false;
      const agent = await getRes.json();
      const existing = (agent.agent && agent.agent.llm_config) || {};
      const llmCfg = { ...existing, model: modelId, use_default: false };
      const putRes = await fetch(apiPath(`/api/v1/agents/${encodeURIComponent(aid)}`), {
        method: 'PUT', headers,
        body: JSON.stringify({ user_id: app.currentUserId || '', llm_config: llmCfg }),
      });
      if (!putRes.ok) return false;
    } else {
      // No agent — fetch current provider config, update just the model field
      const getRes = await fetch(apiPath('/admin/settings/provider'), { headers });
      if (!getRes.ok) return false;
      const cfg = await getRes.json();
      const postRes = await fetch(apiPath('/admin/settings/provider'), {
        method: 'POST', headers,
        body: JSON.stringify({
          provider: cfg.provider,
          base_url: cfg.base_url || '',
          api_key: cfg.api_key || '',
          model: modelId,
        }),
      });
      if (!postRes.ok) return false;
    }
    _currentModelName = modelId;
    refreshModelContext();
    return true;
  } catch (e) {
    console.warn('Failed to switch model:', e);
    return false;
  }
}

// Toggle the picker open/close when clicking the footer left area
function _toggleModelPicker() {
  if (_modelPickerEl && _modelPickerEl.style.display !== 'none') {
    _modelPickerEl.style.display = 'none';
    return;
  }
  _buildModelPicker();

  // Vertical anchor: pin the panel's bottom edge just above the footer row, so it
  // grows upward from there (prefer the dedicated model button, then the ctx chip).
  const vAnchor = modelBtnEl
    || (modelCtxEl && modelCtxEl.style.display !== 'none' ? modelCtxEl : null)
    || footerLeftEl
    || document.getElementById('chat-footer-row')
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

  // Show loading
  _modelPickerList.innerHTML = '<div class="cmp-empty">Loading models…</div>';
  _modelPickerInput.value = '';
  _modelPickerInput.focus();

  // Per-session per-model token totals (independent of the model list).
  _fetchSessionModelUsage().then(() => {
    _renderModelPickerList(_modelPickerInput.value.toLowerCase());
  });

  // Fetch the curated list, render it, then fill in context/description/cost in
  // the background and re-render so each row shows its chips + detail.
  _fetchModelsForPicker().then(() => {
    _renderModelPickerList(_modelPickerInput.value.toLowerCase());
    _enrichPickerContext().then(() => {
      _renderModelPickerList(_modelPickerInput.value.toLowerCase());
    });
  });
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

// The choices the local `claude` accepts via --model. Blank = the CLI's own default.
const _CLAUDE_MODELS = [
  { v: '', label: 'Default', sub: "Claude's own default" },
  { v: 'opus', label: 'Opus', sub: 'Most capable' },
  { v: 'sonnet', label: 'Sonnet', sub: 'Balanced' },
  { v: 'haiku', label: 'Haiku', sub: 'Fastest' },
];

/** Which preset row a configured model id maps to ('' = Default, null = a custom
 *  id that matches no preset so nothing is ticked). */
function _claudeAliasOf(model) {
  const m = (model || '').toLowerCase();
  if (!m) return '';
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
    </div>`;
  document.body.appendChild(p);
  _claudePanelEl = p;

  document.addEventListener('click', (e) => {
    if (_claudePanelEl && _claudePanelEl.style.display !== 'none'
        && !_claudePanelEl.contains(e.target)
        && e.target !== footerLeftEl && !(footerLeftEl && footerLeftEl.contains(e.target))
        && e.target !== modelBtnEl && !(modelBtnEl && modelBtnEl.contains(e.target))
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

/** Load the model rows, ticking whichever preset the agent is configured to use. */
async function _loadClaudeModels() {
  const list = _claudePanelEl && _claudePanelEl.querySelector('.ccp-model-list');
  if (!list) return;
  const aid = app.currentAgentId || '';
  let current = '';
  try {
    const uq = `?user_id=${encodeURIComponent(app.currentUserId || '')}`;
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(aid)}${uq}`, { headers: { ...authHeaders() } });
    if (res.ok) {
      const a = await res.json();
      current = (((a.agent || a).claude_code || {}).model || '').trim();
    }
  } catch (_) { /* best-effort */ }
  const activeAlias = _claudeAliasOf(current);
  list.innerHTML = '';
  _CLAUDE_MODELS.forEach(m => {
    const row = document.createElement('div');
    row.className = 'cmp-item ccp-model-row' + (m.v === activeAlias ? ' cmp-selected' : '');
    row.innerHTML = `<span class="ccp-m-main"><span class="ccp-m-label">${_escHtml(m.label)}</span>`
      + `<span class="ccp-m-sub">${_escHtml(m.sub)}</span></span><span class="cmp-item-status"></span>`;
    row.addEventListener('click', () => _onPickClaudeModel(row, m.v));
    list.appendChild(row);
  });
}

async function _saveClaudeModel(value) {
  const aid = app.currentAgentId || '';
  if (!aid) return false;
  try {
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(aid)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: app.currentUserId || '', claude_code: { model: value } }),
    });
    return res.ok;
  } catch (_) { return false; }
}

async function _onPickClaudeModel(row, value) {
  if (_claudeModelSaving) return;
  _claudeModelSaving = true;
  const status = row.querySelector('.cmp-item-status');
  if (status) status.innerHTML = '<span class="cmp-spinner"></span>';
  const [ok] = await Promise.all([_saveClaudeModel(value), new Promise(r => setTimeout(r, 280))]);
  if (status) status.innerHTML = ok
    ? '<span class="cmp-check" title="Saved">✓</span>'
    : '<span class="cmp-cross" title="Save failed">✕</span>';
  if (ok) {
    _claudePanelEl.querySelectorAll('.ccp-model-row.cmp-selected').forEach(el => el.classList.remove('cmp-selected'));
    row.classList.add('cmp-selected');
    refreshModelContext();   // footer name + window follow the new model
    setTimeout(() => { if (status) status.innerHTML = ''; }, 900);
  }
  _claudeModelSaving = false;
}

function _toggleClaudePanel() {
  if (_claudePanelEl && _claudePanelEl.style.display !== 'none') {
    _claudePanelEl.style.display = 'none';
    return;
  }
  _buildClaudePanel();
  // Anchor identically to the model picker: bottom-pinned above the footer,
  // left-aligned to the chat pill.
  const vAnchor = modelBtnEl
    || (modelCtxEl && modelCtxEl.style.display !== 'none' ? modelCtxEl : null)
    || footerLeftEl
    || document.getElementById('chat-footer-row')
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
  _loadClaudeModels();
  _loadClaudeUsage();
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

function setNote(text) {
  // Dedupe identical notes — this also tames the per-chunk spam from `stream`
  // events, which would otherwise re-fire on every token.
  if (!text || text === currentNote) return;
  currentNote = text;
  if (textEl) textEl.textContent = text;
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
  app._turnHasBubble = false;
  closePanel();
  renderPanel();
  _updateBarAffordance();
}

// Switch the indicator into its live "working" look.
function _activate() {
  active = true;
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
  if (!active && !resting) return;
  active = false;
  if (pillEl) pillEl.classList.remove('thinking');
  _setSpinnerDir(null);

  // Attach any remaining tool calls from the last turn (if not already
  // attached by a turn_start boundary). Each turn's calls are attached
  // incrementally as turns progress, so this only catches the final turn.
  if (toolCalls.length > 0 && app.attachToolCallsToLastBubble) {
    const calls = toolCalls.slice();
    app.attachToolCallsToLastBubble(calls);
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
  resting = false;
  _turnEnded = false;
  toolCalls = [];
  currentTurn = 0;
  currentNote = '';
  app._turnHasBubble = false;
  closePanel();
  renderPanel();
  _updateBarAffordance();
  if (pillEl) pillEl.classList.remove('thinking');
  if (rootEl) rootEl.classList.remove('visible', 'resting');
  if (textEl) textEl.textContent = '';
  resetTokens();
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

function addToolCall(tool, args) {
  toolCalls.push({
    tool: tool || 'tool',
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

function resolveToolResult(tool, result, durationMs, isError, errorType) {
  // Pair with the oldest still-running call of the same name (no call-id in the
  // event stream, so order is the best we have).
  let entry = null;
  for (let i = 0; i < toolCalls.length; i++) {
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

function _buildRow(entry, idx) {
  const row = document.createElement('div');
  row.className = 'ca-tool-row' + (entry.open ? ' open' : '');
  row.dataset.i = String(idx);

  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'ca-tool-head';
  head.setAttribute('aria-expanded', entry.open ? 'true' : 'false');

  const status = document.createElement('span');
  status.className = 'ca-tool-status ca-status-' + entry.status;
  status.textContent = entry.status === 'done' ? '✓' : entry.status === 'error' ? '✕' : '';
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
  if (entry.status === 'running') meta.textContent = 'running…';
  else if (entry.status === 'error') meta.textContent = entry.errorType || 'error';
  else meta.textContent = (entry.durationMs != null) ? entry.durationMs + 'ms' : 'done';
  head.appendChild(meta);

  // Copy-all button for the whole tool call
  const fullText = 'Tool: ' + entry.tool + '\nArguments:\n' + _fmtArgs(entry.args) + '\nResult:\n' + (entry.result || '(empty)');
  const copyAllBtn = _makeCopyBtn(fullText, 'tool call');
  head.appendChild(copyAllBtn);

  const caret = document.createElement('span');
  caret.className = 'ca-tool-caret';
  caret.setAttribute('aria-hidden', 'true');
  caret.textContent = '›';
  head.appendChild(caret);

  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ca-tool-body';

  // ── Saved input (full messages sent to LLM) ──
  if (entry._savedInput) {
    const inputLbl = document.createElement('div');
    inputLbl.className = 'ca-tool-label';
    inputLbl.textContent = 'LLM Input (messages)';
    body.appendChild(inputLbl);
    const inputWrap = document.createElement('div');
    inputWrap.className = 'ca-tool-pre-wrap';
    const inputPre = document.createElement('pre');
    inputPre.className = 'ca-tool-pre';
    let inputText = entry._savedInput;
    try { inputText = JSON.stringify(JSON.parse(entry._savedInput), null, 2); } catch (_) {}
    inputPre.textContent = inputText;
    inputWrap.appendChild(inputPre);
    inputWrap.appendChild(_makeCopyBtn(inputText, 'LLM input'));
    body.appendChild(inputWrap);
  }

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

  _outsideHandler = (e) => { if (rootEl && !rootEl.contains(e.target)) closePanel(); };
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
      case 'memory_search_start':       return 'Searching memory';
      case 'build_prompt':              return 'Preparing';
      case 'attachment':                return 'Reading attachment';
      case 'attachment_describe_start': return 'Looking at image';
      case 'load_tools':                return 'Loading tools';
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
      app.attachToolCallsToLastBubble(calls);
    }
    _resetForNewTurn();
  }

  const type = event.type;

  // Terminal events end the turn. Mark _turnEnded so anything the backend emits
  // AFTER this (background memory save, turn hooks, …) is treated as post-turn
  // housekeeping that self-clears, rather than re-lighting the bar indefinitely.
  if (type === 'response')    { _turnEnded = true; stop(); return; }
  if (type === 'error')       { _turnEnded = true; if (active) setNote('Error');   _endSoon(); return; }
  if (type === 'interrupted') { _turnEnded = true; if (active) setNote('Stopped'); _endSoon(); return; }

  // In-turn signals can never be post-turn housekeeping. If one arrives while we
  // still think the turn ended — e.g. an event/automation run that reused the
  // prior turn number and emitted no fresh user_message — we're actually in a new
  // live turn, so drop settle mode and let it claim the full in-turn watchdog.
  if (type === 'stream'
      || (type === 'pipeline' && (event.step === 'turn_start' || event.step === 'llm_call_start'))) {
    _turnEnded = false;
  }

  // A user message is the authoritative new-turn boundary (covers fresh sends,
  // interrupt-and-replace, and event-triggered runs) — always reset the list.
  if (type === 'user_message') {
    _clearEndTimer();
    _resetForNewTurn();
    _turnEnded = false;       // fresh exchange — out of post-turn settle mode
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
    addToolCall(event.tool, event.args);
    return;
  }
  if (type === 'tool_result') {
    // Synthetic tools that DO emit a tool_result (vision, memory_search) render
    // their own standalone bubble; args come from whatever was stashed at start.
    if (_SYNTH_TOOLS[event.tool]) {
      _ensureActive(_turnPrefix() + (event.error ? 'Error ' : 'Done ') + (event.tool || 'image'));
      _renderSynthToolBubble(event.tool, {
        args: _pendingSynthArgs[event.tool], result: event.result,
        error: !!event.error, durationMs: event.duration_ms,
        errorType: event.error_type, id: event.id,
      });
      return;
    }
    _ensureActive(_turnPrefix() + (event.error ? 'Error ' : 'Done ') + (event.tool || 'tool'));
    resolveToolResult(event.tool, event.result, event.duration_ms, !!event.error, event.error_type);
    _maybeShowVaultCard(event);
    return;
  }

  // Live estimate: while the LLM streams content, count output tokens in real-time
  if (type === 'stream' && typeof event.content === 'string') {
    _onStreamContent(event.content);
  }

  // Capture token usage from pipeline llm_call_end events
  if (type === 'pipeline' && event.step === 'llm_call_end') {
    if (typeof event.input_tokens === 'number' || typeof event.output_tokens === 'number') {
      addTokens(event.input_tokens || 0, event.output_tokens || 0, event.cost_usd);
    }
    // Remember which model/effort ran this turn so the bubble footer can tag it
    // the moment the live answer finalizes (chat-stream.js reads these).
    if (event.model) app._activeTurnModel = event.model;
    app._activeTurnEffort = event.effort || null;
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
  // pipeline lifecycle steps. memory_search renders on its tool_result (above) —
  // here we just stash its query from the start step. memory_save has no
  // tool_result at all, so render its standalone bubble right here from the args +
  // result the backend attached to memory_save_end, then fall through to the
  // completion-settle handler below (which clears the "Saving memory" chip).
  if (type === 'pipeline') {
    if (event.step === 'memory_search_start') {
      _pendingSynthArgs['memory_search'] = { query: event.query, limit: event.limit };
    } else if (event.step === 'memory_save_end') {
      _renderSynthToolBubble('memory_save', {
        args: event.args, result: event.result,
        error: event.ok === false, id: event.slug,
      });
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
  _ensureActive(note);
}

// ── Exported helpers for chat.js (bubble-attached tool panels) ──────────────
// Reuse the same accordion row rendering so bubble panels look identical to the
// live activity panel.

export { _buildRow as buildToolRow };

export function initChatActivity() {
  rootEl = document.getElementById('chat-activity');
  pillEl = document.getElementById('chat-input-row');
  barEl = document.getElementById('chat-activity-bar');
  textEl = rootEl ? rootEl.querySelector('.chat-activity-text') : null;
  panelEl = document.getElementById('chat-activity-panel');
  tokenBarEl = document.getElementById('chat-token-bar');
  tokensInEl = document.getElementById('chat-tokens-in');
  tokensOutEl = document.getElementById('chat-tokens-out');
  tokenSpinnerEl = document.getElementById('chat-token-spinner');
  footerLeftEl = document.querySelector('.chat-footer-left');
  footerRowEl = document.getElementById('chat-footer-row');
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

  if (barEl) barEl.addEventListener('click', togglePanel);

  // Show the active model's context window / max output next to the counters.
  // Re-callable (exposed below) so Settings can refresh it after a model change.
  refreshModelContext();

  // Click footer left (token bar + ctx) to open the picker. Local Claude Code
  // agents get their own panel (context + plan usage + Claude model), since the
  // app's model list doesn't apply to them.
  function _onFooterClick(e) {
    e.stopPropagation();
    if (_altEngine) _toggleClaudePanel();
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
  // Let sessions.js update the ctx indicator after loading message history.
  app.setContextFromMessages = setContextFromMessages;
  app.setContextTokens = setContextTokens;
}
