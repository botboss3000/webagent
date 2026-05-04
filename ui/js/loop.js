'use strict';

import { app } from './state.js';

// ── State ──
let autoScroll = true;
let currentLevel = 'detailed'; // basic | detailed | debug
let currentTurn = null;
let currentTurnNum = 0;
let executingTools = new Map(); // tool_name → node element
let streamingBubble = null;    // current assistant bubble being built token by token
let turns = [];                // all completed turn containers
let eventBuffer = [];          // always collects events, even when tab hidden
let loopActive = false;        // is the loop tab currently visible

// ── Lazy DOM lookup (robust against init timing) ──
function getLoopList() {
  return document.getElementById('loop-list');
}

// ── Level visibility map ──
const LEVEL_VISIBILITY = {
  basic:    new Set(['user', 'agent']),
  detailed: new Set(['user', 'agent', 'pipeline']),
  debug:    new Set(['user', 'agent', 'pipeline', 'db']),
};

// ── Icons per event step ──
const STEP_ICONS = {
  load_context:     '📂',
  memory_search_start: '🔍',
  memory_search_end:   '🔍',
  build_prompt:     '📝',
  load_tools:       '📦',
  tool_defs_built:  '📦',
  llm_call_start:   '🤖',
  llm_call_end:     '🤖',
  turn_start:       '🔄',
  validate_start:   '✓',
  validate_result:  '✓',
  execute_batch_start: '⚡',
  execute_start:    '▶',
  execute_end:      '✓',
  check_continue:   '🏁',
  guardrail_check:  '🛡',
  guardrail_override: '🛡',
  guardrail_blocked: '🚫',
  max_turns_reached: '⚠️',
  memory_save_start: '💾',
  memory_save_end:   '💾',
  user_message:      '👤',
};

// ── Level colors ──
const LEVEL_COLORS = {
  user:     { border: '#7dcfff', bg: '#7dcfff10', icon: '👤', label: 'User' },
  agent:    { border: '#9ece6a', bg: '#9ece6a10', icon: '🤖', label: 'Agent' },
  pipeline: { border: '#e0af68', bg: '#e0af6810', icon: '⚙', label: 'Pipeline' },
  db:       { border: '#bb9af7', bg: '#bb9af710', icon: '💾', label: 'DB' },
};

// ── Init ── (always registers handler so events are collected in background)
export function initLoop() {
  app._loopHandler = handleEvent;
}

// ── Start: activate tab, replay buffer, then live-render ──
export function startLoop() {
  const list = getLoopList();
  if (!list) {
    console.error('[loop] #loop-list not found in DOM');
    return;
  }

  console.log('[loop] startLoop() called, activating');
  loopActive = true;
  list.innerHTML = '';
  currentTurn = null;
  currentTurnNum = 0;
  executingTools.clear();
  streamingBubble = null;
  turns = [];

  // Replay buffered events
  console.log('[loop] replaying ' + eventBuffer.length + ' buffered events');
  eventBuffer.forEach(event => renderEvent(event));
  eventBuffer = [];

  if (list.children.length === 0) {
    list.innerHTML = '<div class="loop-hint" style="padding:20px;text-align:center;color:#565f89;font-size:12px;">Pipeline visualizer active — waiting for agent events…</div>';
  }
}

