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
import { apiPath } from './config.js';
import { fetchAllToolMeta } from './loop-logic.js';
import { NODE_PANEL_INFO } from './loop-node-data.js';
import { LOOP_W, LOOP_H, LOOP_NODES, TOGGLEABLE_NODES, renderLoopDiagram } from './loop-diagram.js';
import { icon } from './icons.js';
import { wireChatPillUploads } from './attachments.js';
import { sortAgentsForDisplay } from './ordering.js';
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
let _extendLlmToAgents = true; // mirrors app-settings.json extend_llm_to_agents

// ── Mock agent (create-in-place) ─────────────────────────────────────────────
const MOCK_AGENT_ID = '__new__';

function _createMockAgent() {
  return {
    id: MOCK_AGENT_ID,
    name: '',
    description: '',
    source: 'custom',
    icon: null,
    access_level: 'user',
    llm_config: { use_default: true },
    allowed_tools: null, // all tools allowed
    loop_logic: null,
    trigger_type: null,
    trigger_key: null,
    max_turn_count: 0,
    max_wall_seconds: null,
    max_identical_tool_calls: 0,
    max_stall_strikes: 0,
    user_mode: 'user',
    slots: [],
    discoverable: false,
    is_mock: true,
  };
}

function _isMockAgent(agent) {
  return agent && agent.id === MOCK_AGENT_ID;
}

// ── Agent Manager (per-detail-panel chat bar) ────────────────────────────────
const AGENT_BUILDER_TEMPLATE_ID = 'agent-builder';
const _agentBuilderAgentCache = new Map(); // userId → agentId

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
  handoff_to_closer:            'Hand off optimization results to the Closer',
  deploy_optimization:          'Deploy an approved optimization change (Closer only)',
  read_source:                  'Read any file on the server filesystem',
  write_source:                 'Create or overwrite a file (with backup)',
  edit_source:                  'Replace exact text in a file',
  delete_source:                'Delete a file or directory',
  resolve_conflict:             'Resolve merge conflict markers in a file',
  run_command:                  'Execute a shell command on the server',
  restart_server:               'Restart the webAgent server process',
  register_user:                'Register a new user from a communication channel',
  generate_image:               'Generate an image from a text description',
  read_diagnostics:             'Read server diagnostics, errors, and logs',
  maps_geocode:                 'Geocode an address or reverse-geocode coordinates',
};

// ── Tool tier definitions ─────────────────────────────────────────────
// Tier 0: Admin-only. Never shown or toggleable for normal agents.
const TIER_0_ADMIN = new Set([
  'read_source','write_source','edit_source','delete_source','resolve_conflict',
  'run_command','restart_server',
  'run_worker_trials','handoff_to_closer','deploy_optimization',
]);

// Tier 1: Always-on. Present for all agents, not shown as toggleable.
const TIER_1_ALWAYS_ON = new Set([
  'list_tools','search_tools','get_tool_definition',
  'get_time','get_date','calculate','read_attachment',
  'register_user',
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
      'maps_geocode',
    ],
  },
  {
    label: 'Data & Context',
    condition: null,
    tools: [
      'db_query','list_agent_context_documents','get_agent_context_document',
      'update_agent_context_document','insert_agent_context_document','create_tool',
      'get_time','get_date','calculate','register_user',
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
    label: 'Image & Diagnostics',
    condition: null,
    tools: [
      'generate_image','read_diagnostics',
    ],
  },
  {
    label: 'Admin & System',
    condition: 'admin',
    tools: [
      'read_source','write_source','edit_source','delete_source','resolve_conflict',
      'run_command','restart_server',
      'run_worker_trials','handoff_to_closer','deploy_optimization',
    ],
  },
  {
    label: 'Codebase Admin',
    condition: 'codebase_admin',
    tools: [
      'read_source','write_source','edit_source','delete_source','resolve_conflict',
      'run_command','restart_server',
    ],
  },
  {
    label: 'Web Access',
    condition: 'web_access',
    tools: ['web_search','get_weather','maps_geocode'],
  },
  {
    label: 'Browser Control',
    condition: 'browser_control',
    tools: ['browser_action','http_request'],
  },
  {
    label: 'Image Generation',
    condition: 'image_generation',
    tools: ['generate_image'],
  },
  {
    label: 'Create Tools',
    condition: 'create_tools',
    tools: ['create_tool'],
  },
  {
    label: 'Diagnostics',
    condition: 'diagnostics',
    tools: ['read_diagnostics'],
  },
  {
    label: 'Agent Orchestration',
    condition: 'agent_orchestration',
    tools: ['delegate_to_agent','list_delegatable_agents'],
  },
];

const PIPELINE_TOOLS = {
  opt_planner: ['run_worker_trials','handoff_to_closer'],
  opt_closer:  ['deploy_optimization'],
};

const DESTRUCTIVE = new Set([
  'db_query','update_agent_context_document','create_tool',
  'delete_webhook','write_source','edit_source','delete_source','resolve_conflict',
  'run_command','restart_server',
]);

// ── Ability-to-tools mapping ──────────────────────────────────────────
// Maps agent connection_type (ability) to the tool names it unlocks.
// Mirrors the gating logic in app/tools/loader.py _inject_builtin_tools.
const ABILITY_TO_TOOLS = {
  codebase_admin:   ['read_source','write_source','edit_source','delete_source','resolve_conflict','run_command','restart_server','db_query','git_tool'],
  web_access:       ['web_search','get_weather','maps_geocode'],
  browser_control:  ['browser_action','http_request'],
  image_generation: ['generate_image'],
  create_tools:     ['create_tool'],
  diagnostics:      ['read_diagnostics'],
  automation:       [],  // automation tools are injected dynamically; no static tool names to list
  visualizer:       [],  // visualizer tools are injected dynamically
  agent_orchestration: ['delegate_to_agent','list_delegatable_agents'],
  // register_user is always-on (Tier 1), not gated by an ability
};

function _toolsForAgent(agent, enabledAbilities) {
  const id = agent.id || '';
  if (agent.is_admin_agent) {
    return [...TIER_1_ALWAYS_ON, ...TIER_2_ALL,
            'read_source','write_source','edit_source','delete_source','run_command','restart_server','git_tool'];
  }
  if (id === 'opt_planner') return [...TIER_1_ALWAYS_ON, ...TIER_2_ALL, ...(PIPELINE_TOOLS.opt_planner || [])];
  if (id === 'opt_closer')  return [...TIER_1_ALWAYS_ON, ...TIER_2_ALL, ...(PIPELINE_TOOLS.opt_closer  || [])];

  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  const base = TIER_2_ALL.filter(name => !disabled.has(name));

  // Add ability-gated tools for any enabled abilities
  const abilityTools = [];
  if (enabledAbilities) {
    for (const [ability, tools] of Object.entries(ABILITY_TO_TOOLS)) {
      if (enabledAbilities.has(ability)) {
        for (const t of tools) {
          if (!disabled.has(t) && !abilityTools.includes(t)) {
            abilityTools.push(t);
          }
        }
      }
    }
  }

  return [
    ...TIER_1_ALWAYS_ON,
    ...base,
    ...abilityTools,
  ];
}

// ── Init ──────────────────────────────────────────────────────────────────────

export async function initAgents() {
  // Always bind the pill bar so the send button works even if the user hasn't
  // logged in yet — _sendAgentBuilderPrompt checks app.currentUserId itself.
  _bindAgentBuilderBar();
  if (!app.currentUserId) return;

  // If the agents tab isn't active, defer the 3 fetches + grid render until
  // the user switches to it. The tab switch calls startAgents() → initAgents()
  // again, which will then proceed past this guard.
  const tabSelect = document.getElementById('main-tab-select');
  if (tabSelect && tabSelect.value !== 'agents') {
    return;
  }

  // Show a shimmer skeleton immediately so the grid isn't blank while the
  // profile/agents/settings fetches are in flight (the gap is most visible on
  // a cold backend). Only on first load — a refresh already has cards to show.
  if (_agents.length === 0) _renderSkeleton();

  await Promise.all([_loadProfile(), _loadAgents(), _loadAppSettings()]);
  _renderList();
  _bindSystemToggle();
  _restoreViewState();
}

// Wire the "Show system agents" checkbox below the grid. Toggling it re-fetches
// the agent list with/without the built-in utility agents and re-renders.
function _bindSystemToggle() {
  const cb = document.getElementById('agents-show-system');
  if (!cb || cb._bound) return;
  cb._bound = true;
  cb.checked = _showSystem;
  cb.addEventListener('change', async () => {
    _showSystem = cb.checked;
    _renderSkeleton();
    await _loadAgents();
    _renderList();
  });
}

// Placeholder cards shown while the agent list loads. Replaced by _renderList()
// as soon as the fetches resolve.
function _renderSkeleton(count = 6) {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;
  let html = '';
  for (let i = 0; i < count; i++) {
    html += `
      <div class="agent-row">
        <div class="agent-skeleton" aria-hidden="true">
          <div class="sk-icon sk-shimmer"></div>
          <div class="sk-lines">
            <div class="sk-line long sk-shimmer"></div>
            <div class="sk-line short sk-shimmer"></div>
          </div>
        </div>
      </div>`;
  }
  grid.innerHTML = html;
}

