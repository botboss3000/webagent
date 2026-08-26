'use strict';

/**
 * 🤖 AI CODING AGENT — for column layout changes, edit column-config.js, NOT this file.
 *    This file is a GENERIC RENDERER — every column, breakpoint, sort key, and
 *    data source is read from the config objects in column-config.js.
 *
 * Sessions Table panel.
 *
 * Renders a table of all the user's sessions with per-session stats:
 * agent, title, status, message count, input/output tokens, cost, duration.
 * Clicking a row switches the main chat to that session.
 *
 * Lazy init: first call to startSessions() lazily initializes; subsequent
 * calls re-render the data.
 */

import { app } from '../../../../shared/js/state.js';
import { apiPath } from '../../../../shared/js/config.js';
import { authHeaders } from '../../../../shared/js/left-login.js';
import { loadSessionChat } from '../../../../chat/js/session-load.js';
import { populateSessionSelect, primeSessionMetadataCache, _formatRelativeTime } from '../../../../chat/js/session-list.js';
import { _esc, _fmtTime } from '../../../../shared/js/dom-utils.js';
import { icon, claudeMark, codexMark } from '../../../../shared/js/icons.js';
import { ICON_PICKER_ICONS } from '../../../../shared/js/icon-picker.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../../../shared/js/delete-control.js';
import { onSessionsChanged } from '../../../../shared/js/session-events.js';
import { loadViewerConfig, getColumns, getGutterConfig, getDataSources, getBreakpointRules, getHiddenColumns, getStickyTotalWidth, respHideClass } from './column-config.js';
import { hydrateAgentDisplay } from '../../../../shared/js/agent-display-cache.js';
import { kvRead, kvWrite } from '../../../../shared/js/kv-ui-state.js';
import { compareSessionsByRecentActivity } from '../../../../shared/js/session-ordering.js';
import {
  ensureAgentCacheHydrated,
  readAgentCache,
  writeAgentCache,
} from '../../js/agent-cache.js';

// ── Helpers ────────────────────────────────────────────────────────

function _qs(id) { return document.getElementById(id); }

function _renderAccessDeniedState() {
  const section = _qs('sessions-section');
  if (section) {
    section.classList.remove('agents-phantom');
    section.dataset.accessBlocked = 'denied';
  }
  const tbody = _qs('sessions-table-body');
  if (tbody) tbody.innerHTML = '';
  const loading = _qs('sessions-loading');
  if (loading) loading.style.display = 'none';
  const spinner = _qs('sessions-loading-spinner');
  if (spinner) spinner.style.display = 'none';
  const empty = _qs('sessions-empty');
  if (empty) {
    const text = empty.querySelector('.sessions-empty-text');
    const hint = empty.querySelector('.sessions-empty-hint');
    if (text) text.textContent = 'Session history is unavailable';
    if (hint) hint.textContent = 'Refresh the page or sign in again to view your sessions';
    empty.style.display = 'flex';
  }
}

/**
 * Render an agent icon from inline fields (Lucide name, engine, emoji).
 * Matches _agentIconHtml in session-agent.js.
 */
function _agentIconHtml(iconName, engine, size) {
  const n = parseFloat(size) || 14;
  const unit = String(size).replace(/[\d.]/g, '') || 'px';
  const big = `${n * 1.5}${unit}`;
  if (engine === 'claude_code' && (!iconName || iconName === 'sparkles')) {
    return claudeMark({ size: big });
  }
  if (engine === 'codex' && (!iconName || iconName === 'code-2')) {
    return codexMark({ size: big });
  }
  if (!iconName) return icon('bot', { size: big });
  if (ICON_PICKER_ICONS.includes(iconName)) return icon(iconName, { size: big });
  return `<span style="font-size:${big};line-height:1;display:inline-flex;align-items:center;justify-content:center">${iconName.replace(/</g, '&lt;')}</span>`;
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

function _statusBadge(status) {
  if (!status) return '<span class="sess-status unknown">Unknown</span>';
  const cls = status.replace(/\s+/g, '_');
  return `<span class="sess-status ${_esc(cls)}">${_esc(status)}</span>`;
}

// Time-ago tint mirrors the session's run-status chip (same semantic hues):
// complete=green, running=blue, interrupted/error=red, stopped=amber; a session
// with no run status keeps the neutral base grey. Same normalization as
// _statusBadge so the class always matches a chip class when one exists.
function _timeStatusClass(runStatus) {
  if (!runStatus) return '';
  return String(runStatus).replace(/\s+/g, '_');
}

// ── Live time-ago ticking + click-to-flash-status ──────────────────
// The relative times next to session names are LIVE: a 1s interval re-runs
// _formatRelativeTime on every [data-ts] span so "8m ago" counts up to 9m, 10m,
// … and hours/days roll over while the page stays open. Clicking a time
// flashes the session's run status to its left (e.g. "Completed 8m ago") for
// exactly 1s, then it clears.
const _STATUS_LABELS = {
  complete: 'Completed',
  running: 'Running',
  queued: 'Queued',
  interrupted: 'Interrupted',
  error: 'Error',
  stopped: 'Stopped',
  needs_manual_resume: 'Needs resume',
};

function _statusFlashLabel(status) {
  if (!status) return '';
  if (_STATUS_LABELS[status]) return _STATUS_LABELS[status];
  return status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, ' ');
}

// One 1s pass: recompute every visible relative time from its data-ts.
function _tickRelativeTimes() {
  document.querySelectorAll('#sessions-table .sess-title-time[data-ts]').forEach(el => {
    el.textContent = _formatRelativeTime(el.dataset.ts);
  });
}

// Show the run-status label left of a clicked time for 1s. Re-clicking while
// visible restarts the timer; a re-render that replaces the node cancels it.
function _flashStatus(timeEl) {
  const wrap = timeEl.closest('.sess-title-time-wrap');
  if (!wrap) return;
  const statusEl = wrap.querySelector('.sess-title-status');
  const label = _statusFlashLabel(timeEl.dataset.status || '');
  if (!statusEl || !label) return;
  if (wrap._flashTimer) clearTimeout(wrap._flashTimer);
  statusEl.classList.remove('flash');
  void statusEl.offsetWidth;   // restart the CSS animation
  statusEl.textContent = label;
  statusEl.classList.add('flash');
  wrap._flashTimer = setTimeout(() => {
    statusEl.classList.remove('flash');
    statusEl.textContent = '';
  }, 1000);
}

// Small icon badges marking sessions that built/edited a genui (has_genui)
// and/or drove the live web browser (has_browser). Both are derived server-side
// from the tool_executions log. Renders an em-dash when neither applies.
// The badges are clickable: a click switches the chat to that session and opens
// the Gen UI or Browser tab so you can jump straight to the artifact
// the session produced. (Handled by the delegated tbody click in _wireDom.)
function _linkBadges(s) {
  const sid = _esc(s.session_id);
  const aid = _esc(s.agent_id || '');
  let html = '';
  if (s.has_genui) {
    html += `<span class="sess-link-badge genui" role="button" tabindex="0"
      data-link-kind="genui" data-session-id="${sid}" data-agent-id="${aid}"
      title="Open this session's genui">
      <i data-lucide="layout-dashboard" style="width:12px;height:12px;"></i></span>`;
  }
  if (s.has_browser) {
    html += `<span class="sess-link-badge browser" role="button" tabindex="0"
      data-link-kind="browser" data-session-id="${sid}" data-agent-id="${aid}"
      title="Open this session in the browser page">
      <i data-lucide="globe" style="width:12px;height:12px;"></i></span>`;
  }
  return html || '<span class="sess-link-none">—</span>';
}

// Which device ran this session (stamped at creation; see app/devices/). Shown
// as a compact badge beside the agent so a multi-device fleet is legible at a
// glance. Rendered only when the session carries a device label.
function _deviceBadge(s) {
  if (!s.device_label) return '';
  return `<span class="sess-device-badge" title="Ran on device: ${_esc(s.device_label)}">
    <i data-lucide="monitor" style="width:11px;height:11px;"></i>${_esc(s.device_label)}</span>`;
}

// ── State & wiring guard ───────────────────────────────────────────

let _initialized = false;
let _sessionsData = [];
let _sessionsDataViewId = '';
let _sessionsDataAgentId = null; // which agent the _sessionsData buffer belongs to (null = account-wide / All Agents)
let _agentFilterId = null; // null = the All Agents catalog
let _nativeCodexContext = null; // agent metadata when its card owns the native Codex catalog
let _refreshTimer = null;
let _sourceAccessDenied = false;
let _tickTimer = null;   // 1s interval that re-renders the live time-ago labels
let _onKsChanged = null; // kill-switch-changed listener while the tab is active
let _viewId = 'sessions';  // 'sessions' | 'recycle-bin' | 'both' (chip switcher)
let _activeConfig = null;
let _searchQuery = '';
let _searchMode = 'content';  // 'title' | 'content' — persisted in IDB-backed UI state
let _lastTheadConfigId = null; // only rebuild thead when config/view actually changes
let _agentContextLoadPending = false;
let _loadStatusTimer = null;

function _cacheKey(viewId, agentId, nativeContext) {
  if (nativeContext && agentId) {
    return `sessions:codex:${app.currentUserId || 'anonymous'}:${agentId}`;
  }
  return agentId ? null : `sessions:${viewId}`;
}

function _setLoadStatus(text = '', spinning = false, clearAfter = 0) {
  if (_loadStatusTimer) clearTimeout(_loadStatusTimer);
  _loadStatusTimer = null;
  const spinner = _qs('sessions-loading-spinner');
  const status = _qs('sessions-loading-status');
  if (spinner) spinner.style.display = spinning ? 'inline-block' : 'none';
  if (status) {
    status.textContent = text;
    status.classList.toggle('active', !!text);
  }
  if (clearAfter && text) {
    _loadStatusTimer = setTimeout(() => _setLoadStatus(), clearAfter);
  }
}

function _markSessionsPaint(source) {
  const section = _qs('sessions-section');
  if (!section || typeof performance === 'undefined') return;
  const at = performance.now().toFixed(1);
  section.dataset.lastPaintAt = at;
  section.dataset.lastPaintSource = source;
  if (!section.dataset.firstPaintAt) {
    section.dataset.firstPaintAt = at;
    section.dataset.firstPaintSource = source;
  }
}

// The Agents page owns one sessions table. The All Agents Sessions tab and an
// opened agent's Sessions tab move that same DOM node into their card so search,
// sorting, selection, and refresh state are preserved.
export function setSessionsAgentContext(agentId = null, container = null, options = {}) {
  const section = _qs('sessions-section');
  const page = _qs('tab-agents');
  const nextId = agentId || null;
  const nextNativeCodex = nextId && options.nativeCodex ? {
    agentId: nextId,
    agentName: options.agentName || 'Codex',
    agentIcon: options.agentIcon || '',
  } : null;
  if (section) {
    const target = container || page;
    if (target && section.parentElement !== target) target.appendChild(section);
    section.classList.toggle('sessions-agent-filtered', !!nextId);
    section.classList.toggle('sessions-native-codex', !!nextNativeCodex);
    if (nextId) section.dataset.agentId = nextId;
    else delete section.dataset.agentId;
  }
  const viewButton = _qs('sessions-view-btn');
  if (viewButton) viewButton.hidden = !!nextNativeCodex;
  const changed = _agentFilterId !== nextId || !!_nativeCodexContext !== !!nextNativeCodex;
  _agentFilterId = nextId;
  _nativeCodexContext = nextNativeCodex;
  if (!nextNativeCodex) _setLoadStatus();
  if (changed && nextId !== _sessionsDataAgentId) {
    // The shared buffer still holds the previous context's rows — never paint
    // them into the new context. Skeleton lines hold the table's shape until
    // the filtered fetch lands (see _loadAndRender).
    _renderSkeletonRows();
  } else if ((changed || container) && _activeConfig) {
    // Context matches the buffer (same agent re-opened, or All Agents view):
    // the immediate render preserves the responsive feel of moving the shared
    // table into an agent card.
    _renderTable(_sessionsData);
  }
  // The immediate render above preserves the responsive feel of moving the
  // shared table into an agent card. Re-fetch on a context change so the card
  // is backed by the selected agent's catalog rather than the account-wide
  // response. Queue this because a card render first clears the context, then
  // assigns the selected agent in the same call stack.
  if (changed && _activeConfig && !_agentContextLoadPending) {
    _agentContextLoadPending = true;
    queueMicrotask(() => {
      _agentContextLoadPending = false;
      _loadAndRender();
    });
  }
}
// Cross-catalog search cache: while a query is active we lazily fetch the OTHER
// catalog (sessions ↔ recycle bin) so results surface from both views at once.
// Rows carry `_origin` ('active' | 'bin') and `_cross` (true = from the other
// catalog — rendered dimmed with an origin badge, excluded from selection).
let _searchExtra = null;
let _searchExtraPending = null;
// ── Advanced search state (message content) ─────────────────────────
// Message search is a client-side scan: for sessions in the current + other
// catalog we lazily fetch their newest messages (light=1) once per session
// and cache role+content for USER and ASSISTANT rows only — tool calls and
// system messages are excluded by design. Matches render as a badge + snippet.
const _MSG_MIN_QUERY_LEN = 2;   // below this, search is title/agent only
const _MSG_SCAN_MAX = 300;      // most-recently-active sessions scanned
const _MSG_FETCH_LIMIT = 300;   // newest messages fetched per session
const _msgIndex = new Map();    // sessionId -> [{role, content}] (user|assistant)
let _msgScanBusy = null;        // query currently being scanned (latest wins)
// Restore view state from the tenant-scoped IDB cache (with legacy mirror).
let _stored = {};
try {
  const saved = kvRead('agents:sessions-view-state', 'data_viewer_view');
  _stored = typeof saved === 'string' ? JSON.parse(saved || '{}') : (saved || {});
  if (_stored.viewId) _viewId = _stored.viewId;
} catch (_) {}
// The old dropdown also offered an Automations view — that is its own main-panel
// tab now, so migrate any stored value back into the sessions scope.
if (!['sessions', 'recycle-bin', 'both'].includes(_viewId)) _viewId = 'sessions';
_searchMode = _stored.searchMode || 'content';
const _REFRESH_INTERVAL_MS = 30000;
// Which parent rows are currently expanded in the tree. Persists across the
// 30s auto-refresh so an open group stays open. Mirrors the chat session list's
// _expandedGroups (ui/chat/js/session-list.js).
const _expandedGroups = new Set();

