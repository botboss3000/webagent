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

export function generateUUID() {
  return randomUUID();
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
  const labelEl = document.getElementById('agent-dropdown-label');
  if (!labelEl) return;
  const aid = app.currentAgentId;
  const found = _agentsCache.find(a => a.id === aid);
  const title = (found && found.name) || (window.__agentName) || aid || 'No agent';
  labelEl.textContent = _truncate(title, 20);
  labelEl.title = (found && found.name) || title || '';
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
    row.innerHTML = `
      <span class="row-drag-handle" data-drag-handle title="Drag to reorder · hold to pin">${icon('grip-vertical', { size: '13px' })}</span>
      <span class="agent-row-pin-icon">${icon('pin', { size: '12px' })}</span>
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
    const agentsRes = await fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`));
    const agentsData = agentsRes.ok ? await agentsRes.json() : { agents: [] };

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

    // Sessions are scoped to an agent — refresh the session list now that
    // currentAgentId is settled.
    populateSessionSelect(userId);
  } catch (e) {
    console.warn('Failed to load agents for selector:', e);
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
  // Refresh the agent list, which in turn refreshes sessions filtered by the
  // resolved agent. Going through populateAgentSelect (rather than calling
  // populateSessionSelect directly here) guarantees currentAgentId is set
  // before the session query fires — otherwise the first fetch would have
  // no agent_id filter and could clobber the filtered result on slow
  // networks. Skipped while the session menu is open so the user doesn't
  // lose their place mid-click.
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

  // Current user row (large avatar + name + email)
  const cur = document.getElementById('dropdown-current-row');
  if (cur) {
    cur.innerHTML = '';
    if (active) {
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
      const info = document.createElement('div');
      info.className = 'user-dropdown-current-info';
      info.innerHTML = '<div class="user-dropdown-current-name">Signed out</div>';
      cur.appendChild(info);
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

  // Refresh Lucide icons for any newly-inserted SVG containers
  try {
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      window.lucide.createIcons();
    }
  } catch (_) {}
}

// Cache of last-fetched sessions (used when rendering rows without refetch)
let _sessionsCache = [];

// Monotonic counter — on init we fire one fetch from populateUserSelect (no
// agent filter yet) and another from populateAgentSelect (filtered once the
// agent is resolved). Whichever resolves last would otherwise win, so we
// tag each call and drop responses from stale calls.
let _sessionFetchSeq = 0;

function _truncate(s, n) {
  return (s && s.length > n) ? s.slice(0, n) + '…' : (s || '');
}

function _setTriggerLabel() {
  const labelEl = document.getElementById('session-dropdown-label');
  if (!labelEl) return;
  const sid = app.currentSessionId;
  const found = _sessionsCache.find(s => s.id === sid);
  const title = (found && found.title) || 'New Session';
  labelEl.textContent = _truncate(title, 20);
  labelEl.title = (found && found.id) || sid || '';
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
}

function _renderSessionRows() {
  const menu = document.getElementById('session-dropdown-menu');
  if (!menu) return;
  menu.innerHTML = '';
  if (!_sessionsCache.length) {
    const empty = document.createElement('div');
    empty.className = 'session-dropdown-empty';
    empty.textContent = 'No sessions yet';
    menu.appendChild(empty);
    return;
  }
  for (const s of _sessionsCache) {
    const row = document.createElement('div');
    row.className = 'session-row' + (s.pinned ? ' pinned' : '') + (s.id === app.currentSessionId ? ' selected' : '');
    row.dataset.id = s.id;
    const label = s.title || 'New Session';
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
      <span class="session-row-title" title="Hold to rename">${_truncate(label, 28).replace(/</g, '&lt;')}</span>
      <button class="session-row-delete" title="Delete session" data-id="${s.id}" data-state="trash">${icon('trash-2', { size: '14px' })}</button>
    `;
    menu.appendChild(row);
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
    const agentId = app.currentAgentId || '';
    let url = `/api/v1/db/sessions?db=local.db&user_id=${encodeURIComponent(userId)}`;
    if (agentId) url += `&agent_id=${encodeURIComponent(agentId)}`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(apiPath(url));
    const data = await res.json();
    if (mySeq !== _sessionFetchSeq) return; // a newer fetch superseded us
    _sessionsCache = (data.sessions || []).map(s => ({
      id: s.id,
      title: s.title || 'New Session',
      created_at: s.created_at,
      pinned: !!s.pinned,
      run_status: s.run_status || null,
      has_unread: !!s.has_unread,
    }));
    // If current session not yet in DB (fresh session before first msg),
    // synthesize a row so trigger label shows "New Session" and it appears
    if (app.currentSessionId && !_sessionsCache.some(s => s.id === app.currentSessionId)) {
      _sessionsCache.unshift({
        id: app.currentSessionId,
        title: 'New Session',
        created_at: null,
        pinned: false,
        run_status: null,
        has_unread: false,
      });
    }
    _renderSessionRows();
    _setTriggerLabel();
    _setAgentTriggerLabel();  // refresh agent status icon based on session states
  } catch (e) {
    console.warn('Failed to load sessions:', e);
  }
}

export async function loadSessionChat(sessionId) {
  try {
    const token = localStorage.getItem('auth_token');
    const res = await fetch(
      apiPath(`/api/v1/db/session-messages?db=local.db&session_id=${encodeURIComponent(sessionId)}${token ? '&token=' + encodeURIComponent(token) : ''}`),
    );
    const data = await res.json();
    app.chatMessages.innerHTML = '';

    if (data.restricted) {
      // Not a participant — silently switch to a fresh session instead of showing restricted notice
      app.currentSessionId = generateUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      app.chatMessages.innerHTML = '';
      app.addChatBubble('agent', 'New session. Start typing below.');
      if (app.currentUserId) populateSessionSelect(app.currentUserId);
      return;
    }

    if (!data.messages || data.messages.length === 0) {
      app.addChatBubble(
        'agent',
        'Session loaded. No messages yet — start typing below.',
      );
      return;
    }
    // Durable run-state: is a turn in progress for this session right now?
    const run = data.run || null;
    let seededStreaming = false;

    // Each assistant row (including intermediate steps that precede tool calls)
    // is its own bubble, keyed by its interaction id so the user sees EVERY
    // agent response in a turn, not just the final one. Empty rows (tool-call-
    // only steps) render nothing.
    for (const msg of data.messages) {
      if (msg.role === 'user') {
        app.addChatBubble('user', msg.content, undefined, undefined, undefined, msg.id);
      } else if (msg.role === 'assistant') {
        let text = msg.content || '';
        const toolCallIdx = text.indexOf('\n\n[Tool calls: ');
        if (toolCallIdx !== -1) text = text.slice(0, toolCallIdx);
        const hasText = !!text.trim();
        if (msg.status === 'streaming') {
          // In-progress step — render the persisted partial as a live bubble the
          // WebSocket will continue updating (keyed by THIS row's id == asst_id).
          if (typeof app.seedStreamingBubble === 'function') {
            app.seedStreamingBubble(msg.id, text);
          } else {
            app.addChatBubble('agent', text || '…', 'streaming', undefined, msg.id);
          }
          seededStreaming = true;
        } else if (!hasText) {
          continue; // empty tool-call-only step — nothing to show
        } else if (msg.status === 'interrupted') {
          app.addChatBubble('agent', text + '\n\n(interrupted)', 'interrupted', undefined, msg.id);
        } else if (msg.status === 'error') {
          app.addChatBubble('agent', text, 'error', undefined, msg.id);
        } else {
          app.addChatBubble('agent', text, undefined, undefined, msg.id);
        }
      }
    }

    // If a run is active, lock display state + set the WS resume floor so
    // replayed chunks only bring text newer than the partial we just rendered.
    // The existing resume paths (switchToSession + WS onopen handshake) replay.
    if (run && run.active) {
      app.isProcessing = true;
      if (!seededStreaming && run.assistant_interaction_id
          && typeof app.ensureStreamingBubbleForActiveTurn === 'function') {
        app.ensureStreamingBubbleForActiveTurn(run.assistant_interaction_id);
      }
      if (!app.lastSessionSeq) app.lastSessionSeq = {};
      const floor = typeof run.latest_session_seq === 'number' ? run.latest_session_seq : 0;
      app.lastSessionSeq[sessionId] = Math.max(app.lastSessionSeq[sessionId] || 0, floor);
    } else {
      app.isProcessing = false;
    }
    // Input availability follows text presence, not run state — sending a
    // follow-up while the agent works is allowed (it interrupts + replaces).
    if (app.chatSend) app.chatSend.disabled = !((app.chatInput && app.chatInput.value.trim()));

    app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
  } catch (e) {
    console.warn('Failed to load session messages:', e);
  }
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
  const userDropdown = document.getElementById('user-dropdown');
  const dropdownMenu = document.getElementById('user-dropdown-menu');
  const trigger = document.querySelector('.user-dropdown-trigger');

  if (trigger && dropdownMenu) {
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = dropdownMenu.style.display === 'block';
      dropdownMenu.style.display = isOpen ? 'none' : 'block';
      userDropdown.classList.toggle('open', !isOpen);
    });

    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
      if (!userDropdown.contains(e.target)) {
        dropdownMenu.style.display = 'none';
        userDropdown.classList.remove('open');
      }
    });

    // Close on Escape
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && dropdownMenu.style.display === 'block') {
        dropdownMenu.style.display = 'none';
        userDropdown.classList.remove('open');
      }
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

  function openMenu() {
    if (!menu) return;
    // Opening one header dropdown closes the other — treat the gesture as
    // an outside-click for any peer menu.
    closeAgentMenu();
    menu.hidden = false;
    dropdown.classList.add('open');
    _resetAllDeleteButtons();
  }

  function closeMenu() {
    if (!menu) return;
    menu.hidden = true;
    dropdown.classList.remove('open');
    _resetAllDeleteButtons();
  }

  async function switchToSession(sid) {
    if (!sid || sid === app.currentSessionId) { closeMenu(); return; }
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
          app.chatMessages.innerHTML = '';
          app.addChatBubble('agent', 'Session deleted. New session created.');
        }
        await populateSessionSelect(app.currentUserId);
      }
    } catch (e) {
      console.warn('Failed to delete session:', e);
    }
  }

  if (sessionTrigger) {
    sessionTrigger.addEventListener('click', (e) => {
      // Don't toggle dropdown when clicking the delete button inside the trigger
      if (e.target.closest('.header-delete-btn')) return;
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

  const sessionNewBtn = document.getElementById('session-new');
  if (sessionNewBtn) {
    // Use pointerdown — lucide replaces the inner SVG paths between mousedown
    // and mouseup, which prevents the browser from synthesising a click event.
    sessionNewBtn.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      closeMenu();
      // Starting a new session leaves the current one running in the background —
      // do NOT interrupt it. Only reset local UI state.
      abortChatStream();
      app.currentSessionId = generateUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      app.chatMessages.innerHTML = '';
      app.addChatBubble('agent', 'New session. Start typing below.');
      populateSessionSelect(app.currentUserId);
      loopSessionChanged();
      loopVisualSessionChanged();
      autoAgentSessionChanged();
      chatActivitySessionChanged();
    });
  }

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

  function switchToAgent(aid) {
    if (!aid || aid === app.currentAgentId) { closeAgentMenu(); return; }
    // Switching agent starts a fresh session but leaves the current session's
    // run going in the background — do NOT interrupt it. Reset local UI only.
    abortChatStream();
    app.currentAgentId = aid;
    localStorage.setItem('selectedAgentId', aid);
    // Sessions are bound to agents — start a fresh session
    app.currentSessionId = generateUUID();
    localStorage.setItem('terminalSessionId', app.currentSessionId);
    app.chatMessages.innerHTML = '';
    app.addChatBubble('agent', 'Switched agent. New session started.');
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

  if (agentTrigger) {
    agentTrigger.addEventListener('click', (e) => {
      // Don't toggle dropdown when clicking the delete button inside the trigger
      if (e.target.closest('.header-delete-btn')) return;
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
      // Defer to let startAgents() bind the create modal button before clicking
      setTimeout(() => {
        const btn = document.getElementById('btn-new-agent');
        if (btn) btn.click();
      }, 50);
    });
  }

  // populateUserSelect now drives populateAgentSelect, which in turn drives
  // populateSessionSelect — see the comment in populateUserSelect for why we
  // funnel through that chain instead of starting two fetches in parallel.
  populateUserSelect().then(function () {
    if (app.currentSessionId) {
      loadSessionChat(app.currentSessionId);
    }
  });
}
