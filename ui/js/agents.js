'use strict';

/**
 * Agent Management panel.
 *
 * Renders a two-column layout:
 *  Left  — scrollable list of agents (system templates + user's custom agents)
 *  Right — detail/editor: Config tab (editable prompts + turn count),
 *                          Tools tab (read-only tool breakdown),
 *                          Test tab  (sandbox test runner)
 *
 * Non-editable fields (system_prompt, bootstrap_tools, model, etc.) are never
 * shown to the user — they are stripped server-side and never rendered here.
 */

import { app } from './state.js';

// ── State ─────────────────────────────────────────────────────────────────────
let _agents        = [];   // full list from server
let _selected      = null; // currently selected agent/template entry
let _activeTab     = 'config';
let _dirty         = false;
let _userIsAdmin   = false;
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
  if (!app.userId) return;
  await _loadProfile();
  await _loadAgents();
  _renderList();
  _bindCreateModal();
}

export function startAgents() {
  if (!app.userId) return;
  initAgents();
}

export function stopAgents() {
  // nothing persistent to tear down
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function _loadProfile() {
  try {
    const res = await fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(app.userId)}`);
    if (res.ok) {
      const data = await res.json();
      _userIsAdmin = !!data.is_admin;
      _defaultAgentId = data.default_agent_id || 'default';
    }
  } catch (e) {
    console.warn('agents: could not load profile', e);
  }
}

async function _loadAgents() {
  try {
    const res = await fetch(`/api/v1/agents?user_id=${encodeURIComponent(app.userId)}`);
    if (res.ok) {
      const data = await res.json();
      _agents = data.agents || [];
    }
  } catch (e) {
    console.warn('agents: could not load agent list', e);
  }
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function _renderList() {
  const list = document.getElementById('agents-list');
  if (!list) return;
  list.innerHTML = '';

  for (const agent of _agents) {
    const item = document.createElement('div');
    item.className = 'agent-list-item';
    if (_selected && (_selected.id === agent.id || _selected.user_id === agent.user_id)) {
      item.classList.add('active');
    }

    const isDefault = agent.is_user_default || agent.id === _defaultAgentId;
    const badgeType = agent.access_level === 'admin_only' ? 'admin'
                    : agent.source === 'custom'            ? 'custom'
                    : 'system';
    const badgeLabel = agent.access_level === 'admin_only' ? 'Admin'
                     : agent.source === 'custom'            ? 'Custom'
                     : 'System';

    item.innerHTML = `
      <span class="agent-list-icon">${agent.icon || '🤖'}</span>
      <div class="agent-list-info">
        <div class="agent-list-name">${_esc(agent.name || agent.id)}</div>
        <div class="agent-list-meta">${_esc(agent.description || '')}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:3px;align-items:flex-end">
        <span class="agent-badge ${badgeType}">${badgeLabel}</span>
        ${isDefault ? '<span class="agent-badge default">Default</span>' : ''}
      </div>
    `;
    item.addEventListener('click', () => _selectAgent(agent));
    list.appendChild(item);
  }
}

function _selectAgent(agent) {
  _selected = agent;
  _dirty = false;
  _renderList();
  _renderDetail();
}

function _renderDetail() {
  const empty   = document.getElementById('agents-detail-empty');
  const content = document.getElementById('agents-detail-content');
  if (!_selected) {
    if (empty)   empty.style.display = '';
    if (content) content.style.display = 'none';
    return;
  }
  if (empty)   empty.style.display = 'none';
  if (content) content.style.display = '';

  // Header
  document.getElementById('agents-detail-icon').textContent    = _selected.icon || '🤖';
  document.getElementById('agents-detail-name').textContent    = _selected.name || _selected.id;
  document.getElementById('agents-detail-description').textContent = _selected.description || '';

  // Actions
  _renderActions();

  // Tab body
  _renderTabBody();
}

function _renderActions() {
  const bar = document.getElementById('agents-detail-actions');
  if (!bar) return;
  bar.innerHTML = '';

  const isCustom   = _selected.source === 'custom';
  const canDefault = _selected.can_be_default !== false && _selected.can_be_default !== 0;
  const isDefault  = _selected.is_user_default || _selected.id === _defaultAgentId;

  if (canDefault && !isDefault) {
    const btn = _btn('Set as Default', 'agents-btn');
    btn.addEventListener('click', _setDefault);
    bar.appendChild(btn);
  } else if (isDefault) {
    const lbl = _btn('✓ Default', 'agents-btn default-active');
    lbl.disabled = true;
    bar.appendChild(lbl);
  }

  if (isCustom) {
    const del = _btn('Delete', 'agents-btn danger');
    del.addEventListener('click', _deleteAgent);
    bar.appendChild(del);
  }
}

function _renderTabBody() {
  // Sync active tab button
  document.querySelectorAll('.agents-detail-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === _activeTab);
  });

  const body = document.getElementById('agents-detail-body');
  if (!body) return;
  body.innerHTML = '';

  if (_activeTab === 'config')  _renderConfigTab(body);
  else if (_activeTab === 'tools') _renderToolsTab(body);
  else if (_activeTab === 'test')  _renderTestTab(body);
}

// ── Config tab ────────────────────────────────────────────────────────────────

function _renderConfigTab(container) {
  const isEditable = _selected.source === 'custom';

  // Name + description (editable for custom agents)
  if (isEditable) {
    _addField(container, 'Name', 'agents-input', 'agents-field-name',
      _selected.name || '', false);
    _addField(container, 'Description', 'agents-textarea', 'agents-field-desc',
      _selected.description || '', false, 2);
  }

  // Turn count
  const tcGroup = document.createElement('div');
  tcGroup.className = 'agents-field-group';
  tcGroup.innerHTML = `
    <label class="agents-field-label">Max Turn Count</label>
    <span class="agents-field-hint">Maximum number of tool-calling turns per session.</span>
    <input type="number" class="agents-input" id="agents-field-max_turn_count"
      value="${_selected.max_turn_count || 10}" min="1" max="99999"
      ${!isEditable ? 'readonly' : ''} style="width:100px">
  `;
  container.appendChild(tcGroup);

  // Five prompt fields
  const FIELDS = [
    { key: 'agent_prompt',  label: 'Identity & Personality',    hint: "Defines this agent's character, tone, and core operating style." },
    { key: 'user_prompt',   label: 'User Preferences',          hint: 'What this agent knows about the user — their preferences and context.' },
    { key: 'skills_prompt', label: 'Skills & Tools Guidance',   hint: 'Which capabilities this agent should focus on and how to use them.' },
    { key: 'tasks_prompt',  label: 'Task Workflows',            hint: 'How this agent handles common task types step-by-step.' },
    { key: 'misc_prompt',   label: 'Miscellaneous Context',     hint: 'Any additional guidance or context for this agent.' },
  ];

  for (const field of FIELDS) {
    _addField(container, field.label, 'agents-textarea', `agents-field-${field.key}`,
      _selected[field.key] || '', !isEditable, 6, field.hint);
  }

  // Save bar (only for custom agents)
  if (isEditable) {
    const bar = document.createElement('div');
    bar.className = 'agents-save-bar';
    bar.id = 'agents-save-bar';
    const saveBtn = _btn('Save Changes', 'agents-btn primary');
    saveBtn.addEventListener('click', _saveChanges);
    const msg = document.createElement('span');
    msg.className = 'agents-save-msg';
    msg.id = 'agents-save-msg';
    bar.appendChild(saveBtn);
    bar.appendChild(msg);
    container.parentElement.parentElement.appendChild(bar);
  }

  // Change tracking
  if (isEditable) {
    container.querySelectorAll('input,textarea').forEach(el => {
      el.addEventListener('input', () => { _dirty = true; });
    });
  }
}

function _addField(container, label, tag, id, value, readonly, rows = 4, hint = '') {
  const group = document.createElement('div');
  group.className = 'agents-field-group';
  group.innerHTML = `
    <label class="agents-field-label" for="${id}">${label}</label>
    ${hint ? `<span class="agents-field-hint">${hint}</span>` : ''}
  `;
  const el = document.createElement(tag === 'agents-textarea' ? 'textarea' : 'input');
  el.className = tag;
  el.id = id;
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

function _renderToolsTab(container) {
  const tools = _toolsForAgent(_selected);
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

  container.appendChild(section);
}

// ── Test tab ──────────────────────────────────────────────────────────────────

function _renderTestTab(container) {
  container.innerHTML = `
    <div id="agents-test-area">
      <div style="font-size:12px;color:#565f89;line-height:1.5;margin-bottom:4px;">
        Send a sample message to see how this agent would respond with its current configuration.
        This runs a real single-turn inference and does not save to any session.
      </div>
      <div id="agents-test-input-row">
        <input id="agents-test-input" class="agents-input" placeholder="Type a test message…" />
        <button class="agents-btn primary" id="agents-test-run">Run</button>
      </div>
      <div id="agents-test-status"></div>
      <div id="agents-test-response" style="display:none;"></div>
    </div>
  `;

  document.getElementById('agents-test-run').addEventListener('click', _runTest);
  document.getElementById('agents-test-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _runTest(); }
  });
}

async function _runTest() {
  const input  = document.getElementById('agents-test-input');
  const status = document.getElementById('agents-test-status');
  const resp   = document.getElementById('agents-test-response');
  if (!input || !status || !resp) return;

  const msg = input.value.trim();
  if (!msg) return;

  const agentId = _selected.source === 'custom' ? _selected.id : _selected.id;
  status.textContent = '⏳ Running…';
  resp.style.display = 'none';
  resp.textContent   = '';

  try {
    const res = await fetch('/api/v1/agents/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.userId, agent_id: agentId, message: msg }),
    });
    const data = await res.json();
    status.textContent = res.ok ? '✓ Response received' : `Error ${res.status}`;
    resp.textContent   = data.reply || data.detail || JSON.stringify(data);
    resp.style.display = '';
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
}

// ── Actions ───────────────────────────────────────────────────────────────────

async function _saveChanges() {
  if (!_selected || _selected.source !== 'custom') return;
  const msg = document.getElementById('agents-save-msg');
  if (msg) { msg.textContent = ''; msg.className = 'agents-save-msg'; }

  const updates = {};
  const nameEl   = document.getElementById('agents-field-name');
  const descEl   = document.getElementById('agents-field-desc');
  const tcEl     = document.getElementById('agents-field-max_turn_count');

  if (nameEl)  updates.name          = nameEl.value.trim();
  if (descEl)  updates.description   = descEl.value;
  if (tcEl)    updates.max_turn_count = parseInt(tcEl.value, 10) || 10;

  for (const key of ['agent_prompt','user_prompt','skills_prompt','tasks_prompt','misc_prompt']) {
    const el = document.getElementById(`agents-field-${key}`);
    if (el) updates[key] = el.value;
  }

  try {
    const res = await fetch(`/api/v1/agents/${_selected.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.userId, ...updates }),
    });
    const data = await res.json();
    if (res.ok) {
      // Update local state
      Object.assign(_selected, data.agent);
      _dirty = false;
      if (msg) { msg.textContent = '✓ Saved'; msg.className = 'agents-save-msg'; }
      await _loadAgents();
      _renderList();
    } else {
      if (msg) { msg.textContent = data.detail || 'Save failed'; msg.className = 'agents-save-msg error'; }
    }
  } catch (e) {
    if (msg) { msg.textContent = `Error: ${e.message}`; msg.className = 'agents-save-msg error'; }
  }
}

