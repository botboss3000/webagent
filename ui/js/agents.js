'use strict';

/**
 * Agent Management panel.
 *
 * Each agent card lives in an .agent-row wrapper.  Clicking a card toggles
 * an inline .agent-detail-panel that is appended directly to that row, so
 * multiple rows can be open simultaneously and each panel is fully
 * independent.  There is no shared / floating panel element.
 */

import { app } from './state.js';
import { fetchAllToolMeta } from './loop-visual.js';

// ── State ─────────────────────────────────────────────────────────────────────
let _agents         = [];   // full list from server
let _expandedAgents = new Map(); // Map<agentId, { tab: string }>
let _userIsAdmin    = false;
let _defaultAgentId = null;

// Tool descriptions for the Tools tab (matches BUILTIN_TOOL_METADATA keys)
const TOOL_DESCRIPTIONS = {
  list_tools:                   'Discover available tools at runtime',
  search_tools:                 'Search for tools by keyword',
  get_tool_definition:          'Retrieve full definition and parameters for a tool',
  web_search:                   'Search the web for current information',
  browser_action:               'Interact with live web pages (click, type, screenshot)',
  http_request:                 'Make arbitrary HTTP requests to external APIs',
  db_query:                     'Query or modify the database directly',
  list_agent_context_documents: 'List the context documents for this agent',
  get_agent_context_document:   'Read a specific context document',
  update_agent_context_document:'Update a context document',
  insert_agent_context_document:'Insert a new context document',
  memory:                       'Store and retrieve information across sessions',
  session_search:               'Search past session interactions',
  get_time:                     'Get the current time',
  get_date:                     'Get today\'s date',
  get_weather:                  'Get current weather for a location',
  calculate:                    'Evaluate a mathematical expression',
  read_attachment:              'Read the contents of an uploaded file',
  create_tool:                  'Create a new tool and save it to the database',
  rate_skill:                   'Rate a skill after user feedback',
  register_webhook:             'Register an inbound webhook',
  list_webhooks:                'List registered webhooks',
  delete_webhook:               'Delete a webhook registration',
  get_webhook_log:              'View recent webhook event log',
  run_optimizer:                'Trigger the optimizer pipeline for this session',
  run_worker_trials:            'Run simulated trial conversations (Optimizer Planner only)',
  handoff_to_finalizer:         'Hand off optimization results to the Finalizer',
  deploy_optimization:          'Deploy an approved optimization change (Finalizer only)',
  read_source:                  'Read any file on the server filesystem',
  write_source:                 'Create or overwrite a file (with backup)',
  edit_source:                  'Replace exact text in a file',
  delete_source:                'Delete a file or directory',
  run_command:                  'Execute a shell command on the server',
  restart_server:               'Restart the webAgent server process',
  register_user:                'Register a new user from a communication channel',
};

// Tools available per agent type
const ADMIN_TOOLS = ['read_source','write_source','edit_source','delete_source','run_command','restart_server'];
const PIPELINE_TOOLS = {
  opt_planner:   ['run_worker_trials','handoff_to_finalizer'],
  opt_finalizer: ['deploy_optimization'],
};
const BASE_TOOLS = [
  'list_tools','search_tools','get_tool_definition',
  'web_search','browser_action','http_request',
  'db_query','list_agent_context_documents','get_agent_context_document',
  'update_agent_context_document','insert_agent_context_document',
  'memory','session_search',
  'get_time','get_date','get_weather','calculate','read_attachment',
  'create_tool','rate_skill',
  'register_webhook','list_webhooks','delete_webhook','get_webhook_log',
  'run_optimizer',
];
const DESTRUCTIVE = new Set([
  'db_query','update_agent_context_document','create_tool',
  'delete_webhook','write_source','edit_source','delete_source','run_command','restart_server',
]);

function _toolsForAgent(agent) {
  const id = agent.id || '';
  if (id === 'admin-agent') return [...BASE_TOOLS, ...ADMIN_TOOLS];
  if (id === 'opt_planner')   return [...BASE_TOOLS, ...(PIPELINE_TOOLS.opt_planner || [])];
  if (id === 'opt_finalizer') return [...BASE_TOOLS, ...(PIPELINE_TOOLS.opt_finalizer || [])];
  return [...BASE_TOOLS];
}

// ── Init ──────────────────────────────────────────────────────────────────────

export async function initAgents() {
  if (!app.currentUserId) return;
  await _loadProfile();
  await _loadAgents();
  _renderList();
  _bindCreateModal();
  _restoreViewState();
}

export function startAgents() {
  if (!app.currentUserId) return;
  initAgents();
}

