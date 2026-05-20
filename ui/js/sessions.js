'use strict';

import { app } from './state.js';
import { loopSessionChanged } from './loop.js';
import { loopVisualSessionChanged } from './loop-logic.js';
import { autoAgentSessionChanged } from './autoagent.js';
import { streamSessionChanged } from './stream.js';
import { apiPath } from './config.js';
import { icon } from './icons.js';

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
    if (!found && sel.options.length) sel.options[0].selected = true;

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
  // Update user ID display in header
  const topUserIdVal = document.getElementById('top-user-id-val');
  if (topUserIdVal) {
    topUserIdVal.textContent = app.currentUserId ? (app.currentUserId.length > 15 ? app.currentUserId.slice(0, 15) + '...' : app.currentUserId) : 'None';
    topUserIdVal.title = app.currentUserId || '';
  }
  // Update dropdown header label
  const dropdownUserLabel = document.getElementById('dropdown-user-label');
  if (dropdownUserLabel) {
    const uid = app.currentUserId || 'Unknown';
    dropdownUserLabel.textContent = uid;
    dropdownUserLabel.title = uid;
  }
  // Populate session select for current user
  if (app.currentUserId) {
    // Preserve current dropdown state (open/focused)
    const activeElement = document.activeElement;
    const isSelectActive = activeElement && activeElement.id === 'session-select';
    if (!isSelectActive) {
      populateSessionSelect(app.currentUserId);
    }
  }
}

