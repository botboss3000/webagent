'use strict';

// Session dropdown list — session data + rendering (cache, validated fetch with
// bounded retry, row rendering, manage/search/bin footer, trigger label, related
// chips, group tree). Centralized home: ui/chat/elements/session-dropdown/.
// ui/chat/js/session-list.js is a compatibility re-export shim for this module,
// so existing importers (session-core.js, chat-send.js, chat-stream.js, ...)
// keep working unchanged. Module map: ui/chat/js/README.md.

import { app } from '../../../shared/js/state.js';
import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { icon, claudeMark, codexMark } from '../../../shared/js/icons.js';
import { storageAdapter } from '../../js/storage/storage-adapter.js';
import { ICON_PICKER_ICONS } from '../../../shared/js/icon-picker.js';
import { _esc, _escAttr } from '../../../shared/js/dom-utils.js';
import { _agentIconFor, _agentIconHtml, _agentNameFor } from '../../js/session-agent.js';
import { hydrateAgentDisplay } from '../../../shared/js/agent-display-cache.js';
// session-core imports from this module too — an ES-module cycle. Safe because
// switchToSession and deleteSession are only CALLED at runtime (in the tab click
// handlers / chip close handlers), by which point the live binding is resolved.
import { switchToSession, deleteSession } from '../../js/session-core.js';
import { applyRubberBand } from '../../../shared/js/rubber-band.js';

let _sessionsCache = [];
let _sessionFetchSeq = 0;
// The Sessions page fetches this same active-session metadata alongside its
// table-only usage stats. Share the common subset so opening the picker after
// the page does not start another list request.
const _ACTIVE_SESSION_CACHE_TTL_MS = 30000;
let _activeSessionCache = { userId: '', sessions: [], loadedAt: 0 };

function _normaliseSessionMetadata(session) {
  const id = session && (session.id || session.session_id);
  if (!id) return null;
  return {
    id,
    title: session.title || 'New Session',
    agent_id: session.agent_id || null,
    agent_name: session.agent_name || '',
    agent_icon: session.agent_icon || '',
    agent_engine: session.agent_engine || '',
    created_at: session.created_at || null,
    updated_at: session.updated_at || session.last_active || session.created_at || null,
    activity_at: session.activity_at || session.last_active || null,
    pinned: !!session.pinned,
    sort_order: Number.isFinite(session.sort_order) ? session.sort_order : null,
    hidden: !!session.hidden,
    recycled: !!session.recycled,
    run_status: session.run_status || null,
    run_updated_at: session.run_updated_at || null,
    queue_position: typeof session.queue_position === 'number' ? session.queue_position : null,
    queue_total: typeof session.queue_total === 'number' ? session.queue_total : null,
    has_unread: !!session.has_unread,
    child_count: session.child_count || 0,
  };
}

function _canUseActiveSessionCache(userId) {
  return _activeSessionCache.userId === userId
    && _activeSessionCache.sessions.length > 0
    && Date.now() - _activeSessionCache.loadedAt < _ACTIVE_SESSION_CACHE_TTL_MS;
}

// Called by the Sessions page after its active /session-stats response.
// Hidden, searched, and recycled scopes still use their own server queries.
function primeSessionMetadataCache(userId, sessions) {
  if (!userId || !Array.isArray(sessions)) return;
  _activeSessionCache = {
    userId,
    sessions: sessions.map(_normaliseSessionMetadata).filter(Boolean),
    loadedAt: Date.now(),
  };
}

function hasFreshSessionMetadataCache(userId) {
  return _canUseActiveSessionCache(userId);
}
// Session-list tree: which parent rows (orchestrator) are expanded, and a
// cache of their fetched children so re-renders don't refetch. Children are
// loaded lazily from the same /related endpoint that feeds the sub-header tab
// bar, so the tree and the tabs always agree. Optimizer Planner/Closer
// sessions are top-level rows of their own — they are not nested here.
const _expandedGroups = new Set();
const _childrenCache = new Map();   // parent sid -> [{session_id,label,role,name,status}]
// Footer eye-toggle state: when true the dropdown reveals hidden sessions and
// each row shows an eye toggle instead of a trash button (manage mode).
let _showHidden = false;
function _setShowHidden(v) { _showHidden = !!v; }
function _getShowHidden() { return _showHidden; }
// Checkbox delete mode: when active, each row shows a checkbox and a "delete selected"
// button appears in the manage row. Independent of show-hidden manage mode.
let _checkboxMode = false;
const _selectedSessions = new Set();
function _setCheckboxMode(v) { _checkboxMode = !!v; }
function _getCheckboxMode() { return _checkboxMode; }
function _clearSelected() { _selectedSessions.clear(); }

// ── Dropdown search + recycle-bin (footer bar) ─────────────────────────────
// The footer search bar filters by session TITLE ('title' mode) or by message
// CONTENT ('content' mode), and the recycle-bin toggle narrows the list to
// binned sessions. Content mode mirrors the Sessions page scan
// (/session-messages?light=1, chunked + cached). State lives here so the 15s
// poll and every re-render preserve the active search/bin scope.
let _searchQuery = '';
let _searchMode = 'title';      // 'title' | 'content'
let _binMode = false;
const _msgIndex = new Map();     // sid -> [{role, content}] (user/assistant only)
const _msgHitCounts = new Map(); // sid -> matching-message count for current query
let _msgScanBusy = null;         // in-flight content-scan query token
const _MSG_MIN_QUERY_LEN = 2;    // below this, content mode falls back to title
const _MSG_SCAN_MAX = 300;       // most-recently-active sessions scanned
const _MSG_FETCH_LIMIT = 300;    // newest messages fetched per session

// Debounce timer for the search input. Centralised here so that ANY query
// state change (typing, clearing via ×/Escape, closing the menu) cancels a
// pending commit — otherwise a stale timer could resurrect an old query AFTER
// the user cleared the box, silently filtering the list while the input looks
// empty (the "dropdown sometimes doesn't show the sessions" bug).
let _searchCommitTimer = 0;

function _cancelPendingSearchCommit() {
  if (_searchCommitTimer) { clearTimeout(_searchCommitTimer); _searchCommitTimer = 0; }
}

function _setSearchQuery(v) {
  // A new value (including clearing to '') supersedes any pending debounce
  // commit — the timer must never overwrite a newer, deliberate state.
  _cancelPendingSearchCommit();
  _searchQuery = (v == null ? '' : String(v));
}
function _getSearchQuery() { return _searchQuery; }
function _setSearchMode(v) { _searchMode = (v === 'content' ? 'content' : 'title'); }
function _getSearchMode() { return _searchMode; }
function _setBinMode(v) { _binMode = !!v; }
function _getBinMode() { return _binMode; }
function _clearMsgSearch() { _msgHitCounts.clear(); }

