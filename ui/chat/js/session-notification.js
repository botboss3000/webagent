'use strict';

// Session completion notification panel.
// Watches _sessionsCache for status transitions (running → done/interrupted/error)
// and shows them as rows inside a single draggable panel — like the chat widget
// but for notifications. The panel is movable by dragging its header.
// Notifications persist in the user's per-user database
// (data/user_data/{user_id}/{user_id}.db, served via /api/v1/db/session-notifications)
// so undismissed rows re-appear after a refresh AND on every device
// (hybrid sync) until they are manually dismissed.
// Module map: ui/chat/js/README.md.

import { _sessionsCache } from './session-list.js';
import { switchToSession } from './session-core.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';

// ── State ──────────────────────────────────────────────────────────────────

/** Map<sid, run_status> snapshot from the last check */
let _lastStatusMap = null;

/** Set<sid> sessions we've already notified for this page session */
const _notifiedSessionIds = new Set();

/** Set<sid> whose initial save to the server failed — retried each poll tick */
const _retrySaves = new Set();

/** Map<sid, { title }> of active notification rows */
const _activeRows = new Map();

/** Panel DOM refs set after _ensurePanel() */
let _panel = null;
let _panelFloating = false;

// ── App-wide on/off gate ─────────────────────────────────────────────────────
// The notification panel is an APP-LEVEL helper, so it is NOT gated on the
// current chat agent's abilities. It IS gated on one app-wide admin switch:
// App Settings → App Functions → "Session completion notifications"
// (session_completion_notifications in app-settings.json), served to every
// visitor via the public /api/v1/auth/ui-config and read once at boot (see
// initSessionNotification). Defaults ON — the panel has always been available,
// so a missing/unreachable config keeps it on (fail-open).
let _enabledFlag = true;

function _enabled() {
  return _enabledFlag;
}

// Fetch the app-wide on/off flag once from the public ui-config. Sends the auth
// token when present so it behaves identically on authenticated pages (mirrors
// app-control-point.js / appearance.js). Best-effort: any failure leaves the
// flag at its ON default.
function _loadEnabledFlag() {
  let headers = {};
  try {
    const t = localStorage.getItem('auth_token');
    if (t) headers = { Authorization: 'Bearer ' + t };
  } catch (_) { /* ignore */ }
  fetch(apiPath('/api/v1/auth/ui-config'), { headers })
    .then((r) => (r.ok ? r.json() : null))
    .then((cfg) => {
      if (cfg && typeof cfg.session_completion_notifications === 'boolean') {
        _enabledFlag = cfg.session_completion_notifications;
      }
    })
    .catch(() => { /* keep the ON default */ });
}

// ── Server persistence (per-user DB — cross-device) ────────────────────────

const _API_BASE = '/api/v1/db/session-notifications';

/** GET the caller's undismissed notifications from their per-user DB. */
async function _apiList() {
  const res = await fetch(apiPath(`${_API_BASE}?db=user.db`), {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`session-notifications list failed: ${res.status}`);
  const data = await res.json();
  return Array.isArray(data.notifications) ? data.notifications : [];
}

/** Upsert one notification row for the caller (dismissed=false → show,
 *  dismissed=true → soft-dismiss across devices). */
async function _apiSave(sid, title, dismissed) {
  const res = await fetch(apiPath(`${_API_BASE}?db=user.db`), {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sid,
      title: title || '',
      dismissed: !!dismissed,
    }),
  });
  if (!res.ok) throw new Error(`session-notifications save failed: ${res.status}`);
  return res.json();
}

// ── Panel DOM ──────────────────────────────────────────────────────────────

const CHECK_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
const X_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
const BELL_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>';

