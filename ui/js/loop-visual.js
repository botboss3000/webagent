'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';

// ── Static items per node (slash commands and Settings links) ──
// These cannot come from the /admin/tools endpoint — they live here.
// type: 'command' = user slash command, 'admin' = settings panel shortcut
const NODE_STATIC_ITEMS = {
  user_input: [
    { name: '/optimize',             type: 'command', desc: 'Run the optimizer on this session to improve agent skills' },
    { name: '/optimize <feedback>',  type: 'command', desc: 'Run optimizer with specific feedback about what to improve' },
  ],
  build_prompt: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Edit the agent\'s core directive and persona' },
  ],
  llm_call: [
    { name: 'Settings → Provider',   type: 'admin',   desc: 'Change the LLM model, base URL, or API key' },
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Edit the agent\'s system prompt and persona' },
  ],
  check_continue: [
    { name: 'Settings → Max Turns',  type: 'admin',   desc: 'Configure the maximum number of agentic turns per request' },
  ],
  final_response: [
    { name: '/optimize',             type: 'command', desc: 'Trigger optimizer on this session to improve future responses' },
  ],
  memory_save: [
    { name: 'Settings → Source',     type: 'admin',   desc: 'View and manage memory and context documents' },
  ],
  opt_collect: [
    { name: '/optimize [feedback]',  type: 'command', desc: 'Trigger a new optimizer run against the current session' },
    { name: 'Settings → Optimizer',  type: 'admin',   desc: 'Configure run mode, intensity, schedule, and scan scope' },
  ],
};

// ── Canvas dimensions ──
const CANVAS_W = 1120;
const CANVAS_H = 430;

// ── Stage column definitions (left → right) ──
const STAGES = [
  { label: 'INPUT',     x1: 0,    x2: 118,  color: '#7dcfff' },
  { label: 'CONTEXT',   x1: 126,  x2: 306,  color: '#c0caf5' },
  { label: 'INFERENCE', x1: 314,  x2: 466,  color: '#bb9af7' },
  { label: 'ROUTING',   x1: 474,  x2: 664,  color: '#e0af68' },
  { label: 'EXECUTION', x1: 672,  x2: 826,  color: '#a9b1d6' },
  { label: 'CONTINUE?', x1: 834,  x2: 966,  color: '#e0af68' },
  { label: 'OUTPUT',    x1: 974,  x2: 1120, color: '#9ece6a' },
];

// ── Main loop nodes: cx,cy = center; hw,hh = half-width, half-height ──
const LOOP_NODES = [
  { id: 'user_input',     label: 'User Input',     type: 'input',    cx: 59,   cy: 150, hw: 52, hh: 18 },
  { id: 'load_context',   label: 'Load Context',   type: 'process',  cx: 185,  cy: 112, hw: 60, hh: 14 },
  { id: 'memory_search',  label: 'Memory Search',  type: 'process',  cx: 185,  cy: 155, hw: 60, hh: 14 },
  { id: 'build_prompt',   label: 'Build Prompt',   type: 'process',  cx: 275,  cy: 133, hw: 60, hh: 14 },
  { id: 'llm_call',       label: 'LLM Call',       type: 'llm',      cx: 390,  cy: 150, hw: 55, hh: 20 },
  { id: 'validate_tools', label: 'Validate',       type: 'process',  cx: 569,  cy: 122, hw: 62, hh: 14 },
  { id: 'guardrails',     label: 'Guardrails',     type: 'guard',    cx: 569,  cy: 165, hw: 62, hh: 14 },
  { id: 'execute_tools',  label: 'Execute Tools',  type: 'process',  cx: 749,  cy: 150, hw: 62, hh: 18 },
  { id: 'check_continue', label: 'Continue?',      type: 'decision', cx: 900,  cy: 150, hw: 58, hh: 18 },
  { id: 'final_response', label: 'Final Response', type: 'output',   cx: 1047, cy: 115, hw: 63, hh: 14 },
  { id: 'memory_save',    label: 'Memory Save',    type: 'process',  cx: 1047, cy: 162, hw: 63, hh: 14 },
];