// Debounced commit of the search input's live value: waits 200ms after the
// last keystroke, then applies the query and refetches (plus a content-mode
// message scan). A newer input call replaces the pending timer.
function _scheduleSearchCommit(q) {
  _cancelPendingSearchCommit();
  _searchCommitTimer = setTimeout(() => {
    _searchCommitTimer = 0;
    _setSearchQuery(q);
    _clearMsgSearch();
    if (app.currentUserId) {
      populateSessionSelect(app.currentUserId);
      const tq = q.trim();
      if (_getSearchMode() === 'content' && tq.length >= 2) _scanMessagesForQuery(tq);
    }
  }, 200);
}

// Fetch ONE session's newest messages (light mode) and cache ONLY the
// user/assistant text rows — tool calls (role 'tool') and system messages are
// excluded here, at the source, so they can never match a query. Mirrors
// _fetchSessionMessages on the Sessions page.
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
    console.warn('Session message fetch failed for', sessionId, e);
  }
  _msgIndex.set(sessionId, store);   // cache even on failure (empty) → no retry loop
  return store;
}

// How many user/assistant messages in this session contain `q`. Null when
// there are no matches (or the session isn't indexed yet).
function _msgMatchInfo(sid, q) {
  const idx = _msgIndex.get(sid);
  if (!idx || !q) return null;
  const ql = q.toLowerCase();
  let count = 0;
  for (const m of idx) {
    if (m.content.toLowerCase().includes(ql)) count++;
  }
  return count > 0 ? { count } : null;
}

// Scan cached sessions' messages for `q` in concurrent batches, re-rendering
// after each batch so matches appear progressively. Results are cached per
// session, so re-typing the same query is instant; a newer query supersedes
// the in-flight one. Ported from the Sessions page _scanMessagesForQuery.
async function _scanMessagesForQuery(q) {
  if (!q || q.length < _MSG_MIN_QUERY_LEN) return;
  if (_searchMode !== 'content') return;
  if (_msgScanBusy === q) return;      // already scanning this query
  _msgScanBusy = q;                    // newer query supersedes an in-flight one
  try {
    const seen = new Set();
    const sessionRows = [];
    for (const s of _sessionsCache) {
      if (!s.id || seen.has(s.id)) continue;
      seen.add(s.id);
      sessionRows.push(s);
    }
    sessionRows.sort((a, b) => {
      const ta = Date.parse(a.activity_at || a.updated_at || a.created_at || '') || 0;
      const tb = Date.parse(b.activity_at || b.updated_at || b.created_at || '') || 0;
      return tb - ta;
    });
    const todo = sessionRows.slice(0, _MSG_SCAN_MAX).filter(s => !_msgIndex.has(s.id));
    if (todo.length === 0) { _renderSessionRows(); return; } // everything indexed
    const CHUNK = 8;
    for (let i = 0; i < todo.length; i += CHUNK) {
      // Stop early if the query changed or was cleared while fetching.
      if (_msgScanBusy !== q || _getSearchQuery().trim() !== q) return;
      await Promise.all(todo.slice(i, i + CHUNK).map(s => _fetchSessionMessages(s.id)));
      if (_msgScanBusy === q && _getSearchQuery().trim() === q) _renderSessionRows();
    }
  } catch (e) {
    console.warn('Session message search failed:', e);
  } finally {
    if (_msgScanBusy === q) {
      _msgScanBusy = null;
      if (_getSearchQuery().trim() === q) _renderSessionRows();
    }
  }
}

function _sortSessionsByPinAndActivity(sessions) {
  return sessions.sort((a, b) => {
    if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
    if (a.pinned && b.pinned) {
      const ao = Number.isFinite(a.sort_order) ? a.sort_order : Number.MAX_SAFE_INTEGER;
      const bo = Number.isFinite(b.sort_order) ? b.sort_order : Number.MAX_SAFE_INTEGER;
      if (ao !== bo) return ao - bo;
    }
    // Mirror the server's activity ordering (latest interaction first, falling
    // back to updated_at, then created_at) so nav and dropdown agree. Using
    // plain updated_at churned the order whenever a pin/rename/admin edit
    // bumped it, which made swipe/arrow navigation look random.
    const at = Date.parse(a.activity_at || a.updated_at || a.created_at || '') || 0;
    const bt = Date.parse(b.activity_at || b.updated_at || b.created_at || '') || 0;
    return bt - at || String(a.id || '').localeCompare(String(b.id || ''));
  });
}

function _truncate(s, n) {
  return (s && s.length > n) ? s.slice(0, n) + '\u2026' : (s || '');
}

function _parseDate(dateStr) {
  if (!dateStr) return new Date(NaN);
  // ISO 8601 with offset (+00:00, -05:00, or Z) — parse as-is
  if (/[+-]\d{2}:\d{2}$/.test(dateStr) || /Z$/i.test(dateStr)) return new Date(dateStr);
  // No timezone info — treat as UTC
  return new Date(dateStr + 'Z');
}

