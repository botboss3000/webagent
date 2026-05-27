'use strict';

import { app } from './state.js';
import { loopSessionChanged } from './loop.js';
import { loopVisualSessionChanged } from './loop-logic.js';
import { autoAgentSessionChanged } from './autoagent.js';
import { abortChatStream } from './chat.js';
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
import { showLeftOverlay } from './left-login.js';
import { randomUUID } from './uuid.js';

export function generateUUID() {
  return randomUUID();
}

// ── Agent selector ───────────────────────────────────────────────────────────

// Cache of last-fetched agents (templates + customs, in display order)
let _agentsCache = [];

function _pinnedAgentsKey() {
  return `pinnedAgents:${app.currentUserId || 'anon'}`;
}

function _getPinnedAgents() {
  try {
    const raw = localStorage.getItem(_pinnedAgentsKey());
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch (_) { return new Set(); }
}

function _setPinnedAgents(set) {
  try {
    localStorage.setItem(_pinnedAgentsKey(), JSON.stringify(Array.from(set)));
  } catch (_) {}
}

function _toggleAgentPin(agentId) {
  const pinned = _getPinnedAgents();
  if (pinned.has(agentId)) pinned.delete(agentId);
  else pinned.add(agentId);
  _setPinnedAgents(pinned);
  // Refresh agents cache (pinned flag) and re-render
  const pinnedNow = _getPinnedAgents();
  _agentsCache = _agentsCache.map(a => ({ ...a, pinned: pinnedNow.has(a.id) }));
  _agentsCache.sort(_agentSortFn);
  _renderAgentRows();
  _setAgentTriggerLabel();
}

function _agentSortFn(a, b) {
  if (!!b.pinned - !!a.pinned !== 0) return (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0);
  // Within pinned/unpinned groups: templates first, then customs; keep server order
  if (a.type !== b.type) return a.type === 'template' ? -1 : 1;
  return 0;
}

function _setAgentTriggerLabel() {
  const labelEl = document.getElementById('agent-dropdown-label');
  if (!labelEl) return;
  const aid = app.currentAgentId;
  const found = _agentsCache.find(a => a.id === aid);
  const title = (found && found.name) || (window.__agentName) || aid || 'No agent';
  labelEl.textContent = _truncate(title, 20);
  labelEl.title = (found && found.name) || title || '';
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
    row.innerHTML = `
      <span class="agent-row-pin-icon">${icon('pin', { size: '12px' })}</span>
      <span class="agent-row-title" title="${a.id}">${_truncate(label, 28).replace(/</g, '&lt;')}</span>
      <button class="agent-row-kebab" title="Agent actions" data-id="${a.id}">${icon('more-vertical', { size: '14px' })}</button>
    `;
    menu.appendChild(row);
  }
}