// Narrow title-only viewer: set by _wireGutter; re-measured by startSessions
// on every tab activation (the tab is display:none until active, so resize
// events alone can't keep the mode correct).
let _narrowUpdater = null;

// ── Sort state ─────────────────────────────────────────────────────
// Default: most recent first (last_active descending).
let _sortColumn = _stored.sortColumn || 'last_active';
let _sortAsc = _stored.sortAsc === true;

// Persist view state to the tenant-scoped IDB cache.
function _saveViewState() {
  kvWrite('agents:sessions-view-state', 'data_viewer_view', {
    viewId: _viewId,
    sortColumn: _sortColumn,
    sortAsc: _sortAsc,
    searchMode: _searchMode,
  });
}

// ── Search-mode button state ─────────────────────────────────────────
// Single toggle: "T+C" (content) ↔ "Title" (title). Title mode gets an
// accent-tinted glass-chip appearance via the .title-mode CSS class.
function _updateSearchModeBtn() {
  const btn = _qs('sessions-search-mode-btn');
  if (!btn) return;
  const isTitle = _searchMode === 'title';
  btn.textContent = isTitle ? 'Title' : 'T+C';
  btn.dataset.mode = _searchMode;
  btn.title = 'Search mode: ' + (isTitle ? 'Title only' : 'Title + Content');
  btn.classList.toggle('title-mode', isTitle);
}

// ── View switcher button state ─────────────────────────────────────
// ONE glass-chip button (like the search T+C/Title chip) that cycles the
// active view filter: Sessions → All → Bin → Sessions. The label mirrors
// the current view; All/Bin carry the .alt-mode accent tint (mirroring the
// search chip's Title-mode state).
const _VIEW_CYCLE = ['sessions', 'both', 'recycle-bin'];
const _VIEW_LABELS = { sessions: 'Sessions', both: 'All', 'recycle-bin': 'Bin' };
function _updateViewButton() {
  const btn = _qs('sessions-view-btn');
  if (!btn) return;
  btn.dataset.view = _viewId;
  const label = _VIEW_LABELS[_viewId] || 'Sessions';
  btn.textContent = label;
  btn.classList.toggle('alt-mode', _viewId !== 'sessions');
  btn.title = 'View: ' + label + ' — click to switch (Sessions / All / Bin)';
}

// ── Sort helper ────────────────────────────────────────────────────
// The sort-key map maps data-sort-key values to the property on the session object.
// Some columns need a type-aware comparator (numbers, strings, dates).
const _SORT_KEY_MAP = {
  agent_name:        { key: 'agent_name',        type: 'string' },
  title:             { key: 'title',             type: 'string' },
  run_status:        { key: 'run_status',        type: 'string' },
  message_count:     { key: 'message_count',     type: 'number' },
  total_input_tokens:  { key: 'total_input_tokens',  type: 'number' },
  total_output_tokens: { key: 'total_output_tokens', type: 'number' },
  total_cost:        { key: 'total_cost',        type: 'number' },
  total_duration_ms: { key: 'total_duration_ms', type: 'number' },
  last_active:       { key: 'last_active',       type: 'date' },
};

function _applySort(sessions) {
  const spec = _SORT_KEY_MAP[_sortColumn] || _SORT_KEY_MAP.last_active;
  const dir = _sortAsc ? 1 : -1;
  return sessions.slice().sort((a, b) => {
    // Pinned sessions always come first, regardless of the column sort — but a
    // binned row loses that pinned ordering status (the pin flag stays set so
    // restore brings it back): bin rows sort purely by the chosen column.
    const ap = !!(a.pinned && a._origin !== 'bin');
    const bp = !!(b.pinned && b._origin !== 'bin');
    if (ap !== bp) return ap ? -1 : 1;
    if (ap && bp) {
      const ao = Number.isFinite(a.sort_order) ? a.sort_order : Number.MAX_SAFE_INTEGER;
      const bo = Number.isFinite(b.sort_order) ? b.sort_order : Number.MAX_SAFE_INTEGER;
      if (ao !== bo) return ao - bo;
    }
    let va = a[spec.key];
    let vb = b[spec.key];
    if (spec.type === 'number') {
      va = (va == null) ? -Infinity : Number(va);
      vb = (vb == null) ? -Infinity : Number(vb);
      return (va - vb) * dir;
    }
    if (spec.type === 'date') {
      // The default recent view intentionally blends coarse recency with
      // repeated use so its first few rows do not reshuffle on every message.
      // Explicit ascending date sort remains strictly chronological.
      if (_sortColumn === 'last_active' && !_sortAsc) {
        return compareSessionsByRecentActivity(a, b);
      }
      va = Date.parse(va || a.created_at || '') || 0;
      vb = Date.parse(vb || b.created_at || '') || 0;
      return (va - vb) * dir;
    }
    // string type
    va = (va || '').toLowerCase();
    vb = (vb || '').toLowerCase();
    return va.localeCompare(vb) * dir;
  });
}

function _updateSortArrows() {
  document.querySelectorAll('#sessions-table thead th.col-sortable').forEach(th => {
    const key = th.dataset.sortKey;
    th.classList.remove('sorted-asc', 'sorted-desc');
    let arrow = th.querySelector('.sort-arrow');
    if (!arrow) {
      arrow = document.createElement('span');
      arrow.className = 'sort-arrow';
      th.appendChild(arrow);
    }
    if (key === _sortColumn) {
      th.classList.add(_sortAsc ? 'sorted-asc' : 'sorted-desc');
      arrow.textContent = _sortAsc ? '▲' : '▼';
    } else {
      arrow.textContent = '▽';
    }
  });
}

function _setSort(columnKey) {
  if (_sortColumn === columnKey) {
    _sortAsc = !_sortAsc;  // toggle direction
  } else {
    _sortColumn = columnKey;
    _sortAsc = false;      // default to descending (most recent / highest first)
  }
  _saveViewState();
  _updateSortArrows();
  _renderTable(_sessionsData);
}

// ── Fetch & render ─────────────────────────────────────────────────

async function _fetchSource(src) {
  if (_sourceAccessDenied) return [];
  const userId = app.currentUserId;
  if (!userId) return [];
  const token = localStorage.getItem('auth_token');
  let url = apiPath(src.endpoint);
  if (!url.includes('user_id=')) url += (url.includes('?') ? '&' : '?') + 'user_id=' + encodeURIComponent(userId);
  // Agent cards must never request or display another agent's sessions. The
  // server supports this filter; the render-time filter remains as defence in
  // depth for cached data and for a context change while a request is in flight.
  if (_agentFilterId && src.row_type === 'session' && !url.includes('agent_id=')) {
    url += '&agent_id=' + encodeURIComponent(_agentFilterId);
  }
  if (token && !url.includes('token=')) url += '&token=' + encodeURIComponent(token);
  const res = await fetch(url);
  if (res.status === 401 || res.status === 403) {
    _sourceAccessDenied = true;
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    _renderAccessDeniedState();
    return [];
  }
  if (!res.ok) { console.warn('[DataViewer] Fetch failed for', src.row_type, res.status); return []; }
  const data = await res.json();
  let rows = [];
  if (src.result_shape === 'nested') {
    for (const agent of (data.agents || [])) {
      for (const r of (agent.automations || [])) rows.push({ ...r, _rowType: src.row_type, _agentName: agent.agent_name, _agentId: agent.agent_id, _agentIcon: agent.agent_icon || '', _agentEngine: agent.agent_engine || '' });
      for (const r of (agent.event_subscriptions || [])) rows.push({ ...r, _rowType: src.row_type, _agentName: agent.agent_name, _agentId: agent.agent_id, _agentIcon: agent.agent_icon || '', _agentEngine: agent.agent_engine || '' });
      for (const r of (agent.spawns || [])) rows.push({ ...r, _rowType: src.row_type, _agentName: agent.agent_name, _agentId: agent.agent_id, _agentIcon: agent.agent_icon || '', _agentEngine: agent.agent_engine || '' });
    }
    for (const w of (data.webhooks || [])) rows.push({ ...w, _rowType: src.row_type, _agentName: '', _agentId: '', _agentIcon: '', _agentEngine: '' });
    for (const c of (data.clones || [])) rows.push({ ...c, _rowType: src.row_type, _agentName: c.name || 'Clone', _agentId: c.agent_id || c.id || '', _agentIcon: c.icon || '', _agentEngine: '' });
  } else {
    rows = (data.sessions || []).map(s => ({ ...s, _rowType: src.row_type }));
    // The stats response contains the dropdown's active-session metadata plus
    // table-only fields. Publish the shared subset for a fast picker open.
    if (src.row_type === 'session' && src.origin === 'active') {
      primeSessionMetadataCache(userId, rows);
    }
  }
  return rows;
}

