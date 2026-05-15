'use strict';

// ── Shared loop diagram data and renderer ──────────────────────────────────────
// Single source of truth for node positions, edge topology, and SVG rendering.
// Consumed by loop-visual.js (live runtime view) and agents.js (config/test view).

export const LOOP_W = 1120;
export const LOOP_H = 295;

export const LOOP_STAGES = [
  { label: 'INPUT',     x1: 0,    x2: 118,  color: '#7dcfff' },
  { label: 'CONTEXT',   x1: 126,  x2: 306,  color: '#c0caf5' },
  { label: 'INFERENCE', x1: 314,  x2: 466,  color: '#bb9af7' },
  { label: 'ROUTING',   x1: 474,  x2: 664,  color: '#e0af68' },
  { label: 'EXECUTION', x1: 672,  x2: 826,  color: '#a9b1d6' },
  { label: 'CONTINUE?', x1: 834,  x2: 966,  color: '#e0af68' },
  { label: 'OUTPUT',    x1: 974,  x2: 1120, color: '#9ece6a' },
];

// cx,cy = center; hw,hh = half-width, half-height
export const LOOP_NODES = [
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

// load_context + memory_search run in parallel, both feed build_prompt before LLM
export const LOOP_EDGES = [
  { from: 'user_input',     to: 'load_context'   },
  { from: 'user_input',     to: 'memory_search'  },
  { from: 'load_context',   to: 'build_prompt'   },
  { from: 'memory_search',  to: 'build_prompt'   },
  { from: 'build_prompt',   to: 'llm_call'       },
  { from: 'llm_call',       to: 'validate_tools', label: 'tools?' },
  { from: 'llm_call',       to: 'check_continue', label: 'no tools', above: true },
  { from: 'validate_tools', to: 'guardrails',     label: 'valid', vertical: true },
  { from: 'guardrails',     to: 'execute_tools',  label: 'pass'  },
  { from: 'guardrails',     to: 'check_continue', label: 'blocked', below: true },
  { from: 'execute_tools',  to: 'llm_call',       label: '↺ loop',     loopback: 245 },
  { from: 'check_continue', to: 'final_response', label: 'stop'  },
  { from: 'check_continue', to: 'llm_call',       label: '↺ continue', loopback: 278 },
  { from: 'final_response', to: 'memory_save',    vertical: true },
];

// ── Edge path computation (works for any nodes list) ──
export function computeEdgePath(edge, nodes) {
  const src = nodes.find(n => n.id === edge.from);
  const dst = nodes.find(n => n.id === edge.to);
  if (!src || !dst) return null;

  if (edge.vertical) {
    const x = src.cx, y1 = src.cy + src.hh, y2 = dst.cy - dst.hh;
    return { d: `M ${x} ${y1} L ${x} ${y2}`, labelX: x + 14, labelY: (y1 + y2) / 2 + 4 };
  }
  if (edge.above) {
    const arcY = 40, x1 = src.cx + src.hw, y1 = src.cy, x2 = dst.cx - dst.hw, y2 = dst.cy;
    return { d: `M ${x1} ${y1} C ${x1} ${arcY}, ${x2} ${arcY}, ${x2} ${y2}`,
             labelX: (x1 + x2) / 2, labelY: arcY - 6 };
  }
  if (edge.below) {
    const arcY = 218, x1 = src.cx + src.hw, y1 = src.cy, x2 = dst.cx - dst.hw, y2 = dst.cy;
    return { d: `M ${x1} ${y1} C ${x1} ${arcY}, ${x2} ${arcY}, ${x2} ${y2}`,
             labelX: (x1 + x2) / 2, labelY: arcY + 12 };
  }
  if (edge.loopback) {
    const arcY = edge.loopback, x1 = src.cx, y1 = src.cy + src.hh, x2 = dst.cx, y2 = dst.cy + dst.hh;
    return { d: `M ${x1} ${y1} C ${x1} ${arcY}, ${x2} ${arcY}, ${x2} ${y2}`,
             labelX: (x1 + x2) / 2, labelY: arcY + 11 };
  }
  const x1 = src.cx + src.hw, y1 = src.cy, x2 = dst.cx - dst.hw, y2 = dst.cy, mx = (x1 + x2) / 2;
  return { d: `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`,
           labelX: mx, labelY: (y1 < y2 ? y1 : y2) - 5 };
}

// ── Shared renderer ────────────────────────────────────────────────────────────
// Renders stage columns, edges, and node HTML onto containerEl.
// Returns { rootEl, svgEl } so callers can append extra content (e.g. optimizer).
//
// opts:
//   markerPrefix  — unique SVG marker ID prefix to avoid DOM clashes between instances
//   canvasH       — SVG height; defaults to LOOP_H (pass 430 for loop-visual's optimizer section)
//   getNodeDetail — (nodeDef) => string  hover detail text
//   onNodeClick   — (nodeDef, nodeEl, rootEl) => void
//   decorateNode  — (nodeDef, nodeEl) => void  called after node creation for extra classes
export function renderLoopDiagram(containerEl, nodeStates, {
  markerPrefix  = 'ld',
  canvasH       = LOOP_H,
  getNodeDetail = () => '',
  onNodeClick   = null,
  decorateNode  = null,
} = {}) {
  const root = document.createElement('div');
  root.style.cssText = `position:relative;width:${LOOP_W}px;min-height:${canvasH}px;flex-shrink:0;margin:0 auto;`;
  containerEl.appendChild(root);

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', LOOP_W);
  svg.setAttribute('height', canvasH);
  svg.setAttribute('viewBox', `0 0 ${LOOP_W} ${canvasH}`);
  svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:0;overflow:visible;';
  root.appendChild(svg);

  // Arrowhead markers — prefix keeps IDs unique when both views coexist in DOM
  const p = markerPrefix;
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  defs.innerHTML = `
    <marker id="${p}-ah"        markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#3a3a5a"/></marker>
    <marker id="${p}-ah-active" markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#7dcfff"/></marker>
    <marker id="${p}-ah-done"   markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#9ece6a"/></marker>
    <marker id="${p}-ah-opt"    markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#9ece6a50"/></marker>
  `;
  svg.appendChild(defs);

  // Stage column backgrounds and labels
  LOOP_STAGES.forEach((stage, i) => {
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', stage.x1 + 1);
    rect.setAttribute('y', 28);
    rect.setAttribute('width', stage.x2 - stage.x1 - 2);
    rect.setAttribute('height', 252);
    rect.setAttribute('fill', i % 2 === 0 ? '#ffffff03' : '#00000008');
    rect.setAttribute('rx', '3');
    svg.appendChild(rect);

    if (i > 0) {
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', stage.x1); line.setAttribute('y1', 28);
      line.setAttribute('x2', stage.x1); line.setAttribute('y2', 280);
      line.setAttribute('stroke', '#1e2035'); line.setAttribute('stroke-width', '1');
      svg.appendChild(line);
    }

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

  // Edge state map (derived from nodeStates)
  const edgeStates = new Map();
  for (const edge of LOOP_EDGES) {
    const fromState = nodeStates.get(edge.from);
    const toState   = nodeStates.get(edge.to);
    const key = `${edge.from}→${edge.to}`;
    if (fromState === 'done' && (toState === 'done' || toState === 'active')) {
      edgeStates.set(key, 'done');
    } else if (fromState === 'active' || fromState === 'done') {
      edgeStates.set(key, 'active');
    }
  }

  // Draw edges
  for (const edge of LOOP_EDGES) {
    const key = `${edge.from}→${edge.to}`;
    const edgeState = edgeStates.get(key) || '';
    const pi = computeEdgePath(edge, LOOP_NODES);
    if (!pi) continue;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', pi.d);
    path.setAttribute('fill', 'none');
    let cls = 'lv-arrow';
    if (edge.above || edge.loopback || edge.below) cls += ' lv-arrow-alt';
    if (edgeState === 'done')        cls += ' lv-arrow-done';
    else if (edgeState === 'active') cls += ' lv-arrow-active';
    path.setAttribute('class', cls);
    const mSuffix = edgeState === 'done' ? '-done' : edgeState === 'active' ? '-active' : '';
    path.setAttribute('marker-end', `url(#${p}-ah${mSuffix})`);
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

  // Render node HTML elements
  for (const nd of LOOP_NODES) {
    const state = nodeStates.get(nd.id) || '';
    const el = document.createElement('div');
    el.className = `lv-node lv-type-${nd.type}`;
    if (state === 'active')     el.classList.add('lv-active');
    else if (state === 'done')  el.classList.add('lv-done');
    else if (state === 'error') el.classList.add('lv-error');

    el.style.left   = (nd.cx - nd.hw) + 'px';
    el.style.top    = (nd.cy - nd.hh) + 'px';
    el.style.width  = (nd.hw * 2) + 'px';
    el.style.height = (nd.hh * 2) + 'px';

    const labelEl = document.createElement('span');
    labelEl.className = 'lv-node-label';
    labelEl.textContent = nd.label;
    el.appendChild(labelEl);

    const detailEl = document.createElement('div');
    detailEl.className = 'lv-node-detail';
    detailEl.textContent = getNodeDetail(nd) || '';
    el.appendChild(detailEl);

    if (decorateNode) decorateNode(nd, el);

    if (onNodeClick) {
      el.style.cursor = 'pointer';
      el.addEventListener('click', e => { e.stopPropagation(); onNodeClick(nd, el, root); });
    }

    root.appendChild(el);
  }

  return { rootEl: root, svgEl: svg };
}
