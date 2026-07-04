'use strict';

// Agent selection — load agents, agent cache, last-session-per-agent map, the
// WebAgent singleton (find-or-create from the `default` template), agent picker.
// Sets app.currentAgentId. Module map: ui/chat-side-panel/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { getPinnedAgents, sortAgentsForDisplay } from '../../shared/js/ordering.js';
import { icon, claudeMark } from '../../shared/js/icons.js';
import { ICON_PICKER_ICONS } from '../../shared/js/icon-picker.js';

// ── Current-agent abilities cache ──────────────────────────────────────────
window.__currentAgentAbilities = null;

async function _refreshAgentAbilities(agentId) {
  window.__currentAgentAbilities = null;
  if (!agentId) return;
  try {
    const res = await fetch(apiPath(`/api/v1/agents/${agentId}/abilities?user_id=${encodeURIComponent(app.currentUserId)}`), { headers: authHeaders() });
    if (!res.ok) return;
    const data = await res.json();
    const enabled = new Set();
    for (const ab of data.abilities || []) {
      if (ab.enabled) enabled.add(ab.id);
    }
    window.__currentAgentAbilities = enabled;
  } catch (_) { /* non-fatal */ }
}

// ── Last-active session per agent ──────────────────────────────────────────
const _lastSessionPerAgent = new Map();

function _loadLastSessionMap() {
  try {
    const raw = localStorage.getItem('lastSessionPerAgent');
    if (raw) {
      const parsed = JSON.parse(raw);
      for (const [k, v] of Object.entries(parsed)) _lastSessionPerAgent.set(k, v);
    }
  } catch (_) { /* ignore corrupt data */ }
}

function _saveLastSessionMap() {
  try {
    const obj = {};
    for (const [k, v] of _lastSessionPerAgent.entries()) obj[k] = v;
    localStorage.setItem('lastSessionPerAgent', JSON.stringify(obj));
  } catch (_) { /* storage may be full */ }
}

// ── WebAgent singleton ─────────────────────────────────────────────────────
const WEBAGENT_TEMPLATE_ID = 'default';
const _webagentIdCache = new Map();