// Re-sync this page after the chat-header dropdown changes agent order or pins.
// Re-fetches so a drag-reorder (which updates the synced sort_order) is picked
// up, then re-renders. No-op when the grid isn't mounted.
app.refreshAgentsOrder = async function refreshAgentsOrder() {
  if (!app.currentUserId) return;
  if (!document.getElementById('agents-grid')) return;
  await _loadAgents();
  _renderList();
};

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
  // Use cached data from main.js's early fetch if available.
  const cached = window.__agentsProfileData;
  if (cached) {
    _userIsAdmin = !!cached.is_admin;
    return;
  }
  try {
    const res = await fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(app.currentUserId)}`);
    if (res.ok) {
      const data = await res.json();
      _userIsAdmin = !!data.is_admin;
    }
  } catch (e) {
    console.warn('agents: could not load profile', e);
  }
}

// Whether the "Show system agents" toggle is on. When off (default) the page
// shows only the agents the user added themselves; when on, the built-in utility
// agents are fetched and shown too (and stay configurable).
let _showSystem = false;

async function _loadAgents() {
  try {
    // Use shared data from sessions.js if available — but only for the default
    // (user-own) view. The system view needs its own fetch with include_system.
    if (!_showSystem) {
      const shared = window.__agentsSharedData;
      if (shared && shared.agents) {
        _agents = shared.agents;
        return;
      }
    }
    const params = new URLSearchParams({ user_id: app.currentUserId });
    if (_showSystem) params.set('include_system', 'true');
    const res = await fetch(`/api/v1/agents?${params.toString()}`);
    if (res.ok) {
      const data = await res.json();
      _agents = data.agents || [];
      // Only cache the default view so other modules don't pick up system rows.
      if (!_showSystem) window.__agentsSharedData = data;
    }
  } catch (e) {
    console.warn('agents: could not load agent list', e);
  }
}

async function _loadAppSettings() {
  // Use cached data from main.js's early fetch if available.
  const cached = window.__agentsAppSettingsData;
  if (cached) {
    _extendLlmToAgents = cached.extend_llm_to_agents !== false;
    return;
  }
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
  if (id.includes('planner') || id.includes('closer') || id.includes('opt')) return 'color-purple';
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

  // ── Mock card (always first) ──────────────────────────────────────────────
  const mockAgent = _createMockAgent();
  const mockExpanded = _expandedAgents.has(MOCK_AGENT_ID);
  _renderAgentCard(grid, mockAgent, mockExpanded);

  // ── Real agents ───────────────────────────────────────────────────────────
  if (_agents.length === 0) {
    // No agents yet — just the mock card is fine
    return;
  }

  // Match the chat-header agent dropdown exactly: the server returns agents in
  // the synced sort_order; sortAgentsForDisplay then floats pinned agents to
  // the top. Reordering happens in the dropdown — this page just follows.
  const ordered = sortAgentsForDisplay(_agents, app.currentUserId);
  for (const agent of ordered) {
    const isExpanded = _expandedAgents.has(agent.id);
    _renderAgentCard(grid, agent, isExpanded);
  }
}

function _renderAgentCard(grid, agent, isExpanded) {
  const isMock = _isMockAgent(agent);

  const badgeType  = isMock ? 'custom'
                   : agent.access_level === 'admin_only' ? 'admin'
                   : agent.source === 'custom'            ? 'custom'
                   : 'system';
  const badgeLabel = isMock ? 'New'
                   : agent.access_level === 'admin_only' ? 'Admin'
                   : agent.source === 'custom'            ? 'Custom'
                   : 'System';

  const isCustom = agent.source === 'custom' || isMock;

  const card = document.createElement('div');
  card.className = 'agent-card' + (isExpanded ? ' active' : '') + (isMock ? ' agent-card-mock' : '');
  const iconSize = isExpanded ? '20px' : '24px';
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap ${isMock ? 'color-blue' : _iconColor(agent)}">
        ${isMock ? icon('plus', { size: iconSize }) : (agent.icon || icon('bot', { size: iconSize }))}
      </div>
      <div class="agent-card-meta">
        <div class="agent-card-name-row">
          <span class="agent-card-name">${isMock ? 'Create a new agent' : _esc(_displayName(agent))}</span>
          <span class="agent-status-dot ${isMock ? 'inactive' : ''}"></span>
        </div>
        ${!isMock && agent.description ? `<div class="agent-card-desc">${_esc(agent.description)}</div>` : ''}
        ${isMock ? '<div class="agent-card-desc agent-card-mock-hint">Click to configure, then save</div>' : ''}
      </div>
      <div class="agent-card-badge-wrap">
        <span class="agent-badge ${badgeType}">${badgeLabel}</span>
        ${isCustom && !isMock ? '<button class="agent-card-action-btn delete-btn">Delete</button>' : ''}
      </div>
    </div>
    <div class="agent-card-tabs" role="tablist"></div>
  `;

  // Wire inline action buttons — stopPropagation so click doesn't toggle the panel
  const deleteBtn = card.querySelector('.delete-btn');
  if (deleteBtn) {
    deleteBtn.addEventListener('click', e => { e.stopPropagation(); _deleteAgent(agent); });
  }

  card.addEventListener('click', () => _selectAgent(agent));

  // Each agent gets its own .agent-row; the detail panel lives inside it
  const row = document.createElement('div');
  row.className = 'agent-row' + (isExpanded ? ' expanded' : '');
  row.dataset.agentId = agent.id;
  row.appendChild(card);

  // Only build the detail panel when a specific tab is selected (not just expanded)
  const state = _expandedAgents.get(agent.id);
  const hasActiveTab = state && state.tab;
  let panel = null;
  if (hasActiveTab) {
    panel = _buildDetailPanel(agent);
    row.appendChild(panel);
  }

  // Tabs render in both collapsed and expanded states; clicking a tab on a
  // collapsed card expands it to that tab.
  const cardTabBar = card.querySelector('.agent-card-tabs');
  if (cardTabBar) _populateAgentTabBar(cardTabBar, agent, panel);

  grid.appendChild(row);
}

// ── Selection / toggle ────────────────────────────────────────────────────────

function _selectAgent(agent) {
  if (_expandedAgents.has(agent.id)) {
    _expandedAgents.delete(agent.id);
  } else {
    // Expand to show the full card (with tabs) but no detail panel yet.
    // User clicks a tab to open a specific panel.
    _expandedAgents.set(agent.id, { tab: null });
  }
  _renderList();
  _saveViewState();
}

// ── Mock agent helpers ────────────────────────────────────────────────────────

function _getMockAgent() {
  // Return the live mock agent from the expanded state, or create a fresh one
  const existing = _agents.find(a => a.id === MOCK_AGENT_ID);
  if (existing) return existing;
  // The mock agent isn't in _agents — it's rendered separately in _renderList
  return _createMockAgent();
}

function _mockAgentName() {
  const row = document.querySelector(`.agent-row[data-agent-id="${MOCK_AGENT_ID}"]`);
  if (!row) return '';
  const nameEl = row.querySelector('[data-field="name"]');
  return nameEl ? nameEl.value.trim() : '';
}

// ── Per-row detail panel ──────────────────────────────────────────────────────

function _populateAgentTabBar(tabBar, agent, panel) {
  const state = _expandedAgents.get(agent.id);
  const isMock = _isMockAgent(agent);
  // Highlight a tab only when the card is open — a collapsed card has no
  // active content, so no tab should look selected.
  // When tab is null (card expanded but no tab selected), no tab is highlighted.
  const activeTab = state ? state.tab : null;
  tabBar.innerHTML = '';
  const tabs = [['config','Config'],['tools','Tools'],['test','Agent Loop'],['connections','Abilities']];
  if (!isMock) {
    if (state?.automationEnabled) tabs.push(['automation','Automation']);
    if (_userIsAdmin) tabs.push(['members','Members']);
    tabs.push(['monetization','Monetization']);
  }

  // ── Compute synchronous counts (base tools, no abilities yet) ──
  const baseToolCount = _toolsForAgent(agent).length;

  for (const [key, label] of tabs) {
    const btn = document.createElement('button');
    btn.className = 'agents-detail-tab' + (activeTab === key ? ' active' : '');
    btn.dataset.tab = key;
    // Set label with count badge where applicable
    if (key === 'tools') {
      btn.innerHTML = `${label} <span class="tab-count-badge tab-count-badge-pending">…</span>`;
    } else if (key === 'connections') {
      btn.innerHTML = `${label} <span class="tab-count-badge tab-count-badge-pending">…</span>`;
    } else if (key === 'members') {
      btn.innerHTML = `${label} <span class="tab-count-badge tab-count-badge-pending">…</span>`;
    } else {
      btn.textContent = label;
    }
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const entry = _expandedAgents.get(agent.id);
      if (entry) {
        // Card is open → switch to the clicked tab (never collapse from tab clicks).
        _expandedAgents.set(agent.id, { tab: key });
      } else {
        // Collapsed → expand to the clicked tab.
        _expandedAgents.set(agent.id, { tab: key });
      }
      _renderList();
      _saveViewState();
    });
    tabBar.appendChild(btn);
  }

  if (isMock) return; // no async fetches for mock agent

  // ── Async: fetch abilities + compute tool count ──
  _fetchAbilitiesAndTools(agent).then(({ toolCount, abilitiesCount }) => {
    const toolsBtn = tabBar.querySelector('.agents-detail-tab[data-tab="tools"]');
    if (toolsBtn) {
      toolsBtn.innerHTML = `Tools <span class="tab-count-badge">${toolCount}</span>`;
    }
    const connBtn = tabBar.querySelector('.agents-detail-tab[data-tab="connections"]');
    if (connBtn) {
      connBtn.innerHTML = `Abilities <span class="tab-count-badge">${abilitiesCount}</span>`;
    }
  }).catch(() => {
    // Fallback: show base count if fetch fails
    const toolsBtn = tabBar.querySelector('.agents-detail-tab[data-tab="tools"]');
    if (toolsBtn) {
      toolsBtn.innerHTML = `Tools <span class="tab-count-badge">${baseToolCount}</span>`;
    }
    const connBtn = tabBar.querySelector('.agents-detail-tab[data-tab="connections"]');
    if (connBtn) {
      connBtn.innerHTML = `Abilities <span class="tab-count-badge">0</span>`;
    }
  });

  // ── Async: fetch members count ──
  _fetchMembersCount(agent).then(count => {
    const memBtn = tabBar.querySelector('.agents-detail-tab[data-tab="members"]');
    if (memBtn) {
      memBtn.innerHTML = `Members <span class="tab-count-badge">${count}</span>`;
    }
  }).catch(() => {
    const memBtn = tabBar.querySelector('.agents-detail-tab[data-tab="members"]');
    if (memBtn) {
      memBtn.innerHTML = `Members <span class="tab-count-badge">0</span>`;
    }
  });
}

async function _fetchAbilitiesCount(agent) {
  const res = await fetch(`/api/v1/agents/${agent.id}/abilities?user_id=${encodeURIComponent(app.currentUserId)}`);
  if (!res.ok) return 0;
  const data = await res.json();
  const abilities = data.abilities || [];
  // Count non-implicit, enabled abilities
  return abilities.filter(a => !a.implicit && a.enabled).length;
}

/**
 * Fetch abilities and compute both the abilities count and the tool count
 * (including ability-gated tools). Returns { toolCount, abilitiesCount }.
 *
 * Uses the connections API to get enabled ability-type connection_types
 * (codebase_admin, web_access, browser_control, etc.) and the abilities
 * API to count non-implicit OAuth abilities.
 */
async function _fetchAbilitiesAndTools(agent) {
  // Fetch connections (for ability-type toggles like codebase_admin)
  let connEnabled = new Set();
  let abilitiesCount = 0;
  try {
    const [connRes, abilRes] = await Promise.all([
      fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`),
      fetch(`/api/v1/agents/${agent.id}/abilities?user_id=${encodeURIComponent(app.currentUserId)}`).catch(() => null),
    ]);
    if (connRes.ok) {
      const connData = await connRes.json();
      for (const c of (connData.connections || [])) {
        if (c.enabled && c.section === 'ability') {
          connEnabled.add(c.connection_type);
        }
      }
    }
    if (abilRes && abilRes.ok) {
      const abilData = await abilRes.json();
      const abilities = abilData.abilities || [];
      abilitiesCount = abilities.filter(a => !a.implicit && a.enabled).length;
    }
  } catch (e) {
    // non-fatal — proceed without ability data
  }

  const tools = _toolsForAgent(agent, connEnabled);
  return { toolCount: tools.length, abilitiesCount };
}

async function _fetchMembersCount(agent) {
  const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/members?user_id=${encodeURIComponent(app.currentUserId)}`);
  if (!res.ok) return 0;
  const data = await res.json();
  const admins = data.admins || [];
  const members = data.members || [];
  return admins.length + members.length;
}

function _refreshAgentTabBar(agent) {
  const row = document.querySelector(`.agent-row[data-agent-id="${agent.id}"]`);
  if (!row) return;
  const tabBar = row.querySelector('.agent-card-tabs');
  const panel = row.querySelector('.agent-detail-panel');
  if (tabBar && panel) _populateAgentTabBar(tabBar, agent, panel);
}

async function _detectAgentAbilities(agent, panel) {
  // Decide whether the Automation tab should appear by inspecting the agent's
  // connections. The `automation` ability surfaces only if (a) the app admin
  // configured it AND (b) the agent admin toggled it on for this agent.
  const state = _expandedAgents.get(agent.id);
  if (!state) return;
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`);
    if (!res.ok) return;
    const data = await res.json();
    const conns = data.connections || [];
    const automation = conns.find(c => c.connection_type === 'automation' && c.section === 'ability');
    const enabled = !!(automation && automation.enabled);
    if (state.automationEnabled !== enabled) {
      state.automationEnabled = enabled;
      if (!enabled && state.tab === 'automation') state.tab = 'config';
      _refreshAgentTabBar(agent);
      _renderPanelBody(agent, panel);
    }
  } catch (_e) { /* leave tab hidden on error */ }
}

function _buildDetailPanel(agent) {
  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel';
  panel.dataset.agentId = agent.id;

  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  panel.appendChild(content);

  // For the mock agent, add a create button bar at the top of the panel
  if (_isMockAgent(agent)) {
    const createBar = document.createElement('div');
    createBar.className = 'agent-mock-create-bar';
    const createBtn = document.createElement('button');
    createBtn.className = 'agent-mock-create-btn';
    createBtn.innerHTML = icon('plus', { size: '16px' }) + ' Create Agent';
    createBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _createAgentFromMock(panel);
    });
    createBar.appendChild(createBtn);
    content.appendChild(createBar);
  }

  // Scrollable body (the tab carousel now lives in the card header above)
  const body = document.createElement('div');
  body.className = 'agent-detail-body';
  content.appendChild(body);

  // Render initial tab content
  _renderPanelBody(agent, panel);

  // Asynchronously discover whether the Automation tab should be visible.
  if (!_isMockAgent(agent)) {
    _detectAgentAbilities(agent, panel);
  }

  return panel;
}

// ── Agent Manager chat bar ────────────────────────────────────────────────────
//
// Persistent textarea + send button at the top of every agent detail body.
// Submitting a prompt hands the conversation off to the built-in `agent-builder`
// system agent (find-or-created on first send), then injects the message into
// the main chat composer. Mirrors the Pages tab Visualizer pattern in
// ui/js/autoagent.js:129-232.

async function _findAgentBuilderAgent(userId) {
  const cached = _agentBuilderAgentCache.get(userId);
  if (cached) return cached;
  try {
    const res = await fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`));
    if (!res.ok) return null;
    const data = await res.json();
    const match = (data.agents || []).find(a => a.template_id === AGENT_BUILDER_TEMPLATE_ID);
    if (match) {
      _agentBuilderAgentCache.set(userId, match.id);
      return match.id;
    }
  } catch (e) {
    console.warn('[AgentMgr] find failed:', e);
  }
  return null;
}