function _formatRelativeTime(dateStr) {
  if (!dateStr) return '';
  const now = Date.now();
  const d = _parseDate(dateStr);
  const diffMs = now - d.getTime();
  if (diffMs < 0) return 'just now';
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `${sec}s ago`;
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
  const wrap = document.getElementById('chat-sub-scroll-wrap');
  if (!subHeader || !wrap) return;

  const sid = app.currentSessionId;
  if (!sid || !app.currentUserId) {
    subHeader.style.display = 'none';
    return;
  }

  try {
    const token = localStorage.getItem('auth_token');
    let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/related?db=user.db`;
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

    // ── Build all chips into a single carousel strip ──
    let chipsHtml = '';
    if (hasFamily) {
      // Root tab
      const otitle = orchestrator.title || `${rootLabel} session`;
      const isActive = orchestrator.session_id === curSid;
      const activeCls = isActive ? ' chip-active' : '';
      const rootIcon = (groupKind === 'optimizer' && rootLabel !== 'Main') ? 'wand-2' : 'layout-dashboard';
      chipsHtml += `<button type="button" class="sub-chip chip-main${activeCls}" title="${isActive ? otitle : 'Back to ' + otitle}" data-parent-id="${orchestrator.session_id}"><span class="chip-status">${icon(rootIcon, { size: '10px' })}</span>${rootLabel}</button>`;

      // Member tabs (spawn helpers / optimizer closer)
      children.forEach((sp, i) => {
        const spSid = sp.session_id;
        const isActive = spSid === curSid;
        const statusClass = sp.status === 'running' || sp.status === 'queued' ? 'chip-running'
                         : sp.status === 'done' ? 'chip-done'
                         : sp.status === 'error' ? 'chip-error' : '';
        const label = sp.label || (groupKind === 'optimizer'
          ? `Optimizer: ${(orchestrator && orchestrator.title) || rootLabel}`
          : `Spawn ${i + 1}`);
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
        const isCompleted = sp.run_status && ['complete','interrupted','error'].includes(sp.run_status) && sp.completed_at;
        const timeHtml = isCompleted ? `<span class="chip-time" data-completed-at="${sp.completed_at}">${_formatRelativeTime(sp.completed_at)}</span>` : '';
        const chipClass = `sub-chip ${statusClass}${isActive ? ' chip-active' : ''}`;
        const title = `${tipName}${sp.status ? ' \u2014 ' + sp.status : ''}`;
        // Member chips carry a × close button that recycles the session to the
        // bin (same DELETE the trash button uses). The root Main chip has none.
        chipsHtml += `<button type="button" class="${chipClass}" title="${title}" data-spawn-id="${spSid}"><span class="chip-status">${statusIcon}</span>${label}${timeHtml}<span class="chip-close" role="button" tabindex="0" title="Delete this session" aria-label="Delete session" data-close-spawn-id="${spSid}">${icon('x', { size: '10px' })}</span></button>`;
      });
    }

    // Browser tabs
    if (browsers.length) {
      chipsHtml += '<span class="chip-sep"></span>';
      for (const bs of browsers) {
        const label = bs.title || bs.url || 'Browser';
        const isPrivate = !bs.shared;
        const chipClass = 'sub-chip chip-browser' + (isPrivate ? ' chip-private' : '');
        const title = (bs.url || label) + (isPrivate ? ' (private)' : '');
        chipsHtml += `<button type="button" class="${chipClass}" title="${title}" data-bs-id="${bs.id}"><span class="chip-status">${icon('globe', { size: '10px' })}</span>${label}<span class="chip-close" role="button" tabindex="0" title="Close browser" aria-label="Close browser" data-close-bs-id="${bs.id}">${icon('x', { size: '10px' })}</span></button>`;
      }
    }

    const wrap = document.getElementById('chat-sub-scroll-wrap');
    if (wrap) {
      wrap.innerHTML = chipsHtml;

      // Delegate click handling on the single wrap element
      wrap.addEventListener('click', (e) => {
        const chip = e.target.closest('[data-parent-id], [data-spawn-id], [data-bs-id]');
        if (!chip) return;
        // Chip × close button — recycle a family session or close a browser.
        const closeBtn = e.target.closest('.chip-close');
        if (closeBtn) {
          e.stopPropagation();
          _handleChipClose(closeBtn);
          return;
        }
        if (chip.dataset.parentId && chip.dataset.parentId !== curSid) {
          switchToSession(chip.dataset.parentId);
        } else if (chip.dataset.spawnId && chip.dataset.spawnId !== curSid) {
          switchToSession(chip.dataset.spawnId);
        } else if (chip.dataset.bsId) {
          const webTab = document.querySelector('.main-tab[data-value="browser"]');
          if (webTab) webTab.click();
          if (window.__switchWebSession) {
            window.__switchWebSession(chip.dataset.bsId);
          }
        }
      });
    }

    subHeader.style.display = (hasFamily || browsers.length) ? 'flex' : 'none';
    _wireSubCarousel();
  } catch (e) {
    console.warn('Failed to fetch related sessions:', e);
    subHeader.style.display = 'none';
  }
}

// ── Chip × close (recycle session / close browser instance) ───────────────
// The sub-header chip close buttons let the user drop a family member into the
// recycle bin (same DELETE the trash button uses) or shut down a browser
// session. The root "Main" chip has no × — the main session is never closable
// from here. After a successful close the tab bar is re-fetched so the chip
// disappears (and, if the deleted session was the open one, the chat switches
// to a remaining session — deleteSession already handles that).
async function _handleChipClose(btn) {
  const spawnId = btn.dataset.closeSpawnId;
  const bsId = btn.dataset.closeBsId;
  try {
    if (spawnId) {
      await deleteSession(spawnId, { retries: 1 });
    } else if (bsId) {
      // Prefer the browser page's own teardown (it also switches away from a
      // deleted active session + refreshes its popover); fall back to the REST
      // call if that module hasn't been loaded yet.
      if (typeof window.__closeWebSession === 'function') {
        await window.__closeWebSession(bsId);
      } else {
        const res = await fetch(apiPath('/api/v1/browser/sessions/' + encodeURIComponent(bsId)), {
          method: 'DELETE',
          headers: { ...authHeaders() },
        });
        if (!res.ok) return;
      }
    }
  } catch (_e) {
    // Never let a failed close break the tab bar; it re-renders on next poll.
  }
  _fetchRelatedSessions();
}

// ── Sub-header carousel wiring (scroll-out chevrons + edge fades) ────────
// Mirrors the Agents / Instances page carousel pattern. Hooked after every
// chip render in _fetchRelatedSessionsInner.
function _wireSubCarousel() {
  const wrap = document.getElementById('chat-sub-scroll-wrap');
  if (!wrap) return;
  // Prevent duplicate wiring — the wrap element is static (only chips inside it
  // are replaced), so skip if already wired.
  if (wrap.dataset.carouselWired === '1') return;
  wrap.dataset.carouselWired = '1';

  const chevLeft  = wrap.parentElement?.querySelector('.sub-carousel-chev.left');
  const chevRight = wrap.parentElement?.querySelector('.sub-carousel-chev.right');

  const update = () => {
    const maxScroll = wrap.scrollWidth - wrap.clientWidth;
    const atStart = wrap.scrollLeft <= 1;
    const atEnd   = wrap.scrollLeft >= maxScroll - 1;
    const overflowing = maxScroll > 1;
    wrap.classList.toggle('can-scroll-left',  overflowing && !atStart);
    wrap.classList.toggle('can-scroll-right', overflowing && !atEnd);
    // Toggle chevrons directly (left chevron is before wrap in DOM, so ~ selector can't reach it)
    if (chevLeft)  chevLeft.style.display  = (overflowing && !atStart) ? 'flex' : 'none';
    if (chevRight) chevRight.style.display = (overflowing && !atEnd)   ? 'flex' : 'none';
  };
  wrap.addEventListener('scroll', update, { passive: true });
  applyRubberBand(wrap);
  requestAnimationFrame(update);
  setTimeout(update, 120);
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(update).observe(wrap);

  const page = () => Math.max(wrap.clientWidth * 0.65, 120);
  if (chevLeft)  chevLeft.addEventListener('click',  e => { e.stopPropagation(); wrap.scrollBy({ left: -page(), behavior: 'smooth' }); });
  if (chevRight) chevRight.addEventListener('click', e => { e.stopPropagation(); wrap.scrollBy({ left:  page(), behavior: 'smooth' }); });
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
      labelEl.className = 'session-row-title';
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
  // Populate the trigger's agent icon (mirrors dropdown row icon).
  const iconEl = document.getElementById('session-dropdown-icon');
  if (iconEl) {
    let triggerIconHtml;
    if (found?.agent_icon || found?.agent_engine) {
      const name = found.agent_icon || '';
      const engine = found.agent_engine || '';
      if (engine === 'claude_code' && (!name || name === 'sparkles')) {
        triggerIconHtml = claudeMark({ size: '14px' });
      } else if (engine === 'codex' && (!name || name === 'code-2')) {
        triggerIconHtml = codexMark({ size: '14px' });
      } else if (!name) {
        triggerIconHtml = icon('bot', { size: '14px' });
      } else if (ICON_PICKER_ICONS.includes(name)) {
        triggerIconHtml = icon(name, { size: '14px' });
      } else {
        triggerIconHtml = `<span style="font-size:14px;line-height:1;display:inline-flex;align-items:center;justify-content:center">${name.replace(/</g, '&lt;')}</span>`;
      }
    } else {
      triggerIconHtml = agentIconHtml || _esc(agentIcon);
    }
    iconEl.innerHTML = triggerIconHtml;
  }
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
        } else if (engine === 'codex' && (!name || name === 'code-2')) {
          headerIconHtml = codexMark({ size: '20px' });
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
      agentNameEl.style.display = '';
      // Move interactive affordances to the glass-chip wrapper so the
      // entire padded chip area is clickable (not just the inner text).
      const nameRow = agentNameEl.parentElement;
      if (nameRow) {
        nameRow.tabIndex = 0;
        nameRow.role = 'button';
        nameRow.setAttribute('aria-label', 'Switch agent');
      }
    } else {
      agentNameEl.textContent = '';
      agentNameEl.style.display = 'none';
    }
  }
  labelEl.title = ((found && found.id) || sid || '');
  // Set data-id on the trigger's kebab and delete buttons.
  const triggerKebab = document.getElementById('session-dropdown-kebab');
  const triggerDelete = document.getElementById('session-dropdown-delete');
  if (triggerKebab) triggerKebab.dataset.id = sid || '';
  if (triggerDelete) triggerDelete.dataset.id = sid || '';
  const sessionDropdown = document.getElementById('session-dropdown');
  if (sessionDropdown) sessionDropdown.dataset.loaded = 'true';
  const statusEl = document.getElementById('session-dropdown-status');
  if (statusEl) {
    if (found && found.run_status === 'running') {
      statusEl.innerHTML = '<span class="session-radial-loader sm"></span>';
      statusEl.className = 'session-row-status session-dropdown-status session-status-running';
      statusEl.title = 'Agent is thinking\u2026';
    } else if (found && found.run_status === 'queued') {
      const qPos = Number.isFinite(found.queue_position) ? found.queue_position : null;
      const qTot = Number.isFinite(found.queue_total) ? found.queue_total : null;
      const qLabel = qPos != null
        ? (qTot != null ? `#${qPos}/${qTot}` : `#${qPos}`)
        : '\u23f3';
      const qTitle = qPos != null
        ? (qTot != null ? `Waiting in the session queue: position ${qPos} of ${qTot}`
          : `Waiting in the session queue: position ${qPos}`)
        : 'Waiting in the session queue\u2026';
      statusEl.textContent = qLabel;
      statusEl.className = 'session-row-status session-dropdown-status session-status-queued';
      statusEl.title = qTitle;
    } else if (found && found.run_updated_at && ['complete','interrupted','error'].includes(found.run_status)) {
      statusEl.innerHTML = _formatRelativeTime(found.run_updated_at);
      statusEl.className = 'session-row-status session-dropdown-status session-status-completed';
      statusEl.title = 'Completed ' + _parseDate(found.run_updated_at).toLocaleString();
    } else if (found && found.has_unread) {
      statusEl.innerHTML = icon('check-circle', { size: '12px' });
      statusEl.className = 'session-row-status session-dropdown-status session-status-unread';
      statusEl.title = 'New response ready';
    } else {
      statusEl.innerHTML = '';
      statusEl.className = 'session-row-status session-dropdown-status';
      statusEl.title = '';
    }
  }
  _fetchRelatedSessions();
}