async function _setDefault() {
  if (!_selected) return;
  try {
    const res = await fetch(`/api/v1/agents/${_selected.id}/set-default`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.userId }),
    });
    if (res.ok) {
      _defaultAgentId = _selected.id;
      await _loadAgents();
      _renderList();
      _renderDetail();
    }
  } catch (e) {
    console.warn('agents: set-default failed', e);
  }
}

async function _deleteAgent() {
  if (!_selected || _selected.source !== 'custom') return;
  if (!confirm(`Delete agent "${_selected.name || _selected.id}"? This cannot be undone.`)) return;

  try {
    const res = await fetch(
      `/api/v1/agents/${_selected.id}?user_id=${encodeURIComponent(app.userId)}`,
      { method: 'DELETE' }
    );
    if (res.ok) {
      _selected = null;
      await _loadAgents();
      _renderList();
      _renderDetail();
    }
  } catch (e) {
    console.warn('agents: delete failed', e);
  }
}

// ── Create modal ──────────────────────────────────────────────────────────────

function _bindCreateModal() {
  const newBtn   = document.getElementById('btn-new-agent');
  const modal    = document.getElementById('agents-create-modal');
  const cancelBtn = document.getElementById('agents-create-cancel');
  const createBtn = document.getElementById('agents-create-confirm');

  if (newBtn)    newBtn.addEventListener('click', () => modal && modal.classList.remove('hidden'));
  if (cancelBtn) cancelBtn.addEventListener('click', () => modal && modal.classList.add('hidden'));

  if (createBtn) {
    createBtn.addEventListener('click', async () => {
      const nameEl = document.getElementById('agents-create-name');
      const descEl = document.getElementById('agents-create-description');
      const name   = nameEl ? nameEl.value.trim() : '';
      if (!name) { nameEl && nameEl.focus(); return; }

      try {
        const res = await fetch('/api/v1/agents', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            user_id: app.userId,
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
          _renderList();
          // Auto-select the new agent
          const newAgent = _agents.find(a => a.id === data.agent?.id);
          if (newAgent) _selectAgent(newAgent);
        }
      } catch (e) {
        console.warn('agents: create failed', e);
      }
    });
  }
}

// ── Tab switching ─────────────────────────────────────────────────────────────

export function initAgentTabs() {
  document.querySelectorAll('.agents-detail-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      _activeTab = btn.dataset.tab;
      // Remove stale save bar if switching away from config
      const oldBar = document.getElementById('agents-save-bar');
      if (oldBar) oldBar.remove();
      _renderTabBody();
    });
  });
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