async function _createAgentBuilderAgent(userId) {
  const res = await fetch(apiPath('/api/v1/agents'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_id: userId,
      name: 'Agent Manager',
      description: 'Guides agent configuration — templates, prompt slots, abilities, model settings.',
      template_id: AGENT_BUILDER_TEMPLATE_ID,
    }),
  });
  if (!res.ok) {
    // Try to read error detail from the response
    let detail = `agent create failed (${res.status})`;
    try { const errBody = await res.json(); detail = errBody.detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const data = await res.json();
  const id = data.agent && data.agent.id;
  if (!id) throw new Error('agent create returned no id');
  _agentBuilderAgentCache.set(userId, id);
  if (typeof app.populateAgentSelect === 'function') {
    try { await app.populateAgentSelect(userId); } catch (_) {}
  }
  return id;
}

async function _ensureAgentBuilderAgent(userId) {
  return (await _findAgentBuilderAgent(userId)) || (await _createAgentBuilderAgent(userId));
}

async function _sendAgentBuilderPrompt() {
  const input = document.getElementById('agent-builder-bar-input');
  const row   = document.getElementById('agent-builder-bar-row');
  if (!input) { console.error('[AgentMgr] input not found'); return; }

  const text = input.value.trim();
  if (!text) return;
  if (!app.currentUserId) {
    console.error('[AgentMgr] no userId');
    return;
  }

  const tagged = `[Agent Manager Request | Source: Agents Page]: ${text}`;

  let builderAgentId;
  try {
    builderAgentId = await _ensureAgentBuilderAgent(app.currentUserId);
  } catch (e) {
    console.error('[AgentMgr] _ensureAgentBuilderAgent failed:', e);
    app.addChatBubble('agent', '❌ Agent Manager unavailable: ' + (e.message || e), 'error');
    return;
  }

  if (typeof app.switchToAgent === 'function') {
    app.switchToAgent(builderAgentId);
  } else {
    app.currentAgentId = builderAgentId;
    try { localStorage.setItem('selectedAgentId', builderAgentId); } catch (_) {}
  }

  input.value = '';
  if (row) row.classList.remove('has-text');

  if (app.chatInput) {
    app.chatInput.value = tagged;
    app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    setTimeout(() => {
      if (app.chatSend) {
        app.chatSend.click();
      } else {
        // Direct fallback if the main chat send button is not wired
        fetch(apiPath('/api/v1/chat/send'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: tagged,
            session_id: app.currentSessionId,
            user_id: app.currentUserId,
            agent_id: builderAgentId,
          }),
        }).catch(err => console.error('[AgentMgr] direct send failed:', err));
      }
    }, 50);
  } else {
    // No main chat input at all — fire direct
    fetch(apiPath('/api/v1/chat/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: tagged,
        session_id: app.currentSessionId,
        user_id: app.currentUserId,
        agent_id: builderAgentId,
      }),
    }).catch(err => console.error('[AgentMgr] direct send (no chatInput) failed:', err));
  }
}

// CHAT-PILL-SYNC: binds the Agents-page chat pill. The same wiring pattern
// (has-text toggle, attach/voice forwarders, enter-to-send) lives in
// ui/js/chat.js (web chat) and ui/js/autoagent.js (Pages tab). Shared CSS
// for all three pills is in ui/css/app1.css under ".chat-pill".
let _agentBuilderBarBound = false;
function _bindAgentBuilderBar() {
  if (_agentBuilderBarBound) return;
  const row      = document.getElementById('agent-builder-bar-row');
  const input    = document.getElementById('agent-builder-bar-input');
  const attachBtn = document.getElementById('agent-builder-bar-attach');
  const voiceBtn = document.getElementById('agent-builder-bar-voice');
  const sendBtn  = document.getElementById('agent-builder-bar-send');
  if (!row || !input) return;
  _agentBuilderBarBound = true;

  // has-text class swaps the visible right-side button between voice (idle)
  // and send (typing). Mirrors #chat-input-row behavior.
  const sync = () => {
    const hasText = input.value.trim().length > 0;
    row.classList.toggle('has-text', hasText);
  };
  input.addEventListener('input', sync);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (input.value.trim().length > 0) _sendAgentBuilderPrompt();
    }
  });
  if (sendBtn) sendBtn.addEventListener('click', () => _sendAgentBuilderPrompt());

  // Forward attach and voice to the main chat composer's existing handlers.
  // We reuse them rather than duplicating the file-picker / recorder logic.
  if (attachBtn) {
    attachBtn.addEventListener('click', () => {
      const mainAttach = document.getElementById('chat-attach-btn');
      if (mainAttach) mainAttach.click();
    });
  }
  if (voiceBtn) {
    voiceBtn.addEventListener('click', () => {
      const mainVoice = document.getElementById('chat-voice-btn');
      if (mainVoice) mainVoice.click();
    });
  }

  // Paste images + drop files onto this pill. Uploads land in the main chat
  // preview bar, mirroring the attach-button forwarding above; the prompt
  // submits via the main chat send so attachment_ids ride along.
  wireChatPillUploads(row, input);

  sync();

  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try { window.lucide.createIcons(); } catch (_) {}
  }
}

function _renderPanelBody(agent, panelEl) {
  const state = _expandedAgents.get(agent.id);
  let tab = state?.tab || 'config';
  if (tab === 'automation' && !state?.automationEnabled) tab = 'config';

  // Sync tab-button active states (the tab carousel lives in the sibling card)
  const row = panelEl.closest('.agent-row');
  if (row) {
    row.querySelectorAll('.agent-card-tabs .agents-detail-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === tab);
    });
  }

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
  else if (tab === 'monetization') {
    // Defer to the billing module (loaded as a separate script).
    if (window.AppBilling && typeof window.AppBilling.renderMonetizationPanel === 'function') {
      window.AppBilling.renderMonetizationPanel(`agent:${agent.id}`, body, { agentId: agent.id });
    } else {
      body.innerHTML = '<div style="padding:12px;color:var(--fg-3);">Billing module not loaded.</div>';
    }
  }
}

// ── Automation tab ────────────────────────────────────────────────────────────

