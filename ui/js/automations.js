'use strict';

/**
 * Automations Dashboard — aggregates scheduled tasks, event triggers,
 * active worker spawns, clone agents, and webhooks into one unified view.
 *
 * Lazy init: first call to startAutomations() does the work; subsequent
 * calls re-render with fresh data.
 */

import { app } from './state.js';
import { apiPath } from './config.js';

// ── Helpers ────────────────────────────────────────────────────────

function _qs(id) { return document.getElementById(id); }

function _esc(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(str || ''));
  return d.innerHTML;
}

function _fmtTime(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now - d;
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'Just now';
    if (diffMin < 60) return diffMin + 'm ago';
    const diffH = Math.floor(diffMin / 60);
    if (diffH < 24) return diffH + 'h ago';
    const diffD = Math.floor(diffH / 24);
    if (diffD < 7) return diffD + 'd ago';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  } catch (_) { return '—'; }
}

function _statusBadge(status) {
  if (!status) return '<span class="auto-status unknown">—</span>';
  const cls = status.replace(/\s+/g, '_');
  const labels = {
    ok: 'OK', error: 'Error', pending: 'Pending',
    running: 'Running', done: 'Done', stopped: 'Stopped',
    queued: 'Queued',
  };
  const label = labels[status] || status;
  return `<span class="auto-status ${_esc(cls)}">${_esc(label)}</span>`;
}

function _typeIcon(type) {
  switch (type) {
    case 'scheduled': return '<i data-lucide="clock" style="width:14px;height:14px;color:var(--accent);"></i>';
    case 'event':     return '<i data-lucide="zap" style="width:14px;height:14px;color:var(--warning, #e5a83e);"></i>';
    case 'worker':    return '<i data-lucide="share-2" style="width:14px;height:14px;color:var(--success, #9ece6a);"></i>';
    case 'clone':     return '<i data-lucide="copy" style="width:14px;height:14px;color:var(--fg-3);"></i>';
    case 'webhook':   return '<i data-lucide="webhook" style="width:14px;height:14px;color:var(--fg-3);"></i>';
    default:          return '<i data-lucide="circle" style="width:14px;height:14px;"></i>';
  }
}

function _enabledToggle(enabled, id, type) {
  const checked = enabled ? 'checked' : '';
  return `<label class="auto-toggle">
    <input type="checkbox" class="auto-toggle-input" data-id="${_esc(id)}" data-type="${_esc(type)}" ${checked}>
    <span class="auto-toggle-slider"></span>
  </label>`;
}

// ── State & wiring guard ───────────────────────────────────────────

let _initialized = false;
let _refreshTimer = null;
const _REFRESH_INTERVAL_MS = 30000;

let _agentsData = [];
let _webhooksData = [];
let _clonesData = [];
let _filterAgentId = '';

// ── Fetch ──────────────────────────────────────────────────────────

async function _fetchDashboard() {
  const userId = app.currentUserId;
  if (!userId) return null;

  const token = localStorage.getItem('auth_token');
  let url = `/api/v1/automations/dashboard?user_id=${encodeURIComponent(userId)}`;
  if (_filterAgentId) url += `&agent_id=${encodeURIComponent(_filterAgentId)}`;
  if (token) url += `&token=${encodeURIComponent(token)}`;

  try {
    const res = await fetch(apiPath(url));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch (e) {
    console.warn('[Automations] Failed to fetch dashboard:', e);
    return null;
  }
}

// ── Render ─────────────────────────────────────────────────────────

function _render() {
  const container = _qs('automations-container');
  if (!container) return;

  const loading = _qs('automations-loading');
  if (loading) loading.style.display = 'none';

  // Build the HTML
  let html = '';

  // Agent filter bar
  html += _renderFilterBar();

  // Per-agent sections
  if (_agentsData.length === 0 && _webhooksData.length === 0 && _clonesData.length === 0) {
    html += `<div class="auto-empty">
      <i data-lucide="settings-2" style="width:32px;height:32px;opacity:0.3;"></i>
      <p>No automations yet. Create scheduled tasks or event triggers in any agent to see them here.</p>
    </div>`;
  }

  for (const agent of _agentsData) {
    html += _renderAgentSection(agent);
  }

  // Clones section (hidden agents only visible here)
  if (_clonesData.length > 0) {
    html += _renderClonesSection();
  }

  // Webhooks section (user-level)
  if (_webhooksData.length > 0) {
    html += _renderWebhooksSection();
  }

  container.innerHTML = html;

  // Re-render lucide icons
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons({ nodes: [container] });
  }

  // Wire filter change
  const filterSelect = _qs('automations-filter-agent');
  if (filterSelect) {
    filterSelect.addEventListener('change', (e) => {
      _filterAgentId = e.target.value;
      _loadAndRender();
    });
  }
}