export async function populateAgentSelect(userId) {
  if (!userId) return;

  try {
    const agentsRes = await fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`));
    const agentsData = agentsRes.ok ? await agentsRes.json() : { agents: [] };

    const saved = localStorage.getItem('selectedAgentId');
    const pinned = _getPinnedAgents();

    // Only the user's actual custom agents appear in the chat-header dropdown.
    // System templates are creation seeds, not chat targets — they're surfaced
    // in the "New agent" modal's template picker (see agents.js).
    const customs = agentsData.agents || [];

    _agentsCache = customs.map(a => ({
      id: a.id,
      name: a.name || a.id.slice(0, 12),
      type: 'custom',
      pinned: pinned.has(a.id),
    }));
    _agentsCache.sort(_agentSortFn);

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
    row.innerHTML = `
      <span class="session-row-pin-icon">${icon('pin', { size: '12px' })}</span>
      <span class="session-row-title" title="${s.id}">${_truncate(label, 28).replace(/</g, '&lt;')}</span>
      <button class="session-row-kebab" title="Session actions" data-id="${s.id}">${icon('more-vertical', { size: '14px' })}</button>
    `;
    menu.appendChild(row);
  }
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
    }));
    // If current session not yet in DB (fresh session before first msg),
    // synthesize a row so trigger label shows "New Session" and it appears
    if (app.currentSessionId && !_sessionsCache.some(s => s.id === app.currentSessionId)) {
      _sessionsCache.unshift({
        id: app.currentSessionId,
        title: 'New Session',
        created_at: null,
        pinned: false,
      });
    }
    _renderSessionRows();
    _setTriggerLabel();
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
    for (const msg of data.messages) {
      if (msg.role === 'user') {
        app.addChatBubble('user', msg.content);
      } else if (msg.role === 'assistant') {
        let text = msg.content || '';
        const toolCallIdx = text.indexOf('\n\n[Tool calls: ');
        if (toolCallIdx !== -1) text = text.slice(0, toolCallIdx);
        app.addChatBubble('agent', text);
      }
    }
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
}

export function initSessions() {

  // ── Theme system ──
  const STORAGE_KEY = 'webagent_theme';

  /** Set theme on <body>: 'light', 'dark', or 'system' (follow OS). */
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

  function closeActionsPopup() {
    const open = document.querySelector('.session-row-actions');
    if (open) open.remove();
  }

  function openMenu() {
    if (!menu) return;
    // Opening one header dropdown closes the other — treat the gesture as
    // an outside-click for any peer menu.
    closeAgentMenu();
    menu.hidden = false;
    dropdown.classList.add('open');
  }

  function closeMenu() {
    if (!menu) return;
    menu.hidden = true;
    dropdown.classList.remove('open');
    closeActionsPopup();
  }

  function switchToSession(sid) {
    if (!sid || sid === app.currentSessionId) { closeMenu(); return; }
    // Tear down the LOCAL SSE fetch only — the BACKEND agent loop keeps
    // running for the session we're leaving. Its events still accumulate
    // in the server-side RunBuffer so we can replay them if we come back.
    abortChatStream();
    app.currentSessionId = sid;
    localStorage.setItem('terminalSessionId', app.currentSessionId);
    loadSessionChat(sid);
    loopSessionChanged();
    loopVisualSessionChanged();
    autoAgentSessionChanged();
    _renderSessionRows();
    _setTriggerLabel();
    closeMenu();

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

  function openRowActions(sid, row) {
    closeActionsPopup();
    const sess = _sessionsCache.find(s => s.id === sid);
    if (!sess) return;
    const popup = document.createElement('div');
    popup.className = 'session-row-actions';
    popup.dataset.id = sid;
    popup.innerHTML = `
      <button class="session-row-action" data-action="pin">${icon('pin', { size: '14px' })} ${sess.pinned ? 'Unpin' : 'Pin'}</button>
      <button class="session-row-action" data-action="rename">${icon('pencil', { size: '14px' })} Rename</button>
      <button class="session-row-action danger" data-action="delete">${icon('trash-2', { size: '14px' })} Delete</button>
    `;
    document.body.appendChild(popup);
    // Position next to kebab, right-aligned, clamped to viewport
    const kebab = row.querySelector('.session-row-kebab');
    const kb = kebab.getBoundingClientRect();
    const pw = popup.offsetWidth;
    const ph = popup.offsetHeight;
    let left = kb.right - pw;
    let top  = kb.bottom + 4;
    if (left < 4) left = 4;
    if (left + pw > window.innerWidth - 4) left = window.innerWidth - pw - 4;
    if (top + ph > window.innerHeight - 4) top = kb.top - ph - 4;
    popup.style.left = left + 'px';
    popup.style.top  = top + 'px';
    // Actions routed here since popup is no longer inside the menu
    popup.addEventListener('click', (e) => {
      e.stopPropagation();
      const actionBtn = e.target.closest('.session-row-action');
      if (!actionBtn) return;
      const action = actionBtn.dataset.action;
      closeActionsPopup();
      if (!action) return;
      if (action === 'pin') togglePin(sid);
      else if (action === 'rename') startRename(sid, row);
      else if (action === 'delete') deleteSession(sid);
    });
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
      e.stopPropagation();
      if (menu.hidden) openMenu(); else closeMenu();
    });
  }

  if (menu) {
    menu.addEventListener('click', (e) => {
      e.stopPropagation();
      // Kebab button?
      const kebab = e.target.closest('.session-row-kebab');
      if (kebab) {
        const row = kebab.closest('.session-row');
        const sid = row && row.dataset.id;
        // Toggle: if popup already open for this row, close it
        const existing = document.querySelector(`.session-row-actions[data-id="${sid}"]`);
        if (existing) { closeActionsPopup(); return; }
        if (sid) openRowActions(sid, row);
        return;
      }
      // Row body click → switch session (ignore clicks inside rename input)
      if (e.target.closest('.session-row-title-input')) return;
      const row = e.target.closest('.session-row');
      if (row) switchToSession(row.dataset.id);
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
      abortChatStream();
      app.currentSessionId = generateUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      app.chatMessages.innerHTML = '';
      app.addChatBubble('agent', 'New session. Start typing below.');
      populateSessionSelect(app.currentUserId);
      loopSessionChanged();
      loopVisualSessionChanged();
      autoAgentSessionChanged();
    });
  }

  // ── Custom agent dropdown ──
  const agentDropdown = document.getElementById('agent-dropdown');
  const agentTrigger  = document.getElementById('agent-dropdown-trigger');
  const agentMenu     = document.getElementById('agent-dropdown-menu');

  function closeAgentActionsPopup() {
    const open = document.querySelector('.agent-row-actions');
    if (open) open.remove();
  }

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
    closeAgentActionsPopup();
  }

  function switchToAgent(aid) {
    if (!aid || aid === app.currentAgentId) { closeAgentMenu(); return; }
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
    _renderAgentRows();
    _setAgentTriggerLabel();
    closeAgentMenu();
  }

  // Expose so other modules (e.g. the Pages prompt bar) can drive the
  // right-side chat agent without duplicating session/teardown logic.
  app.switchToAgent = switchToAgent;

  function openAgentRowActions(aid, row) {
    closeAgentActionsPopup();
    const agent = _agentsCache.find(a => a.id === aid);
    if (!agent) return;
    const popup = document.createElement('div');
    popup.className = 'agent-row-actions';
    popup.dataset.id = aid;
    // Templates can't be opened in Agents page (which lists custom agents only)
    const configBtn = agent.type === 'custom'
      ? `<button class="agent-row-action" data-action="config">${icon('settings', { size: '14px' })} Config</button>`
      : '';
    popup.innerHTML = `
      <button class="agent-row-action" data-action="pin">${icon('pin', { size: '14px' })} ${agent.pinned ? 'Unpin' : 'Pin'}</button>
      ${configBtn}
    `;
    document.body.appendChild(popup);
    // Position next to the kebab, right-aligned, below it. Clamp inside viewport.
    const kebab = row.querySelector('.agent-row-kebab');
    const kb = kebab.getBoundingClientRect();
    const pw = popup.offsetWidth;
    const ph = popup.offsetHeight;
    let left = kb.right - pw;
    let top  = kb.bottom + 4;
    if (left < 4) left = 4;
    if (left + pw > window.innerWidth - 4) left = window.innerWidth - pw - 4;
    if (top + ph > window.innerHeight - 4) top = kb.top - ph - 4;
    popup.style.left = left + 'px';
    popup.style.top  = top + 'px';
    // Action clicks routed here (popup is no longer inside agentMenu)
    popup.addEventListener('click', (e) => {
      e.stopPropagation();
      const actionBtn = e.target.closest('.agent-row-action');
      if (!actionBtn) return;
      const action = actionBtn.dataset.action;
      closeAgentActionsPopup();
      if (!action) return;
      if (action === 'pin') _toggleAgentPin(aid);
      else if (action === 'config') { closeAgentMenu(); openAgentConfig(aid); }
    });
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
      e.stopPropagation();
      if (agentMenu.hidden) openAgentMenu(); else closeAgentMenu();
    });
  }

  if (agentMenu) {
    agentMenu.addEventListener('click', (e) => {
      e.stopPropagation();
      // Kebab?
      const kebab = e.target.closest('.agent-row-kebab');
      if (kebab) {
        const row = kebab.closest('.agent-row-item');
        const aid = row && row.dataset.id;
        const existing = document.querySelector(`.agent-row-actions[data-id="${aid}"]`);
        if (existing) { closeAgentActionsPopup(); return; }
        if (aid) openAgentRowActions(aid, row);
        return;
      }
      // Row body click → switch agent
      const row = e.target.closest('.agent-row-item');
      if (row) switchToAgent(row.dataset.id);
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
