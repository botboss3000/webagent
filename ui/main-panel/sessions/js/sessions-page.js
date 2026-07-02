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

import { app } from '../../../shared/js/state.js';
import { apiPath } from '../../../shared/js/config.js';
import { loadSessionChat } from '../../../chat-side-panel/js/session-load.js';
import { populateSessionSelect } from '../../../chat-side-panel/js/session-list.js';
import { _esc, _fmtTime } from '../../../shared/js/dom-utils.js';
import { icon, claudeMark } from '../../../shared/js/icons.js';
import { ICON_PICKER_ICONS } from '../../../shared/js/icon-picker.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../../shared/js/delete-control.js';
import { onSessionsChanged } from '../../../shared/js/session-events.js';

// ── Helpers ────────────────────────────────────────────────────────

function _qs(id) { return document.getElementById(id); }

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
let _refreshTimer = null;
let _binView = false;   // false = active sessions, true = recycling bin
const _REFRESH_INTERVAL_MS = 30000;
// Which parent rows are currently expanded in the tree. Persists across the
// 30s auto-refresh so an open group stays open. Mirrors the chat session list's
// _expandedGroups (ui/chat-side-panel/js/session-list.js).
const _expandedGroups = new Set();

// ── Fetch & render ─────────────────────────────────────────────────

async function _fetchSessions() {
  const userId = app.currentUserId;
  if (!userId) return [];

  const token = localStorage.getItem('auth_token');
  const status = _binView ? 'recycled' : 'active';
  let url = `/api/v1/db/session-stats?db=local.db&user_id=${encodeURIComponent(userId)}&status=${status}`;
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
    if (empty) {
      const txt = empty.querySelector('.sessions-empty-text');
      const hint = empty.querySelector('.sessions-empty-hint');
      if (txt) txt.textContent = _binView ? 'Recycling bin is empty' : 'No sessions yet';
      if (hint) hint.textContent = _binView
        ? 'Recycled sessions appear here until permanently deleted'
        : 'Start a conversation with an agent to see it here';
      empty.style.display = 'flex';
    }
    return;
  }
  if (empty) empty.style.display = 'none';

  // ── Group into a parent/child tree ──────────────────────────────────
  // Spawns (orchestrator helpers) and Closers (optimizer runs) come back with
  // a parent_session_id. We render only the top-level rows; a parent that has
  // children present in this view gets an expand caret, and its children are
  // rendered (indented) right beneath it when expanded. If a child's parent
  // isn't in this view (e.g. recycled), the child falls back to top-level so
  // nothing silently disappears.
  const byId = new Map(sessions.map(s => [s.session_id, s]));
  const childrenOf = new Map();   // parent sid -> [child rows]
  const topLevel = [];
  for (const s of sessions) {
    const pid = s.parent_session_id;
    if (pid && byId.has(pid)) {
      if (!childrenOf.has(pid)) childrenOf.set(pid, []);
      childrenOf.get(pid).push(s);
    } else {
      topLevel.push(s);
    }
  }

  let html = '';
  for (const s of topLevel) {
    const kids = childrenOf.get(s.session_id) || [];
    html += _renderRow(s, { childCount: kids.length });
    if (kids.length && _expandedGroups.has(s.session_id)) {
      for (const k of kids) html += _renderRow(k, { isChild: true });
    }
  }
  tbody.innerHTML = html;

  // Re-render lucide icons in the new rows
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons({ nodes: [tbody] });
  }
}

