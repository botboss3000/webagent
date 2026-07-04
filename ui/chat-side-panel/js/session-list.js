'use strict';

// Session dropdown list — fetch + render session rows (drag handles, pin icons,
// status spinners, spawn badges, relative times, a per-row "more" (⋯) kebab that
// opens the pin/unpin toggle, and a trash button), header label + notification
// badge, related-session chips. Module map: ui/chat-side-panel/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { icon, claudeMark } from '../../shared/js/icons.js';
import { ICON_PICKER_ICONS } from '../../shared/js/icon-picker.js';
import { _esc } from '../../shared/js/dom-utils.js';
import { _agentIconFor, _agentIconHtml, _agentNameFor } from './session-agent.js';
// session-core imports from this module too — an ES-module cycle. Safe because
// switchToSession is only CALLED at runtime (in the tab click handlers), by
// which point the live binding is resolved.
import { switchToSession } from './session-core.js';

let _sessionsCache = [];
let _sessionFetchSeq = 0;
// Session-list tree: which parent rows (orchestrator / optimizer Planner) are
// expanded, and a cache of their fetched children so re-renders don't refetch.
// Children are loaded lazily from the same /related endpoint that feeds the
// sub-header tab bar, so the tree and the tabs always agree.
const _expandedGroups = new Set();
const _childrenCache = new Map();   // parent sid -> [{session_id,label,role,name,status}]
// Footer eye-toggle state: when true the dropdown reveals hidden sessions and
// each row shows an eye toggle instead of a trash button (manage mode).
let _showHidden = false;
function _setShowHidden(v) { _showHidden = !!v; }
function _getShowHidden() { return _showHidden; }

function _truncate(s, n) {
  return (s && s.length > n) ? s.slice(0, n) + '\u2026' : (s || '');
}

