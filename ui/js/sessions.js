'use strict';

import { app } from './state.js';
import { loopSessionChanged } from './loop.js';
import { loopVisualSessionChanged } from './loop-logic.js';
import { autoAgentSessionChanged } from './autoagent.js';
import { chatActivitySessionChanged } from './chat-activity.js';
import { abortChatStream } from './chat.js';
import { consumeReplayedEventsFor } from './agentWs.js';
import { apiPath } from './config.js';
import { icon } from './icons.js';
import { renderAvatar } from './user-avatar.js';
import {
  listAccounts,
  getActive,
  removeAccount,
  switchTo,
  onChange as onAccountsChange,
} from './accounts.js';
import { showLeftOverlay, authHeaders } from './left-login.js';
import { ensureFreshToken } from './auth-refresh.js';
import { randomUUID } from './uuid.js';
import {
  getPinnedAgents,
  toggleAgentPin as _toggleAgentPinStore,
  sortAgentsForDisplay,
  persistAgentOrder,
  persistSessionOrder,
  makeRowsReorderable,
  attachRowLongPress,
} from './ordering.js';

// ── Message cache + infinite-scroll state ────────────────────────────────────
// Keyed by sessionId. Each entry: { messages: [...], hasMore: bool, loadedAll: bool }
const _messageCache = new Map();
const _CACHE_TTL_MS = 60000; // 60 seconds
const _loadingSessions = new Set();
let _scrollListener = null; // the active scroll listener (one at a time)

// ── Virtual-scroll state ────────────────────────────────────────────────────
// WeakMap<msgId, offsetHeight> — measured after first render of each bubble
const _bubbleHeights = new Map();
// Set of msgIds currently rendered as placeholders (not real bubbles)
const _placeholderIds = new Set();
// The virtual-scroll scroll handler (bound once per session load)
let _virtualScrollHandler = null;
// Buffer zone in px above/below viewport
const _VS_BUFFER = 400;
// Refs to the real addChatBubble / _createBubble for recycling
let _origAddChatBubble = null;
let _origCreateBubble = null;
let _totalUnpinnedCount = 0; // total unpinned sessions on the server (for "+ N more" row)

// ── Last-active session per agent ──────────────────────────────────────────
// Map<agentId, sessionId> — persisted in localStorage so switching back to an
// agent resumes the session you were last using with it.
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

export function generateUUID() {
  return randomUUID();
}

// ── webAgent (the do-it-all agent) ───────────────────────────────────────────
// All page chat pills (Agents / Pages / Source Control / Agent Settings) and the
// "no agent yet" chat fallback target one agent: the user's instance of the
// `default` template (named "webAgent"). We find-or-create exactly one per user
// so there is a single shared do-it-all agent, then start a fresh session on it.
const WEBAGENT_TEMPLATE_ID = 'default';
const _webagentIdCache = new Map(); // userId → agentId

