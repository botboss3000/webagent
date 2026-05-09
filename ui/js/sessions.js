'use strict';

import { app } from './state.js';
import { connectAgent } from './agentWs.js';
import { loopSessionChanged } from './loop.js';
import { loopVisualSessionChanged } from './loop-visual.js';
import { streamSessionChanged } from './stream.js';
import { apiPath } from './config.js';

export function generateUUID() {
  return crypto.randomUUID();
}

export async function populateUserSelect() {
  // Update user ID display in header
  const topUserIdVal = document.getElementById('top-user-id-val');
  if (topUserIdVal) {
    topUserIdVal.textContent = app.currentUserId ? (app.currentUserId.length > 15 ? app.currentUserId.slice(0, 15) + '...' : app.currentUserId) : 'None';
    topUserIdVal.title = app.currentUserId || '';
  }
  // Populate session select for current user
  if (app.currentUserId) populateSessionSelect(app.currentUserId);
}

export async function populateSessionSelect(userId) {
  if (!userId) {
    document.getElementById('session-select').innerHTML = '<option value="">—</option>';
    return;
  }
  try {
    const res = await fetch(
      apiPath(`/api/v1/db/sessions?db=local.db&user_id=${encodeURIComponent(userId)}`),
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
    const res = await fetch(
      apiPath(`/api/v1/db/session-messages?db=local.db&session_id=${encodeURIComponent(sessionId)}`),
    );
    const data = await res.json();
    app.chatMessages.innerHTML = '';
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
        app.addChatBubble('agent', msg.content);
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
  // ── Sign-out button in header ──
  const signoutBtn = document.getElementById('btn-signout-header');
  if (signoutBtn) {
    signoutBtn.addEventListener('click', () => {
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
      deleteBtn.textContent = '🗑️';
      deleteBtn.style.color = '#565f89';
      const opt = sessionSelect.querySelector('option[value="__confirm_delete__"]');
      if (opt) opt.remove();
    }
  }

  sessionSelect.addEventListener('change', (e) => {
    resetDeleteConfirm();
    const sid = e.target.value;
    console.log('[session] change sid=' + sid + ' currentSessionId=' + app.currentSessionId);
    if (!sid || sid === '__new_session__') {
      app.currentSessionId = generateUUID();
      console.log('[session] new UUID=' + app.currentSessionId);
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
    connectAgent();
  });

  populateUserSelect().then(function () {
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
      deleteBtn.textContent = '⛔';
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
          deleteBtn.textContent = '🗑️';
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
    deleteBtn.textContent = '🗑️';
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
          connectAgent();
        }
        populateSessionSelect(app.currentUserId);
      }
    } catch (e) {
      console.warn('Failed to delete session:', e);
    }
  });
}