async function _fetchNativeCodexSessions() {
  const context = _nativeCodexContext;
  const userId = app.currentUserId;
  if (!context || !userId) return [];
  const rows = [];
  let cursor = '';
  const seenCursors = new Set();
  do {
    const qs = new URLSearchParams({
      user_id: userId,
      agent_id: context.agentId,
      limit: '200',
    });
    if (cursor) qs.set('cursor', cursor);
    const res = await fetch(apiPath(`/api/v1/engines/codex/portal/candidates?${qs}`), {
      headers: authHeaders(),
    });
    if (res.status === 401 || res.status === 403) {
      _sourceAccessDenied = true;
      _renderAccessDeniedState();
      return [];
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Codex task catalog HTTP ${res.status}`);
    // Card renders briefly clear and restore the shared sessions context. A
    // slow catalog response belongs only to the context that launched it.
    if (_nativeCodexContext !== context || app.currentUserId !== userId) return [];
    for (const thread of (data.threads || [])) {
      const threadId = String(thread.thread_id || '').trim();
      if (!threadId) continue;
      const status = typeof thread.status === 'string'
        ? thread.status : (thread.status?.type || thread.status?.status || '');
      rows.push({
        ...thread,
        session_id: thread.id || `codex:${threadId}`,
        agent_id: context.agentId,
        agent_name: context.agentName,
        agent_icon: context.agentIcon,
        agent_engine: 'codex',
        last_active: thread.updated_at || thread.created_at,
        run_status: status,
        external_authority: 'codex',
        _rowType: 'session',
        _origin: 'active',
        _nativeCodexCatalog: true,
      });
    }
    const next = String(data.next_cursor || '').trim();
    if (!next || seenCursors.has(next)) break;
    seenCursors.add(next);
    cursor = next;
  } while (cursor);
  return rows;
}

async function _fetchForConfig(cfg) {
  if (!cfg) return [];
  if (_nativeCodexContext) return _fetchNativeCodexSessions();
  const allRows = [];
  for (const src of getDataSources(cfg)) {
    try {
      const rows = await _fetchSource(src);
      // Stamp which catalog each row belongs to ('active' | 'bin') so the
      // merged 'both' view can badge binned rows and keep them non-loadable.
      const origin = src.origin || (src.endpoint.includes('status=recycled') ? 'bin' : 'active');
      for (const r of rows) r._origin = origin;
      allRows.push(...rows);
    } catch (e) { console.warn('[DataViewer] Error fetching', src.row_type, e); }
  }
  return allRows;
}

// ── Cross-catalog search (sessions ↔ recycle bin) ────────────────────
// The search field matches against the CURRENT catalog by default. When a
// query is active we also fetch the other catalog (lazily, once, cached) so
// results surface recycled sessions while in the Sessions view and live
// sessions while in the Recycling bin view. Only session rows cross over —
// automations stay scoped to their own view. The cache is invalidated on
// every load/refresh so it tracks restore/delete changes.
function _invalidateSearchExtra() { _searchExtra = null; _searchExtraPending = null; }

async function _ensureSearchExtra() {
  if (_nativeCodexContext) return [];
  if (_viewId !== 'sessions' && _viewId !== 'recycle-bin') return [];
  if (_searchExtra !== null) return _searchExtra;
  if (_searchExtraPending) return _searchExtraPending;
  const otherId = _viewId === 'recycle-bin' ? 'sessions' : 'recycle-bin';
  const origin = _viewId === 'recycle-bin' ? 'active' : 'bin';
  _searchExtraPending = (async () => {
    try {
      const cfg = await loadViewerConfig(otherId);
      const src = (getDataSources(cfg) || []).find(s => s.row_type === 'session');
      const rows = src ? await _fetchSource(src) : [];
      _searchExtra = rows.map(r => ({ ...r, _origin: origin }));
    } catch (e) {
      console.warn('[Sessions] Failed to load cross-catalog search data:', e);
      _searchExtra = [];
    } finally {
      _searchExtraPending = null;
    }
    return _searchExtra;
  })();
  return _searchExtraPending;
}

// ── Advanced search — message-content matching ──────────────────────
// Toggle the little "searching messages…" spinner inside the search field.
function _setMsgScanning(on) {
  const el = _qs('sessions-msg-scanning');
  if (el) el.classList.toggle('on', !!on);
}

// Fetch ONE session's newest messages (light mode) and cache ONLY the
// user/assistant text rows — tool calls (role 'tool') and system messages
// are excluded here, at the source, so they can never match a query.
async function _fetchSessionMessages(sessionId) {
  if (_msgIndex.has(sessionId)) return _msgIndex.get(sessionId);
  const token = localStorage.getItem('auth_token');
  let url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sessionId)}&limit=${_MSG_FETCH_LIMIT}&light=1`);
  if (token) url += `&token=${encodeURIComponent(token)}`;
  let store = [];
  try {
    const res = await fetch(url);
    if (res.ok) {
      const data = await res.json();
      store = (data.messages || [])
        .filter(m => (m.role === 'user' || m.role === 'assistant') && m.content && String(m.content).trim())
        .map(m => ({ role: m.role, content: String(m.content) }));
    }
  } catch (e) {
    console.warn('[Sessions] Message fetch failed for', sessionId, e);
  }
  _msgIndex.set(sessionId, store);   // cache even on failure (empty) → no retry loop
  return store;
}

// How many user/assistant messages in this session contain `q` (plus up to 3
// snippets for display). Null when there are no matches.
function _msgMatchInfo(sessionId, q) {
  const idx = _msgIndex.get(sessionId);
  if (!idx || !q) return null;
  const ql = q.toLowerCase();
  let count = 0;
  const snippets = [];
  for (const m of idx) {
    if (m.content.toLowerCase().includes(ql)) {
      count++;
      if (snippets.length < 3) snippets.push(m.content);
    }
  }
  return count > 0 ? { count, snippets } : null;
}

// Build a compact highlighted snippet around the first match of `q` in a
// message's text — escaped, with the hit wrapped in <mark>.
function _msgSnippetHtml(content, q) {
  const ql = q.toLowerCase();
  const low = content.toLowerCase();
  const i = low.indexOf(ql);
  if (i < 0) {
    const cut = content.slice(0, 80);
    return _esc(cut) + (content.length > 80 ? '…' : '');
  }
  const start = Math.max(0, i - 30);
  const end = Math.min(content.length, i + q.length + 70);
  let html = start > 0 ? '…' : '';
  html += _esc(content.slice(start, i));
  html += '<mark>' + _esc(content.slice(i, i + q.length)) + '</mark>';
  html += _esc(content.slice(i + q.length, end));
  if (end < content.length) html += '…';
  return html;
}

// Stamp a row with its message-match info (count + snippet) for the current
// query. Only user/assistant content was indexed, so tool & system rows never
// register here.
function _attachMsgMatches(row, q) {
  if (!q || q.length < _MSG_MIN_QUERY_LEN || row._rowType !== 'session' || !row.session_id) return;
  const info = _msgMatchInfo(row.session_id, q);
  if (info) {
    row._msgMatchCount = info.count;
  }
}

// Prune the message index to sessions that still exist (current catalog +
// cached cross catalog). Called on every load/refresh — keeps memory bounded
// and drops deleted sessions WITHOUT forcing a full re-fetch of untouched
// sessions on the 30s auto-refresh.
function _invalidateMsgIndex() {
  const keep = new Set();
  for (const r of _sessionsData) {
    if (r._rowType === 'session' && r.session_id) keep.add(r.session_id);
  }
  for (const r of (_searchExtra || [])) {
    if (r.session_id) keep.add(r.session_id);
  }
  for (const sid of _msgIndex.keys()) {
    if (!keep.has(sid)) _msgIndex.delete(sid);
  }
}

// Scan both catalogs' session messages for `q` in concurrent batches,
// re-rendering after each batch so matches appear progressively. Results are
// cached per session, so re-typing the same query is instant. A newer query
// supersedes the in-flight one (checked at batch boundaries).
async function _scanMessagesForQuery(q) {
  if (!q || q.length < _MSG_MIN_QUERY_LEN) return;
  if (_searchMode === 'title') return; // title-only mode — no message scan
  if (_viewId !== 'sessions' && _viewId !== 'recycle-bin' && _viewId !== 'both') return;
  if (_msgScanBusy === q) return;      // already scanning this query
  _msgScanBusy = q;                    // newer query supersedes an in-flight one
  try {
    const extra = await _ensureSearchExtra();
    const sessionRows = [];
    const seen = new Set();
    for (const r of [..._sessionsData, ...(extra || [])]) {
      if (r._rowType !== 'session' || !r.session_id) continue;
      if (seen.has(r.session_id)) continue;
      seen.add(r.session_id);
      if ((r.message_count || 0) > 0) sessionRows.push(r);
    }
    sessionRows.sort((a, b) => {
      const ta = Date.parse(a.last_active || a.updated_at || a.created_at || '') || 0;
      const tb = Date.parse(b.last_active || b.updated_at || b.created_at || '') || 0;
      return tb - ta;
    });
    const todo = sessionRows.slice(0, _MSG_SCAN_MAX).filter(r => !_msgIndex.has(r.session_id));
    if (todo.length === 0) return;     // everything already indexed — nothing to fetch
    _setMsgScanning(true);
    const CHUNK = 8;
    for (let i = 0; i < todo.length; i += CHUNK) {
      // Stop early if the query changed or was cleared while fetching.
      if (_msgScanBusy !== q || _searchQuery.trim() !== q) return;
      await Promise.all(todo.slice(i, i + CHUNK).map(r => _fetchSessionMessages(r.session_id)));
      if (_msgScanBusy === q && _searchQuery.trim() === q) _renderTable(_sessionsData);
    }
  } catch (e) {
    console.warn('[Sessions] Message search failed:', e);
  } finally {
    if (_msgScanBusy === q) {
      _msgScanBusy = null;
      _setMsgScanning(false);
    }
    if (_searchQuery.trim() === q) _renderTable(_sessionsData);
  }
}

function _buildThead(thead, cfg) {
  const hidden = getHiddenColumns(cfg, window.innerWidth);
  const viewKey = _viewId + '|' + cfg.id;
  if (_lastTheadConfigId === viewKey) {
    // Same config — only update responsive column visibility and sort arrows.
    // Don't rebuild innerHTML — that would kill drag-resized widths and flicker.
    _updateResponsiveColumns();
    return;
  }
  _lastTheadConfigId = viewKey;
  let html = '<tr>';
  for (const col of getColumns(cfg)) {
    const align = col.align ? 'text-align:' + col.align + ';' : '';
    const width = 'width:' + col.width + 'px;min-width:48px;';
    const sortable = col.sort_key ? ' col-sortable' : '';
    const resizable = ' col-resizable';
    const sortKey = col.sort_key ? ' data-sort-key="' + col.sort_key + '"' : '';
    const respHide = hidden.has(col.id) ? ' ' + respHideClass(_breakpointForCol(col, cfg)) : '';
    const label = col.id === 'check' ? '' : col.id.charAt(0).toUpperCase() + col.id.slice(1).replace('_', ' ');
    html += '<th class="col-' + col.id + sortable + resizable + respHide + '"' + sortKey + ' style="' + width + align + '">' + label + '</th>';
  }
  html += '</tr>';
  thead.innerHTML = html;
  _applyColWidths(); // re-apply saved widths after rebuild
}

function _breakpointForCol(col, cfg) {
  for (const rule of getBreakpointRules(cfg)) {
    if ((rule.hide || []).includes(col.id)) return rule.max_width;
  }
  return 9999;
}

function _updateResponsiveColumns() {
  if (!_activeConfig) return;
  const hidden = getHiddenColumns(_activeConfig, window.innerWidth);
  const allBreakpoints = getBreakpointRules(_activeConfig).map(function(r) { return r.max_width; });
  document.querySelectorAll('#sessions-table th, #sessions-table td').forEach(function(el) {
    for (const bp of allBreakpoints) el.classList.remove(respHideClass(bp));
    for (const colId of hidden) {
      if (el.classList.contains('col-' + colId)) {
        const bp = _breakpointForCol({ id: colId }, _activeConfig);
        el.classList.add(respHideClass(bp));
      }
    }
  });
}

function _renderTable(rows) {
  const tbody = _qs('sessions-table-body');
  const empty = _qs('sessions-empty');
  const loading = _qs('sessions-loading');
  const thead = _qs('sessions-table-head');
  if (!tbody) return;
  if (loading) loading.style.display = 'none';
  // Only rebuild thead on view change — _buildThead tracks _lastTheadConfigId
  // internally. After the call we always update responsive + sort arrows.
  if (thead && _activeConfig) { _buildThead(thead, _activeConfig); _updateResponsiveColumns(); _updateSortArrows(); }
  const table = _qs('sessions-table');
  if (table) table.classList.toggle('bin-view', !_nativeCodexContext && _viewId === 'recycle-bin');
  const q = _searchQuery.trim().toLowerCase();
  let candidates = rows;
  if (q) {
    // Cross-catalog search: merge the OTHER catalog (sessions ↔ recycle bin)
    // into the pool so one query matches both. Rows keep an `_origin` tag
    // ('active' | 'bin'); `_cross` marks rows from the other catalog, which
    // render dimmed with an origin badge and are excluded from selection.
    const viewOrigin = _viewId === 'recycle-bin' ? 'bin' : 'active';
    const pool = [];
    const seen = new Set();
    const keyOf = (r) => (r._rowType === 'session' ? 's:' + (r.session_id || '') : 'a:' + (r.id || ''));
    for (const r of rows) {
      const k = keyOf(r);
      if (k && seen.has(k)) continue;
      if (k) seen.add(k);
      const row = Object.assign({}, r, { _origin: r._origin || viewOrigin, _cross: false });
      if (_searchMode !== 'title') _attachMsgMatches(row, q);
      pool.push(row);
    }
    for (const r of (_searchExtra || [])) {
      if (r._origin === viewOrigin) continue;
      const k = keyOf(r);
      if (k && seen.has(k)) continue;
      if (k) seen.add(k);
      const row = Object.assign({}, r, { _cross: true });
      if (_searchMode !== 'title') _attachMsgMatches(row, q);
      pool.push(row);
    }
    candidates = pool.filter(r => {
      const title = (r.title || r.label || r.name || '').toLowerCase();
      const agent = (r.agent_name || r._agentName || '').toLowerCase();
      return title.includes(q) || agent.includes(q) || (_searchMode !== 'title' && (r._msgMatchCount || 0) > 0);
    });
  }
  if (_agentFilterId) {
    candidates = candidates.filter(r => r._rowType !== 'session' || r.agent_id === _agentFilterId);
  }
  if (!candidates || candidates.length === 0) {
    tbody.innerHTML = '';
    if (empty) {
      const txt = empty.querySelector('.sessions-empty-text');
      const hint = empty.querySelector('.sessions-empty-hint');
      const label = _activeConfig ? _activeConfig.label : 'items';
      if (q) {
        if (txt) txt.textContent = 'No matching rows';
        if (hint) hint.textContent = 'Try a different search term';
      } else {
        if (txt) txt.textContent = _nativeCodexContext ? 'No native Codex sessions' : (_agentFilterId ? 'No sessions for this agent' : (_viewId === 'recycle-bin' ? 'Recycling bin is empty' : (_viewId === 'both' ? 'No sessions yet' : 'No ' + label.toLowerCase() + ' yet')));
        if (hint) hint.textContent = _nativeCodexContext ? 'New Codex tasks will appear here automatically' : (_agentFilterId ? 'Start a conversation with this agent to see it here' : (_viewId === 'recycle-bin' ? 'Recycled items appear here until permanently deleted' : (_viewId === 'both' ? 'Active and recycled sessions will appear here' : 'Nothing to show in ' + label)));
      }
      empty.style.display = 'flex';
    }
    return;
  }
  if (empty) empty.style.display = 'none';
  // All views show session rows only (Automations is its own tab). In the
  // merged 'both' view the config fetches BOTH catalogs, so every row already
  // carries its `_origin` ('active' | 'bin') — sorting mixes them freely.
  let filtered = _applySort(candidates.filter(r => r._rowType === 'session'));
  const byId = new Map(filtered.filter(r => r._rowType === 'session' && !r._cross).map(s => [s.session_id, s]));
  const childrenOf = new Map();
  const topLevel = [];
  for (const s of filtered) {
    // Cross-catalog rows are always shown flat — they don't participate in the
    // current view's parent/child tree (and are never expandable).
    if (s._rowType !== 'session' || s._cross) { topLevel.push(s); continue; }
    const pid = s.parent_session_id;
    if (pid && byId.has(pid)) {
      if (!childrenOf.has(pid)) childrenOf.set(pid, []);
      childrenOf.get(pid).push(s);
    } else { topLevel.push(s); }
  }
  let html = '';
  for (const row of topLevel) html += _renderRow(row, { childrenOf, depth: 0 });
  tbody.innerHTML = html;
  if (window.lucide && typeof window.lucide.createIcons === 'function') window.lucide.createIcons({ nodes: [tbody] });
  _updateSortArrows();
  _updateResponsiveColumns();
}

// Build one table row. `childrenOf` (the tree map) drives recursive rendering
// for parents — each parent with children that are expanded renders its child
// rows inline beneath itself. `depth` tracks the nesting level (0 = top-level,
// 1 = child, 2 = grandchild) for visual indentation.
function _renderRow(row, ctx) {
  if (!ctx) ctx = {};
  var childrenOf = ctx.childrenOf || null;
  var depth = ctx.depth || 0;
  if (!_activeConfig) return '';
  var columns = getColumns(_activeConfig);
  var isSession = row._rowType === 'session';
  var isAuto = row._rowType === 'automation';
  var cells = '';
  for (var i = 0; i < columns.length; i++) {
    var col = columns[i];
    if (col.for && !col.for.includes(row._rowType)) {
      cells += '<td class="col-' + col.id + '"><span style="color:var(--fg-4)">—</span></td>';
      continue;
    }
    cells += _renderCell(row, col, { childrenOf: childrenOf, depth: depth, isSession: isSession, isAuto: isAuto });
  }
  var sid = isSession ? _esc(row.session_id) : '';
  var rowId = isSession ? sid : (row.id || '');
  var rowCls = isSession ? _sessionRowClass(row, childrenOf, depth) : '';
  // data-origin stamps which catalog a row lives in ('active' | 'bin') so the
  // click handler can refuse to load recycled rows even when a cross-catalog
  // search surfaced them in the Sessions view. Cross rows get a dimmed class.
  var origin = row._origin || (_viewId === 'recycle-bin' ? 'bin' : 'active');
  var cls = (rowCls || '') + (row._cross ? ' sess-cross-row' : '');
  return '<tr data-session-id="' + sid + '" data-id="' + _esc(rowId) + '" data-agent-id="' + _esc(row.agent_id || row._agentId || '') + '" data-row-type="' + row._rowType + '" data-origin="' + _esc(origin) + '"' + (cls ? ' class="' + cls.trim() + '"' : '') + '>' + cells + '</tr>';
}

function _sessionRowClass(s, childrenOf, depth) {
  var kids = childrenOf ? (childrenOf.get(s.session_id) || []) : [];
  var childCount = kids.length;
  var activeChat = s.session_id === app.currentSessionId ? ' active-chat' : '';
  if (depth > 0) {
    return 'sessions-child-row' + (childCount > 0 ? ' group-parent' + (_expandedGroups.has(s.session_id) ? ' expanded' : '') : '') + activeChat;
  }
  return (childCount > 0 ? 'group-parent' + (_expandedGroups.has(s.session_id) ? ' expanded' : '') : '') + activeChat;
}

function _renderCell(row, col, ctx) {
  const childrenOf = ctx.childrenOf;
  const depth = ctx.depth;
  const isSession = ctx.isSession;
  const isAuto = ctx.isAuto;
  switch (col.id) {
    case 'check': return _renderCheckCell(row, isSession);
    case 'agent': return _renderAgentCell(row, isSession, isAuto);
    case 'title': case 'name': return _renderTitleCell(row, col.id, depth, isSession, isAuto, childrenOf);
    case 'status': return _renderStatusCell(row, isSession, isAuto);
    case 'type': return _renderTypeCell(row, isAuto);
    case 'trigger': return _renderTriggerCell(row, isAuto);
    case 'output': return _renderOutputCell(row, isAuto);
    case 'links': return _renderLinksCell(row, isSession);
    case 'msgs': return '<td class="col-msgs" style="text-align:right;">' + (row.message_count || '—') + '</td>';
    case 'tokens_in': return '<td class="col-tokens_in" style="text-align:right;">' + _fmtTokens(row.total_input_tokens) + '</td>';
    case 'tokens_out': return '<td class="col-tokens_out" style="text-align:right;">' + _fmtTokens(row.total_output_tokens) + '</td>';
    case 'cost': return '<td class="col-cost" style="text-align:right;">' + _fmtCost(row.total_cost) + '</td>';
    case 'duration': return '<td class="col-duration" style="text-align:right;">' + _fmtDuration(row.total_duration_ms) + '</td>';
    case 'last': return '<td class="col-last">' + _fmtTime(row.last_run_at || row.last_event_at || row.heartbeat_at || row.last_triggered_at) + '</td>';
    case 'next': return '<td class="col-next">' + _fmtTime(row.next_run_at) + '</td>';
    case 'count': return '<td class="col-count" style="text-align:right;">' + (row.run_count || row.fire_count || '—') + '</td>';
    case 'device': return _renderDeviceCell(row, isAuto);
    case 'enabled': return _renderEnabledCell(row, isAuto);
    case 'updated': return '<td class="col-updated" style="text-align:right;">' + _fmtTime(row.last_active) + '</td>';
    default: return '<td class="col-' + col.id + '">—</td>';
  }
}

function _renderCheckCell(row, isSession) {
  // Cross-catalog rows can't be selected — bulk actions (delete/restore) must
  // stay scoped to the currently viewed catalog.
  if (row._cross || row._nativeCodexCatalog) return '<td class="col-check"></td>';
  const id = isSession ? row.session_id : row.id;
  return '<td class="col-check"><span class="sessions-check-cell" data-session-id="' + _esc(id || '') + '"> </span></td>';
}

function _renderAgentCell(row, isSession, isAuto) {
  const iconName = isSession ? (row.agent_icon || '') : (row._agentIcon || '');
  const engine = isSession ? (row.agent_engine || '') : (row._agentEngine || '');
  const agentLabel = isSession ? (row.agent_name || row.agent_id || '—') : (row._agentName || row._agentId || '—');
  let deviceBadge = '';
  if (isSession && row.device_label) deviceBadge = '<span class="sess-device-badge" title="Ran on device: ' + _esc(row.device_label) + '"><i data-lucide="monitor" style="width:11px;height:11px;"></i>' + _esc(row.device_label) + '</span>';
  return '<td class="col-agent"><span class="sess-agent-badge">' + _agentIconHtml(iconName, engine, '12px') + _esc(agentLabel) + '</span>' + deviceBadge + '</td>';
}

function _renderTitleCell(row, colId, depth, isSession, isAuto, childrenOf) {
  const titleText = isAuto ? _esc(row.label || row.name || '—') : _esc(row.title);
  // Title text lives in its own span so the title cell's flex layout can
  // truncate it (ellipsis) while the time-ago stays visible on the right.
  let inner = '<span class="sess-title-text">' + titleText + '</span>';
  // Pinned sessions show the pin icon at the left edge of the title cell
  // so the pin reads at a glance even when the title scrolls.
  if (isSession) {
    const pinHtml = row.pinned ? '<i data-lucide="pin" class="sess-title-pin" style="width:12px;height:12px;display:inline-flex;"></i>' : '';
    inner = pinHtml + '<span class="sess-title-agent-icon">' + _agentIconHtml(row.agent_icon, row.agent_engine, '12px') + '</span>' + inner;
  }
  if (depth > 0 && isSession) {
    const roleIcon = row.child_role === 'closer' ? 'flag' : row.child_role === 'planner' ? 'wand-2' : 'git-branch';
    let rails = '';
    for (let i = 0; i < depth; i++) rails += '<span class="sess-child-rail"></span>';
    inner = rails + '<i data-lucide="' + roleIcon + '" class="sess-child-role-icon" style="width:12px;height:12px;"></i>' + inner;
  }
  // Expand/collapse chevron for parent groups — lives at the FAR LEFT of the
  // title cell (the caret column was removed; the gutter holds only the
  // checkbox now). Mirrors the chat session list's tree caret.
  if (isSession && !row._cross) {
    const kids = childrenOf ? (childrenOf.get(row.session_id) || []) : [];
    const childCount = kids.length;
    if (childCount > 0) {
      const open = _expandedGroups.has(row.session_id);
      inner = '<span class="sessions-row-expand" data-session-id="' + _esc(row.session_id) + '" title="' + (open ? 'Collapse' : 'Expand') + ' (' + childCount + ')"><i data-lucide="' + (open ? 'chevron-down' : 'chevron-right') + '" style="width:14px;height:14px;"></i></span>' + inner;
    }
  }
  // Relative "time ago" to the right of the title — same format as the chat
  // session dropdown (s/m/h/d/w ago, then a short date once old enough). It
  // TICKS live (a 1s interval re-renders every [data-ts] span in place) and is
  // clickable: clicking flashes the run-status label to its left for 1s
  // (e.g. "Completed 8m ago"). Running sessions swap the timer for the same
  // radial loader the chat session dropdown shows.
  if (isSession && row.last_active) {
    const timeCls = _timeStatusClass(row.run_status);
    const running = row.run_status === 'running';
    const timeHtml = running
      ? '<span class="sess-title-time" title="Agent is thinking…"><span class="session-radial-loader sm"></span></span>'
      : '<span class="sess-title-time" role="button" tabindex="0" data-ts="' + _esc(row.last_active) + '" data-status="' + _esc(row.run_status || '') + '" title="' + _esc(row.last_active) + ' — click for status">' + _formatRelativeTime(row.last_active) + '</span>';
    inner += '<span class="sess-title-time-wrap' + (timeCls ? ' ' + timeCls : '') + '">' +
      '<span class="sess-title-status" aria-hidden="true"></span>' + timeHtml + '</span>';
  }
  // Origin badge — which catalog a row came from. Cross-catalog search results
  // always carry one, so a match from the other view is unmistakable. In the
  // merged 'both' view binned rows get the same badge so the combined table
  // stays legible. The "bin" badge doubles as a two-click restore control
  // (see _onBinChipClick).
  if (row._cross || (row._origin === 'bin' && _viewId === 'both')) {
    if (row._origin === 'bin') {
      inner += '<span class="sess-origin-badge bin" role="button" tabindex="0" data-session-id="' + _esc(row.session_id) + '" title="In recycling bin — click to restore">bin</span>';
    } else {
      inner += '<span class="sess-origin-badge" title="This session is active">Active</span>';
    }
  }
  // Advanced search: show a small round chip with the number of matching messages.
  if (row._msgMatchCount > 0) {
    inner += '<span class="sess-msg-count-chip" title="' + row._msgMatchCount + ' matching message' + (row._msgMatchCount > 1 ? 's' : '') + '">' + row._msgMatchCount + '</span>';
  }
  return '<td class="col-' + colId + '" title="' + titleText + '">' + '<div class="sess-title-row">' + inner + '</div>' + '</td>';
}

function _renderStatusCell(row, isSession, isAuto) {
  if (isAuto) {
    let statusHtml = '';
    const rawStatus = row.last_status || row.status || '';
    if (row.type === 'webhook') {
      statusHtml = row.active ? '<span class="sess-status complete">Active</span>' : '<span class="sess-status error">Disabled</span>';
    } else {
      statusHtml = _statusBadge(rawStatus || '—');
    }
    if (row.last_error) statusHtml += ' <span class="auto-error" title="' + _esc(row.last_error) + '">Error</span>';
    return '<td class="col-status">' + statusHtml + '</td>';
  }
  return '<td class="col-status">' + _statusBadge(row.run_status) + '</td>';
}

function _renderTypeCell(row, isAuto) {
  if (!isAuto) return '<td class="col-type"></td>';
  const icons = { scheduled: 'clock', event: 'zap', worker: 'cpu', webhook: 'webhook' };
  const ic = icons[row.type] || 'settings';
  return '<td class="col-type"><span class="auto-type"><i data-lucide="' + ic + '" style="width:12px;height:12px;"></i>' + _esc(row.type || '') + '</span></td>';
}

function _renderTriggerCell(row, isAuto) {
  if (!isAuto) return '<td class="col-trigger"></td>';
  let trigger = '—';
  if (row.type === 'scheduled') {
    trigger = '<code class="auto-cron">' + _esc(row.trigger || '—') + '</code>';
    if (row.timezone && row.timezone !== 'UTC') trigger += ' <span class="auto-tz">' + _esc(row.timezone) + '</span>';
  } else if (row.type === 'event') {
    trigger = '<span class="auto-event-src">' + _esc(row.trigger || '—') + '</span>';
    if (row.filter && Object.keys(row.filter).length > 0) trigger += ' <span class="auto-filter-badge">filter</span>';
  } else if (row.type === 'worker') {
    trigger = '<span class="auto-worker-name">' + _esc(row.name || row.task || '—') + '</span>';
  } else if (row.type === 'webhook') {
    trigger = '<span class="auto-webhook-name">' + _esc(row.name || '—') + '</span>';
  } else if (row.type === 'clone') {
    trigger = '<span class="auto-clone-of">clone of ' + _esc(row.clone_of || '?') + '</span>';
  }
  return '<td class="col-trigger">' + trigger + '</td>';
}

function _renderOutputCell(row, isAuto) {
  if (!isAuto || !Array.isArray(row.delivery) || row.delivery.length === 0) return '<td class="col-output"><span class="auto-tz">—</span></td>';
  const outIcons = { chat: 'message-circle', channel: 'send', webhook: 'webhook', file: 'file-text', email: 'mail' };
  const inner = row.delivery.map(function(d) {
    const kind = d.kind || 'other';
    const ic = outIcons[kind] || 'arrow-right';
    return '<span class="auto-out-badge auto-out-' + kind + '" title="' + _esc(d.label || kind) + '"><i data-lucide="' + ic + '" style="width:11px;height:11px;"></i>' + _esc(d.label || kind) + '</span>';
  }).join('');
  return '<td class="col-output"><span class="auto-out-list">' + inner + '</span></td>';
}

function _renderLinksCell(row, isSession) {
  if (!isSession || row._cross) return '<td class="col-links"></td>';
  return '<td class="col-links">' + _linkBadges(row) + '</td>';
}

function _renderDeviceCell(row, isAuto) {
  if (!isAuto || row.type !== 'scheduled') return '<td class="col-device"><span class="auto-tz">—</span></td>';
  const label = row.target_device || 'This device';
  return '<td class="col-device"><span class="auto-device-chip" title="Runs on ' + _esc(label) + '"><i data-lucide="monitor" style="width:11px;height:11px;"></i><span class="auto-device-text">' + _esc(label) + '</span></span></td>';
}

function _renderEnabledCell(row, isAuto) {
  if (!isAuto) return '<td class="col-enabled"></td>';
  const enabled = row.enabled !== false;
  return '<td class="col-enabled" style="text-align:center;"><span class="auto-status ' + (enabled ? 'ok' : 'error') + '">' + (enabled ? 'On' : 'Off') + '</span></td>';
}

// Expand / collapse a parent group. Re-renders the table but preserves the
// user's current checkbox selection across the rebuild.
function _toggleGroup(sid) {
  if (_expandedGroups.has(sid)) _expandedGroups.delete(sid);
  else _expandedGroups.add(sid);

  const checked = new Set(_getSelectedSessionIds());
  _renderTable(_sessionsData);
  if (checked.size) {
    document.querySelectorAll('.sessions-check-cell').forEach(c => {
      if (c.dataset.sessionId && checked.has(c.dataset.sessionId)) {
        c.classList.add('checked');
        const tr = c.closest('tr');
        if (tr) tr.classList.add('selected');
      }
    });
  }
  _updateTrashButton();
}

// Fill the table body with phantom "shimmer" rows that mirror the real column
// layout, so the first paint has the table's shape instead of a lone centered
// spinner. Uses the shared skeleton primitives (sk-shimmer in app3.css).
// SESSIONS-SKELETON (breadcrumb) — three places paint session-list skeletons
// and must stay in sync on row count + line-width ranges:
//   1. session-dropdown → ui/chat/elements/session-dropdown/controller.js
//        (_renderSessionMenuSkeleton — the reference: 6–15 rows, 30–100% lines)
//   2. sessions page    → THIS function (_renderSkeletonRows)
//   3. agents page shell→ ui/main-panel/agents/agents.html (static phantom rows,
//        same .sessions-skeleton-row/.sess-sk classes + ranges)
function _renderSkeletonRows(count) {
  const tbody = _qs('sessions-table-body');
  const empty = _qs('sessions-empty');
  const loading = _qs('sessions-loading');
  if (!tbody) return;
  if (empty) empty.style.display = 'none';
  if (loading) loading.style.display = 'none';   // skeleton rows replace the spinner
  // Randomize EXACTLY like the session-dropdown skeleton (breadcrumb above):
  // 6–15 rows per load, lines 30–100% wide, so every load paints a slightly
  // different ragged phantom table. The FIRST content column (session title)
  // is a fixed 500px bar; the links/msgs icon columns stay fixed 46/40px; the
  // remaining columns draw a fresh 30–100% width per row.
  const rand = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
  const n = (typeof count === 'number') ? count : rand(6, 15);
  const cell = (w) => `<td><span class="sess-sk sk-shimmer" style="width:${w};"></span></td>`;
  let html = '';
  for (let i = 0; i < n; i++) {
    html += `<tr class="sessions-skeleton-row" aria-hidden="true">
      <td class="col-check"></td>
      ${cell('500px')}${cell(rand(30, 100) + '%')}${cell('46px')}${cell('40px')}${cell(rand(30, 100) + '%')}${cell(rand(30, 100) + '%')}${cell(rand(30, 100) + '%')}${cell(rand(30, 100) + '%')}${cell(rand(30, 100) + '%')}${cell(rand(30, 100) + '%')}
    </tr>`;
  }
  tbody.innerHTML = html;
}

async function _loadAndRender() {
  if (_sourceAccessDenied) {
    _renderAccessDeniedState();
    return;
  }
  const loadViewId = _viewId;
  const loadAgentFilterId = _agentFilterId;
  const loadNativeContext = _nativeCodexContext;
  const tbody = _qs('sessions-table-body');
  // Phantom takeover: the static shell ships real .sessions-skeleton-row rows in
  // the partial's tbody — replace them with a fresh random draw just like an
  // empty tbody would get (same classes, so no visual flash).
  const hasPhantom = !!(tbody && tbody.querySelector('tr.sessions-skeleton-row'));
  // Skeleton when the buffer's rows belong to a DIFFERENT context than the one
  // being loaded (e.g. an agent card opening over the account-wide list, or a
  // view-chip flip inside an open card) — never show the previous context's
  // rows while the filtered fetch runs.
  const contextMismatch = _sessionsDataAgentId !== loadAgentFilterId;
  if (hasPhantom || (tbody && tbody.children.length === 0) || contextMismatch) _renderSkeletonRows();
  let loadConfig;
  try {
    loadConfig = await loadViewerConfig(loadViewId);
    if (_viewId !== loadViewId) return;
    _activeConfig = loadConfig;
  } catch (e) {
    console.warn('[DataViewer] Failed to load config for', loadViewId, e);
    return;
  }
  const cacheKey = _cacheKey(loadViewId, loadAgentFilterId, loadNativeContext);
  const cachedRows = cacheKey ? readAgentCache(cacheKey) : null;
  const bufferMismatch = _sessionsDataViewId !== loadViewId
    || _sessionsDataAgentId !== loadAgentFilterId;
  if (bufferMismatch && Array.isArray(cachedRows)) {
    _sessionsData = cachedRows;
    _sessionsDataViewId = loadViewId;
    _sessionsDataAgentId = loadAgentFilterId;
    _renderTable(cachedRows);
    _markSessionsPaint('memory');
  }
  if (loadNativeContext) {
    _setLoadStatus(Array.isArray(cachedRows) ? 'Verifying Codex tasks…' : 'Loading Codex tasks…', true);
  }
  // The other catalog may have changed (restore/delete/refresh) — drop the
  // cross-catalog search cache and prune the message index so the next query
  // re-fetches what it needs (without nuking the whole cache on auto-refresh).
  _invalidateSearchExtra();
  _invalidateMsgIndex();
  let rows;
  try {
    rows = await _fetchForConfig(loadConfig);
  } catch (error) {
    if (loadNativeContext) _setLoadStatus('Could not verify Codex tasks', false, 3000);
    throw error;
  }
  if (_viewId !== loadViewId || _agentFilterId !== loadAgentFilterId) return;
  _sessionsData = rows;
  _sessionsDataViewId = loadViewId;
  _sessionsDataAgentId = loadAgentFilterId;
  // Keep a useful returning-visit snapshot well beyond the 30s live refresh.
  // It is only first paint: the authoritative request above still revalidates
  // immediately, so a 30-minute IDB horizon removes blank/skeleton waits without
  // making stale rows authoritative.
  // The cache key represents the account-wide viewer. Do not replace it with
  // a selected agent's narrower result set.
  if (cacheKey) writeAgentCache(
    cacheKey,
    rows,
    loadNativeContext ? 7 * 24 * 60 * 60 * 1000 : 30 * 60 * 1000,
  );
  _renderTable(rows);
  _markSessionsPaint('network');
  if (loadNativeContext) _setLoadStatus('Codex tasks verified', false, 1400);

  // ── Phantom-cell hydration ────────────────────────────────────────────
  // Session rows always carry agent_id; when the stats payload lacks display
  // fields (orphaned/deleted agents, cold agent plane), fill them from the
  // lean bulk endpoint and repaint in place — no full refetch, no empty
  // "—" cells that stick forever. Missing ids resolve to nothing and simply
  // keep their fallback label.
  const missingAgentIds = [...new Set(rows
    .filter(r => r._rowType === 'session' && r.agent_id && !r.agent_name)
    .map(r => r.agent_id))];
  if (missingAgentIds.length) {
    hydrateAgentDisplay(missingAgentIds, app.currentUserId).then(recs => {
      if (!recs || !recs.length) return;
      const byId = new Map(recs.map(r => [r.agent_id, r]));
      let changed = false;
      for (const r of _sessionsData) {
        if (r._rowType !== 'session') continue;
        const rec = byId.get(r.agent_id);
        if (!rec) continue;
        if (!r.agent_name) { r.agent_name = rec.name; changed = true; }
        if (!r.agent_icon) { r.agent_icon = rec.icon; changed = true; }
        if (!r.agent_engine) { r.agent_engine = rec.engine; changed = true; }
      }
      if (changed) _renderTable(_sessionsData);
    });
  }
  // If a search is active, re-pull the other catalog and re-render so
  // cross-catalog results survive refreshes and view switches.
  if (_searchQuery.trim()) {
    _ensureSearchExtra().then(() => {
      if (_searchQuery.trim()) _renderTable(_sessionsData);
    }).then(() => {
      _scanMessagesForQuery(_searchQuery.trim());
    });
  }
  _updateViewButton();
  _updateTrashButton();
}

// ── Row click: switch to that session ──────────────────────────────

// Toggle the "opening…" state on a clicked row: a shimmer over the row plus a
// spinner in its title cell (where the expand chevron lives), so it's obvious
// which session is loading.
function _setRowLoading(tr, on) {
  if (!tr) return;
  tr.classList.toggle('sessions-row-loading', on);
  const title = tr.querySelector('td.col-title');
  if (!title) return;
  if (on) {
    if (!title.querySelector('.sess-row-spinner')) {
      const sp = document.createElement('span');
      sp.className = 'sess-row-spinner';
      title.appendChild(sp);
    }
  } else {
    title.querySelector('.sess-row-spinner')?.remove();
  }
}

// Make `sessionId` the active chat session (switching agent if needed) and
// optionally jump to a main-panel tab afterwards ('genui' = Gen UI,
// 'browser' = Browser page). Shared by row clicks and the Links badges.
// `row` (when given) shows a per-row loading animation until the chat resolves.
async function _switchToSession(sessionId, agentId, { tab = null, title = null, row = null } = {}) {
  if (!sessionId) return;
  // Recycled sessions aren't loadable — block bin rows, whether the whole bin
  // view is open or a recycled row surfaced via cross-catalog search. Live
  // rows found through search while in the bin view ARE loadable.
  const origin = row && row.dataset ? row.dataset.origin : (_viewId === 'recycle-bin' ? 'bin' : 'active');
  if (origin === 'bin') return;
  if (agentId && app.currentAgentId !== agentId) {
    app.currentAgentId = agentId;
  }
  app.currentSessionId = sessionId;
  app.sessionTitle = title || sessionId.slice(0, 12);

  _setRowLoading(row, true);
  try {
    await loadSessionChat(sessionId);
  } finally {
    _setRowLoading(row, false);
  }
  populateSessionSelect(app.currentUserId);

  // On mobile the tab-switch hides chat — reveal it so the user sees the session
  // they just clicked, mirroring how the Agents page shows chat on tap.
  if (typeof window.__getChatVisible === 'function' && !window.__getChatVisible()
      && typeof window.__applyChatVisible === 'function') {
    window.__applyChatVisible(true);
  }

  if (tab && typeof window.__setMainTab === 'function') {
    window.__setMainTab(tab);
  }
}

function _onRowClick(e) {
  const tr = e.target.closest('tr[data-session-id]');
  if (!tr || !tr.dataset.sessionId) return;
  _switchToSession(tr.dataset.sessionId, tr.dataset.agentId, {
    title: tr.querySelector('.col-title')?.textContent,
    row: tr,
  });
}

// ── Selection helpers ──────────────────────────────────────────────

// Which catalog(s) the currently checked rows belong to ('active' | 'bin').
// Drives restore/delete behaviour in the merged 'both' view, where both
// catalogs can be selected at once.
function _selectedOrigins() {
  const origins = new Set();
  document.querySelectorAll('.sessions-check-cell.checked').forEach(cell => {
    const tr = cell.closest('tr');
    if (tr && tr.dataset.origin) origins.add(tr.dataset.origin);
  });
  return origins;
}

// Selection / bin toolbar — mirrors agents' _updateBinToolbar (HARMONY-SESSIONS-BIN).
function _updateTrashButton() {
  const trash = _qs('sessions-trash-btn');
  const restoreBtn = _qs('sessions-restore-btn');
  _updateViewButton();
  const n = document.querySelectorAll('.sessions-check-cell.checked').length;
  const origins = _selectedOrigins();
  const hasBin = origins.has('bin');
  const inBin = _viewId === 'recycle-bin';
  const inBoth = _viewId === 'both';
  // Restore is meaningful wherever binned rows can be selected: the bin view,
  // or the merged view once a binned row is checked.
  if (restoreBtn) {
    const show = inBin || (inBoth && hasBin);
    restoreBtn.style.display = show ? 'inline-flex' : 'none';
    restoreBtn.disabled = !hasBin;
  }
  if (trash) {
    if (inBin) {
      trash.title = n ? 'Permanently delete selected' : 'Select items to delete';
      trash.disabled = (n === 0);
      trash.innerHTML = icon('trash-2', { size: '16px' });
    } else if (inBoth) {
      trash.title = trashTitle();
      trash.disabled = (n === 0);
      trash.innerHTML = icon('trash-2', { size: '16px' });
    } else {
      trash.title = n ? 'Move selected to the recycling bin' : 'Open recycling bin';
      trash.disabled = false;
      trash.innerHTML = icon(n > 0 ? 'trash-2' : 'recycle', { size: '16px', style: n > 0 ? '' : 'color:var(--success)' });
    }
  }
}

function _getSelectedSessionIds() {
  const ids = [];
  document.querySelectorAll('.sessions-check-cell.checked').forEach(cell => {
    const sid = cell.dataset.sessionId;
    if (sid) ids.push(sid);
  });
  return ids;
}

function _toast(msg) {
  if (!msg) return;
  const toast = document.createElement('div');
  toast.textContent = msg;
  toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;padding:8px 18px;border-radius:8px;font-size:13px;font-weight:600;z-index:9999;';
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2500);
}