async function findWebagentAgent(userId) {
  if (_webagentIdCache.has(userId)) return _webagentIdCache.get(userId);
  try {
    const res = await fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`));
    if (!res.ok) return null;
    const data = await res.json();
    const match = (data.agents || []).find(a => a.template_id === WEBAGENT_TEMPLATE_ID);
    if (match) { _webagentIdCache.set(userId, match.id); return match.id; }
  } catch (e) {
    console.warn('[webAgent] find failed:', e);
  }
  return null;
}

async function createWebagentAgent(userId) {
  const res = await fetch(apiPath('/api/v1/agents'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_id: userId,
      name: 'webAgent',
      description: 'Your all-purpose webAgent — chat, tools, web, browser, code, pages, and source control.',
      template_id: WEBAGENT_TEMPLATE_ID,
    }),
  });
  if (!res.ok) {
    let detail = `agent create failed (${res.status})`;
    try { const e = await res.json(); detail = e.detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const data = await res.json();
  const id = data.agent && data.agent.id;
  if (!id) throw new Error('agent create returned no id');
  _webagentIdCache.set(userId, id);
  if (typeof app.populateAgentSelect === 'function') {
    try { await app.populateAgentSelect(userId); } catch (_) {}
  }
  return id;
}

export async function ensureWebagentAgent(userId) {
  if (!userId) throw new Error('not signed in');
  return (await findWebagentAgent(userId)) || (await createWebagentAgent(userId));
}

/**
 * Interrupt the backend agent loop for a session (best-effort, fire-and-forget).
 * Tells the server to gracefully stop an in-flight run. NOTE: this is now only
 * used when the session is being DELETED — leaving / switching / closing no
 * longer interrupts a run (it keeps going server-side and is viewable from any
 * device). Interrupt remains available via the explicit Stop button.
 */
function interruptSession(sessionId) {
  if (!sessionId) return;
  fetch(apiPath('/api/v1/chat/interrupt'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => { /* best-effort */ });
}

// ── Agent selector ───────────────────────────────────────────────────────────

// Cache of last-fetched agents (templates + customs, in display order)
let _agentsCache = [];

// The header "+" new-session button starts as a spinner and only becomes a
// clickable plus once the agent list has loaded, so early clicks during the
// cold-start window (before the handler/agents are ready) don't half-fire.
// Flipped on by _setNewSessionBtnReady() at the end of populateAgentSelect.
let _newSessionBtnReady = false;
function _setNewSessionBtnReady() {
  _newSessionBtnReady = true;
  const btn = document.getElementById('session-new');
  if (!btn || !btn.classList.contains('loading')) return;
  btn.classList.remove('loading');
  btn.disabled = false;
  btn.title = 'New session';
  btn.innerHTML = icon('plus', { size: '14px' });
}

function _toggleAgentPin(agentId) {
  const pinnedNow = _toggleAgentPinStore(app.currentUserId, agentId);
  // Refresh agents cache (pinned flag) and re-render. sortAgentsForDisplay
  // floats pinned to the top while preserving the synced server order.
  _agentsCache = _agentsCache.map(a => ({ ...a, pinned: pinnedNow.has(a.id) }));
  _agentsCache = sortAgentsForDisplay(_agentsCache, app.currentUserId);
  _renderAgentRows();
  _setAgentTriggerLabel();
  // Keep the Agents page in sync (pinned floats to the top there too).
  if (typeof app.refreshAgentsOrder === 'function') app.refreshAgentsOrder();
}

function _setAgentTriggerLabel() {
  let labelEl = document.getElementById('agent-dropdown-label');
  const trigger = document.getElementById('agent-dropdown-trigger');
  // If the label was replaced by a rename input, restore the label span
  if (!labelEl && trigger) {
    const input = trigger.querySelector('.session-row-title-input');
    if (input) {
      labelEl = document.createElement('span');
      labelEl.id = 'agent-dropdown-label';
      input.replaceWith(labelEl);
    }
  }
  if (!labelEl) return;
  const aid = app.currentAgentId;
  const found = _agentsCache.find(a => a.id === aid);
  const title = (found && found.name) || (window.__agentName) || aid || 'No agent';
  labelEl.textContent = _truncate(title, 20);
  labelEl.title = (found && found.name) || title || '';
  // Click the label to re-enter rename mode
  labelEl.addEventListener('click', (e) => {
    e.stopPropagation();
    _headerRenameAgent();
  });
  // Mark the agent dropdown as loaded (removes shimmer + re-enables trigger)
  const agentDropdown = document.getElementById('agent-dropdown');
  if (agentDropdown) agentDropdown.dataset.loaded = 'true';
  // Status icon: check if any session for this agent is running or has unread
  const statusEl = document.getElementById('agent-dropdown-status');
  if (statusEl) {
    const hasRunning = _sessionsCache.some(s => s.run_status === 'running');
    const hasUnread = _sessionsCache.some(s => s.has_unread);
    if (hasRunning) {
      statusEl.innerHTML = icon('loader-2', { size: '12px' });
      statusEl.className = 'agent-dropdown-status session-status-running';
      statusEl.title = 'Agent is thinking…';
    } else if (hasUnread) {
      statusEl.innerHTML = icon('check-circle', { size: '12px' });
      statusEl.className = 'agent-dropdown-status session-status-unread';
      statusEl.title = 'New response ready';
    } else {
      statusEl.innerHTML = '';
      statusEl.className = 'agent-dropdown-status';
      statusEl.title = '';
    }
  }
  // TUI bridge indicator: show when the selected agent is the TUI bridge agent
  const tuiIndicator = document.getElementById('tui-bridge-indicator');
  if (tuiIndicator) {
    const isTuiAgent = found && (found.template_id === 'web-agent-tui' || found.trigger_type === 'tui_bridge');
    if (isTuiAgent) {
      // Check if the bridge is alive
      fetch('/api/v1/chat/tui-bridge/status')
        .then(r => r.json())
        .then(data => {
          tuiIndicator.style.display = 'inline';
          tuiIndicator.title = data.alive
            ? 'TUI agent bridge active (port ' + data.port + ')'
            : 'TUI agent bridge not connected — start the Server Manager (TUI)';
          tuiIndicator.style.opacity = data.alive ? '1' : '0.4';
        })
        .catch(() => {
          tuiIndicator.style.display = 'inline';
          tuiIndicator.style.opacity = '0.4';
          tuiIndicator.title = 'TUI agent bridge unavailable';
        });
    } else {
      tuiIndicator.style.display = 'none';
    }
  }
  _updateHeaderSessionCounter();
}

function _renderAgentRows() {
  const menu = document.getElementById('agent-dropdown-menu');
  if (!menu) return;
  menu.innerHTML = '';
  if (!_agentsCache.length) {
    const empty = document.createElement('div');
    empty.className = 'agent-dropdown-empty';
    empty.textContent = 'No agents yet';
    menu.appendChild(empty);
    return;
  }
  // Insert visual separator between templates and customs (unpinned only)
  let lastType = null;
  for (const a of _agentsCache) {
    if (lastType !== null && lastType !== a.type && !a.pinned) {
      const sep = document.createElement('div');
      sep.className = 'agent-row-sep';
      menu.appendChild(sep);
    }
    lastType = a.type;
    const row = document.createElement('div');
    row.className = 'agent-row-item' + (a.pinned ? ' pinned' : '') + (a.id === app.currentAgentId ? ' selected' : '');
    row.dataset.id = a.id;
    row.dataset.type = a.type;
    const label = a.name || a.id.slice(0, 12);
    const configBtn = a.type === 'custom'
      ? `<button class="agent-row-config" title="Configure agent" data-id="${a.id}">${icon('settings', { size: '14px' })}</button>`
      : '';
    const hasRunning = !!_agentRunningStatus[a.id];
    const statusHtml = hasRunning
      ? `<span class="session-row-status session-status-running" title="Agent is thinking…">${icon('loader-2', { size: '12px' })}</span>`
      : '';
    row.innerHTML = `
      <span class="row-drag-handle" data-drag-handle title="Drag to reorder · hold to pin">${icon('grip-vertical', { size: '13px' })}</span>
      <span class="agent-row-pin-icon">${icon('pin', { size: '12px' })}</span>
      ${statusHtml}
      <span class="agent-row-title" title="Hold to rename">${_truncate(label, 28).replace(/</g, '&lt;')}</span>
      ${configBtn}
      <button class="agent-row-delete" title="Delete agent" data-id="${a.id}">${icon('trash-2', { size: '14px' })}</button>
    `;
    menu.appendChild(row);
  }
}

/**
 * Apply a drag-to-reorder result for the agent dropdown: reorder the cache to
 * the dropped sequence, re-float pinned agents to the top so storage matches
 * display, persist the synced order, then keep the Agents page in sync.
 */
function _applyAgentReorder(orderedIds) {
  const byId = new Map(_agentsCache.map(a => [a.id, a]));
  const next = orderedIds.map(id => byId.get(id)).filter(Boolean);
  // Defensive: keep any row the DOM didn't report so nothing is dropped.
  for (const a of _agentsCache) if (!orderedIds.includes(a.id)) next.push(a);
  _agentsCache = sortAgentsForDisplay(next, app.currentUserId);
  _renderAgentRows();
  _setAgentTriggerLabel();
  persistAgentOrder(app.currentUserId, _agentsCache.map(a => a.id));
  if (typeof app.refreshAgentsOrder === 'function') app.refreshAgentsOrder();
}

export async function populateAgentSelect(userId) {
  if (!userId) return;

  try {
    // Share the agent data with agents.js so it doesn't re-fetch.
    let agentsData = window.__agentsSharedData;
    if (!agentsData) {
      const agentsRes = await fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`));
      agentsData = agentsRes.ok ? await agentsRes.json() : { agents: [] };
      window.__agentsSharedData = agentsData;
    }

    const saved = localStorage.getItem('selectedAgentId');
    const pinned = getPinnedAgents(userId);

    // Only the user's actual custom agents appear in the chat-header dropdown.
    // System templates are creation seeds, not chat targets — they're surfaced
    // in the "New agent" modal's template picker (see agents.js). The server
    // returns them in synced sort_order; sortAgentsForDisplay then floats pins.
    const customs = agentsData.agents || [];

    _agentsCache = customs.map(a => ({
      id: a.id,
      name: a.name || a.id.slice(0, 12),
      type: 'custom',
      pinned: pinned.has(a.id),
    }));
    _agentsCache = sortAgentsForDisplay(_agentsCache, userId);

    // Pre-select order:
    //   1. __agentId  — viewing a public agent URL
    //   2. saved      — last agent the user selected (localStorage)
    //   3. first custom agent the user owns
    //   4. empty      — chat send opens the new-agent modal
    let found = null;
    if (window.__agentId) {
      found = _agentsCache.find(a => a.id === window.__agentId);
      if (!found) {
        // Synthetic entry so chat sends the correct UUID
        _agentsCache = [{
          id: window.__agentId,
          name: window.__agentName || window.__agentId.slice(0, 12),
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

    // Lock the trigger when visiting a public agent URL
    const trigger = document.getElementById('agent-dropdown-trigger');
    if (trigger) {
      trigger.disabled = !!window.__agentId;
      trigger.style.pointerEvents = window.__agentId ? 'none' : '';
      trigger.style.opacity = window.__agentId ? '0.6' : '';
    }

    _renderAgentRows();
    _setAgentTriggerLabel();

    // Agent names just became available — the unified session dropdown prefixes
    // every row with its agent's name, so refresh any already-rendered rows so
    // they show real names instead of the id fallback.
    if (_sessionsCache.length) {
      _renderSessionRows();
      _setTriggerLabel();
    }

    // Sessions are scoped to an agent — refresh the session list now that
    // currentAgentId is settled. Deferred: sessions are fetched lazily when
    // the user first opens the session dropdown (see openMenu below).
    // populateSessionSelect(userId);

    // Fetch running status for all agents so the dropdown shows spinners
    // next to agents that are currently thinking.
    _fetchAgentRunningStatuses();
  } catch (e) {
    console.warn('Failed to load agents for selector:', e);
  } finally {
    // Agents are loaded (or failed) — turn the spinner into a clickable +.
    _setNewSessionBtnReady();
  }
}

export async function populateUserSelect() {
  // Header avatar (letter icon) + tooltip with full username
  const slot = document.getElementById('top-user-avatar-slot');
  if (slot) {
    slot.innerHTML = '';
    const active = getActive();
    const acct = active || {
      display_name: app.currentUserId || 'None',
      username: app.currentUserId || '',
    };
    slot.appendChild(renderAvatar(acct, 'sm'));
    const trigger = document.getElementById('top-user-id');
    if (trigger) trigger.title = acct.username || acct.display_name || '';
  }
  // Re-render the dropdown contents so the current-row + other accounts stay fresh
  renderUserDropdown();
  // Refresh the agent list, which sets currentAgentId. Sessions are now
  // loaded lazily when the user first opens the session dropdown (see
  // openMenu in initSessions). Skipped while the session menu is open so
  // the user doesn't lose their place mid-click.
  if (app.currentUserId) {
    const menu = document.getElementById('session-dropdown-menu');
    const isOpen = menu && !menu.hidden;
    if (!isOpen) {
      populateAgentSelect(app.currentUserId);
    }
  }
}

/** Render the contents of #user-dropdown-menu's dynamic sections. */
export function renderUserDropdown() {
  const active = getActive();
  const all = listAccounts();

  // Current user row (large avatar + name + email) — or inline login form
  const cur = document.getElementById('dropdown-current-row');
  if (cur) {
    cur.innerHTML = '';
    if (active) {
      // Reset any inline styles that the login form may have set
      cur.style.padding = '';
      cur.style.flexDirection = '';
      cur.style.alignItems = '';
      cur.style.gap = '';
      cur.appendChild(renderAvatar(active, 'lg'));
      const info = document.createElement('div');
      info.className = 'user-dropdown-current-info';
      const nameEl = document.createElement('div');
      nameEl.className = 'user-dropdown-current-name';
      nameEl.textContent = active.display_name || active.username || 'User';
      const emailEl = document.createElement('div');
      emailEl.className = 'user-dropdown-current-email';
      emailEl.textContent = active.username || '';
      info.appendChild(nameEl);
      info.appendChild(emailEl);
      cur.appendChild(info);
    } else {
      // Inline login form inside the dropdown — no full-page modal
      cur.style.padding = '10px 14px 6px';
      cur.style.flexDirection = 'column';
      cur.style.alignItems = 'stretch';
      cur.style.gap = '8px';
      cur.innerHTML = `
        <div style="font-size:13px;font-weight:600;color:#a9b1d6;margin-bottom:2px;">Sign in</div>
        <input type="email" id="dd-login-email" placeholder="Email" autocomplete="username" style="
          width:100%;padding:8px 10px;background:#0d0d1a;border:1px solid #2a2a4a;
          border-radius:6px;color:#c0caf5;font-size:13px;font-family:inherit;
          outline:none;box-sizing:border-box;
        ">
        <input type="password" id="dd-login-pass" placeholder="Password" autocomplete="current-password" style="
          width:100%;padding:8px 10px;background:#0d0d1a;border:1px solid #2a2a4a;
          border-radius:6px;color:#c0caf5;font-size:13px;font-family:inherit;
          outline:none;box-sizing:border-box;
        ">
        <div style="display:flex;align-items:center;gap:6px;">
          <input type="checkbox" id="dd-login-remember" checked style="accent-color:#7dcfff;margin:0;">
          <label for="dd-login-remember" style="font-size:12px;color:#565f89;cursor:pointer;">Remember me</label>
        </div>
        <button id="dd-login-btn" style="
          width:100%;padding:8px;background:#7dcfff;border:none;border-radius:6px;
          color:#0d0d1a;font-size:14px;font-weight:700;cursor:pointer;
        ">Sign In</button>
        <div id="dd-login-error" style="color:#fb4934;font-size:12px;display:none;"></div>
        <div id="dd-login-loading" style="color:#565f89;font-size:12px;text-align:center;display:none;">Signing in…</div>
      `;

      // Wire up the inline login form
      const emailInput = document.getElementById('dd-login-email');
      const passInput = document.getElementById('dd-login-pass');
      const loginBtn = document.getElementById('dd-login-btn');
      const errorEl = document.getElementById('dd-login-error');
      const loadingEl = document.getElementById('dd-login-loading');

      async function doInlineLogin() {
        const email = emailInput.value.trim();
        const password = passInput.value;
        const rememberMe = document.getElementById('dd-login-remember').checked;
        errorEl.style.display = 'none';
        loginBtn.disabled = true;
        loadingEl.style.display = 'block';
        try {
          const res = await fetch('/api/v1/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password, remember_me: rememberMe }),
          });
          if (!res.ok) {
            errorEl.style.display = 'block';
            errorEl.textContent = res.status === 403 ? 'Account pending approval' : 'Invalid email or password';
            loginBtn.disabled = false;
            loadingEl.style.display = 'none';
            return;
          }
          const data = await res.json();
          const { upsertAccount } = await import('./accounts.js');
          upsertAccount({
            user_id: data.user_id,
            username: data.username,
            display_name: data.display_name,
            access_token: data.access_token,
            remember_token: data.remember_token || '',
          });
          window.location.reload();
        } catch (err) {
          errorEl.style.display = 'block';
          errorEl.textContent = 'Connection error';
          loginBtn.disabled = false;
          loadingEl.style.display = 'none';
        }
      }

      loginBtn.addEventListener('click', doInlineLogin);
      passInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doInlineLogin(); });
      emailInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') passInput.focus(); });
    }
  }

  // Other accounts (non-active)
  const list = document.getElementById('dropdown-other-accounts');
  if (list) {
    list.innerHTML = '';
    const others = all.filter((a) => !active || a.user_id !== active.user_id);
    for (const acct of others) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'user-dropdown-account-row';
      row.dataset.userId = acct.user_id;
      row.appendChild(renderAvatar(acct, 'md'));
      const text = document.createElement('div');
      text.className = 'user-dropdown-account-info';
      text.innerHTML = `
        <div class="user-dropdown-account-name"></div>
        <div class="user-dropdown-account-email"></div>
      `;
      text.querySelector('.user-dropdown-account-name').textContent = acct.display_name || acct.username || acct.user_id;
      text.querySelector('.user-dropdown-account-email').textContent = acct.username || '';
      row.appendChild(text);
      row.addEventListener('click', async (e) => {
        e.stopPropagation();
        const ok = await switchTo(acct.user_id);
        if (ok) {
          window.location.reload();
        } else {
          // recall failed — fall back to login overlay
          showLeftOverlay();
        }
      });
      list.appendChild(row);
    }
  }

  // Toggle visibility of signed-in-only elements
  const manageBtn = document.getElementById('btn-manage-account');
  const addBtn = document.getElementById('btn-add-account');
  const signoutBtn = document.getElementById('btn-signout-header');
  const dividers = document.querySelectorAll('#user-dropdown-menu > .user-dropdown-divider');
  const otherAccounts = document.getElementById('dropdown-other-accounts');
  if (active) {
    if (manageBtn) manageBtn.style.display = '';
    if (addBtn) addBtn.style.display = '';
    if (signoutBtn) signoutBtn.style.display = '';
    dividers.forEach(d => d.style.display = '');
    if (otherAccounts) otherAccounts.style.display = '';
  } else {
    if (manageBtn) manageBtn.style.display = 'none';
    if (addBtn) addBtn.style.display = 'none';
    if (signoutBtn) signoutBtn.style.display = 'none';
    dividers.forEach(d => d.style.display = 'none');
    if (otherAccounts) otherAccounts.style.display = 'none';
  }

  // Refresh Lucide icons for any newly-inserted SVG containers
  try {
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      window.lucide.createIcons();
    }
  } catch (_) {}
}

// Cache of last-fetched sessions (used when rendering rows without refetch)
let _sessionsCache = [];

// Map of agentId → boolean indicating whether that agent has running sessions.
// Populated by _fetchAgentRunningStatuses() and used by _renderAgentRows() to
// show a spinner next to agents that are currently thinking.
let _agentRunningStatus = {};

// Monotonic counter — on init we fire one fetch from populateUserSelect (no
// agent filter yet) and another from populateAgentSelect (filtered once the
// agent is resolved). Whichever resolves last would otherwise win, so we
// tag each call and drop responses from stale calls.
let _sessionFetchSeq = 0;