// ── Stop: deactivate tab (but keep collecting events in background) ──
export function stopLoop() {
  loopActive = false;
  currentTurn = null;
  currentTurnNum = 0;
  executingTools.clear();
  streamingBubble = null;
  turns = [];
  // Don't clear eventBuffer — keep collecting

// ── Handle events (called from agentWs.js) ──
let _eventCount = 0;
function handleEvent(event) {
  _eventCount++;

  // Always buffer for replay when tab opens
  eventBuffer.push(event);

  // Only render if loop tab is active
  if (!loopActive) return;

  renderEvent(event);
}

function renderEvent(event) {
  const list = getLoopList();
  if (!list) return;

  const level = event.level || 'agent';
  const visible = LEVEL_VISIBILITY[currentLevel].has(level);
  if (!visible) return;

  // Clear hint on first real event
  const hint = list.querySelector('.loop-hint');
  if (hint && event.type !== 'ping') hint.remove();

  switch (event.type) {
    case 'user_message':
    case 'stream':
      handleStream(event);
      break;
    case 'response':
      handleResponse(event);
      break;
    case 'tool_call':
      handleToolCall(event);
      break;
    case 'tool_result':
      handleToolResult(event);
      break;
    case 'pipeline':
      handlePipeline(event);
      break;
    case 'db':
      handleDb(event);
      break;
    case 'error':
      handleError(event);
      break;
    default:
      break;
  }

  // Auto-scroll
  if (autoScroll) {
    list.scrollTop = list.scrollHeight;
  }
}

// ── Turn management ──
// Returns the parent element for a new node: either a turn-body or the list itself
function getRenderParent() {
  if (currentTurn) {
    return currentTurn.querySelector('.loop-turn-body');
  }
  // No turn yet — render directly in loop-list (pre-agent phase)
  return getLoopList();
}

function ensureTurn(turnNum) {
  if (turnNum && turnNum > currentTurnNum) {
    currentTurnNum = turnNum;
    currentTurn = createTurnContainer(turnNum);
  }
  if (!currentTurn) {
    // Don't create Turn 0 — wait for turn_start
    return null;
  }
  return currentTurn;
}

function createTurnContainer(turnNum) {
  const container = document.createElement('div');
  container.className = 'loop-turn';
  container.dataset.turn = turnNum;

  const header = document.createElement('div');
  header.className = 'loop-turn-header';
  header.innerHTML = `<span class="loop-turn-icon">🔄</span> Turn ${turnNum} <span class="loop-turn-toggle">▼</span>`;
  header.addEventListener('click', () => {
    const body = container.querySelector('.loop-turn-body');
    const collapsed = body.classList.toggle('loop-collapsed');
    header.querySelector('.loop-turn-toggle').textContent = collapsed ? '▶' : '▼';
  });

  const body = document.createElement('div');
  body.className = 'loop-turn-body';

  container.appendChild(header);
  container.appendChild(body);
  const list = getLoopList();
  if (list) list.appendChild(container);
  turns.push(container);
  return container;
}

// ── Stream events (tokens building up the assistant bubble) ──
function handleStream(event) {
  const parent = getRenderParent();
  if (!parent) return;
  
  if (!streamingBubble) {
    streamingBubble = createNode('agent', 'streaming', event.content || '', parent);
    streamingBubble.classList.add('loop-streaming');
    streamingBubble.querySelector('.loop-node-icon').textContent = '🤖';
    const spinner = document.createElement('span');
    spinner.className = 'loop-spinner';
    spinner.textContent = '⟳';
    streamingBubble.querySelector('.loop-node-summary').appendChild(spinner);
  } else {
    const summary = streamingBubble.querySelector('.loop-node-summary');
    summary.appendChild(document.createTextNode(event.content || ''));
  }
}

// ── Response (final assistant message) ──
function handleResponse(event) {
  const parent = getRenderParent();
  
  if (streamingBubble) {
    streamingBubble.classList.remove('loop-streaming');
    const spinner = streamingBubble.querySelector('.loop-spinner');
    if (spinner) spinner.remove();
    // Don't overwrite accumulated stream content
    streamingBubble.querySelector('.loop-node-icon').textContent = '🤖';
    streamingBubble = null;
  } else if (parent) {
    const node = createNode('agent', 'response', event.content || '', parent);
    node.querySelector('.loop-node-icon').textContent = '🤖';
  }
}

// ── Tool calls ──
function handleToolCall(event) {
  const parent = getRenderParent();
  if (!parent) return;
  
  const toolName = event.tool || 'unknown';
  const args = event.args || {};

  const node = createNode('agent', 'tool_call', toolName, parent);
  node.classList.add('loop-executing');
  node.dataset.tool = toolName;
  node.querySelector('.loop-node-icon').textContent = '📖';

  const argsStr = typeof args === 'object' ? JSON.stringify(args).substring(0, 80) : String(args).substring(0, 80);
  const summary = node.querySelector('.loop-node-summary');
  summary.textContent = `${toolName}(${argsStr}${JSON.stringify(args).length > 80 ? '…' : ''})`;

  const spinner = document.createElement('span');
  spinner.className = 'loop-spinner';
  spinner.textContent = ' ⟳';
  summary.appendChild(spinner);

  executingTools.set(toolName, node);
  node._details = { tool: toolName, args: args, fullArgs: args };
}

// ── Tool results ──
function handleToolResult(event) {
  const toolName = event.tool || 'unknown';
  const node = executingTools.get(toolName);

  if (node) {
    executingTools.delete(toolName);
    node.classList.remove('loop-executing');

    const spinner = node.querySelector('.loop-spinner');
    if (spinner) spinner.remove();

    const summary = node.querySelector('.loop-node-summary');
    const dur = event.duration_ms ? ` ${event.duration_ms}ms` : '';
    const status = event.error ? ' ✗' : ' ✓';

    if (event.error_type === 'guardrail_blocked') {
      node.querySelector('.loop-node-icon').textContent = '🚫';
      node.style.borderLeftColor = '#f7768e';
      summary.textContent = `${toolName} BLOCKED${dur}`;
    } else if (event.error) {
      node.querySelector('.loop-node-icon').textContent = '✗';
      node.style.borderLeftColor = '#f7768e';
      summary.textContent = `${toolName}${status}${dur}`;
    } else {
      node.querySelector('.loop-node-icon').textContent = '✓';
      summary.textContent = `${toolName}${status}${dur}`;
    }

    node._details = node._details || {};
    node._details.result = event.result;
    node._details.duration_ms = event.duration_ms;
    node._details.error = event.error;
    node._details.error_type = event.error_type;
  } else {
    const parent = getRenderParent();
    if (parent) {
      const node = createNode('agent', 'tool_result', toolName, parent);
      const status = event.error ? ' ✗' : ' ✓';
      node.querySelector('.loop-node-icon').textContent = event.error ? '✗' : '✓';
      node.querySelector('.loop-node-summary').textContent = `${toolName}${status} ${event.duration_ms || 0}ms`;
      node._details = { tool: toolName, result: event.result, duration_ms: event.duration_ms, error: event.error };
    }
  }
}

// ── Pipeline events ──
function handlePipeline(event) {
  const step = event.step || 'unknown';

  if (step === 'turn_start' && event.turn) {
    currentTurn = ensureTurn(event.turn);
    return;
  }

  const parent = getRenderParent();
  if (!parent) return;

  const icon = STEP_ICONS[step] || '⚙';
  const node = createNode('pipeline', step, '', parent);
  node.querySelector('.loop-node-icon').textContent = icon;

  let summary = '';
  switch (step) {
    case 'load_context':
      summary = `Context loaded: ${event.count || 0} docs`;
      break;
    case 'memory_search_start':
      summary = `memory_search("${(event.query || '').substring(0, 60)}") — searching…`;
      break;
    case 'memory_search_end':
      summary = `memory_search → ${event.results_count || 0} results`;
      break;
    case 'build_prompt':
      summary = `Prompt built${event.brain_injected ? ' (brain injected)' : ''}`;
      break;
    case 'load_tools':
      summary = `Tools loaded: ${event.count || 0} tools (${event.duration_ms || 0}ms)`;
      break;
    case 'tool_defs_built':
      summary = `Tool definitions: ${event.count || 0}`;
      break;
    case 'llm_call_start':
      summary = `LLM call → ${event.model || '?'} (${event.message_count || 0} msgs)`;
      break;
    case 'llm_call_end':
      summary = `LLM done: ${event.duration_ms || 0}ms`;
      if (event.input_tokens) summary += `, ${event.input_tokens}↓${event.output_tokens || 0}↑ tokens`;
      break;
    case 'validate_start':
      summary = `Validating ${event.tool_count || 0} tool(s)`;
      break;
    case 'validate_result':
      summary = `${event.tool || '?'}: ${event.passed ? '✓ passed' : '✗ FAILED'}`;
      if (event.error) summary += ` — ${event.error}`;
      break;
    case 'execute_batch_start':
      summary = `Executing ${event.tool_count || 0} tool(s): ${(event.tools || []).join(', ')}`;
      break;
    case 'execute_start':
      summary = `${event.tool || '?'} ⟳ executing…`;
      break;
    case 'execute_end':
      summary = `${event.tool || '?'}: ${event.duration_ms || 0}ms ${event.success ? '✓' : '✗'}`;
      break;
    case 'check_continue':
      summary = `Turn ${event.turn}/${event.max_turns} — ${event.will_continue ? 'continuing' : 'stopping'}`;
      break;
    case 'guardrail_check':
      summary = `🛡 ${event.tool || '?'} — requires confirmation`;
      node.style.borderLeftColor = '#e0af68';
      break;
    case 'guardrail_override':
      summary = `🛡 ${event.tool || '?'} — confirmed by ${event.by || 'user'}`;
      node.style.borderLeftColor = '#9ece6a';
      break;
    case 'guardrail_blocked':
      summary = `🚫 ${event.tool || '?'} — BLOCKED`;
      node.style.borderLeftColor = '#f7768e';
      break;
    case 'max_turns_reached':
      summary = `⚠️ Max turns (${event.max_turns}) reached`;
      node.style.borderLeftColor = '#e0af68';
      break;
    case 'user_message':
      // New agent run starts — reset turn state
      currentTurn = null;
      currentTurnNum = 0;
      summary = `[USER] ${(event.content || '').substring(0, 100)}`;
      node.style.borderLeftColor = '#7dcfff';
      node.style.backgroundColor = '#7dcfff10';
      break;
    case 'memory_save_start':
      summary = `Memory save → ${event.slug || '?'}`;
      break;
    case 'memory_save_end':
      summary = `Memory saved: ${event.slug || '?'}`;
      break;
    default:
      summary = step;
      break;
  }

  node.querySelector('.loop-node-summary').textContent = summary;
  node._details = event;
}

// ── DB events ──
function handleDb(event) {
  const parent = getRenderParent();
  if (!parent) return;
  
  const node = createNode('db', event.op || 'db_op', '', parent);
  node.querySelector('.loop-node-icon').textContent = '💾';

  let summary = '';
  switch (event.op) {
    case 'insert_interaction':
      summary = `DB insert: ${event.role || '?'}`;
      if (event.tool_name) summary += ` (${event.tool_name})`;
      if (event.ms) summary += ` ${event.ms}ms`;
      break;
    case 'skill_track':
      summary = `Skill track: ${event.tool || '?'} ${event.success ? '✓' : '✗'}`;
      if (event.new_rating != null) summary += ` rating:${Math.round(event.new_rating)}%`;
      break;
    case 'memory_upsert':
      summary = `Memory upsert: ${event.slug || '?'} (${event.page_type || '?'})`;
      break;
    default:
      summary = `DB: ${event.op || '?'}`;
      break;
  }

  node.querySelector('.loop-node-summary').textContent = summary;
  node._details = event;
}

// ── Error events ──
function handleError(event) {
  const parent = getRenderParent();
  if (!parent) return;
  
  const node = createNode('agent', 'error', event.message || 'Unknown error', parent);
  node.style.borderLeftColor = '#f7768e';
  node.querySelector('.loop-node-icon').textContent = '⚠️';
  node.querySelector('.loop-node-summary').textContent = event.message || 'Error';
  node._details = event;
}

// ── Node creation ──
function createNode(level, type, text, parent) {
  const colors = LEVEL_COLORS[level] || LEVEL_COLORS.agent;

  const node = document.createElement('div');
  node.className = 'loop-node';
  node.dataset.level = level;
  node.dataset.type = type;
  node.style.borderLeftColor = colors.border;
  node.style.backgroundColor = colors.bg;

  node.innerHTML = `
    <span class="loop-node-icon">${colors.icon}</span>
    <span class="loop-node-summary">${escapeHtml(text)}</span>
  `;

  node.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleDetails(node);
  });

  node.addEventListener('mouseenter', () => {
    showTooltip(node);
  });
  node.addEventListener('mouseleave', () => {
    hideTooltip();
  });

  parent.appendChild(node);
  return node;
}