// ── Clear header for a session switch ────────────────────────────────────
// Runs SYNCHRONOUSLY the instant a switch begins so the header never carries
// the OLD session's identity while the new one loads (the chat area switches
// over immediately; without this the label/agent-name/status only re-render
// after loadSessionChat's fetch resolves). Reuses the dropdown's existing
// "not loaded" state (data-loaded removed → CSS shimmers the label, dims the
// trigger, spins the chevron — the same affordance as boot). Every control is
// then re-populated in PARALLEL by its own loader once the new session's data
// arrives: _setTriggerLabel (label/icon/status/agent name), reloadExecutionMode
// + setExecutionMode (mode pill), reloadTargetDevice + setTargetDevice (target
// pill), _fetchRelatedSessions (sub-header family chips), refreshSessionChanges
// (changes badge). No fetches, no awaits — must never block the switch.
function _clearSessionHeader() {
  const dropdown = document.getElementById('session-dropdown');
  if (dropdown) {
    delete dropdown.dataset.loaded;
    delete dropdown.dataset.titling;
  }
  const label = document.getElementById('session-dropdown-label');
  if (label) label.textContent = '\u2014';
  const iconEl = document.getElementById('session-dropdown-icon');
  if (iconEl) iconEl.innerHTML = icon('bot', { size: '14px' });
  const statusEl = document.getElementById('session-dropdown-status');
  if (statusEl) {
    statusEl.innerHTML = '';
    statusEl.className = 'session-row-status session-dropdown-status';
    statusEl.title = '';
  }
  const kebab = document.getElementById('session-dropdown-kebab');
  const del = document.getElementById('session-dropdown-delete');
  if (kebab) kebab.dataset.id = '';
  if (del) del.dataset.id = '';
  const agentNameEl = document.getElementById('chat-header-agent-name');
  if (agentNameEl) {
    agentNameEl.textContent = '';
    agentNameEl.style.display = 'none';
  }
  // Sub-header family chips belong to the old session — hide until the new
  // session's /related fetch lands (mirrors the no-family state).
  const subHeader = document.getElementById('chat-sub-header');
  if (subHeader) subHeader.style.display = 'none';
  // Mode / target pills: blank now; reloadExecutionMode / reloadTargetDevice
  // fill them from the new session's localStorage keys synchronously right
  // after, and the server values (setExecutionMode / setTargetDevice) follow
  // when the fetch lands. Never shows a stale value in between.
  const modeBtn = document.getElementById('chat-mode-btn');
  if (modeBtn) modeBtn.textContent = '\u2014';
  const targetBtn = document.getElementById('chat-target-btn');
  if (targetBtn) {
    targetBtn.classList.remove('targeting');
    const labelEl = targetBtn.querySelector('.chat-target-label');
    if (labelEl) labelEl.textContent = '';
    targetBtn.title = 'Remote Control \u2014 choose which device runs your next message';
  }
  // Changes badge belongs to the old session's working tree (the 1s poll in
  // chat-session-changes.js refills it for the new id).
  const changesCount = document.getElementById('chat-changes-count');
  if (changesCount) { changesCount.textContent = '0'; changesCount.hidden = true; }
}