/**
 * Fetch running session statuses for all agents the user owns, building a
 * map of agentId → hasRunning. Called after the agent list is populated so
 * _renderAgentRows() can show a spinner next to agents with active runs.
 */
async function _fetchAgentRunningStatuses() {
  const userId = app.currentUserId;
  if (!userId) return;
  try {
    const token = localStorage.getItem('auth_token');
    let url = `/api/v1/db/sessions?db=local.db&user_id=${encodeURIComponent(userId)}&limit=50`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(apiPath(url));
    if (!res.ok) return;
    const data = await res.json();
    const map = {};
    for (const s of (data.sessions || [])) {
      if (s.run_status === 'running' && s.agent_id) {
        map[s.agent_id] = true;
      }
    }
    _agentRunningStatus = map;
    // Re-render agent rows so the status sprites appear
    _renderAgentRows();
  } catch (e) {
    // Silently fail — the spinner is a nice-to-have, not critical
  }
}

function _truncate(s, n) {
  return (s && s.length > n) ? s.slice(0, n) + '…' : (s || '');
}

/**
 * Resolve an agent id to its display name using the cached agent list.
 * The header session dropdown spans every agent, so each row is prefixed
 * with the owning agent's name. Falls back to a short id slice (or "Agent")
 * when the agent isn't in the cache (e.g. orphaned/legacy sessions).
 */
function _agentNameFor(agentId) {
  if (!agentId) return 'Agent';
  const found = _agentsCache.find(a => a.id === agentId);
  if (found && found.name) return found.name;
  return agentId.slice(0, 8);
}

