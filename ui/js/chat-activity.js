'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { copyText } from './clipboard.js';

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
let modelCtxEl = null;   // #chat-model-ctx (live context tokens / model's max limit)
let _contextTokens = 0;  // live assembled context tokens (from context_status events)
let _modelContextLimit = null; // model's context window limit (from current-model-info)
let cumulativeIn = 0;
let cumulativeOut = 0;
let _streamCharCount = 0;     // chars streamed in current ongoing LLM call
let _pendingOutEstimate = 0;  // estimated output tokens for current streaming call
let _thinkingRamp = null;     // interval handle for the pre-stream thinking ramp

let active = false;     // a turn is in progress
let resting = false;    // turn ended but tool calls remain to inspect
let expanded = false;   // panel open
let currentNote = '';
let clearTimer = null;  // delayed text-clear after a no-tools fade-out
let endTimer = null;    // delayed stop() after a terminal Error/Stopped note
let watchdog = null;    // safety auto-stop if a turn never reports completion

// One entry per tool call this turn: { tool, args, status, result, durationMs,
// errorType, turn, open }. status: 'running' | 'done' | 'error'.
let toolCalls = [];

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

function _clearTextTimer() { if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; } }
function _clearEndTimer()  { if (endTimer)  { clearTimeout(endTimer);  endTimer  = null; } }
function _armWatchdog() {
  if (watchdog) clearTimeout(watchdog);
  watchdog = setTimeout(() => { watchdog = null; stop(); }, WATCHDOG_MS);
}
function _disarmWatchdog() { if (watchdog) { clearTimeout(watchdog); watchdog = null; } }

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
  }, 800);
}