// ── Live tick: update the completed-time status every second ───────────
// Avoids a full poll just for ticking numbers. Only touches the trigger label.
let _completedTimeTick = null;
function _startCompletedTimeTick() {
  if (_completedTimeTick) return;
  _completedTimeTick = setInterval(() => {
    // Session dropdown status
    const statusEl = document.getElementById('session-dropdown-status');
    if (statusEl && statusEl.classList.contains('session-status-completed')) {
      const sid = app.currentSessionId;
      const found = _sessionsCache.find(s => s.id === sid);
      if (found && found.run_updated_at && ['complete','interrupted','error'].includes(found.run_status)) {
        statusEl.innerHTML = _formatRelativeTime(found.run_updated_at);
        statusEl.title = 'Completed ' + _parseDate(found.run_updated_at).toLocaleString();
      }
    }
    // Sub-header chip completion times
    const chipTimes = document.querySelectorAll('#chat-sub-scroll-wrap .chip-time');
    for (const ct of chipTimes) {
      const at = ct.dataset.completedAt;
      if (at) ct.textContent = _formatRelativeTime(at);
    }
  }, 1000);
}

// ── Session-list tree: lazy child fetch + expand/collapse ──────────────────

// Fetch a parent's family members (spawns) via /related and cache them.
// Re-renders the list once they arrive so the nested child rows appear.
// Stores [] on failure so we don't spin.
async function _ensureGroupChildren(sid) {
  if (_childrenCache.has(sid)) return;
  _childrenCache.set(sid, null);   // in-flight marker (renders a "Loading…" row)
  let kids = [];
  try {
    const token = localStorage.getItem('auth_token');
    let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/related?db=user.db`;
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
// handler (controller.js) can call it from the caret button.
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
    return `<div class="session-child-row session-child-loading"><span class="session-radial-loader sm"></span><span class="session-row-sep"> </span>Loading…</div>`;
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
      statusHtml = `<span class="session-row-status session-status-running" title="Working…"><span class="session-radial-loader sm"></span></span>`;
    }
    const text = name ? `${label} · ${name}` : label;
    html += `
      <div class="session-row session-child-row${isSel}" data-id="${kSid}" title="${(name || label).replace(/"/g, '&quot;')}">
        <span class="session-child-rail"></span>
        <span class="session-row-child-icon">${icon(roleIcon, { size: '11px' })}</span>
        <span class="session-row-title">${text.replace(/</g, '&lt;')}</span>
        ${statusHtml}
      </div>`;
  });
  return html;
}

// ── Open-menu height refresh ─────────────────────────────────────────────
// When the dropdown menu is open (or mid-open) and its content size changes —
// e.g. the async session fetch resolves AFTER the open animation measured an
// empty 0-height menu — re-lock --session-menu-open-height to the real list
// height (clamped to the space the open positioned for). Without this the
// menu stays an invisible 0-height box until the next close/open cycle.
function _refreshOpenMenuHeight(menu) {
  if (!menu || menu.hidden) return;
  const state = menu.dataset.state;
  if (state !== 'open' && state !== 'opening') return;
  const avail = menu._availableHeight || menu.scrollHeight;
  const target = Math.min(menu.scrollHeight, avail);
  if (!(target > 0)) return;
  const current = parseFloat(menu.style.getPropertyValue('--session-menu-open-height')) || 0;
  if (Math.abs(current - target) < 1) return;
  menu.style.setProperty('--session-menu-open-height', `${target}px`);
}

function _sessionRowMatches(s, q, qActive, contentMode) {
  // Recycle-bin toggle: with no query it narrows to binned rows only; with a
  // query it keeps the bin view scoped to matches within it.
  if (_binMode && !s.recycled) return false;
  if (!qActive) return true;
  const titleHit = (s.title || '').toLowerCase().includes(q)
    || (s.agent_name || '').toLowerCase().includes(q);
  // Content mode: a row counts when its title matches OR its messages contain
  // the query (count chip). Content matches appear progressively as the scan
  // batches land; before that, title hits carry the row.
  if (contentMode && q.length >= _MSG_MIN_QUERY_LEN) {
    const info = _msgMatchInfo(s.id, q);
    if (info) { _msgHitCounts.set(s.id, info.count); return true; }
    _msgHitCounts.delete(s.id);
  }
  return titleHit;
}

// Restore focus + live value + caret to the footer search input after a
// wholesale menu re-render (debounced refetch, poll, gate events). Without
// this, the rebuild would drop in-progress keystrokes and yank focus away
// mid-typing.
function _restoreSearchInput(menu, state) {
  if (!state) return;
  const inp = menu.querySelector('.session-manage-search-input');
  if (!inp) return;
  inp.value = state.value;
  inp.focus();
  try {
    inp.setSelectionRange(
      Math.min(state.s ?? state.value.length, state.value.length),
      Math.min(state.e ?? state.value.length, state.value.length),
    );
  } catch (_) { /* caret restore is best-effort */ }
}

function _renderSessionRows() {
  const menu = document.getElementById('session-dropdown-menu');
  if (!menu) return;
  // Preserve the search input's live value/caret when it is focused so the
  // re-render below never drops mid-typing keystrokes.
  const _prevSearch = menu.querySelector('.session-manage-search-input');
  const _searchState = _prevSearch && document.activeElement === _prevSearch
    ? { value: _prevSearch.value, s: _prevSearch.selectionStart, e: _prevSearch.selectionEnd }
    : null;
  delete menu.dataset.loading;
  menu.removeAttribute('aria-busy');
  menu.innerHTML = '';

  const q = _searchQuery.trim().toLowerCase();
  const qActive = q.length > 0;
  const contentMode = qActive && _searchMode === 'content';
  if (!qActive) _msgHitCounts.clear();

  const sorted = _sortSessionsByPinAndActivity(
    [..._sessionsCache].filter(s => (_showHidden || !s.hidden) && _sessionRowMatches(s, q, qActive, contentMode)),
  );
  // Only reserve the caret column (spacer on non-parent rows) when the list
  // actually holds an expandable family row — otherwise it's dead space that
  // pushes the pin/title away from the drag grip. With no families present the
  // grip sits right beside the pin/title (no gap).
  const anyParent = sorted.some(s => (s.child_count || 0) > 0);

  if (!sorted.length) {
    const empty = document.createElement('div');
    empty.className = 'session-dropdown-empty';
    empty.textContent = qActive ? 'No matching sessions'
      : (_binMode ? 'Recycle bin is empty' : 'No sessions yet');
    menu.appendChild(empty);
    // Still show the manage footer below the empty message so hidden sessions
    // remain reachable when the visible list is empty.
    _appendManageRow(menu);
    _refreshOpenMenuHeight(menu);
    _restoreSearchInput(menu, _searchState);
    return;
  }
  for (const s of sorted) {
    const hasChildren = (s.child_count || 0) > 0;
    const isExpanded = hasChildren && _expandedGroups.has(s.id);
    const row = document.createElement('div');
    row.className = 'session-row'
      + (s.pinned ? ' pinned' : '')
      + (s.recycled ? ' recycled' : '')
      + (hasChildren ? ' group-parent' : '')
      + (isExpanded ? ' expanded' : '')
      + (s.id === app.currentSessionId ? ' selected' : '');
    row.dataset.id = s.id;
    if (s.agent_id) row.dataset.agent = s.agent_id;
    // Expand caret for a family root (orchestrator); a fixed spacer keeps
    // non-parent rows' titles aligned with parents'.
    const caretHtml = hasChildren && !s.recycled
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
      } else if (engine === 'codex' && (!name || name === 'code-2')) {
        rowIconHtml = codexMark({ size: big });
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
      statusHtml = `<span class="session-row-status session-status-running" title="Agent is thinking\u2026"><span class="session-radial-loader sm"></span></span>`;
    } else if (s.run_status === 'queued') {
      const qPos = Number.isFinite(s.queue_position) ? s.queue_position : null;
      const qTot = Number.isFinite(s.queue_total) ? s.queue_total : null;
      const qLabel = qPos != null
        ? (qTot != null ? `#${qPos} of ${qTot}` : `#${qPos}`)
        : '\u23F3';
      const qTitle = qPos != null
        ? (qTot != null ? `Position ${qPos} of ${qTot} in the session queue` : `Position ${qPos} in the session queue`)
        : 'Waiting in the session queue\u2026';
      statusHtml = `<span class="session-row-status session-status-queued" title="${qTitle}">${qLabel}</span>`;
    } else if (s.run_updated_at && ['complete','interrupted','error'].includes(s.run_status)) {
      statusHtml = `<span class="session-row-status session-status-completed" title="Completed ${_parseDate(s.run_updated_at).toLocaleString()}">${_formatRelativeTime(s.run_updated_at)}</span>`;
    } else if (s.has_unread) {
      statusHtml = `<span class="session-row-status session-status-unread" title="New response ready">${icon('check-circle', { size: '12px' })}</span>`;
    }
    const _isSpawn = typeof s.id === 'string' && s.id.startsWith('spawn-');
    const spawnBadge = _isSpawn
      ? `<span class="session-row-spawn-badge" title="Spawned helper session">${icon('git-branch', { size: '11px' })}</span>`
      : '';
    // Checkbox mode: each row gets a selectable checkbox; kebab/trash/visibility hidden.
    // Recycled rows skip the checkbox (nothing to batch-delete — they're already
    // in the bin) and always show the restore-only bin badge instead.
    const checkboxHtml = _checkboxMode && !s.recycled
      ? `<button class="session-row-checkbox" data-id="${s.id}" data-checked="${_selectedSessions.has(s.id) ? '1' : '0'}">${icon(_selectedSessions.has(s.id) ? 'check-square' : 'square', { size: '14px' })}</button>`
      : '';
    const trailingBtn = s.recycled
      ? `<span class="session-row-bin" role="button" tabindex="0" data-id="${s.id}" title="In recycling bin — click to restore">bin</span>`
      : (_checkboxMode ? '' : (_showHidden
      ? `<button class="session-row-visibility" title="${s.hidden ? 'Un-hide session' : 'Hide session'}" data-id="${s.id}" data-hidden="${s.hidden ? '1' : '0'}">${icon(s.hidden ? 'eye-off' : 'eye', { size: '14px' })}</button>`
      : `<button class="session-row-kebab" title="More…" data-id="${s.id}">${icon('more-vertical', { size: '14px' })}</button>`
        + `<button class="session-row-delete" title="Delete session" data-id="${s.id}" data-state="trash">${icon('trash-2', { size: '14px' })}</button>`));
    // Content-mode hit chip: number of matching messages (see _scanMessagesForQuery).
    const msgHit = _msgHitCounts.has(s.id)
      ? `<span class="session-row-msg-hit" title="${_msgHitCounts.get(s.id)} matching message${_msgHitCounts.get(s.id) === 1 ? '' : 's'}">${_msgHitCounts.get(s.id)}</span>`
      : '';
    row.innerHTML = `
      ${checkboxHtml}
      ${caretHtml}
      <span class="session-row-pin-icon">${icon('pin', { size: '12px' })}</span>
      <span class="session-row-title" title="Hold to rename"><span class="session-row-agent-icon">${rowIconHtml}</span><span class="session-row-sep"> </span>${spawnBadge}${label.replace(/</g, '&lt;')}</span>
      ${msgHit}
      ${statusHtml}
      ${trailingBtn}
    `;
    menu.appendChild(row);
    // Nested child rows (lazily fetched) directly under an expanded parent.
    // Never for recycled rows — their family is binned too and restore-only.
    if (isExpanded && !s.recycled) menu.insertAdjacentHTML('beforeend', _childRowsHtml(s.id));
  }
  _appendManageRow(menu);
  _restoreSearchInput(menu, _searchState);

  // The async session list can resolve AFTER the open animation measured the
  // menu (a 0-height empty menu at measure time = invisible "open" state).
  // Re-measure whenever the open menu's content changes so the real list
  // expands the menu instead of staying clipped at 0 (the dropdown artifact).
  _refreshOpenMenuHeight(menu);
}

