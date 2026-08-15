'use strict';

// Session entry point — registerSessionApi() sets app.* session methods;
// initSessions() orchestrates the chat header: mounts the session-dropdown
// controller (centralized in ui/chat/elements/session-dropdown/), wires the
// agent-name dropdown + agent picker, new-session flows, header buttons, the
// boot sequence and the polling loops. All session-dropdown menu logic lives
// in ui/chat/elements/session-dropdown/ (controller.js + list.js).
// Module map for this folder: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import { isAdmin } from '../../shared/js/left-login.js';
import { copyText } from '../../shared/js/clipboard.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../shared/js/delete-control.js';
import { populateUserSelect, initUserPanel } from '../../shared/js/user-panel.js';
import { loopSessionChanged } from '../../main-panel/agents/agent-loop/js/loop.js';
import { loopVisualSessionChanged } from '../../main-panel/agents/agent-loop/js/loop-logic.js';
import { genuiSessionChanged } from '../../main-panel/genui/js/genui.js';
import { chatActivitySessionChanged } from '../../shared/js/chat-activity.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { addChatBubble } from './chat-bubble.js';
import { _loadSessionFocus, _captureSessionFocus } from './chat-message-cache.js';
import { _teardownVirtualScroll } from './chat-virtual-scroll.js';
import { loadSessionChat, refreshTranscript, _syncTerminalChat } from './session-load.js';
import {
  _sessionsCache,
  populateSessionSelect,
  _fetchRelatedSessions,
  applySessionTitle,
  _startCompletedTimeTick,
} from './session-list.js';
import {
  _agentsCache,
  populateAgentSelect,
  _refreshAgentAbilities,
  _setAgentTriggerLabel,
  _renderAgentRows,
  _renderAgentDropdown,
  _loadLastSessionMap,
  _lastSessionPerAgent,
  _saveLastSessionMap,
  ensureWebagentAgent,
} from './session-agent.js';
import {
  switchToSession,
  deleteSession,
} from './session-core.js';
import { initSessionNotification, checkSessionCompletions } from './session-notification.js';
import { _initPinSwipeNavigation } from './session-swipe.js';
import { agentChatMsg } from '../../shared/js/app-prompts.js';
import { reapplyChatControlsConfig } from '../../chat-controls/chat-controls-config.js';
import { storageAdapter } from './storage/storage-adapter.js';
import { mountSessionDropdown } from '../elements/session-dropdown/controller.js';

// ── registerSessionApi ─────────────────────────────────────────────────────

export function registerSessionApi() {
  app.populateSessionSelect = populateSessionSelect;
  app.populateAgentSelect = populateAgentSelect;
  app.loadSessionChat = loadSessionChat;
  app.applySessionTitle = applySessionTitle;
  app.reloadCurrentSession = () => {
    if (app.currentSessionId) {
      // Same-session safety: clear the loaded-session marker so loadSessionChat
      // clears existing bubbles before re-rendering (the visibility-change
      // handler calls this for the already-open session).
      app._lastLoadedSessionId = null;
      loadSessionChat(app.currentSessionId);
    }
  };

  _loadSessionFocus();
  window.addEventListener('pagehide', () => {
    if (app._lastLoadedSessionId) _captureSessionFocus(app._lastLoadedSessionId);
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden' && app._lastLoadedSessionId) {
      _captureSessionFocus(app._lastLoadedSessionId);
    }
  });
}

// ── initSessions ───────────────────────────────────────────────────────────