function _ensurePanel() {
  if (_panel && document.body.contains(_panel)) return _panel;

  let container = document.getElementById('session-notification-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'session-notification-container';
  }
  // Move to <body> so it escapes #chat-panel stacking context
  if (container.parentNode !== document.body) {
    document.body.appendChild(container);
  }
  container.hidden = false;

  // Only build the inner structure once
  if (container.querySelector('.sn-header')) {
    _panel = container;
    return _panel;
  }

  container.innerHTML = '';

  // ── Header ──
  const header = document.createElement('div');
  header.className = 'sn-header';

  const icon = document.createElement('span');
  icon.className = 'sn-head-icon';
  icon.innerHTML = BELL_SVG;
  header.appendChild(icon);

  const title = document.createElement('span');
  title.className = 'sn-title';
  title.textContent = 'Notifications';
  header.appendChild(title);

  const count = document.createElement('span');
  count.className = 'sn-count';
  count.textContent = '0';
  header.appendChild(count);

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'sn-icon-btn';
  closeBtn.title = 'Dismiss all and close';
  closeBtn.innerHTML = X_SVG;
  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _dismissAll();
  });
  header.appendChild(closeBtn);

  // Drag to move
  header.addEventListener('pointerdown', _onHeaderDown);

  container.appendChild(header);

  // ── Body ──
  const body = document.createElement('div');
  body.className = 'sn-body';
  container.appendChild(body);

  _panel = container;
  _updateCount();
  return _panel;
}

function _buildRow(sid, title) {
  const row = document.createElement('div');
  row.className = 'sn-row';
  row.dataset.sid = sid;

  const icon = document.createElement('span');
  icon.className = 'sn-row-icon';
  icon.innerHTML = CHECK_SVG;
  row.appendChild(icon);

  const textWrap = document.createElement('span');
  textWrap.className = 'sn-row-text';

  const label = document.createElement('span');
  label.className = 'sn-row-label';
  label.textContent = 'Session complete';

  const titleEl = document.createElement('span');
  titleEl.className = 'sn-row-title';
  titleEl.textContent = title || 'New Session';

  textWrap.appendChild(label);
  textWrap.appendChild(titleEl);
  row.appendChild(textWrap);

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'sn-row-close';
  closeBtn.setAttribute('aria-label', 'Dismiss');
  closeBtn.innerHTML = X_SVG;
  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _dismissRow(sid);
  });
  row.appendChild(closeBtn);

  // Click the row → switch to session
  row.addEventListener('click', (e) => {
    if (e.target.closest('button')) return;
    _dismissRow(sid);
    switchToSession(sid);
  });

  return row;
}

function _updateCount() {
  if (!_panel) return;
  const countEl = _panel.querySelector('.sn-count');
  if (countEl) countEl.textContent = String(_activeRows.size);
}

// ── Drag to move (same pattern as chat-widget.js) ──────────────────────────

function _makeFloating() {
  if (_panelFloating) return;
  const r = _panel.getBoundingClientRect();
  _panelFloating = true;
  _panel.classList.add('sn-floating');
  _panel.style.left = r.left + 'px';
  _panel.style.top = r.top + 'px';
  _panel.style.right = 'auto';
}

function _dragLoop(onMove) {
  const up = () => {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', up);
    document.body.classList.remove('sn-dragging');
  };
  document.body.classList.add('sn-dragging');
  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerup', up);
}

function _onHeaderDown(e) {
  if (e.target.closest('.sn-icon-btn')) return;  // header buttons
  if (e.button !== 0) return;
  e.preventDefault();
  _makeFloating();
  const r = _panel.getBoundingClientRect();
  const ox = e.clientX - r.left, oy = e.clientY - r.top;
  _dragLoop((ev) => {
    const nx = Math.max(0, Math.min(ev.clientX - ox, window.innerWidth - 48));
    const ny = Math.max(0, Math.min(ev.clientY - oy, window.innerHeight - 30));
    _panel.style.left = nx + 'px';
    _panel.style.top = ny + 'px';
  });
}

// ── Row lifecycle ──────────────────────────────────────────────────────────

function _addRow(sid, title) {
  if (_activeRows.has(sid)) return;
  _ensurePanel();
  const body = _panel.querySelector('.sn-body');
  const row = _buildRow(sid, title);
  body.appendChild(row);
  _activeRows.set(sid, { title });
  _updateCount();
}

function _removeRow(sid) {
  const row = _panel && _panel.querySelector(`.sn-row[data-sid="${CSS.escape(sid)}"]`);
  if (row) row.remove();
  _activeRows.delete(sid);
  _updateCount();
  // Hide panel when empty
  if (_activeRows.size === 0 && _panel) {
    _panel.hidden = true;
  }
}

