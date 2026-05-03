'use strict';

import { app } from './state.js';
import { connectAgent } from './agentWs.js';

export function generateUUID() {
  return crypto.randomUUID();
}

export async function populateUserSelect() {
  try {
    const res = await fetch('/api/v1/db/users?db=local_webagent.db');
    const data = await res.json();
    const sel = document.getElementById('user-select');
    sel.innerHTML = '<option value="">— select user —</option>';
    for (const uid of data.users || []) {
      const opt = document.createElement('option');
      opt.value = uid;
      opt.textContent = uid.slice(0, 12) + '...';
      opt.title = uid;
      if (uid === app.currentUserId) opt.selected = true;
      sel.appendChild(opt);
    }
    const newUserOpt = document.createElement('option');
    newUserOpt.value = '__new_user__';
    newUserOpt.textContent = '+ New User...';
    sel.appendChild(newUserOpt);
    if (app.currentUserId) populateSessionSelect(app.currentUserId);
  } catch (e) {
    console.warn('Failed to load users:', e);
  }
}

export async function populateSessionSelect(userId) {
  if (!userId) {
    document.getElementById('session-select').innerHTML = '<option value="">—</option>';
    return;
  }
  try {
    const res = await fetch(
      `/api/v1/db/sessions?db=local_webagent.db&user_id=${encodeURIComponent(userId)}`,
    );
    const data = await res.json();
    const sel = document.getElementById('session-select');
    sel.innerHTML = '<option value="">— select session —</option>';
    for (const s of data.sessions || []) {
      const opt = document.createElement('option');
      opt.value = s.id;
      const label = s.title || s.id.slice(0, 12);
      opt.textContent = label.length > 20 ? label.slice(0, 20) + '...' : label;
      opt.title = s.id;
      if (s.id === app.currentSessionId) opt.selected = true;
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
      `/api/v1/db/session-messages?db=local_webagent.db&session_id=${encodeURIComponent(sessionId)}`,
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
}

export function initSessions() {
  document.getElementById('user-select').addEventListener('change', (e) => {
    if (e.target.value === '__new_user__') {
      const name = prompt('Enter new user ID:', '');
      if (!name || !name.trim()) {
        e.target.value = app.currentUserId || '';
        return;
      }
      app.currentUserId = name.trim();
    } else {
      app.currentUserId = e.target.value || '';
    }
    localStorage.setItem('terminalUserId', app.currentUserId);
    app.currentSessionId = generateUUID();
    sessionStorage.setItem('terminalSessionId', app.currentSessionId);
    populateSessionSelect(app.currentUserId);
    connectAgent();
  });

  document.getElementById('session-select').addEventListener('change', (e) => {
    const sid = e.target.value;
    if (!sid || sid === '__new_session__') {
      app.currentSessionId = generateUUID();
      sessionStorage.setItem('terminalSessionId', app.currentSessionId);
      app.chatMessages.innerHTML = '';
      app.addChatBubble('agent', 'New session. Start typing below.');
    } else {
      app.currentSessionId = sid;
      sessionStorage.setItem('terminalSessionId', app.currentSessionId);
      loadSessionChat(sid);
    }
    connectAgent();
  });

  populateUserSelect().then(function () {
    if (app.currentSessionId) {
      loadSessionChat(app.currentSessionId);
    }
  });
}