// Build one table row. `childCount` (parents) drives the expand caret;
// `isChild` renders the indented, role-tagged child variant.
function _renderRow(s, { childCount = 0, isChild = false } = {}) {
  const agentLabel = s.agent_name || s.agent_id || '—';
  const sid = _esc(s.session_id);

  let caretCell;
  if (isChild) {
    caretCell = '<td class="col-caret"></td>';
  } else if (childCount > 0) {
    const open = _expandedGroups.has(s.session_id);
    caretCell = `<td class="col-caret"><span class="sessions-row-expand" data-session-id="${sid}" title="${open ? 'Collapse' : 'Expand'} (${childCount})">
        <i data-lucide="${open ? 'chevron-down' : 'chevron-right'}" style="width:14px;height:14px;"></i></span></td>`;
  } else {
    caretCell = '<td class="col-caret"></td>';
  }

  const rowCls = isChild
    ? 'sessions-child-row'
    : (childCount > 0 ? 'group-parent' + (_expandedGroups.has(s.session_id) ? ' expanded' : '') : '');

  // Indented title with a tree rail + role icon for children.
  let titleInner = _esc(s.title);
  if (isChild) {
    const roleIcon = s.child_role === 'closer' ? 'flag' : s.child_role === 'planner' ? 'wand-2' : 'git-branch';
    titleInner = `<span class="sess-child-rail"></span><i data-lucide="${roleIcon}" class="sess-child-role-icon" style="width:12px;height:12px;"></i>${titleInner}`;
  }

  return `<tr data-session-id="${sid}" data-agent-id="${_esc(s.agent_id || '')}"${rowCls ? ` class="${rowCls}"` : ''}>
      ${caretCell}
      <td class="col-check">
        <span class="sessions-check-cell" data-session-id="${sid}"> </span>
      </td>
      <td class="col-agent">
        <span class="sess-agent-badge">
          ${_agentIconHtml(s.agent_icon, s.agent_engine, '12px')}
          ${_esc(agentLabel)}
        </span>
        ${_deviceBadge(s)}
      </td>
      <td class="col-title" title="${_esc(s.title)}">${titleInner}</td>
      <td class="col-status">${_statusBadge(s.run_status)}</td>
      <td class="col-links">${_linkBadges(s)}</td>
      <td class="col-msgs">${s.message_count || '—'}</td>
      <td class="col-tokens-in">${_fmtTokens(s.total_input_tokens)}</td>
      <td class="col-tokens-out">${_fmtTokens(s.total_output_tokens)}</td>
      <td class="col-cost">${_fmtCost(s.total_cost)}</td>
      <td class="col-duration">${_fmtDuration(s.total_duration_ms)}</td>
      <td class="col-updated">${_fmtTime(s.last_active)}</td>
    </tr>`;
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

async function _loadAndRender() {
  const loading = _qs('sessions-loading');
  if (loading) loading.style.display = 'flex';

  _sessionsData = await _fetchSessions();
  _renderTable(_sessionsData);
  // Reload replaces every row, so any prior selection is gone — clear the
  // header toggle to match and repaint the toolbar (resting recycle icon).
  const checkAll = _qs('sessions-check-all');
  if (checkAll) checkAll.classList.remove('checked');
  _updateTrashButton();
}

// ── Row click: switch to that session ──────────────────────────────

// Make `sessionId` the active chat session (switching agent if needed) and
// optionally jump to a main-panel tab afterwards ('genui' = Gen UI,
// 'browser' = Browser page). Shared by row clicks and the Links badges.
function _switchToSession(sessionId, agentId, { tab = null, title = null } = {}) {
  if (_binView || !sessionId) return;  // recycled sessions aren't loadable
  if (agentId && app.currentAgentId !== agentId) {
    app.currentAgentId = agentId;
  }
  app.currentSessionId = sessionId;
  app.sessionTitle = title || sessionId.slice(0, 12);

  loadSessionChat(sessionId);
  populateSessionSelect(app.currentUserId);

  if (tab && typeof window.__setMainTab === 'function') {
    window.__setMainTab(tab);
  }
}

function _onRowClick(e) {
  const tr = e.target.closest('tr[data-session-id]');
  if (!tr || !tr.dataset.sessionId) return;
  _switchToSession(tr.dataset.sessionId, tr.dataset.agentId, {
    title: tr.querySelector('.col-title')?.textContent,
  });
}

// ── Selection helpers ──────────────────────────────────────────────

// Selection / bin toolbar — mirrors agents' _updateBinToolbar (HARMONY-SESSIONS-BIN).
function _updateTrashButton() {
  const trash      = _qs('sessions-trash-btn');
  const restoreBtn = _qs('sessions-restore-btn');
  const backBtn    = _qs('sessions-back-btn');
  const title      = _qs('sessions-toolbar-title');
  const sub        = _qs('sessions-toolbar-sub');
  const n = document.querySelectorAll('.sessions-check-cell.checked').length;

  if (backBtn) backBtn.style.display = _binView ? 'inline-flex' : 'none';
  if (title) {
    title.textContent = _binView ? 'Back to Sessions' : 'Sessions';
    title.style.cursor = _binView ? 'pointer' : '';
  }
  if (sub) sub.textContent = _binView ? ' · Recycling bin' : ' · history · usage · costs';
  if (restoreBtn) {
    restoreBtn.style.display = _binView ? 'inline-flex' : 'none';
    restoreBtn.disabled = (n === 0);
  }

  if (trash) {
    if (_binView) {
      trash.title = n ? 'Permanently delete selected' : 'Select sessions to delete';
      trash.disabled = (n === 0);
    } else {
      trash.title = n ? 'Move selected to the recycling bin' : 'Open recycling bin';
      trash.disabled = false;
    }
    // Green recycle icon when nothing is selected, red trash icon when items are.
    trash.innerHTML = icon(n > 0 ? 'trash-2' : 'recycle', { size: '16px', style: n > 0 ? '' : 'color:var(--success)' });
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

// Delete the checked sessions. In the active view this soft-deletes (moves to
// the bin); in the bin view it permanently deletes (with ?permanent=true).
async function _deleteSelectedSessions(btn) {
  const ids = _getSelectedSessionIds();
  if (ids.length === 0) return;

  const token = localStorage.getItem('auth_token');
  let ok = 0, fail = 0;
  for (const sid of ids) {
    try {
      let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}?db=local.db`;
      if (_binView) url += '&permanent=true';
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url), { method: 'DELETE' });
      if (res.ok || res.status === 404) ok++;
      else fail++;
    } catch (_) { fail++; }
  }
  if (fail > 0) console.warn('[Sessions] Failed to delete', fail, 'sessions');
  if (ok > 0) {
    _toast(_binView
      ? `Deleted ${ok} session${ok > 1 ? 's' : ''}`
      : `Recycled ${ok} session${ok > 1 ? 's' : ''}`);
  }
  await _loadAndRender();
  // Keep the chat side-panel session list in sync with what we just removed.
  try { populateSessionSelect(app.currentUserId); } catch (_) {}
  if (btn) resetDeleteBtn(btn, { size: '16px', title: trashTitle() });
  _updateTrashButton();
}

// Restore the checked sessions from the bin back to the active list.
async function _restoreSelected() {
  const ids = _getSelectedSessionIds();
  if (ids.length === 0) return;
  const token = localStorage.getItem('auth_token');
  let ok = 0;
  for (const sid of ids) {
    try {
      let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/restore?db=local.db`;
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url), { method: 'POST' });
      if (res.ok) ok++;
    } catch (_) {}
  }
  if (ok > 0) _toast(`Restored ${ok} session${ok > 1 ? 's' : ''}`);
  await _loadAndRender();
  _updateTrashButton();
  if (typeof populateSessionSelect === 'function') {
    try { populateSessionSelect(app.currentUserId); } catch (_) {}
  }
}