function _setTriggerLabel() {
  let labelEl = document.getElementById('session-dropdown-label');
  const trigger = document.getElementById('session-dropdown-trigger');
  // If the label was replaced by a rename input, restore the label span
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
  // Show "Agent name — Session title" so the single dropdown makes clear which
  // agent the active session belongs to.
  const agentId = (found && found.agent_id) || app.currentAgentId;
  const agentName = _agentNameFor(agentId);
  labelEl.textContent = _truncate(agentName, 14) + ' — ' + _truncate(title, 18);
  labelEl.title = ((found && found.id) || sid || '');
  // Click the label to re-enter rename mode
  labelEl.addEventListener('click', (e) => {
    e.stopPropagation();
    _headerRenameSession();
  });
  // Mark the session dropdown as loaded (removes shimmer + re-enables trigger)
  const sessionDropdown = document.getElementById('session-dropdown');
  if (sessionDropdown) sessionDropdown.dataset.loaded = 'true';
  // Status icon in the trigger button
  const statusEl = document.getElementById('session-dropdown-status');
  if (statusEl) {
    if (found && found.run_status === 'running') {
      statusEl.innerHTML = icon('loader-2', { size: '12px' });
      statusEl.className = 'session-dropdown-status session-status-running';
      statusEl.title = 'Agent is thinking…';
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
  _updateHeaderSessionCounter();
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

function _renderSessionRows() {
  const menu = document.getElementById('session-dropdown-menu');
  if (!menu) return;
  menu.innerHTML = '';

  // Sort by last active (updated_at), newest first
  // Pinned sessions float to the top in their existing pinned order
  const sorted = [..._sessionsCache].sort((a, b) => {
    if (a.pinned && !b.pinned) return -1;
    if (!a.pinned && b.pinned) return 1;
    const aTime = a.updated_at ? new Date(a.updated_at).getTime() : 0;
    const bTime = b.updated_at ? new Date(b.updated_at).getTime() : 0;
    return bTime - aTime;
  });

  if (!sorted.length) {
    const empty = document.createElement('div');
    empty.className = 'session-dropdown-empty';
    empty.textContent = 'No sessions yet';
    menu.appendChild(empty);
    return;
  }
  for (const s of sorted) {
    const row = document.createElement('div');
    row.className = 'session-row' + (s.pinned ? ' pinned' : '') + (s.id === app.currentSessionId ? ' selected' : '');
    row.dataset.id = s.id;
    if (s.agent_id) row.dataset.agent = s.agent_id;
    const label = s.title || 'New Session';
    // Each row spans the whole agent list, so prefix the title with the owning
    // agent's name: "Agent name — Session title".
    const agentName = _agentNameFor(s.agent_id);
    // Status indicator: spinning loader for running, checkmark for completed-unread, dot for read
    let statusHtml = '';
    if (s.run_status === 'running') {
      statusHtml = `<span class="session-row-status session-status-running" title="Agent is thinking…">${icon('loader-2', { size: '12px' })}</span>`;
    } else if (s.has_unread) {
      statusHtml = `<span class="session-row-status session-status-unread" title="New response ready">${icon('check-circle', { size: '12px' })}</span>`;
    }
    row.innerHTML = `
      <span class="row-drag-handle" data-drag-handle title="Drag to reorder · hold to pin">${icon('grip-vertical', { size: '13px' })}</span>
      <span class="session-row-pin-icon">${icon('pin', { size: '12px' })}</span>
      ${statusHtml}
      <span class="session-row-title" title="Hold to rename"><span class="session-row-agent">${_truncate(agentName, 18).replace(/</g, '&lt;')}</span><span class="session-row-sep"> — </span>${_truncate(label, 28).replace(/</g, '&lt;')}</span>
      <button class="session-row-delete" title="Delete session" data-id="${s.id}" data-state="trash">${icon('trash-2', { size: '14px' })}</button>
    `;
    menu.appendChild(row);
  }
  // Append "+ N more" row if there are unpinned sessions beyond what was returned
  const unpinnedReturned = sorted.filter(s => !s.pinned).length;
  const extra = _totalUnpinnedCount - unpinnedReturned;
  if (extra > 0) {
    const moreRow = document.createElement('div');
    moreRow.className = 'session-row-more';
    moreRow.textContent = `+ ${extra} more sessions`;
    moreRow.title = `${extra} additional sessions not shown — increase limit to see them`;
    menu.appendChild(moreRow);
  }
}

/**
 * Apply a drag-to-reorder result for the session dropdown: reorder the cache to
 * the dropped sequence, re-float pinned sessions to the top (the server orders
 * pinned-first too), persist the synced order, and re-render.
 */
function _applySessionReorder(orderedIds) {
  const byId = new Map(_sessionsCache.map(s => [s.id, s]));
  const next = orderedIds.map(id => byId.get(id)).filter(Boolean);
  for (const s of _sessionsCache) if (!orderedIds.includes(s.id)) next.push(s);
  // Stable sort → pinned first, manual order preserved within each group.
  next.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
  _sessionsCache = next;
  _renderSessionRows();
  _setTriggerLabel();
  persistSessionOrder(app.currentUserId, _sessionsCache.map(s => s.id));
}

export async function populateSessionSelect(userId) {
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
    // The header dropdown now lists sessions for EVERY agent, so we deliberately
    // omit the agent_id filter — the server returns the user's sessions across
    // all agents, newest-active first, pinned on top.
    let url = `/api/v1/db/sessions?db=local.db&user_id=${encodeURIComponent(userId)}&limit=50`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(apiPath(url));
    const data = await res.json();
    if (mySeq !== _sessionFetchSeq) return; // a newer fetch superseded us
    _sessionsCache = (data.sessions || []).map(s => ({
      id: s.id,
      title: s.title || 'New Session',
      agent_id: s.agent_id || null,
      created_at: s.created_at,
      updated_at: s.updated_at || s.created_at,
      pinned: !!s.pinned,
      run_status: s.run_status || null,
      has_unread: !!s.has_unread,
    }));
    // Store total_count so _renderSessionRows can show the "+ N more" row
    _totalUnpinnedCount = data.total_count || 0;
    // If current session not yet in DB (fresh session before first msg),
    // synthesize a row so trigger label shows "New Session" and it appears
    if (app.currentSessionId && !_sessionsCache.some(s => s.id === app.currentSessionId)) {
      _sessionsCache.unshift({
        id: app.currentSessionId,
        title: 'New Session',
        agent_id: app.currentAgentId || null,
        created_at: null,
        updated_at: null,
        pinned: false,
        run_status: null,
        has_unread: false,
      });
    }
    _renderSessionRows();
    _setTriggerLabel();
    _setAgentTriggerLabel();  // refresh agent status icon based on session states
    _fetchAgentRunningStatuses();  // refresh running-status sprites in agent dropdown
    _updateHeaderSessionCounter();
  } catch (e) {
    console.warn('Failed to load sessions:', e);
  }
}

/**
 * Update the header notification badge with counts of sessions needing attention.
 * Shows a warning triangle + count for interrupted/errored sessions,
 * and a running count for in-progress sessions.
 */
function _updateHeaderSessionCounter() {
  const badge = document.getElementById('header-session-counter');
  const countEl = document.getElementById('header-session-count');
  if (!badge || !countEl) return;

  const needingAttention = _sessionsCache.filter(
    s => s.run_status === 'interrupted' || s.run_status === 'error'
  ).length;
  const running = _sessionsCache.filter(
    s => s.run_status === 'running'
  ).length;
  const unread = _sessionsCache.filter(
    s => s.has_unread && s.run_status !== 'running'
  ).length;

  if (needingAttention > 0) {
    countEl.textContent = needingAttention;
    badge.style.display = 'inline-flex';
    badge.title = needingAttention === 1
      ? '1 session needs attention'
      : `${needingAttention} sessions need attention`;
    // Reset to warning icon
    const iconEl = badge.querySelector('[data-lucide]');
    if (iconEl) iconEl.setAttribute('data-lucide', 'alert-triangle');
    badge.className = 'header-notif-badge';
  } else if (running > 0) {
    countEl.textContent = running;
    badge.style.display = 'inline-flex';
    badge.title = running === 1
      ? '1 session running'
      : `${running} sessions running`;
    const iconEl = badge.querySelector('[data-lucide]');
    if (iconEl) iconEl.setAttribute('data-lucide', 'loader-2');
    badge.className = 'header-notif-badge header-notif-running';
  } else if (unread > 0) {
    countEl.textContent = unread;
    badge.style.display = 'inline-flex';
    badge.title = unread === 1
      ? '1 session with new response'
      : `${unread} sessions with new responses`;
    const iconEl = badge.querySelector('[data-lucide]');
    if (iconEl) iconEl.setAttribute('data-lucide', 'check-circle');
    badge.className = 'header-notif-badge header-notif-unread';
  } else {
    badge.style.display = 'none';
  }
}

/**
 * After all bubbles are rendered, walk the message list and attach tool-call
 * panels to assistant bubbles that have tool call data in their `output` field.
 * Tool results are stored as separate `role="tool"` interactions with a
 * `parent_id` linking back to the assistant interaction that made the call.
 * Also attaches the full saved `input` (messages sent to LLM) and `output`
 * (LLM response) to each bubble for inspection.
 */
function _attachToolCallsFromMessages(messages) {
  if (!app.attachToolCallsToLastBubble) return;
  // Collect tool results keyed by parent_id
  const toolResultsByParent = {};
  for (const msg of messages) {
    if (msg.role === 'tool' && msg.parent_id) {
      if (!toolResultsByParent[msg.parent_id]) toolResultsByParent[msg.parent_id] = [];
      toolResultsByParent[msg.parent_id].push(msg);
    }
  }

  // Walk assistant messages and attach tool calls
  for (const msg of messages) {
    if (msg.role !== 'assistant') continue;
    if (!msg.output) continue;
    let outputData;
    try { outputData = JSON.parse(msg.output); } catch (_) { continue; }
    const toolCalls = outputData.tool_calls;
    if (!toolCalls || toolCalls.length === 0) continue;

    // Reconstruct tool call entries matching the format chat-activity.js expects
    const calls = toolCalls.map((tc, idx) => {
      let args = {};
      try { args = JSON.parse(tc.function.arguments); } catch (_) {}
      const toolName = tc.function.name;
      // Find matching tool result by tool_call_id
      const results = toolResultsByParent[msg.id] || [];
      const resultEntry = results.find(r => r.tool_name === toolName);
      return {
        tool: toolName,
        args: args,
        status: resultEntry ? 'done' : 'done',
        result: resultEntry ? resultEntry.content : null,
        durationMs: resultEntry ? _parseDuration(resultEntry.metadata) : null,
        errorType: null,
        turn: 0,
        open: false,
        // Full saved data for inspection
        _savedInput: msg.input || null,
        _savedOutput: msg.output || null,
        _savedToolOutput: resultEntry ? resultEntry.output : null,
        _savedToolMetadata: resultEntry ? resultEntry.metadata : null,
      };
    });

    if (calls.length > 0) {
      app.attachToolCallsToLastBubble(calls);
    }
  }
}

function _parseDuration(metadataStr) {
  if (!metadataStr) return null;
  try {
    const meta = JSON.parse(metadataStr);
    return meta.duration_ms != null ? meta.duration_ms : null;
  } catch (_) { return null; }
}

/**
 * Render an array of message objects into app.chatMessages.
 * Used by both the initial load and the "load earlier" pagination path.
 * Returns the number of messages rendered.
 */
function _renderMessages(messages, sessionId, run, prepend) {
  let count = 0;
  let seededStreaming = false;

  for (const msg of messages) {
    if (msg.role === 'user') {
      app.addChatBubble('user', msg.content, undefined, undefined, undefined, msg.id);
      count++;
    } else if (msg.role === 'assistant') {
      let text = msg.content || '';
      const toolCallIdx = text.indexOf('\n\n[Tool calls: ');
      if (toolCallIdx !== -1) text = text.slice(0, toolCallIdx);
      const hasText = !!text.trim();
      if (msg.status === 'streaming') {
        if (typeof app.seedStreamingBubble === 'function') {
          app.seedStreamingBubble(msg.id, text);
        } else {
          app.addChatBubble('agent', text || '…', 'streaming', undefined, msg.id);
        }
        seededStreaming = true;
        count++;
      } else if (!hasText) {
        continue;
      } else if (msg.status === 'interrupted') {
        app.addChatBubble('agent', text + '\n\n(interrupted)', 'interrupted', undefined, msg.id);
        count++;
      } else if (msg.status === 'error') {
        app.addChatBubble('agent', text, 'error', undefined, msg.id);
        count++;
      } else {
        app.addChatBubble('agent', text, undefined, undefined, msg.id);
        count++;
      }
    }
  }

  // Run-state handling (only on initial load, not pagination)
  if (run && run.active && !prepend) {
    app.isProcessing = true;
    if (!seededStreaming && run.assistant_interaction_id
        && typeof app.ensureStreamingBubbleForActiveTurn === 'function') {
      app.ensureStreamingBubbleForActiveTurn(run.assistant_interaction_id);
    }
    if (!app.lastSessionSeq) app.lastSessionSeq = {};
    const floor = typeof run.latest_session_seq === 'number' ? run.latest_session_seq : 0;
    app.lastSessionSeq[sessionId] = Math.max(app.lastSessionSeq[sessionId] || 0, floor);
  } else if (!run || !run.active) {
    if (!prepend) app.isProcessing = false;
  }

  // Attach tool-call panels from persisted data (only on initial load, not pagination)
  if (!prepend) {
    _attachToolCallsFromMessages(messages);
  }

  return count;
}

/**
 * Fetch a batch of messages from the server.
 */
async function _fetchMessages(sessionId, limit, beforeId) {
  const token = localStorage.getItem('auth_token');
  let url = apiPath(`/api/v1/db/session-messages?db=local.db&session_id=${encodeURIComponent(sessionId)}&limit=${limit}${token ? '&token=' + encodeURIComponent(token) : ''}`);
  if (beforeId) url += `&before_id=${encodeURIComponent(beforeId)}`;
  const res = await fetch(url);
  return await res.json();
}

/**
 * Prepend a "Load earlier messages" button at the top of chatMessages.
 */
function _prependLoadEarlierBtn(sessionId) {
  const existing = document.getElementById(`load-earlier-${sessionId}`);
  if (existing) return existing;
  const btn = document.createElement('div');
  btn.id = `load-earlier-${sessionId}`;
  btn.className = 'load-earlier-btn';
  btn.textContent = '\u2191 Load earlier messages';
  btn.addEventListener('click', async function onClick() {
    btn.textContent = 'Loading\u2026';
    btn.style.pointerEvents = 'none';
    try {
      const cache = _messageCache.get(sessionId);
      const oldestId = cache && cache.messages.length > 0 ? cache.messages[0].id : null;
      if (!oldestId) { btn.remove(); return; }
      const data = await _fetchMessages(sessionId, 100, oldestId);
      if (!data.messages || data.messages.length === 0) {
        btn.textContent = 'No more messages';
        setTimeout(() => btn.remove(), 2000);
        return;
      }
      // Prepend to cache
      cache.messages = [...data.messages, ...cache.messages];
      cache.hasMore = !!data.has_more;
      // Capture scrollHeight before inserting so we can adjust scrollTop
      const container = app.chatMessages;
      const oldScrollHeight = container ? container.scrollHeight : 0;
      // Render above existing bubbles — find the load-earlier btn as anchor
      const anchor = document.getElementById(`load-earlier-${sessionId}`);
      // We'll insertBefore each new bubble right after the anchor
      for (const msg of data.messages) {
        if (msg.role === 'user') {
          const bubble = _createBubble('user', msg.content, null, null, null, msg.id);
          if (anchor && anchor.parentNode) {
            anchor.parentNode.insertBefore(bubble, anchor.nextSibling);
          }
        } else if (msg.role === 'assistant') {
          let text = msg.content || '';
          const toolCallIdx = text.indexOf('\n\n[Tool calls: ');
          if (toolCallIdx !== -1) text = text.slice(0, toolCallIdx);
          const hasText = !!text.trim();
          if (!hasText && msg.status !== 'streaming') continue;
          const cls = msg.status === 'streaming' ? 'streaming' :
                      msg.status === 'interrupted' ? 'interrupted' :
                      msg.status === 'error' ? 'error' : null;
          const bubble = _createBubble('agent', text, cls, null, msg.id, null);
          if (anchor && anchor.parentNode) {
            anchor.parentNode.insertBefore(bubble, anchor.nextSibling);
          }
        }
      }
      // Adjust scrollTop so the view doesn't jump
      if (container) _adjustScrollForPrepend(container, oldScrollHeight);
      if (!data.has_more) {
        btn.remove();
      } else {
        btn.textContent = '\u2191 Load earlier messages';
        btn.style.pointerEvents = '';
      }
    } catch (e) {
      console.warn('Failed to load earlier messages:', e);
      btn.textContent = '\u2191 Load earlier messages';
      btn.style.pointerEvents = '';
    }
  });
  if (app.chatMessages && app.chatMessages.firstChild) {
    app.chatMessages.insertBefore(btn, app.chatMessages.firstChild);
  } else if (app.chatMessages) {
    app.chatMessages.appendChild(btn);
  }
  return btn;
}

// ── Virtual-scroll helpers ──────────────────────────────────────────────────

/**
 * Create a placeholder div that preserves the stored height of a bubble.
 */
function _makePlaceholder(msgId, height) {
  const el = document.createElement('div');
  el.className = 'chat-bubble-placeholder';
  el.dataset.msgId = msgId;
  el.style.height = height + 'px';
  return el;
}

/**
 * Measure a real bubble's offsetHeight and store it in _bubbleHeights.
 * Called after first render of every bubble.
 */
function _storeBubbleHeight(bubble) {
  const msgId = bubble.getAttribute('data-msg-id');
  if (!msgId) return;
  const h = bubble.offsetHeight;
  if (h > 0) _bubbleHeights.set(msgId, h);
}

/**
 * Given a msgId, return the stored height or a default fallback.
 */
function _getBubbleHeight(msgId) {
  const h = _bubbleHeights.get(msgId);
  return h || 80; // fallback for unmeasured bubbles
}

/**
 * Recycle pass: scan all children of app.chatMessages (excluding the
 * load-earlier button) and swap real bubbles ↔ placeholders based on
 * viewport visibility + _VS_BUFFER.
 */
function _recycleVisible() {
  const container = app.chatMessages;
  if (!container) return;
  const rect = container.getBoundingClientRect();
  const scrollTop = container.scrollTop;
  const viewTop = scrollTop;
  const viewBottom = scrollTop + rect.height;
  const bufferTop = viewTop - _VS_BUFFER;
  const bufferBottom = viewBottom + _VS_BUFFER;

  // Collect all bubble/placeholder children (skip load-earlier btn)
  const children = Array.from(container.children).filter(
    c => c.classList.contains('chat-bubble') || c.classList.contains('chat-bubble-placeholder')
  );

  for (const el of children) {
    const msgId = el.getAttribute('data-msg-id');
    if (!msgId) continue;

    // Approximate position using offsetTop (accurate since container is
    // position:relative and children are block-level).
    const elTop = el.offsetTop;
    const elBottom = elTop + (el.classList.contains('chat-bubble-placeholder')
      ? parseInt(el.style.height, 10) || _getBubbleHeight(msgId)
      : el.offsetHeight);

    const isVisible = elBottom > bufferTop && elTop < bufferBottom;

    if (el.classList.contains('chat-bubble-placeholder')) {
      if (isVisible) {
        // Recycle: placeholder → real bubble
        _recyclePlaceholderToBubble(el, msgId);
      }
    } else {
      if (!isVisible) {
        // Recycle: real bubble → placeholder
        _recycleBubbleToPlaceholder(el, msgId);
      }
    }
  }
}

/**
 * Replace a placeholder element with the real bubble from _messageCache.
 */
function _recyclePlaceholderToBubble(placeholder, msgId) {
  const sessionId = app.currentSessionId;
  if (!sessionId) return;
  const cached = _messageCache.get(sessionId);
  if (!cached) return;
  const msg = cached.messages.find(m => m.id === msgId);
  if (!msg) return;

  _placeholderIds.delete(msgId);

  // Build the real bubble using the same logic as _renderMessages
  const bubble = document.createElement('div');
  let role = msg.role === 'user' ? 'user' : 'agent';
  let extraClass = null;
  if (msg.role === 'assistant') {
    if (msg.status === 'streaming') extraClass = 'streaming';
    else if (msg.status === 'interrupted') extraClass = 'interrupted';
    else if (msg.status === 'error') extraClass = 'error';
  }
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (msg.id) bubble.setAttribute('data-msg-id', msg.id);

  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
    bubble.appendChild(app._linkifyText(msg.content || ''));
  } else {
    let text = msg.content || '';
    const toolCallIdx = text.indexOf('\n\n[Tool calls: ');
    if (toolCallIdx !== -1) text = text.slice(0, toolCallIdx);
    if (text.trim()) {
      // Use the same markdown rendering as addChatBubble
      const body = app._renderMarkdownBody(text, true);
      if (body) {
        bubble.appendChild(body);
        bubble.classList.add('md');
        bubble.__mdSource = text;
      } else {
        bubble.appendChild(app._linkifyText(text));
      }
    }
    if (extraClass === 'streaming') {
      const stopBtn = document.createElement('button');
      stopBtn.className = 'stop-btn';
      stopBtn.textContent = '\uD83D\uDED1';
      stopBtn.title = 'Stop generation';
      stopBtn.addEventListener('click', app._sendStopMessage);
      bubble.appendChild(stopBtn);
    }

    // Add bubble actions for finalized messages
    if (extraClass !== 'streaming' && text && text.trim() && text !== '\u2026') {
      app._addBubbleActions(bubble);
    }
  }

  placeholder.parentNode.replaceChild(bubble, placeholder);
  _storeBubbleHeight(bubble);
}

/**
 * Replace a real bubble element with a placeholder of the same height.
 */
function _recycleBubbleToPlaceholder(el, msgId) {
  _storeBubbleHeight(el);
  const h = _getBubbleHeight(msgId);
  _placeholderIds.add(msgId);
  const placeholder = _makePlaceholder(msgId, h);
  el.parentNode.replaceChild(placeholder, el);
}

/**
 * After prepending older messages (infinite scroll), measure all new bubbles
 * and adjust scrollTop so the view doesn't jump.
 */
function _adjustScrollForPrepend(container, oldScrollHeight) {
  // After new bubbles are inserted at the top, the content grew upward.
  // The user's visible position stays the same if we add the delta to scrollTop.
  const newScrollHeight = container.scrollHeight;
  const delta = newScrollHeight - oldScrollHeight;
  container.scrollTop += delta;
}

/**
 * Wrap app.addChatBubble so every new bubble has its height stored after render.
 */
function _hookAddChatBubble() {
  if (_origAddChatBubble) return; // already hooked
  _origAddChatBubble = app.addChatBubble;
  app.addChatBubble = function(role, text, extraClass, imageUrl, turnId, msgId) {
    const bubble = _origAddChatBubble.call(app, role, text, extraClass, imageUrl, turnId, msgId);
    // Defer measurement so layout is computed
    requestAnimationFrame(() => _storeBubbleHeight(bubble));
    return bubble;
  };
}

/**
 * Install the virtual-scroll scroll handler on app.chatMessages.
 * Also measures all existing bubbles and stores their heights.
 */
function _installVirtualScroll() {
  const container = app.chatMessages;
  if (!container) return;

  // Hook addChatBubble to auto-store heights
  _hookAddChatBubble();

  // Remove old handler if any
  if (_virtualScrollHandler) {
    container.removeEventListener('scroll', _virtualScrollHandler);
  }

  // Measure all existing real bubbles
  container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));

  // Debounced scroll handler
  let _vsTimer = null;
  _virtualScrollHandler = () => {
    if (_vsTimer) cancelAnimationFrame(_vsTimer);
    _vsTimer = requestAnimationFrame(() => {
      _recycleVisible();
      _vsTimer = null;
    });
  };
  container.addEventListener('scroll', _virtualScrollHandler);

  // Run an initial recycle pass
  _recycleVisible();
}

