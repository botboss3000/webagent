'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';

// ── Graph definition: the agent loop structure ──
const LOOP_NODES = [
  { id: 'user_input',     label: 'User Input',          type: 'input',   x: 400, y: 10 },
  { id: 'load_context',   label: 'Load Context',        type: 'process', x: 140, y: 120 },
  { id: 'memory_search',  label: 'Memory Search',       type: 'process', x: 400, y: 120 },
  { id: 'build_prompt',   label: 'Build Prompt',        type: 'process', x: 660, y: 120 },
  { id: 'llm_call',       label: 'LLM Call',            type: 'llm',     x: 400, y: 220 },
  { id: 'validate_tools', label: 'Validate\nTools',     type: 'decision',x: 260, y: 340 },
  { id: 'guardrails',     label: 'Guardrails',           type: 'decision',x: 540, y: 340 },
  { id: 'execute_tools',  label: 'Execute Tools',       type: 'process', x: 400, y: 460 },
  { id: 'check_continue', label: 'Check\nContinue',     type: 'decision',x: 400, y: 580 },
  { id: 'final_response', label: 'Final Response',       type: 'output',  x: 400, y: 700 },
  { id: 'memory_save',    label: 'Memory Save',          type: 'process', x: 400, y: 790 },
];

const LOOP_EDGES = [
  { from: 'user_input',     to: 'load_context' },
  { from: 'user_input',     to: 'memory_search' },
  { from: 'user_input',     to: 'build_prompt' },
  { from: 'load_context',   to: 'llm_call' },
  { from: 'memory_search',  to: 'llm_call' },
  { from: 'build_prompt',   to: 'llm_call' },
  { from: 'llm_call',       to: 'validate_tools', label: 'Has tools' },
  { from: 'llm_call',       to: 'check_continue', label: 'No tools' },
  { from: 'validate_tools', to: 'guardrails',     label: 'Valid' },
  { from: 'validate_tools', to: 'check_continue', label: 'Invalid' },
  { from: 'guardrails',     to: 'execute_tools',  label: 'Pass' },
  { from: 'guardrails',     to: 'final_response', label: 'Blocked' },
  { from: 'execute_tools',  to: 'llm_call',       label: '↺ Loop', loopback: true },
  { from: 'check_continue', to: 'llm_call',       label: 'Continue' },
  { from: 'check_continue', to: 'final_response', label: 'Stop' },
  { from: 'final_response', to: 'memory_save' },
];

// ── Edge routing helper ──
function getEdgePoints(edge, from, to) {
  // Center-to-center by default
  let x1 = from.x, y1 = from.y;
  let x2 = to.x, y2 = to.y;

  // Special routing for specific edges
  if (edge.loopback) {
    // execute_tools → llm_call: exit right, enter left
    x1 = from.x + 80;
    y1 = from.y + 5;
    x2 = to.x - 100;
    y2 = to.y + 5;
  } else if (edge.from === 'validate_tools' && edge.to === 'check_continue' && edge.label === 'Invalid') {
    // Down-left exit from validate
    x1 = from.x - 50; y1 = from.y + 25;
    x2 = to.x - 55; y2 = to.y - 30;
  } else if (edge.from === 'guardrails' && edge.to === 'final_response' && edge.label === 'Blocked') {
    // Right exit from guardrails down to final
    x1 = from.x + 50; y1 = from.y + 25;
    x2 = to.x + 70; y2 = to.y - 20;
  } else if (edge.from === 'llm_call' && edge.to === 'validate_tools' && edge.label === 'Has tools') {
    // Exit llm left-down, enter validate top
    x1 = from.x - 30; y1 = from.y + 25;
    x2 = to.x - 30; y2 = to.y - 5;
  } else if (edge.from === 'llm_call' && edge.to === 'check_continue' && edge.label === 'No tools') {
    // Exit llm right-down, enter check_continue top-right
    x1 = from.x + 30; y1 = from.y + 25;
    x2 = to.x + 30; y2 = to.y - 25;
  } else if (edge.from === 'user_input' && edge.to !== 'load_context') {
    // Fan out from user_input to multiple targets
    if (edge.to === 'memory_search') { x1 += 0; x2 = to.x; y2 = to.y - 15; }
    if (edge.to === 'build_prompt') { x1 += 0; x2 = to.x; y2 = to.y - 15; }
    y1 = from.y + 25;
  } else {
    // Default: bottom-center of from → top-center of to
    y1 = from.y + 25;
    y2 = to.y - 15;
  }

  // Slight horizontal offset for stacked edges to avoid overlap
  if (edge.from === 'user_input') {
    if (edge.to === 'memory_search') x1 -= 15;
    if (edge.to === 'build_prompt') x1 += 15;
    if (edge.to === 'memory_search') x2 = to.x - 30;
    if (edge.to === 'build_prompt') x2 = to.x + 30;
  }

  return { x1, y1, x2, y2 };
}