// ── Optimizer nodes (static reference — shown below main loop) ──
const OPTIMIZER_NODES = [
  { id: 'opt_collect',  label: 'Collect',  type: 'opt', cx: 100, cy: 358, hw: 50, hh: 16,
    desc: 'Scan recent interactions' },
  { id: 'opt_analyze',  label: 'Analyze',  type: 'opt', cx: 285, cy: 358, hw: 50, hh: 16,
    desc: 'Find failure patterns' },
  { id: 'opt_propose',  label: 'Propose',  type: 'opt', cx: 470, cy: 358, hw: 54, hh: 16,
    desc: 'Generate skill changes' },
  { id: 'opt_validate', label: 'Validate', type: 'opt', cx: 658, cy: 358, hw: 54, hh: 16,
    desc: 'Test proposed changes' },
  { id: 'opt_apply',    label: 'Apply',    type: 'opt', cx: 845, cy: 358, hw: 50, hh: 16,
    desc: 'Update skills & prompts' },
];

// ── Main loop edges ──
// route flags: above (arc over routing stage), below (arc under execution),
//              loopback (deep arc below, value = arcY), vertical (straight down)
const LOOP_EDGES = [
  // User input fans out to parallel context prep nodes
  { from: 'user_input',     to: 'load_context'   },
  { from: 'user_input',     to: 'memory_search'  },
  // Parallel context nodes feed into build prompt
  { from: 'load_context',   to: 'build_prompt'   },
  { from: 'memory_search',  to: 'build_prompt'   },
  // Build prompt feeds LLM
  { from: 'build_prompt',   to: 'llm_call'       },
  // LLM → tool routing (if tools were requested)
  { from: 'llm_call',       to: 'validate_tools', label: 'tools?' },
  // LLM → continue check (skip routing when no tools called)
  { from: 'llm_call',       to: 'check_continue', label: 'no tools', above: true },
  // Tool routing pipeline
  { from: 'validate_tools', to: 'guardrails',     label: 'valid', vertical: true },
  { from: 'guardrails',     to: 'execute_tools',  label: 'pass'  },
  // Blocked: guardrail failed, skip execution
  { from: 'guardrails',     to: 'check_continue', label: 'blocked', below: true },
  // Execute → back to LLM (agentic loop)
  { from: 'execute_tools',  to: 'llm_call',       label: '↺ loop',     loopback: 245 },
  // Continue decision
  { from: 'check_continue', to: 'final_response', label: 'stop'  },
  { from: 'check_continue', to: 'llm_call',       label: '↺ continue', loopback: 278 },
  // Final output
  { from: 'final_response', to: 'memory_save',    vertical: true },
];

// ── Optimizer edges ──
const OPTIMIZER_EDGES = [
  { from: 'opt_collect',  to: 'opt_analyze'  },
  { from: 'opt_analyze',  to: 'opt_propose'  },
  { from: 'opt_propose',  to: 'opt_validate' },
  { from: 'opt_validate', to: 'opt_apply',    label: 'validated' },
  { from: 'opt_apply',    to: 'opt_collect',  loopback: 400 },
];

// ── Compute SVG path for an edge ──
function getEdgePath(edge, nodeList) {
  const src = nodeList.find(n => n.id === edge.from);
  const dst = nodeList.find(n => n.id === edge.to);
  if (!src || !dst) return null;

  // Straight vertical drop (validate→guardrails, final_response→memory_save)
  if (edge.vertical) {
    const x = src.cx;
    const y1 = src.cy + src.hh;
    const y2 = dst.cy - dst.hh;
    const labelX = x + 14;
    const labelY = (y1 + y2) / 2 + 4;
    return { d: `M ${x} ${y1} L ${x} ${y2}`, labelX, labelY };
  }

  // Arc above all tool-routing nodes (llm → check_continue, no-tools path)
  if (edge.above) {
    const arcY = 40;
    const x1 = src.cx + src.hw;
    const y1 = src.cy;
    const x2 = dst.cx - dst.hw;
    const y2 = dst.cy;
    const d = `M ${x1} ${y1} C ${x1} ${arcY}, ${x2} ${arcY}, ${x2} ${y2}`;
    const labelX = (x1 + x2) / 2;
    const labelY = arcY - 6;
    return { d, labelX, labelY };
  }

  // Arc below (guardrails → check_continue, blocked path)
  if (edge.below) {
    const arcY = 218;
    const x1 = src.cx + src.hw;
    const y1 = src.cy;
    const x2 = dst.cx - dst.hw;
    const y2 = dst.cy;
    const d = `M ${x1} ${y1} C ${x1} ${arcY}, ${x2} ${arcY}, ${x2} ${y2}`;
    const labelX = (x1 + x2) / 2;
    const labelY = arcY + 12;
    return { d, labelX, labelY };
  }

  // Loopback arc (execute→llm, continue→llm, opt cycle)
  if (edge.loopback) {
    const arcY = edge.loopback;
    const x1 = src.cx;
    const y1 = src.cy + src.hh;
    const x2 = dst.cx;
    const y2 = dst.cy + dst.hh;
    const d = `M ${x1} ${y1} C ${x1} ${arcY}, ${x2} ${arcY}, ${x2} ${y2}`;
    const labelX = (x1 + x2) / 2;
    const labelY = arcY + 11;
    return { d, labelX, labelY };
  }

  // Default: smooth S-curve left-to-right
  const x1 = src.cx + src.hw;
  const y1 = src.cy;
  const x2 = dst.cx - dst.hw;
  const y2 = dst.cy;
  const mx = (x1 + x2) / 2;
  const d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
  const labelX = mx;
  const labelY = (y1 < y2 ? y1 : y2) - 5;
  return { d, labelX, labelY };
}