/**
 * Teardown virtual scroll for the current container.
 */
function _teardownVirtualScroll() {
  if (_virtualScrollHandler && app.chatMessages) {
    app.chatMessages.removeEventListener('scroll', _virtualScrollHandler);
  }
  _virtualScrollHandler = null;
  // Restore original addChatBubble
  if (_origAddChatBubble) {
    app.addChatBubble = _origAddChatBubble;
    _origAddChatBubble = null;
  }
  // Restore all placeholders back to real bubbles on teardown
  if (app.chatMessages) {
    app.chatMessages.querySelectorAll('.chat-bubble-placeholder').forEach(ph => {
      const msgId = ph.getAttribute('data-msg-id');
      if (msgId) {
        _placeholderIds.delete(msgId);
        _recyclePlaceholderToBubble(ph, msgId);
      }
    });
  }
}

/**
 * Create a chat bubble element without appending it (used for prepend pagination).
 */
function _createBubble(role, text, extraClass, imageUrl, turnId, msgId) {
	  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (turnId) bubble.setAttribute('data-turn-id', turnId);
  if (msgId) bubble.setAttribute('data-msg-id', msgId);
  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
  }
  // Simple text node for prepended messages (no markdown rendering needed for old messages)
  bubble.appendChild(document.createTextNode(text || ''));
  // Store height after first render (defer so layout is computed)
  requestAnimationFrame(() => _storeBubbleHeight(bubble));
  return bubble;
}

/**
 * Show a debug bubble with agentId + sessionId for admin users.
 * Only shown when the user is authenticated (not anonymous).
 */
function _showDebugBubble(sessionId) {
  const token = localStorage.getItem('auth_token');
  if (!token) return; // only for authenticated users
  const agentId = app.currentAgentId || '—';
  const text = `🔍 **Debug** — Agent: \`${agentId}\` · Session: \`${sessionId}\``;
  app.addChatBubble('agent', text, 'debug-info');
}

export async function loadSessionChat(sessionId) {
  try {
    // Check cache first
    const cached = _messageCache.get(sessionId);
    const now = Date.now();
    if (cached && (now - cached.loadedAt) < _CACHE_TTL_MS) {
      // Cache hit — render from cache
      if (sessionId !== app._lastLoadedSessionId) {
        _teardownVirtualScroll();
        app.chatMessages.innerHTML = '';
      }
      app._lastLoadedSessionId = sessionId;

      // Remove any stale load-earlier button
      const oldBtn = document.getElementById(`load-earlier-${sessionId}`);
      if (oldBtn) oldBtn.remove();

      // Debug bubble: show agentId + sessionId for admin users
      _showDebugBubble(sessionId);

      for (const msg of cached.messages) {
        if (msg.role === 'user') {
          app.addChatBubble('user', msg.content, undefined, undefined, undefined, msg.id);
        } else if (msg.role === 'assistant') {
          let text = msg.content || '';
          const toolCallIdx = text.indexOf('\n\n[Tool calls: ');
          if (toolCallIdx !== -1) text = text.slice(0, toolCallIdx);
          const hasText = !!text.trim();
          if (msg.status === 'streaming') {
            if (typeof app.seedStreamingBubble === 'function') {
              app.seedStreamingBubble(msg.id, text);
            } else {
              app.addChatBubble('agent', text || '\u2026', 'streaming', undefined, msg.id);
            }
          } else if (!hasText) {
            continue;
          } else if (msg.status === 'interrupted') {
            app.addChatBubble('agent', text + '\n\n(interrupted)', 'interrupted', undefined, msg.id);
          } else if (msg.status === 'error') {
            app.addChatBubble('agent', text, 'error', undefined, msg.id);
          } else {
            app.addChatBubble('agent', text, undefined, undefined, msg.id);
          }
        }
      }

      // Attach tool-call panels from cached data
      _attachToolCallsFromMessages(cached.messages);

      if (cached.hasMore) {
        _prependLoadEarlierBtn(sessionId);
      }

      app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
      // Install virtual scroll after rendering
      _installVirtualScroll();
      return;
    }

    // Cache miss or stale — fetch from API
    let data = await _fetchMessages(sessionId, 100);

    // A `restricted` response almost always means our access token expired, so
    // the server's participant check can't see who we are and the session looks
    // off-limits — which is what made switching sessions silently bounce us into
    // a new empty one. If we hold a persistent credential, refresh the token and
    // retry once before giving up. (The keep-fresh timer in auth-refresh.js
    // normally prevents ever reaching this; this is the boundary safety net.)
    if (data.restricted && getActive() && getActive().remember_token) {
      await ensureFreshToken();
      data = await _fetchMessages(sessionId, 100);
    }

    // Clear DOM when switching to a new session
    if (sessionId !== app._lastLoadedSessionId) {
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
    }
    app._lastLoadedSessionId = sessionId;

    // Debug bubble: show agentId + sessionId for admin users
    _showDebugBubble(sessionId);

    if (data.restricted) {
      // Not a participant — silently switch to a fresh session
      app.currentSessionId = generateUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
      app.addChatBubble('agent', 'New session. Start typing below.');
      if (app.currentUserId) populateSessionSelect(app.currentUserId);
      _messageCache.delete(sessionId);
      return;
    }

    // Store in cache
    const msgs = data.messages || [];
    _messageCache.set(sessionId, {
      messages: [...msgs],
      hasMore: !!data.has_more,
      loadedAt: Date.now(),
    });

    if (msgs.length === 0) {
      app.addChatBubble(
        'agent',
        'Session loaded. No messages yet \u2014 start typing below.',
      );
      return;
    }

    // Render messages
    const run = data.run || null;
    _renderMessages(msgs, sessionId, run, false);

    // Prepend "Load earlier" button if more exist
    if (data.has_more) {
      _prependLoadEarlierBtn(sessionId);
    }

    // Input availability follows text presence, not run state
    if (app.chatSend) app.chatSend.disabled = !((app.chatInput && app.chatInput.value.trim()));

    app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
    // Install virtual scroll after rendering
    _installVirtualScroll();
  } catch (e) {
    console.warn('Failed to load session messages:', e);
  }
}

/**
 * Append or update a message in the in-memory cache for a session (called from WS paths).
 * For streaming chunks (_streaming: true), finds the existing entry by id and appends content.
 * For finalized messages (_finalized: true), replaces the streaming entry's content.
 * For new messages (no special flags), appends to the end.
 */
export function _cacheAppendMessage(sessionId, msg) {
  const cached = _messageCache.get(sessionId);
  if (!cached) return;

  if (msg._streaming && msg.id) {
    // Streaming chunk — find existing entry and append content
    const existing = cached.messages.find(m => m.id === msg.id && m.role === 'assistant');
    if (existing) {
      existing.content = (existing.content || '') + msg.content;
    } else {
      cached.messages.push({ role: 'assistant', content: msg.content, id: msg.id, status: 'streaming' });
    }
  } else if (msg._finalized && msg.id) {
    // Finalized response — replace the streaming entry's content
    const existing = cached.messages.find(m => m.id === msg.id && m.role === 'assistant');
    if (existing) {
      existing.content = msg.content;
      delete existing.status;
    } else {
      cached.messages.push({ role: 'assistant', content: msg.content, id: msg.id });
    }
  } else {
    // New message (user or standalone assistant)
    // Avoid duplicates by checking msg.id
    if (msg.id && cached.messages.some(m => m.id === msg.id)) return;
    cached.messages.push(msg);
  }

  // Refresh the timestamp so the cache doesn't go stale while actively chatting
  cached.loadedAt = Date.now();
}

/** Call before first connectAgent() so agent onopen can refresh users. */
export function registerSessionApi() {
  app.populateUserSelect = populateUserSelect;
  app.populateSessionSelect = populateSessionSelect;
  app.populateAgentSelect = populateAgentSelect;
  app.loadSessionChat = loadSessionChat;
  // Dead-WS fallback: re-render the current session straight from the DB.
  app.reloadCurrentSession = () => {
    if (app.currentSessionId) loadSessionChat(app.currentSessionId);
  };
}

