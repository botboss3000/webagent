'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { LOOP_W, LOOP_NODES, BREAKPOINT_VERTICAL, computeEdgePath, renderLoopDiagram } from './loop-diagram.js';

// ── Static items per node (slash commands and Settings links) ──
// These cannot come from the /admin/tools endpoint — they live here.
// type: 'command' = user slash command, 'admin' = settings panel shortcut
const NODE_STATIC_ITEMS = {
  // INPUT
  user_input: [
    { name: '/optimize',             type: 'command', desc: 'Run the optimizer on this session to improve agent skills' },
    { name: '/optimize <feedback>',  type: 'command', desc: 'Run optimizer with specific feedback about what to improve' },
  ],

  // PRE-LOOP
  slash_cmd: [
    { name: '/optimize [feedback]',  type: 'command', desc: 'Intercepts /optimize before the agent loop — runs optimizer directly' },
  ],
  ensure_session: [
    { name: 'Settings → Sessions',   type: 'admin',   desc: 'Creates a new session record if one does not already exist for this request' },
  ],
  agent_resolve: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Configure agent template, system prompt, and max turns' },
  ],
  save_user_msg: [
    { name: 'Settings → Source',     type: 'admin',   desc: 'User message is persisted to the interactions table before the loop starts' },
  ],

  // CONTEXT
  load_context: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Edit context documents (agent, user, skills, tools, tasks sections)' },
  ],
  copy_defaults: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Default context is seeded from the default template on first run' },
  ],
  skip_gate: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Short/trivial messages skip memory_search entirely (regex skip gate)' },
  ],
  memory_search: [
    { name: 'Settings → Source',     type: 'admin',   desc: 'View and manage memory pages used for brain context injection' },
  ],
  build_prompt: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Edit the agent\'s core directive, persona, and context sections' },
  ],
  build_history: [
    { name: 'Settings → Source',     type: 'admin',   desc: 'View session history — internal tools (memory_search/save) are stripped' },
  ],

  // LOOP INIT
  load_provider: [
    { name: 'Settings → Provider',   type: 'admin',   desc: 'Configure LLM base URL, API key, model, and parallel providers' },
  ],
  load_tools: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Configure which tools are enabled or disabled for this agent' },
  ],
  assemble_msgs: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Assembled as: [system_prompt, ...history, {role:"user"}]' },
  ],

  // INFERENCE
  interrupt_chk: [
    { name: 'Settings → Agent',      type: 'admin',   desc: '_check_interrupt() — checked at the start of every while-loop turn' },
  ],
  turn_counter: [
    { name: 'Settings → Max Turns',  type: 'admin',   desc: 'Increments turn counter; exits loop if max_turns exceeded' },
  ],
  permission_chk: [
    { name: 'Settings → Max Turns',  type: 'admin',   desc: 'At turn 11+ the agent requests permission to continue — configurable via fragments' },
  ],
  build_tool_defs: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Converts loaded tool metadata into the LLM tool_calls schema format' },
  ],
  parallel_mode: [
    { name: 'Settings → Provider',   type: 'admin',   desc: 'Enable PARALLEL_MODE env to race multiple LLM providers simultaneously' },
  ],
  llm_call: [
    { name: 'Settings → Provider',   type: 'admin',   desc: 'Change the LLM model, base URL, or API key' },
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Edit the agent\'s system prompt and persona' },
  ],

  // ROUTING
  db_persist_asst: [
    { name: 'Settings → Source',     type: 'admin',   desc: 'Assistant message (with tool_calls suffix) is saved to DB before validation runs' },
  ],
  validate_tools: [
    { name: 'Settings → Agent',      type: 'admin',   desc: '_validate_tool_call() — checks tool name exists and args are valid' },
  ],
  destructive_chk: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'DESTRUCTIVE_TOOLS set: edit_source, write_source, delete_source, run_command, restart_server' },
  ],
  guardrails: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Destructive tools require _check_user_confirmed() before execution' },
  ],
  post_val_chk: [
    { name: 'Settings → Agent',      type: 'admin',   desc: '_check_interrupt() — checked again after the validation loop completes' },
  ],

  // EXECUTION
  execute_tools: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'Tool is dispatched to its handler; result streamed back to the loop' },
  ],
  db_persist_tool: [
    { name: 'Settings → Source',     type: 'admin',   desc: 'Tool result (role=tool) is persisted to the interactions table' },
  ],
  delegation_chk: [
    { name: 'Settings → Agent',      type: 'admin',   desc: 'If result contains __delegate__ key, agent switches mid-loop and rebinds session' },
  ],
  skill_track: [
    { name: 'Settings → Source',     type: 'admin',   desc: 'skill_track_execution + skill_get_rating run after every tool result' },
  ],

  // CONTINUE?
  check_continue: [
    { name: 'Settings → Max Turns',  type: 'admin',   desc: 'Configure the maximum number of agentic turns per request' },
  ],

  // OUTPUT
  final_response: [
    { name: '/optimize',             type: 'command', desc: 'Trigger optimizer on this session to improve future responses' },
  ],
  db_persist_final: [
    { name: 'Settings → Source',     type: 'admin',   desc: 'Final assistant response is saved to the interactions table' },
  ],
  memory_save: [
    { name: 'Settings → Source',     type: 'admin',   desc: 'View and manage memory and context documents' },
  ],
  fire_optimizer: [
    { name: 'Settings → Optimizer',  type: 'admin',   desc: 'Optimizer fires on every exit path — configure run mode and intensity' },
  ],

  // OPTIMIZER
  opt_collect: [
    { name: '/optimize [feedback]',  type: 'command', desc: 'Trigger a new optimizer run against the current session' },
    { name: 'Settings → Optimizer',  type: 'admin',   desc: 'Configure run mode, intensity, schedule, and scan scope' },
  ],
};

