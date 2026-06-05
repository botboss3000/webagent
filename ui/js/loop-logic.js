'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { LOOP_W, LOOP_NODES, BREAKPOINT_VERTICAL, computeEdgePath, renderLoopDiagram } from './loop-diagram.js';
import { NODE_STATIC_ITEMS, OPTIMIZER_NODES, NODE_PANEL_INFO } from './loop-node-data.js';
import { interactionToEvents } from './loop-events.js';
export { NODE_PANEL_INFO } from './loop-node-data.js';

// LOOP_W imported from loop-diagram.js (used for optimizer label)

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
  try { renderRuntimeLoopSidebar(); } catch (_) {}

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
      try { renderRuntimeLoopSidebar(); } catch (_) {}
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
      case 'attachment_describe_start':
      case 'attachment_describe_end': return 'attachment_describe';

      // LOOP INIT
      case 'load_tools':            return 'load_tools';
      case 'data_src_loaded':       return 'data_src_load';
      case 'integration_status':    return 'integration_status';

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
      case 'data_src_query_started':
      case 'data_src_query_finished': return 'data_src_exec';
      case 'agent_delegation':      return 'delegation_chk';
      case 'skill_track':           return 'skill_track';

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
  try { renderRuntimeLoopSidebar(); } catch (_) {}
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
  try { renderRuntimeLoopSidebar(); } catch (_) {}
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
// interactionToEvents(row) now lives in ./loop-events.js (imported above),
// shared with loop.js so the DB-row -> loop-event mapping stays in sync.

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
  renderRuntimeLoopSidebar();
}

// ── Runtime Loop sidebar ──────────────────────────────────────────
//
// Two lists: run-history scrubber (each row replays a past page) and a
// node directory (click a row to scroll the node into view + open its
// overlay panel). Cheap to re-render — called on every page boundary
// and on selectPage.

function _rlEscape(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _rlRelativeTime(ts) {
  if (!ts) return '';
  const dt = Date.now() - ts;
  if (dt < 0) return 'now';
  const s = Math.floor(dt / 1000);
  if (s < 60) return s + 's ago';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}

function renderRunHistoryList() {
  const host = document.getElementById('rl-pages');
  if (!host) return;
  if (!pages.length) {
    host.innerHTML = '<div class="rl-empty">No runs yet</div>';
    return;
  }
  host.innerHTML = '';
  pages.forEach((p, i) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'rl-row rl-page-row' + (i === currentPageIdx ? ' active' : '');
    row.dataset.idx = String(i);
    const label = p.turnNum <= 1 ? 'Initial' : ('Turn ' + (p.turnNum - 1));
    row.innerHTML =
      '<span class="rl-row-num">#' + _rlEscape(String(p.turnNum)) + '</span>' +
      '<span class="rl-row-label">' + _rlEscape(label) + '</span>' +
      '<span class="rl-row-meta">' + (p.events ? p.events.length : 0) + ' ev · ' + _rlEscape(_rlRelativeTime(p.ts)) + '</span>';
    row.addEventListener('click', () => selectPage(i));
    host.appendChild(row);
  });
}

function renderNodesList() {
  const host = document.getElementById('rl-nodes');
  if (!host) return;
  const page = pages[currentPageIdx];
  const stateMap = (page && page.nodeStates) || currentNodeStates;
  host.innerHTML = '';
  for (const nd of LOOP_NODES) {
    const state = (stateMap && stateMap.get(nd.id)) || 'idle';
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'rl-row rl-node-row rl-state-' + _rlEscape(state);
    row.dataset.nodeId = nd.id;
    row.innerHTML =
      '<span class="rl-node-dot" data-state="' + _rlEscape(state) + '"></span>' +
      '<span class="rl-row-label">' + _rlEscape(nd.label) + '</span>' +
      '<span class="rl-row-meta">' + _rlEscape(state) + '</span>';
    row.addEventListener('click', () => _focusNode(nd.id));
    host.appendChild(row);
  }
}

// Click on a sidebar node row → scroll its graph element into view and
// open the same overlay panel the in-graph click would have. Reuses the
// existing showToolPanel / hideToolPanel functions defined above.
function _focusNode(nodeId) {
  const root = document.getElementById('loop-visual-graph-area');
  if (!root) return;
  const el = root.querySelector('.lv-node[data-id="' + (window.CSS && CSS.escape ? CSS.escape(nodeId) : nodeId) + '"]');
  if (el && typeof el.scrollIntoView === 'function') {
    el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
  }
  const nodeDef = LOOP_NODES.find((n) => n.id === nodeId);
  if (!nodeDef) return;
  // Toggle: a second click on the same row closes the panel.
  if (_activePanelNodeId === nodeId) hideToolPanel();
  else showToolPanel(nodeDef, el, root);
}

// Public re-render entry — called from startLoopVisual, startNewPage,
// selectPage and the session-changed handler.
export function renderRuntimeLoopSidebar() {
  renderRunHistoryList();
  renderNodesList();
  const btn = document.getElementById('rl-refresh');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => { try { startLoopVisual(); } catch (_) {} });
  }
}
