'use strict';

// Skill-selector panel in the chat footer + counter badge.
//
// Provides a footer button (#chat-skills-btn) with a counter badge showing the
// number of active skills. Opens a floating panel (right-aligned) listing ALL
// selectable skills — active (highlighted green) and idle (greyed out, for
// discoverable skills the agent knows about but hasn't loaded yet). Clicking
// idle ones activates them; clicking active ones deactivates them. Uses
// display_name for human-friendly labels (e.g. "Codebase Admin" instead of
// "codebase_admin_src7").
//
// Refreshed on session switch and after each agent reply finalizes.

import { app } from './state.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

// ── State ──
let _inFlight = false;
let _cachedCatalog = [];  // full catalog: { name, display_name, description, mode, active }
let _cachedActive = [];   // active names (raw handles)

// Panel state
let _pickerEl = null;     // the floating .chat-skill-picker container
let _pickerList = null;   // .csp-list inside the picker
let _badgeEl = null;      // #chat-skills-badge
let _btnEl = null;        // #chat-skills-btn

// ── Helpers ──
function _agentId() {
  try { return localStorage.getItem('selectedAgentId') || ''; } catch (_) { return ''; }
}

// ── Badge (counter on the footer button) ──
function _updateBadge(active) {
  if (!_badgeEl) return;
  const count = (active && active.length) || 0;
  _badgeEl.textContent = count;
  _badgeEl.classList.toggle('visible', count > 0);
}

// ── Skill-selector panel (floating, right-aligned) ──
function _buildPicker() {
  if (_pickerEl) return;
  const picker = document.createElement('div');
  picker.className = 'chat-skill-picker';

  // Header row
  const header = document.createElement('div');
  header.className = 'csp-header';
  header.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="7" height="7" x="14" y="3" rx="1"/><path d="M10 21V8a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5a1 1 0 0 0-1-1H3"/></svg>Skills';
  picker.appendChild(header);

  const list = document.createElement('div');
  list.className = 'csp-list';
  picker.appendChild(list);
  document.body.appendChild(picker);
  _pickerEl = picker;
  _pickerList = list;

  // Close on click outside
  document.addEventListener('click', (e) => {
    if (_pickerEl && _pickerEl.style.display !== 'none'
        && !_pickerEl.contains(e.target)
        && e.target !== _btnEl
        && !(_btnEl && _btnEl.contains(e.target))) {
      _pickerEl.style.display = 'none';
    }
  });

  // Close on Escape
  picker.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { _pickerEl.style.display = 'none'; }
  });
}

function _togglePicker() {
  if (!_pickerEl) _buildPicker();
  if (_pickerEl.style.display !== 'none') {
    _pickerEl.style.display = 'none';
    return;
  }

  // Anchor to the RIGHT edge of the chat footer row
  const footer = document.getElementById('chat-footer-row')
    || document.getElementById('chat-input-row');
  if (!footer) return;
  const rect = footer.getBoundingClientRect();

  // Pin bottom edge just above the footer, right-aligned
  _pickerEl.style.top = 'auto';
  _pickerEl.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
  _pickerEl.style.left = 'auto';
  _pickerEl.style.right = Math.max(4, Math.min(window.innerWidth - rect.right, window.innerWidth - 240)) + 'px';

  // Show after microtask so the click handler doesn't immediately close
  setTimeout(() => {
    _pickerEl.style.display = 'block';
  }, 0);

  // Render from current cache
  _renderPickerList();
}

function _renderPickerList() {
  if (!_pickerList) return;
  const catalog = _cachedCatalog || [];
  _pickerList.innerHTML = '';

  if (!catalog.length) {
    _pickerList.innerHTML = '<div class="csp-empty">No skills available</div>';
    return;
  }

  catalog.forEach(s => {
    const item = document.createElement('div');
    const isActive = s.active;
    item.className = 'csp-item' + (isActive ? ' csp-active' : ' csp-idle');
    item.dataset.skillName = s.name;

    // Indicator dot: filled green when active, empty grey circle when idle
    const ind = document.createElement('span');
    ind.className = 'csp-item-indicator';
    ind.textContent = isActive ? '✓' : '';
    item.appendChild(ind);

    const nameSpan = document.createElement('span');
    nameSpan.className = 'csp-item-name';
    nameSpan.textContent = s.display_name || s.name;
    item.appendChild(nameSpan);

    item.title = s.description || (isActive ? 'Active — click to deactivate' : 'Inactive — click to activate');
    item.addEventListener('click', () => _onToggleSkill(s.name, isActive, item));
    _pickerList.appendChild(item);
  });
}

async function _onToggleSkill(name, currentlyActive, itemEl) {
  if (!app.currentSessionId || !app.currentUserId) return;
  if (itemEl) itemEl.style.opacity = '0.5';

  try {
    if (currentlyActive) {
      // Deactivate
      await fetch(apiPath('/api/v1/chat/skills/deactivate'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          user_id: app.currentUserId,
          session_id: app.currentSessionId,
          name,
        }),
      });
    } else {
      // Activate
      await fetch(apiPath('/api/v1/chat/skills/activate'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          user_id: app.currentUserId,
          session_id: app.currentSessionId,
          name,
        }),
      });
    }
    // Re-fetch everything
    await refreshActiveSkills();
    // Re-render the picker with updated state
    _renderPickerList();
  } catch (_) {
    if (itemEl) itemEl.style.opacity = '';
  }
}

// ── Public refresh ──
export async function refreshActiveSkills() {
  if (_inFlight) return;
  if (!app.currentUserId || !app.currentSessionId) { _updateBadge([]); return; }
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
    if (!resp.ok) { _updateBadge([]); return; }
    const data = await resp.json().catch(() => ({}));
    _cachedActive = data.active || [];
    _cachedCatalog = data.skills || [];
    _updateBadge(_cachedActive);
    // If the picker is open, re-render it with the fresh data
    if (_pickerEl && _pickerEl.style.display !== 'none') {
      _renderPickerList();
    }
  } catch (_) {
    _updateBadge([]);
  } finally {
    _inFlight = false;
  }
}

// ── Init ──
export function initSkills() {
  // Wire the footer button and badge
  _btnEl = document.getElementById('chat-skills-btn');
  _badgeEl = document.getElementById('chat-skills-badge');
  if (_btnEl) {
    _btnEl.addEventListener('click', (e) => {
      e.stopPropagation();
      _togglePicker();
    });
  }

  // Expose so chat.js can refresh on session switch + after a reply finalizes.
  app.refreshActiveSkills = refreshActiveSkills;
  refreshActiveSkills();
}