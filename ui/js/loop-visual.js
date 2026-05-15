'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { LOOP_W, LOOP_NODES, computeEdgePath, renderLoopDiagram } from './loop-diagram.js';

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

// ── Canvas height including optimizer section (LOOP_W imported from loop-diagram.js) ──
const CANVAS_H = 430;

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

// ── Optimizer edges ──
const OPTIMIZER_EDGES = [
  { from: 'opt_collect',  to: 'opt_analyze'  },
  { from: 'opt_analyze',  to: 'opt_propose'  },
  { from: 'opt_propose',  to: 'opt_validate' },
  { from: 'opt_validate', to: 'opt_apply',    label: 'validated' },
  { from: 'opt_apply',    to: 'opt_collect',  loopback: 400 },
];

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

  function getNodeDetail(nd) {
    const nodeEvents = page.events.filter(e => e.nodeId === nd.id);
    if (nodeEvents.length === 0) return 'Waiting…';
    const last = nodeEvents[nodeEvents.length - 1];
    const parts = [];
    if (last.event.duration_ms)          parts.push(`${last.event.duration_ms}ms`);
    if (last.event.input_tokens)         parts.push(`↓${last.event.input_tokens}`);
    if (last.event.output_tokens)        parts.push(`↑${last.event.output_tokens}`);
    if (last.event.model)                parts.push(last.event.model);
    if (last.event.tool)                 parts.push(last.event.tool);
    if (last.event.results_count != null) parts.push(`${last.event.results_count} results`);
    return parts.length > 0
      ? parts.join(' · ')
      : `${nodeEvents.length} event${nodeEvents.length !== 1 ? 's' : ''}`;
  }

  const { rootEl, svgEl } = renderLoopDiagram(scaleWrap, page.nodeStates, {
    markerPrefix: 'lv',
    canvasH: CANVAS_H,
    getNodeDetail,
    onNodeClick: (nd, el, root) => {
      if (_activePanelNodeId === nd.id) hideToolPanel();
      else showToolPanel(nd, el, root);
    },
  });

  // ── Optimizer section divider & label ──
  const divLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  divLine.setAttribute('x1', 10);
  divLine.setAttribute('y1', 300);
  divLine.setAttribute('x2', LOOP_W - 10);
  divLine.setAttribute('y2', 300);
  divLine.setAttribute('stroke', '#2a2a4a');
  divLine.setAttribute('stroke-width', '1');
  divLine.setAttribute('stroke-dasharray', '4,4');
  svgEl.appendChild(divLine);

  const optLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  optLabel.setAttribute('x', 14);
  optLabel.setAttribute('y', 316);
  optLabel.setAttribute('class', 'lv-stage-label');
  optLabel.setAttribute('fill', '#9ece6a');
  optLabel.setAttribute('fill-opacity', '0.45');
  optLabel.textContent = '⚙ OPTIMIZER LOOP  —  runs on a separate schedule to improve agent skills';
  svgEl.appendChild(optLabel);

  // ── Draw optimizer edges (always static/dim) ──
  for (const edge of OPTIMIZER_EDGES) {
    const pi = computeEdgePath(edge, OPTIMIZER_NODES);
    if (!pi) continue;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', pi.d);
    path.setAttribute('fill', 'none');
    let cls = 'lv-arrow lv-arrow-opt';
    if (edge.loopback) cls += ' lv-arrow-alt';
    path.setAttribute('class', cls);
    path.setAttribute('marker-end', 'url(#lv-ah-opt)');
    svgEl.appendChild(path);

    if (edge.label) {
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', pi.labelX);
      text.setAttribute('y', pi.labelY);
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('class', 'lv-arrow-label lv-arrow-label-opt');
      text.textContent = edge.label;
      svgEl.appendChild(text);
    }
  }

  // ── Render optimizer node elements ──
  for (const nodeDef of OPTIMIZER_NODES) {
    renderNodeEl(nodeDef, rootEl);
  }

  _scaleLvDiagram(scaleWrap, rootEl, LOOP_W);
  area._lvRo = new ResizeObserver(() => _scaleLvDiagram(scaleWrap, rootEl, LOOP_W));
  area._lvRo.observe(area);
}

// Renders a static optimizer node (no click, no live state)
function renderNodeEl(nodeDef, parent) {
  const node = document.createElement('div');
  node.className = `lv-node lv-type-${nodeDef.type}`;
  node.style.left   = (nodeDef.cx - nodeDef.hw) + 'px';
  node.style.top    = (nodeDef.cy - nodeDef.hh) + 'px';
  node.style.width  = (nodeDef.hw * 2) + 'px';
  node.style.height = (nodeDef.hh * 2) + 'px';

  const label = document.createElement('span');
  label.className = 'lv-node-label';
  label.textContent = nodeDef.label;
  node.appendChild(label);

  const detailEl = document.createElement('div');
  detailEl.className = 'lv-node-detail';
  if (nodeDef.desc) detailEl.textContent = nodeDef.desc;
  node.appendChild(detailEl);

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
  left = Math.max(4, Math.min(left, LOOP_W - PANEL_W - 4));
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