// Delete the checked sessions. Active-origin rows soft-delete (move to the
// bin); bin-origin rows permanently delete (with ?permanent=true) — so the
// merged 'both' view can act on both catalogs in one go.
async function _deleteSelectedSessions(btn) {
  const cells = Array.from(document.querySelectorAll('.sessions-check-cell.checked'));
  if (cells.length === 0) return;

  const token = localStorage.getItem('auth_token');
  let ok = 0, fail = 0, recycled = 0, deleted = 0;
  for (const cell of cells) {
    const sid = cell.dataset.sessionId;
    if (!sid) continue;
    const tr = cell.closest('tr');
    const origin = tr ? tr.dataset.origin : (_viewId === 'recycle-bin' ? 'bin' : 'active');
    const permanent = origin === 'bin';
    try {
      let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}?db=user.db`;
      if (permanent) url += '&permanent=true';
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url), { method: 'DELETE' });
      if (res.ok || res.status === 404) { ok++; if (permanent) deleted++; else recycled++; }
      else fail++;
    } catch (_) { fail++; }
  }
  if (fail > 0) console.warn('[Sessions] Failed to delete', fail, 'sessions');
  if (ok > 0) {
    const parts = [];
    if (recycled > 0) parts.push(`Recycled ${recycled} session${recycled > 1 ? 's' : ''}`);
    if (deleted > 0) parts.push(`Deleted ${deleted} session${deleted > 1 ? 's' : ''}`);
    _toast(parts.join(' · '));
  }
  await _loadAndRender();
  // Keep the chat side-panel session list in sync with what we just removed.
  try { populateSessionSelect(app.currentUserId); } catch (_) {}
  if (btn) resetDeleteBtn(btn, { size: '16px', title: trashTitle() });
  _updateTrashButton();
}

// Restore a single session from the bin. Returns true on success. Shared by
// the bulk Restore button and the "In bin" chip's two-click confirm.
async function _restoreOne(sid) {
  if (!sid) return false;
  const token = localStorage.getItem('auth_token');
  try {
    let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/restore?db=user.db`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(apiPath(url), { method: 'POST' });
    return res.ok;
  } catch (e) {
    console.warn('[Sessions] Failed to restore', sid, e);
    return false;
  }
}