/** User-initiated dismissal: remove the row immediately (optimistic) and
 *  soft-dismiss in the per-user DB so it stays dismissed on every device. */
function _dismissRow(sid) {
  const entry = _activeRows.get(sid);
  _removeRow(sid);
  _apiSave(sid, entry && entry.title, true).catch(() => {
    // Transient — the next reconcile re-shows the row so the user can retry.
  });
}

/** Dismiss all rows and close the panel. */
function _dismissAll() {
  const sids = [..._activeRows.keys()];
  for (const sid of sids) {
    const entry = _activeRows.get(sid);
    _removeRow(sid);
    _apiSave(sid, entry && entry.title, true).catch(() => {});
  }
  if (_panel) _panel.hidden = true;
}

// ── Server reconcile ───────────────────────────────────────────────────────

/**
 * Fetch the caller's undismissed notifications from the per-user DB and
 * reconcile local rows against it: show any row we don't have yet, and
 * remove any row the server no longer lists.
 */
async function _reconcileServerToasts() {
  let rows;
  try {
    rows = await _apiList();
  } catch {
    return;
  }
  const serverIds = new Set(rows.map(r => r.session_id));

  // Show notifications created on other devices (or after init)
  for (const r of rows) {
    if (!_activeRows.has(r.session_id)) {
      _notifiedSessionIds.add(r.session_id);
      _addRow(r.session_id, r.title);
    }
  }

  // Drop rows that are gone server-side (dismissed on another device)
  for (const sid of [..._activeRows.keys()]) {
    if (!serverIds.has(sid)) _removeRow(sid);
  }
}

// ── Public API ─────────────────────────────────────────────────────────────

/**
 * Compare current _sessionsCache against the last snapshot and notify for any
 * session that transitioned from 'running' to a terminal status and is not the
 * current session. Each completion is saved to the caller's per-user DB first
 * (so it survives refresh and reaches other devices), then shown as a row.
 */
export async function checkSessionCompletions() {
  if (!_enabled()) return;
  const currentId = window.app && window.app.currentSessionId;

  // Build a map of current statuses
  const currentMap = {};
  for (const s of _sessionsCache) {
    currentMap[s.id] = s.run_status;
  }

  const completions = [];
  if (_lastStatusMap) {
    for (const s of _sessionsCache) {
      const sid = s.id;
      if (sid === currentId) continue;
      if (_notifiedSessionIds.has(sid)) continue;
      if (_activeRows.has(sid)) continue;
      const oldStatus = _lastStatusMap[sid];
      const newStatus = s.run_status;
      if (
        oldStatus === 'running' &&
        newStatus &&
        newStatus !== 'running' &&
        newStatus !== 'queued'
      ) {
        completions.push(s);
      }
    }
  }

  _lastStatusMap = currentMap;

  for (const s of completions) {
    try {
      await _apiSave(s.id, s.title, false);
      _notifiedSessionIds.add(s.id);
      _addRow(s.id, s.title);
    } catch {
      _retrySaves.add(s.id);
    }
  }

  // Retry saves that failed on earlier ticks
  for (const sid of [..._retrySaves]) {
    const s = _sessionsCache.find(x => x.id === sid);
    if (!s) {
      _retrySaves.delete(sid);
      continue;
    }
    try {
      await _apiSave(sid, s.title, false);
      _retrySaves.delete(sid);
      _notifiedSessionIds.add(sid);
      _addRow(sid, s.title);
    } catch {
      // try again next tick
    }
  }

  await _reconcileServerToasts();
}

/**
 * Initialize the notification system. Call once from initSessions().
 */
export async function initSessionNotification() {
  _loadEnabledFlag();
  if (!_enabled()) return;
  // Clear any rows left from a prior hot init
  _activeRows.clear();
  _notifiedSessionIds.clear();
  _retrySaves.clear();

  if (_panel) {
    const body = _panel.querySelector('.sn-body');
    if (body) body.innerHTML = '';
    _panel.hidden = true;
    _updateCount();
  }

  // Seed the status snapshot with the current cache
  _lastStatusMap = {};
  for (const s of _sessionsCache) {
    _lastStatusMap[s.id] = s.run_status;
  }

  // Restore undismissed notifications from the per-user DB (cross-device)
  await _reconcileServerToasts();
}