export function stopAgents() {
  // nothing persistent to tear down
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function _loadProfile() {
  try {
    const res = await fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(app.currentUserId)}`);
    if (res.ok) {
      const data = await res.json();
      _userIsAdmin    = !!data.is_admin;
      _defaultAgentId = data.default_agent_id || 'default';
    }
  } catch (e) {
    console.warn('agents: could not load profile', e);
  }
}

async function _loadAgents() {
  try {
    const res = await fetch(`/api/v1/agents?user_id=${encodeURIComponent(app.currentUserId)}`);
    if (res.ok) {
      const data = await res.json();
      _agents = data.agents || [];
    }
  } catch (e) {
    console.warn('agents: could not load agent list', e);
  }
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function _iconColor(agent) {
  if (agent.access_level === 'admin_only') return 'color-red';
  const id = (agent.id || '').toLowerCase();
  if (id.includes('planner') || id.includes('finalizer') || id.includes('opt')) return 'color-purple';
  if (agent.source === 'custom') return 'color-blue';
  return 'color-teal';
}

function _timeAgo(iso) {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60)    return 'just now';
  if (diff < 3600)  return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

function _displayName(agent) {
  return agent.name || (agent.source === 'custom' ? 'Fallback Name' : agent.id);
}

function _renderList() {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;

  grid.innerHTML = '';

  if (_agents.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'agents-empty';
    empty.textContent = 'No agents assigned to this user yet.';
    grid.appendChild(empty);
    return;
  }

  for (const agent of _agents) {
    const isExpanded = _expandedAgents.has(agent.id);
    const isDefault  = agent.is_user_default || agent.id === _defaultAgentId;

    const badgeType  = agent.access_level === 'admin_only' ? 'admin'
                     : agent.source === 'custom'            ? 'custom'
                     : 'system';
    const badgeLabel = agent.access_level === 'admin_only' ? 'Admin'
                     : agent.source === 'custom'            ? 'Custom'
                     : 'System';

    const toolCount = _toolsForAgent(agent).length;
    const temp      = agent.temperature != null ? agent.temperature : '—';
    const turns     = agent.max_turn_count || '—';
    const model     = agent.model || '';
    const timeAgo   = _timeAgo(agent.updated_at || agent.created_at || '');

    const canDefault = agent.can_be_default !== false && agent.can_be_default !== 0;
    const isCustom   = agent.source === 'custom';

    const card = document.createElement('div');
    card.className = 'agent-card' + (isExpanded ? ' active' : '');
    card.innerHTML = `
      <div class="agent-card-top">
        <div class="agent-card-icon-wrap ${_iconColor(agent)}">
          ${agent.icon || '🤖'}
        </div>
        <div class="agent-card-meta">
          <div class="agent-card-name-row">
            <span class="agent-card-name">${_esc(_displayName(agent))}</span>
            <span class="agent-status-dot"></span>
          </div>
          ${model ? `<div class="agent-card-model">${_esc(model)}</div>` : ''}
        </div>
        <div class="agent-card-badge-wrap">
          <span class="agent-badge ${badgeType}">${badgeLabel}</span>
          ${isDefault ? '<span class="agent-badge default">Default</span>' : ''}
          ${canDefault && !isDefault ? '<button class="agent-card-action-btn set-default-btn">Set Default</button>' : ''}
          ${isCustom ? '<button class="agent-card-action-btn delete-btn">Delete</button>' : ''}
        </div>
      </div>
      ${agent.description ? `<div class="agent-card-desc">${_esc(agent.description)}</div>` : ''}
      <div class="agent-card-stats">
        <span class="agent-stat"><span class="agent-stat-icon">↻</span>${turns} turns</span>
        <span class="agent-stat"><span class="agent-stat-icon">⋮</span>${temp}</span>
        <span class="agent-stat"><span class="agent-stat-icon">🔧</span>${toolCount} tools</span>
        ${timeAgo ? `<span class="agent-stat agent-stat-time"><span class="agent-stat-icon">🕐</span>${timeAgo}</span>` : ''}
      </div>
    `;

    // Wire inline action buttons — stopPropagation so click doesn't toggle the panel
    const setDefaultBtn = card.querySelector('.set-default-btn');
    if (setDefaultBtn) {
      setDefaultBtn.addEventListener('click', e => { e.stopPropagation(); _setDefault(agent); });
    }
    const deleteBtn = card.querySelector('.delete-btn');
    if (deleteBtn) {
      deleteBtn.addEventListener('click', e => { e.stopPropagation(); _deleteAgent(agent); });
    }

    card.addEventListener('click', () => _selectAgent(agent));

    // Each agent gets its own .agent-row; the detail panel lives inside it
    const row = document.createElement('div');
    row.className = 'agent-row';
    row.dataset.agentId = agent.id;
    row.appendChild(card);

    if (isExpanded) {
      row.appendChild(_buildDetailPanel(agent));
    }

    grid.appendChild(row);
  }
}

// ── Selection / toggle ────────────────────────────────────────────────────────

function _selectAgent(agent) {
  if (_expandedAgents.has(agent.id)) {
    _expandedAgents.delete(agent.id);
  } else {
    _expandedAgents.set(agent.id, { tab: 'config' });
  }
  _renderList();
  _saveViewState();
}

// ── Per-row detail panel ──────────────────────────────────────────────────────

function _buildDetailPanel(agent) {
  const state = _expandedAgents.get(agent.id);
  const activeTab = state?.tab || 'config';

  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel';
  panel.dataset.agentId = agent.id;

  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  panel.appendChild(content);

  // Tab bar
  const tabBar = document.createElement('div');
  tabBar.className = 'agent-detail-tabs';
  for (const [key, label] of [['config','Config'],['tools','Tools'],['test','Agent Loop']]) {
    const btn = document.createElement('button');
    btn.className = 'agents-detail-tab' + (activeTab === key ? ' active' : '');
    btn.dataset.tab = key;
    btn.textContent = label;
    btn.addEventListener('click', () => {
      const entry = _expandedAgents.get(agent.id);
      if (entry) entry.tab = key;
      _renderPanelBody(agent, panel);
      _saveViewState();
    });
    tabBar.appendChild(btn);
  }
  content.appendChild(tabBar);

  // Scrollable body
  const body = document.createElement('div');
  body.className = 'agent-detail-body';
  content.appendChild(body);

  // Render initial tab content
  _renderPanelBody(agent, panel);

  return panel;
}

function _renderPanelBody(agent, panelEl) {
  const state = _expandedAgents.get(agent.id);
  const tab   = state?.tab || 'config';

  // Sync tab-button active states
  panelEl.querySelectorAll('.agents-detail-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });

  const body = panelEl.querySelector('.agent-detail-body');
  if (!body) return;
  body.innerHTML = '';

  // Remove any previously appended save bar
  const content   = panelEl.querySelector('.agent-detail-content');
  const oldSaveBar = content ? content.querySelector(':scope > .agents-save-bar') : null;
  if (oldSaveBar) oldSaveBar.remove();

  if (tab === 'config')       _renderConfigTab(body, agent, panelEl);
  else if (tab === 'tools')   _renderToolsTab(body, agent);
  else if (tab === 'test')    _renderTestTab(body, agent);
}

// ── Config tab ────────────────────────────────────────────────────────────────

function _renderConfigTab(body, agent, panelEl) {
  const isEditable = agent.source === 'custom';

  // Name + description (editable for custom agents only)
  if (isEditable) {
    _addField(body, 'Name', 'agents-input', 'name',
      agent.name || (agent.source === 'custom' ? 'autoAgent' : ''), false);
    _addField(body, 'Description', 'agents-textarea', 'desc',
      agent.description || '', false, 2);
  }

  // Turn count
  const tcGroup = document.createElement('div');
  tcGroup.className = 'agents-field-group';
  tcGroup.innerHTML = `
    <label class="agents-field-label">Max Turn Count</label>
    <span class="agents-field-hint">Maximum number of tool-calling turns per session.</span>
    <input type="number" class="agents-input" data-field="max_turn_count"
      value="${agent.max_turn_count || 10}" min="1" max="99999"
      ${!isEditable ? 'readonly' : ''} style="width:100px">
  `;
  body.appendChild(tcGroup);

  // Five prompt sections
  const FIELDS = [
    { key: 'agent_prompt',  label: 'Identity & Personality',  hint: "Defines this agent's character, tone, and core operating style." },
    { key: 'user_prompt',   label: 'User Preferences',        hint: 'What this agent knows about the user — their preferences and context.' },
    { key: 'skills_prompt', label: 'Skills & Tools Guidance', hint: 'Which capabilities this agent should focus on and how to use them.' },
    { key: 'tasks_prompt',  label: 'Task Workflows',          hint: 'How this agent handles common task types step-by-step.' },
    { key: 'misc_prompt',   label: 'Miscellaneous Context',   hint: 'Any additional guidance or context for this agent.' },
  ];
  for (const f of FIELDS) {
    _addField(body, f.label, 'agents-textarea', f.key,
      agent[f.key] || '', !isEditable, 6, f.hint);
  }

  // Save bar (sticky at bottom of content — outside the scrollable body)
  if (isEditable) {
    const content = panelEl.querySelector('.agent-detail-content');
    const bar = document.createElement('div');
    bar.className = 'agents-save-bar';
    const saveBtn = _btn('Save Changes', 'agents-btn primary');
    const msg = document.createElement('span');
    msg.className = 'agents-save-msg';
    saveBtn.addEventListener('click', () => _saveChanges(agent, bar, panelEl));
    bar.appendChild(saveBtn);
    bar.appendChild(msg);
    if (content) content.appendChild(bar);
  }
}

function _addField(container, label, tag, fieldKey, value, readonly, rows = 4, hint = '') {
  const group = document.createElement('div');
  group.className = 'agents-field-group';
  const labelEl = document.createElement('label');
  labelEl.className = 'agents-field-label';
  labelEl.textContent = label;
  group.appendChild(labelEl);
  if (hint) {
    const hintEl = document.createElement('span');
    hintEl.className = 'agents-field-hint';
    hintEl.textContent = hint;
    group.appendChild(hintEl);
  }
  const el = document.createElement(tag === 'agents-textarea' ? 'textarea' : 'input');
  el.className = tag;
  el.dataset.field = fieldKey; // scope by data-field so multiple panels don't conflict
  if (tag === 'agents-textarea') {
    el.rows = rows;
    el.value = value;
  } else {
    el.type = 'text';
    el.value = value;
  }
  if (readonly) el.readOnly = true;
  group.appendChild(el);
  container.appendChild(group);
}

// ── Tools tab ─────────────────────────────────────────────────────────────────

function _renderToolsTab(body, agent) {
  const tools = _toolsForAgent(agent);
  const section = document.createElement('div');
  section.className = 'agents-tools-list';

  const intro = document.createElement('div');
  intro.style.cssText = 'font-size:12px;color:#565f89;margin-bottom:14px;line-height:1.5;';
  intro.textContent = `This agent has access to ${tools.length} tools. Tools marked destructive can modify data or execute code.`;
  section.appendChild(intro);

  for (const name of tools) {
    const item = document.createElement('div');
    item.className = 'agents-tool-item';
    const isDestructive = DESTRUCTIVE.has(name);
    item.innerHTML = `
      <span class="agents-tool-name">${name}</span>
      <span class="agents-tool-desc">${_esc(TOOL_DESCRIPTIONS[name] || '')}</span>
      <span class="agents-tool-badge ${isDestructive ? 'destructive' : 'safe'}">
        ${isDestructive ? 'write' : 'read-only'}
      </span>
    `;
    section.appendChild(item);
  }

  body.appendChild(section);
}

// ── Agent Loop (Test) tab ─────────────────────────────────────────────────────

function _renderTestTab(body, agent) {
  const area = document.createElement('div');
  area.className = 'agents-test-area';
  area.innerHTML = `
    <div class="agents-test-input-row">
      <input class="agents-input agents-test-input" placeholder="Type a test message and press Run to live-test this pipeline…" />
      <button class="agents-btn primary agents-test-run">Run</button>
    </div>
    <div class="agents-test-status"></div>
    <div class="agents-test-loop"></div>
  `;

  const input  = area.querySelector('.agents-test-input');
  const runBtn = area.querySelector('.agents-test-run');
  const loopEl = area.querySelector('.agents-test-loop');

  runBtn.addEventListener('click', () => _runTest(agent, area));
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _runTest(agent, area); }
  });

  // Render static blueprint immediately
  _drawAgentLoopDiagram(loopEl, new Map(), agent);

  body.appendChild(area);
}

async function _runTest(agent, areaEl) {
  const input  = areaEl.querySelector('.agents-test-input');
  const status = areaEl.querySelector('.agents-test-status');
  const loopEl = areaEl.querySelector('.agents-test-loop');
  if (!input || !status || !loopEl) return;

  const msg = input.value.trim();
  if (!msg) return;

  status.textContent = '⏳ Running…';
  _drawAgentLoopDiagram(loopEl, new Map([
    ['user_input', 'active'], ['load_context', 'active'],
    ['memory_search', 'active'], ['build_prompt', 'active'], ['llm_call', 'active'],
  ]), agent);

  const resetToBlueprint = () => {
    status.textContent = '';
    _drawAgentLoopDiagram(loopEl, new Map(), agent);
  };

  try {
    const res = await fetch('/api/v1/agents/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.currentUserId, agent_id: agent.id, message: msg }),
    });
    const data = await res.json();

    if (!res.ok) {
      _drawAgentLoopDiagram(loopEl, new Map([['llm_call', 'error']]), agent);
      status.innerHTML = `Error ${res.status}: ${_esc(data.detail || 'unknown')} &nbsp;`;
      const resetBtn = _backBtn(); resetBtn.addEventListener('click', resetToBlueprint);
      status.appendChild(resetBtn);
      return;
    }

    const rows = data.interactions || [];
    _drawAgentLoopDiagram(loopEl, _interactionsToNodeStates(rows), agent);
    status.innerHTML = `✓ Complete — ${rows.length} step(s) &nbsp;`;
    const resetBtn = _backBtn(); resetBtn.addEventListener('click', resetToBlueprint);
    status.appendChild(resetBtn);
  } catch (e) {
    _drawAgentLoopDiagram(loopEl, new Map([['llm_call', 'error']]), agent);
    status.innerHTML = `Error: ${_esc(e.message)} &nbsp;`;
    const resetBtn = _backBtn(); resetBtn.addEventListener('click', resetToBlueprint);
    status.appendChild(resetBtn);
  }
}

function _backBtn() {
  const b = document.createElement('button');
  b.className = 'agents-blueprint-back-inline';
  b.textContent = '← Blueprint';
  return b;
}

// ── Agent loop diagram — matches the dedicated Loop View tab ──────────────────

const _LV_W = 1120;
const _LV_H = 295;

const _LV_STAGES = [
  { label: 'INPUT',     x1: 0,    x2: 118,  color: '#7dcfff' },
  { label: 'CONTEXT',   x1: 126,  x2: 306,  color: '#c0caf5' },
  { label: 'INFERENCE', x1: 314,  x2: 466,  color: '#bb9af7' },
  { label: 'ROUTING',   x1: 474,  x2: 664,  color: '#e0af68' },
  { label: 'EXECUTION', x1: 672,  x2: 826,  color: '#a9b1d6' },
  { label: 'CONTINUE?', x1: 834,  x2: 966,  color: '#e0af68' },
  { label: 'OUTPUT',    x1: 974,  x2: 1120, color: '#9ece6a' },
];

const _LV_NODES = [
  { id: 'user_input',     label: 'User Input',     type: 'input',    cx: 59,   cy: 150, hw: 52, hh: 18 },
  { id: 'load_context',   label: 'Load Context',   type: 'process',  cx: 216,  cy: 112, hw: 62, hh: 14 },
  { id: 'memory_search',  label: 'Memory Search',  type: 'process',  cx: 216,  cy: 150, hw: 62, hh: 14 },
  { id: 'build_prompt',   label: 'Build Prompt',   type: 'process',  cx: 216,  cy: 188, hw: 62, hh: 14 },
  { id: 'llm_call',       label: 'LLM Call',       type: 'llm',      cx: 390,  cy: 150, hw: 55, hh: 20 },
  { id: 'validate_tools', label: 'Validate',       type: 'process',  cx: 569,  cy: 122, hw: 62, hh: 14 },
  { id: 'guardrails',     label: 'Guardrails',     type: 'guard',    cx: 569,  cy: 165, hw: 62, hh: 14 },
  { id: 'execute_tools',  label: 'Execute Tools',  type: 'process',  cx: 749,  cy: 150, hw: 62, hh: 18 },
  { id: 'check_continue', label: 'Continue?',      type: 'decision', cx: 900,  cy: 150, hw: 58, hh: 18 },
  { id: 'final_response', label: 'Final Response', type: 'output',   cx: 1047, cy: 115, hw: 63, hh: 14 },
  { id: 'memory_save',    label: 'Memory Save',    type: 'process',  cx: 1047, cy: 162, hw: 63, hh: 14 },
];

const _LV_EDGES = [
  { from: 'user_input',     to: 'load_context'   },
  { from: 'user_input',     to: 'memory_search'  },
  { from: 'user_input',     to: 'build_prompt'   },
  { from: 'load_context',   to: 'llm_call'       },
  { from: 'memory_search',  to: 'llm_call'       },
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

function _lvEdgePath(edge) {
  const src = _LV_NODES.find(n => n.id === edge.from);
  const dst = _LV_NODES.find(n => n.id === edge.to);
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

// One active node-info panel at a time (shared across all loop diagrams)
let _lvActivePanelEl     = null;
let _lvActivePanelNodeId = null;

function _lvHidePanel() {
  if (_lvActivePanelEl) { _lvActivePanelEl.remove(); _lvActivePanelEl = null; }
  _lvActivePanelNodeId = null;
  document.removeEventListener('click', _lvHidePanel);
}

/**
 * Draw the same horizontal swimlane diagram shown in the dedicated Loop View.
 * nodeStates = Map<nodeId, 'active'|'done'|'error'>  — new Map() = static blueprint.
 * agent is passed for tool-list filtering and node hints.
 */
function _drawAgentLoopDiagram(loopEl, nodeStates, agent) {
  loopEl.innerHTML = '';
  _lvHidePanel();

  const root = document.createElement('div');
  root.style.cssText = `position:relative;width:${_LV_W}px;min-height:${_LV_H}px;flex-shrink:0;`;
  loopEl.appendChild(root);

  // ── SVG layer ──
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width',   _LV_W);
  svg.setAttribute('height',  _LV_H);
  svg.setAttribute('viewBox', `0 0 ${_LV_W} ${_LV_H}`);
  svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:0;overflow:visible;';
  root.appendChild(svg);

  // Arrowhead markers (unique IDs so they don't clash with the loop-view tab)
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  defs.innerHTML = `
    <marker id="ag-ah"        markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#3a3a5a"/></marker>
    <marker id="ag-ah-active" markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#7dcfff"/></marker>
    <marker id="ag-ah-done"   markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto"><polygon points="0 0,7 2.5,0 5" fill="#9ece6a"/></marker>
  `;
  svg.appendChild(defs);

  // Stage column backgrounds, dividers, labels
  _LV_STAGES.forEach((stage, i) => {
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', stage.x1 + 1);
    rect.setAttribute('y', 28);
    rect.setAttribute('width',  stage.x2 - stage.x1 - 2);
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

  // Edges
  for (const edge of _LV_EDGES) {
    const fromState = nodeStates.get(edge.from);
    const toState   = nodeStates.get(edge.to);
    let edgeState = '';
    if (fromState === 'done' && (toState === 'done' || toState === 'active')) edgeState = 'done';
    else if (fromState === 'active' || fromState === 'done') edgeState = 'active';

    const pi = _lvEdgePath(edge);
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
    path.setAttribute('marker-end', `url(#ag-ah${mSuffix})`);
    svg.appendChild(path);

    if (edge.label) {
      const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      lbl.setAttribute('x', pi.labelX); lbl.setAttribute('y', pi.labelY);
      lbl.setAttribute('text-anchor', 'middle');
      lbl.setAttribute('class', edgeState ? 'lv-arrow-label lv-arrow-label-active' : 'lv-arrow-label');
      lbl.textContent = edge.label;
      svg.appendChild(lbl);
    }
  }

  // HTML nodes (absolutely positioned on top of SVG)
  for (const nd of _LV_NODES) {
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
    el.style.cursor = 'pointer';

    const labelEl = document.createElement('span');
    labelEl.className = 'lv-node-label';
    labelEl.textContent = nd.label;
    el.appendChild(labelEl);

    const detail = document.createElement('div');
    detail.className = 'lv-node-detail';
    detail.textContent = _lvNodeHint(nd, agent);
    el.appendChild(detail);

    el.addEventListener('click', e => {
      e.stopPropagation();
      if (_lvActivePanelNodeId === nd.id) { _lvHidePanel(); return; }
      _lvShowPanel(nd, el, root, agent);
    });

    root.appendChild(el);
  }

  // Dismiss panel on click outside nodes
  root.addEventListener('click', () => _lvHidePanel());
}

function _lvNodeHint(nd, agent) {
  if (!agent) return '';
  switch (nd.id) {
    case 'user_input':     return 'User message enters the pipeline';
    case 'load_context': {
      const pf = ['agent_prompt','user_prompt','skills_prompt','tasks_prompt','misc_prompt'];
      const n = pf.filter(f => agent[f] && String(agent[f]).trim()).length;
      return `${n} of 5 prompt sections configured`;
    }
    case 'memory_search':  return 'Semantic search over past interactions';
    case 'build_prompt':   return 'Assembles system prompt from context sections';
    case 'llm_call':       return `Model: ${agent.model || 'claude-3-5-sonnet'}`;
    case 'validate_tools': return 'Validates requested tool calls';
    case 'guardrails':     return 'Safety checks before tool execution';
    case 'execute_tools':  return `${_toolsForAgent(agent).length} tools — click for list`;
    case 'check_continue': return `Max turns: ${agent.max_turn_count || 10}`;
    case 'final_response': return 'Final reply delivered to user';
    case 'memory_save':    return 'Key facts stored for future sessions';
    default: return '';
  }
}

// Static items per node (slash commands + settings shortcuts) — mirrors loop-visual.js
const _LV_NODE_STATIC_ITEMS = {
  user_input: [
    { name: '/optimize',            type: 'command', desc: 'Run the optimizer on this session to improve agent skills' },
    { name: '/optimize <feedback>', type: 'command', desc: 'Run optimizer with specific feedback about what to improve' },
  ],
  build_prompt: [
    { name: 'Settings → Agent',     type: 'admin',   desc: "Edit this agent's core directive and persona" },
  ],
  llm_call: [
    { name: 'Settings → Provider',  type: 'admin',   desc: 'Change the LLM model, base URL, or API key' },
    { name: 'Settings → Agent',     type: 'admin',   desc: "Edit this agent's system prompt and persona" },
  ],
  check_continue: [
    { name: 'Settings → Max Turns', type: 'admin',   desc: 'Configure the maximum number of agentic turns per request' },
  ],
  final_response: [
    { name: '/optimize',            type: 'command', desc: 'Trigger optimizer on this session to improve future responses' },
  ],
  memory_save: [
    { name: 'Settings → Source',    type: 'admin',   desc: 'View and manage memory and context documents' },
  ],
};

function _lvShowPanel(nd, nodeEl, container, agent) {
  _lvHidePanel();

  const PANEL_W = 310;
  let left = nd.cx - PANEL_W / 2;
  const top = nd.cy + nd.hh + 10;
  left = Math.max(4, Math.min(left, _LV_W - PANEL_W - 4));

  const panel = document.createElement('div');
  panel.className = 'lv-tool-panel';
  panel.style.cssText = `left:${left}px;top:${top}px;width:${PANEL_W}px;`;

  // Header
  const header = document.createElement('div');
  header.className = 'lv-tool-panel-header';
  const title = document.createElement('span');
  title.className = 'lv-tool-panel-title';
  title.textContent = nd.label;
  const close = document.createElement('button');
  close.className = 'lv-tool-panel-close';
  close.textContent = '✕';
  close.addEventListener('click', e => { e.stopPropagation(); _lvHidePanel(); });
  header.appendChild(title);
  header.appendChild(close);
  panel.appendChild(header);

  // Static items (slash commands + settings shortcuts)
  const staticItems = _LV_NODE_STATIC_ITEMS[nd.id] || [];
  if (staticItems.length > 0) {
    const lbl = document.createElement('div');
    lbl.className = 'lv-tool-section-label';
    lbl.textContent = 'Commands & Settings';
    panel.appendChild(lbl);
    const staticList = document.createElement('div');
    staticList.className = 'lv-tool-panel-list';
    staticItems.forEach(item => _lvAppendItem(staticList, item));
    panel.appendChild(staticList);
  }

  // Live tools section — real DB data filtered to this node's stage AND this agent's tools
  const agentToolNames = new Set(_toolsForAgent(agent));

  if (nd.id === 'load_context' || nd.id === 'build_prompt') {
    const lbl = document.createElement('div');
    lbl.className = 'lv-tool-section-label';
    lbl.textContent = 'Prompt Sections';
    panel.appendChild(lbl);
    const ctxList = document.createElement('div');
    ctxList.className = 'lv-tool-panel-list';
    [
      { key: 'agent_prompt',  label: 'Identity & Personality' },
      { key: 'user_prompt',   label: 'User Preferences' },
      { key: 'skills_prompt', label: 'Skills & Tools' },
      { key: 'tasks_prompt',  label: 'Task Workflows' },
      { key: 'misc_prompt',   label: 'Miscellaneous' },
    ].forEach(f => {
      const val   = agent[f.key];
      const filled = val && String(val).trim();
      _lvAppendItem(ctxList, {
        name: f.label,
        type: filled ? 'tool' : 'command',
        desc: filled ? String(val).trim().substring(0, 90) + '…' : '(empty — configure in Config tab)',
      });
    });
    panel.appendChild(ctxList);
  } else if (nd.id === 'check_continue') {
    const lbl = document.createElement('div');
    lbl.className = 'lv-tool-section-label';
    lbl.textContent = 'Configuration';
    panel.appendChild(lbl);
    const cfgList = document.createElement('div');
    cfgList.className = 'lv-tool-panel-list';
    _lvAppendItem(cfgList, {
      name: `Max turns: ${agent.max_turn_count || 10}`,
      type: 'tool',
      desc: 'Agent stops looping after this many tool-calling turns',
    });
    panel.appendChild(cfgList);
  } else {
    const toolsLbl = document.createElement('div');
    toolsLbl.className = 'lv-tool-section-label lv-tool-section-live';
    toolsLbl.innerHTML = 'Tools <span class="lv-live-dot"></span>';
    panel.appendChild(toolsLbl);

    const toolsList = document.createElement('div');
    toolsList.className = 'lv-tool-panel-list';
    const loadingEl = document.createElement('div');
    loadingEl.className = 'lv-tool-panel-empty lv-tool-loading';
    loadingEl.textContent = 'Loading…';
    toolsList.appendChild(loadingEl);
    panel.appendChild(toolsList);

    fetchAllToolMeta().then(allTools => {
      const nodeTools = allTools.filter(t => {
        const stages = Array.isArray(t.stages)
          ? t.stages
          : (() => { try { return JSON.parse(t.stages || '[]'); } catch { return []; } })();
        return stages.includes(nd.id) && agentToolNames.has(t.name);
      });

      toolsList.innerHTML = '';

      if (nodeTools.length === 0) {
        const none = document.createElement('div');
        none.className = 'lv-tool-panel-empty';
        none.textContent = 'No tools mapped to this stage for this agent.';
        toolsList.appendChild(none);
        return;
      }

      nodeTools.sort((a, b) => {
        const aS = a.source === 'skill' ? 0 : 1;
        const bS = b.source === 'skill' ? 0 : 1;
        return aS - bS || a.name.localeCompare(b.name);
      });

      nodeTools.forEach(t => {
        const isDestructive = t.destructive === 1 || t.destructive === true;
        const isSkill       = t.source === 'skill';
        _lvAppendItem(toolsList, {
          name: t.name,
          type: isDestructive ? 'guarded' : isSkill ? 'skill' : 'tool',
          desc: t.description || '',
        });
      });
    });
  }

  container.appendChild(panel);
  _lvActivePanelNodeId = nd.id;
  _lvActivePanelEl     = panel;

  setTimeout(() => document.addEventListener('click', _lvHidePanel, { once: true }), 0);
}

function _lvAppendItem(listEl, tool) {
  const BADGE_LABELS = { command: 'empty', tool: 'tool', guarded: '🛡 guarded' };
  const item = document.createElement('div');
  item.className = `lv-tool-item lv-tool-${tool.type}`;

  const nameRow = document.createElement('div');
  nameRow.className = 'lv-tool-name-row';
  const badge = document.createElement('span');
  badge.className = `lv-tool-badge lv-badge-${tool.type}`;
  badge.textContent = BADGE_LABELS[tool.type] || tool.type;
  const name = document.createElement('span');
  name.className = 'lv-tool-name';
  name.textContent = tool.name;
  nameRow.appendChild(badge);
  nameRow.appendChild(name);

  const desc = document.createElement('div');
  desc.className = 'lv-tool-desc';
  desc.textContent = tool.desc;

  item.appendChild(nameRow);
  item.appendChild(desc);
  listEl.appendChild(item);
}

/** Convert DB interaction rows → nodeStates map for the diagram. */
function _interactionsToNodeStates(rows) {
  const s = new Map();
  for (const row of rows) {
    const role     = row.role || '';
    const toolName = row.tool_name || '';
    if (role === 'user') {
      s.set('user_input', 'done');
      s.set('load_context', 'done');
      s.set('memory_search', 'done');
      s.set('build_prompt', 'done');
    } else if (role === 'assistant') {
      s.set('llm_call', 'done');
      s.set('validate_tools', 'done');
      s.set('check_continue', 'done');
      s.set('final_response', 'done');
    } else if (role === 'tool') {
      if (toolName === 'memory_search') {
        s.set('memory_search', 'done');
      } else if (toolName === 'memory_save') {
        s.set('memory_save', 'done');
      } else {
        let meta = {};
        try { meta = JSON.parse(row.metadata || '{}'); } catch (_) {}
        const success = meta.success !== false;
        s.set('validate_tools', 'done');
        s.set('guardrails', 'done');
        s.set('execute_tools', success ? 'done' : 'error');
        s.set('check_continue', 'done');
      }
    }
  }
  return s;
}

// ── Actions ───────────────────────────────────────────────────────────────────

async function _saveChanges(agent, barEl, panelEl) {
  if (agent.source !== 'custom') return;
  const msg = barEl.querySelector('.agents-save-msg');
  if (msg) { msg.textContent = ''; msg.className = 'agents-save-msg'; }

  const updates = {};
  const fv = key => { const el = panelEl.querySelector(`[data-field="${key}"]`); return el ? el.value : undefined; };
  const nameVal = fv('name');        if (nameVal !== undefined) updates.name          = nameVal.trim();
  const descVal = fv('desc');        if (descVal !== undefined) updates.description   = descVal;
  const tcVal   = fv('max_turn_count'); if (tcVal !== undefined) updates.max_turn_count = parseInt(tcVal, 10) || 10;
  for (const k of ['agent_prompt','user_prompt','skills_prompt','tasks_prompt','misc_prompt']) {
    const v = fv(k); if (v !== undefined) updates[k] = v;
  }

  try {
    const res = await fetch(`/api/v1/agents/${agent.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.currentUserId, ...updates }),
    });
    const data = await res.json();
    if (res.ok) {
      // Patch the local agents array so re-render shows fresh values
      const idx = _agents.findIndex(a => a.id === agent.id);
      if (idx !== -1) Object.assign(_agents[idx], data.agent);
      Object.assign(agent, data.agent); // also update the closure reference
      if (msg) { msg.textContent = '✓ Saved'; msg.className = 'agents-save-msg'; }
    } else {
      if (msg) { msg.textContent = data.detail || 'Save failed'; msg.className = 'agents-save-msg error'; }
    }
  } catch (e) {
    if (msg) { msg.textContent = `Error: ${e.message}`; msg.className = 'agents-save-msg error'; }
  }
}

async function _setDefault(agent) {
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}/set-default`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.currentUserId }),
    });
    if (res.ok) {
      _defaultAgentId = agent.id;
      await _loadAgents();
      _renderList();
    }
  } catch (e) {
    console.warn('agents: set-default failed', e);
  }
}

