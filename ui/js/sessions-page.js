'use strict';

/**
 * Sessions Table panel.
 *
 * Renders a table of all the user's sessions with per-session stats:
 * agent, title, status, message count, input/output tokens, cost, duration.
 * Clicking a row switches the main chat to that session.
 *
 * Lazy init: first call to startSessions() lazily initializes; subsequent
 * calls re-render the data.
 */

import { app } from './state.js';
import { apiPath } from './config.js';
import { loadSessionChat, populateSessionSelect } from './sessions.js';

// ── Helpers ────────────────────────────────────────────────────────

function _qs(id) { return document.getElementById(id); }

function _esc(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(str || ''));
  return d.innerHTML;
}

function _fmtDuration(ms) {
  if (ms == null || ms === 0) return '—';
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return secs + 's';
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return m + 'm ' + s + 's';
  const h = Math.floor(m / 60);
  const mr = m % 60;
  return h + 'h ' + mr + 'm';
}

function _fmtCost(cost) {
  if (cost == null || cost === 0) return '—';
  if (cost < 0.001) return '$' + cost.toFixed(6);
  return '$' + cost.toFixed(4);
}

function _fmtTokens(n) {
  if (n == null || n === 0) return '—';
  if (n < 1000) return String(n);
  return (n / 1000).toFixed(1) + 'K';
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
  if (!status) return '<span class="sess-status unknown">Unknown</span>';
  const cls = status.replace(/\s+/g, '_');
  return `<span class="sess-status ${_esc(cls)}">${_esc(status)}</span>`;
}

// ── State & wiring guard ───────────────────────────────────────────

let _initialized = false;
let _sessionsData = [];
let _refreshTimer = null;
const _REFRESH_INTERVAL_MS = 30000;

// ── Fetch & render ─────────────────────────────────────────────────

async function _fetchSessions() {
  const userId = app.currentUserId;
  if (!userId) return [];

  const token = localStorage.getItem('auth_token');
  let url = `/api/v1/db/session-stats?db=local.db&user_id=${encodeURIComponent(userId)}`;
  if (token) url += `&token=${encodeURIComponent(token)}`;

  try {
    const res = await fetch(apiPath(url));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    return data.sessions || [];
  } catch (e) {
    console.warn('[Sessions] Failed to fetch stats:', e);
    return [];
  }
}

function _renderTable(sessions) {
  const tbody = _qs('sessions-table-body');
  const empty = _qs('sessions-empty');
  const loading = _qs('sessions-loading');
  if (!tbody) return;

  if (loading) loading.style.display = 'none';

  if (!sessions || sessions.length === 0) {
    tbody.innerHTML = '';
    if (empty) empty.style.display = 'flex';
    return;
  }
  if (empty) empty.style.display = 'none';

  let html = '';
  for (const s of sessions) {
    const agentLabel = s.agent_name || s.agent_id || '—';
    html += `<tr data-session-id="${_esc(s.session_id)}" data-agent-id="${_esc(s.agent_id || '')}">
      <td class="col-agent">
        <span class="sess-agent-badge">
          <i data-lucide="bot" style="width:12px;height:12px;"></i>
          ${_esc(agentLabel)}
        </span>
      </td>
      <td class="col-title" title="${_esc(s.title)}">${_esc(s.title)}</td>
      <td class="col-status">${_statusBadge(s.run_status)}</td>
      <td class="col-msgs">${s.message_count || '—'}</td>
      <td class="col-tokens-in">${_fmtTokens(s.total_input_tokens)}</td>
      <td class="col-tokens-out">${_fmtTokens(s.total_output_tokens)}</td>
      <td class="col-cost">${_fmtCost(s.total_cost)}</td>
      <td class="col-duration">${_fmtDuration(s.total_duration_ms)}</td>
      <td class="col-updated">${_fmtTime(s.last_active)}</td>
    </tr>`;
  }
  tbody.innerHTML = html;

  // Re-render lucide icons in the new rows
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons({ nodes: [tbody] });
  }
}

async function _loadAndRender() {
  const loading = _qs('sessions-loading');
  if (loading) loading.style.display = 'flex';

  _sessionsData = await _fetchSessions();
  _renderTable(_sessionsData);
}

// ── Row click: switch to that session ──────────────────────────────

function _onRowClick(e) {
  const tr = e.target.closest('tr[data-session-id]');
  if (!tr) return;

  const sessionId = tr.dataset.sessionId;
  const agentId = tr.dataset.agentId;

  if (!sessionId) return;

  // Switch the chat to this session
  if (agentId && app.currentAgentId !== agentId) {
    app.currentAgentId = agentId;
  }
  app.currentSessionId = sessionId;
  app.sessionTitle = tr.querySelector('.col-title')?.textContent || sessionId.slice(0, 12);

  loadSessionChat(sessionId);
  populateSessionSelect(app.currentUserId);
}

// ── Wire DOM events (run once) ─────────────────────────────────────

function _wireDom() {
  if (_initialized) return;
  _initialized = true;

  // Wire refresh button
  const refreshBtn = _qs('sessions-refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      _loadAndRender();
    });
  }

  // Wire row clicks (delegated)
  const tbody = _qs('sessions-table-body');
  if (tbody) {
    tbody.addEventListener('click', _onRowClick);
  }
}

// ── Exports (matches the start/stop pattern used by other tabs) ────

export function startSessions() {
  if (!app.currentUserId) return;
  _wireDom();
  _loadAndRender();

  // Start auto-refresh
  if (!_refreshTimer) {
    _refreshTimer = setInterval(_loadAndRender, _REFRESH_INTERVAL_MS);
  }
}

export function stopSessions() {
  if (_refreshTimer) {
    clearInterval(_refreshTimer);
    _refreshTimer = null;
  }
}

// Aliases for backward compat
export const activateSessionsPage = startSessions;
export const initSessionsPage = startSessions;