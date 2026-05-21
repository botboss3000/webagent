'use strict';

import { app } from './state.js';
import { loopSessionChanged } from './loop.js';
import { loopVisualSessionChanged } from './loop-logic.js';
import { autoAgentSessionChanged } from './autoagent.js';
import { streamSessionChanged } from './stream.js';
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

export function generateUUID() {
  return crypto.randomUUID();
}

// ── Agent selector ───────────────────────────────────────────────────────────

export async function populateAgentSelect(userId) {
  const sel = document.getElementById('agent-select');
  if (!sel || !userId) return;

  try {
    const [agentsRes, templatesRes, profileRes] = await Promise.all([
      fetch(apiPath(`/api/v1/agents?user_id=${encodeURIComponent(userId)}`)),
      fetch(apiPath(`/api/v1/agents/templates?user_id=${encodeURIComponent(userId)}`)),
      fetch(apiPath(`/api/v1/user/profile?user_id=${encodeURIComponent(userId)}`)),
    ]);
    const agentsData = agentsRes.ok ? await agentsRes.json() : { agents: [] };
    const templatesData = templatesRes.ok ? await templatesRes.json() : { templates: [] };
    const profileData = profileRes.ok ? await profileRes.json() : {};

    const defaultAgentId = profileData.default_agent_id || 'default';
    const saved = localStorage.getItem('selectedAgentId');

    sel.innerHTML = '';

    const templates = (templatesData.templates || []).filter(t => t.id !== 'admin-agent');
    for (const t of templates) {
      const opt = document.createElement('option');
      opt.value = t.id;
      opt.dataset.type = 'template';
      const label = t.name || t.id;
      opt.textContent = label.length > 22 ? label.slice(0, 22) + '...' : label;
      opt.title = t.name || t.id;
      sel.appendChild(opt);
    }

    const customs = agentsData.agents || [];
    if (customs.length && templates.length) {
      const sep = document.createElement('option');
      sep.disabled = true;
      sep.textContent = '───────────';
      sel.appendChild(sep);
    }
    for (const a of customs) {
      const opt = document.createElement('option');
      opt.value = a.id;
      opt.dataset.type = 'custom';
      const label = a.name || a.id.slice(0, 12);
      opt.textContent = label.length > 22 ? label.slice(0, 22) + '...' : label;
      opt.title = a.name || a.id;
      sel.appendChild(opt);
    }

    // Pre-select: __agentId (public URL) > saved > default > first option
    let target = window.__agentId || saved || defaultAgentId;
    let found = false;
    for (const o of sel.options) {
      if (o.value === target) { o.selected = true; found = true; break; }
    }

    // For public agent URLs: if the specific agent isn't in the list (anon user
    // doesn't own it), add a synthetic option so the correct UUID is sent to chat.
    if (!found && window.__agentId) {
      sel.innerHTML = '';
      const opt = document.createElement('option');
      opt.value = window.__agentId;
      const label = window.__agentName || window.__agentId.slice(0, 12);
      opt.textContent = label.length > 22 ? label.slice(0, 22) + '...' : label;
      opt.title = window.__agentName || window.__agentId;
      opt.selected = true;
      sel.appendChild(opt);
    } else if (!found && sel.options.length) {
      sel.options[0].selected = true;
    }

    app.currentAgentId = sel.value || '';

    // Lock the selector when visiting a public agent URL
    if (window.__agentId) {
      sel.disabled = true;
    }
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
  // Populate session select for current user
  if (app.currentUserId) {
    // Skip refresh while dropdown is open so user doesn't lose place
    const menu = document.getElementById('session-dropdown-menu');
    const isOpen = menu && !menu.hidden;
    if (!isOpen) {
      populateSessionSelect(app.currentUserId);
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
  try {
    const token = localStorage.getItem('auth_token');
    const res = await fetch(
      apiPath(`/api/v1/db/sessions?db=local.db&user_id=${encodeURIComponent(userId)}${token ? '&token=' + encodeURIComponent(token) : ''}`),
    );
    const data = await res.json();
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

  // Load saved theme on init
  let savedTheme = 'dark';
  try { savedTheme = localStorage.getItem(STORAGE_KEY) || 'dark'; } catch (_) {}
  applyTheme(savedTheme);
  highlightThemeOption(savedTheme);

  // Listen to system preference changes when in 'system' mode
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  mq.addEventListener('change', () => {
    let current = 'dark';
    try { current = localStorage.getItem(STORAGE_KEY) || 'dark'; } catch (_) {}
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
      localStorage.removeItem('terminalUserId');
      window.location.reload();
    });
  }

  // ── Custom session dropdown ──
  const dropdown = document.getElementById('session-dropdown');
  const sessionTrigger = document.getElementById('session-dropdown-trigger');
  const menu = document.getElementById('session-dropdown-menu');

  function closeActionsPopup() {
    const open = menu && menu.querySelector('.session-row-actions');
    if (open) open.remove();
  }

  function openMenu() {
    if (!menu) return;
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
    abortChatStream();
    app.currentSessionId = sid;
    localStorage.setItem('terminalSessionId', app.currentSessionId);
    loadSessionChat(sid);
    streamSessionChanged();
    loopSessionChanged();
    loopVisualSessionChanged();
    autoAgentSessionChanged();
    _renderSessionRows();
    _setTriggerLabel();
    closeMenu();
  }

  function openRowActions(sid, row) {
    closeActionsPopup();
    const sess = _sessionsCache.find(s => s.id === sid);
    if (!sess) return;
    const popup = document.createElement('div');
    popup.className = 'session-row-actions';
    popup.innerHTML = `
      <button class="session-row-action" data-action="pin">${icon('pin', { size: '14px' })} ${sess.pinned ? 'Unpin' : 'Pin'}</button>
      <button class="session-row-action" data-action="rename">${icon('pencil', { size: '14px' })} Rename</button>
      <button class="session-row-action danger" data-action="delete">${icon('trash-2', { size: '14px' })} Delete</button>
    `;
    row.appendChild(popup);
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
      // Action button inside popup?
      const actionBtn = e.target.closest('.session-row-action');
      if (actionBtn) {
        const popup = actionBtn.closest('.session-row-actions');
        const row = popup && popup.closest('.session-row');
        const sid = row && row.dataset.id;
        const action = actionBtn.dataset.action;
        closeActionsPopup();
        if (!sid || !action) return;
        if (action === 'pin') togglePin(sid);
        else if (action === 'rename') startRename(sid, row);
        else if (action === 'delete') deleteSession(sid);
        return;
      }
      // Kebab button?
      const kebab = e.target.closest('.session-row-kebab');
      if (kebab) {
        const row = kebab.closest('.session-row');
        const sid = row && row.dataset.id;
        // Toggle: if popup already open in this row, close it
        const existing = row && row.querySelector('.session-row-actions');
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
    sessionNewBtn.addEventListener('click', () => {
      closeMenu();
      abortChatStream();
      app.currentSessionId = generateUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      app.chatMessages.innerHTML = '';
      app.addChatBubble('agent', 'New session. Start typing below.');
      populateSessionSelect(app.currentUserId);
      streamSessionChanged();
      loopSessionChanged();
      loopVisualSessionChanged();
      autoAgentSessionChanged();
    });
  }

  // ── Agent selector change handler ──
  const agentSelect = document.getElementById('agent-select');
  if (agentSelect) {
    agentSelect.addEventListener('change', () => {
      const newAgentId = agentSelect.value;
      if (!newAgentId || newAgentId === app.currentAgentId) return;
      abortChatStream();
      app.currentAgentId = newAgentId;
      localStorage.setItem('selectedAgentId', newAgentId);
      // Sessions are bound to agents — start a fresh session
      app.currentSessionId = generateUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      app.chatMessages.innerHTML = '';
      app.addChatBubble('agent', 'Switched agent. New session started.');
      populateSessionSelect(app.currentUserId);
      streamSessionChanged();
      loopSessionChanged();
      loopVisualSessionChanged();
      autoAgentSessionChanged();
    });
  }

  populateUserSelect().then(function () {
    if (app.currentUserId) populateAgentSelect(app.currentUserId);
    if (app.currentSessionId) {
      loadSessionChat(app.currentSessionId);
    }
  });
}