async function _renderAutomationTab(body, agent, panelEl) {
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to configure automation.</div>';
    return;
  }
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
  intro.innerHTML = 'Describe scheduled work AND event triggers in plain English (one trigger per paragraph). Examples: ' +
    '<em>"every weekday at 9am, send me a Telegram summary"</em> · ' +
    '<em>"when an email arrives from any airline, summarize it"</em>. ' +
    'Saving re-parses the file; both lists below update.';
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

  // ── Event triggers section ───────────────────────────────────────────
  const eventsHeader = document.createElement('div');
  eventsHeader.style.cssText = 'margin-top:22px;font-size:12px;font-weight:600;color:var(--fg-2);text-transform:uppercase;letter-spacing:0.5px;display:flex;align-items:center;justify-content:space-between;';
  const eventsHeaderLabel = document.createElement('span');
  eventsHeaderLabel.textContent = 'Event triggers';
  const eventsHeaderHint = document.createElement('span');
  eventsHeaderHint.style.cssText = 'font-size:10px;font-weight:400;text-transform:none;letter-spacing:0;color:var(--fg-3);';
  eventsHeaderHint.textContent = 'Push / poll triggers (Gmail, Slack, Calendar, …)';
  eventsHeader.appendChild(eventsHeaderLabel);
  eventsHeader.appendChild(eventsHeaderHint);
  body.appendChild(eventsHeader);

  const eventsList = document.createElement('div');
  eventsList.className = 'agents-automation-events';
  eventsList.style.cssText = 'display:flex;flex-direction:column;gap:8px;margin-top:8px;';
  body.appendChild(eventsList);

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

  function renderEvents(subs) {
    eventsList.innerHTML = '';
    if (!subs || !subs.length) {
      const empty = document.createElement('div');
      empty.style.cssText = 'font-size:12px;color:var(--fg-3);padding:8px;';
      empty.textContent = 'No event triggers yet. Try a line like "when an email arrives from any airline, summarize it".';
      eventsList.appendChild(empty);
      return;
    }
    for (const s of subs) {
      eventsList.appendChild(_renderEventTriggerRow(agent, s, availableChannels, () => loadEvents()));
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

  async function loadEvents() {
    try {
      const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/event-subscriptions?user_id=${encodeURIComponent(app.currentUserId)}`);
      if (r.ok) {
        const d = await r.json();
        renderEvents(d.subscriptions || []);
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
      if (Array.isArray(d.automation_event_subscriptions)) {
        renderEvents(d.automation_event_subscriptions);
      } else {
        await loadEvents();
      }
    } catch (e) {
      saveMsg.textContent = `Error: ${e.message}`;
      saveMsg.style.color = 'var(--danger)';
    }
  });

  // Expose a refresh hook so the per-user WebSocket can re-fetch tasks +
  // event subscriptions when an `automation_updated` event arrives
  // (e.g. the chat agent just called event_subscribe / event_unsubscribe).
  // No-op when this tab isn't mounted for the affected agent.
  app.refreshAutomationTab = (forAgentId) => {
    if (forAgentId && forAgentId !== agent.id) return;
    if (!body.isConnected) return;
    loadTasks();
    loadEvents();
  };

  await Promise.all([loadTasks(), loadEvents()]);
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

// ── Event trigger row (push / poll subscriptions) ─────────────────────────────

const _EVENT_HEALTH_STYLE = {
  ok:             { color: 'var(--success)', label: 'Active' },
  expiring_soon:  { color: '#d97706',        label: 'Expiring soon' },
  expired:        { color: 'var(--danger)',  label: 'Expired' },
  error:          { color: 'var(--danger)',  label: 'Error' },
  disabled:       { color: 'var(--fg-3)',    label: 'Disabled' },
};

function _renderEventTriggerRow(agent, sub, channels, onRefresh) {
  const row = document.createElement('div');
  row.className = 'agents-automation-row';
  row.style.cssText = 'border:1px solid var(--border);border-radius:6px;padding:10px 12px;background:var(--bg-elev);display:grid;grid-template-columns:1fr auto;gap:8px;';

  const left = document.createElement('div');
  left.style.cssText = 'display:flex;flex-direction:column;gap:4px;';

  // Title + health badge
  const titleRow = document.createElement('div');
  titleRow.style.cssText = 'display:flex;align-items:center;gap:8px;';
  const title = document.createElement('div');
  title.style.cssText = 'font-size:13px;font-weight:600;color:var(--fg-1);';
  title.textContent = sub.task_label || '(unlabeled)';
  titleRow.appendChild(title);

  const health = _EVENT_HEALTH_STYLE[sub.health] || _EVENT_HEALTH_STYLE.ok;
  const healthBadge = document.createElement('span');
  healthBadge.style.cssText = `font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:${health.color};border:1px solid ${health.color};border-radius:3px;padding:1px 6px;background:transparent;`;
  healthBadge.textContent = health.label;
  titleRow.appendChild(healthBadge);
  left.appendChild(titleRow);

  // Source + event_type
  const trigger = document.createElement('div');
  trigger.style.cssText = 'font-size:11px;color:var(--accent);font-family:monospace;';
  trigger.textContent = `${sub.source}.${sub.event_type}`;
  left.appendChild(trigger);

  // Filter summary (compact)
  const filterDict = sub.filter || {};
  const filterKeys = Object.keys(filterDict);
  if (filterKeys.length) {
    const filt = document.createElement('div');
    filt.style.cssText = 'font-size:11px;color:var(--fg-3);font-family:monospace;';
    filt.textContent = 'filter: ' + filterKeys.map(k => `${k}=${JSON.stringify(filterDict[k])}`).join(', ');
    left.appendChild(filt);
  }

  // Meta line: last event, fire count, expiration
  const meta = document.createElement('div');
  meta.style.cssText = 'font-size:11px;color:var(--fg-3);';
  const parts = [];
  parts.push(sub.last_event_at ? `last fired: ${new Date(sub.last_event_at).toLocaleString()}` : 'never fired');
  if (sub.fire_count) parts.push(`${sub.fire_count} fires`);
  if (sub.external_expiration_at) {
    parts.push(`watch expires: ${new Date(sub.external_expiration_at).toLocaleString()}`);
  } else if (sub.last_polled_at) {
    parts.push(`polled: ${new Date(sub.last_polled_at).toLocaleString()}`);
  }
  meta.textContent = parts.join(' · ');
  left.appendChild(meta);

  if (sub.last_error) {
    const err = document.createElement('div');
    err.style.cssText = 'font-size:11px;color:var(--danger);';
    err.textContent = `Error: ${sub.last_error}`;
    left.appendChild(err);
  }

  // Original English phrase (if available)
  if (sub.trigger_natural) {
    const tn = document.createElement('div');
    tn.style.cssText = 'font-size:11px;color:var(--fg-2);font-style:italic;margin-top:2px;white-space:pre-wrap;';
    tn.textContent = `> ${sub.trigger_natural}`;
    left.appendChild(tn);
  }

  // Prompt preview (collapsed)
  const promptPreview = document.createElement('div');
  promptPreview.style.cssText = 'font-size:11px;color:var(--fg-2);margin-top:4px;white-space:pre-wrap;';
  promptPreview.textContent = sub.prompt ? `prompt: ${sub.prompt}` : '';
  left.appendChild(promptPreview);

  row.appendChild(left);

  // ── Right column: controls ───────────────────────────────────────────
  const right = document.createElement('div');
  right.style.cssText = 'display:flex;flex-direction:column;gap:6px;align-items:flex-end;min-width:180px;';

  const enableLbl = document.createElement('label');
  enableLbl.style.cssText = 'display:flex;align-items:center;gap:5px;font-size:11px;color:var(--fg-2);cursor:pointer;';
  const enableCb = document.createElement('input');
  enableCb.type = 'checkbox';
  enableCb.checked = !!sub.enabled;
  enableLbl.appendChild(enableCb);
  enableLbl.appendChild(document.createTextNode('Enabled'));
  right.appendChild(enableLbl);

  const silentLbl = document.createElement('label');
  silentLbl.style.cssText = 'display:flex;align-items:center;gap:5px;font-size:11px;color:var(--fg-2);cursor:pointer;';
  const silentCb = document.createElement('input');
  silentCb.type = 'checkbox';
  silentCb.checked = !!sub.silent;
  silentLbl.appendChild(silentCb);
  silentLbl.appendChild(document.createTextNode('Silent'));
  right.appendChild(silentLbl);

  // Channel: a blank entry meaning "ask the user at fire time" (per design)
  const chanWrap = document.createElement('div');
  chanWrap.style.cssText = 'display:flex;flex-direction:column;gap:2px;align-items:flex-end;';
  const chanSel = document.createElement('select');
  chanSel.className = 'agents-input';
  chanSel.style.cssText = 'font-size:11px;padding:3px 6px;min-width:150px;';
  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = '— ask the user —';
  chanSel.appendChild(blank);
  for (const c of channels) {
    const opt = document.createElement('option');
    opt.value = c;
    opt.textContent = c;
    if (sub.channel === c) opt.selected = true;
    chanSel.appendChild(opt);
  }
  chanWrap.appendChild(chanSel);

  const recipInput = document.createElement('input');
  recipInput.type = 'text';
  recipInput.className = 'agents-input';
  recipInput.placeholder = 'recipient (optional)';
  recipInput.style.cssText = 'font-size:11px;padding:3px 6px;min-width:150px;';
  recipInput.value = sub.channel_recipient || '';
  chanWrap.appendChild(recipInput);
  right.appendChild(chanWrap);

  const btnRow1 = document.createElement('div');
  btnRow1.style.cssText = 'display:flex;gap:5px;margin-top:4px;';
  const saveBtn = document.createElement('button');
  saveBtn.className = 'agents-btn';
  saveBtn.textContent = 'Save';
  saveBtn.style.cssText = 'font-size:11px;padding:3px 10px;';
  const testBtn = document.createElement('button');
  testBtn.className = 'agents-btn';
  testBtn.textContent = 'Test fire';
  testBtn.style.cssText = 'font-size:11px;padding:3px 10px;';
  btnRow1.appendChild(saveBtn);
  btnRow1.appendChild(testBtn);
  right.appendChild(btnRow1);

  const btnRow2 = document.createElement('div');
  btnRow2.style.cssText = 'display:flex;gap:5px;';
  const reRegBtn = document.createElement('button');
  reRegBtn.className = 'agents-btn';
  reRegBtn.textContent = 'Re-register';
  reRegBtn.style.cssText = 'font-size:11px;padding:3px 10px;';
  reRegBtn.title = 'Re-run register_subscription at the provider (use after a Pub/Sub topic recreate, OAuth reconnect, or to clear an error state)';
  const delBtn = document.createElement('button');
  delBtn.className = 'agents-btn';
  delBtn.textContent = 'Delete';
  delBtn.style.cssText = 'font-size:11px;padding:3px 10px;color:var(--danger);';
  btnRow2.appendChild(reRegBtn);
  btnRow2.appendChild(delBtn);
  right.appendChild(btnRow2);

  // ── Wire actions ─────────────────────────────────────────────────────
  saveBtn.addEventListener('click', async () => {
    saveBtn.textContent = '…';
    try {
      await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/event-subscriptions/${encodeURIComponent(sub.id)}`, {
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

  testBtn.addEventListener('click', async () => {
    testBtn.textContent = 'Firing…';
    testBtn.disabled = true;
    try {
      const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/event-subscriptions/${encodeURIComponent(sub.id)}/test-fire?user_id=${encodeURIComponent(app.currentUserId)}`, {
        method: 'POST',
      });
      const d = await r.json();
      testBtn.textContent = (d?.result?.ok === false) ? 'Error' : 'Done';
      setTimeout(() => { testBtn.textContent = 'Test fire'; testBtn.disabled = false; }, 1500);
      onRefresh && onRefresh();
    } catch (e) {
      testBtn.textContent = 'Err';
      testBtn.disabled = false;
    }
  });

  reRegBtn.addEventListener('click', async () => {
    reRegBtn.textContent = '…';
    reRegBtn.disabled = true;
    try {
      const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/event-subscriptions/${encodeURIComponent(sub.id)}/re-register?user_id=${encodeURIComponent(app.currentUserId)}`, {
        method: 'POST',
      });
      if (r.ok) {
        reRegBtn.textContent = '✓';
      } else {
        const d = await r.json().catch(() => ({}));
        reRegBtn.textContent = 'Err';
        reRegBtn.title = d.detail || 'register failed';
      }
      setTimeout(() => { reRegBtn.textContent = 'Re-register'; reRegBtn.disabled = false; }, 1800);
      onRefresh && onRefresh();
    } catch (e) {
      reRegBtn.textContent = 'Err';
      reRegBtn.disabled = false;
    }
  });

  delBtn.addEventListener('click', async () => {
    if (!confirm(`Delete event trigger "${sub.task_label || sub.source}"?\n\nThe provider-side watch will be stopped. The English line in your Automation file is left in place — a future save will re-create the trigger unless you remove the line.`)) return;
    delBtn.textContent = '…';
    delBtn.disabled = true;
    try {
      const r = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/event-subscriptions/${encodeURIComponent(sub.id)}?user_id=${encodeURIComponent(app.currentUserId)}`, {
        method: 'DELETE',
      });
      if (r.ok) {
        row.remove();
        onRefresh && onRefresh();
      } else {
        delBtn.textContent = 'Err';
        delBtn.disabled = false;
      }
    } catch (e) {
      delBtn.textContent = 'Err';
      delBtn.disabled = false;
    }
  });

  row.appendChild(right);
  return row;
}

// ── Suggested-Replies mode control (user-impersonator config) ───────────────
// Reads/writes the silent suggestion engine's runtime config so the user can
// switch between Off / On / On + idle refresh and pick how many chips to show.
function _renderSuggestionModeControl(body) {
  const group = document.createElement('div');
  group.className = 'agents-field-group';

  const intro = document.createElement('div');
  intro.className = 'agents-field-label';
  intro.textContent = 'Suggested replies';
  group.appendChild(intro);

  const hint = document.createElement('div');
  hint.style.cssText = 'font-size:12px;color:var(--fg-4);margin:-2px 0 8px;';
  hint.textContent = 'Grey suggestion chips above the chat box, written in your voice.';
  group.appendChild(hint);

  // Mode selector
  const modeWrap = document.createElement('label');
  modeWrap.style.cssText = 'display:block;font-size:12px;color:var(--fg-3);margin-bottom:8px;';
  modeWrap.textContent = 'When to suggest';
  const modeSel = document.createElement('select');
  modeSel.className = 'agents-input';
  [['on', 'On — after each reply'],
   ['scheduler', 'On + refresh while idle'],
   ['off', 'Off']].forEach(([v, t]) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = t; modeSel.appendChild(o);
  });
  modeWrap.appendChild(modeSel);
  group.appendChild(modeWrap);

  // Count selector
  const countWrap = document.createElement('label');
  countWrap.style.cssText = 'display:block;font-size:12px;color:var(--fg-3);margin-bottom:8px;';
  countWrap.textContent = 'How many suggestions';
  const countSel = document.createElement('select');
  countSel.className = 'agents-input';
  [1, 2, 3, 4, 5].forEach(n => {
    const o = document.createElement('option');
    o.value = String(n); o.textContent = String(n); countSel.appendChild(o);
  });
  countWrap.appendChild(countSel);
  group.appendChild(countWrap);

  const status = document.createElement('div');
  status.style.cssText = 'font-size:11px;color:var(--fg-4);min-height:14px;';
  group.appendChild(status);

  body.appendChild(group);

  // Load current values.
  fetch('/api/v1/chat/suggestions/config')
    .then(r => r.ok ? r.json() : null)
    .then(cfg => {
      if (!cfg) return;
      if (cfg.mode) modeSel.value = cfg.mode;
      if (cfg.count) countSel.value = String(cfg.count);
    })
    .catch(() => {});

  async function _save() {
    status.textContent = 'Saving…';
    try {
      const res = await fetch('/api/v1/chat/suggestions/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: modeSel.value, count: parseInt(countSel.value, 10) }),
      });
      status.textContent = res.ok ? 'Saved.' : 'Save failed.';
    } catch (_) {
      status.textContent = 'Save failed.';
    }
    setTimeout(() => { status.textContent = ''; }, 2000);
  }
  modeSel.addEventListener('change', _save);
  countSel.addEventListener('change', _save);
}

// ── Config tab ────────────────────────────────────────────────────────────────

function _renderConfigTab(body, agent, panelEl) {
  const isEditable = agent.source === 'custom';
  const isMock = _isMockAgent(agent);

  // The Suggested Replies (user-impersonator) agent powers the grey suggestion
  // chips above the chat pill. Its behaviour is tuned by a small runtime config
  // (mode + count), not the normal agent-row fields — so give it a dedicated
  // control here.
  if (agent.id === 'user-impersonator') {
    _renderSuggestionModeControl(body);
  }

  // Name + description (editable for custom agents only)
  if (isEditable) {
    _addField(body, 'Name', 'agents-input', 'name',
      agent.name || '', false);
    _addField(body, 'Description', 'agents-textarea', 'desc',
      agent.description || '', false, 2);
  }

  // ── Template selector (mock agent only) ────────────────────────────────────
  if (isMock) {
    const tplGroup = document.createElement('div');
    tplGroup.className = 'agents-field-group';
    const tplLabel = document.createElement('label');
    tplLabel.className = 'agents-field-label';
    tplLabel.textContent = 'Template';
    const tplSelect = document.createElement('select');
    tplSelect.className = 'agents-input';
    tplSelect.dataset.field = 'template';
    tplSelect.innerHTML = '<option value="">— No template —</option>';
    tplGroup.appendChild(tplLabel);
    tplGroup.appendChild(tplSelect);
    body.appendChild(tplGroup);

    // Fetch templates
    (async () => {
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
        console.warn('agents: failed to load templates for mock', e);
      }
    })();
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

  // Turn count + wall clock (side-by-side)
  const limitsRow = document.createElement('div');
  limitsRow.style = 'display:flex;gap:16px;flex-wrap:wrap;';

  // Turn count
  const tcGroup = document.createElement('div');
  tcGroup.className = 'agents-field-group';
  tcGroup.style = 'flex:1;min-width:200px;';
  const tcVal = agent.max_turn_count != null ? agent.max_turn_count : 9999;
  tcGroup.innerHTML = `
    <label class="agents-field-label">Max Turn Count <span style="font-weight:normal;color:var(--fg-3);">(0 = unlimited)</span></label>
    <span class="agents-field-hint">Caps the LLM → tool → LLM cycles per response — catches loops where the agent repeats tool calls without resolving. 0 = unlimited (a wall-clock safety limit still ends a stuck run gracefully). New agents start at 9999 — effectively unlimited but bounded.</span>
    <input type="number" class="agents-input" data-field="max_turn_count"
      value="${tcVal}" min="0" max="99999"
      ${!isEditable ? 'readonly' : ''} style="width:100px" placeholder="9999">
  `;
  limitsRow.appendChild(tcGroup);

  // Wall clock safety cap
  const wcGroup = document.createElement('div');
  wcGroup.className = 'agents-field-group';
  wcGroup.style = 'flex:1;min-width:200px;';
  const wcVal = agent.max_wall_seconds != null ? agent.max_wall_seconds : '';
  wcGroup.innerHTML = `
    <label class="agents-field-label">Wall Clock Safety Cap (seconds) <span style="font-weight:normal;color:var(--fg-muted,#565f89);">(0 = off)</span></label>
    <span class="agents-field-hint">Limits total real time (in seconds) for one response, across all turns. Catches hanging tool calls, slow models, and long-running operations. Wall clock is about <em>real-world duration</em>. Set e.g. 300 for 5 min.</span>
    <input type="number" class="agents-input" data-field="max_wall_seconds"
      value="${wcVal}" min="0" max="86400" step="1"
      ${!isEditable ? 'readonly' : ''} style="width:100px" placeholder="0 (off)">
  `;
  limitsRow.appendChild(wcGroup);

  // Max identical tool calls (stall guard)
  const icGroup = document.createElement('div');
  icGroup.className = 'agents-field-group';
  icGroup.style = 'flex:1;min-width:200px;';
  const icVal = agent.max_identical_tool_calls != null ? agent.max_identical_tool_calls : '';
  icGroup.innerHTML = `
    <label class="agents-field-label">Max Identical Tool Calls <span style="font-weight:normal;color:var(--fg-3);">(0 = off)</span></label>
    <span class="agents-field-hint">Limits how many times the agent can call the <strong>same tool with the same arguments</strong>. Prevents infinite loops (e.g. running the same search 20 times). Also limits how many consecutive calls to the same tool (different args) are allowed. Set to 0 for no limit.</span>
    <input type="number" class="agents-input" data-field="max_identical_tool_calls"
      value="${icVal}" min="0" max="9999" step="1"
      ${!isEditable ? 'readonly' : ''} style="width:100px" placeholder="0 (off)">
  `;
  limitsRow.appendChild(icGroup);

  // Max stall strikes (loop strikes before hard stop)
  const ssGroup = document.createElement('div');
  ssGroup.className = 'agents-field-group';
  ssGroup.style = 'flex:1;min-width:200px;';
  const ssVal = agent.max_stall_strikes != null ? agent.max_stall_strikes : '';
  ssGroup.innerHTML = `
    <label class="agents-field-label">Max Stall Strikes <span style="font-weight:normal;color:var(--fg-3);">(0 = off)</span></label>
    <span class="agents-field-hint">After this many stall guard strikes (tool-call loop detections), the agent stops and asks the user for clarification. Set to 0 for no limit — the agent can keep looping as long as max-turn-count allows.</span>
    <input type="number" class="agents-input" data-field="max_stall_strikes"
      value="${ssVal}" min="0" max="99" step="1"
      ${!isEditable ? 'readonly' : ''} style="width:100px" placeholder="0 (off)">
  `;
  limitsRow.appendChild(ssGroup);

  body.appendChild(limitsRow);

  // ── Memory (per-agent save + recall switches) ───────────────────────────────
  // Sits with the Max Turn Count / Wall Clock limits. Surfaces the same two
  // controls the Agent Loop diagram exposes (memory_search / memory_save); both
  // switches drive loop_logic + allowed_tools so the Config tab and the loop
  // diagram always agree.
  if (isEditable) {
    const mem = _memoryStateFromAgent(agent);
    const memGroup = document.createElement('div');
    memGroup.className = 'agents-field-group';
    memGroup.innerHTML = `
      <label class="agents-field-label">Memory</label>
      <span class="agents-field-hint">This agent's long-term memory — a private knowledge store kept per user and shared across that user's agents. When on, the agent automatically pulls relevant past information into its context before replying, and/or files a short note of each exchange afterward. Trivial messages (greetings, "ok") skip recall automatically.</span>
      <label style="display:flex;align-items:flex-start;gap:8px;cursor:pointer;margin-top:8px;">
        <input type="checkbox" data-field="memory_recall" ${mem.recall ? 'checked' : ''} style="margin-top:2px;">
        <span style="font-size:13px;color:var(--fg-2);"><strong>Recall past info</strong> — search memory before answering, and let the agent read or write it on demand.</span>
      </label>
      <label style="display:flex;align-items:flex-start;gap:8px;cursor:pointer;margin-top:8px;">
        <input type="checkbox" data-field="memory_save" ${mem.save ? 'checked' : ''} style="margin-top:2px;">
        <span style="font-size:13px;color:var(--fg-2);"><strong>Remember conversations</strong> — automatically save a short note of each exchange afterward.</span>
      </label>
    `;
    body.appendChild(memGroup);
  }

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
  if (isEditable && !isMock) {
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
  // Skip for mock agent — the create button in the panel header handles saving.
  if (isEditable && !isMock) {
    const content = panelEl.querySelector('.agent-detail-content');
    const bar = document.createElement('div');
    bar.className = 'agents-save-bar';
    const saveBtn = _btn('Save Changes', 'agents-btn primary');
    const msg = document.createElement('span');
    msg.className = 'agents-save-msg';
    saveBtn.addEventListener('click', () => _saveChanges(agent, bar, panelEl));
    bar.appendChild(saveBtn);
    if (_userIsAdmin) {
      const tplBtn = _btn('Save as Template', 'agents-btn');
      tplBtn.title = 'Save this agent\'s config and prompts as a reusable template (admin only).';
      tplBtn.addEventListener('click', () => _openSaveAsTemplateModal(agent, bar));
      bar.appendChild(tplBtn);
    }
    bar.appendChild(msg);
    if (content) content.appendChild(bar);
  }
}

// ── Save-as-template modal ────────────────────────────────────────────────────

function _openSaveAsTemplateModal(agent, hostBar) {
  const existing = document.getElementById('agents-save-as-template-modal');
  if (existing) existing.remove();

  const backdrop = document.createElement('div');
  backdrop.id = 'agents-save-as-template-modal';
  backdrop.style.cssText =
    'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9000;' +
    'display:flex;align-items:center;justify-content:center;';

  const panel = document.createElement('div');
  panel.style.cssText =
    'background:var(--bg-elev);color:var(--fg-1);' +
    'border:1px solid var(--border);border-radius:8px;' +
    'padding:18px 20px;width:420px;max-width:92vw;box-shadow:0 8px 24px rgba(0,0,0,0.4);';

  const defaultSlug = (agent.name || agent.id || 'template')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || 'template';

  panel.innerHTML = `
    <div style="font-size:15px;font-weight:600;margin-bottom:4px;">Save as Template</div>
    <div style="font-size:12px;color:var(--fg-3);margin-bottom:14px;">
      Snapshots this agent's config and admin-base prompts into a reusable template.
      Future new agents can be created from it.
    </div>
    <label style="display:block;font-size:12px;color:var(--fg-2);margin-bottom:2px;">Template ID (slug)</label>
    <input id="sat-tpl-id" type="text" class="agents-input" style="width:100%;margin-bottom:10px;"
      value="${_esc(defaultSlug)}" placeholder="my_template">
    <label style="display:block;font-size:12px;color:var(--fg-2);margin-bottom:2px;">Name</label>
    <input id="sat-tpl-name" type="text" class="agents-input" style="width:100%;margin-bottom:10px;"
      value="${_esc(agent.name || '')}" placeholder="Display name">
    <label style="display:block;font-size:12px;color:var(--fg-2);margin-bottom:2px;">Description</label>
    <textarea id="sat-tpl-desc" class="agents-textarea" rows="2" style="width:100%;margin-bottom:10px;"
      placeholder="Short description shown in the template list">${_esc(agent.description || '')}</textarea>
    <label style="display:block;font-size:12px;color:var(--fg-2);margin-bottom:2px;">Icon (emoji, optional)</label>
    <input id="sat-tpl-icon" type="text" class="agents-input" style="width:100px;margin-bottom:12px;"
      value="${_esc(agent.icon || '')}" placeholder="🤖" maxlength="4">
    <label style="display:flex;align-items:center;gap:8px;font-size:13px;color:var(--fg-2);margin-bottom:6px;cursor:pointer;">
      <input id="sat-tpl-discoverable" type="checkbox">
      Discoverable in the "New Agent" menu
    </label>
    <div id="sat-tpl-msg" style="font-size:12px;color:var(--danger,#f7768e);min-height:16px;margin-top:8px;"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
      <button id="sat-tpl-cancel" class="agents-btn">Cancel</button>
      <button id="sat-tpl-save" class="agents-btn primary">Save Template</button>
    </div>
  `;
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);

  const slugIn = panel.querySelector('#sat-tpl-id');
  const nameIn = panel.querySelector('#sat-tpl-name');
  const descIn = panel.querySelector('#sat-tpl-desc');
  const iconIn = panel.querySelector('#sat-tpl-icon');
  const discCb = panel.querySelector('#sat-tpl-discoverable');
  const msgEl  = panel.querySelector('#sat-tpl-msg');
  const saveBtn = panel.querySelector('#sat-tpl-save');
  const cancelBtn = panel.querySelector('#sat-tpl-cancel');

  function _close() { backdrop.remove(); }
  cancelBtn.addEventListener('click', _close);
  backdrop.addEventListener('click', e => { if (e.target === backdrop) _close(); });

  saveBtn.addEventListener('click', async () => {
    msgEl.textContent = '';
    msgEl.style.color = 'var(--danger,#f7768e)';
    const slug = (slugIn.value || '').trim().toLowerCase();
    const name = (nameIn.value || '').trim();
    if (!slug || !/^[a-z0-9][a-z0-9_-]{1,63}$/.test(slug)) {
      msgEl.textContent = 'Template ID must be 2-64 chars: lowercase letters, digits, "_" or "-".';
      return;
    }
    if (!name) {
      msgEl.textContent = 'Template name is required.';
      return;
    }
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/save-as-template`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: app.currentUserId,
          template_id: slug,
          name,
          description: (descIn.value || '').trim(),
          icon: (iconIn.value || '').trim(),
          discoverable: !!discCb.checked,
          access_level: 'all',
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        msgEl.textContent = data.detail || `Save failed (HTTP ${res.status}).`;
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save Template';
        return;
      }
      msgEl.style.color = 'var(--success,#9ece6a)';
      msgEl.textContent = '✓ Template saved.';
      if (hostBar) {
        const sm = hostBar.querySelector('.agents-save-msg');
        if (sm) { sm.textContent = `✓ Saved as template "${slug}"`; sm.className = 'agents-save-msg'; }
      }
      setTimeout(() => {
        _close();
        // Refresh the agents list so the new template surfaces.
        _loadAgents().then(_renderList).catch(() => {});
      }, 700);
    } catch (e) {
      msgEl.textContent = `Error: ${e.message}`;
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save Template';
    }
  });

  slugIn.focus();
  slugIn.select();
}

async function _loadAndRenderSlots(panelEl, agent, _isEditable) {
  const listEl = panelEl.querySelector('[data-role="slots-list"]');
  if (!listEl) return;

  // Mock agent: show placeholder
  if (_isMockAgent(agent)) {
    listEl.innerHTML = '<div style="font-size:12px;color:var(--fg-muted,#565f89);padding:12px;text-align:center;">Save this agent first to configure prompt slots.</div>';
    return;
  }

  listEl.innerHTML = '<div style="font-size:12px;color:var(--fg-muted,#565f89);">Loading slots…</div>';
  let data = null;
  try {
    const token = localStorage.getItem('auth_token');
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const res = await fetch(
      `/api/v1/agents/${encodeURIComponent(agent.id)}/slots?user_id=${encodeURIComponent(app.currentUserId)}`,
      { headers }
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
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to configure tools.</div>';
    return;
  }
  const isEditable = agent.source === 'custom';

  // ── Fetch enabled abilities to include ability-gated tools ──────────────
  let enabledAbilities = new Set();
  try {
    const connRes = await fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`);
    if (connRes.ok) {
      const connData = await connRes.json();
      for (const c of (connData.connections || [])) {
        if (c.enabled && c.section === 'ability') {
          enabledAbilities.add(c.connection_type);
        }
      }
    }
  } catch (e) {
    // non-fatal — proceed without ability-gated tools
  }

  // ── Non-editable: read-only tool list ────────────────────────────────────
  if (!isEditable) {
    const tools = _toolsForAgent(agent, enabledAbilities);
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
      // Check category condition: null=always, 'admin'=admin/optimizer only,
      // otherwise it's an ability name that must be enabled
      if (cat.condition === 'admin' && !isAdmin) continue;
      if (cat.condition && cat.condition !== 'admin' && !enabledAbilities.has(cat.condition)) continue;
      const catTools = cat.tools.filter(n => toolSet.has(n));
      if (catTools.length === 0) continue;

      const wrapper = document.createElement('div');
      wrapper.className = 'agents-tool-category';

      const header = document.createElement('div');
      header.className = 'agents-tool-category-header collapsed';
      header.innerHTML = `<span class="agents-tool-category-chevron">▶</span>
        <span class="agents-tool-category-label">${_esc(cat.label)}</span>
        <span class="agents-tool-category-count">${catTools.length}</span>`;

      const catBody = document.createElement('div');
      catBody.className = 'agents-tool-category-body collapsed';

      header.addEventListener('click', () => {
        header.classList.toggle('collapsed');
        catBody.classList.toggle('collapsed');
        const chevron = header.querySelector('.agents-tool-category-chevron');
        if (chevron) chevron.textContent = header.classList.contains('collapsed') ? '▶' : '▼';
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
      header.className = 'agents-tool-category-header collapsed';
      header.innerHTML = `<span class="agents-tool-category-chevron">▶</span>
        <span class="agents-tool-category-label">Custom Skills</span>
        <span class="agents-tool-category-count">${uncategorized.length}</span>`;
      const catBody = document.createElement('div');
      catBody.className = 'agents-tool-category-body collapsed';
      header.addEventListener('click', () => {
        header.classList.toggle('collapsed');
        catBody.classList.toggle('collapsed');
        const chevron = header.querySelector('.agents-tool-category-chevron');
        if (chevron) chevron.textContent = header.classList.contains('collapsed') ? '▶' : '▼';
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
    const tools      = _toolsForAgent(agent, enabledAbilities);
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

    // Render categories as collapsible accordions (collapsed by default)
    const categorized = new Set(TOOL_CATEGORIES.flatMap(c => c.tools));

    for (const cat of TOOL_CATEGORIES) {
      // Check category condition: null=always, 'admin'=admin/optimizer only,
      // otherwise it's an ability name that must be enabled
      if (cat.condition === 'admin' && !isAdmin) continue;
      if (cat.condition && cat.condition !== 'admin' && !enabledAbilities.has(cat.condition)) continue;
      const catTools = cat.tools.filter(n => toolSet.has(n));
      if (catTools.length === 0) continue;

      const wrapper = document.createElement('div');
      wrapper.className = 'agents-tool-category';

      const header = document.createElement('div');
      header.className = 'agents-tool-category-header collapsed';
      header.innerHTML = `<span class="agents-tool-category-chevron">▶</span>
        <span class="agents-tool-category-label">${_esc(cat.label)}</span>
        <span class="agents-tool-category-count">${catTools.length}</span>`;

      const catBody = document.createElement('div');
      catBody.className = 'agents-tool-category-body collapsed';

      header.addEventListener('click', () => {
        header.classList.toggle('collapsed');
        catBody.classList.toggle('collapsed');
        const chevron = header.querySelector('.agents-tool-category-chevron');
        if (chevron) chevron.textContent = header.classList.contains('collapsed') ? '▶' : '▼';
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
      header.className = 'agents-tool-category-header collapsed';
      header.innerHTML = `<span class="agents-tool-category-chevron">▶</span>
        <span class="agents-tool-category-label">Custom Skills</span>
        <span class="agents-tool-category-count">${uncategorized.length}</span>`;
      const catBody = document.createElement('div');
      catBody.className = 'agents-tool-category-body collapsed';
      header.addEventListener('click', () => {
        header.classList.toggle('collapsed');
        catBody.classList.toggle('collapsed');
        const chevron = header.querySelector('.agents-tool-category-chevron');
        if (chevron) chevron.textContent = header.classList.contains('collapsed') ? '▶' : '▼';
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
  scraper:         'globe',
  browser_session: 'cookie',
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
  // Agent Tools
  codebase_admin:   'folder-cog',
  create_tools:     'wrench',
  automation:       'clock',
  web_access:       'globe',
  browser_control:  'mouse-pointer-2',
  image_generation: 'image',
  visualizer:       'layout-dashboard',
  agent_orchestration: 'workflow',
  diagnostics:      'stethoscope',
};

async function _renderConnectionsTab(body, agent) {
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to configure abilities.</div>';
    return;
  }
  body.innerHTML = '<div class="conn-loading">Loading abilities…</div>';

  let connections = [];
  let userRole = 'member';
  let abilitiesByProvider = {};
  try {
    // Fetch connections + abilities in parallel — both are required for the
    // OAuth cards to render their nested per-ability toggles.
    const [connRes, abilRes] = await Promise.all([
      fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`),
      fetch(`/api/v1/agents/${agent.id}/abilities?user_id=${encodeURIComponent(app.currentUserId)}`).catch(() => null),
    ]);
    if (connRes.ok) {
      const data = await connRes.json();
      connections = data.connections || [];
      userRole = data.user_role || 'member';
    }
    if (abilRes && abilRes.ok) {
      const data = await abilRes.json();
      for (const ab of (data.abilities || [])) {
        const list = abilitiesByProvider[ab.provider] || (abilitiesByProvider[ab.provider] = []);
        list.push(ab);
      }
    }
    // Attach the per-provider ability list onto each matching connection so
    // _buildConnectionBody can render the toggles without re-fetching.
    for (const c of connections) {
      // Meta covers both facebook & instagram; aliases share the same scopes.
      const provider = c.connection_type === 'facebook' || c.connection_type === 'instagram'
        ? 'meta' : c.connection_type;
      c._abilities = abilitiesByProvider[provider] || [];
    }
  } catch (e) {
    body.innerHTML = `<div class="conn-loading" style="color:#f7768e">Failed to load connections: ${_esc(e.message)}</div>`;
    return;
  }

  const canEdit = (userRole === 'admin');

  body.innerHTML = '';

  // Slot for transient banners (e.g. "Automation tab is now available").
  const noticeSlot = document.createElement('div');
  noticeSlot.className = 'conn-notice-slot';
  body.appendChild(noticeSlot);

  if (!canEdit) {
    const notice = document.createElement('div');
    notice.style.cssText = 'font-size:11px;color:#565f89;padding:8px 12px;background:#1a1b26;border:1px solid #2a2a4a;border-radius:6px;margin-bottom:12px;line-height:1.5;';
    notice.textContent = 'Integration toggles are managed by agent admins. You can connect your accounts on enabled integrations.';
    body.appendChild(notice);
  }

  // Search field — filters cards by display name / connection type / section label.
  const searchWrap = document.createElement('div');
  searchWrap.className = 'conn-search-wrap';
  const searchInput = document.createElement('input');
  searchInput.type = 'search';
  searchInput.className = 'conn-search-input agents-input';
  searchInput.placeholder = 'Search abilities…';
  searchInput.autocomplete = 'off';
  const searchEmpty = document.createElement('div');
  searchEmpty.className = 'conn-search-empty';
  searchEmpty.textContent = 'No abilities match your search.';
  searchEmpty.style.display = 'none';
  searchWrap.appendChild(searchInput);
  body.appendChild(searchWrap);
  body.appendChild(searchEmpty);

  const sections = [
    { key: 'ability',     label: 'Agent Tools',  hint: 'Privileged capabilities — web access, browser control, image gen, codebase edits, tool creation, automation.' },
    { key: 'channel',     label: 'Channels',     hint: 'How this agent sends and receives messages.' },
    { key: 'integration', label: 'Integrations', hint: 'Services and data sources this agent can access.' },
    { key: 'social',      label: 'Social Media', hint: 'Social platforms this agent can post and interact on.' },
    { key: 'marketplace', label: 'Marketplaces', hint: 'E-commerce platforms this agent can list, browse, and manage items on.' },
  ];

  // Track cards + their haystack for search filtering, plus their section
  // element so we can hide section headers when every card inside is filtered out.
  const indexed = [];
  const sectionEls = [];

  for (const sec of sections) {
    const items = connections.filter(c => c.section === sec.key);
    if (!items.length) continue;

    const secEl = document.createElement('div');
    secEl.className = 'conn-section';

    const header = document.createElement('div');
    header.className = 'conn-section-header conn-section-header-static';
    header.innerHTML = `
      <span class="conn-section-label">${_esc(sec.label)}</span>
      <span class="conn-section-hint">${_esc(sec.hint)}</span>
    `;

    const grid = document.createElement('div');
    grid.className = 'conn-grid';

    for (const conn of items) {
      const card = _buildConnectionCard(agent, conn, canEdit);
      const haystack = (
        (conn.display_name || '') + ' ' +
        (conn.connection_type || '') + ' ' +
        sec.label
      ).toLowerCase();
      indexed.push({ card, haystack });
      grid.appendChild(card);
    }

    secEl.appendChild(header);
    secEl.appendChild(grid);
    body.appendChild(secEl);
    sectionEls.push({ secEl, grid });
  }

  const applySearch = () => {
    const q = searchInput.value.trim().toLowerCase();
    let visible = 0;
    for (const { card, haystack } of indexed) {
      const match = !q || haystack.includes(q);
      card.style.display = match ? '' : 'none';
      if (match) visible++;
    }
    // Hide section wrappers whose grids have no visible cards left.
    for (const { secEl, grid } of sectionEls) {
      const anyVisible = Array.from(grid.children).some(c => c.style.display !== 'none');
      secEl.style.display = anyVisible ? '' : 'none';
    }
    searchEmpty.style.display = (q && visible === 0) ? 'block' : 'none';
  };
  searchInput.addEventListener('input', applySearch);

  // Populate Telegram mode badge and wire Test button
  _loadTelegramCardStatus(body);
}