function _stopThinkingRamp() {
  if (_thinkingRamp) {
    clearInterval(_thinkingRamp);
    _thinkingRamp = null;
  }
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

function addTokens(inputTokens, outputTokens) {
  // Real tokens arrived — stop the thinking ramp
  _stopThinkingRamp();
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
}

// ── Active-model context indicator (next to in/out counters) ────────────────

function _fmtCtxNum(n) {
  if (!n || typeof n !== 'number') return '';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}K`;
  return `${n}`;
}

/** Fetch the active model's context window / max output and show it in the
 *  footer. Resolves the user's effective model server-side (one call). Silent
 *  on failure — the indicator just stays hidden. */
async function refreshModelContext() {
  if (!modelCtxEl) return;
  try {
    const headers = {};
    const tok = localStorage.getItem('auth_token');
    if (tok) headers.Authorization = `Bearer ${tok}`;
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
    _renderCtxIndicator();
  } catch (e) {
    modelCtxEl.style.display = 'none';
    modelCtxEl.innerHTML = '';
  }
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
  _renderCtxIndicator();
}

/** Render the ctx indicator: live context tokens / model's max context limit */
function _renderCtxIndicator() {
  if (!modelCtxEl) return;
  const ctx = _contextTokens || 0;
  const max = _modelContextLimit;
  if (!max && !ctx) { modelCtxEl.style.display = 'none'; modelCtxEl.innerHTML = ''; return; }
  modelCtxEl.innerHTML = `<span class="chat-token-label">ctx</span> ${_fmtCtxNum(ctx)} <span class="chat-ctx-sep">/</span> ${_fmtCtxNum(max)}`;
  modelCtxEl.title = `${_currentModelName || '??'} — ctx ${ctx.toLocaleString()} / max ${max ? max.toLocaleString() : '?'} — click to switch model`;
  modelCtxEl.style.display = '';
}

// ── Model picker (click ctx indicator) ───────────────────────────────────────

let _modelPickerEl = null;       // the floating dropdown container
let _modelPickerInput = null;    // search input inside the picker
let _modelPickerList = null;     // scrollable list inside the picker
let _modelPickerDetail = null;   // detail footer (description + cost)
let _allAvailableModels = [];    // fetched model list
let _currentModelName = '';      // the currently-selected model id (for highlight)
let _sessionModelUsage = {};     // { [modelId]: {input, output, total} } for this session

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
    if (_modelPickerEl && !_modelPickerEl.contains(e.target) && e.target !== footerLeftEl && !(footerLeftEl && footerLeftEl.contains(e.target))) {
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
    item.innerHTML = `<span class="cmp-item-id">${_escHtml(m.id)}</span>`
      + `<span class="cmp-item-meta">`
      + `<span class="cmp-item-tok" title="Tokens used by this model in the current session">${tokStr}</span>`
      + (ctxStr ? `<span class="cmp-item-ctx">ctx ${ctxStr}</span>` : '')
      + `<span class="cmp-item-status"></span>`
      + `</span>`;
    item.addEventListener('click', () => _onPickModel(item, m.id));
    item.addEventListener('mouseenter', () => _renderModelDetail(m));
    _modelPickerList.appendChild(item);
  });
  // Default the detail to the selected model (or the first row).
  const def = filtered.find(m => m.id === _currentModelName) || filtered[0];
  _renderModelDetail(def);
}

/** Build the curated model list for the footer picker: the enabled models saved
 *  in App Config → Agent Settings (the multi-providers list whose "Text" box is
 *  ticked). NOT the full provider catalog, and NOT the agent's stale model. */
async function _fetchModelsForPicker() {
  const headers = {};
  const tok = localStorage.getItem('auth_token');
  if (tok) headers.Authorization = `Bearer ${tok}`;

  const ids = new Set();

  // App Config → Agent Settings → saved models (the multi-providers list).
  // Mirror the saved-models table exactly: only models whose "Text" box is
  // ticked (i.e. enabled !== false) are selectable here. We deliberately do NOT
  // force-add the agent's configured model or the currently-active model — that
  // used to leak a stale, no-longer-saved model into the picker. If the active
  // model isn't in the enabled list, nothing is highlighted, which is correct.
  try {
    const res = await fetch(apiPath('/admin/settings/multi-providers'), { headers });
    if (res.ok) {
      const data = await res.json();
      (data.providers || []).forEach(p => {
        if (p && p.model && p.enabled !== false) ids.add(p.model);
      });
    }
  } catch (_) { /* best-effort */ }

  _allAvailableModels = Array.from(ids)
    .sort((a, b) => a.localeCompare(b))
    .map(id => ({ id, name: id, context: null }));
}

/** Fill in each curated model's context window, description and cost from the
 *  lightweight per-model metadata endpoint (best-effort, in parallel). The list
 *  renders before this resolves, so missing data just shows fewer chips. */
async function _enrichPickerContext() {
  const headers = {};
  const tok = localStorage.getItem('auth_token');
  if (tok) headers.Authorization = `Bearer ${tok}`;
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
        m._enriched = true;
      }
    } catch (_) { /* best-effort */ }
  }));
}

/** Fetch this session's per-model token totals (best-effort). */
async function _fetchSessionModelUsage() {
  _sessionModelUsage = {};
  const sid = app.currentSessionId || '';
  if (!sid) return;
  const headers = {};
  const tok = localStorage.getItem('auth_token');
  if (tok) headers.Authorization = `Bearer ${tok}`;
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
    const headers = { 'Content-Type': 'application/json' };
    const tok = localStorage.getItem('auth_token');
    if (tok) headers.Authorization = `Bearer ${tok}`;
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

  // Vertical anchor: pin the panel's bottom edge just above the context-details
  // line in the footer, so it grows upward from there.
  const vAnchor = (modelCtxEl && modelCtxEl.style.display !== 'none' ? modelCtxEl : null)
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

function resetTokens() {
  cumulativeIn = 0;
  cumulativeOut = 0;
  _pendingOutEstimate = 0;
  _streamCharCount = 0;
  _setSpinnerDir(null);
  if (tokensInEl) tokensInEl.textContent = '0';
  if (tokensOutEl) tokensOutEl.textContent = '0';
  _updateTokenBar();
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
  closePanel();
  renderPanel();
  _updateBarAffordance();
}

// Switch the indicator into its live "working" look.
function _activate() {
  active = true;
  resting = false;
  if (rootEl) { rootEl.classList.remove('resting'); rootEl.classList.add('visible'); }
  if (pillEl) pillEl.classList.add('thinking');
  _startThinkingRamp();
  _updateTokenBar();
}

function start(initialNote) {
  _clearTextTimer();
  _clearEndTimer();
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
  toolCalls = [];
  currentTurn = 0;
  currentNote = '';
  closePanel();
  renderPanel();
  _updateBarAffordance();
  if (pillEl) pillEl.classList.remove('thinking');
  if (rootEl) rootEl.classList.remove('visible', 'resting');
  if (textEl) textEl.textContent = '';
  resetTokens();
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
  _armWatchdog();
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
    // If we had tool calls from the previous turn, attach them now.
    if (prevTurn > 0 && toolCalls.length > 0 && app.attachToolCallsToLastBubble) {
      const calls = toolCalls.slice();
      app.attachToolCallsToLastBubble(calls);
    }
    _resetForNewTurn();
  }

  const type = event.type;

  // Terminal events end the turn.
  if (type === 'response')    { stop(); return; }
  if (type === 'error')       { if (active) setNote('Error');   _endSoon(); return; }
  if (type === 'interrupted') { if (active) setNote('Stopped'); _endSoon(); return; }

  // A user message is the authoritative new-turn boundary (covers fresh sends,
  // interrupt-and-replace, and event-triggered runs) — always reset the list.
  if (type === 'user_message') {
    _clearEndTimer();
    _resetForNewTurn();
    currentTurn = 0;          // new exchange — turn_start will set it to 1
    _activate();
    _armWatchdog();
    setNote('Thinking…');
    return;
  }

  if (type === 'tool_call') {
    _ensureActive(_turnPrefix() + 'Toolcall ' + (event.tool || 'tool'));
    addToolCall(event.tool, event.args);
    return;
  }
  if (type === 'tool_result') {
    _ensureActive(_turnPrefix() + (event.error ? 'Error ' : 'Done ') + (event.tool || 'tool'));
    resolveToolResult(event.tool, event.result, event.duration_ms, !!event.error, event.error_type);
    return;
  }

  // Live estimate: while the LLM streams content, count output tokens in real-time
  if (type === 'stream' && typeof event.content === 'string') {
    _onStreamContent(event.content);
  }

  // Capture token usage from pipeline llm_call_end events
  if (type === 'pipeline' && event.step === 'llm_call_end') {
    if (typeof event.input_tokens === 'number' || typeof event.output_tokens === 'number') {
      addTokens(event.input_tokens || 0, event.output_tokens || 0);
    }
  }

  // Live context size from pipeline context_status events
  if (type === 'pipeline' && event.step === 'context_status') {
    if (typeof event.tokens === 'number') {
      _contextTokens = event.tokens;
      _renderCtxIndicator();
    }
  }

  const note = eventToNote(event);
  if (note == null) {
    if (active) _armWatchdog(); // sign of life — keep the watchdog from firing
    return;
  }
  _ensureActive(note);
}

// ── Exported helpers for chat.js (bubble-attached tool panels) ──────────────
// Reuse the same accordion row rendering so bubble panels look identical to the
// live activity panel.

export { _fmtArgs as fmtArgs, _buildRow as buildToolRow };

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
  modelCtxEl = document.getElementById('chat-model-ctx');

  if (barEl) barEl.addEventListener('click', togglePanel);

  // Show the active model's context window / max output next to the counters.
  // Re-callable (exposed below) so Settings can refresh it after a model change.
  refreshModelContext();

  // Click footer left (token bar + ctx) to open model picker
  function _onFooterClick(e) {
    e.stopPropagation();
    _toggleModelPicker();
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
  // Let Settings re-pull the footer context indicator after a model change.
  app.refreshModelContext = refreshModelContext;

  // Receive every current-session agent event from the per-user WebSocket.
  app._chatActivityHandler = handleEvent;
  // Let sessions.js update the ctx indicator after loading message history.
  app.setContextFromMessages = setContextFromMessages;
}
