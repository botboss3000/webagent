'use strict';

// Session entry point — registerSessionApi() sets app.* session methods;
// initSessions() builds the session dropdown, agent picker, header buttons and
// starts polling (TUI bridge, sessions, related sessions) + swipe nav.
// Module map for this folder: ui/chat-side-panel/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders, isAdmin } from '../../shared/js/left-login.js';
import { icon } from '../../shared/js/icons.js';
import { copyText } from '../../shared/js/clipboard.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../shared/js/delete-control.js';
import { makeRowsReorderable, attachRowLongPress, persistSessionOrder } from '../../shared/js/ordering.js';
import { populateUserSelect, initUserPanel } from '../../shared/js/user-panel.js';
import { consumeReplayedEventsFor } from '../../shared/js/agentWs.js';
import { loopSessionChanged } from '../../main-panel/agents/agent-loop/js/loop.js';
import { loopVisualSessionChanged } from '../../main-panel/agents/agent-loop/js/loop-logic.js';
import { genuiSessionChanged } from '../../main-panel/genui/js/genui.js';
import { chatActivitySessionChanged } from '../../shared/js/chat-activity.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { addChatBubble } from './chat-bubble.js';
import { _cacheAppendMessage, _loadSessionFocus, _captureSessionFocus } from './chat-message-cache.js';
import { _teardownVirtualScroll, _installVirtualScroll } from './chat-virtual-scroll.js';
import { loadSessionChat, refreshTranscript, _syncTerminalChat } from './session-load.js';
import {
  _sessionsCache,
  populateSessionSelect,
  _renderSessionRows,
  _setTriggerLabel,
  _fetchRelatedSessions,
  applySessionTitle,
  _setShowHidden,
  _getShowHidden,
  toggleSessionGroup,
} from './session-list.js';
import {
  _agentsCache,
  populateAgentSelect,
  _refreshAgentAbilities,
  _agentIconFor,
  _setAgentTriggerLabel,
  _renderAgentRows,
  _renderAgentDropdown,
  _fetchAgentRunningStatuses,
  _loadLastSessionMap,
  _lastSessionPerAgent,
  _saveLastSessionMap,
  ensureWebagentAgent,
} from './session-agent.js';
import {
  interruptSession,
  switchToSession,
  deleteSession,
  patchSession,
  togglePin,
  toggleHidden,
  startRename,
  _headerRenameSession,
} from './session-core.js';
import { _initPinSwipeNavigation } from './session-swipe.js';
import { agentChatMsg } from '../../shared/js/app-prompts.js';

// ── registerSessionApi ─────────────────────────────────────────────────────