// ── Tool panel state ──
let _activePanelNodeId = null;
let _activePanelEl = null;

// ── Tool metadata cache (30s TTL — avoids re-fetching on every panel open) ──
let _toolMetaCache = null;
let _toolMetaCacheTs = 0;
const TOOL_META_CACHE_MS = 30_000;

// ── Fetch all tool metadata from /admin/tools (built-ins + user skills) ──
export async function fetchAllToolMeta() {
  const now = Date.now();
  if (_toolMetaCache && (now - _toolMetaCacheTs) < TOOL_META_CACHE_MS) {
    return _toolMetaCache;
  }
  try {
    const token = localStorage.getItem('auth_token');
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const res = await fetch(apiPath('/admin/tools'), { headers });
    if (!res.ok) return [];
    const data = await res.json();
    _toolMetaCache = Array.isArray(data) ? data : [];
    _toolMetaCacheTs = now;
    return _toolMetaCache;
  } catch {
    return [];
  }
}

// ── Safely parse a JSON field that may already be an array or a JSON string ──
function _parseJsonField(val, fallback) {
  if (Array.isArray(val)) return val;
  if (typeof val === 'string') {
    try { return JSON.parse(val); } catch { return fallback; }
  }
  return fallback;
}

// ── State ──
let loopVisualActive = false;
let eventBuffer = [];
const MAX_BUFFER = 2000;

let pages = [];
let currentPageIdx = 0;
let nextTurnNum = 0;
let sessionLoaded = false;

let currentNodeStates = new Map();
let currentTurnEvents = [];

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

  pages = [];
  currentPageIdx = 0;
  nextTurnNum = 0;
  currentNodeStates = new Map();
  currentTurnEvents = [];
  sessionLoaded = false;

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
  if (eventBuffer.length >= MAX_BUFFER) eventBuffer.shift();
  eventBuffer.push(event);

  if (!loopVisualActive) return;
  processEvent(event);
}