// ── Details toggle ──
function toggleDetails(node) {
  let detailPanel = node.querySelector('.loop-detail-panel');
  if (detailPanel) {
    detailPanel.remove();
    return;
  }

  detailPanel = document.createElement('div');
  detailPanel.className = 'loop-detail-panel';

  const details = node._details || {};
  let html = '';

  if (details.fullArgs) {
    html += `<div class="loop-detail-section">
      <span class="loop-detail-label">Arguments</span>
      <pre class="loop-detail-pre">${escapeHtml(JSON.stringify(details.fullArgs, null, 2))}</pre>
    </div>`;
  } else if (details.args && Object.keys(details.args).length > 0) {
    html += `<div class="loop-detail-section">
      <span class="loop-detail-label">Arguments</span>
      <pre class="loop-detail-pre">${escapeHtml(JSON.stringify(details.args, null, 2))}</pre>
    </div>`;
  }

  if (details.result != null) {
    const resultStr = typeof details.result === 'string' ? details.result : JSON.stringify(details.result, null, 2);
    const truncated = resultStr.length > 500 ? resultStr.substring(0, 500) + '…' : resultStr;
    html += `<div class="loop-detail-section">
      <span class="loop-detail-label">Result</span>
      <pre class="loop-detail-pre">${escapeHtml(truncated)}</pre>
      ${resultStr.length > 500 ? '<button class="loop-show-more">Show full result</button><pre class="loop-detail-pre loop-detail-full" style="display:none;">' + escapeHtml(resultStr) + '</pre>' : ''}
    </div>`;
  }

  if (details.duration_ms != null) {
    html += `<div class="loop-detail-section">
      <span class="loop-detail-label">Duration</span>
      <span>${details.duration_ms}ms</span>
    </div>`;
  }

  if (details.error) {
    html += `<div class="loop-detail-section">
      <span class="loop-detail-label" style="color:#f7768e;">Error</span>
      <span style="color:#f7768e;">${details.error_type || 'error'}: ${details.error === true ? 'Failed' : escapeHtml(String(details.error))}</span>
    </div>`;
  }

  if (Object.keys(details).length > 0 && !details.fullArgs && !details.result && !details.error) {
    html += `<div class="loop-detail-section">
      <span class="loop-detail-label">Event data</span>
      <pre class="loop-detail-pre">${escapeHtml(JSON.stringify(details, null, 2))}</pre>
    </div>`;
  }

  if (!html) {
    html = '<div class="loop-detail-section" style="color:#565f89;">No details available</div>';
  }

  detailPanel.innerHTML = html;
  node.appendChild(detailPanel);

  const showMore = detailPanel.querySelector('.loop-show-more');
  if (showMore) {
    showMore.addEventListener('click', (e) => {
      e.stopPropagation();
      const full = detailPanel.querySelector('.loop-detail-full');
      full.style.display = full.style.display === 'none' ? 'block' : 'none';
      showMore.textContent = full.style.display === 'none' ? 'Show full result' : 'Hide full result';
    });
  }
}