// LOOP_W imported from loop-diagram.js (used for optimizer label)

// ── Optimizer nodes (static reference — shown below main loop) ──
const OPTIMIZER_NODES = [
  { id: 'opt_collect',  label: 'Collect',  type: 'opt', cx: 100, cy: 458, hw: 50, hh: 16,
    desc: 'Scan recent interactions' },
  { id: 'opt_analyze',  label: 'Analyze',  type: 'opt', cx: 285, cy: 458, hw: 50, hh: 16,
    desc: 'Find failure patterns' },
  { id: 'opt_propose',  label: 'Propose',  type: 'opt', cx: 470, cy: 458, hw: 54, hh: 16,
    desc: 'Generate skill changes' },
  { id: 'opt_validate', label: 'Validate', type: 'opt', cx: 658, cy: 458, hw: 54, hh: 16,
    desc: 'Test proposed changes' },
  { id: 'opt_apply',    label: 'Apply',    type: 'opt', cx: 845, cy: 458, hw: 50, hh: 16,
    desc: 'Update skills & prompts' },
];

// ── Optimizer edges ──
const OPTIMIZER_EDGES = [
  { from: 'opt_collect',  to: 'opt_analyze'  },
  { from: 'opt_analyze',  to: 'opt_propose'  },
  { from: 'opt_propose',  to: 'opt_validate' },
  { from: 'opt_validate', to: 'opt_apply',    label: 'validated' },
  { from: 'opt_apply',    to: 'opt_collect',  loopback: 500 },
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
      // INPUT
      case 'user_message':          return 'user_input';

      // PRE-LOOP
      case 'agent_assigned':        return 'agent_resolve';

      // CONTEXT
      case 'load_context':          return 'load_context';
      case 'memory_search_skip':    return 'skip_gate';
      case 'memory_search_start':
      case 'memory_search_end':     return 'memory_search';
      case 'build_prompt':          return 'build_prompt';
      case 'attachment':            return 'resolve_attach';

      // LOOP INIT
      case 'load_tools':            return 'load_tools';

      // INFERENCE (per-turn)
      case 'turn_start':            return 'interrupt_chk';
      case 'tool_defs_built':       return 'build_tool_defs';
      case 'parallel_winner':
      case 'parallel_complete':     return 'parallel_mode';
      case 'llm_call_start':
      case 'llm_call_end':          return 'llm_call';

      // ROUTING
      case 'validate_start':
      case 'validate_result':       return 'validate_tools';
      case 'guardrail_check':       return 'destructive_chk';
      case 'guardrail_override':
      case 'guardrail_blocked':     return 'guardrails';
      case 'execute_batch_start':   return 'post_val_chk';

      // EXECUTION
      case 'execute_start':
      case 'execute_end':           return 'execute_tools';
      case 'agent_delegation':      return 'delegation_chk';

      // CONTINUE?
      case 'check_continue':
      case 'max_turns_reached':     return 'check_continue';

      // OUTPUT
      case 'memory_save_start':
      case 'memory_save_end':       return 'memory_save';

      default: return null;
    }
  }

  if (type === 'attachment') return 'resolve_attach';

  if (type === 'db') {
    const op   = event.op   || '';
    const role = event.role || '';
    if (op === 'insert_interaction' && role === 'assistant') return 'db_persist_asst';
    if (op === 'insert_interaction' && role === 'tool')      return 'db_persist_tool';
    if (op === 'skill_track')                                return 'skill_track';
    if (op === 'memory_upsert')                              return 'memory_save';
    return null;
  }

  if (type === 'tool_call')  return 'execute_tools';
  if (type === 'tool_result') {
    // memory_search tool_result maps back to memory_search node
    if (event.tool === 'memory_search') return 'memory_search';
    return 'execute_tools';
  }
  if (type === 'response')   return 'final_response';
  if (type === 'error')      return 'llm_call';
  if (type === 'interrupted') return 'check_continue';
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

  hideToolPanel(); // close panel on explicit page switch
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