function _showAbilitiesNotice(agent, text) {
  const panel = document.querySelector(`.agent-detail-panel[data-agent-id="${agent.id}"]`);
  if (!panel) return;
  const slot = panel.querySelector('.conn-notice-slot');
  if (!slot) return;
  slot.innerHTML = '';
  const banner = document.createElement('div');
  banner.className = 'conn-banner-notice';
  banner.textContent = text;
  slot.appendChild(banner);
  setTimeout(() => {
    banner.classList.add('fade-out');
    setTimeout(() => { if (banner.parentNode) banner.remove(); }, 400);
  }, 4000);
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

  // Per-ability toggles inside OAuth cards (Phase 2: 3-tier OAuth system).
  card.querySelectorAll('.conn-ability-row').forEach(row => {
    const toggle = row.querySelector('.conn-ability-toggle');
    if (!toggle) return;
    toggle.addEventListener('change', async () => {
      const abilityId = row.dataset.abilityId;
      // Find the matching ability object to pull its current `source`.
      const ab = (conn._abilities || []).find(a => a.id === abilityId) || {};
      await _onAbilityToggle(agent, conn, card, abilityId, toggle.checked, ab.source);
    });
  });

  // "Use my own credentials" launcher (Phase 3 BYO).
  card.querySelectorAll('.conn-byo-setup-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();  // don't trigger card collapse
      _openByoSetupModal(agent, btn.dataset.provider, conn);
    });
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
        <input type="text" class="conn-token-input agents-input" placeholder="Enter bot token..." value="${_esc(tokenVal)}" autocomplete="off" data-lpignore="true" data-1p-ignore="true" style="font-family:monospace;">
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
    ebay: {
      label: 'eBay Account',
      hint: 'Link your eBay seller account to search, list, and manage items.',
    },
    etsy: {
      label: 'Etsy Account',
      hint: 'Link your Etsy account to search listings and manage your shop.',
    },
    shopify: {
      label: 'Shopify Store',
      hint: 'Connect your Shopify store to read and manage products and orders.',
      requiresShopDomain: true,
    },
    amazon: {
      label: 'Amazon Seller',
      hint: 'Link your Amazon Selling Partner account (region required).',
      requiresRegion: true,
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
    const abilitiesHtml = _renderAbilitiesBlock(conn._abilities || [], conn, canEdit);

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
        ${abilitiesHtml}
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
        ${abilitiesHtml}
        <span class="conn-save-msg"></span>
      `;
    }
    return el;
  }

  // No expandable body for other available-but-unconfigured types yet
  return null;
}

// ── Per-ability toggles inside OAuth provider cards ──────────────────────

function _renderAbilitiesBlock(abilities, conn, canEdit) {
  // Drop implicit abilities (sign-in / profile) — they're always on.
  const list = (abilities || []).filter(a => !a.implicit && a.mode !== 'disabled');
  if (!list.length) return '';

  // BYO setup link surfaces only when the app admin allows BYO somewhere
  // on this provider AND the caller can edit (members can't set creds).
  const byoAllowed = canEdit && list.some(a => a.mode === 'byo_only' || a.mode === 'both');
  const byoLink = byoAllowed
    ? `<button class="conn-byo-setup-btn" data-provider="${_esc(list[0].provider)}"
                style="background:none;border:none;color:#7aa2f7;font-size:10px;cursor:pointer;padding:0;margin-left:auto;">
         Use my own credentials →
       </button>`
    : '';

  const rows = list.map(ab => {
    const cantToggle = !canEdit;
    const checked = ab.enabled ? 'checked' : '';
    const dis = cantToggle ? 'disabled' : '';
    const sourceTag = ab.source === 'byo'
      ? '<span style="font-size:9px;background:#3a2a4a;color:#bb9af7;padding:1px 4px;border-radius:3px;margin-left:6px;font-weight:600;">BYO</span>'
      : '';
    const byoConfigured = ab.byo_configured
      ? '<span title="BYO credentials configured" style="font-size:10px;color:#9ece6a;margin-left:6px;">●</span>'
      : '';
    return `
      <div class="conn-ability-row" data-ability-id="${_esc(ab.id)}" data-provider="${_esc(ab.provider)}"
           style="display:flex;align-items:center;padding:6px 8px;gap:8px;border-top:1px solid #2a2a4a;">
        <div style="flex:1;min-width:0;">
          <div style="font-size:12px;color:#c0caf5;font-weight:500;">
            ${_esc(ab.display_name)}${sourceTag}${byoConfigured}
          </div>
          <div style="font-size:10px;color:#565f89;line-height:1.4;">${_esc(ab.description || '')}</div>
        </div>
        <label class="conn-toggle-wrap" title="${cantToggle ? 'Admin only' : (ab.enabled ? 'Disable' : 'Enable')}">
          <input type="checkbox" class="conn-toggle conn-ability-toggle" ${checked} ${dis}>
          <span class="conn-toggle-track"></span>
        </label>
      </div>
    `;
  }).join('');

  return `
    <div class="conn-abilities-block" style="margin-top:10px;border:1px solid #2a2a4a;border-radius:6px;background:#161728;">
      <div style="font-size:10px;color:#7aa2f7;padding:6px 8px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;display:flex;align-items:center;">
        Abilities
        ${byoLink}
      </div>
      ${rows}
    </div>
  `;
}

async function _onAbilityToggle(agent, conn, cardEl, abilityId, enabled, source) {
  const msgEl = cardEl.querySelector('.conn-save-msg');
  if (msgEl) { msgEl.textContent = ''; msgEl.className = 'conn-save-msg'; }
  try {
    const body = { user_id: app.currentUserId, enabled, source: source || 'platform' };
    const res = await fetch(`/api/v1/agents/${agent.id}/abilities/${encodeURIComponent(abilityId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      const reason = data.detail || `HTTP ${res.status}`;
      if (msgEl) { msgEl.textContent = reason; msgEl.className = 'conn-save-msg error'; }
      return;
    }
    if (data.reauth_required && data.authorize_url) {
      // Open the authorize URL in a popup — same flow as the Connect button.
      _oauthRefreshCallback = () => {
        const body = cardEl.closest('.agent-detail-panel')?.querySelector('.agent-detail-body');
        if (body) _renderConnectionsTab(body, agent);
      };
      _oauthPopup = window.open(data.authorize_url, `oauth-reauth`, 'width=520,height=640,left=200,top=100');
      if (!_oauthPopup) window.location.href = data.authorize_url;
      if (msgEl) { msgEl.textContent = 'Re-authorize to grant the new scope...'; msgEl.className = 'conn-save-msg'; }
      return;
    }
    if (msgEl) { msgEl.textContent = '✓ Saved'; }
  } catch (e) {
    if (msgEl) { msgEl.textContent = `Error: ${e.message}`; msgEl.className = 'conn-save-msg error'; }
  }
}