// ── Tooltip ──
let tooltipEl = null;

function showTooltip(node) {
  if (tooltipEl) tooltipEl.remove();

  const details = node._details || {};
  const parts = [];
  if (details.duration_ms != null) parts.push(`${details.duration_ms}ms`);
  if (details.input_tokens != null && details.output_tokens != null) {
    parts.push(`${details.input_tokens}↓${details.output_tokens}↑ tokens`);
  }
  if (details.id) parts.push(`ID: ${details.id}`);
  if (parts.length === 0) return;

  tooltipEl = document.createElement('div');
  tooltipEl.className = 'loop-tooltip';
  tooltipEl.textContent = parts.join(' · ');
  tooltipEl.style.cssText = `
    position: fixed; z-index: 9999;
    background: #1a1a2e; border: 1px solid #2a2a4a;
    color: #a9b1d6; padding: 4px 10px; border-radius: 4px;
    font-size: 11px; pointer-events: none;
    white-space: nowrap;
  `;

  const rect = node.getBoundingClientRect();
  tooltipEl.style.left = rect.right + 8 + 'px';
  tooltipEl.style.top = rect.top + 'px';
  document.body.appendChild(tooltipEl);
}

function hideTooltip() {
  if (tooltipEl) {
    tooltipEl.remove();
    tooltipEl = null;
  }
}

// ── Escape HTML ──
function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Filter buttons ──
export function setLoopLevel(level) {
  currentLevel = level;
  document.querySelectorAll('.loop-filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.level === level);
  });
  const list = getLoopList();
  if (!list) return;
  list.querySelectorAll('.loop-node').forEach(node => {
    const lv = node.dataset.level || 'agent';
    node.style.display = LEVEL_VISIBILITY[currentLevel].has(lv) ? '' : 'none';
  });
}

// ── Auto-scroll toggle ──
export function toggleAutoScroll() {
  autoScroll = !autoScroll;
  const btn = document.getElementById('loop-autoscroll');
  if (btn) {
    btn.textContent = autoScroll ? 'Auto-scroll ✓' : 'Auto-scroll ✗';
    btn.classList.toggle('active', autoScroll);
  }
}