// ── Management footer row ──────────────────────────────────────────────
// A row below the session list with list-wide actions: a search bar (title or
// message-content mode) with a clear button, a recycle-bin toggle, the
// show/hide-hidden eye toggle and a checkbox-batch-delete toggle. Only shown
// when there is at least one session.
function _appendManageRow(menu) {
  if (!_sessionsCache.length) return;
  const bar = document.createElement('div');
  bar.className = 'session-manage-row';
  const eyeTitle = _showHidden ? 'Hide hidden sessions' : 'Show hidden sessions';
  const checkboxTitle = _checkboxMode ? 'Disable checkbox mode' : 'Enable checkbox mode';
  const binTitle = _binMode ? 'Show all sessions' : 'Show recycled sessions (bin)';
  const q = _searchQuery.trim();
  const deleteBtn = _checkboxMode && _selectedSessions.size > 0
    ? `<button class="session-manage-delete-selected" title="Delete selected (${_selectedSessions.size})" data-state="trash">${icon('trash-2', { size: '15px' })}</button>`
    : '';
  bar.innerHTML = `
    <span class="session-manage-search">
      ${icon('search', { size: '13px' })}
      <input class="session-manage-search-input" type="text" placeholder="Search sessions…" value="${_escAttr(_searchQuery)}" aria-label="Search sessions" spellcheck="false">
      ${q
        ? `<button class="session-manage-search-clear" title="Clear search">${icon('x', { size: '13px' })}</button>`
        : ''}
      <button class="session-manage-search-mode" title="${_searchMode === 'content' ? 'Switch to title search' : 'Search message content too'}" data-mode="${_searchMode}">${_searchMode === 'content' ? 'Content' : 'Title'}</button>
    </span>
    <span class="session-manage-actions">
      ${deleteBtn}
      <button class="session-manage-bin" title="${binTitle}" data-state="${_binMode ? 'on' : 'off'}">${icon('recycle', { size: '15px' })}</button>
      <button class="session-manage-eye" title="${eyeTitle}" data-state="${_showHidden ? 'on' : 'off'}">${icon(_showHidden ? 'eye' : 'eye-off', { size: '15px' })}</button>
      <button class="session-manage-checkbox-toggle" title="${checkboxTitle}" data-state="${_checkboxMode ? 'on' : 'off'}">${icon('square', { size: '15px' })}</button>
    </span>
  `;
  menu.appendChild(bar);
}