function _formatRelativeTime(dateStr) {
  if (!dateStr) return '';
  const now = Date.now();
  const d = new Date(dateStr.endsWith('Z') ? dateStr : dateStr + 'Z');
  const diffMs = now - d.getTime();
  if (diffMs < 0) return 'just now';
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return 'just now';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hrs = Math.floor(min / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 4) return `${weeks}w ago`;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ── Related sessions (spawns + browser sessions) ───────────────────────────

// Collapse concurrent same-session refreshes into a single fetch. On load the
// init call, the first list render, and the 15s poll can all fire
// _fetchRelatedSessions() for the same session at once — firing 3 identical
// /related round-trips (each opens a fresh Postgres connection + a heavy query
// server-side, observed back-to-back in the diagnostics feed). While one is in
// flight for the CURRENT session, later callers reuse the same promise. Keyed by
// session id so a session switch still triggers a fresh fetch.
let _relatedInFlight = null; // { sid, promise }
async function _fetchRelatedSessions() {
  const _sid = app.currentSessionId;
  if (_relatedInFlight && _relatedInFlight.sid === _sid) return _relatedInFlight.promise;
  const promise = _fetchRelatedSessionsInner();
  _relatedInFlight = { sid: _sid, promise };
  promise.finally(() => {
    if (_relatedInFlight && _relatedInFlight.promise === promise) _relatedInFlight = null;
  });
  return promise;
}

async function _fetchRelatedSessionsInner() {
  const subHeader = document.getElementById('chat-sub-header');
  const parentEl = document.getElementById('chat-sub-parent');
  const spawnsEl = document.getElementById('chat-sub-spawns');
  const browsersEl = document.getElementById('chat-sub-browsers');
  if (!subHeader || !parentEl || !spawnsEl || !browsersEl) return;

  const sid = app.currentSessionId;
  if (!sid || !app.currentUserId) {
    subHeader.style.display = 'none';
    return;
  }

  try {
    const token = localStorage.getItem('auth_token');
    let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/related?db=local.db`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const resp = await fetch(apiPath(url));
    if (!resp.ok) { subHeader.style.display = 'none'; return; }
    const data = await resp.json();

    // Unified family-member list. Newer backends return `children` (covers
    // spawned helpers AND optimizer worker/closer sessions, each with a label +
    // role); fall back to the legacy `spawns` array so an un-restarted backend
    // still renders spawn tabs.
    const children = (data.children && data.children.length)
      ? data.children
      : (data.spawns || []).map(s => ({
          session_id: s.spawn_session_id || s.id, label: null,
          role: 'spawn', name: s.name, status: s.status,
        }));
    const browsers = data.browser_sessions || [];
    const curSid = app.currentSessionId;
    const parent = data.parent || null;   // set when THIS session is itself a child
    const rootLabel = data.root_label || 'Main';
    const groupKind = data.group_kind
      || (children.length || parent ? 'orchestrator' : null);
    // The root tab target (orchestrator / Planner). Fallbacks keep an
    // un-restarted backend working: a child → its parent is the root; a root
    // (has members, no parent) → it IS the root.
    const orchestrator = data.orchestrator
      || (parent && parent.session_id ? { session_id: parent.session_id, title: parent.title } : null)
      || (children.length ? { session_id: curSid, title: null } : null);

    // The family renders as a persistent tab bar — a root tab ("Main" for an
    // orchestrator, "Planner" for an optimizer run) plus one tab per member —
    // identical whether the user is viewing the root OR any member, so they can
    // hop freely. The open session's tab is highlighted (chip-active). Shown
    // whenever there's a family relationship: this session HAS members, or it IS
    // a member (so it always offers at least a root tab back — even on an
    // un-restarted backend that can't yet list the siblings).
    const hasFamily = !!(orchestrator && orchestrator.session_id
                         && (children.length || (parent && parent.session_id)));

    // ── Root tab (the orchestrator / Planner session) ──
    let parentHtml = '';
    if (hasFamily) {
      const otitle = orchestrator.title || `${rootLabel} session`;
      const isActive = orchestrator.session_id === curSid;
      const activeCls = isActive ? ' chip-active' : '';
      // Root is the "Main" session for both families (an orchestrator, or the
      // base session an optimizer ran on). Only a legacy un-restarted backend
      // that still roots the optimizer family on the Planner uses the wand.
      const rootIcon = (groupKind === 'optimizer' && rootLabel !== 'Main') ? 'wand-2' : 'layout-dashboard';
      // No divider — root sits 1px from the first member (uniform tab strip).
      parentHtml = `<span class="sub-chip chip-main${activeCls}" title="${isActive ? otitle : 'Back to ' + otitle}" data-parent-id="${orchestrator.session_id}"><span class="chip-status">${icon(rootIcon, { size: '10px' })}</span>${rootLabel}</span>`;
    }
    parentEl.innerHTML = parentHtml;

    // ── Member tabs (spawn helpers / optimizer closer) ──
    let spawnHtml = '';
    if (hasFamily) {
      children.forEach((sp, i) => {
        const spSid = sp.session_id;
        const isActive = spSid === curSid;
        const statusClass = sp.status === 'running' || sp.status === 'queued' ? 'chip-running'
                         : sp.status === 'done' ? 'chip-done'
                         : sp.status === 'error' ? 'chip-error' : '';
        // Backend label (e.g. "Closer") wins; spawns fall back to "Spawn N" by
        // order. The member's real name stays in the hover tooltip.
        const label = sp.label || `Spawn ${i + 1}`;
        const tipName = sp.name || label;
        const roleIcon = sp.role === 'closer' ? 'flag'
                       : sp.role === 'planner' ? 'wand-2'
                       : sp.role === 'delegate' ? 'share-2'
                       : 'git-branch';
        const statusIcon = sp.status === 'running' || sp.status === 'queued'
          ? icon('loader-2', { size: '10px' })
          : sp.status === 'done'
            ? icon('check-circle', { size: '10px' })
            : sp.status === 'error'
              ? icon('alert-circle', { size: '10px' })
              : icon(roleIcon, { size: '10px' });
        const chipClass = `sub-chip ${statusClass}${isActive ? ' chip-active' : ''}`;
        const title = `${tipName}${sp.status ? ' \u2014 ' + sp.status : ''}`;
        spawnHtml += `<span class="${chipClass}" title="${title}" data-spawn-id="${spSid}"><span class="chip-status">${statusIcon}</span>${label}</span>`;
      });
    }
    spawnsEl.innerHTML = spawnHtml;

    let browserHtml = '';
    if (browsers.length) {
      browserHtml = '<span class="chip-sep"></span>';
      for (const bs of browsers) {
        const label = bs.title || bs.url || 'Browser';
        const isPrivate = !bs.shared;
        const chipClass = 'sub-chip chip-browser' + (isPrivate ? ' chip-private' : '');
        const title = (bs.url || label) + (isPrivate ? ' (private)' : '');
        browserHtml += `<span class="${chipClass}" title="${title}" data-bs-id="${bs.id}"><span class="chip-status">${icon('globe', { size: '10px' })}</span>${label}</span>`;
      }
    }
    browsersEl.innerHTML = browserHtml;

    parentEl.querySelectorAll('[data-parent-id]').forEach(el => {
      el.addEventListener('click', () => {
        const pid = el.dataset.parentId;
        // A full session switch (updates app.currentSessionId so the active-tab
        // highlight follows); switchToSession self-skips when already current.
        if (pid && pid !== curSid) switchToSession(pid);
      });
    });
    spawnsEl.querySelectorAll('[data-spawn-id]').forEach(el => {
      el.addEventListener('click', () => {
        const spawnSid = el.dataset.spawnId;
        if (spawnSid && spawnSid !== curSid) switchToSession(spawnSid);
      });
    });
    browsersEl.querySelectorAll('[data-bs-id]').forEach(el => {
      el.addEventListener('click', () => {
        const bsId = el.dataset.bsId;
        if (!bsId) return;
        const webTab = document.querySelector('.main-tab[data-value="browser"]');
        if (webTab) webTab.click();
        if (window.__switchWebSession) {
          window.__switchWebSession(bsId);
        }
      });
    });

    subHeader.style.display = (hasFamily || browsers.length) ? 'flex' : 'none';
  } catch (e) {
    console.warn('Failed to fetch related sessions:', e);
    subHeader.style.display = 'none';
  }
}

// ── Session title (applied from WS titler) ─────────────────────────────────

function applySessionTitle(event) {
  const sid = event && (event.session_id || event.sessionId);
  if (!sid) return;
  const status = event.status || 'done';
  const dropdown = document.getElementById('session-dropdown');
  const isCurrent = sid === app.currentSessionId;

  if (status === 'generating') {
    if (isCurrent && dropdown) dropdown.dataset.titling = 'true';
    return;
  }

  if (event.title) {
    const found = _sessionsCache.find(s => s.id === sid);
    if (found) found.title = event.title;
  }
  if (isCurrent && dropdown) delete dropdown.dataset.titling;
  if (isCurrent) _setTriggerLabel();
}

// ── Session trigger label ──────────────────────────────────────────────────

function _setTriggerLabel() {
  let labelEl = document.getElementById('session-dropdown-label');
  const trigger = document.getElementById('session-dropdown-trigger');
  if (!labelEl && trigger) {
    const input = trigger.querySelector('.session-row-title-input');
    if (input) {
      labelEl = document.createElement('span');
      labelEl.id = 'session-dropdown-label';
      input.replaceWith(labelEl);
    }
  }
  if (!labelEl) return;
  const sid = app.currentSessionId;
  const found = _sessionsCache.find(s => s.id === sid);
  const title = (found && found.title) || 'New Session';
  const agentId = (found && found.agent_id) || app.currentAgentId;
  const agentIcon = found?.agent_icon || _agentIconFor(agentId);
  const agentIconHtml = _agentIconHtml(agentId, '16px');
  labelEl.textContent = title || 'New Session';
  // Agent-name row above the session dropdown.
  const agentNameEl = document.getElementById('chat-header-agent-name');
  if (agentNameEl) {
    const agentName = found?.agent_name || _agentNameFor(agentId);
    if (agentName) {
      // Build icon HTML from session's own icon if available
      let headerIconHtml;
      if (found?.agent_icon || found?.agent_engine) {
        const name = found.agent_icon || '';
        const engine = found.agent_engine || '';
        if (engine === 'claude_code' && (!name || name === 'sparkles')) {
          headerIconHtml = claudeMark({ size: '20px' });
        } else if (!name) {
          headerIconHtml = icon('bot', { size: '20px' });
        } else if (ICON_PICKER_ICONS.includes(name)) {
          headerIconHtml = icon(name, { size: '20px' });
        } else {
          headerIconHtml = `<span style="font-size:20px;line-height:1;display:inline-flex;align-items:center;justify-content:center">${name.replace(/</g, '&lt;')}</span>`;
        }
      } else {
        headerIconHtml = agentIconHtml || _esc(agentIcon);
      }
      agentNameEl.innerHTML = `<span class="header-agent-icon">${headerIconHtml}</span> <span class="header-agent-label">${_esc(agentName)}</span>`;
      agentNameEl.title = agentName;
      agentNameEl.tabIndex = 0;
      agentNameEl.role = 'button';
      agentNameEl.setAttribute('aria-label', 'Switch agent');
      agentNameEl.style.display = '';
    } else {
      agentNameEl.textContent = '';
      agentNameEl.style.display = 'none';
    }
  }
  labelEl.title = ((found && found.id) || sid || '');
  const sessionDropdown = document.getElementById('session-dropdown');
  if (sessionDropdown) sessionDropdown.dataset.loaded = 'true';
  const statusEl = document.getElementById('session-dropdown-status');
  if (statusEl) {
    if (found && found.run_status === 'running') {
      statusEl.innerHTML = icon('loader-2', { size: '12px' });
      statusEl.className = 'session-dropdown-status session-status-running';
      statusEl.title = 'Agent is thinking\u2026';
    } else if (found && found.has_unread) {
      statusEl.innerHTML = icon('check-circle', { size: '12px' });
      statusEl.className = 'session-dropdown-status session-status-unread';
      statusEl.title = 'New response ready';
    } else {
      statusEl.innerHTML = '';
      statusEl.className = 'session-dropdown-status';
      statusEl.title = '';
    }
  }
  _fetchRelatedSessions();
}

// ── Session-list tree: lazy child fetch + expand/collapse ──────────────────

// Fetch a parent's family members (spawns / optimizer closer) via /related and
// cache them. Re-renders the list once they arrive so the nested child rows
// appear. Stores [] on failure so we don't spin.
async function _ensureGroupChildren(sid) {
  if (_childrenCache.has(sid)) return;
  _childrenCache.set(sid, null);   // in-flight marker (renders a "Loading…" row)
  let kids = [];
  try {
    const token = localStorage.getItem('auth_token');
    let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/related?db=local.db`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const resp = await fetch(apiPath(url));
    if (resp.ok) {
      const data = await resp.json();
      kids = (data.children && data.children.length)
        ? data.children
        : (data.spawns || []).map(s => ({
            session_id: s.spawn_session_id || s.id, label: null,
            role: 'spawn', name: s.name, status: s.status,
          }));
    }
  } catch (e) {
    console.warn('Failed to fetch group children:', e);
  }
  _childrenCache.set(sid, kids);
  _renderSessionRows();
}

// Toggle a parent group open/closed. Exported so the dropdown's delegated click
// handler (session-init.js) can call it from the caret button.
function toggleSessionGroup(sid) {
  if (!sid) return;
  if (_expandedGroups.has(sid)) {
    _expandedGroups.delete(sid);
  } else {
    _expandedGroups.add(sid);
    _ensureGroupChildren(sid);   // async; re-renders when ready
  }
  _renderSessionRows();
}

// Build the indented child rows for an expanded parent.
function _childRowsHtml(sid) {
  const kids = _childrenCache.get(sid);
  if (kids === null || kids === undefined) {
    return `<div class="session-child-row session-child-loading">${icon('loader-2', { size: '12px' })}<span class="session-row-sep"> </span>Loading…</div>`;
  }
  if (!kids.length) return '';
  let html = '';
  kids.forEach((k, i) => {
    const kSid = k.session_id;
    const label = k.label || `Spawn ${i + 1}`;
    const name = k.name && k.name !== label ? k.name : '';
    const roleIcon = k.role === 'closer' ? 'flag' : k.role === 'planner' ? 'wand-2' : 'git-branch';
    const isSel = kSid === app.currentSessionId ? ' selected' : '';
    let statusHtml = '';
    if (k.status === 'running' || k.status === 'queued') {
      statusHtml = `<span class="session-row-status session-status-running" title="Working…">${icon('loader-2', { size: '12px' })}</span>`;
    }
    const text = name ? `${label} · ${name}` : label;
    html += `
      <div class="session-row session-child-row${isSel}" data-id="${kSid}" title="${(name || label).replace(/"/g, '&quot;')}">
        <span class="session-child-rail"></span>
        <span class="session-row-child-icon">${icon(roleIcon, { size: '11px' })}</span>
        ${statusHtml}
        <span class="session-row-title">${text.replace(/</g, '&lt;')}</span>
      </div>`;
  });
  return html;
}

function _renderSessionRows() {
  const menu = document.getElementById('session-dropdown-menu');
  if (!menu) return;
  menu.innerHTML = '';

  const sorted = [..._sessionsCache]
    .filter(s => _showHidden || !s.hidden)
    .sort((a, b) => {
      if (a.pinned && !b.pinned) return -1;
      if (!a.pinned && b.pinned) return 1;
      const aTime = a.updated_at ? new Date(a.updated_at).getTime() : 0;
      const bTime = b.updated_at ? new Date(b.updated_at).getTime() : 0;
      return bTime - aTime;
    });
  // Only reserve the caret column (spacer on non-parent rows) when the list
  // actually holds an expandable family row — otherwise it's dead space that
  // pushes the pin/title away from the drag grip. With no families present the
  // grip sits right beside the pin/title (no gap).
  const anyParent = sorted.some(s => (s.child_count || 0) > 0);

  if (!sorted.length) {
    const empty = document.createElement('div');
    empty.className = 'session-dropdown-empty';
    empty.textContent = 'No sessions yet';
    menu.appendChild(empty);
    // Still show the manage footer below the empty message so hidden sessions
    // remain reachable when the visible list is empty.
    _appendManageRow(menu);
    return;
  }
  for (const s of sorted) {
    const hasChildren = (s.child_count || 0) > 0;
    const isExpanded = hasChildren && _expandedGroups.has(s.id);
    const row = document.createElement('div');
    row.className = 'session-row'
      + (s.pinned ? ' pinned' : '')
      + (hasChildren ? ' group-parent' : '')
      + (isExpanded ? ' expanded' : '')
      + (s.id === app.currentSessionId ? ' selected' : '');
    row.dataset.id = s.id;
    if (s.agent_id) row.dataset.agent = s.agent_id;
    // Expand caret for a family root (orchestrator / optimizer Planner); a fixed
    // spacer keeps non-parent rows' titles aligned with parents'.
    const caretHtml = hasChildren
      ? `<span class="session-row-expand" data-id="${s.id}" title="${isExpanded ? 'Collapse' : 'Show grouped sessions'}">${icon(isExpanded ? 'chevron-down' : 'chevron-right', { size: '13px' })}</span>`
      : (anyParent ? `<span class="session-row-expand-spacer"></span>` : '');
    const label = s.title || 'New Session';
    const sAgentIcon = s.agent_icon || '';
    const sAgentEngine = s.agent_engine || '';
    // Build icon HTML from session's own data (API) or fall back to agent-cache lookup
    let rowIconHtml;
    if (sAgentIcon || sAgentEngine) {
      const big = '21px';
      const name = sAgentIcon;
      const engine = sAgentEngine;
      if (engine === 'claude_code' && (!name || name === 'sparkles')) {
        rowIconHtml = claudeMark({ size: big });
      } else if (!name) {
        rowIconHtml = icon('bot', { size: big });
      } else if (ICON_PICKER_ICONS.includes(name)) {
        rowIconHtml = icon(name, { size: big });
      } else {
        rowIconHtml = `<span style="font-size:${big};line-height:1;display:inline-flex;align-items:center;justify-content:center">${name.replace(/</g, '&lt;')}</span>`;
      }
    } else {
      const agentIconHtml = _agentIconHtml(s.agent_id, '14px');
      rowIconHtml = agentIconHtml || _esc(_agentIconFor(s.agent_id));
    }
    let statusHtml = '';
    if (s.run_status === 'running') {
      statusHtml = `<span class="session-row-status session-status-running" title="Agent is thinking\u2026">${icon('loader-2', { size: '12px' })}</span>`;
    } else if (s.has_unread) {
      statusHtml = `<span class="session-row-status session-status-unread" title="New response ready">${icon('check-circle', { size: '12px' })}</span>`;
    }
    const _isSpawn = typeof s.id === 'string' && s.id.startsWith('spawn-');
    const spawnBadge = _isSpawn
      ? `<span class="session-row-spawn-badge" title="Spawned helper session">${icon('git-branch', { size: '11px' })}</span>`
      : '';
    // Manage mode: swap the trash button for a visibility (eye) toggle that
    // reflects and flips the row's hidden state. Normal mode: a "more" (⋯) kebab
    // that opens the pin/unpin toggle, then the trash button.
    const trailingBtn = _showHidden
      ? `<button class="session-row-visibility" title="${s.hidden ? 'Un-hide session' : 'Hide session'}" data-id="${s.id}" data-hidden="${s.hidden ? '1' : '0'}">${icon(s.hidden ? 'eye-off' : 'eye', { size: '14px' })}</button>`
      : `<button class="session-row-kebab" title="More…" data-id="${s.id}">${icon('more-vertical', { size: '14px' })}</button>`
        + `<button class="session-row-delete" title="Delete session" data-id="${s.id}" data-state="trash">${icon('trash-2', { size: '14px' })}</button>`;
    row.innerHTML = `
      <span class="row-drag-handle" data-drag-handle title="Drag to reorder">${icon('grip-vertical', { size: '13px' })}</span>
      ${caretHtml}
      <span class="session-row-pin-icon">${icon('pin', { size: '12px' })}</span>
      ${statusHtml}
      <span class="session-row-title" title="Hold to rename"><span class="session-row-agent-icon">${rowIconHtml}</span><span class="session-row-sep"> </span>${spawnBadge}${label.replace(/</g, '&lt;')}</span>
      ${trailingBtn}
    `;
    menu.appendChild(row);
    // Nested child rows (lazily fetched) directly under an expanded parent.
    if (isExpanded) menu.insertAdjacentHTML('beforeend', _childRowsHtml(s.id));
  }
  _appendManageRow(menu);
}

// \u2500\u2500 Management footer row \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// A row below the session list with list-wide actions: a show/hide-hidden eye
// toggle and a delete-all button. Only shown when there is at least one session.
function _appendManageRow(menu) {
  if (!_sessionsCache.length) return;
  const bar = document.createElement('div');
  bar.className = 'session-manage-row';
  const eyeTitle = _showHidden ? 'Hide hidden sessions' : 'Show hidden sessions';
  bar.innerHTML = `
    <span class="session-manage-actions">
      <button class="session-manage-eye" title="${eyeTitle}" data-state="${_showHidden ? 'on' : 'off'}">${icon(_showHidden ? 'eye' : 'eye-off', { size: '15px' })}</button>
      <button class="session-manage-delete-all" title="Delete all sessions" data-state="trash">${icon('trash-2', { size: '15px' })}</button>
    </span>
  `;
  menu.appendChild(bar);
}

async function populateSessionSelect(userId) {
  const menu = document.getElementById('session-dropdown-menu');
  if (!userId) {
    _sessionsCache = [];
    if (menu) menu.innerHTML = '';
    _setTriggerLabel();
    return;
  }
  const mySeq = ++_sessionFetchSeq;
  try {
    const token = localStorage.getItem('auth_token');
    let url = `/api/v1/db/sessions?db=local.db&user_id=${encodeURIComponent(userId)}&limit=50`;
    if (_showHidden) url += `&include_hidden=1`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(apiPath(url));
    const data = await res.json();
    if (mySeq !== _sessionFetchSeq) return;
    _sessionsCache = (data.sessions || []).map(s => ({
      id: s.id,
      title: s.title || 'New Session',
      agent_id: s.agent_id || null,
      agent_name: s.agent_name || '',
      agent_icon: s.agent_icon || '',
      agent_engine: s.agent_engine || '',
      created_at: s.created_at,
      updated_at: s.updated_at || s.created_at,
      pinned: !!s.pinned,
      hidden: !!s.hidden,
      run_status: s.run_status || null,
      has_unread: !!s.has_unread,
      child_count: s.child_count || 0,
    }));
    if (app.currentSessionId && !_sessionsCache.some(s => s.id === app.currentSessionId)) {
      _sessionsCache.unshift({
        id: app.currentSessionId,
        title: 'New Session',
        agent_id: app.currentAgentId || null,
        agent_name: '',
        agent_icon: '',
        agent_engine: '',
        created_at: null,
        updated_at: null,
        pinned: false,
        hidden: false,
        run_status: null,
        has_unread: false,
      });
    }
    _renderSessionRows();
    _setTriggerLabel();
  } catch (e) {
    console.warn('Failed to load sessions:', e);
  }
}

export {
  _sessionsCache,
  populateSessionSelect,
  _renderSessionRows,
  _setTriggerLabel,
  _fetchRelatedSessions,
  applySessionTitle,
  _formatRelativeTime,
  _truncate,
  _setShowHidden,
  _getShowHidden,
  toggleSessionGroup,
};