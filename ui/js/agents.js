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
import { fetchAllToolMeta } from './loop-logic.js';
import { NODE_PANEL_INFO } from './loop-node-data.js';
import { LOOP_W, LOOP_H, LOOP_NODES, TOGGLEABLE_NODES, renderLoopDiagram } from './loop-diagram.js';
import { icon } from './icons.js';

function _triggerKeyPlaceholder(triggerType) {
  const map = {
    slash_command: 'Slash command (e.g. /optimize)',
    tool_call:     'Tool name (e.g. run_optimizer)',
    schedule:      'Cron expression (e.g. 0 9 * * *)',
    webhook:       'Webhook path slug',
    background:    'Internal identifier',
  };
  return map[triggerType] || '';
}

// ── State ─────────────────────────────────────────────────────────────────────
let _agents         = [];   // full list from server
let _expandedAgents = new Map(); // Map<agentId, { tab: string }>
let _userIsAdmin    = false;
let _defaultAgentId = null;
let _extendLlmToAgents = true; // mirrors app-settings.json extend_llm_to_agents

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

// ── Tool tier definitions ─────────────────────────────────────────────
// Tier 0: Admin-only. Never shown or toggleable for normal agents.
const TIER_0_ADMIN = new Set([
  'read_source','write_source','edit_source','delete_source','run_command','restart_server',
  'run_worker_trials','handoff_to_finalizer','deploy_optimization',
]);

// Tier 1: Always-on. Present for all agents, not shown as toggleable.
const TIER_1_ALWAYS_ON = new Set([
  'list_tools','search_tools','get_tool_definition',
  'get_time','get_date','calculate','read_attachment',
  'delegate_to_agent','list_delegatable_agents','register_user',
]);

// Tier 2: Configurable standard tools — shown in the toggle UI.
const TIER_2_TOOLS = [
  'web_search','http_request','browser_action',
  'db_query','list_agent_context_documents','get_agent_context_document',
  'update_agent_context_document','insert_agent_context_document','session_search',
  'memory',
  'get_weather','create_tool','rate_skill','run_optimizer',
  'register_webhook','list_webhooks','delete_webhook','get_webhook_log',
];

const TIER_2_ALL = TIER_2_TOOLS;

// Category definitions covering all tiers. condition: null=always, 'admin'=admin/optimizer only
const TOOL_CATEGORIES = [
  {
    label: 'Information & Search',
    condition: null,
    tools: [
      'web_search','http_request','browser_action','session_search','memory',
      'list_tools','search_tools','get_tool_definition','read_attachment',
    ],
  },
  {
    label: 'Data & Context',
    condition: null,
    tools: [
      'db_query','list_agent_context_documents','get_agent_context_document',
      'update_agent_context_document','insert_agent_context_document','create_tool',
      'get_time','get_date','calculate','delegate_to_agent','list_delegatable_agents','register_user',
    ],
  },
  {
    label: 'Integrations & Automation',
    condition: null,
    tools: [
      'register_webhook','list_webhooks','delete_webhook','get_webhook_log',
      'get_weather','rate_skill','run_optimizer',
    ],
  },
  {
    label: 'Admin & System',
    condition: 'admin',
    tools: [
      'read_source','write_source','edit_source','delete_source',
      'run_command','restart_server',
      'run_worker_trials','handoff_to_finalizer','deploy_optimization',
    ],
  },
];

const PIPELINE_TOOLS = {
  opt_planner:   ['run_worker_trials','handoff_to_finalizer'],
  opt_finalizer: ['deploy_optimization'],
};

const DESTRUCTIVE = new Set([
  'db_query','update_agent_context_document','create_tool',
  'delete_webhook','write_source','edit_source','delete_source','run_command','restart_server',
]);

function _toolsForAgent(agent) {
  const id = agent.id || '';
  if (agent.is_admin_agent) {
    return [...TIER_1_ALWAYS_ON, ...TIER_2_ALL,
            'read_source','write_source','edit_source','delete_source','run_command','restart_server'];
  }
  if (id === 'opt_planner')   return [...TIER_1_ALWAYS_ON, ...TIER_2_ALL, ...(PIPELINE_TOOLS.opt_planner || [])];
  if (id === 'opt_finalizer') return [...TIER_1_ALWAYS_ON, ...TIER_2_ALL, ...(PIPELINE_TOOLS.opt_finalizer || [])];

  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  return [
    ...TIER_1_ALWAYS_ON,
    ...TIER_2_ALL.filter(name => !disabled.has(name)),
  ];
}

// ── Init ──────────────────────────────────────────────────────────────────────

export async function initAgents() {
  if (!app.currentUserId) return;
  await Promise.all([_loadProfile(), _loadAgents(), _loadAppSettings()]);
  _renderList();
  _bindCreateModal();
  _restoreViewState();
}

// Expand a specific agent's card and scroll it into view. Called from the
// chat-header agent dropdown's "Config" action. Retries until grid is rendered
// (initAgents may still be loading when the user clicks Config from chat).
window.expandAgent = function expandAgent(agentId) {
  if (!agentId) return;
  _expandedAgents.set(agentId, { tab: 'config' });
  _saveViewState();
  let attempts = 0;
  const tryRender = () => {
    attempts += 1;
    const grid = document.getElementById('agents-grid');
    if (_agents.find(a => a.id === agentId)) {
      _renderList();
      requestAnimationFrame(() => {
        const row = document.querySelector(`.agent-row[data-agent-id="${CSS.escape(agentId)}"]`);
        if (row && row.scrollIntoView) row.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      return;
    }
    if (attempts < 40) setTimeout(tryRender, 50);
  };
  tryRender();
};

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

async function _loadAppSettings() {
  try {
    const token = localStorage.getItem('auth_token');
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const res = await fetch('/admin/settings/app', { headers });
    if (res.ok) {
      const data = await res.json();
      _extendLlmToAgents = data.extend_llm_to_agents !== false;
    }
  } catch (e) {
    // non-fatal — keep default true
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
    const isDefault  = agent.id === _defaultAgentId;

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
          ${agent.icon || icon('bot', { size: '20px' })}
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
      <div class="agent-card-url">
        <span class="agent-url-text">${location.origin}/${agent.id}</span>
        <button class="agent-url-copy" title="Copy URL">${icon('copy', { size: '13px' })}</button>
      </div>
      <div class="agent-card-stats">
        <span class="agent-stat"><span class="agent-stat-icon">↻</span>${turns} turns</span>
        <span class="agent-stat"><span class="agent-stat-icon">⋮</span>${temp}</span>
        <span class="agent-stat"><span class="agent-stat-icon">${icon('wrench', { size: '11px' })}</span>${toolCount} tools</span>
        ${timeAgo ? `<span class="agent-stat agent-stat-time"><span class="agent-stat-icon">${icon('clock', { size: '11px' })}</span>${timeAgo}</span>` : ''}
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
    const copyUrlBtn = card.querySelector('.agent-url-copy');
    if (copyUrlBtn) {
      copyUrlBtn.addEventListener('click', e => {
        e.stopPropagation();
        const url = `${location.origin}/${agent.id}`;
        navigator.clipboard.writeText(url).then(() => {
          copyUrlBtn.innerHTML = icon('check', { size: '13px' });
          setTimeout(() => { copyUrlBtn.innerHTML = icon('copy', { size: '13px' }); }, 1500);
        });
      });
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

  // Tab bar (wrapped in chevron-scroll carousel for narrow screens)
  const tabWrap = document.createElement('div');
  tabWrap.className = 'agent-detail-tabs-wrap';

  const chevLeft = document.createElement('button');
  chevLeft.type = 'button';
  chevLeft.className = 'agent-detail-tabs-chev left';
  chevLeft.setAttribute('aria-label', 'Scroll tabs left');
  chevLeft.innerHTML = '&#10094;';

  const chevRight = document.createElement('button');
  chevRight.type = 'button';
  chevRight.className = 'agent-detail-tabs-chev right';
  chevRight.setAttribute('aria-label', 'Scroll tabs right');
  chevRight.innerHTML = '&#10095;';

  const tabBar = document.createElement('div');
  tabBar.className = 'agent-detail-tabs';
  const tabs = [['config','Config'],['tools','Tools'],['test','Agent Loop'],['connections','Connections'],['automation','Automation']];
  if (_userIsAdmin) tabs.push(['members','Members']);
  for (const [key, label] of tabs) {
    const btn = document.createElement('button');
    btn.className = 'agents-detail-tab' + (activeTab === key ? ' active' : '');
    btn.dataset.tab = key;
    btn.textContent = label;
    btn.addEventListener('click', () => {
      const entry = _expandedAgents.get(agent.id);
      if (entry) entry.tab = key;
      _renderPanelBody(agent, panel);
      _saveViewState();
      btn.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    });
    tabBar.appendChild(btn);
  }

  const updateChevrons = () => {
    const overflow = tabBar.scrollWidth - tabBar.clientWidth > 1;
    tabWrap.classList.toggle('has-overflow', overflow);
    chevLeft.classList.toggle('visible', overflow && tabBar.scrollLeft > 1);
    chevRight.classList.toggle('visible', overflow && tabBar.scrollLeft < tabBar.scrollWidth - tabBar.clientWidth - 1);
  };
  const scrollStep = () => Math.max(80, Math.floor(tabBar.clientWidth * 0.6));
  chevLeft.addEventListener('click', () => tabBar.scrollBy({ left: -scrollStep(), behavior: 'smooth' }));
  chevRight.addEventListener('click', () => tabBar.scrollBy({ left: scrollStep(), behavior: 'smooth' }));
  tabBar.addEventListener('scroll', updateChevrons, { passive: true });

  tabWrap.appendChild(chevLeft);
  tabWrap.appendChild(tabBar);
  tabWrap.appendChild(chevRight);
  content.appendChild(tabWrap);

  // Recompute on mount and on resize. ResizeObserver catches container width changes.
  requestAnimationFrame(() => {
    updateChevrons();
    const active = tabBar.querySelector('.agents-detail-tab.active');
    if (active) active.scrollIntoView({ inline: 'center', block: 'nearest' });
  });
  if (typeof ResizeObserver !== 'undefined') {
    let roPending = false;
    const ro = new ResizeObserver(() => {
      if (roPending) return;
      roPending = true;
      requestAnimationFrame(() => { roPending = false; updateChevrons(); });
    });
    ro.observe(tabBar);
    ro.observe(tabWrap);
  }
  window.addEventListener('resize', updateChevrons);

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

  if (tab === 'config')            _renderConfigTab(body, agent, panelEl);
  else if (tab === 'tools')        _renderToolsTab(body, agent, panelEl);
  else if (tab === 'test')         _renderTestTab(body, agent);
  else if (tab === 'connections')  _renderConnectionsTab(body, agent);
  else if (tab === 'automation')   _renderAutomationTab(body, agent, panelEl);
  else if (tab === 'members')      _renderMembersTab(body, agent);
}

// ── Automation tab ────────────────────────────────────────────────────────────

async function _renderAutomationTab(body, agent, panelEl) {
  body.innerHTML = '<div style="font-size:12px;color:var(--fg-3);padding:8px;">Loading automation…</div>';

  let slotContent = '';
  try {
    const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/slots?user_id=${encodeURIComponent(app.currentUserId)}`);
    if (r.ok) {
      const d = await r.json();
      const s = (d.slots || []).find(x => x.slot_name === 'automation');
      if (s) slotContent = (s.override_content != null ? s.override_content : s.content) || '';
    }
  } catch (_) {}

  let availableChannels = ['webchat'];
  try {
    const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/connections?user_id=${encodeURIComponent(app.currentUserId)}`);
    if (r.ok) {
      const d = await r.json();
      for (const c of (d.connections || [])) {
        if (c.section === 'channel' && c.enabled && c.connection_type) {
          if (!availableChannels.includes(c.connection_type)) {
            availableChannels.push(c.connection_type);
          }
        }
      }
    }
  } catch (_) {}

  body.innerHTML = '';

  const intro = document.createElement('div');
  intro.style.cssText = 'font-size:12px;color:var(--fg-2);padding:8px 10px;background:var(--accent-soft);border:1px solid var(--border);border-radius:6px;margin-bottom:10px;line-height:1.5;';
  intro.textContent = 'Describe scheduled work in plain English (one task per paragraph). Saving re-parses the file into structured tasks shown below.';
  body.appendChild(intro);

  const textWrap = document.createElement('div');
  textWrap.className = 'agents-field-group';
  const lbl = document.createElement('label');
  lbl.className = 'agents-field-label';
  lbl.textContent = 'Automation file';
  const ta = document.createElement('textarea');
  ta.className = 'agents-textarea agents-automation-textarea';
  ta.style.cssText = 'width:100%;min-height:220px;font-family:monospace;font-size:12px;line-height:1.45;';
  ta.value = slotContent;
  textWrap.appendChild(lbl);
  textWrap.appendChild(ta);
  body.appendChild(textWrap);

  const saveBar = document.createElement('div');
  saveBar.style.cssText = 'display:flex;align-items:center;gap:10px;margin-top:6px;';
  const saveBtn = _btn('Save & Parse', 'agents-btn primary');
  const saveMsg = document.createElement('span');
  saveMsg.style.cssText = 'font-size:12px;color:var(--fg-3);';
  saveBar.appendChild(saveBtn);
  saveBar.appendChild(saveMsg);
  body.appendChild(saveBar);

  const parseErr = document.createElement('div');
  parseErr.style.cssText = 'display:none;font-size:11px;color:var(--danger);margin-top:8px;padding:6px 10px;background:var(--danger-soft);border:1px solid var(--danger);border-radius:4px;';
  body.appendChild(parseErr);

  const tasksHeader = document.createElement('div');
  tasksHeader.style.cssText = 'margin-top:18px;font-size:12px;font-weight:600;color:var(--fg-2);text-transform:uppercase;letter-spacing:0.5px;';
  tasksHeader.textContent = 'Scheduled tasks';
  body.appendChild(tasksHeader);

  const tasksList = document.createElement('div');
  tasksList.className = 'agents-automation-tasks';
  tasksList.style.cssText = 'display:flex;flex-direction:column;gap:8px;margin-top:8px;';
  body.appendChild(tasksList);

  function renderTasks(tasks) {
    tasksList.innerHTML = '';
    if (!tasks || !tasks.length) {
      const empty = document.createElement('div');
      empty.style.cssText = 'font-size:12px;color:var(--fg-3);padding:8px;';
      empty.textContent = 'No scheduled tasks yet.';
      tasksList.appendChild(empty);
      return;
    }
    for (const t of tasks) {
      tasksList.appendChild(_renderAutomationTaskRow(agent, t, availableChannels, () => loadTasks()));
    }
  }

  async function loadTasks() {
    try {
      const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/automations?user_id=${encodeURIComponent(app.currentUserId)}`);
      if (r.ok) {
        const d = await r.json();
        renderTasks(d.tasks || []);
      }
    } catch (_) {}
  }

  saveBtn.addEventListener('click', async () => {
    saveMsg.textContent = 'Saving…';
    saveMsg.style.color = '#565f89';
    parseErr.style.display = 'none';
    parseErr.textContent = '';
    try {
      const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/my-prompts`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: app.currentUserId,
          slots: [{ slot_name: 'automation', content: ta.value }],
        }),
      });
      const d = await r.json();
      if (!r.ok) {
        saveMsg.textContent = d.detail || 'Save failed';
        saveMsg.style.color = 'var(--danger)';
        return;
      }
      saveMsg.textContent = '✓ Saved';
      saveMsg.style.color = 'var(--success)';
      if (d.automation_error) {
        parseErr.style.display = 'block';
        parseErr.textContent = `Parse warning: ${d.automation_error}`;
      }
      if (Array.isArray(d.automation_tasks)) {
        renderTasks(d.automation_tasks);
      } else {
        await loadTasks();
      }
    } catch (e) {
      saveMsg.textContent = `Error: ${e.message}`;
      saveMsg.style.color = 'var(--danger)';
    }
  });

  await loadTasks();
}