// Restore the checked binned sessions back to the active list. Only bin-origin
// rows are touched — in the merged 'both' view an active row can never be
// "restored" (and the Restore button only appears once a binned row is checked).
async function _restoreSelected() {
  const cells = Array.from(document.querySelectorAll('.sessions-check-cell.checked'));
  let ok = 0;
  for (const cell of cells) {
    const sid = cell.dataset.sessionId;
    if (!sid) continue;
    const tr = cell.closest('tr');
    if (tr && tr.dataset.origin && tr.dataset.origin !== 'bin') continue;
    if (await _restoreOne(sid)) ok++;
  }
  if (ok > 0) _toast(`Restored ${ok} session${ok > 1 ? 's' : ''}`);
  await _loadAndRender();
  _updateTrashButton();
  if (typeof populateSessionSelect === 'function') {
    try { populateSessionSelect(app.currentUserId); } catch (_) {}
  }
}

// "bin" chip — two-click restore. First click arms the chip ("Restore?"),
// second click restores the session. Disarms after 3s if the user doesn't
// confirm, mirroring the delete button's confirm pattern (HARMONY-SESSIONS-BIN).
async function _onBinChipClick(chip) {
  const sid = chip && chip.dataset.sessionId;
  if (!sid) return;
  if (!chip.classList.contains('armed')) {
    chip.classList.add('armed');
    chip.textContent = 'Restore?';
    chip.title = 'Click again to restore this session';
    clearTimeout(chip._disarmTimer);
    chip._disarmTimer = setTimeout(() => {
      chip.classList.remove('armed');
      chip.textContent = 'bin';
      chip.title = 'In recycling bin — click to restore';
    }, 3000);
    return;
  }
  // Confirmed — restore now.
  clearTimeout(chip._disarmTimer);
  chip.classList.remove('armed');
  chip.textContent = 'bin';
  chip.title = 'In recycling bin — click to restore';
  if (await _restoreOne(sid)) _toast('Session restored');
  await _loadAndRender();
  _updateTrashButton();
  if (typeof populateSessionSelect === 'function') {
    try { populateSessionSelect(app.currentUserId); } catch (_) {}
  }
}