export function initSessions() {
  initUserPanel();

  // ── Session dropdown controller ──
  // Centralized in ui/chat/elements/session-dropdown/. Mounted here for the
  // static chat-side-panel.html markup (the chat-controls loader reuses
  // existing DOM and does NOT call the element's init()); the loader's
  // dynamic path mounts it via init() instead. Idempotent either way.
  // The controller exposes { open, close, isOpen, destroy } on
  // root._sessionDropdownController so the new-session flows below can
  // close the menu without importing internals.
  const _sessionDropdownEl = document.getElementById('session-dropdown');
  mountSessionDropdown(_sessionDropdownEl);

  function _closeSessionDropdown() {
    const live = document.getElementById('session-dropdown');
    const handle = live && live._sessionDropdownController;
    if (handle) handle.close();
  }

  // ── Agent-name dropdown (click agent name to switch agents) ──────────────
  const agentNameEl = document.getElementById('chat-header-agent-name');
  const agentMenu = document.getElementById('agent-dropdown-menu');

  function openAgentMenu() {
    if (!agentMenu) return;
    _renderAgentDropdown();
    // Position the menu fixed below the trigger so it escapes the
    // glass-chip stacking context (backdrop-filter traps absolute children).
    const triggerRect = agentTrigger.getBoundingClientRect();
    const panel = document.getElementById('chat-panel');
    const panelRect = panel ? panel.getBoundingClientRect() : { left: 0, width: window.innerWidth };
    agentMenu.style.position = 'fixed';
    agentMenu.style.top = (triggerRect.bottom + 4) + 'px';
    agentMenu.style.left = (panelRect.left + panelRect.width / 2) + 'px';
    agentMenu.style.transform = 'translateX(-50%)';
    agentMenu.style.minWidth = Math.max(240, triggerRect.width) + 'px';
    agentMenu.hidden = false;
  }

  function closeAgentMenu() {
    if (!agentMenu) return;
    agentMenu.style.transform = '';
    agentMenu.hidden = true;
  }

  // Listen on the parent wrapper (.chat-header-name-row) so the entire glass
  // chip area is clickable — the inner span's padding is zero once the header
  // config system positions it inside a zone.
  const agentNameRow = agentNameEl ? agentNameEl.parentElement : null;
  const agentTrigger = agentNameRow || agentNameEl;

  if (agentTrigger && agentMenu) {
    agentTrigger.addEventListener('click', (e) => {
      e.stopPropagation();
      if (agentMenu.hidden) openAgentMenu(); else closeAgentMenu();
    });
    agentTrigger.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (agentMenu.hidden) openAgentMenu(); else closeAgentMenu();
      }
    });
    // Click inside the menu — switch agent (and its last session, or start new)
    agentMenu.addEventListener('click', (e) => {
      e.stopPropagation();
      const row = e.target.closest('.agent-dropdown-item');
      if (!row) return;
      const agentId = row.dataset.agentId;
      if (!agentId || agentId === app.currentAgentId) { closeAgentMenu(); return; }
      closeAgentMenu();

      // Find the last session for this agent, or start a new one
      const lastSid = _lastSessionPerAgent.get(agentId);
      const existing = lastSid && _sessionsCache.find(s => s.id === lastSid && s.agent_id === agentId);
      if (existing) {
        switchToSession(existing.id);
      } else {
        _startNewSession(agentId);
      }
      // Rebuild header for the new agent's per-agent chat_ui
      try { reapplyChatControlsConfig(); } catch (_) {}
    });
  }

  // Close agent dropdown on outside click / focus
  document.addEventListener('click', (e) => {
    if (agentTrigger && agentMenu && !agentTrigger.contains(e.target) && !agentMenu.contains(e.target)) {
      closeAgentMenu();
    }
  });
  document.addEventListener('focusin', (e) => {
    if (agentTrigger && agentMenu && !agentTrigger.contains(e.target) && !agentMenu.contains(e.target)) {
      closeAgentMenu();
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && agentMenu && !agentMenu.hidden) closeAgentMenu();
  });

  // ── New session ──
  function _startNewSession(agentId) {
    app.abortChatStream?.();
    if (agentId && agentId !== app.currentAgentId) {
      app.currentAgentId = agentId;
      _refreshAgentAbilities(agentId);
      try { localStorage.setItem('selectedAgentId', agentId); } catch (_) {}
      // Rebuild header for the new agent's per-agent chat_ui
      try { reapplyChatControlsConfig(); } catch (_) {}
    }
    app.currentSessionId = randomUUID();
    localStorage.setItem('terminalSessionId', app.currentSessionId);
    // Re-sync the execution-mode pill for this brand-new session (per-session key,
    // defaults to Ask). Without this, a fresh chat keeps the previous session's
    // in-memory mode (e.g. a leftover Auto) while the pill shows the wrong label —
    // so the message would silently send a different mode than the pill displays.
    if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
    if (typeof app.reloadThinking === 'function') app.reloadThinking();
    if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
    if (typeof app.reloadFooterExpanded === 'function') app.reloadFooterExpanded();
    _teardownVirtualScroll();
    app.chatMessages.innerHTML = '';
    // Empty-state placeholder — drops away the moment the user sends (chat-send.js).
    // The admin Agent/Session diagnostic line is intentionally NOT shown here: a
    // brand-new session has no DB row yet, so it's surfaced only once the first
    // message is sent (chat-send.js, after the send succeeds).
    app.addChatBubble('agent', agentChatMsg('new_session_bubble'), 'session-placeholder');
    // Refresh the composer pill placeholder for this agent's override.
    if (typeof app.applyChatGate === 'function') { try { app.applyChatGate(); } catch (_) {} }
    populateSessionSelect(app.currentUserId);
    loopSessionChanged();
    loopVisualSessionChanged();
    genuiSessionChanged();
    chatActivitySessionChanged();
    _setAgentTriggerLabel();
    if (typeof app.refreshActiveAbilities === 'function') {
      try { app.refreshActiveAbilities(); } catch (_) { /* best-effort */ }
    }
    if (app.chatInput) {
      // Keep any text already typed in the composer — the user may have drafted
      // it intending to send it in this new session. Re-dispatch input so the
      // send button, auto-resize, footer mode and draft persistence all re-sync
      // with the preserved text (chat-ui.js input listener).
      app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    }
    if (app.focusChatInput) app.focusChatInput();
  }

  // ── Agent picker ──
  let _agentPickerEl = null;
  function _closeAgentPicker() {
    if (_agentPickerEl) { _agentPickerEl.remove(); _agentPickerEl = null; }
    document.removeEventListener('click', _onAgentPickerOutside, true);
    document.removeEventListener('keydown', _onAgentPickerEsc, true);
    window.removeEventListener('resize', _closeAgentPicker);
  }
  function _pickerIsOpen() {
    return !!_agentPickerEl && document.body.contains(_agentPickerEl);
  }
  function _onAgentPickerOutside(e) {
    if (_agentPickerEl && !_agentPickerEl.contains(e.target) && !e.target.closest('#session-new-header-btn')) {
      _closeAgentPicker();
    }
  }
  function _onAgentPickerEsc(e) { if (e.key === 'Escape') _closeAgentPicker(); }

  function _openAgentPicker(anchorEl) {
    _closeAgentPicker();
    const picker = document.createElement('div');
    picker.className = 'agent-dropdown-menu new-session-agent-picker';
    const head = document.createElement('div');
    head.className = 'new-session-picker-head';
    head.textContent = 'New session with\u2026';
    picker.appendChild(head);
    for (const a of _agentsCache) {
      const row = document.createElement('div');
      row.className = 'agent-row-item';
      const name = (a.name || a.id.slice(0, 12)).replace(/</g, '&lt;');
      row.innerHTML = `<span class="agent-row-title">${name}</span>`;
      row.addEventListener('click', (ev) => {
        ev.stopPropagation();
        _closeAgentPicker();
        _startNewSession(a.id);
      });
      picker.appendChild(row);
    }
    document.body.appendChild(picker);
    const r = anchorEl.getBoundingClientRect();
    picker.style.top = Math.round(r.bottom + 6) + 'px';
    const w = picker.offsetWidth || 200;
    let left = r.right - w;
    if (left < 8) left = 8;
    picker.style.left = Math.round(left) + 'px';
    _agentPickerEl = picker;
    setTimeout(() => {
      document.addEventListener('click', _onAgentPickerOutside, true);
      document.addEventListener('keydown', _onAgentPickerEsc, true);
      window.addEventListener('resize', _closeAgentPicker);
    }, 0);
  }

  let _newSessionBusy = false;
  async function _ensureAgentsLoaded() {
    if (window.__agentsSharedData || !app.currentUserId) return;
    try { await populateAgentSelect(app.currentUserId); }
    catch (_) { /* fall through */ }
  }

  async function _onNewSessionSingle(anchorEl) {
    if (_newSessionBusy) return;
    _newSessionBusy = true;
    try {
      _closeSessionDropdown();
      _closeAgentPicker();
      await _ensureAgentsLoaded();
      const targetId = app.currentAgentId || (_agentsCache[0] && _agentsCache[0].id) || null;
      _startNewSession(targetId);
    } catch (err) {
      console.error('New-session click failed:', err);
    } finally {
      _newSessionBusy = false;
    }
  }

  // ── Header delete button ──
  const sessionDelHeader = document.getElementById('session-delete-header');
  if (sessionDelHeader) {
    const _resetHeaderDel = () => {
      clearTimeout(sessionDelHeader._delTimer);
      resetDeleteBtn(sessionDelHeader, { size: '18px', title: 'Delete session' });
    };
    sessionDelHeader.addEventListener('click', (e) => {
      e.stopPropagation();
      const sid = app.currentSessionId;
      if (!sid) return;
      advanceDeleteBtn(sessionDelHeader, {
        size: '18px', spinSize: '18px',
        onArm: () => {
          clearTimeout(sessionDelHeader._delTimer);
          sessionDelHeader._delTimer = setTimeout(_resetHeaderDel, 3000);
        },
        onConfirm: () => {
          clearTimeout(sessionDelHeader._delTimer);
          deleteSession(sid, { retries: 1 }).then(result => {
            _resetHeaderDel();
            if (!result.ok) {
              // Show error in the header trigger label (the session name area).
              _showHeaderLabelError(result.error || 'Delete failed');
            }
          });
        },
      });
    });
  }

  // Show an ephemeral error message in the header dropdown label (the session
  // name shown in the trigger), then revert to the current session's title
  // after ~2.5s.
  function _showHeaderLabelError(msg) {
    const label = document.getElementById('session-dropdown-label');
    if (!label) return;
    const orig = label.dataset._delErrOrig || label.textContent;
    label.dataset._delErrOrig = orig;
    label.textContent = msg;
    // Resolve --danger via getComputedStyle — CSS var() only works in stylesheets.
    const dangerColor = getComputedStyle(document.documentElement).getPropertyValue('--danger').trim();
    if (dangerColor) label.style.color = dangerColor;
    else label.style.color = '#e44'; // fallback if variable isn't defined
    clearTimeout(label._delErrTimer);
    label._delErrTimer = setTimeout(() => {
      const restored = label.dataset._delErrOrig || orig;
      label.textContent = restored;
      label.style.color = '';
      delete label.dataset._delErrOrig;
    }, 2500);
  }

  // ── Header refresh button — reset just the chat transcript (never the page) ──
  const refreshBtn = document.getElementById('chat-refresh-transcript');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (refreshBtn._busy) return;
      refreshBtn._busy = true;
      refreshBtn.classList.add('spinning');
      try {
        await refreshTranscript();
      } catch (err) {
        console.warn('Transcript refresh failed:', err);
      } finally {
        refreshBtn.classList.remove('spinning');
        refreshBtn._busy = false;
      }
    });
  }

  // ── Switch agent ──
  function switchToAgent(aid, opts) {
    opts = opts || {};
    const forceNew = !!opts.forceNewSession;
    const silent   = !!opts.silent;
    if (!aid) return;
    if (aid === app.currentAgentId && !forceNew) return;
    const prevAgentId = app.currentAgentId;
    if (prevAgentId && app.currentSessionId) {
      _lastSessionPerAgent.set(prevAgentId, app.currentSessionId);
      _saveLastSessionMap();
    }
    app.abortChatStream?.();
    app.currentAgentId = aid;
    localStorage.setItem('selectedAgentId', aid);
    // Rebuild header for the new agent's per-agent chat_ui overrides
    try { reapplyChatControlsConfig(); } catch (_) {}
    const lastSid = forceNew ? null : _lastSessionPerAgent.get(aid);
    if (lastSid) {
      app.currentSessionId = lastSid;
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
      loadSessionChat(lastSid);
    } else {
      app.currentSessionId = randomUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      // Re-sync the execution-mode pill for this fresh session (per-session key,
      // defaults to Ask) so it can't inherit the previous session's leftover mode.
      if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
      if (typeof app.reloadThinking === 'function') app.reloadThinking();
      if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
      if (typeof app.reloadFooterExpanded === 'function') app.reloadFooterExpanded();
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
      // Empty-state placeholder — drops away the moment the user sends (chat-send.js).
      // No admin diagnostic line here either: this fresh session isn't in the DB
      // until the first message is sent (surfaced from chat-send.js at that point).
      if (!silent) app.addChatBubble('agent', agentChatMsg('switched_agent_bubble'), 'session-placeholder');
      // This fresh-session branch never calls loadSessionChat, so trigger the
      // terminal-chat sync directly: if this agent runs the terminal_chat engine,
      // mount the live terminal right now (no message needed); otherwise tear down
      // any terminal left over from a previously-selected terminal agent.
      try { _syncTerminalChat(app.currentSessionId); } catch (_) {}
    }
    // Refresh the composer pill placeholder for the newly-selected agent.
    if (typeof app.applyChatGate === 'function') { try { app.applyChatGate(); } catch (_) {} }
    populateSessionSelect(app.currentUserId);
    loopSessionChanged();
    loopVisualSessionChanged();
    genuiSessionChanged();
    chatActivitySessionChanged();
    _renderAgentRows();
    _setAgentTriggerLabel();
  }
  app.switchToAgent = switchToAgent;

  async function startWebagentSession() {
    const id = await ensureWebagentAgent(app.currentUserId);
    switchToAgent(id, { forceNewSession: true, silent: true });
    return id;
  }
  app.startWebagentSession = startWebagentSession;
  app.ensureWebagentAgent = ensureWebagentAgent;

  // ── Header new-session button (lives in the chat panel header now) ──
  const headerNewBtn = document.getElementById('session-new-header-btn');
  if (headerNewBtn) {
    headerNewBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!window.__getChatVisible()) {
        window.__applyChatVisible(true);
      }
      _onNewSessionSingle(headerNewBtn);
    });
    // Middle-click → open a fresh session with the current agent in a new browser
    // tab. (Relocated here from tabs.js when the + button moved into the chat
    // header — tabs.js runs at shell init, before this partial mounts.)
    headerNewBtn.addEventListener('auxclick', (e) => {
      if (e.button !== 1) return;  // middle-click only
      e.preventDefault();
      const base = location.pathname.replace(/\/+$/, '') || '/';
      let agentId = '';
      try { agentId = localStorage.getItem('selectedAgentId') || ''; } catch (_) {}
      window.open(base + '?agent=' + encodeURIComponent(agentId) + '&new=1', '_blank');
    });
  }

  // ── New session with the DEFAULT agent, no picker ──
  // Used by the main-header Chat button's LONG-PRESS (index.html). Goes straight
  // to a fresh session with the user's default agent (top of the sorted cache,
  // falling back to the currently-selected agent) — never the multi-agent picker.
  async function startNewSessionDefault() {
    if (_newSessionBusy) return;
    _newSessionBusy = true;
    try {
      _closeSessionDropdown();
      _closeAgentPicker();
      await _ensureAgentsLoaded();
      const defId = (_agentsCache[0] && _agentsCache[0].id) || app.currentAgentId || null;
      _startNewSession(defId);
    } catch (err) {
      console.error('Long-press new-session failed:', err);
    } finally {
      _newSessionBusy = false;
    }
  }
  app.startNewSessionDefault = startNewSessionDefault;
  // Bridge for the shell-level (non-module) long-press script in index.html,
  // which can't see the module-scoped `app` singleton.
  window.__startNewSessionDefault = startNewSessionDefault;

  // ── Boot sequence ──
  // Auto-detect storage mode from server policy. A URL may force server mode,
  // but browser authority/cache can only be enabled by server capabilities.
  const _agentId = app.currentAgentId || localStorage.getItem('selectedAgentId') || '';
  if (_agentId) {
    storageAdapter.autoSelectMode(_agentId).then(mode => {

      app.reloadStorageMode?.();
    }).catch(e => {
      console.warn('[SessionInit] Failed to auto-select storage mode:', e);
    });
  }

  _loadLastSessionMap();

  populateUserSelect().then(function () {
    if (app.currentUserId) {
      populateSessionSelect(app.currentUserId);
    }
    if (app.currentSessionId) {
      Promise.resolve(loadSessionChat(app.currentSessionId)).then(() => {
        // Cold reload onto a session whose turn is still running (possibly on a
        // different worker): start the DB-reconcile loop so it keeps streaming.
        if (app.isProcessing && typeof app.startReconcileLoop === 'function') {
          app.startReconcileLoop();
        }
      }).catch(() => {});
    }
  });

  // ── Session completion notification ──
  initSessionNotification();

  // ── Live tick for completed session times ──
  _startCompletedTimeTick();

  // ── TUI bridge status poll ──
  setInterval(() => {
    const tuiIndicator = document.getElementById('tui-bridge-indicator');
    if (!tuiIndicator || tuiIndicator.style.display === 'none') return;
    fetch('/api/v1/chat/tui-bridge/status')
      .then(r => r.json())
      .then(data => {
        tuiIndicator.title = data.alive
          ? 'TUI agent bridge active (port ' + data.port + ')'
          : 'TUI agent bridge not connected \u2014 start the Server Manager (TUI)';
        tuiIndicator.style.opacity = data.alive ? '1' : '0.4';
      })
      .catch(() => {});
  }, 15000);

  // ── Session list poll ──
  // PAUSE while a turn is streaming: these endpoints (/db/sessions,
  // /db/sessions/{id}/related) open a fresh Postgres connection and run heavy
  // queries ON the server's event loop, so polling them during a turn starves
  // the live reply. The session list / family tabs don't change mid-turn (a new
  // spawn is rare), so skipping is safe; a terminal event refreshes them anyway.
  setInterval(() => {
    if (window.__agentTurnActive) return;
    const sessionMenu = document.getElementById('session-dropdown-menu');
    const menuOpen = sessionMenu && !sessionMenu.hidden;
    if (app.currentUserId && !menuOpen) {
      populateSessionSelect(app.currentUserId);
      checkSessionCompletions();
    }
  }, 15000);

  // ── Related sessions poll ──
  setInterval(() => {
    if (window.__agentTurnActive) return;
    _fetchRelatedSessions();
  }, 15000);

  // ── Swipe navigation ──
  _initPinSwipeNavigation();
}