// ── Render a single page (turn) ──
function renderPage(idx) {
  // NOTE: do NOT call hideToolPanel here — streaming events call renderPage on every
  // frame, which would destroy the panel immediately after the user opens it.
  // Panel is now attached to #loop-visual-container (outside area), so area rebuilds
  // don't affect it. selectPage/startNewPage call hideToolPanel explicitly on page switch.
  const area = document.getElementById('loop-visual-graph-area');
  if (!area) return;

  const page = pages[idx];
  if (!page) {
    area.innerHTML = '<div class="loop-visual-hint">No loop data yet — waiting for agent events…</div>';
    return;
  }

  const savedScroll = area.scrollTop;
  area._lvRo?.disconnect();
  area.innerHTML = '';

  const scaleWrap = document.createElement('div');
  scaleWrap.style.cssText = 'width:100%;flex-shrink:0;overflow:hidden;';
  area.appendChild(scaleWrap);

  // Measure scaleWrap (not area) — excludes area padding and accounts for scrollbar
  const availableWidth = Math.max(300, scaleWrap.clientWidth || scaleWrap.offsetWidth || LOOP_W);

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

  // Scale optimizer cx values (designed for 1000px span) to fit available width
  const OPT_BASE_CX       = [100, 285, 470, 658, 845];
  const OPT_SECTION_H     = 130;
  const optSx             = Math.min(1, availableWidth / 1000);
  const positionedOptNodes = OPTIMIZER_NODES.map((n, i) => ({ ...n, cx: Math.round(OPT_BASE_CX[i] * optSx) }));

  const { rootEl, svgEl, layout } = renderLoopDiagram(scaleWrap, page.nodeStates, {
    availableWidth,
    markerPrefix: 'lv',
    getNodeDetail,
    onNodeClick: (nd, el, root) => {
      if (_activePanelNodeId === nd.id) hideToolPanel();
      else showToolPanel(nd, el, root);
    },
  });

  // Extend SVG to include optimizer section below main loop
  const totalH = layout.canvasH + OPT_SECTION_H;
  svgEl.setAttribute('height', totalH);
  svgEl.setAttribute('viewBox', `0 0 ${layout.canvasW} ${totalH}`);
  rootEl.style.minHeight = totalH + 'px';

  const divY  = layout.canvasH + 18;
  const labY  = layout.canvasH + 34;
  const optCY = layout.canvasH + 78;

  // ── Optimizer section divider & label ──
  const divLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  divLine.setAttribute('x1', 10);
  divLine.setAttribute('y1', divY);
  divLine.setAttribute('x2', layout.canvasW - 10);
  divLine.setAttribute('y2', divY);
  divLine.setAttribute('stroke', '#2a2a4a');
  divLine.setAttribute('stroke-width', '1');
  divLine.setAttribute('stroke-dasharray', '4,4');
  svgEl.appendChild(divLine);

  const optLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  optLabel.setAttribute('x', 14);
  optLabel.setAttribute('y', labY);
  optLabel.setAttribute('class', 'lv-stage-label');
  optLabel.setAttribute('fill', '#9ece6a');
  optLabel.setAttribute('fill-opacity', '0.45');
  optLabel.textContent = '⚙ OPTIMIZER LOOP  —  runs on a separate schedule to improve agent skills';
  svgEl.appendChild(optLabel);

  // Position optimizer nodes at computed cy
  const finalOptNodes = positionedOptNodes.map(n => ({ ...n, cy: optCY }));

  // Loopback arc must be below the nodes; OPTIMIZER_EDGES has a hardcoded arcY=400
  // so patch it dynamically to optCY + node_hh + 30
  const OPT_HH = OPTIMIZER_NODES[0].hh;
  const dynOptEdges = OPTIMIZER_EDGES.map(e => e.loopback ? { ...e, loopback: optCY + OPT_HH + 30 } : e);

  // ── Draw optimizer edges (always static/dim) ──
  for (const edge of dynOptEdges) {
    const pi = computeEdgePath(edge, finalOptNodes);
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
  for (const nodeDef of finalOptNodes) {
    renderNodeEl(nodeDef, rootEl);
  }

  // Restore scroll position after browser has laid out the new content.
  // Must use rAF — setting scrollTop synchronously after innerHTML clear may be
  // ignored because the browser hasn't computed the new scroll height yet.
  if (savedScroll > 0) requestAnimationFrame(() => { area.scrollTop = savedScroll; });

  // Re-render on resize — observe parentElement, not area itself, to avoid feedback
  // loop where re-rendering area content triggers another resize observation.
  area._lvRo = new ResizeObserver(() => {
    clearTimeout(area._lvResizeTimer);
    area._lvResizeTimer = setTimeout(() => renderPage(currentPageIdx), 120);
  });
  if (area.parentElement) area._lvRo.observe(area.parentElement);
  else area._lvRo.observe(area);
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

// ── Node description & detail rows shown in the panel ──
const NODE_PANEL_INFO = {
  slash_cmd: {
    desc: 'Checks the message against _OPTIMIZE_PATTERN. If matched, routes directly to the optimizer session — the main agent loop never runs.',
    details: [
      { key: 'Pattern',  val: '_OPTIMIZE_PATTERN regex — catches /optimize and variants' },
      { key: 'On match', val: 'Session rewritten to optimizer; main loop bypassed entirely' },
      { key: 'On miss',  val: 'Falls through to ensure_session' },
    ],
  },
  ensure_session: {
    desc: '_ensure_session() — creates a new session row in the DB if none exists, or validates the provided session_id.',
    details: [
      { key: 'Table',   val: 'sessions' },
      { key: 'Creates', val: 'session_id, user_id, agent_id, created_at' },
    ],
  },
  agent_resolve: {
    desc: 'Loads the agent record from the agents table. Determines which agent template, system prompt, tools, and max_turns apply.',
    details: [
      { key: 'Table',   val: 'agents' },
      { key: 'Outputs', val: 'agent_id, system_prompt, bootstrap_tools, max_turn_count, model, temperature' },
    ],
  },
  participants: {
    desc: 'Enforces participant access rules — verifies the user is permitted to interact with this agent before any work begins.',
    details: [
      { key: 'Checks',  val: 'user_id present in agent participants list' },
      { key: 'On fail', val: 'Request rejected before any DB writes or loop execution' },
    ],
  },
  save_user_msg: {
    desc: 'Inserts the user\'s message as role="user" into the interactions table — the source-of-truth record for this turn.',
    details: [
      { key: 'Table',   val: 'interactions' },
      { key: 'Role',    val: 'user' },
      { key: 'Timing',  val: 'Before the agent loop — always written even if the agent errors out' },
    ],
  },
  load_context: {
    desc: 'Queries the agents table for context documents to inject into the system prompt. Each column maps to a named section.',
    details: [
      { key: 'Columns',  val: 'agent_prompt, user_prompt, skills_prompt, tasks_prompt, misc_prompt, system_prompt' },
      { key: 'Sections', val: 'AGENT IDENTITY · USER · SKILLS · TOOLS · TASKS · MEMORY · PROJECT' },
    ],
  },
  copy_defaults: {
    desc: 'On the agent\'s first run, copies context documents from the default template so the agent has a working starting configuration.',
    details: [
      { key: 'Trigger', val: 'No existing context rows found for this agent' },
      { key: 'Source',  val: 'Default agent template (agents table defaults)' },
    ],
  },
  skip_gate: {
    desc: '_should_skip_memory() — trivial or short messages (greetings, single commands) skip memory_search entirely to save latency.',
    details: [
      { key: 'Pattern',  val: 'Regex match on message length and content' },
      { key: 'On skip',  val: 'memory_search bypassed; brain_context = None' },
      { key: 'On pass',  val: 'Continues to memory_search normally' },
    ],
  },
  memory_search: {
    desc: 'Semantic search over the agent\'s memory pages to find relevant facts from past sessions. Results injected into the system prompt as [BRAIN CONTEXT].',
    details: [
      { key: 'Tool',     val: 'memory_search (internal — stripped from history shown to LLM)' },
      { key: 'Injects',  val: '# [BRAIN CONTEXT] section in system prompt' },
      { key: 'On empty', val: 'No brain context injected; pipeline continues normally' },
    ],
  },
  resolve_attach: {
    desc: 'Resolves attachment IDs into file metadata. Prepares a [USER ATTACHMENTS] section so the agent knows what files were uploaded.',
    details: [
      { key: 'Reads',   val: 'attachments table by attachment_id list' },
      { key: 'Injects', val: '# [USER ATTACHMENTS] — name, mime_type, size, attachment_id' },
      { key: 'Tool',    val: 'read_attachment — agent uses this to fetch file content' },
    ],
  },
  build_prompt: {
    desc: 'build_system_prompt() — assembles all context sections, brain context, and bootstrap tools into a single system prompt string.',
    details: [
      { key: 'Order',  val: '[AGENT DIRECTIVE] → [AGENT IDENTITY] → [USER] → [SKILLS] → [TOOLS] → [TASKS] → [BRAIN CONTEXT]' },
      { key: 'Source', val: 'context docs + memory results + agent.system_prompt + bootstrap_tools' },
    ],
  },
  build_history: {
    desc: 'build_openai_history_from_session() — fetches all prior interactions and converts to OpenAI message format. Strips internal tools from history.',
    details: [
      { key: 'Strips',   val: 'memory_search, memory_save (internal — never forwarded to LLM)' },
      { key: 'Rebuilds', val: 'assistant tool_calls from persisted [Tool calls: …] suffix' },
      { key: 'Output',   val: '[{role:user/assistant/tool, content:…}, …]' },
    ],
  },
  load_provider: {
    desc: 'Reads LLM provider config from the agent record and environment. Sets base_url, api_key, model, temperature, max_tokens.',
    details: [
      { key: 'Sources',  val: 'agent.model, agent.temperature, agent.max_tokens, env OPENROUTER_API_KEY / BASE_URL' },
      { key: 'Parallel', val: 'PARALLEL_MODE env enables multi-provider race (first chunk wins)' },
    ],
  },
  load_tools: {
    desc: 'Fetches tool definitions — built-in tools plus user skills — filtered by the agent\'s allowed_tools disabled list.',
    details: [
      { key: 'Sources', val: 'BUILTIN_TOOL_METADATA + skills table' },
      { key: 'Filter',  val: 'agent.allowed_tools = list of disabled tool names' },
      { key: 'Output',  val: 'List of tool dicts: name, description, parameters schema' },
    ],
  },
  assemble_msgs: {
    desc: 'Builds the final messages array for the LLM: system prompt at [0], conversation history at [1..N], current user message at [N+1].',
    details: [
      { key: '[0]',    val: '{role: "system", content: system_prompt}' },
      { key: '[1..N]', val: 'Prior session turns from build_history' },
      { key: '[N+1]',  val: '{role: "user", content: current message + any attachment context}' },
    ],
  },
  interrupt_chk: {
    desc: '_check_interrupt() — checks for a cancellation or shutdown signal. Raises AgentInterrupted if set, which triggers fire_optimizer and ends the stream.',
    details: [
      { key: 'Checked', val: '5× per turn: loop start, before LLM, in validation loop (per tool), after validation, in execution loop (per result)' },
      { key: 'On flag', val: 'Raises AgentInterrupted → fire_optimizer fires → stream ends cleanly' },
    ],
  },
  turn_counter: {
    desc: 'Increments the turn counter and checks against max_turns. If the limit is reached, forces exit to final_response.',
    details: [
      { key: 'Default',  val: 'max_turns = agent.max_turn_count (typically 10)' },
      { key: 'On limit', val: 'Exits while loop → final_response' },
      { key: 'Extended', val: '+10 turns granted if user approves the permission_chk request' },
    ],
  },
  permission_chk: {
    desc: 'At turn ≥ 11, injects a permission-request fragment asking the user if the agent may continue. Scans the last user message for approval.',
    details: [
      { key: 'Trigger',  val: 'turn_num >= 11' },
      { key: 'Approval', val: 'Scans user message for "yes", "continue", "proceed", etc.' },
      { key: 'On grant', val: 'max_turns extended by 10' },
    ],
  },
  build_tool_defs: {
    desc: 'Converts the loaded tool list into the OpenAI tool_calls schema format consumed by the LLM.',
    details: [
      { key: 'Format', val: '[{type:"function", function:{name, description, parameters:{type:"object",properties:{…}}}}]' },
      { key: 'Source', val: 'Tools loaded at load_tools stage' },
    ],
  },
  parallel_mode: {
    desc: 'If PARALLEL_MODE env is set, _race_llm_calls() races multiple LLM providers. First to return a non-empty chunk wins; losers are saved to DB.',
    details: [
      { key: 'Env',    val: 'PARALLEL_MODE=1 (comma-separated provider list)' },
      { key: 'Winner', val: 'First provider to yield a non-empty streaming chunk' },
      { key: 'Losers', val: 'Remaining responses saved to DB for optimizer analysis' },
      { key: 'Off',    val: 'Single LLM call via load_provider config' },
    ],
  },
  llm_call: {
    desc: 'The actual streaming LLM call. Yields text chunks and/or tool_call deltas. Response determines whether routing or final output follows.',
    details: [
      { key: 'Provider',  val: 'OpenRouter (or custom base_url from load_provider)' },
      { key: 'Streaming', val: 'Server-sent event chunks yielded in real time to client' },
      { key: 'Tools',     val: 'LLM may return tool_calls → db_persist_asst → validation → execution' },
      { key: 'No tools',  val: 'LLM returns plain text → check_continue → final_response' },
    ],
  },
  db_persist_asst: {
    desc: 'Saves the assistant\'s raw response as role="assistant" BEFORE validation runs. Appends a [Tool calls: …] JSON suffix listing requested tool calls.',
    details: [
      { key: 'Table',  val: 'interactions (role=assistant)' },
      { key: 'Suffix', val: 'Appends \\n\\n[Tool calls: [{name, args}, …]] to content' },
      { key: 'Timing', val: 'Immediately after LLM response — before any tool is validated or executed' },
    ],
  },
  validate_tools: {
    desc: '_validate_tool_call() — for each requested tool call, checks the tool name exists and arguments are parseable. Invalid calls get an error result without executing.',
    details: [
      { key: 'Checks',   val: 'Tool name in registry · args JSON-parseable · required params present' },
      { key: 'On fail',  val: 'Returns validation error result; tool skipped' },
      { key: 'Per call', val: 'Validated in a for-loop; interrupt_chk runs each iteration' },
    ],
  },
  destructive_chk: {
    desc: 'Checks if the requested tool is in the DESTRUCTIVE_TOOLS set. These tools require explicit user confirmation before they can execute.',
    details: [
      { key: 'Set',      val: 'edit_source · write_source · delete_source · run_command · restart_server' },
      { key: 'On match', val: 'Forwards to guardrails for confirmation check' },
      { key: 'On clear', val: 'Proceeds to post_val_chk then execution' },
    ],
  },
  guardrails: {
    desc: '_check_user_confirmed() — verifies the user explicitly approved the destructive action in their most recent message. Blocks execution if no confirmation.',
    details: [
      { key: 'Checks',   val: 'Last user message for confirmation keywords' },
      { key: 'On pass',  val: 'Tool proceeds to execution' },
      { key: 'On block', val: 'Tool skipped; emits blocked event; jumps to check_continue' },
    ],
  },
  post_val_chk: {
    desc: '_check_interrupt() again after the full validation loop completes. Catches cancellations that arrived while tools were being validated.',
    details: [
      { key: 'Timing', val: 'After the per-tool validation for-loop, before execute_tools' },
      { key: 'On flag', val: 'Raises AgentInterrupted — no tools execute' },
    ],
  },
  execute_tools: {
    desc: 'Dispatches each validated tool to its registered handler function. Streams the result back into the turn\'s tool_results list.',
    details: [
      { key: 'Dispatch', val: 'tool_name → handler lookup in tool registry' },
      { key: 'Results',  val: 'Collected into tool_results list for this turn' },
      { key: 'Errors',   val: 'Execution errors are caught and returned as error results — never raised' },
    ],
  },
  db_persist_tool: {
    desc: 'Saves each tool result as role="tool" in the interactions table. Includes execution metadata.',
    details: [
      { key: 'Table',    val: 'interactions (role=tool)' },
      { key: 'Metadata', val: 'tool_name · tool_call_id · duration_ms · success · input_params · error_message' },
    ],
  },
  delegation_chk: {
    desc: 'Checks if the tool result contains a __delegate__ key. If found, switches the active agent mid-loop and rebinds the session.',
    details: [
      { key: 'Trigger',  val: 'result contains __delegate__: {agent_id, session_id}' },
      { key: 'On match', val: 'Session rebound · tools reloaded · new system prompt injected · loop continues' },
      { key: 'On clear', val: 'Proceeds normally to skill_track' },
    ],
  },
  skill_track: {
    desc: 'After each tool result: looks up the skill by tool name, records the execution event, and updates the performance rating score.',
    details: [
      { key: 'Calls',        val: 'skill_get_id_by_name → skill_track_execution → skill_get_rating' },
      { key: 'Updates',      val: 'skill score in DB (used by optimizer to prioritize improvements)' },
      { key: 'Non-blocking', val: 'Errors swallowed — skill tracking never fails the turn' },
    ],
  },
  check_continue: {
    desc: 'Decides whether to loop back for another turn or proceed to final output. Tool results present = loop; no tool calls = stop.',
    details: [
      { key: 'Loop',  val: 'tool_results non-empty → back to interrupt_chk for turn N+1' },
      { key: 'Stop',  val: 'No tool calls in LLM response → proceed to final_response' },
      { key: 'Limit', val: 'max_turns reached → final_response regardless of tool results' },
    ],
  },
  final_response: {
    desc: 'Streams the final assistant message to the client over WebSocket. This is the text the user sees.',
    details: [
      { key: 'Delivery', val: 'Streamed as SSE chunks via WebSocket' },
      { key: 'Content',  val: 'LLM response text — tool call results already reflected in conversation history' },
    ],
  },
  db_persist_final: {
    desc: 'Saves the final assistant response to the interactions table. Distinct from db_persist_asst — that saved mid-loop tool-calling turns; this saves the concluding message.',
    details: [
      { key: 'Table', val: 'interactions (role=assistant)' },
      { key: 'When',  val: 'On clean exit — interrupt and max_turns paths are handled earlier in the loop' },
    ],
  },
  memory_save: {
    desc: 'The memory_save internal tool runs after the final response — extracts key facts from the session and upserts them into the agent\'s long-term memory store.',
    details: [
      { key: 'Tool',     val: 'memory_save (internal — stripped from history shown to LLM)' },
      { key: 'Storage',  val: 'brain / memory pages table' },
      { key: 'Optional', val: 'Skipped if the memory tool is disabled for this agent' },
    ],
  },
  fire_optimizer: {
    desc: '_fire_optimizer() — fires as a background task on every exit path. Analyzes the session and proposes skill and prompt improvements.',
    details: [
      { key: 'Mode',     val: 'Fire-and-forget (asyncio.create_task) — never blocks the response stream' },
      { key: 'Triggers', val: 'All exit paths: clean finish · max_turns · interrupt · unhandled error' },
      { key: 'Output',   val: 'Optimizer session → planner → finalizer → skill updates in DB' },
    ],
  },
};

// ── Show tool panel for a node ──
function showToolPanel(nodeDef, nodeEl, container) {
  hideToolPanel();

  // Find the current page's events for this node
  const page = pages[currentPageIdx];

  const panel = document.createElement('div');
  panel.className = 'lv-tool-panel lv-panel-overlay';

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

  // ── Node description (from NODE_PANEL_INFO) ──
  const info = NODE_PANEL_INFO[nodeDef.id];
  if (info) {
    const descLbl = document.createElement('div');
    descLbl.className = 'lv-tool-section-label';
    descLbl.textContent = 'What this does';
    panel.appendChild(descLbl);

    const descEl = document.createElement('div');
    descEl.className = 'lv-bp-meta';
    descEl.textContent = info.desc;
    panel.appendChild(descEl);

    if (info.details && info.details.length) {
      const detList = document.createElement('div');
      detList.className = 'lv-tool-panel-list';
      info.details.forEach(d => {
        const item = document.createElement('div');
        item.className = 'lv-tool-item';
        const nameEl = document.createElement('div');
        nameEl.className = 'lv-tool-name';
        nameEl.textContent = d.key;
        const valEl = document.createElement('div');
        valEl.className = 'lv-tool-desc';
        valEl.textContent = d.val;
        item.appendChild(nameEl);
        item.appendChild(valEl);
        detList.appendChild(item);
      });
      panel.appendChild(detList);
    }
  }

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

  // ── Load Context: DB query info ──
  if (nodeDef.id === 'load_context') {
    const lcLabel = document.createElement('div');
    lcLabel.className = 'lv-tool-section-label';
    lcLabel.textContent = 'Query: agents table';
    panel.appendChild(lcLabel);
    const LC_COLS = ['system_prompt', 'agent_prompt', 'user_prompt', 'skills_prompt', 'tasks_prompt', 'misc_prompt'];
    const lcList = document.createElement('div');
    lcList.className = 'lv-tool-panel-list';
    LC_COLS.forEach(col => {
      const item = document.createElement('div');
      item.className = 'lv-tool-item';
      const name = document.createElement('div');
      name.className = 'lv-tool-name';
      name.textContent = col;
      item.appendChild(name);
      lcList.appendChild(item);
    });
    panel.appendChild(lcList);
  }

  // ── Build History: session history info ──
  if (nodeDef.id === 'build_history') {
    const atLabel = document.createElement('div');
    atLabel.className = 'lv-tool-section-label';
    atLabel.textContent = 'Session history';
    panel.appendChild(atLabel);
    const atDesc = document.createElement('div');
    atDesc.className = 'lv-bp-meta';
    atDesc.textContent = 'Loads prior interactions → OpenAI messages format. Strips internal tools (memory_search / memory_save). Rebuilds assistant tool_calls from persisted [Tool calls: …] suffix.';
    panel.appendChild(atDesc);
  }

  // ── Assemble Msgs: show messages structure ──
  if (nodeDef.id === 'assemble_msgs' && page) {
    const asLabel = document.createElement('div');
    asLabel.className = 'lv-tool-section-label';
    asLabel.textContent = 'messages[ ] structure';
    panel.appendChild(asLabel);
    const bpEvent = [...page.events].reverse().find(e => e.event && e.event.step === 'build_prompt');
    const sysSnippet = bpEvent ? (bpEvent.event.system_prompt || '').slice(0, 200) : '{ system_prompt }';
    const asPre = document.createElement('pre');
    asPre.className = 'lv-bp-prompt';
    asPre.textContent = `[0] system:\n${sysSnippet}${sysSnippet.length >= 200 ? '…' : ''}\n\n[1..N] { transcript }\n\n[N+1] { current user message }`;
    panel.appendChild(asPre);
  }

  // ── Load Tools: show tool count and names from pipeline event ──
  if (nodeDef.id === 'load_tools' && page) {
    const ltEvent = [...page.events].reverse().find(e => e.event && e.event.step === 'load_tools');
    const ltLabel = document.createElement('div');
    ltLabel.className = 'lv-tool-section-label';
    ltLabel.textContent = 'Tool registry loaded';
    panel.appendChild(ltLabel);
    if (ltEvent) {
      const ev = ltEvent.event;
      const meta = document.createElement('div');
      meta.className = 'lv-bp-meta';
      meta.textContent = `${ev.count ?? '?'} tools · ${ev.duration_ms ?? '?'}ms`;
      panel.appendChild(meta);
      if (Array.isArray(ev.names) && ev.names.length) {
        const pre = document.createElement('pre');
        pre.className = 'lv-bp-prompt';
        pre.style.maxHeight = '120px';
        pre.textContent = ev.names.join('\n');
        panel.appendChild(pre);
      }
    } else {
      const none = document.createElement('div');
      none.className = 'lv-tool-panel-empty';
      none.textContent = 'No load_tools event yet for this turn.';
      panel.appendChild(none);
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

  // Clicks inside the panel must not bubble to document, or the outside-click
  // handler fires and immediately closes the panel.
  panel.addEventListener('click', e => e.stopPropagation());

  // Append to body so position:fixed;inset:0 resolves against the true viewport,
  // unaffected by any ancestor overflow:hidden, transform, or stacking context.
  document.body.appendChild(panel);
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