function trashTitle() {
  if (_binView) return 'Select sessions to delete';
  return 'Open recycling bin';
}

async function _enterBin() {
  if (_binView) return;
  _binView = true;
  await _loadAndRender();
  _updateTrashButton();
}

async function _exitBin() {
  if (!_binView) return;
  _binView = false;
  await _loadAndRender();
  _updateTrashButton();
}

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
// Planner (optimizer-*) / Closer (closer-*) sessions (child_role planner|
// closer); spawns are orchestrator helpers (child_role spawn / spawn-* ids).
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
// column-class in localStorage so they survive reloads and re-renders (only
// the <tbody> is rebuilt; the <thead> widths stay put).
const _COLW_KEY = 'sessions.colWidths';
let _resizing = null;

function _loadColWidths() {
  try { return JSON.parse(localStorage.getItem(_COLW_KEY) || '{}') || {}; }
  catch (_) { return {}; }
}
function _saveColWidths(m) {
  try { localStorage.setItem(_COLW_KEY, JSON.stringify(m)); } catch (_) {}
}
// A column's stable identity = its semantic col-* class (not col-resizable).
function _colKey(th) {
  return Array.from(th.classList).find(c => c.startsWith('col-') && c !== 'col-resizable') || '';
}
function _applyColWidths() {
  const m = _loadColWidths();
  document.querySelectorAll('#sessions-table thead th').forEach(th => {
    const k = _colKey(th);
    if (k && m[k]) th.style.width = m[k] + 'px';
  });
}
function _onResizeMove(e) {
  if (!_resizing) return;
  const w = Math.max(48, _resizing.startW + (e.clientX - _resizing.startX));
  _resizing.th.style.width = w + 'px';
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
}
function _wireColumnResize() {
  const thead = document.querySelector('#sessions-table thead');
  if (!thead || thead._resizeWired) return;
  thead._resizeWired = true;
  thead.addEventListener('mousedown', (e) => {
    const th = e.target.closest('th.col-resizable');
    if (!th) return;
    const rect = th.getBoundingClientRect();
    // Only start when grabbing the ~8px right-edge handle (the col-resize zone).
    if (e.clientX < rect.right - 8) return;
    e.preventDefault();
    th.classList.add('resizing');
    _resizing = { th, startX: e.clientX, startW: th.offsetWidth, key: _colKey(th) };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('mousemove', _onResizeMove);
    window.addEventListener('mouseup', _onResizeUp);
  });
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
      _onRowClick(e);
    });
  }

  // Column resize (drag the right edge of any .col-resizable header).
  _wireColumnResize();
  _applyColWidths();

  // Bulk-select control — check a whole family of sessions at once
  // (optimizer runs / spawned sessions) so they can be deleted in one go.
  const bulkSel = _qs('sessions-bulk-select');
  if (bulkSel) {
    bulkSel.addEventListener('change', () => {
      const v = bulkSel.value;
      bulkSel.value = '';            // reset so the same option can be re-picked
      if (v) _selectSubset(v);
    });
  }

  // Refresh when a session is mutated anywhere else (e.g. deleted from the chat
  // header / a session-row menu). Only reload while this tab is on screen;
  // startSessions() reloads on activation, so background fetches are wasteful.
  onSessionsChanged(() => {
    if (!_initialized) return;
    const tab = document.getElementById('tab-sessions');
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
      if (!_binView && _getSelectedSessionIds().length === 0) { _enterBin(); return; }
      if (_getSelectedSessionIds().length === 0) return;  // bin view, nothing selected → no-op
      advanceDeleteBtn(trash, {
        size: '16px', spinSize: '16px',
        armTitle: _binView ? 'Click again to permanently delete' : 'Click again to move to the bin',
        onConfirm: () => _deleteSelectedSessions(trash),
      });
    });
  }

  // Back button + title click — leave the bin (mirrors the agents header).
  const backBtn = _qs('sessions-back-btn');
  if (backBtn) backBtn.addEventListener('click', () => { if (_binView) _exitBin(); });
  const title = _qs('sessions-toolbar-title');
  if (title) title.addEventListener('click', () => { if (_binView) _exitBin(); });

  // Restore button — bring the selected recycled sessions back.
  const restoreBtn = _qs('sessions-restore-btn');
  if (restoreBtn) restoreBtn.addEventListener('click', () => {
    if (_getSelectedSessionIds().length > 0) _restoreSelected();
  });
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
const activateSessionsPage = startSessions;
const initSessionsPage = startSessions;