async function findWebagentAgent(userId) {
  if (_webagentIdCache.has(userId)) return _webagentIdCache.get(userId);
  try {
    const res = await fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`), { headers: authHeaders() });
    if (!res.ok) return null;
    const data = await res.json();
    const match = (data.agents || []).find(a => a.template_id === WEBAGENT_TEMPLATE_ID);
    if (match) { _webagentIdCache.set(userId, match.id); return match.id; }
  } catch (e) {
    console.warn('[WebAgent] find failed:', e);
  }
  return null;
}

async function createWebagentAgent(userId) {
  // Idempotent get-or-create on the server: /agents/ensure-default returns the
  // EXISTING WebAgent when one is already present on the authority, and only
  // creates one otherwise. This is what stops duplicate WebAgents piling up when
  // several devices (or this page's two ensure paths) resolve the singleton at
  // once \u2014 the old code POSTed /agents, which ALWAYS created a new row.
  const res = await fetch(apiPath('/api/v1/agents/ensure-default'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ user_id: userId }),
  });
  if (!res.ok) {
    let detail = `agent ensure failed (${res.status})`;
    try { const e = await res.json(); detail = e.detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const data = await res.json();
  const id = data.agent && data.agent.id;
  if (!id) throw new Error('agent ensure returned no id');
  _webagentIdCache.set(userId, id);
  if (typeof app.populateAgentSelect === 'function') {
    try { await app.populateAgentSelect(userId); } catch (_) {}
  }
  return id;
}

async function ensureWebagentAgent(userId) {
  if (!userId) throw new Error('not signed in');
  // Prefer the local find (cheap, avoids a round-trip when the roster already
  // holds the WebAgent); fall back to the authoritative server get-or-create,
  // which is itself idempotent so a miss here can never mint a duplicate.
  return (await findWebagentAgent(userId)) || (await createWebagentAgent(userId));
}

// ── Agent selector ─────────────────────────────────────────────────────────
let _agentsCache = [];

function _setAgentTriggerLabel() {
  // Agent dropdown UI removed — no-op kept for call sites.
}

function _renderAgentRows() {
  // Agent dropdown UI removed — no-op kept for call sites.
}

/**
 * Render the agent dropdown menu (#agent-dropdown-menu) from _agentsCache.
 * Highlights the current agent. Called on page init and whenever agents change.
 */
function _renderAgentDropdown() {
  const menu = document.getElementById('agent-dropdown-menu');
  if (!menu) return;
  menu.innerHTML = '';
  if (!_agentsCache.length) {
    const empty = document.createElement('div');
    empty.className = 'agent-dropdown-empty';
    empty.textContent = 'No agents available';
    menu.appendChild(empty);
    return;
  }
  for (const a of _agentsCache) {
    const row = document.createElement('div');
    row.className = 'agent-dropdown-item' + (a.id === app.currentAgentId ? ' selected' : '');
    row.dataset.agentId = a.id;
    const name = (a.name || a.id.slice(0, 12)).replace(/</g, '&lt;');
    row.innerHTML = `<span class="agent-row-icon">${_agentIconHtml(a.id, '14px')}</span> ${name}`;
    row.title = name;
    menu.appendChild(row);
  }
}

function _agentIconFor(agentId) {
  if (!agentId) return '';
  const found = _agentsCache.find(a => a.id === agentId);
  return (found && found.icon) || '';
}

/**
 * Render an agent's icon as proper HTML — Lucide SVG via icon(), Claude spark
 * mark for Claude Code agents, or emoji/text. Matches the agent page rendering.
 * @param {string} agentId
 * @param {string} [size='16px']  CSS size e.g. '16px'
 * @param {boolean} [useDefault=true]  Whether to fall back to 'bot' icon
 * @returns {string} HTML string (empty if no agent and no default)
 */
function _agentIconHtml(agentId, size, useDefault) {
  if (!agentId) return '';
  const found = _agentsCache.find(a => a.id === agentId);
  if (!found) return '';
  const name = found.icon || '';
  const engine = found.engine || '';
  const n = parseFloat(size) || 16;
  const unit = String(size).replace(/[\d.]/g, '') || 'px';
  const big = `${n * 1.5}${unit}`;
  // Claude Code agents get the real spark mark
  if (engine === 'claude_code' && (!name || name === 'sparkles')) {
    return claudeMark({ size: big });
  }
  if (!name) {
    return icon('bot', { size: big });
  }
  if (ICON_PICKER_ICONS.includes(name)) {
    return icon(name, { size: big });
  }
  // Emoji / raw-text
  return `<span style="font-size:${big};line-height:1;display:inline-flex;align-items:center;justify-content:center">${name.replace(/</g, '&lt;')}</span>`;
}

function _agentNameFor(agentId) {
  if (!agentId) return '';
  const found = _agentsCache.find(a => a.id === agentId);
  if (found && found.name) return found.name;
  if (agentId === window.__agentId && window.__agentName) return window.__agentName;
  return '';
}

// Resolve an agent's runtime engine (e.g. 'claude_code', 'terminal_chat') from
// the dropdown cache. Returns '' when the agent isn't cached yet (cold boot) —
// callers that must not miss a match should treat '' as "unknown, don't assume".
function _agentEngineFor(agentId) {
  if (!agentId) return '';
  const found = _agentsCache.find(a => a.id === agentId);
  return (found && found.engine) || '';
}

async function _fetchAgentRunningStatuses() {
  // Agent dropdown UI removed — no-op kept for call sites.
}

async function populateAgentSelect(userId) {
  if (!userId) { return; }

  try {
    let agentsData = window.__agentsSharedData;
    if (!agentsData) {
      const agentsRes = await fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`), { headers: authHeaders() });
      agentsData = agentsRes.ok ? await agentsRes.json() : { agents: [] };
      window.__agentsSharedData = agentsData;
    }

    const saved = localStorage.getItem('selectedAgentId');
    const pinned = getPinnedAgents(userId);

    const customs = agentsData.agents || [];

    _agentsCache = customs.map(a => ({
      id: a.id,
      name: a.name || a.id.slice(0, 12),
      icon: a.icon || '',
      engine: a.engine || '',
      type: 'custom',
      pinned: pinned.has(a.id),
    }));
    _agentsCache = sortAgentsForDisplay(_agentsCache, userId);

    let found = null;
    if (window.__agentId) {
      found = _agentsCache.find(a => a.id === window.__agentId);
      if (!found) {
        _agentsCache = [{
          id: window.__agentId,
          name: window.__agentName || window.__agentId.slice(0, 12),
          icon: '',
          engine: '',
          type: 'custom',
          pinned: false,
        }];
        found = _agentsCache[0];
      }
    } else if (saved) {
      found = _agentsCache.find(a => a.id === saved);
    }
    if (!found) {
      found = _agentsCache[0] || null;
    }
    app.currentAgentId = (found && found.id) || '';
    _refreshAgentAbilities(app.currentAgentId);

    _renderAgentRows();
    _renderAgentDropdown();
    _setAgentTriggerLabel();

    // Trigger session list refresh if sessions exist
    if (typeof app.populateSessionSelect === 'function') {
      // sessions not yet fetched here
    }
  } catch (e) {
    console.warn('Failed to load agents for selector:', e);
  }
}

export {
  _agentsCache,
  _lastSessionPerAgent,
  _loadLastSessionMap,
  _saveLastSessionMap,
  _fetchAgentRunningStatuses,
  ensureWebagentAgent,
  populateAgentSelect,
  _refreshAgentAbilities,
  _agentIconFor,
  _agentIconHtml,
  _agentNameFor,
  _agentEngineFor,
  _setAgentTriggerLabel,
  _renderAgentRows,
  _renderAgentDropdown,
};