function _renderAutomationTaskRow(agent, task, channels, onRefresh) {
  const row = document.createElement('div');
  row.className = 'agents-automation-row';
  row.style.cssText = 'border:1px solid var(--border);border-radius:6px;padding:10px 12px;background:var(--bg-elev);display:grid;grid-template-columns:1fr auto;gap:8px;';

  const left = document.createElement('div');
  left.style.cssText = 'display:flex;flex-direction:column;gap:4px;';

  const title = document.createElement('div');
  title.style.cssText = 'font-size:13px;font-weight:600;color:var(--fg-1);';
  title.textContent = task.task_label || '(unlabeled)';
  left.appendChild(title);

  const sched = document.createElement('div');
  sched.style.cssText = 'font-size:11px;color:var(--accent);font-family:monospace;';
  sched.textContent = `${task.schedule_cron || ''}  ${task.schedule_natural ? '· ' + task.schedule_natural : ''}`;
  left.appendChild(sched);

  const meta = document.createElement('div');
  meta.style.cssText = 'font-size:11px;color:var(--fg-3);';
  const next = task.next_run_at ? `next: ${new Date(task.next_run_at).toLocaleString()}` : 'next: —';
  const last = task.last_run_at ? `last: ${new Date(task.last_run_at).toLocaleString()} (${task.last_status || 'ok'})` : 'last: —';
  meta.textContent = `${next} · ${last}`;
  left.appendChild(meta);

  if (task.last_error) {
    const err = document.createElement('div');
    err.style.cssText = 'font-size:11px;color:var(--danger);';
    err.textContent = `Error: ${task.last_error}`;
    left.appendChild(err);
  }

  const promptPreview = document.createElement('div');
  promptPreview.style.cssText = 'font-size:11px;color:var(--fg-2);font-style:italic;margin-top:4px;white-space:pre-wrap;';
  promptPreview.textContent = task.prompt ? `> ${task.prompt}` : '';
  left.appendChild(promptPreview);

  row.appendChild(left);

  const right = document.createElement('div');
  right.style.cssText = 'display:flex;flex-direction:column;gap:6px;align-items:flex-end;min-width:170px;';

  const enableLbl = document.createElement('label');
  enableLbl.style.cssText = 'display:flex;align-items:center;gap:5px;font-size:11px;color:var(--fg-2);cursor:pointer;';
  const enableCb = document.createElement('input');
  enableCb.type = 'checkbox';
  enableCb.checked = !!task.enabled;
  enableLbl.appendChild(enableCb);
  enableLbl.appendChild(document.createTextNode('Enabled'));
  right.appendChild(enableLbl);

  const silentLbl = document.createElement('label');
  silentLbl.style.cssText = 'display:flex;align-items:center;gap:5px;font-size:11px;color:var(--fg-2);cursor:pointer;';
  const silentCb = document.createElement('input');
  silentCb.type = 'checkbox';
  silentCb.checked = !!task.silent;
  silentLbl.appendChild(silentCb);
  silentLbl.appendChild(document.createTextNode('Silent'));
  right.appendChild(silentLbl);

  const chanWrap = document.createElement('div');
  chanWrap.style.cssText = 'display:flex;flex-direction:column;gap:2px;align-items:flex-end;';
  const chanSel = document.createElement('select');
  chanSel.className = 'agents-input';
  chanSel.style.cssText = 'font-size:11px;padding:3px 6px;min-width:140px;';
  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = '— none —';
  chanSel.appendChild(blank);
  for (const c of channels) {
    const opt = document.createElement('option');
    opt.value = c;
    opt.textContent = c;
    if (task.channel === c) opt.selected = true;
    chanSel.appendChild(opt);
  }
  chanWrap.appendChild(chanSel);

  const recipInput = document.createElement('input');
  recipInput.type = 'text';
  recipInput.className = 'agents-input';
  recipInput.placeholder = 'recipient (id, phone, email)';
  recipInput.style.cssText = 'font-size:11px;padding:3px 6px;min-width:140px;';
  recipInput.value = task.channel_recipient || '';
  chanWrap.appendChild(recipInput);
  right.appendChild(chanWrap);

  const btnRow = document.createElement('div');
  btnRow.style.cssText = 'display:flex;gap:5px;margin-top:4px;';

  const saveBtn = document.createElement('button');
  saveBtn.className = 'agents-btn';
  saveBtn.textContent = 'Save';
  saveBtn.style.cssText = 'font-size:11px;padding:3px 10px;';

  const runBtn = document.createElement('button');
  runBtn.className = 'agents-btn';
  runBtn.textContent = 'Run now';
  runBtn.style.cssText = 'font-size:11px;padding:3px 10px;';

  btnRow.appendChild(saveBtn);
  btnRow.appendChild(runBtn);
  right.appendChild(btnRow);

  saveBtn.addEventListener('click', async () => {
    saveBtn.textContent = '…';
    try {
      await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/automations/${encodeURIComponent(task.id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: app.currentUserId,
          enabled: enableCb.checked,
          silent: silentCb.checked,
          channel: chanSel.value || null,
          channel_recipient: recipInput.value || null,
        }),
      });
      saveBtn.textContent = '✓';
      setTimeout(() => { saveBtn.textContent = 'Save'; }, 1200);
      onRefresh && onRefresh();
    } catch (e) {
      saveBtn.textContent = 'Err';
    }
  });

  runBtn.addEventListener('click', async () => {
    runBtn.textContent = 'Running…';
    runBtn.disabled = true;
    try {
      const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/automations/${encodeURIComponent(task.id)}/run-now?user_id=${encodeURIComponent(app.currentUserId)}`, {
        method: 'POST',
      });
      const d = await r.json();
      if (d?.result?.ok === false) {
        runBtn.textContent = 'Error';
      } else {
        runBtn.textContent = 'Done';
      }
      setTimeout(() => { runBtn.textContent = 'Run now'; runBtn.disabled = false; }, 1500);
      onRefresh && onRefresh();
    } catch (e) {
      runBtn.textContent = 'Err';
      runBtn.disabled = false;
    }
  });

  row.appendChild(right);
  return row;
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

  // ── Per-agent LLM card ───────────────────────────────────────────────────
  if (isEditable) {
    const llmCfg = agent.llm_config || { use_default: true };
    panelEl._llmState = { ...llmCfg };

    const llmGroup = document.createElement('div');
    llmGroup.className = 'agents-field-group';

    const llmCard = document.createElement('div');
    llmCard.className = 'agents-llm-card';

    // Header
    const llmHeader = document.createElement('div');
    llmHeader.className = 'agents-llm-header';
    const llmTitle = document.createElement('span');
    llmTitle.className = 'agents-llm-title';
    llmTitle.textContent = 'LLM';
    const llmBadge = document.createElement('span');
    llmBadge.className = 'agents-llm-badge';
    const llmChevron = document.createElement('span');
    llmChevron.className = 'agents-llm-chevron';
    llmChevron.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>`;
    llmHeader.appendChild(llmTitle);
    llmHeader.appendChild(llmBadge);
    llmHeader.appendChild(llmChevron);

    // Body (collapsed by default)
    const llmBody = document.createElement('div');
    llmBody.className = 'agents-llm-body';
    llmBody.style.display = 'none';

    llmHeader.addEventListener('click', () => {
      const open = llmBody.style.display === 'none';
      llmBody.style.display = open ? 'flex' : 'none';
      llmChevron.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
    });

    function _llmUpdateBadge() {
      const s = panelEl._llmState;
      if (s.use_default !== false) {
        llmBadge.textContent = 'Default';
        llmBadge.className = 'agents-llm-badge';
      } else {
        const p = s.provider && s.provider !== 'custom' ? s.provider : 'custom';
        const label = s.model ? `${p} · ${s.model}` : p;
        llmBadge.textContent = label;
        llmBadge.className = 'agents-llm-badge custom';
      }
    }
    _llmUpdateBadge();

    // Mode radios
    const modeRow = document.createElement('div');
    modeRow.className = 'agents-llm-mode-row';
    const _llmModeLabels = {};
    const _modeOptions = _extendLlmToAgents
      ? [['default', 'Use app default'], ['custom', 'Custom']]
      : [['custom', 'Custom']];
    for (const [val, txt] of _modeOptions) {
      const lbl = document.createElement('label');
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = `agent-llm-mode-${_esc(agent.id)}`;
      radio.value = val;
      radio.style.accentColor = '#7aa2f7';
      lbl.appendChild(radio);
      lbl.appendChild(document.createTextNode(' ' + txt));
      modeRow.appendChild(lbl);
      _llmModeLabels[val] = { lbl, radio };
    }
    llmBody.appendChild(modeRow);

    // Custom fields
    const customFields = document.createElement('div');
    customFields.className = 'agents-llm-fields';

    // Provider
    const provField = document.createElement('div');
    provField.className = 'agents-llm-field';
    const provLbl = document.createElement('label');
    provLbl.textContent = 'Provider';
    const provSel = document.createElement('select');
    provSel.className = 'agents-input';
    provSel.style.width = '100%';
    provField.appendChild(provLbl);
    provField.appendChild(provSel);
    customFields.appendChild(provField);

    // Base URL
    const urlField = document.createElement('div');
    urlField.className = 'agents-llm-field';
    const urlLbl = document.createElement('label');
    urlLbl.textContent = 'Base URL';
    const urlInput = document.createElement('input');
    urlInput.type = 'text';
    urlInput.className = 'agents-input';
    urlInput.style.width = '100%';
    urlInput.placeholder = 'https://openrouter.ai/api/v1';
    urlField.appendChild(urlLbl);
    urlField.appendChild(urlInput);
    customFields.appendChild(urlField);

    // API Key
    const keyField = document.createElement('div');
    keyField.className = 'agents-llm-field';
    const keyLbl = document.createElement('label');
    keyLbl.textContent = 'API Key';
    const keyInput = document.createElement('input');
    keyInput.type = 'password';
    keyInput.className = 'agents-input';
    keyInput.style.width = '100%';
    keyInput.placeholder = 'sk-...';
    keyField.appendChild(keyLbl);
    keyField.appendChild(keyInput);
    customFields.appendChild(keyField);

    // Model search + dropdown
    const modelField = document.createElement('div');
    modelField.className = 'agents-llm-field';
    const modelLbl = document.createElement('label');
    modelLbl.textContent = 'Model';
    const modelWrap = document.createElement('div');
    modelWrap.className = 'agents-llm-model-wrap';
    const modelSearch = document.createElement('input');
    modelSearch.type = 'text';
    modelSearch.className = 'agents-input';
    modelSearch.style.width = '100%';
    modelSearch.placeholder = 'Search models…';
    const modelDd = document.createElement('div');
    modelDd.className = 'agents-llm-model-dropdown';
    modelDd.style.display = 'none';
    modelWrap.appendChild(modelSearch);
    modelWrap.appendChild(modelDd);
    const modelStatus = document.createElement('div');
    modelStatus.style.cssText = 'font-size:11px;color:#565f89;margin-top:3px;';
    modelField.appendChild(modelLbl);
    modelField.appendChild(modelWrap);
    modelField.appendChild(modelStatus);
    customFields.appendChild(modelField);

    llmBody.appendChild(customFields);
    llmCard.appendChild(llmHeader);
    llmCard.appendChild(llmBody);
    llmGroup.appendChild(llmCard);
    body.appendChild(llmGroup);

    // ── Agent-scoped model state ──
    let _agentAllModels = [];
    let _agentSelectedModel = llmCfg.model || '';
    let _agentProviderPresets = {};

    function _renderAgentModelDd(filter) {
      if (!_agentAllModels.length) { modelDd.style.display = 'none'; return; }
      const filtered = filter
        ? _agentAllModels.filter(m => m.id.toLowerCase().includes(filter) || m.name.toLowerCase().includes(filter))
        : _agentAllModels;
      if (!filtered.length) { modelDd.style.display = 'none'; return; }
      modelDd.innerHTML = '';
      modelDd.style.display = 'block';
      filtered.slice(0, 200).forEach(m => {
        const item = document.createElement('div');
        item.className = 'agents-llm-model-item';
        if (m.id === _agentSelectedModel) item.style.background = 'rgba(125,207,255,0.12)';
        item.innerHTML = `<span style="font-weight:500;">${_esc(m.id)}</span> <span style="color:#565f89;font-size:11px;margin-left:6px;">${_esc(m.name)}</span>`;
        item.addEventListener('click', () => {
          _agentSelectedModel = m.id;
          modelSearch.value = m.id;
          modelDd.style.display = 'none';
          modelStatus.textContent = `Selected: ${m.id}`;
          modelStatus.style.color = '#9ece6a';
          panelEl._llmState.model = m.id;
          _llmUpdateBadge();
        });
        modelDd.appendChild(item);
      });
    }

    async function _fetchAgentModels() {
      const provider = provSel.value === '_custom' ? '' : provSel.value;
      modelStatus.textContent = 'Loading models…';
      modelStatus.style.color = '#565f89';
      _agentAllModels = [];
      modelDd.style.display = 'none';
      try {
        let url = `/admin/settings/models?provider=${encodeURIComponent(provider)}`;
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data.error) {
          modelStatus.textContent = data.error === 'No API key configured'
            ? 'Enter an API key to see available models.'
            : `Error: ${data.error}`;
          return;
        }
        _agentAllModels = data.models || [];
        modelStatus.textContent = _agentAllModels.length
          ? `${_agentAllModels.length} models available. Type to filter.`
          : 'No models available.';
      } catch (e) {
        modelStatus.textContent = 'Failed to load models.';
        modelStatus.style.color = '#f7768e';
      }
    }

    provSel.addEventListener('change', () => {
      const preset = _agentProviderPresets[provSel.value];
      if (preset && preset.base_url) urlInput.value = preset.base_url;
      panelEl._llmState.provider = provSel.value === '_custom' ? 'custom' : provSel.value;
      panelEl._llmState.base_url = urlInput.value;
      _agentSelectedModel = '';
      modelSearch.value = '';
      modelStatus.textContent = '';
      panelEl._llmState.model = '';
      _fetchAgentModels();
      _llmUpdateBadge();
    });

    urlInput.addEventListener('change', () => { panelEl._llmState.base_url = urlInput.value.trim(); });
    keyInput.addEventListener('change', () => {
      panelEl._llmState.api_key = keyInput.value.trim();
    });

    modelSearch.addEventListener('focus', () => _renderAgentModelDd(modelSearch.value.toLowerCase()));
    modelSearch.addEventListener('input', () => _renderAgentModelDd(modelSearch.value.toLowerCase()));

    document.addEventListener('click', e => {
      if (!e.target.closest('.agents-llm-model-wrap')) modelDd.style.display = 'none';
    });

    // Mode switching
    function _applyLlmMode(mode) {
      const isCustom = mode === 'custom';
      customFields.style.display = isCustom ? 'flex' : 'none';
      panelEl._llmState.use_default = !isCustom;
      Object.entries(_llmModeLabels).forEach(([val, { lbl, radio }]) => {
        radio.checked = val === mode;
        lbl.classList.toggle('active', val === mode);
      });
      _llmUpdateBadge();
    }

    Object.entries(_llmModeLabels).forEach(([val, { radio }]) => {
      radio.addEventListener('change', () => { if (radio.checked) _applyLlmMode(val); });
    });

    const initialMode = (llmCfg.use_default === false || !_extendLlmToAgents) ? 'custom' : 'default';
    _applyLlmMode(initialMode);

    // Populate saved values into fields
    if (llmCfg.use_default === false) {
      urlInput.value = llmCfg.base_url || '';
      keyInput.value = llmCfg.api_key || '';
      modelSearch.value = llmCfg.model || '';
      _agentSelectedModel = llmCfg.model || '';
      if (_agentSelectedModel) {
        modelStatus.textContent = `Selected: ${_agentSelectedModel}`;
        modelStatus.style.color = '#9ece6a';
      }
    }

    // Load provider presets and set saved provider
    (async () => {
      try {
        const res = await fetch('/admin/settings/providers');
        if (!res.ok) return;
        _agentProviderPresets = await res.json();
        provSel.innerHTML = '';
        for (const [key, preset] of Object.entries(_agentProviderPresets)) {
          const opt = document.createElement('option');
          opt.value = key;
          opt.textContent = preset.name;
          provSel.appendChild(opt);
        }
        const customOpt = document.createElement('option');
        customOpt.value = '_custom';
        customOpt.textContent = 'Custom';
        provSel.appendChild(customOpt);

        const savedProv = llmCfg.provider && llmCfg.provider !== 'custom' ? llmCfg.provider : null;
        provSel.value = savedProv && _agentProviderPresets[savedProv] ? savedProv : '_custom';
        if (llmCfg.use_default === false) _fetchAgentModels();
      } catch (e) { /* silent */ }
    })();
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

  // User mode (applies across all channels)
  const umGroup = document.createElement('div');
  umGroup.className = 'agents-field-group';
  const umMode = agent.user_mode || 'anonymous';
  umGroup.innerHTML = `
    <label class="agents-field-label">User Mode</label>
    <span class="agents-field-hint">How this agent handles users across all channels.</span>
    <div class="conn-user-mode" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px;">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;padding:6px 10px;border-radius:6px;border:1px solid ${umMode === 'anonymous' ? '#7aa2f7' : 'var(--border,#2a2a3a)'};background:${umMode === 'anonymous' ? 'rgba(122,162,247,0.08)' : 'transparent'};">
        <input type="radio" name="agent-user-mode-${_esc(agent.id)}" value="anonymous" data-field="user_mode" ${umMode === 'anonymous' ? 'checked' : ''} style="accent-color:#7aa2f7;" ${!isEditable ? 'disabled' : ''}>
        Anonymous
      </label>
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;padding:6px 10px;border-radius:6px;border:1px solid ${umMode === 'register' ? '#7aa2f7' : 'var(--border,#2a2a3a)'};background:${umMode === 'register' ? 'rgba(122,162,247,0.08)' : 'transparent'};">
        <input type="radio" name="agent-user-mode-${_esc(agent.id)}" value="register" data-field="user_mode" ${umMode === 'register' ? 'checked' : ''} style="accent-color:#7aa2f7;" ${!isEditable ? 'disabled' : ''}>
        Register
      </label>
    </div>
    <span class="conn-user-mode-hint" style="font-size:11px;color:var(--fg-muted,#565f89);display:block;margin-top:4px;">${umMode === 'register'
      ? 'Agent guides new users to register and links accounts across channels.'
      : 'Users get auto-generated anonymous IDs. No registration required.'}</span>
  `;
  if (isEditable) {
    umGroup.querySelectorAll('[data-field="user_mode"]').forEach(radio => {
      radio.addEventListener('change', () => {
        const selected = umGroup.querySelector('[data-field="user_mode"]:checked')?.value || 'anonymous';
        umGroup.querySelectorAll('.conn-user-mode label').forEach(lbl => {
          const val = lbl.querySelector('input')?.value;
          lbl.style.borderColor = val === selected ? '#7aa2f7' : 'var(--border,#2a2a3a)';
          lbl.style.background = val === selected ? 'rgba(122,162,247,0.08)' : 'transparent';
        });
        const hintEl = umGroup.querySelector('.conn-user-mode-hint');
        if (hintEl) {
          hintEl.textContent = selected === 'register'
            ? 'Agent guides new users to register and links accounts across channels.'
            : 'Users get auto-generated anonymous IDs. No registration required.';
        }
      });
    });
  }
  body.appendChild(umGroup);

  // trigger type
  if (isEditable) {
    const triggerRow = document.createElement('div');
    triggerRow.className = 'agents-field-group';
    const triggerLabel = document.createElement('label');
    triggerLabel.className = 'agents-field-label';
    triggerLabel.textContent = 'Trigger';
    const triggerSel = document.createElement('select');
    triggerSel.className = 'agents-input';
    triggerSel.dataset.field = 'trigger_type';
    for (const [val, text] of [
      ['user_input',    'User Input'],
      ['slash_command', 'Slash Command'],
      ['tool_call',     'Tool Call'],
      ['schedule',      'Schedule'],
      ['webhook',       'Webhook'],
      ['background',    'Background'],
    ]) {
      const opt = document.createElement('option');
      opt.value = val;
      opt.textContent = text;
      if ((agent.trigger_type || 'user_input') === val) opt.selected = true;
      triggerSel.appendChild(opt);
    }
    triggerRow.appendChild(triggerLabel);
    triggerRow.appendChild(triggerSel);
    body.appendChild(triggerRow);

    const keyRow = document.createElement('div');
    keyRow.className = 'agents-field-group';
    keyRow.style.display = (agent.trigger_type && agent.trigger_type !== 'user_input') ? '' : 'none';
    const keyLabel = document.createElement('label');
    keyLabel.className = 'agents-field-label';
    keyLabel.textContent = 'Trigger Key';
    const keyInput = document.createElement('input');
    keyInput.type = 'text';
    keyInput.className = 'agents-input';
    keyInput.dataset.field = 'trigger_key';
    keyInput.value = agent.trigger_key || '';
    keyInput.placeholder = _triggerKeyPlaceholder(agent.trigger_type || 'user_input');
    keyRow.appendChild(keyLabel);
    keyRow.appendChild(keyInput);
    body.appendChild(keyRow);

    triggerSel.addEventListener('change', () => {
      keyRow.style.display = triggerSel.value !== 'user_input' ? '' : 'none';
      keyInput.placeholder = _triggerKeyPlaceholder(triggerSel.value);
    });
  }

  // ── Prompt slots ────────────────────────────────────────────────────────
  // Admin (isEditable) sees the full slot editor; everyone else sees the
  // override view for unlocked slots + read-only base for locked ones.
  const slotsHost = document.createElement('div');
  slotsHost.className = 'agents-field-group';
  slotsHost.dataset.role = 'slots-host';

  const slotsCard = document.createElement('div');
  slotsCard.className = 'agents-llm-card';

  const slotsHeader = document.createElement('div');
  slotsHeader.className = 'agents-llm-header';
  const slotsTitle = document.createElement('span');
  slotsTitle.className = 'agents-llm-title';
  slotsTitle.textContent = 'Prompt Slots';
  const slotsChevron = document.createElement('span');
  slotsChevron.className = 'agents-llm-chevron';
  slotsChevron.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>`;
  slotsHeader.appendChild(slotsTitle);
  slotsHeader.appendChild(slotsChevron);

  const slotsBody = document.createElement('div');
  slotsBody.className = 'agents-llm-body';
  slotsBody.style.display = 'none';

  slotsHeader.addEventListener('click', () => {
    const open = slotsBody.style.display === 'none';
    slotsBody.style.display = open ? 'flex' : 'none';
    slotsChevron.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
  });

  const slotsHint = document.createElement('span');
  slotsHint.className = 'agents-field-hint';
  slotsHint.textContent = isEditable
    ? 'Slots are concatenated in order into the system message. Lock a slot to keep it admin-only; otherwise users may write overrides.'
    : 'You can override unlocked slots for yourself without affecting other users.';
  slotsBody.appendChild(slotsHint);

  const slotsList = document.createElement('div');
  slotsList.className = 'agents-slots-list';
  slotsList.dataset.role = 'slots-list';
  slotsList.style.marginTop = '8px';
  slotsBody.appendChild(slotsList);

  if (isEditable) {
    const slotsAddBtn = document.createElement('button');
    slotsAddBtn.type = 'button';
    slotsAddBtn.className = 'agents-btn';
    slotsAddBtn.dataset.role = 'slots-add';
    slotsAddBtn.style.marginTop = '8px';
    slotsAddBtn.textContent = '+ Add slot';
    slotsBody.appendChild(slotsAddBtn);
  }

  slotsCard.appendChild(slotsHeader);
  slotsCard.appendChild(slotsBody);
  slotsHost.appendChild(slotsCard);
  body.appendChild(slotsHost);
  // Track slot edit state on the panel for save handlers.
  panelEl._slotState = { slots: [], overrides: {}, resetOverridesFor: new Set(), userRole: 'member', loaded: false };
  _loadAndRenderSlots(panelEl, agent, isEditable);

  // Admin-only: Discoverable toggle for system templates
  if (!isEditable && _userIsAdmin && agent.source === 'template') {
    const discGroup = document.createElement('div');
    discGroup.className = 'agents-field-group';
    discGroup.innerHTML = `
      <label class="agents-field-label">Discoverable</label>
      <span class="agents-field-hint">Show this template in the "New Agent" creation dropdown.</span>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-top:4px;">
        <input type="checkbox" data-field="discoverable" ${agent.discoverable ? 'checked' : ''}>
        <span style="font-size:13px;color:var(--fg-2)">Show in New Agent menu</span>
      </label>
    `;
    body.appendChild(discGroup);

    const content = panelEl.querySelector('.agent-detail-content');
    const bar = document.createElement('div');
    bar.className = 'agents-save-bar';
    const saveBtn = _btn('Save', 'agents-btn primary');
    const msg = document.createElement('span');
    msg.className = 'agents-save-msg';
    saveBtn.addEventListener('click', async () => {
      msg.textContent = '';
      msg.className = 'agents-save-msg';
      const cb = panelEl.querySelector('[data-field="discoverable"]');
      try {
        const res = await fetch(`/api/v1/agent-templates/config`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ user_id: app.currentUserId, template_id: agent.id, discoverable: cb ? cb.checked : false }),
        });
        const data = await res.json();
        if (res.ok) {
          agent.discoverable = data.template?.discoverable;
          const idx = _agents.findIndex(a => a.id === agent.id);
          if (idx !== -1) _agents[idx].discoverable = agent.discoverable;
          msg.textContent = '✓ Saved';
          msg.className = 'agents-save-msg';
        } else {
          msg.textContent = data.detail || 'Save failed';
          msg.className = 'agents-save-msg error';
        }
      } catch (e) {
        msg.textContent = `Error: ${e.message}`;
        msg.className = 'agents-save-msg error';
      }
    });
    bar.appendChild(saveBtn);
    bar.appendChild(msg);
    if (content) content.appendChild(bar);
  }

  // ── External Data Sources (per-agent) ───────────────────────────────────────
  if (isEditable) {
    const dsHost = document.createElement('div');
    dsHost.className = 'agents-field-group';
    dsHost.dataset.role = 'agent-data-sources';
    body.appendChild(dsHost);
    try {
      if (window.AgentDataSourcesUI && typeof window.AgentDataSourcesUI.mount === 'function') {
        window.AgentDataSourcesUI.mount(dsHost, agent, app.currentUserId);
      } else {
        // Lazy-load if the module loaded after this tab opened.
        import('./data-sources.js').then(mod => {
          if (mod && mod.mount) mod.mount(dsHost, agent, app.currentUserId);
        }).catch(() => {
          dsHost.innerHTML = '<div class="agents-field-hint">Data sources editor module not loaded.</div>';
        });
      }
    } catch (e) {
      dsHost.innerHTML = `<div class="agents-field-hint">Error mounting data sources: ${e.message}</div>`;
    }
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

async function _loadAndRenderSlots(panelEl, agent, _isEditable) {
  const listEl = panelEl.querySelector('[data-role="slots-list"]');
  if (!listEl) return;
  listEl.innerHTML = '<div style="font-size:12px;color:var(--fg-muted,#565f89);">Loading slots…</div>';
  let data = null;
  try {
    const res = await fetch(
      `/api/v1/agents/${encodeURIComponent(agent.id)}/slots?user_id=${encodeURIComponent(app.currentUserId)}`
    );
    if (res.ok) data = await res.json();
  } catch (e) { /* ignore — fall back to empty list */ }

  const state = panelEl._slotState || { slots: [], overrides: {}, resetOverridesFor: new Set(), userRole: 'member', loaded: false };
  state.userRole = data?.user_role || 'member';
  state.slots = (data?.slots || []).map(s => ({
    slot_name: s.slot_name,
    order_index: s.order_index || 0,
    lock: !!s.lock,
    merge_mode: s.merge_mode || 'replace',
    content: s.content || '',
  }));
  state.overrides = {};
  for (const s of (data?.slots || [])) {
    if (s.override_content !== undefined && s.override_content !== null) {
      state.overrides[s.slot_name] = s.override_content;
    }
  }
  state.resetOverridesFor = new Set();
  state.loaded = true;
  panelEl._slotState = state;
  const adminEditor = state.userRole === 'admin';
  // Hide the "Add slot" button for non-admins (server would reject it anyway).
  const addBtn = panelEl.querySelector('[data-role="slots-add"]');
  if (addBtn && !adminEditor) addBtn.style.display = 'none';
  _renderSlotsList(panelEl, agent, adminEditor);

  if (adminEditor && addBtn) {
    addBtn.addEventListener('click', () => {
      const nextOrder = (state.slots.reduce((m, s) => Math.max(m, s.order_index || 0), 0) || 0) + 10;
      let n = 1;
      let nm = 'new_slot';
      while (state.slots.some(s => s.slot_name === nm)) { n += 1; nm = `new_slot_${n}`; }
      state.slots.push({ slot_name: nm, order_index: nextOrder, lock: false, merge_mode: 'replace', content: '' });
      _renderSlotsList(panelEl, agent, adminEditor);
    });
  }
}

function _renderSlotsList(panelEl, agent, adminEditor) {
  const listEl = panelEl.querySelector('[data-role="slots-list"]');
  if (!listEl) return;
  const state = panelEl._slotState;
  listEl.innerHTML = '';
  if (!state.slots.length) {
    listEl.innerHTML = '<div style="font-size:12px;color:var(--fg-muted,#565f89);">No prompt slots defined yet.</div>';
    return;
  }
  // Keep slot list sorted by order_index for consistent rendering.
  state.slots.sort((a, b) => (a.order_index || 0) - (b.order_index || 0));
  for (let i = 0; i < state.slots.length; i++) {
    listEl.appendChild(_renderSlotCard(panelEl, agent, adminEditor, i));
  }
}

function _renderSlotCard(panelEl, agent, adminEditor, idx) {
  const state = panelEl._slotState;
  const slot = state.slots[idx];
  const card = document.createElement('div');
  card.className = 'agents-slot-card';
  card.style.cssText = 'border:1px solid var(--border,#2a2a3a);border-radius:6px;padding:10px;margin-bottom:8px;background:var(--bg-1,#1a1b26);';

  // Header row: name + lock + merge + order + delete
  const head = document.createElement('div');
  head.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;';
  if (adminEditor) {
    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'agents-input';
    nameInput.value = slot.slot_name;
    nameInput.style.cssText = 'flex:1;min-width:140px;';
    nameInput.addEventListener('input', () => { slot.slot_name = nameInput.value.trim(); });
    head.appendChild(nameInput);

    const orderInput = document.createElement('input');
    orderInput.type = 'number';
    orderInput.className = 'agents-input';
    orderInput.value = slot.order_index;
    orderInput.style.cssText = 'width:70px;';
    orderInput.title = 'Order (lower = earlier in system prompt)';
    orderInput.addEventListener('input', () => {
      slot.order_index = parseInt(orderInput.value, 10) || 0;
    });
    head.appendChild(orderInput);

    const lockLabel = document.createElement('label');
    lockLabel.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;';
    const lockCb = document.createElement('input');
    lockCb.type = 'checkbox';
    lockCb.checked = !!slot.lock;
    lockCb.addEventListener('change', () => {
      slot.lock = lockCb.checked;
      // Locking disables merge_mode in the UI.
      _renderSlotsList(panelEl, agent, adminEditor);
    });
    const lockTxt = document.createElement('span'); lockTxt.textContent = 'admin only';
    lockLabel.appendChild(lockCb); lockLabel.appendChild(lockTxt);
    head.appendChild(lockLabel);

    const modeSel = document.createElement('select');
    modeSel.className = 'agents-input';
    modeSel.style.cssText = 'width:110px;';
    for (const mode of ['replace', 'append']) {
      const opt = document.createElement('option');
      opt.value = mode; opt.textContent = mode;
      if (slot.merge_mode === mode) opt.selected = true;
      modeSel.appendChild(opt);
    }
    modeSel.disabled = slot.lock;
    modeSel.title = slot.lock ? 'Locked slots have no overrides — merge mode does not apply.' : 'How a user override combines with the admin base.';
    modeSel.addEventListener('change', () => { slot.merge_mode = modeSel.value; });
    head.appendChild(modeSel);

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'agents-btn';
    delBtn.textContent = '🗑';
    delBtn.title = 'Delete this slot (also drops all user overrides for it)';
    delBtn.addEventListener('click', () => {
      if (!confirm(`Delete slot "${slot.slot_name}"? This also drops all user overrides for it.`)) return;
      state.slots.splice(idx, 1);
      _renderSlotsList(panelEl, agent, adminEditor);
    });
    head.appendChild(delBtn);
  } else {
    const label = document.createElement('div');
    label.style.cssText = 'flex:1;font-size:13px;font-weight:600;color:var(--fg-1);';
    label.textContent = slot.slot_name;
    head.appendChild(label);
    const badge = document.createElement('span');
    badge.style.cssText = 'font-size:11px;padding:2px 6px;border-radius:4px;background:var(--bg-2,#24253a);color:var(--fg-muted,#565f89);';
    badge.textContent = slot.lock ? 'locked' : `unlocked · ${slot.merge_mode}`;
    head.appendChild(badge);
  }
  card.appendChild(head);

  // Body: admin sees content textarea. Non-admin sees read-only base + override editor when unlocked.
  if (adminEditor) {
    const ta = document.createElement('textarea');
    ta.className = 'agents-textarea';
    ta.rows = 6;
    ta.value = slot.content || '';
    ta.placeholder = 'Slot content (admin base)';
    ta.addEventListener('input', () => { slot.content = ta.value; });
    card.appendChild(ta);

    // Reset-overrides checkbox shown only for unlocked slots (locked → nothing to reset).
    if (!slot.lock) {
      const resetWrap = document.createElement('label');
      resetWrap.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;color:var(--fg-muted,#565f89);margin-top:6px;cursor:pointer;';
      const resetCb = document.createElement('input');
      resetCb.type = 'checkbox';
      resetCb.checked = state.resetOverridesFor.has(slot.slot_name);
      resetCb.addEventListener('change', () => {
        if (resetCb.checked) state.resetOverridesFor.add(slot.slot_name);
        else state.resetOverridesFor.delete(slot.slot_name);
      });
      const resetTxt = document.createElement('span');
      resetTxt.textContent = 'On save: reset existing user overrides for this slot';
      resetWrap.appendChild(resetCb); resetWrap.appendChild(resetTxt);
      card.appendChild(resetWrap);
    }
  } else {
    const base = document.createElement('div');
    base.style.cssText = 'font-size:12px;background:var(--bg-2,#24253a);border-radius:4px;padding:8px;white-space:pre-wrap;color:var(--fg-muted,#a9b1d6);max-height:200px;overflow:auto;';
    base.textContent = slot.content || '(empty)';
    card.appendChild(base);

    if (slot.lock) {
      const note = document.createElement('div');
      note.style.cssText = 'font-size:11px;color:var(--fg-muted,#565f89);margin-top:6px;';
      note.textContent = '🔒 Locked by admin — cannot be overridden.';
      card.appendChild(note);
    } else {
      const ovLabel = document.createElement('div');
      ovLabel.style.cssText = 'font-size:11px;color:var(--fg-muted,#565f89);margin-top:10px;';
      ovLabel.textContent = slot.merge_mode === 'append'
        ? 'Your override is APPENDED below the admin base.'
        : 'Your override REPLACES the admin base for you.';
      card.appendChild(ovLabel);

      const ovTa = document.createElement('textarea');
      ovTa.className = 'agents-textarea';
      ovTa.rows = 4;
      ovTa.value = state.overrides[slot.slot_name] || '';
      ovTa.placeholder = 'Your override (leave empty to inherit admin base)';
      ovTa.addEventListener('input', () => { state.overrides[slot.slot_name] = ovTa.value; });
      card.appendChild(ovTa);

      const clearBtn = document.createElement('button');
      clearBtn.type = 'button';
      clearBtn.className = 'agents-btn';
      clearBtn.style.marginTop = '4px';
      clearBtn.textContent = 'Clear my override';
      clearBtn.addEventListener('click', async () => {
        try {
          await fetch(
            `/api/v1/agents/${encodeURIComponent(agent.id)}/my-prompts/${encodeURIComponent(slot.slot_name)}?user_id=${encodeURIComponent(app.currentUserId)}`,
            { method: 'DELETE' }
          );
          state.overrides[slot.slot_name] = '';
          ovTa.value = '';
        } catch (e) { /* noop */ }
      });
      card.appendChild(clearBtn);
    }
  }

  return card;
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

// Resolve loop_logic as an object array without touching _lvPendingChanges
function _localLoopLogicObjs(agent) {
  const ll = Array.isArray(agent.loop_logic) ? agent.loop_logic : [];
  if (ll.length === 0 || typeof ll[0] === 'string') {
    return LOOP_NODES.map(n => ({ node: n.id, enabled: true }));
  }
  return ll.map(item => ({ ...item }));
}

async function _renderToolsTab(body, agent, panelEl) {
  const isEditable = agent.source === 'custom';

  // ── Non-editable: read-only tool list ────────────────────────────────────
  if (!isEditable) {
    const tools = _toolsForAgent(agent);
    const toolSet = new Set(tools);
    const section = document.createElement('div');
    section.className = 'agents-tools-list';
    const intro = document.createElement('div');
    intro.style.cssText = 'font-size:12px;color:#565f89;margin-bottom:14px;line-height:1.5;';
    intro.textContent = `This agent has access to ${tools.length} tools. Tools marked destructive can modify data or execute code.`;
    section.appendChild(intro);

    const agentId = agent.id || '';
    const isAdmin = agent.is_admin_agent || agentId.startsWith('opt_');

    for (const cat of TOOL_CATEGORIES) {
      if (cat.condition === 'admin' && !isAdmin) continue;
      const catTools = cat.tools.filter(n => toolSet.has(n));
      if (catTools.length === 0) continue;

      const wrapper = document.createElement('div');
      wrapper.className = 'agents-tool-category';

      const header = document.createElement('div');
      header.className = 'agents-tool-category-header';
      header.innerHTML = `<span class="agents-tool-category-chevron">▼</span>
        <span class="agents-tool-category-label">${_esc(cat.label)}</span>
        <span class="agents-tool-category-count">${catTools.length}</span>`;

      const catBody = document.createElement('div');
      catBody.className = 'agents-tool-category-body';

      header.addEventListener('click', () => {
        header.classList.toggle('collapsed');
        catBody.classList.toggle('collapsed');
      });

      for (const name of catTools) {
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
        catBody.appendChild(item);
      }

      wrapper.appendChild(header);
      wrapper.appendChild(catBody);
      section.appendChild(wrapper);
    }

    // Uncategorized tools (custom skills or tools not in any category)
    const categorized = new Set(TOOL_CATEGORIES.flatMap(c => c.tools));
    const uncategorized = tools.filter(n => !categorized.has(n));
    if (uncategorized.length > 0) {
      const wrapper = document.createElement('div');
      wrapper.className = 'agents-tool-category';
      const header = document.createElement('div');
      header.className = 'agents-tool-category-header';
      header.innerHTML = `<span class="agents-tool-category-chevron">▼</span>
        <span class="agents-tool-category-label">Custom Skills</span>
        <span class="agents-tool-category-count">${uncategorized.length}</span>`;
      const catBody = document.createElement('div');
      catBody.className = 'agents-tool-category-body';
      header.addEventListener('click', () => {
        header.classList.toggle('collapsed');
        catBody.classList.toggle('collapsed');
      });
      for (const name of uncategorized) {
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
        catBody.appendChild(item);
      }
      wrapper.appendChild(header);
      wrapper.appendChild(catBody);
      section.appendChild(wrapper);
    }

    body.appendChild(section);
    return;
  }

  // ── Editable: full guardrails + per-tool controls ─────────────────────────

  // Mutable state captured by closures for the Save handler
  const sp0 = (agent.safety_policy && typeof agent.safety_policy === 'object') ? agent.safety_policy : {};
  const blockedSet = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  const dtSet      = new Set(Array.isArray(sp0.destructive_tools) ? sp0.destructive_tools : []);
  let autoConfirmAll = Boolean(sp0.auto_confirm);
  let maxConcVal     = sp0.max_concurrent_tools || '';

  // Loop-logic state for the guardrail master toggle
  let localLL   = _localLoopLogicObjs(agent);
  let llDirty   = false;
  const llGuard = localLL.find(o => o.node === 'guardrails');
  let guardrailOn = llGuard ? llGuard.enabled !== false : true;

  function setGuardrailEnabled(on) {
    guardrailOn = on;
    llDirty     = true;
    const idx   = localLL.findIndex(o => o.node === 'guardrails');
    if (idx !== -1) localLL[idx] = { ...localLL[idx], enabled: on };
    else            localLL.push({ node: 'guardrails', enabled: on });
  }

  // ── Section 1: Guardrail master toggle ──────────────────────────────────
  const guardrailGroup = document.createElement('div');
  guardrailGroup.className = 'agents-field-group';

  const guardrailLabelEl = document.createElement('label');
  guardrailLabelEl.className = 'agents-field-label';
  guardrailLabelEl.textContent = 'Guardrails';
  guardrailGroup.appendChild(guardrailLabelEl);

  const guardrailHintEl = document.createElement('span');
  guardrailHintEl.className = 'agents-field-hint';
  guardrailHintEl.textContent = 'Master switch for the confirmation gate. When off, all tools run without prompting regardless of per-tool settings.';
  guardrailGroup.appendChild(guardrailHintEl);

  const guardrailRow = document.createElement('label');
  guardrailRow.style.cssText = 'display:flex;align-items:center;gap:8px;cursor:pointer;margin-top:10px;';
  const guardrailCb = document.createElement('input');
  guardrailCb.type    = 'checkbox';
  guardrailCb.checked = guardrailOn;
  guardrailCb.addEventListener('change', () => setGuardrailEnabled(guardrailCb.checked));
  const guardrailTxt = document.createElement('span');
  guardrailTxt.style.cssText = 'font-size:13px;color:var(--fg-1);';
  guardrailTxt.textContent = 'Require confirmation before running destructive tools';
  guardrailRow.appendChild(guardrailCb);
  guardrailRow.appendChild(guardrailTxt);
  guardrailGroup.appendChild(guardrailRow);
  body.appendChild(guardrailGroup);

  // ── Section 2: Execution settings (auto-confirm all, max concurrent) ────
  const execGroup = document.createElement('div');
  execGroup.className = 'agents-field-group';

  const execLabel = document.createElement('label');
  execLabel.className = 'agents-field-label';
  execLabel.textContent = 'Execution Settings';
  execGroup.appendChild(execLabel);

  // Auto-confirm all
  const autoRow = document.createElement('label');
  autoRow.style.cssText = 'display:flex;align-items:center;gap:8px;cursor:pointer;margin-top:10px;';
  const autoCb = document.createElement('input');
  autoCb.type    = 'checkbox';
  autoCb.checked = autoConfirmAll;
  autoCb.addEventListener('change', () => { autoConfirmAll = autoCb.checked; });
  const autoTxt = document.createElement('span');
  autoTxt.style.cssText = 'font-size:13px;color:var(--fg-1);';
  autoTxt.textContent = 'Auto-confirm all tools (bypass guardrail for this agent — useful for automation)';
  autoRow.appendChild(autoCb);
  autoRow.appendChild(autoTxt);
  execGroup.appendChild(autoRow);

  // Max concurrent
  const maxRow = document.createElement('div');
  maxRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:10px;';
  const maxLbl = document.createElement('span');
  maxLbl.style.cssText = 'font-size:13px;color:var(--fg-1);white-space:nowrap;';
  maxLbl.textContent = 'Max concurrent tools:';
  const maxInput = document.createElement('input');
  maxInput.type        = 'number';
  maxInput.min         = '0';
  maxInput.max         = '20';
  maxInput.className   = 'agents-input';
  maxInput.style.width = '70px';
  maxInput.value       = sp0.max_concurrent_tools || '';
  maxInput.placeholder = 'unlimited';
  maxInput.addEventListener('input', () => { maxConcVal = maxInput.value; });
  const maxHint = document.createElement('span');
  maxHint.style.cssText = 'font-size:11px;color:var(--fg-3);';
  maxHint.textContent = '0 = unlimited';
  maxRow.appendChild(maxLbl);
  maxRow.appendChild(maxInput);
  maxRow.appendChild(maxHint);
  execGroup.appendChild(maxRow);
  body.appendChild(execGroup);

  // ── Section 3: Per-tool mode ─────────────────────────────────────────────
  const toolsGroup = document.createElement('div');
  toolsGroup.className = 'agents-field-group';

  const toolsLabelEl = document.createElement('label');
  toolsLabelEl.className = 'agents-field-label';
  toolsLabelEl.textContent = 'Per-Tool Mode';
  toolsGroup.appendChild(toolsLabelEl);

  const toolsHintEl = document.createElement('span');
  toolsHintEl.className = 'agents-field-hint';
  toolsHintEl.textContent = 'Auto-confirm: runs freely. Ask: pauses and asks before running. Deny: blocked entirely.';
  toolsGroup.appendChild(toolsHintEl);

  const loadingEl = document.createElement('div');
  loadingEl.style.cssText = 'font-size:12px;color:var(--fg-3);margin-top:10px;';
  loadingEl.textContent = 'Loading…';
  toolsGroup.appendChild(loadingEl);
  body.appendChild(toolsGroup);

  // ── Save bar (sticky, outside scrollable body) ───────────────────────────
  const content = panelEl ? panelEl.querySelector('.agent-detail-content') : null;
  const bar     = document.createElement('div');
  bar.className = 'agents-save-bar';
  const saveBtn = _btn('Save Changes', 'agents-btn primary');
  const saveMsg = document.createElement('span');
  saveMsg.className = 'agents-save-msg';

  saveBtn.addEventListener('click', async () => {
    saveMsg.textContent = 'Saving…';
    saveMsg.className   = 'agents-save-msg';

    const newSp = {
      ...sp0,
      destructive_tools: [...dtSet],
      auto_confirm:      autoConfirmAll,
    };
    const mv = parseInt(maxConcVal, 10);
    if (mv > 0) newSp.max_concurrent_tools = mv;
    else        delete newSp.max_concurrent_tools;

    const updates = {
      allowed_tools: [...blockedSet],
      safety_policy: newSp,
    };
    if (llDirty) updates.loop_logic = localLL;

    try {
      const res = await fetch(`/api/v1/agents/${agent.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: app.currentUserId, ...updates }),
      });
      const data = await res.json();
      if (res.ok) {
        const idx = _agents.findIndex(a => a.id === agent.id);
        if (idx !== -1) Object.assign(_agents[idx], data.agent);
        Object.assign(agent, data.agent);
        saveMsg.textContent = '✓ Saved';
        saveMsg.className   = 'agents-save-msg';
      } else {
        saveMsg.textContent = data.detail || 'Save failed';
        saveMsg.className   = 'agents-save-msg error';
      }
    } catch (e) {
      saveMsg.textContent = `Error: ${e.message}`;
      saveMsg.className   = 'agents-save-msg error';
    }
  });

  bar.appendChild(saveBtn);
  bar.appendChild(saveMsg);
  if (content) content.appendChild(bar);

  // ── Async: fetch tool metadata and render per-tool rows ──────────────────
  try {
    const allMeta    = await fetchAllToolMeta();
    const metaByName = new Map(allMeta.map(t => [t.name, t]));
    const tools      = _toolsForAgent(agent);
    const toolSet    = new Set(tools);

    loadingEl.remove();

    const agentId = agent.id || '';
    const isAdmin = agent.is_admin_agent || agentId.startsWith('opt_');

    // Helper: build a tool row with segmented control or "always on" pill
    function buildToolRow(name, meta) {
      const isTier1 = TIER_1_ALWAYS_ON.has(name);

      const row = document.createElement('div');
      row.style.cssText = 'display:grid;grid-template-columns:1fr 2fr auto;align-items:center;gap:8px;' +
                          'padding:5px 8px;border-radius:5px;background:var(--bg-2);';

      const nameEl = document.createElement('span');
      nameEl.style.cssText = 'font-size:12px;font-weight:500;color:var(--fg-1);font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      nameEl.textContent = name;
      row.appendChild(nameEl);

      const descEl = document.createElement('span');
      descEl.style.cssText = 'font-size:11px;color:var(--fg-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      descEl.textContent = TOOL_DESCRIPTIONS[name] || (meta && meta.description) || '';
      row.appendChild(descEl);

      if (isTier1) {
        const pill = document.createElement('span');
        pill.className = 'agents-tool-always-on';
        pill.textContent = 'always on';
        row.appendChild(pill);
      } else {
        let initialMode;
        if      (blockedSet.has(name))          initialMode = 'deny';
        else if (dtSet.has(name) || DESTRUCTIVE.has(name) || Boolean(meta && meta.requires_confirmation))
                                                initialMode = 'ask';
        else                                    initialMode = 'auto';

        const seg = document.createElement('div');
        seg.style.cssText = 'display:flex;border:1px solid var(--border);border-radius:4px;overflow:hidden;flex-shrink:0;font-size:11px;';

        const MODES = [
          { id: 'auto', label: 'Auto', activeStyle: 'background:#9ece6a;color:#1a1b26;' },
          { id: 'ask',  label: 'Ask',  activeStyle: 'background:#e0af68;color:#1a1b26;' },
          { id: 'deny', label: 'Deny', activeStyle: 'background:#f7768e;color:#fff;'    },
        ];
        const segBtns = {};

        function applyMode(mode) {
          blockedSet.delete(name);
          dtSet.delete(name);
          if (mode === 'deny') blockedSet.add(name);
          else if (mode === 'ask') dtSet.add(name);
          for (const m of MODES) {
            const b = segBtns[m.id];
            if (m.id === mode) {
              b.style.cssText = 'border:none;cursor:pointer;padding:3px 8px;font-weight:600;border-right:1px solid var(--border);' + m.activeStyle;
            } else {
              b.style.cssText = 'border:none;cursor:pointer;padding:3px 8px;font-weight:400;color:var(--fg-2);background:var(--bg-3);border-right:1px solid var(--border);';
            }
          }
          segBtns['deny'].style.borderRight = 'none';
        }

        for (const m of MODES) {
          const b = document.createElement('button');
          b.textContent = m.label;
          b.addEventListener('click', () => applyMode(m.id));
          segBtns[m.id] = b;
          seg.appendChild(b);
        }
        applyMode(initialMode);
        row.appendChild(seg);
      }

      return row;
    }

    // Render categories as collapsible accordions
    const categorized = new Set(TOOL_CATEGORIES.flatMap(c => c.tools));

    for (const cat of TOOL_CATEGORIES) {
      if (cat.condition === 'admin' && !isAdmin) continue;
      const catTools = cat.tools.filter(n => toolSet.has(n));
      if (catTools.length === 0) continue;

      const wrapper = document.createElement('div');
      wrapper.className = 'agents-tool-category';

      const header = document.createElement('div');
      header.className = 'agents-tool-category-header';
      header.innerHTML = `<span class="agents-tool-category-chevron">▼</span>
        <span class="agents-tool-category-label">${_esc(cat.label)}</span>
        <span class="agents-tool-category-count">${catTools.length}</span>`;

      const catBody = document.createElement('div');
      catBody.className = 'agents-tool-category-body';

      header.addEventListener('click', () => {
        header.classList.toggle('collapsed');
        catBody.classList.toggle('collapsed');
      });

      for (const name of catTools) {
        catBody.appendChild(buildToolRow(name, metaByName.get(name) || {}));
      }

      wrapper.appendChild(header);
      wrapper.appendChild(catBody);
      toolsGroup.appendChild(wrapper);
    }

    // Custom Skills category: tools from metadata not in any built-in category
    const uncategorized = tools.filter(n => !categorized.has(n) && !TIER_0_ADMIN.has(n));
    if (uncategorized.length > 0) {
      const wrapper = document.createElement('div');
      wrapper.className = 'agents-tool-category';
      const header = document.createElement('div');
      header.className = 'agents-tool-category-header';
      header.innerHTML = `<span class="agents-tool-category-chevron">▼</span>
        <span class="agents-tool-category-label">Custom Skills</span>
        <span class="agents-tool-category-count">${uncategorized.length}</span>`;
      const catBody = document.createElement('div');
      catBody.className = 'agents-tool-category-body';
      header.addEventListener('click', () => {
        header.classList.toggle('collapsed');
        catBody.classList.toggle('collapsed');
      });
      for (const name of uncategorized) {
        catBody.appendChild(buildToolRow(name, metaByName.get(name) || {}));
      }
      wrapper.appendChild(header);
      wrapper.appendChild(catBody);
      toolsGroup.appendChild(wrapper);
    }

  } catch (e) {
    loadingEl.textContent = 'Failed to load tool list.';
  }
}

