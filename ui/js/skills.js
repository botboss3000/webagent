'use strict';

// Active on-demand skill chips below the chat box.
//
// When the agent calls `load_skill`, that skill's full instructions enter the
// conversation and stay active until the user removes them. This row shows one
// chip per active skill; the × deactivates it via /api/v1/chat/skills/deactivate
// (which drops the skill's body from the agent's context on the next turn).
//
// Refreshed on session switch and after each agent reply finalizes (that's when
// a freshly-loaded skill becomes visible). The row hides itself when empty.

import { app } from './state.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

let _row = null;
let _inFlight = false;

function _agentId() {
  try { return localStorage.getItem('selectedAgentId') || ''; } catch (_) { return ''; }
}

function _hide() {
  if (_row) { _row.innerHTML = ''; _row.hidden = true; }
}

function _render(active, catalog) {
  if (!_row) return;
  _row.innerHTML = '';
  if (!active || !active.length) { _row.hidden = true; return; }
  const descByName = {};
  (catalog || []).forEach(s => { descByName[s.name] = s.description || ''; });

  const label = document.createElement('span');
  label.className = 'chat-skills-label';
  label.textContent = 'Skills active:';
  _row.appendChild(label);

  for (const name of active) {
    const chip = document.createElement('span');
    chip.className = 'chat-skill-chip';
    chip.title = descByName[name] || `Skill "${name}" is loaded in this conversation`;

    const txt = document.createElement('span');
    txt.className = 'chat-skill-chip-name';
    txt.textContent = name;
    chip.appendChild(txt);

    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'chat-skill-chip-x';
    x.setAttribute('aria-label', `Deactivate skill ${name}`);
    x.title = 'Deactivate — remove this skill from the conversation';
    x.textContent = '×';
    x.addEventListener('click', () => _deactivate(name, x));
    chip.appendChild(x);

    _row.appendChild(chip);
  }
  _row.hidden = false;
}

async function _deactivate(name, btn) {
  if (!app.currentSessionId || !app.currentUserId) return;
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const resp = await fetch(apiPath('/api/v1/chat/skills/deactivate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        user_id: app.currentUserId,
        session_id: app.currentSessionId,
        name,
      }),
    });
    if (resp.ok) {
      // Re-fetch so descriptions stay in sync, then re-render.
      await refreshActiveSkills();
    } else if (btn) {
      btn.disabled = false; btn.textContent = '×';
    }
  } catch (_) {
    if (btn) { btn.disabled = false; btn.textContent = '×'; }
  }
}

export async function refreshActiveSkills() {
  if (!_row) return;
  if (_inFlight) return;
  if (!app.currentUserId || !app.currentSessionId) { _hide(); return; }
  _inFlight = true;
  try {
    const params = new URLSearchParams({
      user_id: app.currentUserId,
      session_id: app.currentSessionId,
    });
    const aid = _agentId();
    if (aid) params.set('agent_id', aid);
    const resp = await fetch(apiPath('/api/v1/chat/skills?') + params.toString(), {
      headers: { ...authHeaders() },
    });
    if (!resp.ok) { _hide(); return; }
    const data = await resp.json().catch(() => ({}));
    _render(data.active || [], data.skills || []);
  } catch (_) {
    _hide();
  } finally {
    _inFlight = false;
  }
}

export function initSkills() {
  _row = document.getElementById('chat-skills-row');
  if (!_row) return;
  // Expose so chat.js can refresh on session switch + after a reply finalizes.
  app.refreshActiveSkills = refreshActiveSkills;
  refreshActiveSkills();
}