// ── BYO OAuth setup (Phase 3) ─────────────────────────────────────────────
//
// Agent admins can supply their own OAuth client_id/secret for any provider
// the app admin has marked as `byo_only` or `both`. The creds are stored on
// every BYO ability row for that provider on this agent — the per-agent
// redirect URI shown in the popup is what the admin must register in their
// own Google Cloud / Microsoft / etc. project.
async function _openByoSetupModal(agent, provider, conn) {
  const redirectUri = `${window.location.origin}/agents/${agent.id}/oauth/callback/${provider}`;
  // Pick a representative ability for this provider — any will do, the BYO
  // creds row is shared across the whole provider on this agent.
  const byoAbility = (conn._abilities || []).find(a => a.provider === provider && !a.implicit);
  if (!byoAbility) {
    alert('No BYO-capable abilities available for this provider. The app admin must enable BYO first.');
    return;
  }
  const cid = window.prompt(
    `Bring-your-own OAuth credentials for ${provider}.\n\n`
    + `Register THIS redirect URI in your own OAuth project:\n`
    + `  ${redirectUri}\n\n`
    + `Then paste your Client ID:`,
    ''
  );
  if (!cid) return;
  const csec = window.prompt('Paste your Client Secret:', '');
  if (!csec) return;
  try {
    const res = await fetch(
      `/api/v1/agents/${agent.id}/abilities/${encodeURIComponent(byoAbility.id)}/byo-creds`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: app.currentUserId, client_id: cid.trim(), client_secret: csec.trim() }),
      }
    );
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(`Failed to save BYO credentials: ${data.detail || res.status}`);
      return;
    }
    alert('BYO credentials saved. Toggle abilities on to use them.');
    const body = document.querySelector(`.agent-detail-panel[data-agent-id="${agent.id}"] .agent-detail-body`);
    if (body) _renderConnectionsTab(body, agent);
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
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
    'ebay-oauth-success',
    'etsy-oauth-success',
    'shopify-oauth-success',
    'amazon-oauth-success',
  ];
  if (successTypes.includes(e.data?.type)) {
    if (_oauthPopup) { _oauthPopup.close(); _oauthPopup = null; }
    if (_oauthRefreshCallback) { _oauthRefreshCallback(); _oauthRefreshCallback = null; }
  }
});