async function _deleteAgent(agent) {
  if (agent.source !== 'custom') return;
  const displayName = _displayName(agent);
  if (!confirm(`Delete agent "${displayName}"? This cannot be undone.`)) return;

  try {
    const res = await fetch(
      `/api/v1/agents/${agent.id}?user_id=${encodeURIComponent(app.currentUserId)}`,
      { method: 'DELETE' }
    );
    if (res.ok) {
      _expandedAgents.delete(agent.id);
      await _loadAgents();
      _renderList();
      _saveViewState();
    }
  } catch (e) {
    console.warn('agents: delete failed', e);
  }
}

// ── Create modal ──────────────────────────────────────────────────────────────

function _bindCreateModal() {
  // initAgents may run multiple times (page load + tab switch). Replace each
  // button with a clone to drop any prior listeners, then bind fresh.
  let newBtn    = document.getElementById('btn-new-agent');
  const modal   = document.getElementById('agents-create-modal');
  let cancelBtn = document.getElementById('btn-create-cancel');
  let createBtn = document.getElementById('btn-create-confirm');
  if (newBtn)    { const c = newBtn.cloneNode(true);    newBtn.replaceWith(c);    newBtn    = c; }
  if (cancelBtn) { const c = cancelBtn.cloneNode(true); cancelBtn.replaceWith(c); cancelBtn = c; }
  if (createBtn) { const c = createBtn.cloneNode(true); createBtn.replaceWith(c); createBtn = c; }
  if (newBtn)    newBtn.addEventListener('click',    () => modal && modal.classList.remove('hidden'));
  if (cancelBtn) cancelBtn.addEventListener('click', () => modal && modal.classList.add('hidden'));

  if (createBtn) {
    createBtn.addEventListener('click', async () => {
      const nameEl = document.getElementById('agents-create-name');
      const descEl = document.getElementById('agents-create-desc');
      const name   = nameEl ? nameEl.value.trim() : '';
      if (!name) { nameEl && nameEl.focus(); return; }

      try {
        const res = await fetch('/api/v1/agents', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            user_id: app.currentUserId,
            name,
            description: descEl ? descEl.value.trim() : '',
          }),
        });
        const data = await res.json();
        if (res.ok) {
          modal && modal.classList.add('hidden');
          if (nameEl) nameEl.value = '';
          if (descEl) descEl.value = '';
          await _loadAgents();
          // Auto-expand the new agent
          const newAgent = _agents.find(a => a.id === data.agent?.id);
          if (newAgent) {
            _expandedAgents.set(newAgent.id, { tab: 'config' });
            _saveViewState();
          }
          _renderList();
        }
      } catch (e) {
        console.warn('agents: create failed', e);
      }
    });
  }
}

// ── Persisted view state ──────────────────────────────────────────────────────

const _STORAGE_KEY = 'agents_view_state';

function _saveViewState() {
  try {
    const expanded = {};
    for (const [agentId, state] of _expandedAgents) {
      expanded[agentId] = { tab: state.tab || 'config' };
    }
    localStorage.setItem(_STORAGE_KEY, JSON.stringify({ expanded }));
  } catch (_) {}
}

function _restoreViewState() {
  try {
    const raw = localStorage.getItem(_STORAGE_KEY);
    if (!raw) return;
    const { expanded } = JSON.parse(raw);
    if (!expanded || typeof expanded !== 'object') return;
    let changed = false;
    for (const [agentId, state] of Object.entries(expanded)) {
      // Only restore if the agent still exists
      if (_agents.find(a => a.id === agentId)) {
        _expandedAgents.set(agentId, { tab: state.tab || 'config' });
        changed = true;
      }
    }
    if (changed) _renderList();
  } catch (_) {}
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function _btn(label, cls) {
  const b = document.createElement('button');
  b.className = cls;
  b.textContent = label;
  return b;
}

function _esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