// ── State ──
let loopVisualActive = false;
let eventBuffer = [];
const MAX_BUFFER = 2000;

// Pages: each "turn" (user message cycle) = one page
let pages = [];          // { turnNum, events: [], nodeStates: Map<nodeId, state> }
let currentPageIdx = 0;
let nextTurnNum = 0;
let sessionLoaded = false;

// Track current turn's node states
let currentNodeStates = new Map();
let currentTurnEvents = [];

// Render debounce
let _renderTimer = null;

// ── Init ──
export function initLoopVisual() {
  app._loopVisualHandler = handleEvent;
}

// ── Start ──
export function startLoopVisual() {
  loopVisualActive = true;

  const area = document.getElementById('loop-visual-graph-area');
  const pagesContainer = document.getElementById('loop-visual-pages');
  if (!area || !pagesContainer) return;

  // Reset state
  pages = [];
  currentPageIdx = 0;
  nextTurnNum = 0;
  currentNodeStates = new Map();
  currentTurnEvents = [];
  sessionLoaded = false;

  // Show loading
  area.innerHTML = '<div class="loop-visual-hint">Loading loop history…</div>';
  pagesContainer.innerHTML = '';

  fetchLoopHistory();
}

// ── Stop ──
export function stopLoopVisual() {
  loopVisualActive = false;
}

// ── Handle events from agentWs ──
function handleEvent(event) {
  // Always buffer
  if (eventBuffer.length >= MAX_BUFFER) eventBuffer.shift();
  eventBuffer.push(event);

  if (!loopVisualActive) return;
  processEvent(event);
}

function processEvent(event) {
  const type = event.type;
  const step = event.step;

  // Detect new user message → start new page
  if (type === 'pipeline' && step === 'user_message') {
    nextTurnNum++;
    startNewPage(nextTurnNum);
  }

  // Turn start → also create page if not already
  if (type === 'pipeline' && step === 'turn_start' && event.turn) {
    if (!pages.find(p => p.turnNum === event.turn)) {
      startNewPage(event.turn);
    }
  }

  // Fallback: if no pages exist and we get any event, create page 1
  if (pages.length === 0 && type !== 'ping') {
    nextTurnNum = 1;
    startNewPage(1);
  }

  // Map event to node → update state
  const nodeId = eventToNodeId(event);
  if (nodeId) {
    const state = getNodeState(event);
    currentNodeStates.set(nodeId, state);
    currentTurnEvents.push({ nodeId, state, event, ts: Date.now() });

    // Record on current page
    const page = pages[currentPageIdx];
    if (page) {
      page.nodeStates.set(nodeId, state);
      page.events.push({ nodeId, state, event, ts: Date.now() });
    }
  }

  // Re-render current page (debounced via rAF)
  if (currentPageIdx < pages.length) {
    if (_renderTimer) cancelAnimationFrame(_renderTimer);
    _renderTimer = requestAnimationFrame(() => {
      _renderTimer = null;
      const idx = currentPageIdx;
      renderPage(idx);
      updatePageSummary(idx);
    });
  }
}

function eventToNodeId(event) {
  const type = event.type;
  const step = event.step;

  if (type === 'pipeline') {
    switch (step) {
      case 'user_message':     return 'user_input';
      case 'load_context':     return 'load_context';
      case 'memory_search_start':
      case 'memory_search_end': return 'memory_search';
      case 'build_prompt':     return 'build_prompt';
      case 'load_tools':
      case 'tool_defs_built':  return 'load_context';
      case 'llm_call_start':
      case 'llm_call_end':     return 'llm_call';
      case 'validate_start':
      case 'validate_result':  return 'validate_tools';
      case 'guardrail_check':
      case 'guardrail_override':
      case 'guardrail_blocked': return 'guardrails';
      case 'execute_batch_start':
      case 'execute_start':
      case 'execute_end':      return 'execute_tools';
      case 'check_continue':   return 'check_continue';
      case 'max_turns_reached': return 'check_continue';
      case 'memory_save_start':
      case 'memory_save_end':  return 'memory_save';
      default: return null;
    }
  }
  if (type === 'tool_call' || type === 'tool_result') {
    return 'execute_tools';
  }
  if (type === 'response') {
    return 'final_response';
  }
  if (type === 'error') {
    // Highlight the llm node or relevant one
    return 'llm_call';
  }
  return null;
}