export async function populateSessionSelect(userId) {
  if (!userId) {
    document.getElementById('session-select').innerHTML = '<option value="">—</option>';
    return;
  }
  try {
    const token = localStorage.getItem('auth_token');
    const res = await fetch(
      apiPath(`/api/v1/db/sessions?db=local.db&user_id=${encodeURIComponent(userId)}${token ? '&token=' + encodeURIComponent(token) : ''}`),
    );
    const data = await res.json();
    const sel = document.getElementById('session-select');
    sel.innerHTML = '';
    let found = false;
    for (const s of data.sessions || []) {
      const opt = document.createElement('option');
      opt.value = s.id;
      const label = s.title || s.id.slice(0, 12);
      opt.textContent = label.length > 20 ? label.slice(0, 20) + '...' : label;
      opt.title = s.id;
      if (s.id === app.currentSessionId) {
        opt.selected = true;
        found = true;
      }
      sel.appendChild(opt);
    }
    // If current session isn't in DB yet (new session before first msg),
    // add it as a temporary selected option so dropdown doesn't jump to old session
    if (!found && app.currentSessionId) {
      const opt = document.createElement('option');
      opt.value = app.currentSessionId;
      opt.textContent = app.currentSessionId.slice(0, 12);
      opt.selected = true;
      sel.appendChild(opt);
    }
    const newSessOpt = document.createElement('option');
    newSessOpt.value = '__new_session__';
    newSessOpt.textContent = '+ New Session...';
    sel.appendChild(newSessOpt);
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

  // ── Update dropdown header with user id ──
  const dropdownUserLabel = document.getElementById('dropdown-user-label');
  if (dropdownUserLabel) {
    const uid = app.currentUserId || 'Unknown';
    dropdownUserLabel.textContent = uid;
    dropdownUserLabel.title = uid;
  }

  // Auto-poll sessions dropdown every 1s
  setInterval(() => {
    if (app.currentUserId) {
      // Don't auto-refresh while the select is focused/open to avoid layout thrashing
      const activeElement = document.activeElement;
      if (!activeElement || activeElement.id !== 'session-select') {
        populateSessionSelect(app.currentUserId);
      }
    }
  }, 1000);

  // ── Sign-out button in header ──
  const signoutBtn = document.getElementById('btn-signout-header');
  if (signoutBtn) {
    signoutBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      localStorage.removeItem('auth_token');
      localStorage.removeItem('auth_username');
      localStorage.removeItem('auth_user_id');
      localStorage.removeItem('auth_display_name');
      localStorage.removeItem('remember_token');
      localStorage.removeItem('terminalUserId');
      window.location.reload();
    });
  }

  const sessionSelect = document.getElementById('session-select');

  // Reset delete confirm mode when user changes selection
  let deletePending = null;
  const deleteBtn = document.getElementById('session-delete');
  function resetDeleteConfirm() {
    if (deletePending !== null) {
      deletePending = null;
      deleteBtn.innerHTML = icon('trash-2', { size: '14px' });
      deleteBtn.style.color = '#565f89';
      const opt = sessionSelect.querySelector('option[value="__confirm_delete__"]');
      if (opt) opt.remove();
    }
  }

  sessionSelect.addEventListener('change', (e) => {
    resetDeleteConfirm();
    const sid = e.target.value;
    if (!sid || sid === '__new_session__') {
      app.currentSessionId = generateUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      // Add temp option so dropdown shows the new session ID
      const opt = document.createElement('option');
      opt.value = app.currentSessionId;
      opt.textContent = app.currentSessionId.slice(0, 12);
      opt.selected = true;
      sessionSelect.appendChild(opt);
      app.chatMessages.innerHTML = '';
      app.addChatBubble('agent', 'New session. Start typing below.');
    } else {
      app.currentSessionId = sid;
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      loadSessionChat(sid);
    }
    streamSessionChanged();
    loopSessionChanged();
    loopVisualSessionChanged();
    autoAgentSessionChanged();
    // WS is per-user — no reconnect needed on session switch
  });

  // ── Agent selector change handler ──
  const agentSelect = document.getElementById('agent-select');
  if (agentSelect) {
    agentSelect.addEventListener('change', () => {
      const newAgentId = agentSelect.value;
      if (!newAgentId || newAgentId === app.currentAgentId) return;
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

  // Session delete button — two-step: first click warns, second confirms
  deleteBtn.addEventListener('click', async () => {
    let sid = sessionSelect.value;
    // If confirm option is selected, use the pending session ID
    if (sid === '__confirm_delete__') sid = deletePending;
    if (!sid || sid === '__new_session__') return;

    if (deletePending !== sid) {
      // First click: enter confirm mode
      deletePending = sid;
      deleteBtn.innerHTML = icon('ban', { size: '14px' });
      deleteBtn.style.color = '#f7768e';
      // Temporarily add "delete msgs?" option selected in dropdown
      const confirmOpt = document.createElement('option');
      confirmOpt.value = '__confirm_delete__';
      confirmOpt.textContent = 'delete msgs?';
      confirmOpt.selected = true;
      confirmOpt.style.color = '#f7768e';
      sessionSelect.appendChild(confirmOpt);
      setTimeout(() => {
        if (deletePending === sid) {
          // Reset if user doesn't click within 5s
          deletePending = null;
          deleteBtn.innerHTML = icon('trash-2', { size: '14px' });
          deleteBtn.style.color = '#565f89';
          const opt = sessionSelect.querySelector('option[value="__confirm_delete__"]');
          if (opt) opt.remove();
          // Re-select the original session
          for (const o of sessionSelect.options) {
            if (o.value === sid) { o.selected = true; break; }
          }
        }
      }, 5000);
      return;
    }

    // Second click: actually delete
    deletePending = null;
    deleteBtn.innerHTML = icon('trash-2', { size: '14px' });
    deleteBtn.style.color = '#565f89';
    const confirmOpt = sessionSelect.querySelector('option[value="__confirm_delete__"]');
    if (confirmOpt) confirmOpt.remove();

    try {
      const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=local.db'), { method: 'DELETE' });
      if (res.ok) {
        if (sid === app.currentSessionId) {
          app.currentSessionId = generateUUID();
          localStorage.setItem('terminalSessionId', app.currentSessionId);
          app.chatMessages.innerHTML = '';
          app.addChatBubble('agent', 'Session deleted. New session created.');
        }
        populateSessionSelect(app.currentUserId);
      }
    } catch (e) {
      console.warn('Failed to delete session:', e);
    }
  });
}