function _renderFilterBar() {
  let html = '<div class="auto-filter-bar">';
  html += '<label class="auto-filter-label">Agent:</label>';
  html += '<select id="automations-filter-agent" class="auto-filter-select">';
  html += '<option value="">All agents</option>';
  for (const a of _agentsData) {
    const sel = _filterAgentId === a.agent_id ? ' selected' : '';
    const count = a.automations.length + a.event_subscriptions.length + a.spawns.length;
    html += `<option value="${_esc(a.agent_id)}"${sel}>${_esc(a.agent_name)} (${count})</option>`;
  }
  html += '</select>';
  html += `<button type="button" id="automations-refresh-btn" class="auto-refresh-btn" title="Refresh">
    <i data-lucide="refresh-cw" style="width:14px;height:14px;"></i>
  </button>`;
  html += '</div>';
  return html;
}

function _renderAgentSection(agent) {
  const hasAny = agent.automations.length > 0 || agent.event_subscriptions.length > 0 || agent.spawns.length > 0;
  if (!hasAny && _filterAgentId) {
    // In single-agent mode, show even if empty
  }

  const totalItems = agent.automations.length + agent.event_subscriptions.length + agent.spawns.length;
  let html = '<div class="auto-agent-section">';

  // Agent header
  html += `<div class="auto-agent-header">
    <i data-lucide="bot" style="width:18px;height:18px;color:var(--accent);"></i>
    <span class="auto-agent-name">${_esc(agent.agent_name)}</span>
    <span class="auto-agent-count">${totalItems} item${totalItems !== 1 ? 's' : ''}</span>
  </div>`;

  // Scheduled tasks
  for (const row of agent.automations) {
    html += _renderRow(row, agent.agent_name);
  }

  // Event subscriptions
  for (const row of agent.event_subscriptions) {
    html += _renderRow(row, agent.agent_name);
  }

  // Spawns
  for (const row of agent.spawns) {
    html += _renderRow(row, agent.agent_name);
  }

  if (totalItems === 0) {
    html += `<div class="auto-empty-subtle">No automated tasks for this agent</div>`;
  }

  html += '</div>';
  return html;
}