function getNodeState(event) {
  const type = event.type;
  const step = event.step;

  if (type === 'error') return 'error';
  if (type === 'tool_result' && event.error) return 'error';
  if (type === 'pipeline') {
    if (step === 'guardrail_blocked') return 'error';
    if (step === 'validate_result' && !event.passed) return 'error';
    // Start states
    if (step === 'llm_call_start' || step === 'execute_start' ||
        step === 'memory_search_start' || step === 'validate_start' ||
        step === 'guardrail_check' || step === 'memory_save_start') {
      return 'active';
    }
    // End states → done
    return 'done';
  }
  if (type === 'tool_call') return 'active';
  if (type === 'tool_result') return 'done';
  if (type === 'response') return 'done';
  return 'done';
}

function startNewPage(turnNum) {
  // Save current page if exists
  if (currentNodeStates.size > 0 && !pages.find(p => p.turnNum === turnNum)) {
    // Check if we already have this turn
  }

  // Clear current states
  currentNodeStates = new Map();
  currentTurnEvents = [];

  const page = {
    turnNum,
    nodeStates: new Map(),
    events: [],
    ts: Date.now(),
  };
  pages.push(page);
  currentPageIdx = pages.length - 1;

  // Update page buttons
  renderPageButtons();
  renderPage(currentPageIdx);
  updatePageSummary(currentPageIdx);

  // If we were on an older page, don't auto-switch unless it's the latest
  // Auto-switch to new page
  selectPage(currentPageIdx);
}

function selectPage(idx) {
  if (idx < 0 || idx >= pages.length) return;
  currentPageIdx = idx;

  // Update button active state
  document.querySelectorAll('.loop-visual-page-btn').forEach((btn, i) => {
    btn.classList.toggle('active', i === idx);
  });

  renderPage(idx);
  updatePageSummary(idx);
}

function renderPageButtons() {
  const container = document.getElementById('loop-visual-pages');
  if (!container) return;

  container.innerHTML = pages.map((p, i) => {
    const active = i === currentPageIdx ? 'active' : '';
    const label = p.turnNum <= 1 ? 'Initial' : `Turn ${p.turnNum - 1}`;
    return `<button class="loop-visual-page-btn ${active}" data-idx="${i}">
      <span class="page-turn-num">#${p.turnNum}</span> ${label}
    </button>`;
  }).join('');

  // Wire clicks
  container.querySelectorAll('.loop-visual-page-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      selectPage(parseInt(btn.dataset.idx));
    });
  });
}

