'use strict';

// Suggested-reply chips above the chat pill.
//
// Calls the SILENT /api/v1/chat/suggestions endpoint (backed by the
// user-impersonator system agent) and renders the returned messages as
// tappable chips. Tapping a chip drops its text into the pill (editable) and
// focuses it. None of this touches the visible chat, the agent loop, or the WS.
//
// Refresh timing follows the engine's mode (read once at init, refreshed by the
// endpoint response):
//   - "off"        → never fetch, never show chips
//   - "on"         → refresh after each agent reply + on conversation open
//   - "scheduler"  → "on" PLUS refresh after the user sits idle

import { app } from './state.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

let _mode = 'on';
let _idleSeconds = 25;
let _idleTimer = null;
let _inFlight = false;
let _row = null;

function _canChat() {
  // Mirror chat.js gating: only suggest when the visitor can actually chat.
  try {
    const token = localStorage.getItem('auth_token');
    const mode = (window.__accessMode || '');
    if (mode === 'public_anonymous') return true;
    return !!token;
  } catch (_) { return true; }
}

function _clearChips() {
  if (_row) _row.innerHTML = '';
}

function _renderChips(items) {
  if (!_row) return;
  _row.innerHTML = '';
  if (!items || !items.length) return;
  // Don't clobber text the user is actively composing.
  if (app.chatInput && app.chatInput.value.trim()) return;
  for (const text of items) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chat-suggest-chip';
    chip.textContent = text;
    chip.title = text;
    chip.addEventListener('click', () => _applyChip(text));
    _row.appendChild(chip);
  }
}

function _applyChip(text) {
  if (!app.chatInput) return;
  app.chatInput.value = text;
  // Trigger chat.js listeners (send-button enable, auto-resize, draft save).
  app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
  app.chatInput.focus();
  _clearChips();
}

async function refreshSuggestions() {
  if (_mode === 'off') { _clearChips(); return; }
  if (_inFlight) return;
  if (!app.currentUserId) return;
  if (!_canChat()) { _clearChips(); return; }
  // Never compete with an in-progress turn, or with text the user is typing.
  if (app.isProcessing) return;
  if (app.chatInput && app.chatInput.value.trim()) return;
  _inFlight = true;
  try {
    const resp = await fetch(apiPath('/api/v1/chat/suggestions'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        user_id: app.currentUserId,
        session_id: app.currentSessionId || null,
      }),
    });
    if (!resp.ok) { _clearChips(); return; }
    const data = await resp.json().catch(() => ({}));
    // Keep mode/idle in sync with the server in case it changed in the config panel.
    if (data.mode) _mode = data.mode;
    if (typeof data.idle_seconds === 'number') _idleSeconds = data.idle_seconds;
    if (_mode === 'off') { _clearChips(); return; }
    _renderChips(data.suggestions || []);
  } catch (_) {
    _clearChips();
  } finally {
    _inFlight = false;
  }
}

// ── Idle handling (scheduler mode) ──────────────────────────────────────────
function _resetIdleTimer() {
  if (_idleTimer) clearTimeout(_idleTimer);
  if (_mode !== 'scheduler') return;
  _idleTimer = setTimeout(() => {
    // Only refresh when the box is empty and nothing is running.
    if (!app.isProcessing && !(app.chatInput && app.chatInput.value.trim())) {
      refreshSuggestions();
    }
  }, Math.max(5, _idleSeconds) * 1000);
}

function _wireIdleResetters() {
  // Any sign of activity resets the idle countdown.
  ['keydown', 'mousedown', 'touchstart'].forEach(evt => {
    document.addEventListener(evt, _resetIdleTimer, { passive: true });
  });
  // Typing in the pill clears stale chips immediately.
  if (app.chatInput) {
    app.chatInput.addEventListener('input', () => {
      if (app.chatInput.value.trim()) _clearChips();
    });
  }
}

export async function initSuggestions() {
  _row = document.getElementById('chat-suggest-row');
  if (!_row) return;

  // Expose so chat.js can refresh after a reply finalizes, and clear on send.
  app.refreshSuggestions = refreshSuggestions;
  app.clearSuggestions = _clearChips;

  // Load current mode/idle from the server.
  try {
    const resp = await fetch(apiPath('/api/v1/chat/suggestions/config'), { headers: { ...authHeaders() } });
    if (resp.ok) {
      const cfg = await resp.json();
      if (cfg.mode) _mode = cfg.mode;
      if (typeof cfg.idle_seconds === 'number') _idleSeconds = cfg.idle_seconds;
    }
  } catch (_) { /* keep defaults */ }

  if (_mode === 'off') return;

  _wireIdleResetters();
  _resetIdleTimer();
  // First suggestion pass once the user/session are known.
  refreshSuggestions();
}