function trashTitle() {
  const n = document.querySelectorAll('.sessions-check-cell.checked').length;
  const origins = _selectedOrigins();
  if (_viewId === 'recycle-bin') return 'Select items to delete';
  if (_viewId === 'both') {
    if (n === 0) return 'Select sessions to recycle or permanently delete';
    if (origins.has('bin') && !origins.has('active')) return 'Permanently delete selected';
    if (origins.has('bin')) return 'Recycle active · permanently delete binned';
    return 'Move selected to the recycling bin';
  }
  return 'Open recycling bin';
}

async function _switchView(viewId) {
  if (_viewId === viewId) return;
  const spinner = _qs('sessions-loading-spinner');
  if (spinner) spinner.style.display = 'inline-block';
  _viewId = viewId;
  _lastTheadConfigId = null; // force full thead rebuild on view change
  _sortColumn = 'last_active';
  _sortAsc = false;
  _saveViewState();
  await _loadAndRender();
  if (spinner) spinner.style.display = 'none';
}

async function _enterBin() { return _switchView('recycle-bin'); }
async function _exitBin() { return _switchView('sessions'); }

function _toggleCheck(cell) {
  cell.classList.toggle('checked');
  const tr = cell.closest('tr');
  if (tr) tr.classList.toggle('selected', cell.classList.contains('checked'));
  const trash = _qs('sessions-trash-btn');
  if (trash) resetDeleteBtn(trash, { size: '16px', title: trashTitle() });
  _updateTrashButton();
}

// ── Bulk subset selection ──────────────────────────────────────────
// Identify which "family" a session belongs to. Optimizer runs are the
// Planner (optimizer-*) / Closer (closer-*) sessions, which are top-level
// rows (matched by id prefix, since the backend no longer tags them as
// children); spawns are orchestrator helpers (child_role spawn / spawn-* ids).
function _matchKind(s, kind) {
  if (kind === 'all') return true;
  const sid = s.session_id || '';
  if (kind === 'optimizer') {
    return s.child_role === 'planner' || s.child_role === 'closer' ||
      /^(optimizer|closer)-/.test(sid);
  }
  if (kind === 'spawn') {
    return s.child_role === 'spawn' || /^spawn-/.test(sid);
  }
  return false;
}

// Check every session matching `kind` (optimizer | spawn | all | none). Expands
// all parent groups first so collapsed children are in the DOM and can be
// selected, then deletion works on the whole family in one click.
function _selectSubset(kind) {
  // Clear any current selection (data rows + the header toggle).
  document.querySelectorAll('.sessions-check-cell.checked').forEach(c => {
    c.classList.remove('checked');
    c.closest('tr')?.classList.remove('selected');
  });
  const checkAll = _qs('sessions-check-all');
  if (checkAll) checkAll.classList.remove('checked');

  if (kind === 'none') { _updateTrashButton(); return; }

  // Reveal collapsed children so their checkboxes exist, then re-render.
  for (const s of _sessionsData) {
    if ((s.child_count || 0) > 0) _expandedGroups.add(s.session_id);
  }
  _renderTable(_sessionsData);

  const want = new Set(_sessionsData.filter(s => _matchKind(s, kind)).map(s => s.session_id));
  document.querySelectorAll('.sessions-check-cell').forEach(c => {
    const sid = c.dataset.sessionId;
    if (sid && want.has(sid)) {
      c.classList.add('checked');
      c.closest('tr')?.classList.add('selected');
    }
  });
  const trash = _qs('sessions-trash-btn');
  if (trash) resetDeleteBtn(trash, { size: '16px', title: trashTitle() });
  _updateTrashButton();
}