export function registerSessionApi() {
  app.populateSessionSelect = populateSessionSelect;
  app.populateAgentSelect = populateAgentSelect;
  app.loadSessionChat = loadSessionChat;
  app.applySessionTitle = applySessionTitle;
  app.reloadCurrentSession = () => {
    if (app.currentSessionId) loadSessionChat(app.currentSessionId);
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

  const dropdown = document.getElementById('session-dropdown');
  const sessionTrigger = document.getElementById('session-dropdown-trigger');
  const menu = document.getElementById('session-dropdown-menu');

  let _sessionsLoaded = false;
  function openMenu() {
    if (!menu) return;
    menu.hidden = false;
    dropdown.classList.add('open');
    _resetAllDeleteButtons();
    if (app.currentUserId) {
      _sessionsLoaded = true;
      // Paint cached sessions instantly so the list is never blank/stale while
      // we refresh, then fetch from the DB to pull in new/changed sessions
      // (re-renders again when the response lands).
      if (_sessionsCache.length) _renderSessionRows();
      populateSessionSelect(app.currentUserId);
    }
  }

  function closeMenu() {
    if (!menu) return;
    menu.hidden = true;
    dropdown.classList.remove('open');
    _resetAllDeleteButtons();
    _closeRowActions();
    // Reset manage mode so the dropdown reopens in the normal (non-hidden) view.
    _setShowHidden(false);
  }

  function _resetDeleteBtn(btn) {
    resetDeleteBtn(btn, { title: 'Delete session' });
  }

  function _resetAllDeleteButtons() {
    document.querySelectorAll('.session-row-delete').forEach(_resetDeleteBtn);
  }

  function handleDeleteClick(btn, sid) {
    advanceDeleteBtn(btn, {
      onArm: (b) => document.querySelectorAll('.session-row-delete[data-state="warning"]').forEach(other => {
        if (other !== b) resetDeleteBtn(other, { title: 'Delete session' });
      }),
      onConfirm: () => deleteSession(sid),
    });
  }

  function handleDeleteAllClick(btn) {
    advanceDeleteBtn(btn, {
      size: '15px', spinSize: '15px',
      onArm: () => document.querySelectorAll('.session-row-delete[data-state="warning"]').forEach(other => {
        resetDeleteBtn(other, { title: 'Delete session' });
      }),
      onConfirm: async () => {
        // Spare pinned and hidden sessions; delete the rest of the visible list.
        const targets = _sessionsCache.filter(s => !s.pinned && !s.hidden).map(s => s.id);
        for (const sid of targets) {
          await deleteSession(sid);
        }
      },
    });
  }

  // ── Trigger events ──
  if (sessionTrigger) {
    let _lpTimer = null, _lpStartX = 0, _lpStartY = 0;
    sessionTrigger.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.header-delete-btn, .header-plus-btn, .session-dropdown-status')) return;
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      _lpStartX = e.clientX; _lpStartY = e.clientY;
      _lpTimer = setTimeout(() => {
        _lpTimer = null;
        e.preventDefault();
        _headerRenameSession();
      }, 500);
    });
    sessionTrigger.addEventListener('pointermove', (e) => {
      if (!_lpTimer) return;
      if (Math.abs(e.clientX - _lpStartX) > 8 || Math.abs(e.clientY - _lpStartY) > 8) {
        clearTimeout(_lpTimer); _lpTimer = null;
      }
    });
    sessionTrigger.addEventListener('pointerup', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
    sessionTrigger.addEventListener('pointercancel', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
    sessionTrigger.addEventListener('click', (e) => {
      if (e.target.closest('.header-delete-btn, .header-plus-btn')) return;
      if (e.target.closest('.session-row-title-input')) return;
      e.stopPropagation();
      if (menu.hidden) openMenu(); else closeMenu();
    });
  }

  // The chevron now sits OUTSIDE the trigger button (so the delete button can sit to
  // its left), so it no longer inherits the trigger's click-to-open — wire it to the
  // same toggle. (Disabled-while-loading is handled by CSS pointer-events.)
  const sessionChevron = dropdown && dropdown.querySelector('.session-dropdown-chevron');
  if (sessionChevron && menu) {
    sessionChevron.addEventListener('click', (e) => {
      e.stopPropagation();
      if (menu.hidden) openMenu(); else closeMenu();
    });
    sessionChevron.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (menu.hidden) openMenu(); else closeMenu();
      }
    });
  }

  // ── Per-row "more" (⋯) popup: pin/unpin toggle ─────────────────────────────
  // Body-mounted floating menu anchored under the kebab. Mirrors the genui page
  // kebab popup (.session-row-actions / .session-row-action). Only a pin/unpin
  // action for now; the two-click delete stays on the row's own trash button.
  let _rowActionsEl = null;
  function _closeRowActions() {
    if (_rowActionsEl) { _rowActionsEl.remove(); _rowActionsEl = null; }
    document.removeEventListener('pointerdown', _onRowActionsOutside, true);
    document.removeEventListener('keydown', _onRowActionsEsc, true);
    window.removeEventListener('resize', _closeRowActions);
  }
  function _onRowActionsOutside(e) {
    if (_rowActionsEl && !_rowActionsEl.contains(e.target) && !e.target.closest('.session-row-kebab')) {
      _closeRowActions();
    }
  }
  function _onRowActionsEsc(e) { if (e.key === 'Escape') _closeRowActions(); }
  function _openRowActions(sid, kebabBtn) {
    const sess = _sessionsCache.find(s => s.id === sid);
    const pinned = !!(sess && sess.pinned);
    const popup = document.createElement('div');
    popup.className = 'session-row-actions';
    popup.dataset.id = sid;
    popup.dataset.source = 'session-list';
    popup.innerHTML =
      `<button class="session-row-action" data-action="pin">${icon(pinned ? 'pin-off' : 'pin', { size: '14px' })} ${pinned ? 'Unpin session' : 'Pin session'}</button>`;
    document.body.appendChild(popup);
    // Position under the kebab, right-aligned, clamped to the viewport.
    const kb = kebabBtn.getBoundingClientRect();
    const pw = popup.offsetWidth, ph = popup.offsetHeight;
    let left = kb.right - pw;
    let top = kb.bottom + 4;
    if (left < 4) left = 4;
    if (left + pw > window.innerWidth - 4) left = window.innerWidth - pw - 4;
    if (top + ph > window.innerHeight - 4) top = kb.top - ph - 4;
    popup.style.left = left + 'px';
    popup.style.top = top + 'px';
    popup.addEventListener('click', (e) => {
      e.stopPropagation();
      const actionBtn = e.target.closest('.session-row-action');
      if (!actionBtn) return;
      if (actionBtn.dataset.action === 'pin') { togglePin(sid); _closeRowActions(); }
    });
    _rowActionsEl = popup;
    // Defer so the click that opened the popup doesn't immediately close it.
    setTimeout(() => {
      document.addEventListener('pointerdown', _onRowActionsOutside, true);
      document.addEventListener('keydown', _onRowActionsEsc, true);
      window.addEventListener('resize', _closeRowActions);
    }, 0);
  }

  if (menu) {
    menu.addEventListener('click', (e) => {
      e.stopPropagation();
      const delBtn = e.target.closest('.session-row-delete');
      if (delBtn) {
        const row = delBtn.closest('.session-row');
        const sid = row && row.dataset.id;
        if (sid) handleDeleteClick(delBtn, sid);
        return;
      }
      // Per-row "more" (⋯) kebab — opens a small popup with the pin/unpin toggle.
      // A second click on the same kebab closes it (toggle behaviour).
      const kebabBtn = e.target.closest('.session-row-kebab');
      if (kebabBtn) {
        const sid = kebabBtn.dataset.id;
        const open = document.querySelector('.session-row-actions[data-source="session-list"]');
        if (open && open.dataset.id === sid) { _closeRowActions(); return; }
        _closeRowActions();
        if (sid) _openRowActions(sid, kebabBtn);
        return;
      }
      // Footer: toggle show-hidden (manage mode) — reveals hidden rows and
      // swaps row trash buttons for eye toggles.
      const eyeBtn = e.target.closest('.session-manage-eye');
      if (eyeBtn) {
        _setShowHidden(!_getShowHidden());
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        return;
      }
      // Footer: delete all visible, non-pinned sessions (two-click confirm).
      const delAllBtn = e.target.closest('.session-manage-delete-all');
      if (delAllBtn) {
        handleDeleteAllClick(delAllBtn);
        return;
      }
      // Per-row visibility (eye) toggle in manage mode.
      const visBtn = e.target.closest('.session-row-visibility');
      if (visBtn) {
        const sid = visBtn.dataset.id;
        if (sid) toggleHidden(sid);
        return;
      }
      // Expand/collapse a family-root row (orchestrator / optimizer Planner).
      // Caught before the row-switch below so toggling the tree doesn't also
      // open the parent session.
      const expandBtn = e.target.closest('.session-row-expand');
      if (expandBtn) {
        toggleSessionGroup(expandBtn.dataset.id);
        return;
      }
      if (e.target.closest('.session-row-title-input')) return;
      // Both top-level rows and nested child rows are `.session-row` with a
      // data-id, so a click switches to whichever session was clicked. Picking a
      // session also closes the dropdown so it doesn't linger over the new chat.
      const row = e.target.closest('.session-row');
      if (row) { switchToSession(row.dataset.id); closeMenu(); }
    });
    makeRowsReorderable(menu, {
      // Only top-level rows reorder; nested child rows (.session-child-row) are
      // positioned by their parent group, not independently draggable.
      rowSelector: '.session-row:not(.session-child-row)',
      handleSelector: '.row-drag-handle',
      onReorder: (orderedIds) => {
        const byId = new Map(_sessionsCache.map(s => [s.id, s]));
        const next = orderedIds.map(id => byId.get(id)).filter(Boolean);
        for (const s of _sessionsCache) if (!orderedIds.includes(s.id)) next.push(s);
        next.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
        _sessionsCache.length = 0;
        _sessionsCache.push(...next);
        _renderSessionRows();
        _setTriggerLabel();
        persistSessionOrder(app.currentUserId, _sessionsCache.map(s => s.id));
      },
    });
    attachRowLongPress(menu, {
      rowSelector: '.session-row:not(.session-child-row)',
      ignoreSelector: '.row-drag-handle, .session-row-delete, .session-row-kebab, .session-row-title-input',
      // Only a long-press ON THE SESSION NAME opens rename — not the pin icon,
      // status dot, caret, or empty row gaps.
      requireSelector: '.session-row-title',
      onLongPress: (sid, row) => startRename(sid, row),
    });
  }

  // ── Agent-name dropdown (click name to switch agents) ────────────────────
  const agentNameEl = document.getElementById('chat-header-agent-name');
  const agentMenu = document.getElementById('agent-dropdown-menu');

  function openAgentMenu() {
    if (!agentMenu) return;
    _renderAgentDropdown();
    agentMenu.hidden = false;
  }

  function closeAgentMenu() {
    if (!agentMenu) return;
    agentMenu.hidden = true;
  }

  if (agentNameEl && agentMenu) {
    agentNameEl.addEventListener('click', (e) => {
      e.stopPropagation();
      if (agentMenu.hidden) openAgentMenu(); else closeAgentMenu();
    });
    agentNameEl.addEventListener('keydown', (e) => {
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
    });
  }

  // Close agent dropdown on outside click
  document.addEventListener('click', (e) => {
    if (agentNameEl && agentMenu && !agentNameEl.contains(e.target) && !agentMenu.contains(e.target)) {
      closeAgentMenu();
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && agentMenu && !agentMenu.hidden) closeAgentMenu();
  });

  document.addEventListener('click', (e) => {
    if (dropdown && !dropdown.contains(e.target)) closeMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && menu && !menu.hidden) closeMenu();
  });

  // ── New session ──
  function _startNewSession(agentId) {
    app.abortChatStream?.();
    if (agentId && agentId !== app.currentAgentId) {
      app.currentAgentId = agentId;
      _refreshAgentAbilities(agentId);
      try { localStorage.setItem('selectedAgentId', agentId); } catch (_) {}
    }
    app.currentSessionId = randomUUID();
    localStorage.setItem('terminalSessionId', app.currentSessionId);
    // Re-sync the execution-mode pill for this brand-new session (per-session key,
    // defaults to Ask). Without this, a fresh chat keeps the previous session's
    // in-memory mode (e.g. a leftover Auto) while the pill shows the wrong label —
    // so the message would silently send a different mode than the pill displays.
    if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
    if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
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
      app.chatInput.value = '';
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
      closeMenu();
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
          deleteSession(sid).then(_resetHeaderDel);
        },
      });
    });
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

  // ── Header "more" (⋯) menu — admin-only Agent/Session debug + copy ──────────
  // A small dropdown beside the refresh / new-session buttons. Rebuilds its body
  // on each open so it always reflects the LIVE agent + session id. The wrapper
  // is CSS-hidden for non-admins (body.is-admin in app1.css); we also re-guard on
  // isAdmin() here. This replaces the old in-transcript Agent/Session diagnostic
  // line (removed from session-load.js so it can't drift off the top on paging).
  (function _wireHeaderMoreMenu() {
    const wrap = document.getElementById('chat-header-more');
    const moreBtn = document.getElementById('chat-header-more-btn');
    const menu = document.getElementById('chat-header-more-menu');
    if (!wrap || !moreBtn || !menu) return;

    let open = false;
    let onDocDown = null;

    function onKey(e) { if (e.key === 'Escape') { closeMenu(); moreBtn.focus(); } }

    function closeMenu() {
      if (!open) return;
      open = false;
      menu.hidden = true;
      wrap.classList.remove('open');
      moreBtn.setAttribute('aria-expanded', 'false');
      document.removeEventListener('pointerdown', onDocDown, true);
      document.removeEventListener('keydown', onKey, true);
    }

    function buildBody() {
      const agentId = app.currentAgentId || '—';
      const sessionId = app.currentSessionId || '—';
      menu.textContent = '';

      const section = document.createElement('div');
      section.className = 'chm-section';

      const head = document.createElement('div');
      head.className = 'chm-section-head';
      const title = document.createElement('span');
      title.className = 'chm-section-title';
      title.textContent = 'Session debug';
      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'chm-copy';
      copyBtn.title = 'Copy Agent + Session id';
      copyBtn.innerHTML = icon('copy', { size: '14px' });
      head.append(title, copyBtn);

      const mkRow = (k, v) => {
        const row = document.createElement('div');
        row.className = 'chm-row';
        const key = document.createElement('span');
        key.className = 'chm-key';
        key.textContent = k;
        const val = document.createElement('span');
        val.className = 'chm-val';
        val.textContent = v;
        row.append(key, val);
        return row;
      };

      section.append(head, mkRow('Agent', agentId), mkRow('Session', sessionId));
      menu.appendChild(section);

      copyBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        copyText(`Agent: ${agentId} · Session: ${sessionId}`).then(() => {
          copyBtn.classList.add('copied');
          copyBtn.innerHTML = icon('check', { size: '14px' });
          setTimeout(() => {
            copyBtn.classList.remove('copied');
            copyBtn.innerHTML = icon('copy', { size: '14px' });
          }, 1200);
        }).catch(() => {});
      });
    }

    function openMenu() {
      if (open || !isAdmin()) return;
      buildBody();
      open = true;
      menu.hidden = false;
      wrap.classList.add('open');
      moreBtn.setAttribute('aria-expanded', 'true');
      onDocDown = (e) => { if (!wrap.contains(e.target)) closeMenu(); };
      // Defer the outside-click/Escape listeners so the SAME click that opened
      // the menu doesn't immediately close it.
      setTimeout(() => {
        document.addEventListener('pointerdown', onDocDown, true);
        document.addEventListener('keydown', onKey, true);
      }, 0);
    }

    moreBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (open) closeMenu(); else openMenu();
    });
  })();

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
    if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
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
      closeMenu();
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
  _loadLastSessionMap();

  populateUserSelect().then(function () {
    if (app.currentUserId) {
      _sessionsLoaded = true;
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