function _renderRow(row, agentName) {
  let trigger = '—';
  let runner = _esc(agentName) || '—';
  let target = '—';
  let lastRun = '—';
  let nextRun = '—';
  let status = '';
  let runCount = '—';
  let error = '';

  switch (row.type) {
    case 'scheduled':
      trigger = `<code class="auto-cron">${_esc(row.trigger || '—')}</code>`;
      if (row.timezone && row.timezone !== 'UTC') trigger += ` <span class="auto-tz">${_esc(row.timezone)}</span>`;
      target = row.channel ? _esc(row.channel) : 'chat';
      lastRun = _fmtTime(row.last_run_at);
      nextRun = _fmtTime(row.next_run_at);
      status = _statusBadge(row.last_status);
      runCount = row.run_count || '—';
      error = row.last_error ? `<span class="auto-error" title="${_esc(row.last_error)}">Error</span>` : '';
      break;

    case 'event':
      trigger = `<span class="auto-event-src">${_esc(row.trigger)}</span>`;
      if (row.filter && Object.keys(row.filter).length > 0) {
        trigger += ` <span class="auto-filter-badge">filter</span>`;
      }
      target = row.channel ? _esc(row.channel) : 'chat';
      lastRun = _fmtTime(row.last_event_at);
      status = _statusBadge(row.last_status);
      runCount = row.fire_count != null ? String(row.fire_count) : '—';
      error = row.last_error ? `<span class="auto-error" title="${_esc(row.last_error)}">Error</span>` : '';
      break;

    case 'worker':
      trigger = `<span class="auto-worker-name">${_esc(row.name || row.task || '—')}</span>`;
      runner = _esc(agentName) + ' → <span class="auto-clone-badge">clone</span>';
      lastRun = _fmtTime(row.heartbeat_at);
      status = _statusBadge(row.status);
      runCount = '—';
      error = row.result_summary ? `<span class="auto-error" title="${_esc(row.result_summary)}">summary</span>` : '';
      break;

    case 'webhook':
      trigger = `<span class="auto-webhook-name">${_esc(row.name || '—')}</span>`;
      runner = '— (user-level)';
      lastRun = _fmtTime(row.last_triggered_at);
      status = row.active ? '<span class="auto-status ok">Active</span>' : '<span class="auto-status error">Disabled</span>';
      runCount = '—';
      break;

    case 'clone':
      trigger = `<span class="auto-clone-of">clone of ${_esc(row.clone_of || '?')}</span>`;
      runner = _esc(row.name || '—');
      status = _statusBadge(row.status);
      break;
  }

  const label = row.label || row.name || '';
  const labelHtml = label ? `<div class="auto-row-label">${_esc(label)}</div>` : '';

  return `<div class="auto-row" data-id="${_esc(row.id)}" data-type="${_esc(row.type)}">
    <div class="auto-row-icon">${_typeIcon(row.type)}</div>
    <div class="auto-row-body">
      ${labelHtml}
      <div class="auto-row-meta">
        <span class="auto-meta-trigger">${trigger}</span>
        <span class="auto-meta-divider">·</span>
        <span class="auto-meta-runner">${runner}</span>
        <span class="auto-meta-divider">·</span>
        <span class="auto-meta-target">${target}</span>
      </div>
    </div>
    <div class="auto-row-stats">
      <span class="auto-stat"><span class="auto-stat-label">Last</span> ${lastRun}</span>
      <span class="auto-stat"><span class="auto-stat-label">Next</span> ${nextRun}</span>
      <span class="auto-stat"><span class="auto-stat-label">Count</span> ${runCount}</span>
    </div>
    <div class="auto-row-end">
      ${_enabledToggle(row.enabled !== false, row.id, row.type)}
      ${status}
      ${error}
    </div>
  </div>`;
}

function _renderClonesSection() {
  let html = '<div class="auto-agent-section">';
  html += `<div class="auto-agent-header">
    <i data-lucide="copy" style="width:18px;height:18px;color:var(--fg-3);"></i>
    <span class="auto-agent-name">Clone Agents</span>
    <span class="auto-agent-count">hidden from normal roster — visible here only</span>
  </div>`;
  for (const c of _clonesData) {
    html += _renderRow(c, c.name);
  }
  html += '</div>';
  return html;
}

function _renderWebhooksSection() {
  let html = '<div class="auto-agent-section">';
  html += `<div class="auto-agent-header">
    <i data-lucide="webhook" style="width:18px;height:18px;color:var(--fg-3);"></i>
    <span class="auto-agent-name">Inbound Webhooks</span>
    <span class="auto-agent-count">user-level (not tied to a specific agent)</span>
  </div>`;
  for (const w of _webhooksData) {
    html += _renderRow(w, '');
  }
  html += '</div>';
  return html;
}

// ── Data loading ───────────────────────────────────────────────────

async function _loadAndRender() {
  const loading = _qs('automations-loading');
  if (loading) loading.style.display = 'flex';

  const data = await _fetchDashboard();
  if (!data) {
    if (loading) loading.style.display = 'none';
    return;
  }

  _agentsData = data.agents || [];
  _webhooksData = data.webhooks || [];
  _clonesData = data.clones || [];
  _render();
}

// ── Wire DOM (run once) ────────────────────────────────────────────

function _wireDom() {
  if (_initialized) return;
  _initialized = true;

  // Refresh button (delegated)
  const container = _qs('automations-container');
  if (container) {
    container.addEventListener('click', (e) => {
      const refreshBtn = e.target.closest('#automations-refresh-btn');
      if (refreshBtn) {
        _loadAndRender();
      }
    });
  }
}

// ── Exports ────────────────────────────────────────────────────────

export function startAutomations() {
  if (!app.currentUserId) return;
  _wireDom();
  _loadAndRender();

  // Auto-refresh
  if (!_refreshTimer) {
    _refreshTimer = setInterval(_loadAndRender, _REFRESH_INTERVAL_MS);
  }
}

export function stopAutomations() {
  if (_refreshTimer) {
    clearInterval(_refreshTimer);
    _refreshTimer = null;
  }
}