// ── Column resize ──────────────────────────────────────────────────
// Drag the right edge of any .col-resizable <th>. With table-layout:fixed,
// setting the header's width drives the whole column. Widths persist per
// column-class in tenant-scoped IDB UI state so they survive reloads and re-renders (only
// the <tbody> is rebuilt; the <thead> widths stay put).
const _COLW_KEY = 'sessions.colWidths';
let _resizing = null;

function _loadColWidths() {
  try {
    const saved = kvRead('agents:sessions-column-widths', _COLW_KEY);
    return (typeof saved === 'string' ? JSON.parse(saved || '{}') : saved) || {};
  } catch (_) { return {}; }
}
function _saveColWidths(m) {
  kvWrite('agents:sessions-column-widths', _COLW_KEY, m);
}
// A column's stable identity = its semantic col-* class (not col-resizable).
function _colKey(th) {
  return Array.from(th.classList).find(c => c.startsWith('col-') && c !== 'col-resizable') || '';
}
function _applyColWidths() {
  const m = _loadColWidths();
  document.querySelectorAll('#sessions-table thead th').forEach(th => {
    const k = _colKey(th);
    if (k && m[k]) { th.style.width = m[k] + 'px'; th.style.minWidth = m[k] + 'px'; }
  });
}
function _onResizeMove(e) {
  if (!_resizing) return;
  const cx = e.clientX ?? e.touches?.[0]?.clientX ?? 0;
  const w = Math.max(48, _resizing.startW + (cx - _resizing.startX));
  _resizing.th.style.width = w + 'px';
  _resizing.th.style.minWidth = w + 'px';
}
function _onResizeUp() {
  if (!_resizing) return;
  if (_resizing.key) {
    const m = _loadColWidths();
    m[_resizing.key] = _resizing.th.offsetWidth;
    _saveColWidths(m);
  }
  _resizing.th.classList.remove('resizing');
  _resizing = null;
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  window.removeEventListener('mousemove', _onResizeMove);
  window.removeEventListener('mouseup', _onResizeUp);
  window.removeEventListener('touchmove', _onResizeMove);
  window.removeEventListener('touchend', _onResizeUp);
}
function _wireColumnResize() {
  const thead = document.querySelector('#sessions-table thead');
  if (!thead || thead._resizeWired) return;
  thead._resizeWired = true;

  function _startResize(e, clientX) {
    const th = e.target.closest('th.col-resizable');
    if (!th) return;
    const rect = th.getBoundingClientRect();
    // Only start when grabbing the ~8px right-edge handle (the col-resize zone).
    if (clientX < rect.right - 8) return;
    e.preventDefault();
    th.classList.add('resizing');
    _resizing = { th, startX: clientX, startW: th.offsetWidth, key: _colKey(th) };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('mousemove', _onResizeMove);
    window.addEventListener('mouseup', _onResizeUp);
    window.addEventListener('touchmove', _onResizeMove, { passive: false });
    window.addEventListener('touchend', _onResizeUp);
  }

  thead.addEventListener('mousedown', (e) => {
    _startResize(e, e.clientX);
  });

  thead.addEventListener('touchstart', (e) => {
    const touch = e.touches?.[0];
    if (!touch) return;
    _startResize(e, touch.clientX);
  }, { passive: false });
}

// ── Wire DOM events (run once) ─────────────────────────────────────

function _wireDom() {
  if (_initialized) return;
  _initialized = true;

  // Refresh button — full reload of the sessions page state, as if the page
  // were refreshed in the browser: clears the search query, selection and
  // expanded groups, resets the sort to the persisted default, then re-fetches
  // fresh data. Deliberately NOT window.location.reload() — that would reload
  // the whole app (chat, agents, …), not just this page.
  const refreshBtn = _qs('sessions-refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      // Clear the search field + query + any in-flight message scan.
      const searchInput = _qs('sessions-search-input');
      const searchClear = _qs('sessions-search-clear');
      if (searchInput) searchInput.value = '';
      if (searchClear) searchClear.style.display = 'none';
      _searchQuery = '';
      _msgScanBusy = null;
      _setMsgScanning(false);
      // Clear selection (data rows + the header toggle).
      document.querySelectorAll('.sessions-check-cell.checked').forEach(c => {
        c.classList.remove('checked');
        c.closest('tr')?.classList.remove('selected');
      });
      const checkAll = _qs('sessions-check-all');
      if (checkAll) checkAll.classList.remove('checked');
      _expandedGroups.clear();
      // Reset the sort back to the persisted default (what a browser refresh
      // would re-read from the persisted UI-state row).
      _sortColumn = _stored.sortColumn || 'last_active';
      _sortAsc = _stored.sortAsc === true;
      // Spin the icon while the fresh data loads.
      const ic = refreshBtn.querySelector('i');
      if (ic) ic.classList.add('lucide-spin');
      refreshBtn.disabled = true;
      try {
        await _loadAndRender();
        _updateTrashButton();
      } finally {
        refreshBtn.disabled = false;
        if (ic) ic.classList.remove('lucide-spin');
      }
    });
  }

  // Wire row clicks (delegated) — skip clicks on check cells
  const tbody = _qs('sessions-table-body');
  if (tbody) {
    tbody.addEventListener('click', (e) => {
      // Expand/collapse caret takes priority over row-switch & checkbox.
      const expandBtn = e.target.closest('.sessions-row-expand');
      if (expandBtn) {
        _toggleGroup(expandBtn.dataset.sessionId);
        return;
      }
      const checkCell = e.target.closest('.sessions-check-cell');
      if (checkCell) {
        _toggleCheck(checkCell);
        return;
      }
      // Links column: open the genui / browser for that session.
      const linkBadge = e.target.closest('.sess-link-badge');
      if (linkBadge) {
        e.stopPropagation();
        const tab = linkBadge.dataset.linkKind === 'genui' ? 'genui' : 'browser';
        _switchToSession(linkBadge.dataset.sessionId, linkBadge.dataset.agentId, { tab });
        return;
      }
      // "In bin" chip on cross-catalog search rows — two-click restore.
      const binChip = e.target.closest('.sess-origin-badge.bin');
      if (binChip) {
        e.stopPropagation();
        _onBinChipClick(binChip);
        return;
      }
      // Live time-ago — click to flash the run status to its left for 1s
      // (e.g. "Completed 8m ago") instead of switching to the session.
      const timeEl = e.target.closest('.sess-title-time[data-ts]');
      if (timeEl) {
        e.stopPropagation();
        _flashStatus(timeEl);
        return;
      }
      _onRowClick(e);
    });
  }

  // Column resize (drag the right edge of any .col-resizable header).
  _wireColumnResize();
  _applyColWidths();

  // Column header sort — click any .col-sortable <th> to sort by that column.
  const thead = document.querySelector('#sessions-table thead');
  if (thead) {
    thead.addEventListener('click', (e) => {
      const th = e.target.closest('th.col-sortable');
      if (!th || !th.dataset.sortKey) return;
      // Don't intercept column-resize drags on the right edge.
      const rect = th.getBoundingClientRect();
      if (th.classList.contains('col-resizable') && e.clientX >= rect.right - 8) return;
      _setSort(th.dataset.sortKey);
    });
  }

  // Refresh when a session is mutated anywhere else (e.g. deleted from the chat
  // header / a session-row menu). Only reload while this tab is on screen;
  // startSessions() reloads on activation, so background fetches are wasteful.
  onSessionsChanged(() => {
    if (!_initialized) return;
    const tab = document.getElementById('tab-agents');
    if (tab && tab.classList.contains('active')) _loadAndRender();
  });

  // Header checkbox — toggle all on/off (this replaces the old toolbar
  // Select-all / Deselect-all buttons).
  const checkAll = _qs('sessions-check-all');
  if (checkAll) {
    checkAll.addEventListener('click', () => {
      const allChecked = document.querySelectorAll('.sessions-check-cell');
      const currentlyChecked = document.querySelectorAll('.sessions-check-cell.checked');
      const checkTo = currentlyChecked.length < allChecked.length;
      allChecked.forEach(c => {
        c.classList.toggle('checked', checkTo);
        const tr = c.closest('tr');
        if (tr) tr.classList.toggle('selected', checkTo);
      });
      checkAll.classList.toggle('checked', checkTo);
      const trash = _qs('sessions-trash-btn');
      if (trash) resetDeleteBtn(trash, { size: '16px', title: trashTitle() });
      _updateTrashButton();
    });
  }

  // Trash / delete button — green recycle (open bin) when nothing is selected;
  // red trash (two-click confirm) to move selected to the bin, or — when
  // already in the bin — to permanently delete them. (HARMONY-SESSIONS-BIN)
  const trash = _qs('sessions-trash-btn');
  if (trash) {
    trash.addEventListener('click', () => {
      const n = _getSelectedSessionIds().length;
      if (n === 0) {
        // Nothing selected — in the plain Sessions view this opens the bin;
        // in the merged 'both' view the bin is already visible, so no-op.
        if (_viewId === 'sessions') { _switchView('recycle-bin'); return; }
        return;
      }
      const origins = _selectedOrigins();
      let armTitle = 'Click again to move to the bin';
      if (_viewId === 'recycle-bin') armTitle = 'Click again to permanently delete';
      else if (_viewId === 'both') {
        armTitle = (origins.has('bin') && !origins.has('active'))
          ? 'Click again to permanently delete'
          : (origins.has('bin') ? 'Click again: recycle active, delete binned' : 'Click again to move to the bin');
      }
      advanceDeleteBtn(trash, {
        size: '16px', spinSize: '16px',
        armTitle,
        onConfirm: () => _deleteSelectedSessions(trash),
      });
    });
  }

  // View switcher button — one chip (like the search T+C/Title chip) that
  // cycles Sessions → All → Bin, filtering the ONE combined dataset.
  const viewBtn = _qs('sessions-view-btn');
  if (viewBtn) {
    viewBtn.addEventListener('click', async () => {
      const idx = _VIEW_CYCLE.indexOf(_viewId);
      const next = _VIEW_CYCLE[(idx + 1) % _VIEW_CYCLE.length];
      if (next === _viewId) return;
      await _switchView(next);
      _updateViewButton();
    });
  }

  // Search input — filter sessions by title as the user types. When a query is
  // active, results also surface from the OTHER catalog (sessions ↔ recycle
  // bin), fetched lazily so one search covers both views at once. A clear (×)
  // button on the right empties the field.
  const searchInput = _qs('sessions-search-input');
  const searchClear = _qs('sessions-search-clear');
  if (searchInput) {
    const syncSearchClear = () => {
      if (searchClear) searchClear.style.display = searchInput.value ? 'inline-flex' : 'none';
    };
    // Debounced: re-render 150ms after the user stops typing.
    let _searchTimer = null;
    searchInput.addEventListener('input', () => {
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => {
        _searchQuery = searchInput.value;
        _renderTable(_sessionsData);
        const q = _searchQuery.trim();
        if (q) {
          _ensureSearchExtra().then(() => {
            if (_searchQuery.trim() === q) {
              _renderTable(_sessionsData);
              _scanMessagesForQuery(q);
            }
          });
        }
      }, 150);
      syncSearchClear();
    });
    // Clear (×) — empty the field, drop the query, cancel any message scan,
    // and show the full view again.
    const clearSearch = () => {
      searchInput.value = '';
      _searchQuery = '';
      _msgScanBusy = null;          // stop the in-flight message scan
      _setMsgScanning(false);
      _renderTable(_sessionsData);
      syncSearchClear();
    };
    if (searchClear) {
      searchClear.addEventListener('click', () => {
        clearSearch();
        searchInput.focus();
      });
    }
    syncSearchClear();

    // Search trigger — the toolbar starts collapsed to an icon-only button
    // at the start of the actions group; clicking it reveals the T+C/Title
    // filter chip + the search field (#sessions-search-area). A second click
    // (or Escape in the field) collapses again and clears any active query.
    const searchTrigger = _qs('sessions-search-trigger');
    const toolbarActions = _qs('sessions-toolbar-actions');
    if (searchTrigger && toolbarActions) {
      // Start collapsed: hide the search area, show only the trigger icon.
      toolbarActions.classList.add('search-collapsed');
      const setSearchOpen = (open) => {
        if (open) {
          toolbarActions.classList.remove('search-collapsed');
        } else {
          toolbarActions.classList.add('search-collapsed');
        }
        searchTrigger.classList.toggle('active', open);
        searchTrigger.setAttribute('aria-expanded', open ? 'true' : 'false');
        searchTrigger.title = open ? 'Hide search' : 'Search sessions';
        if (open) searchInput.focus();
      };
      searchTrigger.addEventListener('click', () => {
        if (!toolbarActions.classList.contains('search-collapsed')) {
          clearSearch();
          setSearchOpen(false);
        } else {
          setSearchOpen(true);
        }
      });
      searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !toolbarActions.classList.contains('search-collapsed')) {
          clearSearch();
          setSearchOpen(false);
        }
      });
    }
  }

  // Search-mode toggle — single button that switches between "Title" and "T+C".
  const searchModeBtn = _qs('sessions-search-mode-btn');
  if (searchModeBtn) {
    _updateSearchModeBtn();
    searchModeBtn.addEventListener('click', function() {
      _searchMode = _searchMode === 'title' ? 'content' : 'title';
      _saveViewState();
      _updateSearchModeBtn();
      if (_searchMode === 'title') {
        _msgScanBusy = null;
        _setMsgScanning(false);
      }
      _renderTable(_sessionsData);
      const q = _searchQuery.trim();
      if (q && _searchMode !== 'title') {
        _scanMessagesForQuery(q);
      }
    });
  }

  // Restore button — bring the selected recycled sessions back.
  const restoreBtn = _qs('sessions-restore-btn');
  if (restoreBtn) restoreBtn.addEventListener('click', () => {
    if (_getSelectedSessionIds().length > 0) _restoreSelected();
  });

  _wireGutter();
  window.addEventListener('resize', function() { if (_activeConfig) _updateResponsiveColumns(); });
}