function renderPage(idx) {
  const area = document.getElementById('loop-visual-graph-area');
  if (!area) return;

  const page = pages[idx];
  if (!page) {
    area.innerHTML = '<div class="loop-visual-hint">No loop data yet — waiting for agent events…</div>';
    return;
  }

  area.innerHTML = '';

  // Create nodes container first
  const nodesContainer = document.createElement('div');
  nodesContainer.id = 'loop-visual-nodes';
  nodesContainer.style.cssText = 'position:relative;width:800px;min-height:840px;flex-shrink:0;margin:0 auto;';
  area.appendChild(nodesContainer);

  // Create SVG for arrows (inside nodes container, positioned absolutely)
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.id = 'loop-visual-svg';
  svg.setAttribute('width', '800');
  svg.setAttribute('height', '840');
  svg.setAttribute('viewBox', '0 0 800 840');
  svg.style.position = 'absolute';
  svg.style.top = '0';
  svg.style.left = '0';
  svg.style.pointerEvents = 'none';
  svg.style.zIndex = '0';
  nodesContainer.appendChild(svg);

  // Add arrowhead marker def
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  defs.innerHTML = `<marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto" fill="#2a2a4a">
    <polygon points="0 0, 8 3, 0 6" />
  </marker>
  <marker id="arrowhead-active" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto" fill="#7dcfff">
    <polygon points="0 0, 8 3, 0 6" />
  </marker>
  <marker id="arrowhead-done" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto" fill="#9ece6a">
    <polygon points="0 0, 8 3, 0 6" />
  </marker>`;
  svg.appendChild(defs);

  // Build edge states from node states
  const edgeStates = new Map();

  for (const edge of LOOP_EDGES) {
    const fromState = page.nodeStates.get(edge.from);
    const toState = page.nodeStates.get(edge.to);
    if (fromState === 'done' && (toState === 'done' || toState === 'active')) {
      edgeStates.set(edge.from + '->' + edge.to, 'done');
    } else if (fromState === 'done' || fromState === 'active') {
      edgeStates.set(edge.from + '->' + edge.to, 'active');
    }
  }

  // Draw edges (SVG lines)
  for (const edge of LOOP_EDGES) {
    const from = LOOP_NODES.find(n => n.id === edge.from);
    const to = LOOP_NODES.find(n => n.id === edge.to);
    if (!from || !to) continue;

    const edgeState = edgeStates.get(edge.from + '->' + edge.to) || '';

    // Compute edge path points
    let pts = getEdgePoints(edge, from, to);

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    path.setAttribute('x1', pts.x1);
    path.setAttribute('y1', pts.y1);
    path.setAttribute('x2', pts.x2);
    path.setAttribute('y2', pts.y2);
    let cls = 'lv-arrow';
    if (edge.loopback) cls += ' lv-arrow-loopback';
    if (edgeState === 'done') cls += ' lv-arrow-done';
    else if (edgeState === 'active') cls += ' lv-arrow-active';
    path.setAttribute('class', cls);
    svg.appendChild(path);

    // Edge label
    if (edge.label) {
      const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      const lx = (pts.x1 + pts.x2) / 2;
      const ly = (pts.y1 + pts.y2) / 2 - 6;
      label.setAttribute('x', lx);
      label.setAttribute('y', ly);
      let lcls = 'lv-arrow-label';
      if (edgeState === 'done' || edgeState === 'active') lcls += ' lv-arrow-label-active';
      label.setAttribute('class', lcls);
      label.textContent = edge.label;
      svg.appendChild(label);
    }
  }

  // Render nodes
  for (const nodeDef of LOOP_NODES) {
    const nodeState = page.nodeStates.get(nodeDef.id) || '';
    const node = document.createElement('div');
    node.className = `lv-node lv-type-${nodeDef.type}`;
    if (nodeState === 'active') node.classList.add('lv-active');
    else if (nodeState === 'done') node.classList.add('lv-done');
    else if (nodeState === 'error') node.classList.add('lv-error');

    // Center node on (x,y)
    const nw = nodeDef.type === 'decision' ? 80 : 120;
    node.style.left = (nodeDef.x - nw/2) + 'px';
    node.style.top = (nodeDef.y - (nodeDef.type === 'decision' ? 40 : 18)) + 'px';
    if (nodeDef.type !== 'decision') {
      node.style.minWidth = '120px';
    }

    const label = document.createElement('span');
    label.className = 'lv-node-label';
    label.textContent = nodeDef.label;
    node.appendChild(label);

    // Detail tooltip area
    const detailEl = document.createElement('div');
    detailEl.className = 'lv-node-detail';
    // Fill with event info for this node on this page
    const nodeEvents = page.events.filter(e => e.nodeId === nodeDef.id);
    if (nodeEvents.length > 0) {
      const lastEv = nodeEvents[nodeEvents.length - 1];
      const parts = [];
      if (lastEv.event.duration_ms) parts.push(`${lastEv.event.duration_ms}ms`);
      if (lastEv.event.input_tokens) parts.push(`↓${lastEv.event.input_tokens}`);
      if (lastEv.event.output_tokens) parts.push(`↑${lastEv.event.output_tokens}`);
      if (lastEv.event.model) parts.push(lastEv.event.model);
      if (lastEv.event.tool) parts.push(lastEv.event.tool);
      if (parts.length > 0) {
        detailEl.textContent = parts.join(' · ');
      } else {
        // Show count
        const count = nodeEvents.length;
        const actionCounts = {};
        nodeEvents.forEach(e => {
          const t = e.event.type + (e.event.step ? '/' + e.event.step : '');
          actionCounts[t] = (actionCounts[t] || 0) + 1;
        });
        detailEl.textContent = Object.entries(actionCounts)
          .map(([k, v]) => `${k}×${v}`).join(' · ');
      }
    } else {
      detailEl.textContent = 'Waiting…';
    }
    node.appendChild(detailEl);

    nodesContainer.appendChild(node);
  }
}