function processEvent(event) {
  const type = event.type;
  const step = event.step;

  if (type === 'pipeline' && step === 'user_message') {
    nextTurnNum++;
    startNewPage(nextTurnNum);
  }

  if (type === 'pipeline' && step === 'turn_start' && event.turn) {
    if (!pages.find(p => p.turnNum === event.turn)) {
      startNewPage(event.turn);
    }
  }

  if (pages.length === 0 && type !== 'ping') {
    nextTurnNum = 1;
    startNewPage(1);
  }

  const nodeId = eventToNodeId(event);
  if (nodeId) {
    const state = getNodeState(event);
    currentNodeStates.set(nodeId, state);
    currentTurnEvents.push({ nodeId, state, event, ts: Date.now() });

    const page = pages[currentPageIdx];
    if (page) {
      page.nodeStates.set(nodeId, state);
      page.events.push({ nodeId, state, event, ts: Date.now() });
    }
  }

  if (currentPageIdx < pages.length) {
    if (_renderTimer) cancelAnimationFrame(_renderTimer);
    _renderTimer = requestAnimationFrame(() => {
      _renderTimer = null;
      renderPage(currentPageIdx);
      updatePageSummary(currentPageIdx);
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
  if (type === 'tool_call' || type === 'tool_result') return 'execute_tools';
  if (type === 'response') return 'final_response';
  if (type === 'error')    return 'llm_call';
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
    if (step === 'llm_call_start' || step === 'execute_start' ||
        step === 'memory_search_start' || step === 'validate_start' ||
        step === 'guardrail_check' || step === 'memory_save_start') {
      return 'active';
    }
    return 'done';
  }
  if (type === 'tool_call') return 'active';
  if (type === 'tool_result') return 'done';
  if (type === 'response') return 'done';
  return 'done';
}

function startNewPage(turnNum) {
  currentNodeStates = new Map();
  currentTurnEvents = [];

  const page = { turnNum, nodeStates: new Map(), events: [], ts: Date.now() };
  pages.push(page);
  currentPageIdx = pages.length - 1;

  renderPageButtons();
  renderPage(currentPageIdx);
  updatePageSummary(currentPageIdx);
  selectPage(currentPageIdx);
}

function selectPage(idx) {
  if (idx < 0 || idx >= pages.length) return;
  currentPageIdx = idx;

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

  container.querySelectorAll('.loop-visual-page-btn').forEach(btn => {
    btn.addEventListener('click', () => selectPage(parseInt(btn.dataset.idx)));
  });
}

// ── Scale a fixed-width diagram to fit its container ──
// Uses CSS zoom (not transform) so the scaled size affects layout flow — no overflow.
function _scaleLvDiagram(wrap, root, cw) {
  const avail = wrap.clientWidth || wrap.parentElement?.clientWidth || 0;
  const s = (avail > 0 && avail < cw) ? avail / cw : 1;
  root.style.zoom = s < 1 ? String(s) : '';
}

// ── Render a single page (turn) ──
function renderPage(idx) {
  hideToolPanel();
  const area = document.getElementById('loop-visual-graph-area');
  if (!area) return;

  const page = pages[idx];
  if (!page) {
    area.innerHTML = '<div class="loop-visual-hint">No loop data yet — waiting for agent events…</div>';
    return;
  }

  area._lvRo?.disconnect();
  area.innerHTML = '';

  const scaleWrap = document.createElement('div');
  scaleWrap.style.cssText = 'width:100%;flex-shrink:0;overflow:hidden;';
  area.appendChild(scaleWrap);

  const root = document.createElement('div');
  root.style.cssText = `position:relative;width:${CANVAS_W}px;min-height:${CANVAS_H}px;`;
  scaleWrap.appendChild(root);

  // ── SVG layer (backgrounds, arrows, labels) ──
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', CANVAS_W);
  svg.setAttribute('height', CANVAS_H);
  svg.setAttribute('viewBox', `0 0 ${CANVAS_W} ${CANVAS_H}`);
  svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:0;overflow:visible;';
  root.appendChild(svg);

  // Arrowhead markers
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  defs.innerHTML = `
    <marker id="lv-ah"       markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#3a3a5a"/></marker>
    <marker id="lv-ah-active" markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#7dcfff"/></marker>
    <marker id="lv-ah-done"  markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#9ece6a"/></marker>
    <marker id="lv-ah-opt"   markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#9ece6a50"/></marker>
  `;
  svg.appendChild(defs);

  // ── Stage column backgrounds ──
  STAGES.forEach((stage, i) => {
    // Subtle column band
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', stage.x1 + 1);
    rect.setAttribute('y', 28);
    rect.setAttribute('width', stage.x2 - stage.x1 - 2);
    rect.setAttribute('height', 252);
    rect.setAttribute('fill', i % 2 === 0 ? '#ffffff03' : '#00000008');
    rect.setAttribute('rx', '3');
    svg.appendChild(rect);

    // Left divider (skip first)
    if (i > 0) {
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', stage.x1);
      line.setAttribute('y1', 28);
      line.setAttribute('x2', stage.x1);
      line.setAttribute('y2', 280);
      line.setAttribute('stroke', '#1e2035');
      line.setAttribute('stroke-width', '1');
      svg.appendChild(line);
    }

    // Stage label
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', (stage.x1 + stage.x2) / 2);
    text.setAttribute('y', 20);
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('class', 'lv-stage-label');
    text.setAttribute('fill', stage.color);
    text.setAttribute('fill-opacity', '0.45');
    text.textContent = stage.label;
    svg.appendChild(text);
  });

  // ── Optimizer section divider & label ──
  const divLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  divLine.setAttribute('x1', 10);
  divLine.setAttribute('y1', 300);
  divLine.setAttribute('x2', CANVAS_W - 10);
  divLine.setAttribute('y2', 300);
  divLine.setAttribute('stroke', '#2a2a4a');
  divLine.setAttribute('stroke-width', '1');
  divLine.setAttribute('stroke-dasharray', '4,4');
  svg.appendChild(divLine);

  const optLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  optLabel.setAttribute('x', 14);
  optLabel.setAttribute('y', 316);
  optLabel.setAttribute('class', 'lv-stage-label');
  optLabel.setAttribute('fill', '#9ece6a');
  optLabel.setAttribute('fill-opacity', '0.45');
  optLabel.textContent = '⚙ OPTIMIZER LOOP  —  runs on a separate schedule to improve agent skills';
  svg.appendChild(optLabel);

  // ── Build edge state map from page node states ──
  const edgeStates = new Map();
  for (const edge of LOOP_EDGES) {
    const fromState = page.nodeStates.get(edge.from);
    const toState   = page.nodeStates.get(edge.to);
    const key = `${edge.from}→${edge.to}`;
    if (fromState === 'done' && (toState === 'done' || toState === 'active')) {
      edgeStates.set(key, 'done');
    } else if (fromState === 'active' || fromState === 'done') {
      edgeStates.set(key, 'active');
    }
  }

  // ── Draw main loop edges ──
  for (const edge of LOOP_EDGES) {
    const key = `${edge.from}→${edge.to}`;
    const edgeState = edgeStates.get(key) || '';
    const pi = getEdgePath(edge, LOOP_NODES);
    if (!pi) continue;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', pi.d);
    path.setAttribute('fill', 'none');

    let cls = 'lv-arrow';
    if (edge.above || edge.loopback || edge.below) cls += ' lv-arrow-alt';
    if (edgeState === 'done')   cls += ' lv-arrow-done';
    else if (edgeState === 'active') cls += ' lv-arrow-active';
    path.setAttribute('class', cls);

    const markerSuffix = edgeState === 'done' ? '-done' : edgeState === 'active' ? '-active' : '';
    path.setAttribute('marker-end', `url(#lv-ah${markerSuffix})`);
    svg.appendChild(path);

    if (edge.label) {
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', pi.labelX);
      text.setAttribute('y', pi.labelY);
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('class', edgeState ? 'lv-arrow-label lv-arrow-label-active' : 'lv-arrow-label');
      text.textContent = edge.label;
      svg.appendChild(text);
    }
  }

  // ── Draw optimizer edges (always static/dim) ──
  for (const edge of OPTIMIZER_EDGES) {
    const pi = getEdgePath(edge, OPTIMIZER_NODES);
    if (!pi) continue;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', pi.d);
    path.setAttribute('fill', 'none');
    let cls = 'lv-arrow lv-arrow-opt';
    if (edge.loopback) cls += ' lv-arrow-alt';
    path.setAttribute('class', cls);
    path.setAttribute('marker-end', 'url(#lv-ah-opt)');
    svg.appendChild(path);

    if (edge.label) {
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', pi.labelX);
      text.setAttribute('y', pi.labelY);
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('class', 'lv-arrow-label lv-arrow-label-opt');
      text.textContent = edge.label;
      svg.appendChild(text);
    }
  }

  // ── Render main loop node elements ──
  for (const nodeDef of LOOP_NODES) {
    const nodeState = page.nodeStates.get(nodeDef.id) || '';
    renderNodeEl(nodeDef, nodeState, page, root);
  }

  // ── Render optimizer node elements ──
  for (const nodeDef of OPTIMIZER_NODES) {
    renderNodeEl(nodeDef, '', null, root);
  }

  // Scale to fit, re-scale on resize
  _scaleLvDiagram(scaleWrap, root, CANVAS_W);
  area._lvRo = new ResizeObserver(() => _scaleLvDiagram(scaleWrap, root, CANVAS_W));
  area._lvRo.observe(area);
}

function renderNodeEl(nodeDef, nodeState, page, parent) {
  const node = document.createElement('div');
  node.className = `lv-node lv-type-${nodeDef.type}`;
  if (nodeState === 'active') node.classList.add('lv-active');
  else if (nodeState === 'done') node.classList.add('lv-done');
  else if (nodeState === 'error') node.classList.add('lv-error');

  node.style.left   = (nodeDef.cx - nodeDef.hw) + 'px';
  node.style.top    = (nodeDef.cy - nodeDef.hh) + 'px';
  node.style.width  = (nodeDef.hw * 2) + 'px';
  node.style.height = (nodeDef.hh * 2) + 'px';

  // Label
  const label = document.createElement('span');
  label.className = 'lv-node-label';
  label.textContent = nodeDef.label;
  node.appendChild(label);

  // Hover detail tooltip
  const detailEl = document.createElement('div');
  detailEl.className = 'lv-node-detail';

  if (page && nodeDef.type !== 'opt') {
    const nodeEvents = page.events.filter(e => e.nodeId === nodeDef.id);
    if (nodeEvents.length > 0) {
      const last = nodeEvents[nodeEvents.length - 1];
      const parts = [];
      if (last.event.duration_ms)    parts.push(`${last.event.duration_ms}ms`);
      if (last.event.input_tokens)   parts.push(`↓${last.event.input_tokens}`);
      if (last.event.output_tokens)  parts.push(`↑${last.event.output_tokens}`);
      if (last.event.model)          parts.push(last.event.model);
      if (last.event.tool)           parts.push(last.event.tool);
      if (last.event.results_count != null) parts.push(`${last.event.results_count} results`);
      detailEl.textContent = parts.length > 0
        ? parts.join(' · ')
        : `${nodeEvents.length} event${nodeEvents.length !== 1 ? 's' : ''}`;
    } else {
      detailEl.textContent = 'Waiting…';
    }
  } else if (nodeDef.desc) {
    detailEl.textContent = nodeDef.desc;
  }

  node.appendChild(detailEl);

  // ── Click: toggle tool panel ──
  node.addEventListener('click', (e) => {
    e.stopPropagation();
    if (_activePanelNodeId === nodeDef.id) {
      hideToolPanel();
    } else {
      showToolPanel(nodeDef, node, parent);
    }
  });

  // Mark clickable
  node.style.cursor = 'pointer';

  parent.appendChild(node);
}

// ── Show tool panel for a node ──
function showToolPanel(nodeDef, nodeEl, container) {
  hideToolPanel();

  // Find the current page's events for this node
  const page = pages[currentPageIdx];

  const panel = document.createElement('div');
  panel.className = 'lv-tool-panel';

  // Position: below the node, centered on it, clamped to canvas
  const PANEL_W = 310;
  let left = nodeDef.cx - PANEL_W / 2;
  let top  = nodeDef.cy + nodeDef.hh + 10;
  left = Math.max(4, Math.min(left, CANVAS_W - PANEL_W - 4));
  panel.style.left  = left + 'px';
  panel.style.top   = top  + 'px';
  panel.style.width = PANEL_W + 'px';

  // Header
  const header = document.createElement('div');
  header.className = 'lv-tool-panel-header';

  const title = document.createElement('span');
  title.className = 'lv-tool-panel-title';
  title.textContent = nodeDef.label;
  header.appendChild(title);

  const close = document.createElement('button');
  close.className = 'lv-tool-panel-close';
  close.textContent = '✕';
  close.addEventListener('click', (e) => { e.stopPropagation(); hideToolPanel(); });
  header.appendChild(close);
  panel.appendChild(header);

  // ── Build Prompt: raw LLM payload viewer ──
  if (nodeDef.id === 'build_prompt' && page) {
    const bpEvent = [...page.events].reverse().find(e => e.event.step === 'build_prompt');
    if (bpEvent) {
      const ev = bpEvent.event;
      const payloadLabel = document.createElement('div');
      payloadLabel.className = 'lv-tool-section-label';
      payloadLabel.textContent = 'LLM Payload';
      panel.appendChild(payloadLabel);

      const meta = document.createElement('div');
      meta.className = 'lv-bp-meta';
      const parts = [];
      if (ev.tool_count_in_prompt != null) parts.push(`${ev.tool_count_in_prompt} tools`);
      if (ev.brain_injected) parts.push('memory injected');
      if (ev.sections?.length) parts.push(`sections: ${ev.sections.join(', ')}`);
      meta.textContent = parts.join(' · ');
      panel.appendChild(meta);

      if (ev.system_prompt) {
        const pre = document.createElement('pre');
        pre.className = 'lv-bp-prompt';
        pre.textContent = ev.system_prompt;
        panel.appendChild(pre);
      }
    }
  }

  // ── Static items (slash commands + Settings shortcuts) ──
  const staticItems = NODE_STATIC_ITEMS[nodeDef.id] || [];
  if (staticItems.length > 0) {
    const lbl = document.createElement('div');
    lbl.className = 'lv-tool-section-label';
    lbl.textContent = 'Commands & Settings';
    panel.appendChild(lbl);

    const list = document.createElement('div');
    list.className = 'lv-tool-panel-list';
    staticItems.forEach(item => _appendToolItem(list, item));
    panel.appendChild(list);
  }

  // ── Live tools section (derived from /admin/tools stage metadata) ──
  const toolsLabel = document.createElement('div');
  toolsLabel.className = 'lv-tool-section-label lv-tool-section-live';
  toolsLabel.innerHTML = 'Tools <span class="lv-live-dot"></span>';
  panel.appendChild(toolsLabel);

  const toolsList = document.createElement('div');
  toolsList.className = 'lv-tool-panel-list';

  const loadingEl = document.createElement('div');
  loadingEl.className = 'lv-tool-panel-empty lv-tool-loading';
  loadingEl.textContent = 'Loading…';
  toolsList.appendChild(loadingEl);
  panel.appendChild(toolsList);

  container.appendChild(panel);
  _activePanelNodeId = nodeDef.id;
  _activePanelEl = panel;

  // Dismiss on outside click
  setTimeout(() => {
    document.addEventListener('click', _outsideClickHandler, { once: true });
  }, 0);

  // Fetch tool metadata and populate tools section
  fetchAllToolMeta().then(allTools => {
    const nodeTools = allTools.filter(t => {
      const stages = _parseJsonField(t.stages, []);
      return stages.includes(nodeDef.id);
    });

    toolsList.innerHTML = '';

    if (nodeTools.length === 0) {
      const none = document.createElement('div');
      none.className = 'lv-tool-panel-empty';
      none.textContent = 'No tools mapped to this stage.';
      toolsList.appendChild(none);
      return;
    }

    // Sort: skills first (user-created), then built-ins
    nodeTools.sort((a, b) => {
      const aSkill = a.source === 'skill' ? 0 : 1;
      const bSkill = b.source === 'skill' ? 0 : 1;
      return aSkill - bSkill || a.name.localeCompare(b.name);
    });

    nodeTools.forEach(t => {
      const isDestructive = t.destructive === 1 || t.destructive === true;
      const isSkill = t.source === 'skill';
      const badge = isDestructive ? 'guarded' : isSkill ? 'skill' : 'tool';
      _appendToolItem(toolsList, {
        name: t.name,
        type: badge,
        desc: t.description || '',
      });
    });
  });
}

// ── Append a single tool row to a list element ──
function _appendToolItem(listEl, tool) {
  const BADGE_LABELS = {
    command: 'cmd',
    tool:    'tool',
    guarded: '🛡 guarded',
    admin:   'admin',
    skill:   '✦ skill',
  };

  const item = document.createElement('div');
  item.className = `lv-tool-item lv-tool-${tool.type}`;

  const nameRow = document.createElement('div');
  nameRow.className = 'lv-tool-name-row';

  const badge = document.createElement('span');
  badge.className = `lv-tool-badge lv-badge-${tool.type}`;
  badge.textContent = BADGE_LABELS[tool.type] || tool.type;
  nameRow.appendChild(badge);

  const name = document.createElement('span');
  name.className = 'lv-tool-name';
  name.textContent = tool.name;
  nameRow.appendChild(name);

  item.appendChild(nameRow);

  const desc = document.createElement('div');
  desc.className = 'lv-tool-desc';
  desc.textContent = tool.desc;
  item.appendChild(desc);

  listEl.appendChild(item);
}

function _outsideClickHandler() {
  hideToolPanel();
}

function hideToolPanel() {
  if (_activePanelEl) {
    _activePanelEl.remove();
    _activePanelEl = null;
  }
  _activePanelNodeId = null;
  document.removeEventListener('click', _outsideClickHandler);
}

// ── Page summary bar (shown at bottom of graph area) ──
function updatePageSummary(idx) {
  const page = pages[idx];
  if (!page) return;

  const old = document.querySelector('.lv-page-summary');
  if (old) old.remove();

  const area = document.getElementById('loop-visual-graph-area');
  if (!area) return;

  const doneCount  = [...page.nodeStates.values()].filter(s => s === 'done').length;
  const errorCount = [...page.nodeStates.values()].filter(s => s === 'error').length;
  const totalCount = LOOP_NODES.length;

  const summary = document.createElement('div');
  summary.className = 'lv-page-summary';
  summary.innerHTML = `
    <span><span class="lv-summary-label">Turn</span> ${page.turnNum}</span>
    <span><span class="lv-summary-label">Steps</span> ${doneCount}/${totalCount}</span>
    ${errorCount > 0 ? `<span style="color:#f7768e;"><span class="lv-summary-label" style="color:#f7768e;">Errors</span> ${errorCount}</span>` : ''}
    <span><span class="lv-summary-label">Events</span> ${page.events.length}</span>
  `;
  area.appendChild(summary);
}

// ── Fetch session history from DB ──
async function fetchLoopHistory() {
  const area = document.getElementById('loop-visual-graph-area');
  const userId    = app.currentUserId;
  const sessionId = app.currentSessionId;

  if (!userId || !sessionId) {
    replayBuffer();
    return;
  }

  try {
    const url = apiPath(`/api/v1/db/stream/interactions?user_id=${encodeURIComponent(userId)}&session_id=${encodeURIComponent(sessionId)}`);
    const res  = await fetch(url);
    const data = await res.json();
    const rows = data.interactions || [];

    sessionLoaded = true;
    rows.forEach(row => {
      const events = interactionToEvents(row);
      events.forEach(ev => processEvent(ev));
    });

    replayBuffer();

    if (pages.length === 0 && area) {
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
    if (el) el.innerHTML = '<div class="loop-visual-hint">Agent loop visualizer — waiting for agent events…</div>';
  }
}

// ── Convert DB interaction row → loop events ──
function interactionToEvents(row) {
  const events = [];
  const role = row.role || 'unknown';

  if (role === 'user') {
    events.push({ type: 'pipeline', level: 'user', step: 'user_message', content: row.content || '' });
  } else if (role === 'assistant') {
    let meta = {};
    try { meta = JSON.parse(row.metadata || '{}'); } catch(e) {}
    if (meta.turn) {
      events.push({ type: 'pipeline', level: 'pipeline', step: 'turn_start', turn: meta.turn, max_turns: 10 });
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
      events.push({ type: 'pipeline', level: 'pipeline', step: 'memory_search_end',
        results_count: meta.count || contentObj.count || 0 });
    } else if (toolName === 'memory_save') {
      events.push({ type: 'pipeline', level: 'pipeline', step: 'memory_save_end',
        slug: meta.slug || toolName });
    } else {
      events.push({ type: 'tool_call', level: 'agent', tool: toolName, args: meta.input_params || {} });
      events.push({ type: 'tool_result', level: 'agent', tool: toolName, result: row.content || '',
        duration_ms: meta.duration_ms || 0,
        error: !(meta.success !== false),
        error_type: meta.error_message ? 'execution_error' : null,
        recoverable: true,
      });
    }
  }

  return events;
}

// ── Session changed (called from sessions.js) ──
export function loopVisualSessionChanged() {
  if (!loopVisualActive) return;

  const area = document.getElementById('loop-visual-graph-area');
  if (area) area.innerHTML = '<div class="loop-visual-hint">Session changed — reloading…</div>';

  pages = [];
  currentPageIdx = 0;
  nextTurnNum = 0;
  currentNodeStates = new Map();
  currentTurnEvents = [];
  sessionLoaded = false;
  eventBuffer = [];

  fetchLoopHistory();
}