// ── Session-list fetch: reliability policy ─────────────────────────────────
// A transient server/replica blip must NEVER be presented as an empty account.
// Treat an empty body cautiously and distinguish exhausted request failures
// from a confirmed successful empty response. Policy:
//   • the response must be res.ok with a real `sessions` array, else it is a failure
//   • an empty body is always double-checked (one quick re-fetch); only two
//     consecutive empty responses are accepted as the truth, so a genuinely
//     empty account converges while a swallowed server error doesn't wipe the list
//   • a COLD cache keeps the skeleton phantom rows visible while retrying
//     (bounded backoff); only after the retry budget is spent do we render an
//     explicit load-error state rather than claiming the account is empty
let _sessionFetchRetryTimer = 0;
let _sessionFetchRetries = 0;
let _sessionEmptyResponses = 0;   // consecutive VALID empty responses
let _sessionIsRetryPopulate = false; // true only for fetches fired by the retry timer
const _SESSION_FETCH_MAX_RETRIES = 3;

function _scheduleSessionFetchRetry() {
  clearTimeout(_sessionFetchRetryTimer);
  if (_sessionFetchRetries >= _SESSION_FETCH_MAX_RETRIES) return;
  const delay = 700 * Math.pow(2, _sessionFetchRetries); // 700ms → 1.4s → 2.8s
  _sessionFetchRetries++;
  _sessionIsRetryPopulate = true;
  _sessionFetchRetryTimer = setTimeout(() => {
    _sessionFetchRetryTimer = 0;
    if (app.currentUserId) populateSessionSelect(app.currentUserId);
  }, delay);
}

function _cancelSessionFetchRetry() {
  clearTimeout(_sessionFetchRetryTimer);
  _sessionFetchRetryTimer = 0;
}

function _renderSessionLoadError(menu) {
  if (!menu) return;
  delete menu.dataset.loading;
  menu.removeAttribute('aria-busy');
  menu.replaceChildren();
  const error = document.createElement('div');
  error.className = 'session-dropdown-empty';
  error.textContent = 'Unable to load sessions';
  menu.appendChild(error);
}

async function populateSessionSelect(userId, { preferCache = false } = {}) {
  const menu = document.getElementById('session-dropdown-menu');
  if (!userId) {
    _cancelSessionFetchRetry();
    _sessionsCache = [];
    if (menu) menu.innerHTML = '';
    _setTriggerLabel();
    return;
  }
  const isDefaultScope = !_showHidden && !_binMode && !_searchQuery.trim();
  if (preferCache && !storageAdapter.isBrowser && isDefaultScope && _canUseActiveSessionCache(userId)) {
    _sessionsCache = _activeSessionCache.sessions.map(s => ({ ...s }));
    _sortSessionsByPinAndActivity(_sessionsCache);
    _renderSessionRows();
    _setTriggerLabel();
    return true;
  }
  const mySeq = ++_sessionFetchSeq;
  // A fetch triggered by the retry timer keeps the current retry budget; any
  // user/poll-initiated fetch starts a fresh one, so opening the dropdown
  // always gets a full skeleton + retry window even after a background burst.
  if (!_sessionIsRetryPopulate) _sessionFetchRetries = 0;
  _sessionIsRetryPopulate = false;
  let sessions = null;
  let failed = false;
  try {
    if (storageAdapter.isBrowser) {
      const raw = await storageAdapter.listSessions(userId);
      if (!Array.isArray(raw)) throw new Error('Malformed session list');
      sessions = raw.map(s => ({ ...s, recycled: !!s.recycled }));
    } else {
      const token = localStorage.getItem('auth_token');
      const q = _searchQuery.trim();
      const searchActive = q.length > 0;
      // limit=0 asks the server for the FULL session list (no cap), so the
      // dropdown shows every session — matching the Sessions page, which reads
      // the same data from /session-stats with no limit. The server also
      // ignores the row cap while q is set, so title search stays exact.
      let url = `/api/v1/db/sessions?db=user.db&user_id=${encodeURIComponent(userId)}&limit=0`;
      // include_manifest=0: this list only renders title/agent/status — it
      // never reads manifest fields (authority_revision/content_hash/…), so
      // skip the per-session manifest computation entirely (it was the list's
      // real N+1). Storage/sync consumers keep the default include_manifest=1.
      url += `&include_manifest=0`;
      if (_showHidden) url += `&include_hidden=1`;
      // Bin rows come back when the toggle is on OR a search is active (a name
      // search must be able to surface recycled sessions, like the Sessions
      // page cross-catalog search).
      if (_binMode || searchActive) url += `&include_recycled=1`;
      if (searchActive) url += `&q=${encodeURIComponent(q)}`;
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (!data || !Array.isArray(data.sessions)) throw new Error('Malformed session list');
      sessions = data.sessions.map(_normaliseSessionMetadata).filter(Boolean);
    }
  } catch (e) {
    failed = true;
    console.warn('Failed to load sessions:', e);
  }
  if (mySeq !== _sessionFetchSeq) return;

  // ── Failure path: never clobber; keep what the user can see; retry ──
  if (failed) {
    _scheduleSessionFetchRetry();
    if (_sessionsCache.length) {
      // Warm cache: keep showing the real list (never the stub-only state).
      _renderSessionRows();
    } else if (_sessionFetchRetries >= _SESSION_FETCH_MAX_RETRIES) {
      // A failed request is not evidence of an empty account. Stop the
      // skeleton after the retry budget, but report the load failure honestly.
      _renderSessionLoadError(menu);
    }
    // Cold cache + budget remaining: leave the skeleton phantom rows in place.
    _setTriggerLabel();
    return;
  }

  // ── Empty-but-valid response: always double-check before adopting it ──
  // A search/bin "no matches" result is legitimate and adopted immediately.
  const searchOrBin = !!(_binMode || _searchQuery.trim());
  if (sessions.length === 0) {
    if (searchOrBin) {
      _sessionEmptyResponses = 0;
    } else {
      _sessionEmptyResponses++;
      if (_sessionEmptyResponses < 2) {
        // The first empty could be a swallowed server error. Keep warm rows on
        // screen, or leave the cold-load skeleton in place, and verify once.
        _scheduleSessionFetchRetry();
        if (_sessionsCache.length) _renderSessionRows();
        _setTriggerLabel();
        return;
      }
      // This empty result was confirmed. Reset the counter so a later,
      // unrelated empty response must earn confirmation again.
      _sessionEmptyResponses = 0;
    }
  } else {
    _sessionEmptyResponses = 0;
  }

  _cancelSessionFetchRetry();
  _sessionFetchRetries = 0;
  if (isDefaultScope) primeSessionMetadataCache(userId, sessions);
  _sessionsCache = sessions;
  if (app.currentSessionId && !_sessionsCache.some(s => s.id === app.currentSessionId)) {
    _sessionsCache.push({
      id: app.currentSessionId,
      title: 'New Session',
      agent_id: app.currentAgentId || null,
      agent_name: '',
      agent_icon: '',
      agent_engine: '',
      created_at: null,
      updated_at: null,
      pinned: false,
      sort_order: null,
      hidden: false,
      recycled: false,
      run_status: null,
      has_unread: false,
    });
  }
  _sortSessionsByPinAndActivity(_sessionsCache);
  _renderSessionRows();
  _setTriggerLabel();

  // ── Phantom-cell hydration ────────────────────────────────────────────
  // Paint the list immediately (rows already carry server-enriched agent
  // fields when the agent plane is healthy). Then fill any agent display
  // fields this payload didn't carry — cold cache, orphaned, or deleted
  // agents — with ONE batched lean request, and repaint only if something
  // landed. The display cache also warms the header fallbacks (_agentNameFor
  // / _agentIconFor) for the current agent on a cold boot.
  const sessionAgentIds = [...new Set(_sessionsCache.map(s => s.agent_id).filter(Boolean))];
  if (sessionAgentIds.length) {
    hydrateAgentDisplay(sessionAgentIds, userId).then(recs => {
      if (recs && recs.length && mySeq === _sessionFetchSeq) {
        _renderSessionRows();
        _setTriggerLabel();
      }
    });
  }
  return false;
}