// ── Connections tab ───────────────────────────────────────────────────────────

const _CONN_ICONS = {
  // Channels
  telegram:  'send',
  twilio:    'phone',
  email:     'mail',
  whatsapp:  'message-circle',
  discord:   'gamepad-2',
  slack:     'briefcase',
  // Integrations
  google:    'search',
  microsoft: 'monitor',
  yahoo:     'mail',
  dropbox:   'folder-open',
  github:    'github',
  bank:      'landmark',
  search:    'globe',
  // Social Media
  facebook:  'users',
  instagram: 'camera',
  twitter:   'twitter',
  linkedin:  'linkedin',
  tiktok:    'music',
  pinterest: 'image',
  reddit:    'message-square',
  snapchat:  'zap',
  twitch:    'tv',
};

async function _renderConnectionsTab(body, agent) {
  body.innerHTML = '<div class="conn-loading">Loading connections…</div>';

  let connections = [];
  let userRole = 'member';
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`);
    if (res.ok) {
      const data = await res.json();
      connections = data.connections || [];
      userRole = data.user_role || 'member';
    }
  } catch (e) {
    body.innerHTML = `<div class="conn-loading" style="color:#f7768e">Failed to load connections: ${_esc(e.message)}</div>`;
    return;
  }

  const canEdit = userRole === 'admin';

  body.innerHTML = '';

  if (!canEdit) {
    const notice = document.createElement('div');
    notice.style.cssText = 'font-size:11px;color:#565f89;padding:8px 12px;background:#1a1b26;border:1px solid #2a2a4a;border-radius:6px;margin-bottom:12px;line-height:1.5;';
    notice.textContent = 'Integration toggles are managed by agent admins. You can connect your accounts on enabled integrations.';
    body.appendChild(notice);
  }

  const sections = [
    { key: 'channel',     label: 'Channels',     hint: 'How this agent sends and receives messages.' },
    { key: 'integration', label: 'Integrations', hint: 'Services and data sources this agent can access.' },
    { key: 'social',      label: 'Social Media', hint: 'Social platforms this agent can post and interact on.' },
  ];

  for (const sec of sections) {
    const items = connections.filter(c => c.section === sec.key);
    if (!items.length) continue;

    const secEl = document.createElement('div');
    secEl.className = 'conn-section collapsed';

    const header = document.createElement('div');
    header.className = 'conn-section-header';
    header.innerHTML = `
      <span class="conn-section-chevron">${icon('chevron-right', { size: '12px' })}</span>
      <span class="conn-section-label">${_esc(sec.label)}</span>
      <span class="conn-section-hint">${_esc(sec.hint)}</span>
    `;

    const grid = document.createElement('div');
    grid.className = 'conn-grid';

    for (const conn of items) {
      grid.appendChild(_buildConnectionCard(agent, conn, canEdit));
    }

    header.addEventListener('click', () => {
      const isCollapsed = secEl.classList.toggle('collapsed');
      header.querySelector('.conn-section-chevron').innerHTML = icon(isCollapsed ? 'chevron-right' : 'chevron-down', { size: '12px' });
    });

    secEl.appendChild(header);
    secEl.appendChild(grid);
    body.appendChild(secEl);
  }

  // Populate Telegram mode badge and wire Test button
  _loadTelegramCardStatus(body);
}

async function _loadTelegramCardStatus(body) {
  let mode = 'unknown';
  try {
    const r = await fetch('/admin/communications/plugins');
    if (r.ok) {
      const d = await r.json();
      const base = d.webhook_base_url || '';
      const local = ['localhost', '127.0.0.1', '0.0.0.0'];
      mode = (base && !local.some(h => base.includes(h))) ? 'webhook' : 'polling';
    }
  } catch (_e) {}

  const modeBadge = mode === 'webhook'
    ? '<span style="font-size:10px;background:#1a3a2a;color:#9ece6a;padding:2px 6px;border-radius:3px;font-weight:600;">WEBHOOK</span>'
    : '<span style="font-size:10px;background:#2a2a4a;color:#7dcfff;padding:2px 6px;border-radius:3px;font-weight:600;">POLLING</span>';

  const modeNote = mode === 'webhook'
    ? '<span style="font-size:11px;color:#565f89;margin-left:4px;">Messages arrive via webhook</span>'
    : '<span style="font-size:11px;color:#565f89;margin-left:4px;">Server is polling for messages</span>';

  body.querySelectorAll('.conn-tg-mode-info').forEach(el => {
    el.innerHTML = modeBadge + modeNote;
  });

  // Cache real token value as user types (prevents Chrome autofill corruption)
  body.querySelectorAll('.conn-token-input').forEach(inp => {
    inp.dataset.realValue = inp.value;
    inp.addEventListener('input', () => { inp.dataset.realValue = inp.value; });
  });

  body.querySelectorAll('.conn-tg-test-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const card = btn.closest('.conn-fields');
      const tokenInput = card && card.querySelector('.conn-token-input');
      const statusEl = card && card.querySelector('.conn-tg-test-status');
      const rawToken = tokenInput ? (tokenInput.dataset.realValue?.trim() || tokenInput.value.trim()) : '';
      const isMasked = rawToken.includes('•');
      const agentId = btn.dataset.agentId || null;
      const connType = btn.dataset.connType || 'telegram';
      if (!rawToken && !agentId) {
        if (statusEl) { statusEl.textContent = 'Enter a token first.'; statusEl.style.color = '#e0af68'; }
        return;
      }
      btn.disabled = true;
      btn.textContent = 'Testing...';
      if (statusEl) statusEl.textContent = '';
      const payload = isMasked
        ? { agent_id: agentId, connection_type: connType }
        : { token: rawToken, agent_id: agentId, connection_type: connType };
      try {
        const r = await fetch('/admin/communications/plugins/telegram/test-token', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const result = await r.json();
        if (statusEl) {
          if (result.status === 'ok') {
            statusEl.textContent = 'Connected as @' + result.bot_username + ' (' + result.bot_name + ')';
            statusEl.style.color = '#9ece6a';
          } else {
            statusEl.textContent = 'Failed: ' + (result.message || 'unknown');
            statusEl.style.color = '#f7768e';
          }
        }
      } catch (e) {
        if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.style.color = '#f7768e'; }
      }
      btn.disabled = false;
      btn.textContent = 'Test Connection';
    });
  });
}

function _buildConnectionCard(agent, conn, canEdit = true) {
  const isComingSoon = conn.status === 'coming_soon';
  const card = document.createElement('div');
  card.className = 'conn-card' + (isComingSoon ? ' coming-soon' : '') + (conn.enabled ? ' enabled' : '');

  const connIcon = icon(_CONN_ICONS[conn.connection_type] || 'plug', { size: '16px' });

  const toggleTitle = canEdit
    ? (conn.enabled ? 'Disable' : 'Enable')
    : (conn.enabled ? 'Enabled (admin only)' : 'Disabled (admin only)');

  // ── Always-visible header ──
  const header = document.createElement('div');
  header.className = 'conn-card-header';
  header.innerHTML = `
    <span class="conn-icon">${connIcon}</span>
    <span class="conn-name">${_esc(conn.display_name)}</span>
    ${isComingSoon
      ? '<span class="conn-badge coming-soon">Coming soon</span>'
      : `<label class="conn-toggle-wrap" title="${toggleTitle}">
           <input type="checkbox" class="conn-toggle" ${conn.enabled ? 'checked' : ''} ${canEdit ? '' : 'disabled'}>
           <span class="conn-toggle-track"></span>
         </label>`
    }
  `;
  card.appendChild(header);

  // ── Expandable body (provider-specific, hidden by default) ──
  const connBody = _buildConnectionBody(conn, canEdit, agent.id);
  if (connBody) {
    card.appendChild(connBody);
    // Click header (not toggle) to expand/collapse
    header.classList.add('conn-card-header-clickable');
    header.addEventListener('click', e => {
      if (e.target.closest('.conn-toggle-wrap')) return;
      card.classList.toggle('conn-card-expanded');
    });
  }

  if (isComingSoon) return card;

  // Toggle handler (admin only)
  const toggle = card.querySelector('.conn-toggle');
  if (toggle && canEdit) {
    toggle.addEventListener('change', () => _saveConnection(agent, conn, card, toggle.checked));
  }

  // Save button handler (admin only)
  const saveBtn = card.querySelector('.conn-save-btn');
  if (saveBtn && canEdit) {
    saveBtn.addEventListener('click', () => {
      const enabled = toggle ? toggle.checked : conn.enabled;
      _saveConnection(agent, conn, card, enabled);
    });
  }

  // OAuth connect/disconnect handlers (works for google, microsoft, yahoo, dropbox)
  card.querySelectorAll('[data-oauth-connect]').forEach(btn => {
    const provider = btn.dataset.oauthConnect;
    btn.addEventListener('click', () => _oauthConnectFromAgent(provider, agent, conn, card));
  });
  card.querySelectorAll('[data-oauth-disconnect]').forEach(btn => {
    const provider = btn.dataset.oauthDisconnect;
    btn.addEventListener('click', () => _oauthDisconnectFromAgent(provider, agent, conn, card));
  });

  return card;
}

function _buildConnectionBody(conn, canEdit = true, agentId = null) {
  if (conn.status === 'coming_soon') return null;

  const el = document.createElement('div');
  el.className = 'conn-fields';

  if (conn.connection_type === 'telegram') {
    if (!canEdit) {
      const tokenVal = conn.config?.bot_token || '';
      el.innerHTML = `
        <label class="conn-field-label">Bot Token</label>
        <div class="conn-token-row">
          <input type="text" class="agents-input" value="${tokenVal ? '••••••' + _esc(tokenVal.slice(-4)) : 'Not configured'}" readonly style="font-family:monospace;opacity:0.6;">
        </div>
        <div class="conn-tg-mode-info" style="margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:18px;"></div>
      `;
      return el;
    }
    const tokenVal = conn.config?.bot_token || '';
    el.innerHTML = `
      <label class="conn-field-label">Bot Token</label>
      <div class="conn-token-row">
        <input type="text" class="conn-token-input agents-input" placeholder="Enter bot token..." value="${_esc(tokenVal)}" autocomplete="one-time-code" style="font-family:monospace;">
        <button class="agents-btn primary conn-save-btn">Save</button>
      </div>
      <span class="conn-field-hint">From <a href="https://t.me/BotFather" target="_blank" style="color:#7aa2f7">@BotFather</a> &mdash; format: <code style="font-size:10px;color:#a9b1d6;">1234567890:ABCdef...</code></span>
      <div class="conn-tg-mode-info" style="margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:18px;"></div>
      <div style="margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <button class="agents-btn conn-tg-test-btn" data-agent-id="${agentId || ''}" data-conn-type="telegram" style="font-size:11px;padding:4px 10px;">Test Connection</button>
        <span class="conn-tg-test-status" style="font-size:11px;"></span>
      </div>
      <span class="conn-save-msg"></span>
    `;
    return el;
  }

  // ── OAuth-backed integration + social providers ──────────────────────────
  const _OAUTH_PROVIDERS = {
    google: {
      label: 'Google Account',
      hint: 'Link your Google account for Gmail, Drive, Docs, and Calendar access.',
    },
    microsoft: {
      label: 'Microsoft Account',
      hint: 'Link your Microsoft account for Outlook, OneDrive, and SharePoint access.',
    },
    yahoo: {
      label: 'Yahoo Account',
      hint: 'Link your Yahoo account for Yahoo Mail access.',
    },
    dropbox: {
      label: 'Dropbox Account',
      hint: 'Link your Dropbox account for file storage and sharing access.',
    },
    facebook: {
      label: 'Facebook (via Meta)',
      hint: 'Link your Meta account for Facebook Pages and feed access.',
    },
    instagram: {
      label: 'Instagram (via Meta)',
      hint: 'Link your Meta account for Instagram Business account access.',
    },
    twitter: {
      label: 'X (Twitter) Account',
      hint: 'Link your X account to read and post tweets.',
    },
    linkedin: {
      label: 'LinkedIn Account',
      hint: 'Link your LinkedIn account for profile and company page access.',
    },
    tiktok: {
      label: 'TikTok Account',
      hint: 'Link your TikTok account for video and account access.',
    },
    pinterest: {
      label: 'Pinterest Account',
      hint: 'Link your Pinterest account for board and pin access.',
    },
    reddit: {
      label: 'Reddit Account',
      hint: 'Link your Reddit account to read and post to subreddits.',
    },
    snapchat: {
      label: 'Snapchat Account',
      hint: 'Link your Snapchat account for story and audience access.',
    },
    twitch: {
      label: 'Twitch Account',
      hint: 'Link your Twitch account for channel and stream data access.',
    },
  };

  const oauthInfo = _OAUTH_PROVIDERS[conn.connection_type];
  if (oauthInfo) {
    const ct = conn.connection_type;
    const connected = conn[`${ct}_connected`];
    const picture   = conn[`${ct}_picture`] || '';
    const name      = conn[`${ct}_name`] || '';
    const email     = conn[`${ct}_email`] || '';
    const connAt    = conn[`${ct}_connected_at`] || '';

    if (connected) {
      el.innerHTML = `
        <div class="conn-google-account">
          ${picture ? `<img src="${_esc(picture)}" alt="" style="width:28px;height:28px;border-radius:50%;">` : ''}
          <div style="flex:1;min-width:0;">
            <div style="font-weight:500;font-size:12px;">${_esc(name)}</div>
            <div style="font-size:11px;color:var(--fg-muted,#565f89);overflow:hidden;text-overflow:ellipsis;">${_esc(email)}</div>
          </div>
        </div>
        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;">
          <button class="agents-btn" data-oauth-disconnect="${ct}" style="color:#f7768e;border-color:#f7768e;">Disconnect</button>
          ${connAt ? `<span style="font-size:10px;color:var(--fg-muted,#565f89);">Connected ${new Date(connAt).toLocaleDateString()}</span>` : ''}
        </div>
        <span class="conn-save-msg"></span>
      `;
    } else if (!canEdit && !conn.enabled) {
      el.innerHTML = `
        <span style="font-size:12px;color:#565f89;">Not enabled by admin</span>
      `;
    } else {
      el.innerHTML = `
        <button class="agents-btn primary" data-oauth-connect="${ct}" style="width:100%;">Connect ${oauthInfo.label}</button>
        <span class="conn-field-hint" style="margin-top:6px;">${oauthInfo.hint}</span>
        <span class="conn-save-msg"></span>
      `;
    }
    return el;
  }

  // No expandable body for other available-but-unconfigured types yet
  return null;
}

// ── OAuth helpers for Agent Connections tab (generic, all providers) ──────────

let _oauthPopup = null;
let _oauthRefreshCallback = null;

// Listen for OAuth popup completion from any provider
window.addEventListener('message', e => {
  const successTypes = [
    'google-oauth-success',
    'microsoft-oauth-success',
    'yahoo-oauth-success',
    'dropbox-oauth-success',
    'meta-oauth-success',
    'twitter-oauth-success',
    'linkedin-oauth-success',
    'tiktok-oauth-success',
    'pinterest-oauth-success',
    'reddit-oauth-success',
    'snapchat-oauth-success',
    'twitch-oauth-success',
  ];
  if (successTypes.includes(e.data?.type)) {
    if (_oauthPopup) { _oauthPopup.close(); _oauthPopup = null; }
    if (_oauthRefreshCallback) { _oauthRefreshCallback(); _oauthRefreshCallback = null; }
  }
});

async function _oauthConnectFromAgent(provider, agent, conn, cardEl) {
  const msgEl = cardEl.querySelector('.conn-save-msg');
  if (msgEl) { msgEl.textContent = ''; msgEl.className = 'conn-save-msg'; }

  try {
    const res = await fetch(
      `/api/v1/agents/${agent.id}/connections/${provider}/authorize?user_id=${encodeURIComponent(app.currentUserId)}`
    );
    const data = await res.json();
    if (data.error) {
      if (msgEl) { msgEl.textContent = data.error; msgEl.className = 'conn-save-msg error'; }
      return;
    }

    // Set up refresh callback to re-render the connections tab after success
    _oauthRefreshCallback = () => {
      const body = cardEl.closest('.agent-detail-panel')?.querySelector('.agent-detail-body');
      if (body) _renderConnectionsTab(body, agent);
    };

    _oauthPopup = window.open(data.authorize_url, `${provider}-oauth`, 'width=520,height=640,left=200,top=100');
    if (!_oauthPopup) {
      // Popup blocked — fallback to redirect
      window.location.href = data.authorize_url;
    }
  } catch (e) {
    if (msgEl) { msgEl.textContent = `Error: ${e.message}`; msgEl.className = 'conn-save-msg error'; }
  }
}

async function _oauthDisconnectFromAgent(provider, agent, conn, cardEl) {
  const msgEl = cardEl.querySelector('.conn-save-msg');
  if (msgEl) { msgEl.textContent = ''; msgEl.className = 'conn-save-msg'; }

  try {
    const res = await fetch(
      `/api/v1/agents/${agent.id}/connections/${provider}/disconnect?user_id=${encodeURIComponent(app.currentUserId)}`,
      { method: 'DELETE' }
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    // Re-render the connections tab
    const body = cardEl.closest('.agent-detail-panel')?.querySelector('.agent-detail-body');
    if (body) _renderConnectionsTab(body, agent);
  } catch (e) {
    if (msgEl) { msgEl.textContent = `Error: ${e.message}`; msgEl.className = 'conn-save-msg error'; }
  }
}

async function _saveConnection(agent, conn, cardEl, enabled) {
  const msgEl = cardEl.querySelector('.conn-save-msg');
  const tokenInput = cardEl.querySelector('.conn-token-input');
  if (msgEl) { msgEl.textContent = ''; msgEl.className = 'conn-save-msg'; }

  const config = {};
  if (conn.connection_type === 'telegram' && tokenInput) {
    config.bot_token = (tokenInput.dataset.realValue ?? tokenInput.value).trim();
  }

  try {
    const res = await fetch(
      `/api/v1/agents/${agent.id}/connections/${conn.connection_type}`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: app.currentUserId, enabled, config }),
      }
    );
    const data = await res.json();
    if (res.ok) {
      conn.enabled = enabled;
      conn.config = data.connection?.config || conn.config;
      cardEl.classList.toggle('enabled', enabled);
      // Restore cached value so Chrome autofill can't replace it with bullets
      if (tokenInput && tokenInput.dataset.realValue) {
        tokenInput.value = tokenInput.dataset.realValue;
      }
      if (msgEl) { msgEl.textContent = '✓ Saved'; }
    } else {
      if (msgEl) { msgEl.textContent = data.detail || 'Save failed'; msgEl.className = 'conn-save-msg error'; }
    }
  } catch (e) {
    if (msgEl) { msgEl.textContent = `Error: ${e.message}`; msgEl.className = 'conn-save-msg error'; }
  }
}

// ── Members tab (admin only) ──────────────────────────────────────────────────

async function _renderMembersTab(body, agent) {
  body.innerHTML = '<div class="members-loading">Loading members…</div>';

  let admins = [], members = [], userMode = agent.user_mode || 'anonymous';
  try {
    const res = await fetch(
      `/api/v1/agents/${encodeURIComponent(agent.id)}/members?user_id=${encodeURIComponent(app.currentUserId)}`
    );
    if (!res.ok) {
      const detail = await res.text();
      body.innerHTML = `<div class="members-loading" style="color:#f7768e">Failed to load members: ${_esc(detail || res.statusText)}</div>`;
      return;
    }
    const data = await res.json();
    admins   = data.admins  || [];
    members  = data.members || [];
    userMode = data.user_mode || userMode;
    agent.user_mode = userMode;
  } catch (e) {
    body.innerHTML = `<div class="members-loading" style="color:#f7768e">Failed to load members: ${_esc(e.message)}</div>`;
    return;
  }

  body.innerHTML = '';
  body.appendChild(_buildAccessPolicyControl(agent, userMode, body));

  const notice = document.createElement('div');
  notice.className = 'members-notice';
  notice.textContent = 'Activity counts reflect this agent only.';
  body.appendChild(notice);

  body.appendChild(_buildMembersSection(agent, 'Admins', admins, 'admin', body));
  body.appendChild(_buildMembersSection(agent, 'Members', members, 'member', body));
}

function _buildAccessPolicyControl(agent, currentMode, panelBody) {
  const wrap = document.createElement('div');
  wrap.className = 'members-policy';

  const opts = [
    ['anonymous', 'Anonymous',  'Anyone with the link can chat. No registration needed.'],
    ['register',  'Registered', 'Users must have a registered account to chat.'],
    ['authorized','Authorized', 'Registered users must be authorized by an admin before they can chat.'],
  ];

  const title = document.createElement('div');
  title.className = 'members-policy-title';
  title.textContent = 'Access policy';
  wrap.appendChild(title);

  const choices = document.createElement('div');
  choices.className = 'members-policy-choices';

  for (const [val, label, hint] of opts) {
    const id = `acp-${agent.id}-${val}`;
    const optEl = document.createElement('label');
    optEl.className = 'members-policy-opt' + (currentMode === val ? ' active' : '');
    optEl.htmlFor = id;
    optEl.innerHTML = `
      <input type="radio" id="${_esc(id)}" name="acp-${_esc(agent.id)}" value="${_esc(val)}" ${currentMode === val ? 'checked' : ''}>
      <div class="members-policy-opt-body">
        <div class="members-policy-opt-label">${_esc(label)}</div>
        <div class="members-policy-opt-hint">${_esc(hint)}</div>
      </div>
    `;
    optEl.querySelector('input').addEventListener('change', async (ev) => {
      const newMode = ev.target.value;
      try {
        const res = await fetch(
          `/api/v1/agents/${encodeURIComponent(agent.id)}/user-mode`,
          { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: app.currentUserId, user_mode: newMode }) }
        );
        if (!res.ok) throw new Error(await res.text());
        agent.user_mode = newMode;
        _renderMembersTab(panelBody, agent);
      } catch (e) {
        alert('Failed to update access policy: ' + e.message);
        _renderMembersTab(panelBody, agent);
      }
    });
    choices.appendChild(optEl);
  }
  wrap.appendChild(choices);
  return wrap;
}

function _buildMembersSection(agent, title, rows, kind, panelBody) {
  const sec = document.createElement('div');
  sec.className = 'members-section';

  const header = document.createElement('div');
  header.className = 'members-section-header';
  header.innerHTML = `
    <span class="members-section-title">${_esc(title)}</span>
    <span class="members-section-count">${rows.length}</span>
  `;
  sec.appendChild(header);

  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'members-empty';
    empty.textContent = kind === 'admin'
      ? 'No admins assigned to this agent yet.'
      : 'No members have used this agent yet.';
    sec.appendChild(empty);
    return sec;
  }

  const showActions = kind === 'member';
  const table = document.createElement('table');
  table.className = 'members-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>User</th>
        <th>Channel</th>
        <th class="members-num">Sessions</th>
        <th class="members-num">Messages</th>
        <th>Last login</th>
        ${showActions ? '<th>Status</th><th></th>' : ''}
      </tr>
    </thead>
    <tbody></tbody>
  `;

  const tbody = table.querySelector('tbody');
  for (const r of rows) {
    const tr = document.createElement('tr');
    const name = r.display_name || r.username || r.user_id;
    const subId = r.username && r.username !== name ? r.username : r.user_id;
    const channel = r.channel || (r.username ? 'web' : '—');
    const last = r.last_login_at ? _timeAgo(r.last_login_at) : '—';

    let statusHtml = '';
    let actionHtml = '';
    if (showActions) {
      const isAuth = !!r.is_authorized;
      statusHtml = `<td><span class="members-status ${isAuth ? 'ok' : 'pending'}">${isAuth ? 'Authorized' : 'Pending'}</span></td>`;
      actionHtml = `<td class="members-actions">
        <button class="members-btn ${isAuth ? 'restrict' : 'authorize'}" data-act="${isAuth ? 'restrict' : 'authorize'}" data-uid="${_esc(r.user_id)}">
          ${isAuth ? 'Restrict' : 'Authorize'}
        </button>
      </td>`;
    }

    tr.innerHTML = `
      <td>
        <div class="members-user-name">${_esc(name)}</div>
        <div class="members-user-sub">${_esc(subId)}</div>
      </td>
      <td>${_esc(channel)}</td>
      <td class="members-num">${r.session_count ?? 0}</td>
      <td class="members-num">${r.interaction_count ?? 0}</td>
      <td>${_esc(last)}</td>
      ${statusHtml}${actionHtml}
    `;
    tbody.appendChild(tr);
  }

  if (showActions) {
    tbody.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button.members-btn');
      if (!btn) return;
      const uid = btn.dataset.uid;
      const act = btn.dataset.act;
      btn.disabled = true;
      try {
        const res = await fetch(
          `/api/v1/agents/${encodeURIComponent(agent.id)}/members/${encodeURIComponent(uid)}/${act}`,
          { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: app.currentUserId }) }
        );
        if (!res.ok) throw new Error(await res.text());
        _renderMembersTab(panelBody, agent);
      } catch (e) {
        alert('Action failed: ' + e.message);
        btn.disabled = false;
      }
    });
  }

  sec.appendChild(table);
  return sec;
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

  body.appendChild(area);

  // Render static blueprint after area is in DOM so clientWidth is measurable
  _drawAgentLoopDiagram(loopEl, new Map(), agent);
}