function _wireGutter() {
  const gutter = document.getElementById('sessions-gutter');
  if (!gutter) return;
  const _NARROW_WIDTH = 500;   // below this the view becomes title-only (see .narrow CSS)
  const cfg = _activeConfig ? getGutterConfig(_activeConfig) : { default_open: true, auto_collapse_at: 800 };
  const stickyTotal = _activeConfig ? getStickyTotalWidth(_activeConfig) : 86;
  const minOffset = -stickyTotal;
  // Rest position of the divider when open: the right edge of the checkbox
  // column (table inset + check 32px). Matches the CSS rule
  // `#sessions-gutter { left: calc(var(--sessions-content-inset) + 32px); }`.
  const _tab = document.getElementById('sessions-section');
  const _inset = _tab ? (parseFloat(getComputedStyle(_tab).getPropertyValue('--sessions-content-inset')) || 20) : 20;
  const boundary = _inset + 32;
  function _setDividerFromOffset(offset) {
    gutter.style.left = Math.max(0, boundary + offset) + 'px';
  }
  const _tableWrap = document.getElementById('sessions-table-wrap');
  function _syncGutterClosed() {
    if (_tableWrap) _tableWrap.classList.toggle('gutter-closed', !_gutterOpen);
  }
  let _gutterOpen = cfg.default_open;
  let _narrowActive = false;
  let _preNarrowOpen = _gutterOpen;
  // Single open/close path — every call site (init, drag release, click,
  // auto-collapse, narrow mode) funnels through here so the state machine
  // (--sticky-offset, .expanded, .gutter-closed) can never drift.
  function _setGutterOpen(open, opts) {
    opts = opts || {};
    _gutterOpen = open;
    document.documentElement.style.setProperty('--sticky-offset', (open ? 0 : minOffset) + 'px');
    gutter.classList.toggle('expanded', open);
    if (opts.spring) {
      const cls = opts.spring === 'click' ? 'spring-back-click' : 'spring-back';
      const ms = opts.spring === 'click' ? 750 : 350;
      gutter.classList.add(cls);
      setTimeout(function() { gutter.classList.remove(cls); }, ms);
    }
    _syncGutterClosed();
    // Opening/closing the checkbox changes how much of the view the title can
    // occupy — recompute the title width immediately while in narrow mode.
    if (_narrowActive) _updateNarrowMode();
  }
  _setGutterOpen(_gutterOpen);
  function _autoCollapse() {
    if (window.innerWidth <= cfg.auto_collapse_at && _gutterOpen) _setGutterOpen(false);
  }
  // ── Narrow title-only viewer ──────────────────────────────────────
  // Below the narrow threshold the sessions view becomes a pure session-title
  // viewer: the checkbox is auto-tucked away on entry (its pre-narrow state is
  // remembered and restored on widening) but the gutter handle STAYS — showing
  // the checkbox again makes the title column narrower (its right edge stays
  // put at the view edge) instead of pushing it right. The title width
  // re-tracks the view on every resize while narrow; the scroll position is
  // only snapped to the left edge when ENTERING the mode, so a user who
  // scrolled right to inspect other columns isn't yanked back.
  function _viewWidth() {
    const w = _tableWrap ? _tableWrap.clientWidth : 0;
    return w > 0 ? w : window.innerWidth;   // clientWidth is 0 while the tab is hidden
  }
  // The checkbox side's ACTUAL rendered width (inset + sticky check column).
  // Measured, not config-derived: the check <th> carries an inline
  // min-width:48px from _buildThead that beats the stylesheet's 32px, and
  // stickyTotal is captured before _activeConfig loads (stale fallback 86).
  // The check cell is sticky at the left edge, so its width is stable at any
  // scroll position.
  function _measureCheckWidth() {
    if (_tableWrap) {
      const el = _tableWrap.querySelector('th.col-check, td.col-check');
      if (el) {
        const w = el.getBoundingClientRect().width;
        if (w > 0) return w;
      }
    }
    return stickyTotal;
  }
  function _updateNarrowMode() {
    const viewW = _viewWidth();
    if (viewW <= _NARROW_WIDTH) {
      if (!_narrowActive) {
        _narrowActive = true;
        _preNarrowOpen = _gutterOpen;
        _setGutterOpen(false);
        if (_tableWrap) { _tableWrap.classList.add('narrow'); _tableWrap.scrollLeft = 0; }
      }
      // The title column must end exactly at the view's right edge. Its left
      // edge sits at (inset + checkbox column) when the gutter is open, or 0
      // when the checkbox is tucked away (gutter-closed collapses the column
      // width and the inner's left padding). The checkbox width is measured
      // from the live DOM (see _measureCheckWidth) — config totals and the
      // pre-config fallback are both wrong for the real rendered column.
      // Showing the gutter makes the title narrower instead of pushing it right.
      const titleLeft = _gutterOpen ? (_inset + _measureCheckWidth()) : 0;
      const titleW = Math.max(48, Math.min(viewW - titleLeft, _NARROW_WIDTH));
      document.documentElement.style.setProperty('--sessions-title-width', titleW + 'px');
    } else if (_narrowActive) {
      _narrowActive = false;
      _setGutterOpen(_preNarrowOpen && window.innerWidth > cfg.auto_collapse_at);
      document.documentElement.style.setProperty('--sessions-title-width', '');
      if (_tableWrap) _tableWrap.classList.remove('narrow');
    }
  }
  _narrowUpdater = _updateNarrowMode;
  // Watch the table wrap's box size directly — NOT just window resize. The
  // #chat-resize-handle drag (ui/shared/js/chatResize.js) resizes #main-panel
  // by setting chat-panel width inline and never fires a window resize event,
  // so only an element-level observer catches the split-layout drag path.
  if (typeof ResizeObserver !== 'undefined' && _tableWrap) {
    new ResizeObserver(function() { _updateNarrowMode(); }).observe(_tableWrap);
  }
  window.addEventListener('resize', function() { _autoCollapse(); _updateNarrowMode(); });
  _autoCollapse();
  _updateNarrowMode();
  let dragging = false, startX, startOffset;
  gutter.addEventListener('mousedown', function(e) {
    dragging = true; startX = e.clientX;
    startOffset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sticky-offset')) || 0;
    document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none';
    gutter.classList.add('dragging');
    e.preventDefault();
  });
  window.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    const offset = Math.max(minOffset, Math.min(0, startOffset + (e.clientX - startX)));
    document.documentElement.style.setProperty('--sticky-offset', offset + 'px');
    _setDividerFromOffset(offset);
  });
  window.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false; document.body.style.cursor = ''; document.body.style.userSelect = '';
    const offset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sticky-offset')) || 0;
    gutter.classList.remove('dragging'); gutter.style.left = '';
    _setGutterOpen(offset >= (minOffset / 2), { spring: true });
  });
  gutter.addEventListener('click', function(e) {
    if (Math.abs(e.clientX - (startX || e.clientX)) > 3) return;
    _setGutterOpen(!_gutterOpen, { spring: 'click' });
  });
  gutter.addEventListener('touchstart', function(e) {
    const t = e.touches[0]; if (!t) return;
    dragging = true; startX = t.clientX;
    startOffset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sticky-offset')) || 0;
    gutter.classList.add('dragging');
  }, { passive: true });
  window.addEventListener('touchmove', function(e) {
    if (!dragging) return; const t = e.touches[0]; if (!t) return;
    const offset = Math.max(minOffset, Math.min(0, startOffset + (t.clientX - startX)));
    document.documentElement.style.setProperty('--sticky-offset', offset + 'px');
    _setDividerFromOffset(offset);
  }, { passive: true });
  window.addEventListener('touchend', function() {
    if (!dragging) return;
    dragging = false;
    const offset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sticky-offset')) || 0;
    gutter.classList.remove('dragging'); gutter.style.left = '';
    _setGutterOpen(offset >= (minOffset / 2), { spring: true });
  });
}

// ── Exports (matches the start/stop pattern used by other tabs) ────

export function startSessions() {
  if (!app.currentUserId) return;
  if (_sourceAccessDenied) {
    stopSessions();
    _renderAccessDeniedState();
    return;
  }
  const section = _qs('sessions-section');
  if (section) delete section.dataset.accessBlocked;
  _wireDom();
  // On a cold boot the cache may still be hydrating. Paint it as soon as it is
  // ready unless the live request already won the race.
  ensureAgentCacheHydrated().then(async () => {
    if (_sessionsDataViewId === _viewId && _sessionsDataAgentId === _agentFilterId) return;
    const key = _cacheKey(_viewId, _agentFilterId, _nativeCodexContext);
    const rows = key ? readAgentCache(key) : null;
    if (!Array.isArray(rows)) return;
    _activeConfig = await loadViewerConfig(_viewId);
    if (_sessionsDataViewId === _viewId) return;
    _sessionsData = rows;
    _sessionsDataViewId = _viewId;
    _sessionsDataAgentId = _agentFilterId;
    _renderTable(rows);
    _markSessionsPaint('indexeddb');
  }).catch(() => {});
  _loadAndRender();
  // Kill switch: refresh immediately when it toggles so running spinners
  // clear (or return) without waiting for the 30s auto-refresh.
  if (!_onKsChanged) {
    _onKsChanged = () => { try { _loadAndRender(); } catch (_) { /* ignore */ } };
    window.addEventListener('kill-switch-changed', _onKsChanged);
  }
  // Re-measure the narrow title-only viewer now that the tab is visible
  // (it is display:none while inactive, so clientWidth is only meaningful
  // at activation time).
  if (_narrowUpdater) _narrowUpdater();

  // Start auto-refresh
  if (!_refreshTimer) {
    _refreshTimer = setInterval(_loadAndRender, _REFRESH_INTERVAL_MS);
  }
  // Start the 1s live ticker for the relative time-ago labels. Runs only while
  // this tab is initialized — stopSessions() clears it.
  if (!_tickTimer) {
    _tickTimer = setInterval(_tickRelativeTimes, 1000);
  }
}

export function stopSessions() {
  if (_refreshTimer) {
    clearInterval(_refreshTimer);
    _refreshTimer = null;
  }
  if (_tickTimer) {
    clearInterval(_tickTimer);
    _tickTimer = null;
  }
  if (_onKsChanged) {
    window.removeEventListener('kill-switch-changed', _onKsChanged);
    _onKsChanged = null;
  }
}

// Aliases for backward compat
const activateSessionsPage = startSessions;
const initSessionsPage = startSessions;