export function initSessions() {

  // ── Theme system ──
  const STORAGE_KEY = 'webagent_theme';

  /** Set theme on <body>: 'light', 'dark', or 'system' (follow OS).
   *  Also updates the PWA theme-color meta tag to match. */
  function applyTheme(theme) {
    const body = document.body;
    if (theme === 'light') {
      body.classList.add('light-mode');
    } else if (theme === 'dark') {
      body.classList.remove('light-mode');
    } else {
      // system — follow OS preference
      const prefersLight = window.matchMedia('(prefers-color-scheme: light)').matches;
      body.classList.toggle('light-mode', prefersLight);
    }
    // Sync PWA theme-color with current background
    const isLight = body.classList.contains('light-mode');
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = isLight ? '#faf5ee' : '#0d0d1a';
  }

  /** Highlight the matching theme button. */
  function highlightThemeOption(theme) {
    document.querySelectorAll('.theme-option').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.theme === theme);
    });
  }

  // Load saved theme on init (default 'system' on first load)
  let savedTheme = 'system';
  try { savedTheme = localStorage.getItem(STORAGE_KEY) || 'system'; } catch (_) {}
  applyTheme(savedTheme);
  highlightThemeOption(savedTheme);

  // Listen to system preference changes when in 'system' mode
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  mq.addEventListener('change', () => {
    let current = 'system';
    try { current = localStorage.getItem(STORAGE_KEY) || 'system'; } catch (_) {}
    if (current === 'system') applyTheme('system');
  });

  // Wire theme buttons — pointerdown is more reliable than click for small targets
  document.querySelectorAll('.theme-option').forEach(btn => {
    btn.addEventListener('pointerdown', (e) => {
      e.stopPropagation();
      e.preventDefault();
      const theme = btn.dataset.theme;
      if (!theme) return;
      applyTheme(theme);
      highlightThemeOption(theme);
      try { localStorage.setItem(STORAGE_KEY, theme); } catch (_) {}
    });
  });

  // ── User dropdown toggle ──
  // The trigger lives inside #main-tabs, a horizontally-scrolling carousel
  // whose overflow clips any absolutely-positioned descendant. The menu is
  // therefore position:fixed (see app1.css) and anchored under the trigger by
  // JS each time it opens, so it escapes the carousel's clip region.
  const userDropdown = document.getElementById('user-dropdown');
  const dropdownMenu = document.getElementById('user-dropdown-menu');
  const trigger = document.querySelector('.user-dropdown-trigger');

  function positionUserMenu() {
    if (!trigger || !dropdownMenu) return;
    const r = trigger.getBoundingClientRect();
    dropdownMenu.style.marginTop = '0';
    dropdownMenu.style.top = Math.round(r.bottom + 6) + 'px';
    // Clamp horizontally so the menu never spills past the viewport edges.
    const w = dropdownMenu.offsetWidth || 280;
    let left = r.left;
    const maxLeft = window.innerWidth - w - 8;
    if (left > maxLeft) left = maxLeft;
    if (left < 8) left = 8;
    dropdownMenu.style.left = Math.round(left) + 'px';
  }

  function openUserMenu() {
    dropdownMenu.style.display = 'block';
    userDropdown.classList.add('open');
    positionUserMenu();
  }

  function closeUserMenu() {
    dropdownMenu.style.display = 'none';
    userDropdown.classList.remove('open');
  }

  if (trigger && dropdownMenu) {
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      if (dropdownMenu.style.display === 'block') closeUserMenu();
      else openUserMenu();
    });

    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
      if (!userDropdown.contains(e.target)) closeUserMenu();
    });

    // Close on Escape
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && dropdownMenu.style.display === 'block') closeUserMenu();
    });

    // Re-anchor the fixed menu to the trigger if the viewport changes size.
    window.addEventListener('resize', () => {
      if (dropdownMenu.style.display === 'block') positionUserMenu();
    });
  }

  // ── Render dropdown contents (current row + other accounts) ──
  renderUserDropdown();

  // Re-render whenever the accounts list/active user changes
  onAccountsChange(() => renderUserDropdown());

  // ── Manage Account button (in dropdown) → switch to account tab ──
  const manageBtn = document.getElementById('btn-manage-account');
  if (manageBtn) {
    manageBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const sel = document.getElementById('main-tab-select');
      if (sel) {
        sel.value = 'account';
        sel.dispatchEvent(new Event('change'));
      }
      const menu = document.getElementById('user-dropdown-menu');
      if (menu) menu.style.display = 'none';
      const dd = document.getElementById('user-dropdown');
      if (dd) dd.classList.remove('open');
    });
  }

  // ── Add account button → show login overlay for a fresh sign-in ──
  const addBtn = document.getElementById('btn-add-account');
  if (addBtn) {
    addBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const menu = document.getElementById('user-dropdown-menu');
      if (menu) menu.style.display = 'none';
      const dd = document.getElementById('user-dropdown');
      if (dd) dd.classList.remove('open');
      showLeftOverlay();
    });
  }

  // ── Sign-out button: remove active account; switch to another if possible ──
  const signoutBtn = document.getElementById('btn-signout-header');
  if (signoutBtn) {
    signoutBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const active = getActive();
      if (active) {
        removeAccount(active.user_id);
        const remaining = listAccounts();
        if (remaining.length > 0) {
          const next = remaining[0];
          const ok = await switchTo(next.user_id);
          if (!ok) {
            // recall failed for next — fall through to full logout reload
          }
        }
      } else {
        // No tracked accounts — clear legacy keys directly
        localStorage.removeItem('auth_token');
        localStorage.removeItem('auth_username');
        localStorage.removeItem('auth_user_id');
        localStorage.removeItem('auth_display_name');
        localStorage.removeItem('remember_token');
      }
      localStorage.removeItem('anonUserId');
      localStorage.removeItem('terminalUserId');
      window.location.reload();
    });
  }

  // ── Custom session dropdown ──
  const dropdown = document.getElementById('session-dropdown');
  const sessionTrigger = document.getElementById('session-dropdown-trigger');
  const menu = document.getElementById('session-dropdown-menu');

  let _sessionsLoaded = false;
  function openMenu() {
    if (!menu) return;
    // Opening one header dropdown closes the other — treat the gesture as
    // an outside-click for any peer menu.
    closeAgentMenu();
    menu.hidden = false;
    dropdown.classList.add('open');
    _resetAllDeleteButtons();
    // Lazy-load sessions on first open instead of fetching eagerly on page load.
    if (!_sessionsLoaded && app.currentUserId) {
      _sessionsLoaded = true;
      populateSessionSelect(app.currentUserId);
    }
  }

  function closeMenu() {
    if (!menu) return;
    menu.hidden = true;
    dropdown.classList.remove('open');
    _resetAllDeleteButtons();
  }

  async function switchToSession(sid) {
    if (!sid || sid === app.currentSessionId) { closeMenu(); return; }
    // The dropdown spans every agent, so the picked session may belong to a
    // different agent than the one currently active. Switch the agent to match
    // before loading the session, so chat sends and agent-scoped UI line up.
    const targetSess = _sessionsCache.find(s => s.id === sid);
    const targetAgentId = (targetSess && targetSess.agent_id) || app.currentAgentId;
    if (targetAgentId && targetAgentId !== app.currentAgentId) {
      app.currentAgentId = targetAgentId;
      try { localStorage.setItem('selectedAgentId', targetAgentId); } catch (_) {}
    }
    // Record this session as the last active for its owning agent
    if (targetAgentId) {
      _lastSessionPerAgent.set(targetAgentId, sid);
      _saveLastSessionMap();
    }
    // Leaving a session does NOT stop its run — it keeps going server-side and
    // we can view it again later from any device. Only tear down LOCAL UI state.
    abortChatStream();
    app.currentSessionId = sid;
    localStorage.setItem('terminalSessionId', app.currentSessionId);
    // Await so the in-progress partial is rendered + the resume floor is set
    // BEFORE we ask the WS to replay only-newer events below.
    await loadSessionChat(sid);
    loopSessionChanged();
    loopVisualSessionChanged();
    autoAgentSessionChanged();
    chatActivitySessionChanged();
    _renderSessionRows();
    _setTriggerLabel();
    _setAgentTriggerLabel();  // refresh TUI-bridge indicator for the new agent
    closeMenu();

    // Drain any WS-replayed events that arrived BEFORE we navigated here
    // (e.g. user hard-refreshed onto the home page while a run was in
    // flight on this session). agentWs.js stashed them keyed by sid.
    try {
      const pending = consumeReplayedEventsFor(sid);
      for (const ev of pending) {
        const key = ev.asst_id || ev.turn_id;
        if (ev.type === 'stream' && typeof app.appendStreamToActiveBubble === 'function') {
          app.appendStreamToActiveBubble(ev.content || '', key);
        } else if (ev.type === 'agent_step_end' && typeof app.finalizeAgentStep === 'function') {
          app.finalizeAgentStep(ev.content || '', key);
        } else if (ev.type === 'response' && typeof app.finalizeAgentResponse === 'function') {
          app.finalizeAgentResponse(ev.content || '', key, true);
        } else if (ev.type === 'interrupted' && typeof app.markAgentInterrupted === 'function') {
          app.markAgentInterrupted(ev.asst_id);
        }
        // Keep cache fresh for replayed events
        if (ev.type === 'response' && ev.content) {
          _cacheAppendMessage(sid, { role: 'assistant', content: ev.content, id: ev.asst_id || ev.turn_id });
        }
      }
    } catch (_pendErr) { /* never let drain break navigation */ }

    // Ask the WS to replay any events for THIS (newly-active) session that
    // we missed while we were on the other one. The server checks its
    // in-memory RunBuffer for `sid` and resends events > last_session_seq.
    try {
      if (app.agentWs && app.agentWs.readyState === WebSocket.OPEN) {
        const lastSeq = (app.lastSessionSeq && app.lastSessionSeq[sid]) || 0;
        app.agentWs.send(JSON.stringify({
          type: 'resume',
          session_id: sid,
          last_session_seq: lastSeq,
        }));
      }
    } catch (_e) { /* socket may not be ready — replay on next reconnect */ }
  }

  /**
   * Two-click delete: first click shows ⚠️ (warning), second click deletes.
   * Any other interaction (clicking elsewhere, opening menu) resets all buttons.
   */
  function handleDeleteClick(btn, sid) {
    const state = btn.dataset.state;
    if (state === 'trash') {
      // First click: switch to warning state
      btn.dataset.state = 'warning';
      btn.classList.add('warning');
      btn.title = 'Click again to confirm delete';
      btn.innerHTML = icon('alert-triangle', { size: '14px' });
      // Reset all other delete buttons back to trash
      document.querySelectorAll('.session-row-delete[data-state="warning"]').forEach(other => {
        if (other !== btn) _resetDeleteBtn(other);
      });
    } else if (state === 'warning') {
      // Second click: delete
      btn.dataset.state = 'deleting';
      deleteSession(sid);
    }
  }

  function _resetDeleteBtn(btn) {
    btn.dataset.state = 'trash';
    btn.classList.remove('warning');
    btn.title = 'Delete session';
    btn.innerHTML = icon('trash-2', { size: '14px' });
  }

  // Reset all delete buttons to trash state (e.g. when menu opens/closes)
  function _resetAllDeleteButtons() {
    document.querySelectorAll('.session-row-delete').forEach(_resetDeleteBtn);
  }

  async function patchSession(sid, body) {
    try {
      const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=local.db'), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return res.ok;
    } catch (e) {
      console.warn('Failed to patch session:', e);
      return false;
    }
  }

  async function togglePin(sid) {
    const sess = _sessionsCache.find(s => s.id === sid);
    if (!sess) return;
    const newPinned = !sess.pinned;
    const ok = await patchSession(sid, { pinned: newPinned });
    if (ok) {
      sess.pinned = newPinned;
      await populateSessionSelect(app.currentUserId);
    }
  }

  function startRename(sid, row) {
    const titleEl = row.querySelector('.session-row-title');
    if (!titleEl) return;
    const sess = _sessionsCache.find(s => s.id === sid);
    const current = (sess && sess.title) || '';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'session-row-title-input';
    input.value = current;
    titleEl.replaceWith(input);
    input.focus();
    input.select();

    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      const newTitle = input.value.trim();
      if (commit && newTitle && newTitle !== current) {
        const ok = await patchSession(sid, { title: newTitle });
        if (ok) {
          if (sess) sess.title = newTitle;
        }
      }
      await populateSessionSelect(app.currentUserId);
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
  }

  async function deleteSession(sid) {
    // Interrupt the backend agent loop for the session being deleted.
    interruptSession(sid);
    try {
      const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=local.db'), { method: 'DELETE' });
      if (res.ok) {
        if (sid === app.currentSessionId) {
          app.currentSessionId = generateUUID();
          localStorage.setItem('terminalSessionId', app.currentSessionId);
          _teardownVirtualScroll();
          app.chatMessages.innerHTML = '';
          app.addChatBubble('agent', 'Session deleted. New session created.');
        }
        await populateSessionSelect(app.currentUserId);
      }
    } catch (e) {
      console.warn('Failed to delete session:', e);
    }
  }

  // ── Header trigger: long-press or double-click to rename session ──
  function _headerRenameSession() {
    const labelEl = document.getElementById('session-dropdown-label');
    if (!labelEl) return;
    const sid = app.currentSessionId;
    if (!sid) return;
    const sess = _sessionsCache.find(s => s.id === sid);
    const current = (sess && sess.title) || 'New Session';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'session-row-title-input';
    input.value = current;
    input.style.width = '140px';
    labelEl.replaceWith(input);
    // Override the trigger's pointer-events:none on direct children so clicks
    // reach the input and the cursor can be placed at the click location.
    input.style.pointerEvents = 'auto';
    input.focus();
    input.select();
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      const newTitle = input.value.trim();
      if (commit && newTitle && newTitle !== current) {
        const ok = await patchSession(sid, { title: newTitle });
        if (ok && sess) sess.title = newTitle;
      }
      _setTriggerLabel();
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
  }

  if (sessionTrigger) {
    let _lpTimer = null, _lpStartX = 0, _lpStartY = 0;
    sessionTrigger.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.header-delete-btn, .header-plus-btn, .session-dropdown-status')) return;
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      _lpStartX = e.clientX; _lpStartY = e.clientY;
      _lpTimer = setTimeout(() => {
        _lpTimer = null;
        e.preventDefault();
        _headerRenameSession();
      }, 500);
    });
    sessionTrigger.addEventListener('pointermove', (e) => {
      if (!_lpTimer) return;
      if (Math.abs(e.clientX - _lpStartX) > 8 || Math.abs(e.clientY - _lpStartY) > 8) {
        clearTimeout(_lpTimer); _lpTimer = null;
      }
    });
    sessionTrigger.addEventListener('pointerup', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
    sessionTrigger.addEventListener('pointercancel', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
    sessionTrigger.addEventListener('dblclick', (e) => {
      if (e.target.closest('.header-delete-btn, .header-plus-btn, .session-dropdown-status')) return;
      e.stopPropagation();
      _headerRenameSession();
    });
    sessionTrigger.addEventListener('click', (e) => {
      // Don't toggle dropdown when clicking the delete button inside the trigger
      if (e.target.closest('.header-delete-btn, .header-plus-btn')) return;
      if (e.target.closest('.session-row-title-input')) return;
      e.stopPropagation();
      if (menu.hidden) openMenu(); else closeMenu();
    });
  }

  if (menu) {
    menu.addEventListener('click', (e) => {
      e.stopPropagation();
      // Delete button (right side of the row) — two-click confirm: trash → ⚠️ → delete
      const delBtn = e.target.closest('.session-row-delete');
      if (delBtn) {
        const row = delBtn.closest('.session-row');
        const sid = row && row.dataset.id;
        if (sid) handleDeleteClick(delBtn, sid);
        return;
      }
      // Row body click → switch session (ignore clicks inside rename input)
      if (e.target.closest('.session-row-title-input')) return;
      const row = e.target.closest('.session-row');
      if (row) switchToSession(row.dataset.id);
    });
    // Drag the grip handle to reorder; press-and-hold it to pin. Per-account.
    makeRowsReorderable(menu, {
      rowSelector: '.session-row',
      handleSelector: '.row-drag-handle',
      onReorder: _applySessionReorder,
      onHandleLongPress: (sid) => togglePin(sid),
    });
    // Press-and-hold the row body to rename (grip + delete button opt out).
    attachRowLongPress(menu, {
      rowSelector: '.session-row',
      ignoreSelector: '.row-drag-handle, .session-row-delete, .session-row-title-input',
      onLongPress: (sid, row) => startRename(sid, row),
    });
  }

  // Outside click closes menu + popups
  document.addEventListener('click', (e) => {
    if (dropdown && !dropdown.contains(e.target)) closeMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && menu && !menu.hidden) closeMenu();
  });

  // New session button — start a fresh session. When the user owns more than
  // one agent, clicking + opens a small picker so they choose which agent the
  // new session belongs to; with a single agent (or none) it starts directly.
  const sessionNewBtn = document.getElementById('session-new');

  /** Create a fresh session, optionally switching to a chosen agent first. */
  function _startNewSession(agentId) {
    abortChatStream();
    if (agentId && agentId !== app.currentAgentId) {
      app.currentAgentId = agentId;
      try { localStorage.setItem('selectedAgentId', agentId); } catch (_) {}
    }
    app.currentSessionId = generateUUID();
    localStorage.setItem('terminalSessionId', app.currentSessionId);
    _teardownVirtualScroll();
    app.chatMessages.innerHTML = '';
    app.addChatBubble('agent', 'New session. Start typing below.');
    populateSessionSelect(app.currentUserId);
    loopSessionChanged();
    loopVisualSessionChanged();
    autoAgentSessionChanged();
    chatActivitySessionChanged();
    _setAgentTriggerLabel();
  }

  // ── New-session agent picker (only shown when >1 agent) ──
  let _agentPickerEl = null;
  function _closeAgentPicker() {
    if (_agentPickerEl) { _agentPickerEl.remove(); _agentPickerEl = null; }
    document.removeEventListener('click', _onAgentPickerOutside, true);
    document.removeEventListener('keydown', _onAgentPickerEsc, true);
    window.removeEventListener('resize', _closeAgentPicker);
  }
  function _onAgentPickerOutside(e) {
    if (_agentPickerEl && !_agentPickerEl.contains(e.target) && !e.target.closest('#session-new')) {
      _closeAgentPicker();
    }
  }
  function _onAgentPickerEsc(e) { if (e.key === 'Escape') _closeAgentPicker(); }

  function _openAgentPicker() {
    _closeAgentPicker();
    const picker = document.createElement('div');
    picker.className = 'agent-dropdown-menu new-session-agent-picker';
    const head = document.createElement('div');
    head.className = 'new-session-picker-head';
    head.textContent = 'New session with…';
    picker.appendChild(head);
    for (const a of _agentsCache) {
      const row = document.createElement('div');
      row.className = 'agent-row-item';
      row.innerHTML = `<span class="agent-row-title">${_truncate(a.name || a.id.slice(0, 12), 28).replace(/</g, '&lt;')}</span>`;
      row.addEventListener('click', (ev) => {
        ev.stopPropagation();
        _closeAgentPicker();
        _startNewSession(a.id);
      });
      picker.appendChild(row);
    }
    document.body.appendChild(picker);
    // Anchor under the + button (re-query fresh in case it was re-rendered),
    // right-aligned so it doesn't spill off-screen.
    const anchorBtn = document.getElementById('session-new') || sessionNewBtn;
    const r = anchorBtn.getBoundingClientRect();
    picker.style.top = Math.round(r.bottom + 6) + 'px';
    const w = picker.offsetWidth || 200;
    let left = r.right - w;
    if (left < 8) left = 8;
    picker.style.left = Math.round(left) + 'px';
    _agentPickerEl = picker;
    // Defer listener attach so this same click doesn't immediately close it.
    setTimeout(() => {
      document.addEventListener('click', _onAgentPickerOutside, true);
      document.addEventListener('keydown', _onAgentPickerEsc, true);
      window.addEventListener('resize', _closeAgentPicker);
    }, 0);
  }

  // Delegated on document (capture) so it fires even if the #session-new button
  // is re-rendered/replaced after init or sits under another element — we just
  // need the click to land anywhere on (or inside) #session-new.
  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('#session-new');
    if (!btn) return;
    // Ignore clicks until the agent list has loaded — the button is a spinner
    // until then (see _setNewSessionBtnReady), so it isn't really clickable yet.
    if (!_newSessionBtnReady) return;
    e.stopPropagation();
    e.preventDefault();
    closeMenu();           // close the session list if it's open
    if (_agentPickerEl) { _closeAgentPicker(); return; }  // toggle off
    if (_agentsCache.length > 1) {
      _openAgentPicker();
      return;
    }
    // 0 agents → keep current (empty) agent; 1 agent → use it.
    _startNewSession(_agentsCache.length === 1 ? _agentsCache[0].id : null);
  }, true);

  // Header delete button — two-click confirm (same pattern as dropdown rows)
  const sessionDelHeader = document.getElementById('session-delete-header');
  if (sessionDelHeader) {
    sessionDelHeader.addEventListener('click', (e) => {
      e.stopPropagation();
      const sid = app.currentSessionId;
      if (!sid) return;
      const state = sessionDelHeader.dataset.state;
      if (state === 'trash') {
        sessionDelHeader.dataset.state = 'warning';
        sessionDelHeader.title = 'Click again to confirm delete';
        sessionDelHeader.innerHTML = icon('alert-triangle', { size: '14px' });
        sessionDelHeader.style.color = '#ff5577';
        setTimeout(() => {
          sessionDelHeader.dataset.state = 'trash';
          sessionDelHeader.title = 'Delete session';
          sessionDelHeader.innerHTML = icon('trash-2', { size: '14px' });
          sessionDelHeader.style.color = '';
        }, 3000);
      } else if (state === 'warning') {
        deleteSession(sid);
        sessionDelHeader.dataset.state = 'trash';
        sessionDelHeader.title = 'Delete session';
        sessionDelHeader.innerHTML = icon('trash-2', { size: '14px' });
        sessionDelHeader.style.color = '';
      }
    });
  }

  // Header delete button for agent — two-click confirm
  const agentDelHeader = document.getElementById('agent-delete-header');
  if (agentDelHeader) {
    agentDelHeader.addEventListener('click', (e) => {
      e.stopPropagation();
      const aid = app.currentAgentId;
      if (!aid) return;
      const state = agentDelHeader.dataset.state;
      if (state === 'trash') {
        agentDelHeader.dataset.state = 'warning';
        agentDelHeader.title = 'Click again to confirm delete';
        agentDelHeader.innerHTML = icon('alert-triangle', { size: '14px' });
        agentDelHeader.style.color = '#ff5577';
        setTimeout(() => {
          agentDelHeader.dataset.state = 'trash';
          agentDelHeader.title = 'Delete agent';
          agentDelHeader.innerHTML = icon('trash-2', { size: '14px' });
          agentDelHeader.style.color = '';
        }, 3000);
      } else if (state === 'warning') {
        confirmDeleteAgent(aid);
        agentDelHeader.dataset.state = 'trash';
        agentDelHeader.title = 'Delete agent';
        agentDelHeader.innerHTML = icon('trash-2', { size: '14px' });
        agentDelHeader.style.color = '';
      }
    });
  }

  // ── Custom agent dropdown ──
  const agentDropdown = document.getElementById('agent-dropdown');
  const agentTrigger  = document.getElementById('agent-dropdown-trigger');
  const agentMenu     = document.getElementById('agent-dropdown-menu');

  function openAgentMenu() {
    if (!agentMenu) return;
    // Opening one header dropdown closes the other — treat the gesture as
    // an outside-click for any peer menu.
    closeMenu();
    agentMenu.hidden = false;
    agentDropdown.classList.add('open');
  }

  function closeAgentMenu() {
    if (!agentMenu) return;
    agentMenu.hidden = true;
    agentDropdown.classList.remove('open');
  }

  function switchToAgent(aid, opts) {
    opts = opts || {};
    // forceNewSession: always begin a brand-new session, even if this agent
    //   already has a prior session or is already the current agent. Used by
    //   the page chat pills (Agents / Pages / Source Control / Agent Settings)
    //   so every hand-off to the webAgent starts a fresh conversation.
    // silent: skip the "Switched agent. New session started." info bubble —
    //   the caller is about to inject the user's own message immediately.
    const forceNew = !!opts.forceNewSession;
    const silent   = !!opts.silent;
    if (!aid) { closeAgentMenu(); return; }
    if (aid === app.currentAgentId && !forceNew) { closeAgentMenu(); return; }
    // Save the current session under the PREVIOUS agent before switching
    const prevAgentId = app.currentAgentId;
    if (prevAgentId && app.currentSessionId) {
      _lastSessionPerAgent.set(prevAgentId, app.currentSessionId);
      _saveLastSessionMap();
    }
    // Switching agent leaves the current session's run going in the
    // background — do NOT interrupt it. Reset local UI only.
    abortChatStream();
    app.currentAgentId = aid;
    localStorage.setItem('selectedAgentId', aid);
    // Look up the last session for this agent (skipped when forcing a new one)
    const lastSid = forceNew ? null : _lastSessionPerAgent.get(aid);
    if (lastSid) {
      app.currentSessionId = lastSid;
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
      // loadSessionChat will render messages; if the session was deleted
      // (restricted response) it auto-creates a fresh one.
      loadSessionChat(lastSid);
    } else {
      // No prior session for this agent — start a fresh one
      app.currentSessionId = generateUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
      if (!silent) app.addChatBubble('agent', 'Switched agent. New session started.');
    }
    populateSessionSelect(app.currentUserId);
    loopSessionChanged();
    loopVisualSessionChanged();
    autoAgentSessionChanged();
    chatActivitySessionChanged();
    _renderAgentRows();
    _setAgentTriggerLabel();
    closeAgentMenu();
  }

  // Expose so other modules (e.g. the Pages prompt bar) can drive the
  // right-side chat agent without duplicating session/teardown logic.
  app.switchToAgent = switchToAgent;

  // Ensure the user's webAgent (the `default` template) exists, switch the
  // chat to it, and start a brand-new session. This is the single entry point
  // for every page chat pill — Agents, Pages, Source Control, Agent Settings —
  // and for the "no agent yet" fallback in chat.js. Returns the agent id.
  async function startWebagentSession() {
    const id = await ensureWebagentAgent(app.currentUserId);
    switchToAgent(id, { forceNewSession: true, silent: true });
    return id;
  }
  app.startWebagentSession = startWebagentSession;
  app.ensureWebagentAgent = ensureWebagentAgent;

  function startAgentRename(aid, row) {
    const titleEl = row.querySelector('.agent-row-title');
    if (!titleEl) return;
    const agent = _agentsCache.find(a => a.id === aid);
    const current = (agent && agent.name) || '';
    const input = document.createElement('input');
    input.type = 'text';
    // Reuse the session input styling; the agent class lets the long-press
    // handler opt out of re-triggering on the input.
    input.className = 'agent-row-title-input session-row-title-input';
    input.value = current;
    titleEl.replaceWith(input);
    input.focus();
    input.select();

    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      const newName = input.value.trim();
      if (commit && newName && newName !== current) {
        const ok = await patchAgentName(aid, newName);
        if (ok && agent) agent.name = newName;
      }
      _renderAgentRows();
      _setAgentTriggerLabel();
      // Keep the Agents page label in sync with the rename.
      if (typeof app.refreshAgentsOrder === 'function') app.refreshAgentsOrder();
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
  }

  async function patchAgentName(aid, name) {
    try {
      const res = await fetch(apiPath('/api/v1/agents/' + encodeURIComponent(aid)), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: app.currentUserId, name }),
      });
      return res.ok;
    } catch (e) {
      console.warn('Failed to rename agent:', e);
      return false;
    }
  }

  async function confirmDeleteAgent(aid) {
    const agent = _agentsCache.find(a => a.id === aid);
    const name = (agent && agent.name) || 'this agent';
    if (!window.confirm(`Delete agent "${name}"? This cannot be undone.`)) return;
    try {
      const res = await fetch(
        apiPath('/api/v1/agents/' + encodeURIComponent(aid) + '?user_id=' + encodeURIComponent(app.currentUserId)),
        { method: 'DELETE' },
      );
      if (!res.ok) return;
      // If the active agent was deleted, drop the saved selection so
      // populateAgentSelect re-resolves to whatever agent remains.
      if (aid === app.currentAgentId) {
        try { localStorage.removeItem('selectedAgentId'); } catch (_) {}
      }
      // Re-fetch + re-render the dropdown (and the agent-scoped session list).
      await populateAgentSelect(app.currentUserId);
      // Mirror the removal on the Agents page if it's mounted.
      if (typeof app.refreshAgentsOrder === 'function') app.refreshAgentsOrder();
    } catch (e) {
      console.warn('Failed to delete agent:', e);
    }
  }

  function openAgentConfig(aid) {
    const sel = document.getElementById('main-tab-select');
    if (sel) {
      sel.value = 'agents';
      sel.dispatchEvent(new Event('change'));
    }
    // Defer to allow startAgents() to populate the grid before expanding
    setTimeout(() => {
      if (window.expandAgent) window.expandAgent(aid);
    }, 50);
  }

  // ── Header trigger: long-press or double-click to rename ──
  function _headerRenameAgent() {
    const labelEl = document.getElementById('agent-dropdown-label');
    if (!labelEl) return;
    const aid = app.currentAgentId;
    if (!aid) return;
    const agent = _agentsCache.find(a => a.id === aid);
    const current = (agent && agent.name) || (window.__agentName) || '';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'session-row-title-input';
    input.value = current;
    input.style.width = '140px';
    labelEl.replaceWith(input);
    // Override the trigger's pointer-events:none on direct children so clicks
    // reach the input and the cursor can be placed at the click location.
    input.style.pointerEvents = 'auto';
    input.focus();
    input.select();
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      const newName = input.value.trim();
      if (commit && newName && newName !== current) {
        const ok = await patchAgentName(aid, newName);
        if (ok && agent) agent.name = newName;
      }
      _setAgentTriggerLabel();
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
  }

  if (agentTrigger) {
    let _lpTimer = null, _lpStartX = 0, _lpStartY = 0;
    agentTrigger.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.header-delete-btn, .header-plus-btn, .agent-dropdown-status')) return;
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      _lpStartX = e.clientX; _lpStartY = e.clientY;
      _lpTimer = setTimeout(() => {
        _lpTimer = null;
        e.preventDefault();
        _headerRenameAgent();
      }, 500);
    });
    agentTrigger.addEventListener('pointermove', (e) => {
      if (!_lpTimer) return;
      if (Math.abs(e.clientX - _lpStartX) > 8 || Math.abs(e.clientY - _lpStartY) > 8) {
        clearTimeout(_lpTimer); _lpTimer = null;
      }
    });
    agentTrigger.addEventListener('pointerup', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
    agentTrigger.addEventListener('pointercancel', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
    agentTrigger.addEventListener('dblclick', (e) => {
      if (e.target.closest('.header-delete-btn, .header-plus-btn, .agent-dropdown-status')) return;
      e.stopPropagation();
      _headerRenameAgent();
    });
    agentTrigger.addEventListener('click', (e) => {
      // Don't toggle dropdown when clicking the delete button inside the trigger
      if (e.target.closest('.header-delete-btn, .header-plus-btn')) return;
      // If the label was replaced by a rename input, don't toggle dropdown
      if (e.target.closest('.session-row-title-input')) return;
      e.stopPropagation();
      if (agentMenu.hidden) openAgentMenu(); else closeAgentMenu();
    });
  }

  if (agentMenu) {
    agentMenu.addEventListener('click', (e) => {
      e.stopPropagation();
      // Config button (right side)
      const cfgBtn = e.target.closest('.agent-row-config');
      if (cfgBtn) {
        const aid = cfgBtn.dataset.id;
        closeAgentMenu();
        if (aid) openAgentConfig(aid);
        return;
      }
      // Delete button (far right)
      const delBtn = e.target.closest('.agent-row-delete');
      if (delBtn) {
        const aid = delBtn.dataset.id;
        if (aid) confirmDeleteAgent(aid);
        return;
      }
      // Row body click → switch agent (ignore clicks inside the rename input)
      if (e.target.closest('.agent-row-title-input')) return;
      const row = e.target.closest('.agent-row-item');
      if (row) switchToAgent(row.dataset.id);
    });
    // Drag the grip handle to reorder; press-and-hold it to pin. Per-account,
    // mirrored on the Agents page.
    makeRowsReorderable(agentMenu, {
      rowSelector: '.agent-row-item',
      handleSelector: '.row-drag-handle',
      onReorder: _applyAgentReorder,
      onHandleLongPress: (aid) => _toggleAgentPin(aid),
    });
    // Press-and-hold the row body to rename (grip + action buttons opt out).
    attachRowLongPress(agentMenu, {
      rowSelector: '.agent-row-item',
      ignoreSelector: '.row-drag-handle, .agent-row-config, .agent-row-delete, .agent-row-title-input',
      onLongPress: (aid, row) => startAgentRename(aid, row),
    });
  }

  document.addEventListener('click', (e) => {
    if (agentDropdown && !agentDropdown.contains(e.target)) closeAgentMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && agentMenu && !agentMenu.hidden) closeAgentMenu();
  });

  // ── + new agent button ──
  const agentNewBtn = document.getElementById('agent-new');
  if (agentNewBtn) {
    // Use pointerdown — see note on sessionNewBtn above.
    agentNewBtn.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      e.stopPropagation();
      closeAgentMenu();
      const sel = document.getElementById('main-tab-select');
      if (sel) {
        sel.value = 'agents';
        sel.dispatchEvent(new Event('change'));
      }
      // Defer to let the agents tab render, then expand the mock card
      setTimeout(() => {
        if (window.expandAgent) {
          window.expandAgent('__new__');
        }
      }, 50);
    });
  }

  // Restore the last-session-per-agent map from localStorage
  _loadLastSessionMap();

  // populateUserSelect drives populateAgentSelect, which sets currentAgentId.
  // Phase 1: agents (eagerly fetched in populateUserSelect).
  // Phase 2: sessions (eagerly fetched right after agents resolve).
  // Phase 3: messages (loaded after both are settled).
  populateUserSelect().then(function () {
    if (app.currentUserId) {
      _sessionsLoaded = true;
      populateSessionSelect(app.currentUserId);
    }
    if (app.currentSessionId) {
      loadSessionChat(app.currentSessionId);
    }
  });

  // ── TUI bridge status poll ──
  // Refresh the TUI bridge indicator every 15 seconds so it stays in sync.
  setInterval(() => {
    const tuiIndicator = document.getElementById('tui-bridge-indicator');
    if (!tuiIndicator || tuiIndicator.style.display === 'none') return;
    fetch('/api/v1/chat/tui-bridge/status')
      .then(r => r.json())
      .then(data => {
        tuiIndicator.title = data.alive
          ? 'TUI agent bridge active (port ' + data.port + ')'
          : 'TUI agent bridge not connected — start the Server Manager (TUI)';
        tuiIndicator.style.opacity = data.alive ? '1' : '0.4';
      })
      .catch(() => {});
  }, 15000);

  // ── Session counter poll ──
  // Refresh the header notification badge every 10 seconds so it stays in sync
  // with session status changes (runs completing, erroring, etc.).
  setInterval(() => {
    _updateHeaderSessionCounter();
  }, 10000);
}