async function _runTest(agent, areaEl) {
  const input  = areaEl.querySelector('.agents-test-input');
  const status = areaEl.querySelector('.agents-test-status');
  const loopEl = areaEl.querySelector('.agents-test-loop');
  if (!input || !status || !loopEl) return;

  const msg = input.value.trim();
  if (!msg) return;

  status.innerHTML = `${icon('loader-2', { size: '13px' })} Running…`;
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

// ── Agent loop diagram — uses shared loop-diagram.js for topology + rendering ──

// One active node-info panel at a time (shared across all loop diagrams)
let _lvActivePanelEl     = null;
let _lvActivePanelNodeId = null;

function _lvHidePanel(force = false) {
  if (!force && Object.keys(_lvPendingChanges).length > 0) {
    if (!confirm('You have unsaved changes — discard them?')) return;
  }
  if (_lvActivePanelEl) { _lvActivePanelEl.remove(); _lvActivePanelEl = null; }
  _lvActivePanelNodeId = null;
  _lvPendingChanges    = {};
  _lvSaveBtnEl         = null;
  document.removeEventListener('click', _lvOutsideClickHandler);
}

function _triggerExclusions(agent) {
  const tt = agent?.trigger_type || 'user_input';
  if (tt === 'user_input') {
    return {
      excludeNodes: ['slash_cmd'],
      extraEdges:   [{ from: 'user_input', to: 'session_setup' }],
      nodeLabelMap: {},
    };
  }
  if (tt === 'slash_command') {
    return {
      excludeNodes: [],
      extraEdges:   [],
      nodeLabelMap: { slash_cmd: 'Slash Trigger' },
    };
  }
  return { excludeNodes: [], extraEdges: [], nodeLabelMap: {} };
}

// nodeStates = Map<nodeId, 'active'|'done'|'error'>  — new Map() = static blueprint
function _drawAgentLoopDiagram(loopEl, nodeStates, agent) {
  loopEl._lvRo?.disconnect();

  const savedScroll = loopEl.scrollTop;

  loopEl.innerHTML = '';
  _lvHidePanel();

  // Store for resize re-render
  loopEl._lvNodeStates = nodeStates;
  loopEl._lvAgent      = agent;

  const scaleWrap = document.createElement('div');
  scaleWrap.style.cssText = 'width:100%;overflow:hidden;';
  loopEl.appendChild(scaleWrap);

  // Measure scaleWrap (not loopEl) — excludes loopEl padding and accounts for scrollbar
  const availableWidth = Math.max(300, scaleWrap.clientWidth || scaleWrap.offsetWidth || LOOP_W);

  const { excludeNodes, extraEdges, nodeLabelMap } = _triggerExclusions(agent);

  // Build nodeFilter (legacy flat-string arrays only) and nodeConfig (new object format).
  // Legacy: loop_logic is an array of node-ID strings → pass as nodeFilter to show only
  //   those nodes (preserves existing behaviour for agents that stored a flat list).
  // New: loop_logic is an array of {node, enabled} objects → pass as nodeConfig so
  //   disabled nodes get the lv-disabled class while all nodes remain visible.
  const _ll     = Array.isArray(agent?.loop_logic) ? agent.loop_logic : [];
  const _llFlat = _ll.length > 0 && typeof _ll[0] === 'string';
  const _nodeFilter = (agent && _llFlat && !_ll[0].startsWith('opt_')) ? _ll : null;
  const _nodeConfig = (!_llFlat && _ll.length > 0)
    ? new Map(_ll.map(item => [item.node, { enabled: item.enabled !== false }]))
    : null;

  const { rootEl } = renderLoopDiagram(scaleWrap, nodeStates, {
    availableWidth,
    markerPrefix: 'ag',
    nodeFilter:   _nodeFilter,
    nodeConfig:   _nodeConfig,
    excludeNodes,
    extraEdges,
    nodeLabelMap,
    getNodeDetail: nd => _lvNodeHint(nd, agent),
    onNodeClick: (nd, el, root) => {
      if (_lvActivePanelNodeId === nd.id) { _lvHidePanel(true); return; }
      _lvShowPanel(nd, el, root, agent);
    },
    decorateNode: (nd, el) => {
      if (agent?.source === 'custom' &&
          nd.id !== 'user_input' && nd.id !== 'final_response' && nd.id !== 'validate_tools') {
        el.classList.add('lv-node-editable');
      }
      // Toggleable nodes get a subtle visual hint so the user knows they can be gated
      if (TOGGLEABLE_NODES.has(nd.id)) {
        el.classList.add('lv-node-toggleable');
      }
    },
  });

  rootEl.addEventListener('click', _lvOutsideClickHandler);

  if (savedScroll > 0) {
    requestAnimationFrame(() => { loopEl.scrollTop = savedScroll; });
  }

  // Re-render on resize to reflow layout (debounced).
  // Observe parentElement, not loopEl itself — loopEl's content changes on re-render
  // which would retrigger the observer every 120ms, resetting scroll and killing panels.
  loopEl._lvRo = new ResizeObserver((entries) => {
    const w = entries[0]?.contentRect?.width ?? 0;
    if (w && Math.abs(w - (loopEl._lvLastRoWidth || 0)) < 2) return;
    loopEl._lvLastRoWidth = w;
    clearTimeout(loopEl._lvResizeTimer);
    loopEl._lvResizeTimer = setTimeout(() => {
      // Skip re-render while a panel is open — panel insertion can change the
      // container height (and thus trigger scrollbar-related width changes),
      // which would incorrectly fire this observer and close the panel.
      if (_lvActivePanelEl) return;
      _drawAgentLoopDiagram(loopEl, loopEl._lvNodeStates, loopEl._lvAgent);
    }, 120);
  });
  if (loopEl.parentElement) loopEl._lvRo.observe(loopEl.parentElement);
  else loopEl._lvRo.observe(loopEl);
}

function _lvNodeHint(nd, agent) {
  if (!agent) return '';
  const isCustom = agent.source === 'custom';
  switch (nd.id) {
    case 'user_input':
      return 'User message enters the pipeline here';

    case 'slash_cmd':
      return 'Intercept check — /optimize routes directly to the optimizer, bypassing the agent loop';

    case 'session_setup':
      return isCustom
        ? `Agent: ${agent.name || agent.id} — session init, agent resolve, participants`
        : 'Ensures session exists, resolves agent, registers participants';

    case 'save_user_msg':
      return 'Persists the user message as role="user" before the loop starts';

    case 'load_context': {
      const pf = ['agent_prompt','user_prompt','skills_prompt','tasks_prompt','misc_prompt'];
      const n = pf.filter(f => agent[f] && String(agent[f]).trim()).length;
      return isCustom
        ? `${n}/5 prompt sections configured — click to edit`
        : `${n} of 5 prompt sections configured`;
    }

    case 'memory_search': {
      const dis = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
      const on = !dis.has('memory');
      return isCustom
        ? `Memory search ${on ? 'enabled' : 'disabled'} — click to toggle`
        : 'Semantic search over past sessions; trivial messages skip this step automatically';
    }

    case 'resolve_attach':
      return 'Resolves uploaded file IDs into metadata for the system prompt';

    case 'build_prompt':
      return isCustom
        ? 'Click to edit prompt sections'
        : 'Assembles system prompt from all context sections and memory results';

    case 'build_history':
      return 'Loads session history → OpenAI format; strips internal tools (memory_search/save)';

    case 'load_provider':
      return isCustom
        ? `${agent.model || 'claude-3-5-sonnet'} — click to configure provider`
        : `LLM config: ${agent.model || 'claude-3-5-sonnet'}`;

    case 'load_tools': {
      const count = _toolsForAgent(agent).length;
      return isCustom
        ? `${count} tools — click to manage`
        : `${count} tools loaded for this agent`;
    }

    case 'assemble_msgs':
      return 'Builds messages array: [system, ...history, {role:"user"}]';

    case 'interrupt_chk':
      return 'Checks for cancellation signal — raises AgentInterrupted if set';

    case 'turn_counter':
      return isCustom
        ? `Max turns: ${agent.max_turn_count || 10} — click to edit turn limit & permission gate`
        : `Turn counter — exits at max_turns (${agent.max_turn_count || 10})`;

    case 'build_tool_defs':
      return 'Converts tool metadata into the OpenAI tool_calls schema for the LLM';

    case 'parallel_mode':
      return 'PARALLEL_MODE: races multiple LLM providers — first chunk wins';

    case 'llm_call':
      return isCustom
        ? `${agent.model || 'claude-3-5-sonnet'} — click to configure`
        : `Model: ${agent.model || 'claude-3-5-sonnet'}`;

    case 'db_persist_asst':
      return 'Saves assistant message to DB before validation — with [Tool calls: …] suffix';

    case 'validate_tools':
      return 'Validates each tool call — name exists, args parseable, required params present';

    case 'destructive_chk':
      return 'Checks DESTRUCTIVE_TOOLS set: edit_source, write_source, delete_source, run_command, restart_server';

    case 'guardrails':
      return isCustom
        ? 'Click to configure tool guardrails'
        : 'Destructive tools require explicit user confirmation before executing';

    case 'post_val_chk':
      return 'Interrupt check after validation loop — catches cancellations before any tool runs';

    case 'execute_tools': {
      const count = _toolsForAgent(agent).length;
      return isCustom
        ? `${count} tools — click to manage`
        : `${count} tools available`;
    }

    case 'db_persist_tool':
      return 'Saves tool result as role="tool" with execution metadata (duration, success, error)';

    case 'delegation_chk':
      return 'If result contains __delegate__, switches active agent mid-loop and rebinds session';

    case 'skill_track':
      return 'Records tool execution event and updates skill performance score in DB';

    case 'check_continue':
      return isCustom
        ? `Max turns: ${agent.max_turn_count || 10} — click to edit`
        : `Loops back if tool results exist; stops at max_turns (${agent.max_turn_count || 10})`;

    case 'final_response':
      return 'Final reply streamed to the user over WebSocket';

    case 'db_persist_final':
      return 'Saves final assistant response to interactions table';

    case 'memory_save': {
      const dis = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
      const on = !dis.has('memory_save');
      return isCustom
        ? `Memory save ${on ? 'enabled' : 'disabled'} — click to toggle`
        : 'Key facts from session saved to long-term memory store';
    }

    case 'fire_optimizer':
      return isCustom
        ? 'Optimizer fires on every exit path — click to configure'
        : 'fire-and-forget: analyzes session and proposes skill improvements';

    default:
      return '';
  }
}

// ── Interactive loop diagram panel system ─────────────────────────────────────────

let _lvPendingChanges = {};
let _lvSaveBtnEl     = null;

// ── Loop-logic helpers ─────────────────────────────────────────────────────
// Returns the current loop_logic as an object-array, merging in any pending
// changes.  Handles both the legacy flat-string format (treated as all-enabled)
// and the new {node, enabled} object format.
function _resolveLoopLogicObjects(agent) {
  const pending = Array.isArray(_lvPendingChanges.loop_logic) ? _lvPendingChanges.loop_logic : null;
  const source  = pending || (Array.isArray(agent.loop_logic) ? agent.loop_logic : []);
  if (source.length === 0 || typeof source[0] === 'string') {
    // Legacy flat format or empty → seed from full node list, all enabled
    return LOOP_NODES.map(n => ({ node: n.id, enabled: true }));
  }
  return source.map(item => ({ ...item }));
}

function _isNodeLoopEnabled(agent, nodeId) {
  const objs  = _resolveLoopLogicObjects(agent);
  const found = objs.find(o => o.node === nodeId);
  return found ? found.enabled !== false : true;  // unknown nodes default to enabled
}

function _setNodeLoopEnabled(agent, nodeId, enabled) {
  const objs = _resolveLoopLogicObjects(agent);
  const idx  = objs.findIndex(o => o.node === nodeId);
  if (idx !== -1) {
    objs[idx] = { ...objs[idx], enabled };
  } else {
    objs.push({ node: nodeId, enabled });
  }
  _lvSetPending('loop_logic', objs);
}

function _lvSetPending(key, value) {
  _lvPendingChanges[key] = value;
  _lvMarkDirty();
}

function _lvMarkDirty() {
  if (!_lvSaveBtnEl) return;
  const n = Object.keys(_lvPendingChanges).length;
  _lvSaveBtnEl.classList.toggle('dirty', n > 0);
  _lvSaveBtnEl.textContent = n > 0 ? `Save (${n} field${n > 1 ? 's' : ''})` : 'Save changes';
}

function _lvOutsideClickHandler() {
  if (Object.keys(_lvPendingChanges).length > 0) {
    if (!confirm('You have unsaved changes — discard them?')) return;
  }
  _lvHidePanel(true);
}

function _lvShowPanel(nd, nodeEl, container, agent) {
  if (agent && agent.source === 'custom') {
    _lvShowEditPanel(nd, nodeEl, container, agent);
  } else {
    _lvShowReadOnlyPanel(nd, nodeEl, container, agent);
  }
}

function _lvShowReadOnlyPanel(nd, nodeEl, container, agent) {
  _lvHidePanel();

  const panel = document.createElement('div');
  panel.className = 'lv-tool-panel';

  const header = document.createElement('div');
  header.className = 'lv-tool-panel-header';
  const title = document.createElement('span');
  title.className = 'lv-tool-panel-title';
  title.textContent = nd.label;
  const close = document.createElement('button');
  close.className = 'lv-tool-panel-close';
  close.innerHTML = icon('x', { size: '14px' });
  close.addEventListener('click', e => { e.stopPropagation(); _lvHidePanel(); });
  header.appendChild(title);
  header.appendChild(close);
  panel.appendChild(header);

  // Always render the NODE_PANEL_INFO description + details first
  _lvRenderNodeInfo(panel, nd);

  // Then render agent-specific extras per node
  switch (nd.id) {

    case 'session_setup': {
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'This agent';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      _lvAppendItem(list, { name: 'Name',     type: 'tool', desc: agent.name || agent.id || '—' });
      _lvAppendItem(list, { name: 'Model',    type: 'tool', desc: agent.model || 'claude-3-5-sonnet-20241022' });
      _lvAppendItem(list, { name: 'Max turns',type: 'tool', desc: String(agent.max_turn_count || 10) });
      panel.appendChild(list);
      break;
    }

    case 'load_context':
    case 'build_prompt': {
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'Prompt sections';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      [
        { key: 'system_prompt',  label: 'Directive',    hint: 'Core agent directive' },
        { key: 'agent_prompt',   label: 'Identity',     hint: 'Agent personality'   },
        { key: 'user_prompt',    label: 'User prefs',   hint: 'User preferences'    },
        { key: 'skills_prompt',  label: 'Skills',       hint: 'Skills & tools'      },
        { key: 'tasks_prompt',   label: 'Tasks',        hint: 'Task workflows'      },
        { key: 'misc_prompt',    label: 'Misc',         hint: 'Miscellaneous'       },
      ].forEach(f => {
        const filled = agent[f.key] && String(agent[f.key]).trim();
        _lvAppendItem(list, {
          name: f.label,
          type: filled ? 'tool' : 'command',
          desc: filled ? String(agent[f.key]).trim().substring(0, 80) + '…' : '(empty)',
        });
      });
      panel.appendChild(list);
      break;
    }

    case 'memory_search': {
      const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
      const on = !disabled.has('memory');
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'Status';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      _lvAppendItem(list, {
        name: 'Memory search',
        type: on ? 'tool' : 'command',
        desc: on ? 'Enabled — past sessions are searched before each response' : 'Disabled — brain context is skipped',
      });
      panel.appendChild(list);
      break;
    }

    case 'build_history': {
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'Steps';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      [
        'Fetch all interactions for the current session',
        'Convert rows to OpenAI message format (role + content)',
        'Reconstruct tool_calls from assistant messages',
        'Filter out internal tools (memory_search, memory_save)',
      ].forEach(s => {
        const item = document.createElement('div');
        item.className = 'lv-tool-item';
        const el = document.createElement('div');
        el.className = 'lv-tool-desc';
        el.textContent = s;
        item.appendChild(el);
        list.appendChild(item);
      });
      panel.appendChild(list);
      break;
    }

    case 'load_provider':
    case 'llm_call': {
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'Provider config';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      _lvAppendItem(list, { name: 'Model',       type: 'tool', desc: agent.model || 'claude-3-5-sonnet-20241022' });
      _lvAppendItem(list, { name: 'Temperature', type: 'tool', desc: String(agent.temperature ?? 1.0) });
      _lvAppendItem(list, { name: 'Max tokens',  type: 'tool', desc: String(agent.max_tokens ?? 8096) });
      panel.appendChild(list);
      break;
    }

    case 'load_tools':
    case 'execute_tools': {
      const count = _toolsForAgent(agent).length;
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label lv-tool-section-live';
      lbl.innerHTML = `Tools (${count}) <span class="lv-live-dot"></span>`;
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      const loadingEl = document.createElement('div');
      loadingEl.className = 'lv-tool-panel-empty lv-tool-loading';
      loadingEl.textContent = 'Loading…';
      list.appendChild(loadingEl);
      panel.appendChild(list);
      const agentToolNames = new Set(_toolsForAgent(agent));
      fetchAllToolMeta().then(allTools => {
        const nodeTools = allTools.filter(t => agentToolNames.has(t.name));
        list.innerHTML = '';
        if (!nodeTools.length) {
          const none = document.createElement('div');
          none.className = 'lv-tool-panel-empty';
          none.textContent = 'No tools for this agent.';
          list.appendChild(none);
          return;
        }
        nodeTools.sort((a, b) => {
          const aS = a.source === 'skill' ? 0 : 1;
          const bS = b.source === 'skill' ? 0 : 1;
          return aS - bS || a.name.localeCompare(b.name);
        });
        nodeTools.forEach(t => {
          _lvAppendItem(list, {
            name: t.name,
            type: t.destructive ? 'guarded' : t.source === 'skill' ? 'skill' : 'tool',
            desc: t.description || '',
          });
        });
      });
      break;
    }

    case 'assemble_msgs': {
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'messages[ ] payload';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      [
        { index: '[0]',    role: 'system',    detail: 'system_prompt — directive + context + memory' },
        { index: '[1..N]', role: 'assistant', detail: 'transcript — prior turns this session'       },
        { index: '[N+1]',  role: 'user',      detail: "current message — this turn's input"         },
      ].forEach(m => {
        _lvAppendItem(list, { name: `${m.index} ${m.role}`, type: 'tool', desc: m.detail });
      });
      panel.appendChild(list);
      break;
    }

    case 'turn_counter':
    case 'permission_chk':
    case 'check_continue': {
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'Configuration';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      _lvAppendItem(list, {
        name: `Max turns: ${agent.max_turn_count || 10}`,
        type: 'tool',
        desc: 'Agent stops looping after this many tool-calling turns',
      });
      panel.appendChild(list);
      break;
    }

    case 'destructive_chk': {
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'Guarded tools';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      ['edit_source', 'write_source', 'delete_source', 'run_command', 'restart_server'].forEach(name => {
        _lvAppendItem(list, { name, type: 'guarded', desc: 'Requires user confirmation before executing' });
      });
      panel.appendChild(list);
      break;
    }

    case 'guardrails': {
      const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'Tool categories';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      TIER_2_CATEGORIES.forEach(cat => {
        const allBlocked = cat.tools.every(t => disabled.has(t.name));
        _lvAppendItem(list, {
          name: cat.label,
          type: allBlocked ? 'command' : 'tool',
          desc: allBlocked ? 'Blocked' : 'Allowed',
        });
      });
      panel.appendChild(list);
      break;
    }

    case 'memory_save': {
      const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
      const on = !disabled.has('memory_save');
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'Status';
      panel.appendChild(lbl);
      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      _lvAppendItem(list, {
        name: 'Long-term memory save',
        type: on ? 'tool' : 'command',
        desc: on ? 'Enabled — key facts are saved after each session' : 'Disabled — facts are not persisted',
      });
      panel.appendChild(list);
      break;
    }

    default:
      // NODE_PANEL_INFO already rendered above — nothing extra needed
      break;
  }

  panel.addEventListener('click', e => e.stopPropagation());

  const _outerLvA = container.closest('.agents-test-area') || container;
  _outerLvA.insertBefore(panel, _outerLvA.querySelector('.agents-test-loop'));
  _lvActivePanelNodeId = nd.id;
  _lvActivePanelEl     = panel;
}

function _lvShowEditPanel(nd, nodeEl, container, agent) {
  _lvHidePanel(true); // force-close without confirm (same agent, different node)
  // _lvPendingChanges intentionally NOT reset — changes accumulate across nodes

  const panel = document.createElement('div');
  panel.className = 'lv-edit-panel';
  panel.addEventListener('click', e => e.stopPropagation());

  const header = document.createElement('div');
  header.className = 'lv-tool-panel-header';
  const hLeft = document.createElement('div');
  hLeft.style.cssText = 'display:flex;align-items:center;gap:7px;';
  const title = document.createElement('span');
  title.className = 'lv-tool-panel-title';
  title.textContent = nd.label;
  const badge = document.createElement('span');
  badge.className = 'lv-edit-badge';
  badge.textContent = 'editable';
  hLeft.appendChild(title);
  hLeft.appendChild(badge);
  const close = document.createElement('button');
  close.className = 'lv-tool-panel-close';
  close.innerHTML = icon('x', { size: '14px' });
  close.addEventListener('click', e => { e.stopPropagation(); _lvHidePanel(); });
  header.appendChild(hLeft);
  header.appendChild(close);
  panel.appendChild(header);

  const body = document.createElement('div');
  body.className = 'lv-edit-body';
  panel.appendChild(body);

  switch (nd.id) {
    case 'load_context':   _lvRenderLoadContextInfo(body, agent);    break;
    case 'build_prompt':   _lvRenderBuildPromptInfo(body);            break;
    case 'build_history':      _lvRenderTranscriptInfo(body);                  break;
    case 'load_tools':         _lvRenderLoadToolsInfo(body);                   break;
    case 'assemble_msgs':      _lvRenderAssembleInfo(body);                    break;
    case 'session_setup':  _lvRenderSessionSetupInfo(body, agent);   break;
    case 'memory_search':  _lvRenderMemorySearchEditor(body, agent); break;
    case 'llm_call':       _lvRenderLlmEditor(body, agent);          break;
    case 'execute_tools':  _lvRenderToolsEditor(body, agent);        break;
    case 'guardrails':     _lvRenderGuardrailsEditor(body, agent);   break;
    case 'turn_counter':
    case 'permission_chk':
    case 'check_continue': _lvRenderContinueEditor(body, agent);     break;
    case 'memory_save':    _lvRenderMemorySaveEditor(body, agent);   break;
    // ── Loop-gated nodes (loop_logic enable/disable) ──
    case 'interrupt_chk':
      _lvRenderGatedNodeEditor(body, agent, 'interrupt_chk', 'Interrupt check',
        'Polls for a cancellation signal at the start of each turn and after every tool result. When the user clicks Stop, this node detects the flag and halts the loop immediately, returning whatever the agent has produced so far. Disable for batch or headless agents that must run to completion without interruption.',
        {
          dbEffect: 'Reads only — checks the in-memory interrupt registry and interrupts table. No writes. Overhead is negligible (one flag read per turn).',
          effectiveWhen: 'Next message sent',
          effectiveClass: 'immediate'
        });
      break;
    case 'delegation_chk':
      _lvRenderGatedNodeEditor(body, agent, 'delegation_chk', 'Agent delegation',
        'After each tool result, checks whether the response contains a __delegate__ sentinel. If detected, the loop reinitialises with the target agent\'s config — swapping system prompt, tools, model, and loop settings — mid-session without starting a new session. Disable to keep this agent isolated and prevent it from handing off to other agents.',
        {
          dbEffect: 'On delegation: updates sessions.agent_id and logs an agent_delegation pipeline event in interactions. No writes on turns where delegation is not triggered.',
          effectiveWhen: 'Next message sent',
          effectiveClass: 'immediate'
        });
      break;
    case 'skill_track':
      _lvRenderGatedNodeEditor(body, agent, 'skill_track', 'Skill tracking',
        'After each successful tool call, records the usage event in the skills table. Increments the use count and updates the last-used timestamp for that tool under the current user. This data drives tool performance scoring and the Skills tab analytics. Disable to reduce DB write load on high-throughput or lightweight agents.',
        {
          dbEffect: 'Writes to skills table: increments use_count, sets last_used_at. One row write per tool call per turn. Disable eliminates these writes entirely.',
          effectiveWhen: 'Next message sent',
          effectiveClass: 'immediate'
        });
      break;
    case 'fire_optimizer':
      _lvRenderGatedNodeEditor(body, agent, 'fire_optimizer', 'Fire optimizer',
        'After the agent produces a final response (or hits max turns / interruption), triggers the optimizer pipeline in the background. The optimizer analyses the session for improvement opportunities. Disable for agents that should never trigger optimization (ephemeral, test, or pipeline-internal agents).',
        {
          dbEffect: 'Creates an optimizer session and inserts interactions into a temp DB. No writes to the main session.',
          effectiveWhen: 'Next message sent',
          effectiveClass: 'immediate'
        });
      break;
    case 'copy_defaults':
      _lvRenderGatedNodeEditor(body, agent, 'copy_defaults', 'Copy default context',
        'On first use, copies the default agent\'s context documents into this agent\'s context. Disable if you want this agent to start with a completely blank context (no inherited personality, skills, or task workflows from the default template).',
        {
          dbEffect: 'Inserts context_documents rows (one-time copy). Once copied, toggling this off has no effect — the docs already exist. To reset, delete the agent\'s context docs manually.',
          effectiveWhen: 'Next message sent',
          effectiveClass: 'immediate'
        });
      break;
    default: {
      _lvRenderNodeInfo(body, nd);
      if (!NODE_PANEL_INFO[nd.id]) {
        const info = document.createElement('div');
        info.className = 'lv-edit-desc';
        info.textContent = _lvNodeHint(nd, agent) || 'No editable settings for this node.';
        body.appendChild(info);
      }
    }
  }

  const _INFO_NODES = new Set(['load_context', 'build_prompt', 'build_history', 'load_tools', 'data_src_load', 'integration_status', 'assemble_msgs', 'data_src_exec']);
  if (!_INFO_NODES.has(nd.id)) {
  const saveBar = document.createElement('div');
  saveBar.className = 'lv-edit-save-bar';
  const saveMsg = document.createElement('span');
  saveMsg.className = 'lv-edit-save-msg';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'lv-edit-cancel-btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', e => { e.stopPropagation(); _lvHidePanel(true); });

  const saveBtn = document.createElement('button');
  saveBtn.className = 'lv-edit-save-btn';
  saveBtn.textContent = 'Save changes';
  _lvSaveBtnEl = saveBtn;
  _lvMarkDirty();

  saveBtn.addEventListener('click', async e => {
    e.stopPropagation();
    if (!Object.keys(_lvPendingChanges).length) {
      saveMsg.textContent = 'No changes';
      saveMsg.className = 'lv-edit-save-msg';
      return;
    }
    saveBtn.disabled = true;
    cancelBtn.disabled = true;
    saveMsg.textContent = 'Saving…';
    saveMsg.className = 'lv-edit-save-msg';
    try {
      const res = await fetch(`/api/v1/agents/${agent.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: app.currentUserId, ..._lvPendingChanges }),
      });
      const data = await res.json();
      if (res.ok) {
        const idx = _agents.findIndex(a => a.id === agent.id);
        if (idx !== -1) Object.assign(_agents[idx], data.agent);
        Object.assign(agent, data.agent);
        saveMsg.textContent = '✓ Saved';
        saveMsg.className = 'lv-edit-save-msg ok';
        _lvPendingChanges = {};
        _lvSaveBtnEl = null;
        const loopEl = panel.closest('.agents-test-area')?.querySelector('.agents-test-loop');
        if (loopEl) _drawAgentLoopDiagram(loopEl, new Map(), agent);
      } else {
        saveMsg.textContent = data.detail || 'Save failed';
        saveMsg.className = 'lv-edit-save-msg error';
      }
    } catch (err) {
      saveMsg.textContent = `Error: ${err.message}`;
      saveMsg.className = 'lv-edit-save-msg error';
    } finally {
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  });

  saveBar.appendChild(saveMsg);
  saveBar.appendChild(cancelBtn);
  saveBar.appendChild(saveBtn);
  panel.appendChild(saveBar);
  } // end !_INFO_NODES

  const _outerLvB = container.closest('.agents-test-area') || container;
  _outerLvB.insertBefore(panel, _outerLvB.querySelector('.agents-test-loop'));
  _lvActivePanelNodeId = nd.id;
  _lvActivePanelEl     = panel;
}

// ── Node-specific renderers ─────────────────────────────────────────────

function _lvRenderPromptEditor(body, agent) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Shape what context the agent loads before each LLM call.';
  body.appendChild(desc);

  const SECTIONS = [
    { key: 'agent_prompt',  label: 'Identity & Personality', hint: 'Who the agent is and how it behaves' },
    { key: 'user_prompt',   label: 'User Preferences',       hint: 'Personalisation for this user'       },
    { key: 'skills_prompt', label: 'Skills & Tools',         hint: 'What tools the agent uses and when'  },
    { key: 'tasks_prompt',  label: 'Task Workflows',         hint: 'Recurring workflows or checklists'   },
    { key: 'misc_prompt',   label: 'Miscellaneous',          hint: 'Anything else to inject'             },
  ];

  SECTIONS.forEach(s => {
    const row = document.createElement('div');
    row.className = 'lv-edit-prompt-row';
    const labelrow = document.createElement('div');
    labelrow.className = 'lv-edit-prompt-labelrow';
    const val = agent[s.key] || '';
    const dot = document.createElement('span');
    dot.className = 'lv-edit-prompt-dot' + (val.trim() ? ' filled' : '');
    const lbl = document.createElement('span');
    lbl.className = 'lv-edit-prompt-label';
    lbl.textContent = s.label;
    const hint = document.createElement('span');
    hint.className = 'lv-edit-prompt-hint';
    hint.textContent = s.hint;
    labelrow.appendChild(dot);
    labelrow.appendChild(lbl);
    labelrow.appendChild(hint);
    const ta = document.createElement('textarea');
    ta.className = 'lv-edit-textarea';
    ta.rows = 3;
    ta.value = val;
    ta.placeholder = '(empty)';
    ta.addEventListener('input', () => {
      dot.className = 'lv-edit-prompt-dot' + (ta.value.trim() ? ' filled' : '');
      _lvSetPending(s.key, ta.value);
    });
    row.appendChild(labelrow);
    row.appendChild(ta);
    body.appendChild(row);
  });
}

function _lvRenderNodeInfo(panel, nd) {
  const info = NODE_PANEL_INFO[nd.id];
  if (!info) return;

  const descEl = document.createElement('div');
  descEl.className = 'lv-edit-desc';
  descEl.textContent = info.desc;
  panel.appendChild(descEl);

  if (info.details && info.details.length) {
    const lbl = document.createElement('div');
    lbl.className = 'lv-tool-section-label';
    lbl.textContent = 'Details';
    panel.appendChild(lbl);

    const list = document.createElement('div');
    list.className = 'lv-tool-panel-list';
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
      list.appendChild(item);
    });
    panel.appendChild(list);
  }
}

function _lvRenderLoadContextInfo(body, agent) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Loads context documents into the system prompt from the agents table. Also seeds default context from templates on first use — if the user has no context rows yet, defaults are copied in before loading proceeds.';
  body.appendChild(desc);

  const lbl = document.createElement('div');
  lbl.className = 'lv-tool-section-label';
  lbl.textContent = 'Prompt columns (agents table)';
  body.appendChild(lbl);

  const COLS = [
    { key: 'system_prompt',  label: 'system_prompt',  hint: 'Agent directive'   },
    { key: 'agent_prompt',   label: 'agent_prompt',   hint: 'Agent identity'    },
    { key: 'user_prompt',    label: 'user_prompt',    hint: 'User preferences'  },
    { key: 'skills_prompt',  label: 'skills_prompt',  hint: 'Skills & tools'    },
    { key: 'tasks_prompt',   label: 'tasks_prompt',   hint: 'Task workflows'    },
    { key: 'misc_prompt',    label: 'misc_prompt',    hint: 'Miscellaneous'     },
  ];

  const list = document.createElement('div');
  list.className = 'lv-tool-panel-list';
  COLS.forEach(c => {
    const item = document.createElement('div');
    item.className = 'lv-tool-item';
    const nameRow = document.createElement('div');
    nameRow.className = 'lv-tool-name-row';
    const dot = document.createElement('span');
    dot.className = 'lv-edit-prompt-dot' + ((agent[c.key] || '').trim() ? ' filled' : '');
    const name = document.createElement('span');
    name.className = 'lv-tool-name';
    name.textContent = c.label;
    nameRow.appendChild(dot);
    nameRow.appendChild(name);
    const hint = document.createElement('div');
    hint.className = 'lv-tool-desc';
    hint.textContent = c.hint;
    item.appendChild(nameRow);
    item.appendChild(hint);
    list.appendChild(item);
  });
  body.appendChild(list);
}

function _lvRenderBuildPromptInfo(body) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Builds the system_prompt string from loaded context and memory results.';
  body.appendChild(desc);

  const lbl = document.createElement('div');
  lbl.className = 'lv-tool-section-label';
  lbl.textContent = 'Inputs';
  body.appendChild(lbl);

  const MSGS = [
    { index: '①', role: 'agent_prompt',   detail: 'Agent identity and directive'          },
    { index: '②', role: 'brain_context',  detail: 'Memory search results (if any)'        },
    { index: '③', role: 'context_docs',   detail: 'user_prompt, skills, tasks, misc'      },
    { index: '④', role: 'attachments',    detail: 'File/image context (if attached)'      },
  ];

  const list = document.createElement('div');
  list.className = 'lv-tool-panel-list';
  MSGS.forEach(m => {
    const item = document.createElement('div');
    item.className = 'lv-tool-item';
    const nameRow = document.createElement('div');
    nameRow.className = 'lv-tool-name-row';
    const badge = document.createElement('span');
    badge.className = 'lv-tool-badge lv-badge-tool';
    badge.textContent = m.index;
    const name = document.createElement('span');
    name.className = 'lv-tool-name';
    name.textContent = m.role;
    nameRow.appendChild(badge);
    nameRow.appendChild(name);
    const detail = document.createElement('div');
    detail.className = 'lv-tool-desc';
    detail.textContent = m.detail;
    item.appendChild(nameRow);
    item.appendChild(detail);
    list.appendChild(item);
  });
  body.appendChild(list);
}

function _lvRenderTranscriptInfo(body) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Loads the conversation history for this session from the database.';
  body.appendChild(desc);

  const lbl = document.createElement('div');
  lbl.className = 'lv-tool-section-label';
  lbl.textContent = 'Steps';
  body.appendChild(lbl);

  const STEPS = [
    'Fetch all interactions for the current session',
    'Convert rows to OpenAI message format (role + content)',
    'Reconstruct tool_calls from assistant messages',
    'Filter out internal steps (memory_search, memory_save)',
  ];

  const list = document.createElement('div');
  list.className = 'lv-tool-panel-list';
  STEPS.forEach(s => {
    const item = document.createElement('div');
    item.className = 'lv-tool-item';
    const name = document.createElement('div');
    name.className = 'lv-tool-desc';
    name.textContent = s;
    item.appendChild(name);
    list.appendChild(item);
  });
  body.appendChild(list);
}

function _lvRenderLoadToolsInfo(body) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Loads tool definitions available to this agent for the current turn.';
  body.appendChild(desc);

  const lbl = document.createElement('div');
  lbl.className = 'lv-tool-section-label';
  lbl.textContent = 'Sources';
  body.appendChild(lbl);

  const SOURCES = [
    { label: 'Built-in tools',   detail: 'Core tools always available (db_query, web_search, memory…)' },
    { label: 'Tier-2 tools',     detail: 'Optional tools enabled per-agent (browser, run_command…)'    },
    { label: 'Skill tools',      detail: 'User-created tools registered via create_tool'               },
    { label: 'Admin tools',      detail: 'Admin-only tools gated by agent template_id'                 },
  ];

  const list = document.createElement('div');
  list.className = 'lv-tool-panel-list';
  SOURCES.forEach(s => {
    const item = document.createElement('div');
    item.className = 'lv-tool-item';
    const nameRow = document.createElement('div');
    nameRow.className = 'lv-tool-name-row';
    const badge = document.createElement('span');
    badge.className = 'lv-tool-badge lv-badge-tool';
    badge.textContent = 'src';
    const name = document.createElement('span');
    name.className = 'lv-tool-name';
    name.textContent = s.label;
    nameRow.appendChild(badge);
    nameRow.appendChild(name);
    const detail = document.createElement('div');
    detail.className = 'lv-tool-desc';
    detail.textContent = s.detail;
    item.appendChild(nameRow);
    item.appendChild(detail);
    list.appendChild(item);
  });
  body.appendChild(list);
}

function _lvRenderAssembleInfo(body) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Combines system prompt, conversation history, and current message into the final LLM payload.';
  body.appendChild(desc);

  const lbl = document.createElement('div');
  lbl.className = 'lv-tool-section-label';
  lbl.textContent = 'messages[ ] payload';
  body.appendChild(lbl);

  const SLOTS = [
    { index: '[0]',    role: 'system',    detail: '{ system_prompt }  —  agent directive + context sections + memory' },
    { index: '[1..N]', role: 'assistant', detail: '{ transcript }  —  prior turns from this session'                 },
    { index: '[N+1]',  role: 'user',      detail: '{ current message }  —  this turn\'s input'                       },
  ];

  const list = document.createElement('div');
  list.className = 'lv-tool-panel-list';
  SLOTS.forEach(m => {
    const item = document.createElement('div');
    item.className = 'lv-tool-item';
    const nameRow = document.createElement('div');
    nameRow.className = 'lv-tool-name-row';
    const badge = document.createElement('span');
    badge.className = 'lv-tool-badge lv-badge-tool';
    badge.textContent = m.index;
    const name = document.createElement('span');
    name.className = 'lv-tool-name';
    name.textContent = m.role;
    nameRow.appendChild(badge);
    nameRow.appendChild(name);
    const detail = document.createElement('div');
    detail.className = 'lv-tool-desc';
    detail.textContent = m.detail;
    item.appendChild(nameRow);
    item.appendChild(detail);
    list.appendChild(item);
  });
  body.appendChild(list);
}

function _lvRenderMemorySearchEditor(body, agent) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Searches past sessions for semantically relevant context and injects it into the prompt as [BRAIN CONTEXT]. Short or trivial messages (greetings, single words, affirmations) skip this step automatically via a regex gate to save latency.';
  body.appendChild(desc);

  const statusLbl = document.createElement('div');
  statusLbl.className = 'lv-tool-section-label';
  statusLbl.textContent = 'Runtime gate';
  body.appendChild(statusLbl);

  const loopEnabled = _isNodeLoopEnabled(agent, 'memory_search');
  _lvToggleRow(body, 'Memory search node (loop_logic)', loopEnabled, on => _setNodeLoopEnabled(agent, 'memory_search', on));

  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  const memEnabled = !disabled.has('memory');
  _lvToggleRow(body, 'Memory tool access (allowed_tools)', memEnabled, enabled => {
    const cur = new Set(Array.isArray(_lvPendingChanges.allowed_tools)
      ? _lvPendingChanges.allowed_tools
      : Array.isArray(agent.allowed_tools) ? [...agent.allowed_tools] : []);
    if (enabled) { cur.delete('memory'); } else { cur.add('memory'); }
    _lvSetPending('allowed_tools', [...cur]);
  });
}

function _lvRenderLlmEditor(body, agent) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Configure the language model used for this agent.';
  body.appendChild(desc);
  const MODELS = [
    'claude-opus-4-6',
    'claude-sonnet-4-6',
    'claude-haiku-4-5-20251001',
    'claude-3-5-sonnet-20241022',
    'claude-3-5-haiku-20241022',
    'claude-3-opus-20240229',
  ];
  _lvSelectRow(body, 'Model', agent.model || 'claude-3-5-sonnet-20241022', MODELS, val => {
    _lvSetPending('model', val);
  });
  _lvSliderRow(body, 'Temperature', agent.temperature ?? 1.0, 0, 1, 0.05, val => {
    _lvSetPending('temperature', Math.round(val * 100) / 100);
  });
  _lvSliderRow(body, 'Max tokens', agent.max_tokens ?? 8096, 512, 16384, 512, val => {
    _lvSetPending('max_tokens', parseInt(val, 10));
  });
}

function _lvRenderToolsEditor(body, agent) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Enable or disable Tier-2 tools for this agent. Always-on tools cannot be disabled.';
  body.appendChild(desc);
  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  TIER_2_CATEGORIES.forEach(cat => {
    const catLabel = document.createElement('div');
    catLabel.className = 'lv-edit-cat-label';
    catLabel.textContent = cat.label;
    body.appendChild(catLabel);
    cat.tools.forEach(tool => {
      const enabled = !disabled.has(tool.name);
      _lvToolToggleRow(body, tool, enabled, isOn => {
        const cur = new Set(Array.isArray(_lvPendingChanges.allowed_tools)
          ? _lvPendingChanges.allowed_tools
          : Array.isArray(agent.allowed_tools) ? [...agent.allowed_tools] : []);
        if (isOn) { cur.delete(tool.name); } else { cur.add(tool.name); }
        _lvSetPending('allowed_tools', [...cur]);
      });
    });
  });
}

function _lvRenderGuardrailsEditor(body, agent) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Configure the destructive-tool guardrail. The runtime gate controls whether confirmation is required at all; category blocks prevent individual tool classes from running.';
  body.appendChild(desc);

  // ── Runtime gate: whether the confirmation check runs at all ──────────────
  const gateLbl = document.createElement('div');
  gateLbl.className = 'lv-tool-section-label';
  gateLbl.textContent = 'Runtime gate';
  body.appendChild(gateLbl);

  const guardEnabled = _isNodeLoopEnabled(agent, 'guardrails');
  _lvToggleRow(body, 'Require confirmation for destructive tools', guardEnabled,
    on => _setNodeLoopEnabled(agent, 'guardrails', on));

  const guardDetails = document.createElement('div');
  guardDetails.className = 'lv-gate-details';
  guardDetails.style.marginTop = '6px';
  const _gdbRow = document.createElement('div');
  _gdbRow.className = 'lv-gate-detail-row';
  const _gdbl = document.createElement('span'); _gdbl.className = 'lv-gate-detail-label'; _gdbl.textContent = 'DB effect';
  const _gdbv = document.createElement('span'); _gdbv.className = 'lv-gate-detail-val';
  _gdbv.textContent = 'No direct writes from the guard itself. Tool results are stored in interactions as normal once confirmed — or suppressed if the user denies.';
  _gdbRow.appendChild(_gdbl); _gdbRow.appendChild(_gdbv); guardDetails.appendChild(_gdbRow);
  const _gewRow = document.createElement('div');
  _gewRow.className = 'lv-gate-detail-row';
  const _gewl = document.createElement('span'); _gewl.className = 'lv-gate-detail-label'; _gewl.textContent = 'Takes effect';
  const _gewb = document.createElement('span'); _gewb.className = 'lv-gate-effect-badge lv-gate-effect-immediate'; _gewb.textContent = 'Next message sent';
  _gewRow.appendChild(_gewl); _gewRow.appendChild(_gewb); guardDetails.appendChild(_gewRow);
  body.appendChild(guardDetails);

  // ── Per-category tool blocks ───────────────────────────────────────────────
  const toolsLbl = document.createElement('div');
  toolsLbl.className = 'lv-tool-section-label';
  toolsLbl.style.marginTop = '12px';
  toolsLbl.textContent = 'Tool category blocks';
  body.appendChild(toolsLbl);

  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  TIER_2_CATEGORIES.forEach(cat => {
    const allNames = cat.tools.map(t => t.name);
    const allBlocked = allNames.every(n => disabled.has(n));
    _lvToggleRow(body, `Allow: ${cat.label}`, !allBlocked, allowed => {
      const cur = new Set(Array.isArray(_lvPendingChanges.allowed_tools)
        ? _lvPendingChanges.allowed_tools
        : Array.isArray(agent.allowed_tools) ? [...agent.allowed_tools] : []);
      if (allowed) { allNames.forEach(n => cur.delete(n)); }
      else         { allNames.forEach(n => cur.add(n));    }
      _lvSetPending('allowed_tools', [...cur]);
    });
  });
}

function _lvRenderSessionSetupInfo(body, agent) {
  const steps = [
    {
      label: 'Ensure Session',
      text:  'Creates a session row in the DB if one does not already exist for this session_id. On subsequent messages in the same session this is a no-op.',
      db:    'Conditional write to sessions table — only on first message.',
    },
    {
      text:  'Determines which agent handles this request. Checks for optimizer routing, then the user\'s configured default agent, then falls back to the system default.',
      db:    'Read from agents and agent_templates tables.',
    },
    {
      label: 'Participants',
      text:  'Registers the user and the resolved agent as active participants in the session. Both writes are conditional — skipped if already registered.',
      db:    'Conditional writes to session_participants: one row for role "user", one for role "agent".',
    },
  ];

  steps.forEach(step => {
    const lbl = document.createElement('div');
    lbl.className = 'lv-tool-section-label';
    lbl.style.marginTop = '10px';
    lbl.textContent = step.label;
    body.appendChild(lbl);

    const desc = document.createElement('div');
    desc.className = 'lv-edit-desc';
    desc.textContent = step.text;
    body.appendChild(desc);

    const box = document.createElement('div');
    box.className = 'lv-gate-details';
    const row = document.createElement('div');
    row.className = 'lv-gate-detail-row';
    const lh = document.createElement('span'); lh.className = 'lv-gate-detail-label'; lh.textContent = 'DB effect';
    const rv = document.createElement('span'); rv.className = 'lv-gate-detail-val'; rv.textContent = step.db;
    row.appendChild(lh); row.appendChild(rv);
    box.appendChild(row);
    body.appendChild(box);
  });
}

function _lvRenderContinueEditor(body, agent) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Set how many agentic turns the loop can run before forcing a final response.';
  body.appendChild(desc);
  _lvSliderRow(body, 'Max turns', agent.max_turn_count ?? 10, 1, 30, 1, val => {
    _lvSetPending('max_turn_count', parseInt(val, 10));
  });

  // ── Permission gate (permission_chk) ─────────────────────────────────────
  const permLbl = document.createElement('div');
  permLbl.className = 'lv-tool-section-label';
  permLbl.style.marginTop = '12px';
  permLbl.textContent = 'Permission gate';
  body.appendChild(permLbl);

  const permDesc = document.createElement('div');
  permDesc.className = 'lv-edit-desc';
  permDesc.textContent = 'When the agent reaches the max turn ceiling, it pauses and asks the user "Do you want me to continue?" If approved, the ceiling extends by one full block (the same max turns value) and the loop resumes. This repeats each time the new ceiling is reached. Disable for headless agents that should run to completion without interruption.';
  body.appendChild(permDesc);

  const permDetails = document.createElement('div');
  permDetails.className = 'lv-gate-details';
  const _pdbRow = document.createElement('div'); _pdbRow.className = 'lv-gate-detail-row';
  const _pdbl = document.createElement('span'); _pdbl.className = 'lv-gate-detail-label'; _pdbl.textContent = 'DB effect';
  const _pdbv = document.createElement('span'); _pdbv.className = 'lv-gate-detail-val';
  _pdbv.textContent = 'The gate\'s ON/OFF state is saved to agents.loop_logic when you click Save — this persists across restarts and reloads. At runtime, the permission-request text is injected as a temporary system message into the LLM\'s context only; it is not written to the interactions table.';
  _pdbRow.appendChild(_pdbl); _pdbRow.appendChild(_pdbv); permDetails.appendChild(_pdbRow);
  const _pewRow = document.createElement('div'); _pewRow.className = 'lv-gate-detail-row';
  const _pewl = document.createElement('span'); _pewl.className = 'lv-gate-detail-label'; _pewl.textContent = 'Takes effect';
  const _pewb = document.createElement('span'); _pewb.className = 'lv-gate-effect-badge lv-gate-effect-immediate'; _pewb.textContent = 'Next message sent';
  _pewRow.appendChild(_pewl); _pewRow.appendChild(_pewb); permDetails.appendChild(_pewRow);
  body.appendChild(permDetails);

  const permEnabled = _isNodeLoopEnabled(agent, 'permission_chk');
  _lvToggleRow(body, 'Ask permission before stopping', permEnabled,
    on => _setNodeLoopEnabled(agent, 'permission_chk', on));
}

function _lvRenderMemorySaveEditor(body, agent) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = 'Control whether key facts from each session are saved to long-term memory.';
  body.appendChild(desc);
  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  const saveEnabled = !disabled.has('memory_save');
  _lvToggleRow(body, 'Save facts to long-term memory', saveEnabled, enabled => {
    const cur = new Set(Array.isArray(_lvPendingChanges.allowed_tools)
      ? _lvPendingChanges.allowed_tools
      : Array.isArray(agent.allowed_tools) ? [...agent.allowed_tools] : []);
    if (enabled) { cur.delete('memory_save'); } else { cur.add('memory_save'); }
    _lvSetPending('allowed_tools', [...cur]);
  });
}

// ── Generic editor for loop-gated nodes (interrupt_chk, permission_chk, etc.) ─
// details: { dbEffect: string, effectiveWhen: string, effectiveClass: string }
function _lvRenderGatedNodeEditor(body, agent, nodeId, label, description, details) {
  const desc = document.createElement('div');
  desc.className = 'lv-edit-desc';
  desc.textContent = description;
  body.appendChild(desc);

  if (details) {
    const box = document.createElement('div');
    box.className = 'lv-gate-details';

    if (details.dbEffect) {
      const row = document.createElement('div');
      row.className = 'lv-gate-detail-row';
      const lbl = document.createElement('span');
      lbl.className = 'lv-gate-detail-label';
      lbl.textContent = 'DB effect';
      const val = document.createElement('span');
      val.className = 'lv-gate-detail-val';
      val.textContent = details.dbEffect;
      row.appendChild(lbl);
      row.appendChild(val);
      box.appendChild(row);
    }

    if (details.effectiveWhen) {
      const row = document.createElement('div');
      row.className = 'lv-gate-detail-row';
      const lbl = document.createElement('span');
      lbl.className = 'lv-gate-detail-label';
      lbl.textContent = 'Takes effect';
      const badge = document.createElement('span');
      badge.className = 'lv-gate-effect-badge lv-gate-effect-' + (details.effectiveClass || 'immediate');
      badge.textContent = details.effectiveWhen;
      row.appendChild(lbl);
      row.appendChild(badge);
      box.appendChild(row);
    }

    body.appendChild(box);
  }

  const statusLbl = document.createElement('div');
  statusLbl.className = 'lv-tool-section-label';
  statusLbl.textContent = 'Runtime gate';
  body.appendChild(statusLbl);

  const enabled = _isNodeLoopEnabled(agent, nodeId);
  _lvToggleRow(body, label, enabled, on => _setNodeLoopEnabled(agent, nodeId, on));
}

// ── UI helper widgets ─────────────────────────────────────────────────────

function _lvToggleRow(container, label, initialValue, onChange) {
  const row = document.createElement('div');
  row.className = 'lv-edit-toggle-row';
  const lbl = document.createElement('span');
  lbl.className = 'lv-edit-toggle-label';
  lbl.textContent = label;
  const tog = document.createElement('button');
  tog.className = 'lv-edit-toggle';
  tog.dataset.on = initialValue ? '1' : '0';
  tog.textContent = initialValue ? 'ON' : 'OFF';
  tog.addEventListener('click', e => {
    e.stopPropagation();
    const nowOn = tog.dataset.on !== '1';
    tog.dataset.on = nowOn ? '1' : '0';
    tog.textContent = nowOn ? 'ON' : 'OFF';
    onChange(nowOn);
  });
  row.appendChild(lbl);
  row.appendChild(tog);
  container.appendChild(row);
}

function _lvToolToggleRow(container, tool, enabled, onChange) {
  const row = document.createElement('div');
  row.className = 'lv-edit-tool-row';
  const nameEl = document.createElement('span');
  nameEl.className = 'lv-edit-tool-name';
  nameEl.textContent = tool.name;
  const left = document.createElement('div');
  left.className = 'lv-edit-tool-left';
  left.appendChild(nameEl);
  if (tool.destructive) {
    const bdg = document.createElement('span');
    bdg.className = 'lv-edit-tool-badge';
    bdg.textContent = '🛡';
    left.appendChild(bdg);
  }
  const descEl = document.createElement('div');
  descEl.className = 'lv-edit-tool-desc';
  descEl.textContent = tool.desc;
  const tog = document.createElement('button');
  tog.className = 'lv-edit-toggle small';
  tog.dataset.on = enabled ? '1' : '0';
  tog.textContent = enabled ? 'ON' : 'OFF';
  tog.addEventListener('click', e => {
    e.stopPropagation();
    const nowOn = tog.dataset.on !== '1';
    tog.dataset.on = nowOn ? '1' : '0';
    tog.textContent = nowOn ? 'ON' : 'OFF';
    onChange(nowOn);
  });
  const nameRow = document.createElement('div');
  nameRow.style.cssText = 'display:flex;align-items:center;justify-content:space-between;';
  const leftBlock = document.createElement('div');
  leftBlock.appendChild(left);
  leftBlock.appendChild(descEl);
  nameRow.appendChild(leftBlock);
  nameRow.appendChild(tog);
  row.appendChild(nameRow);
  container.appendChild(row);
}

function _lvSliderRow(container, label, initialValue, min, max, step, onChange) {
  const row = document.createElement('div');
  row.className = 'lv-edit-slider-row';
  const labelRow = document.createElement('div');
  labelRow.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;';
  const lbl = document.createElement('span');
  lbl.className = 'lv-edit-toggle-label';
  lbl.textContent = label;
  const valEl = document.createElement('span');
  valEl.className = 'lv-edit-slider-val';
  valEl.textContent = initialValue;
  labelRow.appendChild(lbl);
  labelRow.appendChild(valEl);
  const slider = document.createElement('input');
  slider.type = 'range';
  slider.className = 'lv-edit-slider';
  slider.min  = min;
  slider.max  = max;
  slider.step = step;
  slider.value = initialValue;
  slider.addEventListener('input', () => {
    const v = parseFloat(slider.value);
    valEl.textContent = step < 1 ? v.toFixed(2) : slider.value;
    onChange(v);
  });
  row.appendChild(labelRow);
  row.appendChild(slider);
  container.appendChild(row);
}

function _lvSelectRow(container, label, initialValue, options, onChange) {
  const row = document.createElement('div');
  row.className = 'lv-edit-select-row';
  const lbl = document.createElement('div');
  lbl.className = 'lv-edit-toggle-label';
  lbl.textContent = label;
  lbl.style.marginBottom = '4px';
  const sel = document.createElement('select');
  sel.className = 'lv-edit-select';
  options.forEach(opt => {
    const o = document.createElement('option');
    o.value = opt;
    o.textContent = opt;
    if (opt === initialValue) o.selected = true;
    sel.appendChild(o);
  });
  sel.addEventListener('change', () => onChange(sel.value));
  row.appendChild(lbl);
  row.appendChild(sel);
  container.appendChild(row);
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
  const ttVal   = fv('trigger_type');   if (ttVal !== undefined) updates.trigger_type   = ttVal;
  const tkVal   = fv('trigger_key');    if (tkVal !== undefined) updates.trigger_key    = tkVal || null;
  const umChecked = panelEl.querySelector('[data-field="user_mode"]:checked');
  if (umChecked) updates.user_mode = umChecked.value;

  // Per-agent LLM override
  if (panelEl._llmState) updates.llm_config = { ...panelEl._llmState };

  // Slot writes: split into admin (slots payload) and member (overrides payload).
  const sstate = panelEl._slotState || {};
  const role = sstate.userRole || 'member';
  let slotsPayload = null;
  let overridesPayload = null;
  if (sstate.loaded) {
    if (role === 'admin') {
      slotsPayload = (sstate.slots || []).map(s => ({
        slot_name: s.slot_name,
        order_index: s.order_index || 0,
        lock: !!s.lock,
        merge_mode: s.merge_mode || 'replace',
        content: s.content || '',
      }));
      updates.slots = slotsPayload;
      if (sstate.resetOverridesFor && sstate.resetOverridesFor.size > 0) {
        updates.reset_overrides_for = Array.from(sstate.resetOverridesFor);
      }
    } else {
      const items = [];
      for (const s of (sstate.slots || [])) {
        if (s.lock) continue;
        const v = sstate.overrides && sstate.overrides[s.slot_name];
        if (v === undefined || v === null) continue;
        items.push({ slot_name: s.slot_name, content: v });
      }
      if (items.length > 0) overridesPayload = items;
    }
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

      // Member-only: send override writes via the my-prompts endpoint.
      if (overridesPayload) {
        try {
          await fetch(`/api/v1/agents/${agent.id}/my-prompts`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: app.currentUserId, slots: overridesPayload }),
          });
        } catch (_) { /* surface only main save status */ }
      }
      // Reset the reset-overrides marks so saving again doesn't repeat.
      if (sstate.resetOverridesFor) sstate.resetOverridesFor.clear();
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
  if (newBtn) newBtn.addEventListener('click', async () => {
    if (!modal) return;
    modal.classList.remove('hidden');
    // Populate template dropdown
    const tplSelect = document.getElementById('agents-create-template');
    if (tplSelect) {
      tplSelect.innerHTML = '<option value="">— No template —</option>';
      try {
        const url = `/api/v1/agents/templates?user_id=${encodeURIComponent(app.currentUserId)}&discoverable_only=true${_userIsAdmin ? '&include_admin=true' : ''}`;
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json();
          (data.templates || []).forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.name || t.id;
            if (t.id === 'default') opt.selected = true;
            tplSelect.appendChild(opt);
          });
        }
      } catch (e) {
        console.warn('agents: failed to load templates', e);
      }
    }
  });
  if (cancelBtn) cancelBtn.addEventListener('click', () => modal && modal.classList.add('hidden'));

  if (createBtn) {
    createBtn.addEventListener('click', async () => {
      const nameEl = document.getElementById('agents-create-name');
      const descEl = document.getElementById('agents-create-desc');
      const name   = nameEl ? nameEl.value.trim() : '';
      if (!name) { nameEl && nameEl.focus(); return; }

      try {
        const tplSelect = document.getElementById('agents-create-template');
        const templateId = tplSelect ? tplSelect.value : 'default';
        const res = await fetch('/api/v1/agents', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            user_id: app.currentUserId,
            name,
            description: descEl ? descEl.value.trim() : '',
            template_id: templateId || 'default',
          }),
        });
        const data = await res.json();
        if (res.ok) {
          modal && modal.classList.add('hidden');
          if (nameEl) nameEl.value = '';
          if (descEl) descEl.value = '';
          if (tplSelect) tplSelect.value = '';
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