async function _oauthConnectFromAgent(provider, agent, conn, cardEl) {
  const msgEl = cardEl.querySelector('.conn-save-msg');
  if (msgEl) { msgEl.textContent = ''; msgEl.className = 'conn-save-msg'; }

  // Some providers need extra parameters before the authorize URL can be built.
  const extraParams = {};
  if (provider === 'shopify') {
    const shop = window.prompt('Enter your Shopify shop domain (e.g. my-store.myshopify.com):', '');
    if (!shop) return;
    extraParams.shop = shop.trim();
  } else if (provider === 'amazon') {
    const region = window.prompt('Amazon SP-API region — NA, EU, or FE:', 'NA');
    if (!region) return;
    extraParams.region = region.trim().toUpperCase();
  }

  const qs = new URLSearchParams({ user_id: app.currentUserId, ...extraParams }).toString();
  try {
    const res = await fetch(
      `/api/v1/agents/${agent.id}/connections/${provider}/authorize?${qs}`
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
      // Toggling the per-agent Automation ability shows/hides the Automation tab.
      if (conn.connection_type === 'automation' && conn.section === 'ability') {
        const state = _expandedAgents.get(agent.id);
        if (state) {
          state.automationEnabled = enabled;
          _refreshAgentTabBar(agent);
        }
        if (enabled) {
          _showAbilitiesNotice(agent, '✓ Automation tab is now available above.');
        }
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
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to manage members.</div>';
    return;
  }
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
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to test it in the loop.</div>';
    return;
  }
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

    case 'attachment_describe':
      return isCustom
        ? 'Describe images for non-vision models — click to toggle'
        : 'When the turn model can\'t see images, a vision model describes them and the text is injected';

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
        ? `Max turns: ${agent.max_turn_count || '∞ (unlimited)'} — click to edit turn limit & permission gate`
        : `Turn counter — unlimited by default (${agent.max_turn_count || '∞'})`;

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
        ? `Max turns: ${agent.max_turn_count || '∞ (unlimited)'} — click to edit`
        : `Loops back if tool results exist; stops at max_turns (${agent.max_turn_count || 'unlimited'})`;

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
      _lvAppendItem(list, { name: 'Max turns',type: 'tool', desc: agent.max_turn_count ? String(agent.max_turn_count) : '∞ (unlimited)' });
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
        name: `Max turns: ${agent.max_turn_count || '∞ (unlimited)'}`,
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
    case 'attachment_describe':
      _lvRenderGatedNodeEditor(body, agent, 'attachment_describe', 'Attachment Description',
        'When an image is attached and the model(s) handling this turn cannot see images, a separately-configured vision model describes each image once and the description is injected into the message as text (and persisted, so later turns keep it). The image describer is whichever model you mark image-capable in App Config → Default LLM. Disable for agents that never need to read images.',
        {
          dbEffect: 'Calls the configured vision model once per new image; caches the description on the attachment row and updates the user interaction content. No new tables.',
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
  desc.textContent = 'Set how many agentic turns the loop can run (0 = unlimited) before forcing a final response.';
  body.appendChild(desc);
  _lvSliderRow(body, 'Max turns', agent.max_turn_count ?? 0, 0, 30, 1, val => {
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

// ── Per-agent Memory switches (Config tab) ────────────────────────────────────
// Memory has two independently-gated halves at runtime, both already honored by
// the agent loop (app/api/chat.py) and the Agent Loop diagram:
//   • Recall — auto pre-turn brain search (loop_logic "memory_search") plus the
//     agent-callable "memory" tool.
//   • Save   — auto post-turn note, gated by BOTH loop_logic "memory_save" AND
//     the absence of "memory_save" from allowed_tools.
// NOTE: allowed_tools is a DISABLED list, not an allow list — a tool name present
// there is OFF. These helpers read/merge the very same signals the loop diagram
// uses, so flipping a switch here is identical to flipping it on the diagram.
function _loopNodeEnabledPersisted(agent, nodeId) {
  const ll = Array.isArray(agent.loop_logic) ? agent.loop_logic : [];
  if (!ll.length || typeof ll[0] === 'string') return true;   // legacy flat / empty → all nodes run
  const found = ll.find(it => it && it.node === nodeId);
  return found ? found.enabled !== false : true;              // unknown node → enabled
}

function _memoryStateFromAgent(agent) {
  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  return {
    recall: _loopNodeEnabledPersisted(agent, 'memory_search'),
    save: _loopNodeEnabledPersisted(agent, 'memory_save') && !disabled.has('memory_save'),
  };
}

// Encode the two switches into loop_logic (object-array) + allowed_tools, preserving
// every other node/tool setting. Returns { loop_logic, allowed_tools }.
function _memoryUpdatesFor(agent, recall, save) {
  // loop_logic → object array (seed from the full node list when legacy/empty,
  // exactly as the loop diagram does, so the stored shape stays identical).
  const ll = Array.isArray(agent.loop_logic) ? agent.loop_logic : [];
  let objs;
  if (!ll.length || typeof ll[0] === 'string') {
    objs = (Array.isArray(LOOP_NODES) && LOOP_NODES.length)
      ? LOOP_NODES.map(n => ({ node: n.id, enabled: true }))
      : [{ node: 'memory_search', enabled: true }, { node: 'memory_save', enabled: true }];
  } else {
    objs = ll.map(it => ({ ...it }));
  }
  const setNode = (nodeId, enabled) => {
    const f = objs.find(o => o && o.node === nodeId);
    if (f) f.enabled = enabled; else objs.push({ node: nodeId, enabled });
  };
  setNode('memory_search', recall);
  setNode('memory_save', save);

  // allowed_tools (DISABLED list): the "memory" read/write tool follows Recall;
  // the "memory_save" auto-save gate follows Save.
  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  if (recall) disabled.delete('memory'); else disabled.add('memory');
  if (save) disabled.delete('memory_save'); else disabled.add('memory_save');

  return { loop_logic: objs, allowed_tools: [...disabled] };
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
  const tcVal   = fv('max_turn_count'); if (tcVal !== undefined) updates.max_turn_count = parseInt(tcVal, 10) || 0;
  const wcVal   = fv('max_wall_seconds');
  if (wcVal !== undefined && wcVal !== '') {
    const parsed = parseFloat(wcVal);
    updates.max_wall_seconds = isNaN(parsed) ? null : parsed;
  } else if (wcVal !== undefined) {
    updates.max_wall_seconds = null;
  }

  // Stall guard limits (0 = off/infinite)
  const icVal = fv('max_identical_tool_calls');
  if (icVal !== undefined && icVal !== '') {
    const parsed = parseInt(icVal, 10);
    updates.max_identical_tool_calls = isNaN(parsed) ? 0 : parsed;
  }
  const ssVal = fv('max_stall_strikes');
  if (ssVal !== undefined && ssVal !== '') {
    const parsed = parseInt(ssVal, 10);
    updates.max_stall_strikes = isNaN(parsed) ? 0 : parsed;
  }
  const ttVal   = fv('trigger_type');   if (ttVal !== undefined) updates.trigger_type   = ttVal;
  const tkVal   = fv('trigger_key');    if (tkVal !== undefined) updates.trigger_key    = tkVal || null;
  const umChecked = panelEl.querySelector('[data-field="user_mode"]:checked');
  if (umChecked) updates.user_mode = umChecked.value;

  // Memory switches → same loop_logic + allowed_tools the Agent Loop diagram uses.
  const memRecallCb = panelEl.querySelector('[data-field="memory_recall"]');
  const memSaveCb   = panelEl.querySelector('[data-field="memory_save"]');
  if (memRecallCb || memSaveCb) {
    const cur    = _memoryStateFromAgent(agent);
    const recall = memRecallCb ? memRecallCb.checked : cur.recall;
    const save   = memSaveCb   ? memSaveCb.checked   : cur.save;
    const mu = _memoryUpdatesFor(agent, recall, save);
    updates.loop_logic    = mu.loop_logic;
    updates.allowed_tools = mu.allowed_tools;
  }

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

// ── Create agent from mock card ───────────────────────────────────────────────

async function _createAgentFromMock(panelEl) {
  // Gather the name from the config tab
  const nameEl = panelEl.querySelector('[data-field="name"]');
  const descEl = panelEl.querySelector('[data-field="desc"]');
  const tplEl = panelEl.querySelector('[data-field="template"]');
  const name = nameEl ? nameEl.value.trim() : '';
  if (!name) {
    if (nameEl) nameEl.focus();
    return;
  }

  const templateId = tplEl ? tplEl.value : 'default';

  // Gather LLM config from panel state
  const llmConfig = panelEl._llmState || { use_default: true };

  try {
    const res = await fetch('/api/v1/agents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: app.currentUserId,
        name,
        description: descEl ? descEl.value.trim() : '',
        template_id: templateId || 'default',
        llm_config: llmConfig,
      }),
    });
    const data = await res.json();
    if (res.ok) {
      // Close the mock card
      _expandedAgents.delete(MOCK_AGENT_ID);
      // Reload agents list
      await _loadAgents();
      _renderList();
      _saveViewState();
      // Auto-expand the new agent
      const newAgent = _agents.find(a => a.id === data.agent?.id);
      if (newAgent) {
        _expandedAgents.set(newAgent.id, { tab: 'config' });
        _saveViewState();
        _renderList();
      }
      // Make the new agent the active one so chat picks it up on next send.
      const newId = data.agent && data.agent.id;
      if (newId) {
        app.currentAgentId = newId;
        try { localStorage.setItem('selectedAgentId', newId); } catch (_) {}
        if (typeof app.populateAgentSelect === 'function') {
          try { await app.populateAgentSelect(app.currentUserId); } catch (_) {}
        }
      }
    } else {
      alert(data.detail || 'Failed to create agent');
    }
  } catch (e) {
    console.warn('agents: create from mock failed', e);
    alert('Error creating agent: ' + e.message);
  }
}

// ── Create modal (removed — replaced by mock card) ───────────────────────────

// ── Persisted view state ──────────────────────────────────────────────────────

const _STORAGE_KEY = 'agents_view_state';

function _saveViewState() {
  try {
    const expanded = {};
    for (const [agentId, state] of _expandedAgents) {
      if (agentId === MOCK_AGENT_ID) continue; // don't persist mock state
      expanded[agentId] = { tab: state.tab };
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
        _expandedAgents.set(agentId, { tab: state.tab || null });
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