// The picker always paints the local SQLite mirror first.  Once it is visible,
// check the shared authority for newer *session metadata* only.  The API applies
// changes to SQLite before replying; reload the local list only when something
// actually changed, avoiding flicker and redundant renders on an unchanged open.
async function refreshSessionMetadata(userId) {
  if (!userId) return false;
  if (storageAdapter.isBrowser) return false; // no remote authority
  try {
    const token = localStorage.getItem('auth_token');
    let url = `/api/v1/db/sessions/refresh?db=user.db&user_id=${encodeURIComponent(userId)}`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(apiPath(url), { method: 'POST', headers: authHeaders() });
    if (!res.ok) return false;
    const data = await res.json();
    if (!data.changed) return false;
    await populateSessionSelect(userId);
    return true;
  } catch (e) {
    console.warn('Failed to refresh session metadata:', e);
    return false;
  }
}

// ── Gate-queue live refresh ────────────────────────────────────────────
// Called from agentWs.js when agent_status: queued fires. Triggers an
// immediate session-list refresh so the dropdown shows the queue position
// without waiting for the next 15s poll.

async function onSessionGateQueue(event) {
  if (!event || !event.session_id) return;
  // If the session is already in our cache, don't wait for a full reload —
  // just re-render with the queued status patched in.
  const found = _sessionsCache.find(s => s.id === event.session_id);
  if (found) {
    found.run_status = 'queued';
    found.run_updated_at = null;
    found.queue_position = Number.isFinite(event.queue_position) ? event.queue_position : null;
    found.queue_total = Number.isFinite(event.queue_total) ? event.queue_total : null;
    _renderSessionRows();
    _setTriggerLabel();
  } else {
    // Session not yet in cache — do a full refresh.
    try { await populateSessionSelect(app.currentUserId); } catch (_) { /* ignore */ }
  }
}

app.onSessionGateQueue = onSessionGateQueue;

function onSessionGateRunning(event) {
  if (!event || !event.session_id) return;
  const found = _sessionsCache.find(s => s.id === event.session_id);
  if (!found) return;
  found.run_status = 'running';
  found.run_updated_at = null;
  found.queue_position = null;
  found.queue_total = null;
  _renderSessionRows();
  _setTriggerLabel();
}

app.onSessionGateRunning = onSessionGateRunning;

// Called after a successful force-run: patch the cached session row from
// queued → running so the dropdown reflects it immediately (the 15s poll
// would otherwise be the next update). Clear the queue fields even if the
// cached row lost its run_status, and refresh the trigger label so the
// header's queue badge (e.g. "#1/2") disappears at once, not just the row.

async function onSessionGateForce() {
  const sid = app.currentSessionId;
  if (!sid) return;
  const found = _sessionsCache.find(s => s.id === sid);
  if (found && (found.run_status === 'queued' || found.queue_position != null)) {
    found.run_status = 'running';
    found.run_updated_at = null;
    found.queue_position = null;
    found.queue_total = null;
    _renderSessionRows();
    _setTriggerLabel();
  }
}

app.onSessionGateForce = onSessionGateForce;

export {
  _sessionsCache,
  primeSessionMetadataCache,
  hasFreshSessionMetadataCache,
  _sortSessionsByPinAndActivity,
  populateSessionSelect,
  refreshSessionMetadata,
  _renderSessionRows,
  _setTriggerLabel,
  _clearSessionHeader,
  _fetchRelatedSessions,
  applySessionTitle,
  _formatRelativeTime,
  _truncate,
  _setShowHidden,
  _getShowHidden,
  _setCheckboxMode,
  _getCheckboxMode,
  _selectedSessions,
  _clearSelected,
  toggleSessionGroup,
  _startCompletedTimeTick,
  _setSearchQuery,
  _getSearchQuery,
  _setSearchMode,
  _getSearchMode,
  _setBinMode,
  _getBinMode,
  _scanMessagesForQuery,
  _clearMsgSearch,
  _scheduleSearchCommit,
  _cancelPendingSearchCommit,
};