function updatePageSummary(idx) {
  const page = pages[idx];
  if (!page) return;

  // Remove old summary
  const old = document.querySelector('.lv-page-summary');
  if (old) old.remove();

  const area = document.getElementById('loop-visual-graph-area');
  if (!area) return;

  const summary = document.createElement('div');
  summary.className = 'lv-page-summary';

  const doneCount = [...page.nodeStates.values()].filter(s => s === 'done').length;
  const totalCount = LOOP_NODES.length;

  summary.innerHTML = `
    <span><span class="lv-summary-label">Turn</span> ${page.turnNum}</span>
    <span><span class="lv-summary-label">Steps</span> ${doneCount}/${totalCount}</span>
    <span><span class="lv-summary-label">Events</span> ${page.events.length}</span>
  `;

  area.appendChild(summary);
}

// ── Fetch history from DB ──
async function fetchLoopHistory() {
  const area = document.getElementById('loop-visual-graph-area');
  const userId = app.currentUserId;
  const sessionId = app.currentSessionId;

  if (!userId || !sessionId) {
    replayBuffer();
    return;
  }

  try {
    const url = apiPath(`/api/v1/db/stream/interactions?user_id=${encodeURIComponent(userId)}&session_id=${encodeURIComponent(sessionId)}`);
    const res = await fetch(url);
    const data = await res.json();
    const rows = data.interactions || [];

    sessionLoaded = true;

    // Process rows into events
    rows.forEach(row => {
      const events = interactionToEvents(row);
      events.forEach(ev => processEvent(ev));
    });

    replayBuffer();

    if (pages.length === 0) {
      area.innerHTML = '<div class="loop-visual-hint">Agent loop visualizer — waiting for agent events…</div>';
    }
  } catch (e) {
    console.error('[loop-visual] fetch failed:', e);
    replayBuffer();
  }
}

function replayBuffer() {
  const area = document.getElementById('loop-visual-graph-area');
  const hint = area?.querySelector('.loop-visual-hint');
  if (hint && eventBuffer.length > 0) hint.remove();

  eventBuffer.forEach(ev => processEvent(ev));
  eventBuffer = [];

  if (pages.length === 0) {
    const el = document.getElementById('loop-visual-graph-area');
    if (el) {
      el.innerHTML = '<div class="loop-visual-hint">Agent loop visualizer — waiting for agent events…</div>';
    }
  }
}

// ── Convert DB row to events (same as loop.js) ──
function interactionToEvents(row) {
  const events = [];
  const role = row.role || 'unknown';

  if (role === 'user') {
    events.push({
      type: 'pipeline', level: 'user',
      step: 'user_message', content: row.content || '',
    });
  } else if (role === 'assistant') {
    let meta = {};
    try { meta = JSON.parse(row.metadata || '{}'); } catch(e) {}
    if (meta.turn) {
      events.push({
        type: 'pipeline', level: 'pipeline',
        step: 'turn_start', turn: meta.turn, max_turns: 10,
      });
    }
    events.push({
      type: 'response', level: 'agent',
      content: (row.content || '').replace(/\n\n\[Tool calls:.*\]$/s, ''),
      _input_tokens: meta.input_tokens,
      _output_tokens: meta.output_tokens,
      _duration_ms: meta.duration_ms,
      _model: meta.model,
    });
  } else if (role === 'tool') {
    const toolName = row.tool_name || 'unknown';
    let meta = {};
    try { meta = JSON.parse(row.metadata || '{}'); } catch(e) {}

    if (toolName === 'memory_search') {
      let contentObj = {};
      try { contentObj = JSON.parse(row.content || '{}'); } catch(e) {}
      events.push({
        type: 'pipeline', level: 'pipeline',
        step: 'memory_search_end', results_count: meta.count || contentObj.count || 0,
      });
    } else if (toolName === 'memory_save') {
      events.push({
        type: 'pipeline', level: 'pipeline',
        step: 'memory_save_end', slug: meta.slug || toolName,
      });
    } else {
      events.push({
        type: 'tool_call', level: 'agent',
        tool: toolName, args: meta.input_params || {},
      });
      events.push({
        type: 'tool_result', level: 'agent',
        tool: toolName, result: row.content || '',
        duration_ms: meta.duration_ms || 0,
        error: !(meta.success !== false),
        error_type: meta.error_message ? 'execution_error' : null,
        recoverable: true,
      });
    }
  }

  return events;
}

// ── Session changed ──
export function loopVisualSessionChanged() {
  if (!loopVisualActive) return;

  const area = document.getElementById('loop-visual-graph-area');
  if (area) {
    area.innerHTML = '<div class="loop-visual-hint">Session changed — reloading…</div>';
  }

  pages = [];
  currentPageIdx = 0;
  nextTurnNum = 0;
  currentNodeStates = new Map();
  currentTurnEvents = [];
  sessionLoaded = false;
  eventBuffer = [];

  fetchLoopHistory();
}
