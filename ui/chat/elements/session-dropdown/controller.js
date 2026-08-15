'use strict';

// Session dropdown controller — the session selector chip in the chat header.
// Centralized home: ui/chat/elements/session-dropdown/ (index.js = element
// contract, controller.js = this file, list.js = session data + rendering).
//
// Mounted two ways, both idempotent (per root element, guarded by a WeakMap):
//   1. the element's init() — when the chat-controls loader creates the
//      control dynamically, and
//   2. session-init.js at boot — when the static chat-side-panel.html markup is
//      present (the loader REUSES existing DOM and does NOT call init()).
//
// mountSessionDropdown(root) returns { open, close, isOpen, destroy } and
// stores the handle on root._sessionDropdownController so header code
// (new-session flows, etc.) can close the menu without importing internals.
//
// Owns: open/close/position/animation, the loading skeleton, keyboard
// dismissal, trigger wiring (click/Enter/space/long-press/kebab/delete), the
// per-row "more" popup, the menu click delegation (switch/delete/bin/visibility/
// checkbox/expand/search/manage footer), row reordering and long-press rename.

import { app } from '../../../shared/js/state.js';
import { apiPath } from '../../../shared/js/config.js';
import { icon } from '../../../shared/js/icons.js';
import { copyText } from '../../../shared/js/clipboard.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../../shared/js/delete-control.js';
import { makeRowsReorderable, attachRowLongPress, persistSessionOrder } from '../../../shared/js/ordering.js';
import {
  _sessionsCache,
  populateSessionSelect,
  refreshSessionMetadata,
  hasFreshSessionMetadataCache,
  _renderSessionRows,
  _setTriggerLabel,
  _setShowHidden, _getShowHidden,
  _setCheckboxMode, _getCheckboxMode,
  _selectedSessions, _clearSelected,
  toggleSessionGroup,
  _setSearchQuery, _getSearchQuery,
  _setSearchMode, _getSearchMode,
  _setBinMode, _getBinMode,
  _scanMessagesForQuery, _clearMsgSearch,
  _scheduleSearchCommit, _cancelPendingSearchCommit,
} from './list.js';
import {
  switchToSession,
  deleteSession,
  togglePin,
  toggleHidden,
  startRename,
  _headerRenameSession,
  autoRenameSession,
} from '../../js/session-core.js';

const _mounts = new WeakMap(); // root el -> { handle, destroy }

export function mountSessionDropdown(root, opts = {}) {
  if (!root) return null;
  if (_mounts.has(root)) return _mounts.get(root).handle;
  const menu = root.querySelector('.session-dropdown-menu')
    || document.getElementById('session-dropdown-menu');
  if (!menu) return null;

  const dropdown = root;
  let _openRequestId = 0;
  let _keyboardDismissRequested = false;
  let _closeAnimationTimer = 0;
  let _openAnimationTimer = 0;
  let _lastToggleTime = 0;

  function _isEditableElement(el) {
    if (!el || el === document.body) return false;
    return el.matches?.('input, textarea, select, [contenteditable="true"]') || false;
  }

  function _viewportHeight() {
    return window.visualViewport?.height || window.innerHeight;
  }

  // Focus is the reliable keyboard signal. Some Android WebViews shrink both
  // visualViewport and innerHeight, so comparing those two values can falsely
  // report that the keyboard is already closed.
  function _dismissKeyboardBeforeOpen() {
    const active = document.activeElement;
    const hadEditableFocus = _isEditableElement(active)
      || document.body.classList.contains('chat-pill-focused');
    if (!hadEditableFocus) return false;

    _keyboardDismissRequested = true;
    active?.blur?.();
    document.body.classList.remove('chat-pill-focused');
    return true;
  }

  function _waitForKeyboardDismiss() {
    const vv = window.visualViewport;
    const startHeight = _viewportHeight();
    let lastHeight = startHeight;
    let lastChangeAt = performance.now();
    // Whether the viewport has actually moved since the blur. False on desktop
    // (blurring an input never resizes the viewport) — lets the wait finish in
    // ~120ms instead of the full 450ms settle window when there is no keyboard.
    let sawResize = false;
    let finished = false;
    let frame = 0;
    let timeout = 0;

    return new Promise(resolve => {
      const finish = () => {
        if (finished) return;
        finished = true;
        cancelAnimationFrame(frame);
        clearTimeout(timeout);
        vv?.removeEventListener('resize', noteResize);
        window.removeEventListener('resize', noteResize);
        if (menu._deferredOpenCleanup === finish) menu._deferredOpenCleanup = null;
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      };
      const noteResize = () => {
        sawResize = true;
        lastHeight = _viewportHeight();
        lastChangeAt = performance.now();
      };
      const check = now => {
        const height = _viewportHeight();
        if (Math.abs(height - lastHeight) > 1) {
          lastHeight = height;
          lastChangeAt = now;
          // A measured height change also counts as motion (covers browsers
          // that resize without firing a resize event).
          sawResize = true;
        }
        const viewportGrew = height > startHeight + 40;
        const quietFor = now - lastChangeAt;
        // Fast path (desktop / no keyboard): nothing moved within a short
        // grace window — there is no keyboard to wait for, open immediately.
        if (!sawResize && quietFor >= 120) { finish(); return; }
        // Keyboard collapse confirmed: wait until the viewport has grown AND
        // settled, or (motion without growth) the full settle window.
        if ((viewportGrew && quietFor >= 120) || (sawResize && quietFor >= 450)) {
          finish();
          return;
        }
        frame = requestAnimationFrame(check);
      };

      vv?.addEventListener('resize', noteResize);
      window.addEventListener('resize', noteResize);
      frame = requestAnimationFrame(check);
      timeout = setTimeout(finish, 2500);
      menu._deferredOpenCleanup = finish;
    });
  }

  // The menu must have real geometry before the open animation measures it.
  // On a cold load the session request is still in flight, so paint phantom
  // skeleton rows synchronously instead of briefly exposing an empty,
  // border-only box. The skeleton stays visible (via list.js's retry policy)
  // until the real list lands.
  function _renderSessionMenuSkeleton() {
    if (!menu) return;
    menu.replaceChildren();
    menu.dataset.loading = 'true';
    menu.setAttribute('aria-busy', 'true');
    for (let i = 0; i < 5; i++) {
      const row = document.createElement('div');
      row.className = 'session-dropdown-loading-row';
      row.setAttribute('aria-hidden', 'true');
      row.innerHTML = `
        <span class="session-dropdown-loading-icon"></span>
        <span class="session-dropdown-loading-line"></span>
      `;
      menu.appendChild(row);
    }
  }

  function _prepareSessionMenuContent() {
    if (app.currentUserId) {
      _renderSessionMenuSkeleton();
    } else {
      menu.replaceChildren();
      delete menu.dataset.loading;
      menu.removeAttribute('aria-busy');
      const empty = document.createElement('div');
      empty.className = 'session-dropdown-empty';
      empty.textContent = 'No sessions yet';
      menu.appendChild(empty);
    }
  }

  /** Position and show the menu (called after the keyboard is gone). */
  function _positionAndShowMenu() {
    if (!menu) return;
    clearTimeout(_closeAnimationTimer);
    clearTimeout(_openAnimationTimer);
    _closeAnimationTimer = 0;
    _openAnimationTimer = 0;
    menu._closeAnimation?.cancel();
    menu._closeAnimation = null;
    menu.style.maxHeight = '';
    menu.style.overflow = '';
    // Clear a stale transition lock (an interrupted close sets transition:none)
    // and any stale measured height so every open starts from a clean slate.
    menu.style.transition = '';
    menu.style.removeProperty('--session-menu-open-height');
    // Move to body so it escapes overflow-clipped ancestors
    if (menu.parentNode !== document.body) document.body.appendChild(menu);

    const rect = dropdown.getBoundingClientRect();
    const viewport = window.visualViewport;
    const viewportTop = viewport?.offsetTop || 0;
    const viewportBottom = viewportTop + (viewport?.height || window.innerHeight);
    const spaceBelow = viewportBottom - rect.bottom;
    const spaceAbove = rect.top - viewportTop;
    const flipUp = spaceBelow < 180 && spaceAbove > spaceBelow;

    const isMobile = window.innerWidth <= 800;
    // Desktop: the open menu mirrors the header bar above it (not the whole
    // chat panel), so it lines up edge-to-edge with #chat-header's width.
    const header = document.getElementById('chat-header');
    const headerRect = header ? header.getBoundingClientRect() : null;
    const panel = document.getElementById('chat-panel');
    const panelRect = panel ? panel.getBoundingClientRect() : { left: 0, width: window.innerWidth };

    menu.style.position = 'fixed';
    menu.style.top = flipUp ? 'auto' : (rect.bottom + 4) + 'px';
    menu.style.bottom = flipUp ? (window.innerHeight - rect.top + 4) + 'px' : 'auto';
    const availableHeight = Math.max(100, Math.floor((flipUp ? spaceAbove : spaceBelow) - 8));
    menu._availableHeight = availableHeight;

    if (isMobile) {
      menu.style.left = '0';
      menu.style.right = '0';
      menu.style.width = 'auto';
      menu.style.minWidth = '0';
      menu.style.maxWidth = 'none';
      menu.style.transform = '';
    } else {
      // Anchor to the header bar so the open menu spans exactly the header
      // width; fall back to the chat panel if the header isn't present.
      const anchorLeft = headerRect ? headerRect.left : panelRect.left;
      const anchorWidth = headerRect ? headerRect.width : panelRect.width;
      menu.style.left = anchorLeft + 'px';
      menu.style.width = anchorWidth + 'px';
      menu.style.transform = '';
      const ddStyle = getComputedStyle(dropdown);
      menu.style.minWidth = ddStyle.minWidth !== '0px' ? ddStyle.minWidth : '420px';
      menu.style.maxWidth = ddStyle.maxWidth !== 'none' ? ddStyle.maxWidth : 'none';
    }

    // Now reveal
    menu.hidden = false;
    menu.classList.toggle('flip-up', flipUp);
    menu.dataset.state = 'opening';
    menu.classList.remove('closing');
    dropdown.classList.add('open');
    _resetAllDeleteButtons();
  }

  async function openMenu() {
    if (!menu) return;
    const requestId = ++_openRequestId;
    const shouldWait = _keyboardDismissRequested || _dismissKeyboardBeforeOpen();
    _keyboardDismissRequested = false;
    if (shouldWait) {
      await _waitForKeyboardDismiss();
      if (requestId !== _openRequestId) return;
    }
    // Do this before revealing the menu. The following measurement can now
    // never observe an empty list while the async session fetch is pending.
    _prepareSessionMenuContent();
    _positionAndShowMenu();
    requestAnimationFrame(() => {
      if (requestId !== _openRequestId || menu.dataset.state !== 'opening') return;
      const availableHeight = menu._availableHeight || menu.scrollHeight;
      const measuredHeight = menu.scrollHeight;
      // Keep a defensive non-zero floor even if an embedded browser reports a
      // transient zero scrollHeight. max-height does not force extra blank
      // space once real rows replace the skeleton.
      const targetHeight = Math.min(Math.max(measuredHeight, 100), availableHeight);
      menu.style.setProperty('--session-menu-open-height', `${targetHeight}px`);
      const rows = Array.from(menu.children);
      const stagger = rows.length ? Math.min(0.045, 0.3 / rows.length) : 0.035;
      rows.forEach((row, index) => {
        row.style.animationDelay = `${(index * stagger).toFixed(3)}s`;
      });
      // Match the tool-call disclosure: establish the collapsed frame, then
      // transition to the measured list height on the following frame.
      menu.offsetHeight;
      requestAnimationFrame(() => {
        if (requestId !== _openRequestId || menu.dataset.state !== 'opening') return;
        menu.classList.add('opening');
        menu.dataset.state = 'open';
        // Start the request only after the skeleton has occupied a painted
        // frame. Otherwise a warm cache (or a very fast local response) can
        // replace the phantom rows before the browser ever displays them.
        requestAnimationFrame(() => {
          if (requestId !== _openRequestId || menu.dataset.state !== 'open') return;
          _loadSessionList();
        });
        const totalMs = rows.length * stagger * 1000 + 180;
        _openAnimationTimer = setTimeout(() => {
          menu.classList.remove('opening');
          rows.forEach(row => { row.style.animationDelay = ''; });
          _openAnimationTimer = 0;
        }, totalMs);
      });
    });
  }

  function _loadSessionList() {
    if (!app.currentUserId) return;
    const cacheWasFresh = hasFreshSessionMetadataCache(app.currentUserId);
    populateSessionSelect(app.currentUserId, { preferCache: true });
    // A fresh page/dropdown cache already has this metadata, so don't follow
    // it with a second refresh request that defeats the fast open.
    if (!cacheWasFresh) refreshSessionMetadata(app.currentUserId);
  }

  function _finishMenuClose() {
    clearTimeout(_closeAnimationTimer);
    _closeAnimationTimer = 0;
    if (menu.dataset.state !== 'closing') return;
    menu.hidden = true;
    delete menu.dataset.state;
    menu.classList.remove('flip-up');
    // Return the menu to its original parent so next open's body-append works
    if (menu.parentNode !== dropdown) {
      dropdown.appendChild(menu);
    }
    // Clear inline position styles so the next open recalculates fresh
    menu.style.position = '';
    menu.style.top = '';
    menu.style.left = '';
    menu.style.bottom = '';
    menu.style.transform = '';
    menu.style.width = '';
    menu.style.minWidth = '';
    menu.style.maxWidth = '';
    menu.style.maxHeight = '';
    menu.style.overflow = '';
    menu.style.transition = '';
    menu.style.removeProperty('--session-menu-open-height');
    menu._availableHeight = 0;
    menu.classList.remove('opening', 'closing');
    menu._closeAnimation?.cancel();
    menu._closeAnimation = null;
  }

  function closeMenu() {
    if (!menu) return;
    // A pending search-input debounce must not fire after the menu closes —
    // it would commit a half-typed query the user thought they abandoned.
    _cancelPendingSearchCommit();
    _openRequestId++;
    dropdown.classList.remove('open');
    // Clean up the keyboard-deferred open handler if it's pending
    menu._deferredOpenCleanup?.();
    if (!menu.hidden) {
      clearTimeout(_openAnimationTimer);
      _openAnimationTimer = 0;
      menu.classList.remove('opening');
      const rows = Array.from(menu.children);
      const stagger = rows.length ? Math.min(0.035, 0.18 / rows.length) : 0.025;
      // Lock the current rendered height before transitioning it to zero.
      menu.style.maxHeight = `${menu.getBoundingClientRect().height}px`;
      menu.style.overflow = 'hidden';
      menu.offsetHeight;
      rows.reverse().forEach((row, index) => {
        row.style.animationDelay = `${(index * stagger).toFixed(3)}s`;
      });
      menu.classList.add('closing');
      menu.dataset.state = 'closing';
      clearTimeout(_closeAnimationTimer);
      if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
        _finishMenuClose();
      } else if (typeof menu.animate !== 'function') {
        requestAnimationFrame(() => {
          if (menu.dataset.state === 'closing') menu.style.maxHeight = '0px';
        });
        _closeAnimationTimer = setTimeout(_finishMenuClose, 380);
      } else {
        const startHeight = menu.getBoundingClientRect().height;
        const endTranslate = menu.classList.contains('flip-up') ? '4px' : '-4px';
        // Suppress CSS transitions so they don't fight the WAAPI animation.
        menu.style.transition = 'none';
        menu._closeAnimation = menu.animate([
          {
            maxHeight: `${startHeight}px`,
            clipPath: 'inset(0 0 0 0 round 8px)',
            opacity: 1,
            transform: 'translateY(0)',
          },
          {
            maxHeight: '0px',
            clipPath: menu.classList.contains('flip-up')
              ? 'inset(100% 0 0 0 round 8px)'
              : 'inset(0 0 100% 0 round 8px)',
            opacity: 0,
            transform: `translateY(${endTranslate})`,
          },
        ], {
          duration: 340,
          easing: 'cubic-bezier(.4, 0, .2, 1)',
          fill: 'forwards',
        });
        menu._closeAnimation.onfinish = _finishMenuClose;
        menu._closeAnimation.oncancel = () => {};
        // Fallback for embedded browsers that expose animate() but fail to
        // deliver the finish callback when the tab loses visibility.
        _closeAnimationTimer = setTimeout(_finishMenuClose, 450);
      }
    }
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
      onConfirm: async () => {
        const result = await deleteSession(sid, { retries: 1 });
        if (!result.ok) {
          // Show error in the session name space — replace the title text, revert
          // after a couple seconds so the user can retry.
          const row = btn.closest('.session-row');
          if (row) {
            _showRowError(row, result.error || 'Delete failed');
          }
          resetDeleteBtn(btn, { title: 'Delete session', size: '14px' });
        }
      },
    });
  }

  // Show an ephemeral error message in the session row's title span, then
  // revert to the original session name after ~2.5s. Uses design-system
  // colours via a class — no hard-coded hex.
  function _showRowError(row, msg, durationMs) {
    const titleSpan = row.querySelector('.session-row-title');
    if (!titleSpan) return;
    const orig = titleSpan.dataset._delErrOrig || titleSpan.innerHTML;
    titleSpan.dataset._delErrOrig = orig;
    titleSpan.textContent = msg;
    titleSpan.classList.add('session-row-title-error');
    titleSpan.title = msg;
    clearTimeout(titleSpan._delErrTimer);
    titleSpan._delErrTimer = setTimeout(() => {
      titleSpan.innerHTML = titleSpan.dataset._delErrOrig || orig;
      titleSpan.classList.remove('session-row-title-error');
      titleSpan.title = 'Hold to rename';
      delete titleSpan.dataset._delErrOrig;
    }, durationMs || 2500);
  }

  // ── Shared "Renaming…" feedback (header trigger + dropdown rows) ─────────
  // Auto rename swaps the session NAME for a spinner + "Renaming…" in BOTH the
  // header trigger label and the dropdown row's title — one code path, one
  // look. The agent icon (a child of each row's title span, a sibling of the
  // header label) is left untouched, so the only thing that changes is the
  // name itself.

  function _setRenamingState(el, on) {
    if (!el) return;
    if (on) {
      el.dataset._renOrig = el.innerHTML;
      el.dataset._renOrigTitle = el.title || '';
      const iconEl = el.querySelector(':scope > .session-row-agent-icon');
      if (iconEl) {
        el.innerHTML = '';
        el.appendChild(iconEl);
        el.insertAdjacentHTML('beforeend',
          '<span class="session-row-sep"> </span>' +
          icon('loader-2', { size: '14px', className: 'session-radial-loader' }) +
          ' Renaming\u2026');
      } else {
        el.innerHTML = icon('loader-2', { size: '14px', className: 'session-radial-loader' }) + ' Renaming\u2026';
      }
      el.title = 'Generating name\u2026';
    } else if (el.dataset._renOrig !== undefined) {
      el.innerHTML = el.dataset._renOrig;
      el.title = el.dataset._renOrigTitle;
      delete el.dataset._renOrig;
      delete el.dataset._renOrigTitle;
    }
  }

  // Set a session name in a title element, keeping any agent icon inside it.
  function _setSessionName(el, name) {
    if (!el) return;
    const iconEl = el.querySelector(':scope > .session-row-agent-icon');
    if (iconEl) {
      el.innerHTML = '';
      el.appendChild(iconEl);
      el.insertAdjacentHTML('beforeend', '<span class="session-row-sep"> </span>');
      el.appendChild(document.createTextNode(name));
    } else {
      el.textContent = name;
    }
  }

  // Drop any saved renaming snapshot (used after _setTriggerLabel re-renders
  // the header label from the cache, so a stale snapshot can't linger).
  function _clearRenamingSaved(el) {
    if (!el) return;
    delete el.dataset._renOrig;
    delete el.dataset._renOrigTitle;
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
          const result = await deleteSession(sid, { retries: 1 });
          if (!result.ok) {
            console.warn('Delete-all: failed to delete', sid, result.error);
            // Don't abort the batch — log and continue. The surviving row will
            // still be visible after the batch for the user to retry individually.
          }
        }
      },
    });
  }

  function handleDeleteSelectedClick(btn) {
    advanceDeleteBtn(btn, {
      size: '15px', spinSize: '15px',
      onConfirm: async () => {
        const targets = [..._selectedSessions];
        for (const sid of targets) {
          const result = await deleteSession(sid, { retries: 1 });
          if (!result.ok) {
            console.warn('Delete-selected: failed to delete', sid, result.error);
          }
        }
        _clearSelected();
        _setCheckboxMode(false);
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
      },
    });
  }

  // Restore a recycled session from the bin (POST /sessions/{id}/restore),
  // then refetch so the row moves back to the live list (or disappears from a
  // bin-only view). Same endpoint as the Sessions page restore; the current
  // search/bin scope is preserved across the refetch.
  async function _restoreFromBin(sid) {
    if (!sid) return;
    const token = localStorage.getItem('auth_token');
    try {
      let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/restore?db=user.db`;
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url), { method: 'POST' });
      if (!res.ok) {
        console.warn('Restore failed for', sid, res.status);
        return;
      }
    } catch (err) {
      console.warn('Restore failed for', sid, err);
      return;
    }
    if (app.currentUserId) {
      await populateSessionSelect(app.currentUserId);
      const q = _getSearchQuery().trim();
      if (_getSearchMode() === 'content' && q.length >= 2) _scanMessagesForQuery(q);
    }
  }

  // ── Trigger events ──
  // Listen on the DROPDOWN WRAPPER (#session-dropdown), not the trigger button,
  // so clicks work even when CSS applies pointer-events: none to the trigger
  // (before data-loaded is set). Mirrors the agent-name dropdown pattern which
  // listens on .chat-header-name-row.
  let _lpTimer = null, _lpStartX = 0, _lpStartY = 0;
  dropdown.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.header-delete-btn, .header-plus-btn, .session-dropdown-status')) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    _lpStartX = e.clientX; _lpStartY = e.clientY;
    _lpTimer = setTimeout(() => {
      _lpTimer = null;
      e.preventDefault();
      _headerRenameSession();
    }, 500);
  });
  dropdown.addEventListener('pointermove', (e) => {
    if (!_lpTimer) return;
    if (Math.abs(e.clientX - _lpStartX) > 8 || Math.abs(e.clientY - _lpStartY) > 8) {
      clearTimeout(_lpTimer); _lpTimer = null;
    }
  });
  dropdown.addEventListener('pointerup', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
  dropdown.addEventListener('pointercancel', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
  dropdown.addEventListener('click', (e) => {
    if (e.target.closest('.header-delete-btn, .header-plus-btn')) return;
    // A rename input may have just replaced the label (via long-press), so
    // e.target could be the old detached span — check the DOM for any input.
    // The menu may be body-attached during open, so check both dropdown and menu.
    if (e.target.closest('.session-row-title-input') || dropdown.querySelector('.session-row-title-input') || menu.querySelector('.session-row-title-input')) return;
    // Kebab / delete inside the trigger: don't toggle the dropdown menu.
    if (e.target.closest('#session-dropdown-kebab, #session-dropdown-delete')) {
      e.stopPropagation();
      return;
    }
    e.stopPropagation();
    // Ignore rapid double-clicks and clicks during animation.
    const now = Date.now();
    if (now - _lastToggleTime < 350 || menu.dataset.state === 'opening' || menu.dataset.state === 'closing') return;
    _lastToggleTime = now;
    // A stale "open" can be an invisible 0-height box (the list arrived after
    // the open animation measured an empty menu). Treat it as closed so the
    // click re-opens — and re-fetches — instead of toggling nothing closed.
    const effectivelyOpen = !menu.hidden && menu.dataset.state === 'open'
      && menu.getBoundingClientRect().height > 1;
    if (!effectivelyOpen) {
      _dismissKeyboardBeforeOpen();
      openMenu();
    } else {
      closeMenu();
    }
    // Pointer clicks leave the button/span focused in some browsers, whose
    // native focus ring makes this compact control appear to grow and shift.
    // Drop pointer-acquired focus after toggling; keyboard focus is preserved.
    if (e.detail > 0) {
      e.target.closest('.session-dropdown-trigger')?.blur();
    }
  });
  dropdown.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      // Don't toggle if the focus is on the kebab, delete, or a rename input.
      if (e.target.closest('#session-dropdown-kebab, #session-dropdown-delete, .session-row-title-input')) return;
      e.preventDefault();
      // Same effectively-open test as the click handler: a stale "open" that
      // is an invisible 0-height box counts as closed so Enter re-opens it.
      const effectivelyOpen = !menu.hidden && menu.dataset.state === 'open'
        && menu.getBoundingClientRect().height > 1;
      if (menu.hidden || menu.dataset.state === 'closing' || !effectivelyOpen) openMenu(); else closeMenu();
    }
  });
  // ── Trigger kebab: open the row-actions popup (pin/unpin) ──────────
  // Prefer the ids from the static/element markup; fall back to the kebab
  // inside the trigger (legacy chat-controls markup has no ids).
  const triggerKebab = root.querySelector('#session-dropdown-kebab')
    || root.querySelector(':scope > .session-dropdown-trigger .session-row-kebab');
  const triggerDelete = root.querySelector('#session-dropdown-delete')
    || root.querySelector(':scope > .session-dropdown-trigger .session-row-delete');
  if (triggerKebab) {
    triggerKebab.addEventListener('click', (e) => {
      e.stopPropagation();
      const sid = triggerKebab.dataset.id || app.currentSessionId;
      const open = document.querySelector('.session-row-actions[data-source="session-list"]');
      if (open && open.dataset.id === sid) { _closeRowActions(); return; }
      _closeRowActions();
      if (sid) _openRowActions(sid, triggerKebab);
    });
  }
  if (triggerDelete) {
    triggerDelete.addEventListener('click', (e) => {
      e.stopPropagation();
      const sid = triggerDelete.dataset.id || app.currentSessionId;
      if (sid) handleDeleteClick(triggerDelete, sid);
    });
  }

  // ── Per-row "more" (⋯) popup: pin/unpin toggle ─────────────────────────────
  // Body-mounted floating menu anchored under the kebab. Mirrors the genui page
  // kebab popup (.session-row-actions / .session-row-action). The row's
  // Agent / Session id (compact, click-to-copy the full id — same helper as the
  // chat header more menu) sits at the BOTTOM, under the actions; the two-click
  // delete stays on the row's own trash button.
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
    const row = kebabBtn.closest('.session-row');
    const popup = document.createElement('div');
    popup.className = 'session-row-actions';
    popup.dataset.id = sid;
    popup.dataset.source = 'session-list';
    popup.innerHTML =
      `<button class="session-row-action" data-action="pin">${icon(pinned ? 'pin-off' : 'pin', { size: '14px' })} ${pinned ? 'Unpin session' : 'Pin session'}</button>` +
      `<button class="session-row-action" data-action="auto-rename">${icon('wand-2', { size: '14px' })} Auto rename</button>` +
      `<button class="session-row-action" data-action="rename">${icon('pencil', { size: '14px' })} Rename session</button>` +
      `<button class="session-row-action danger" data-action="delete">${icon('trash-2', { size: '14px' })} Delete session</button>` +
      `<button class="session-row-action disabled" data-action="sync" disabled title="Under development">${icon('refresh-cw', { size: '14px' })} Sync<span class="session-row-action-tag">Under development</span></button>`;
    // Agent / Session id rows — compact "A: <first8>" / "S: <first8>" at the
    // bottom of the popup, laid out like the action buttons above (label at
    // the icon column, id left-aligned at the description column — one
    // continuous string, no pill container). Click copies the FULL id; the
    // confirmation shows the full word ("Agent ID copied ✓"), mirroring the
    // chat header more menu (session-init.js).
    const info = document.createElement('div');
    info.className = 'session-row-actions-info';
    const agentId = (sess && sess.agent_id) || app.currentAgentId || '';
    const sessionId = sid || '';
    const mkIdRow = (label, fullLabel, v) => {
      const r = document.createElement('div');
      r.className = 'session-row-action session-row-id';
      r.title = `Copy ${fullLabel} id`;
      r.tabIndex = 0;
      const key = document.createElement('span');
      key.className = 'session-row-id-key';
      key.textContent = label;
      const val = document.createElement('span');
      val.className = 'session-row-id-val';
      val.textContent = v ? v.slice(0, 8) : '—';
      r.append(key, val);

      // Click-to-copy the FULL id value with green check feedback
      r.addEventListener('click', (e) => {
        e.stopPropagation();
        copyText(v).then(() => {
          r.classList.add('copied');
          const orig = val.textContent;
          val.textContent = `${fullLabel} ID copied \u2713`;
          setTimeout(() => {
            r.classList.remove('copied');
            val.textContent = orig;
          }, 1200);
        }).catch(() => {});
      });

      return r;
    };
    info.append(mkIdRow('A:', 'Agent', agentId), mkIdRow('S:', 'Session', sessionId));
    popup.appendChild(info);
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
      else if (actionBtn.dataset.action === 'auto-rename') {
        _closeRowActions();
        console.log('[auto-rename] clicked for session', sid);
        // Commonized feedback: the header trigger and the dropdown rows share
        // one look — the agent icon stays put and the session NAME is swapped
        // for a spinner + "Renaming…" in both places.
        const titleSpan = row && row.querySelector('.session-row-title');
        const origText = titleSpan ? (titleSpan.textContent || '').trim() : '';
        const dropdown = document.getElementById('session-dropdown');
        const headerLabel = document.getElementById('session-dropdown-label');
        const isCurrent = sid === app.currentSessionId;
        _setRenamingState(titleSpan, true);
        if (isCurrent) {
          if (dropdown) dropdown.dataset.titling = 'true';
          _setRenamingState(headerLabel, true);
        }
        autoRenameSession(sid).then((r) => {
          const newTitle = r && r.title ? String(r.title).trim() : '';
          // Success only if the server produced a NEW name — an unchanged title
          // (the LLM returned nothing) is a failure, not a silent no-op.
          const changed = r.ok && newTitle && newTitle !== origText;
          if (changed) {
            // Swap in the new name immediately (icon kept) — don't wait for
            // the next populateSessionSelect cycle.
            if (titleSpan) {
              _setRenamingState(titleSpan, false);
              _setSessionName(titleSpan, newTitle);
              titleSpan.title = 'Hold to rename';
            }
            // Also update the cache entry so the header label + any other
            // view sees it on the next render.
            const sess = _sessionsCache.find(s => s.id === sid);
            if (sess) sess.title = newTitle;
            if (isCurrent) {
              if (dropdown) delete dropdown.dataset.titling;
              _clearRenamingSaved(headerLabel);
              _setTriggerLabel();
            }
          } else {
            console.log('[auto-rename] failed — result:', r);
            // Restore the original title, then flash the error briefly in its
            // place (~1s) before the old name comes back.
            if (titleSpan) _setRenamingState(titleSpan, false);
            if (isCurrent) {
              if (dropdown) delete dropdown.dataset.titling;
              _clearRenamingSaved(headerLabel);
              _setTriggerLabel();
            }
            if (row) {
              const msg = r.message || r.error || (newTitle ? 'Name unchanged' : 'Could not generate a name');
              _showRowError(row, msg, 1200);
            }
          }
        }).catch((_err) => {
          if (titleSpan) _setRenamingState(titleSpan, false);
          if (isCurrent) {
            if (dropdown) delete dropdown.dataset.titling;
            _clearRenamingSaved(headerLabel);
            _setTriggerLabel();
          }
          if (row) _showRowError(row, 'Auto rename failed', 1200);
        });
      }
      else if (actionBtn.dataset.action === 'rename') { if (row) { _closeRowActions(); startRename(sid, row); } else { _headerRenameSession(); _closeRowActions(); } }
      else if (actionBtn.dataset.action === 'delete') { _closeRowActions(); deleteSession(sid, { retries: 1 }); }
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
      // Footer: checkbox mode toggle — adds checkboxes to each row for batch delete.
      const checkboxToggle = e.target.closest('.session-manage-checkbox-toggle');
      if (checkboxToggle) {
        _setCheckboxMode(!_getCheckboxMode());
        _clearSelected();
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        return;
      }
      // Footer: delete selected sessions (two-click confirm).
      const delSelectedBtn = e.target.closest('.session-manage-delete-selected');
      if (delSelectedBtn) {
        handleDeleteSelectedClick(delSelectedBtn);
        return;
      }
      // Footer: recycle-bin toggle — narrows the list to binned sessions.
      const binBtn = e.target.closest('.session-manage-bin');
      if (binBtn) {
        _setBinMode(!_getBinMode());
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        return;
      }
      // Footer: search-mode chip — Title ⇄ Content.
      const modeBtn = e.target.closest('.session-manage-search-mode');
      if (modeBtn) {
        _setSearchMode(_getSearchMode() === 'content' ? 'title' : 'content');
        _clearMsgSearch();
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        const q = _getSearchQuery().trim();
        if (_getSearchMode() === 'content' && q.length >= 2) _scanMessagesForQuery(q);
        return;
      }
      // Footer: clear-search button (×) — empties the query and refetches.
      const clearBtn = e.target.closest('.session-manage-search-clear');
      if (clearBtn) {
        _setSearchQuery('');
        _clearMsgSearch();
        const input = menu.querySelector('.session-manage-search-input');
        if (input) { input.value = ''; input.focus(); }
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        return;
      }
      // Bin badge on a recycled row — two-click restore (mirrors the Sessions
      // page chip): first click arms ("Restore?"), second restores. Caught
      // before the row-switch handler so clicking it never opens the session.
      const binChip = e.target.closest('.session-row-bin');
      if (binChip) {
        const sid = binChip.dataset.id;
        if (!sid) return;
        if (!binChip.classList.contains('armed')) {
          binChip.classList.add('armed');
          binChip.textContent = 'Restore?';
          binChip.title = 'Click again to restore this session';
          clearTimeout(binChip._disarmTimer);
          binChip._disarmTimer = setTimeout(() => {
            binChip.classList.remove('armed');
            binChip.textContent = 'bin';
            binChip.title = 'In recycling bin — click to restore';
          }, 3000);
          return;
        }
        clearTimeout(binChip._disarmTimer);
        binChip.classList.remove('armed');
        binChip.textContent = 'bin';
        binChip.title = 'In recycling bin — click to restore';
        _restoreFromBin(sid);
        return;
      }
      // Per-row visibility (eye) toggle in manage mode.
      const visBtn = e.target.closest('.session-row-visibility');
      if (visBtn) {
        const sid = visBtn.dataset.id;
        if (sid) toggleHidden(sid);
        return;
      }
      // Per-row checkbox toggle in checkbox mode.
      const rowCheckbox = e.target.closest('.session-row-checkbox');
      if (rowCheckbox) {
        const sid = rowCheckbox.dataset.id;
        if (_selectedSessions.has(sid)) {
          _selectedSessions.delete(sid);
        } else {
          _selectedSessions.add(sid);
        }
        _renderSessionRows();
        return;
      }
      // Expand/collapse a family-root row (orchestrator). Optimizer sessions
      // are top-level rows of their own, so only spawn families get carets.
      // Caught before the row-switch below so toggling the tree doesn't also
      // open the parent session.
      const expandBtn = e.target.closest('.session-row-expand');
      if (expandBtn) {
        toggleSessionGroup(expandBtn.dataset.id);
        return;
      }
      if (e.target.closest('.session-row-title-input')) return;
      // In checkbox mode, clicking a row toggles its checkbox instead of switching.
      const row = e.target.closest('.session-row');
      if (row) {
        // Recycled rows are restore-only (via their bin badge) — never switch
        // into a binned session from the dropdown.
        if (row.classList.contains('recycled')) return;
        if (_getCheckboxMode()) {
          const sid = row.dataset.id;
          if (_selectedSessions.has(sid)) {
            _selectedSessions.delete(sid);
          } else {
            _selectedSessions.add(sid);
          }
          _renderSessionRows();
          return;
        }
        switchToSession(row.dataset.id); closeMenu();
      }
    });
    // Search bar (footer): debounced input → refetch + content scan. The menu
    // is re-rendered wholesale on every poll/refetch, so listen on the menu
    // container and look up the live input each time. The debounce lives in
    // list.js so any query change (including clearing via ×/Escape) cancels a
    // pending commit — a stale timer must never resurrect an old query after
    // the user cleared the box.
    menu.addEventListener('input', (e) => {
      const inp = e.target.closest('.session-manage-search-input');
      if (!inp) return;
      _scheduleSearchCommit(inp.value);
    });
    // Typing inside the search field must not toggle the menu (the dropdown
    // wrapper's Enter/Space handler never sees it — the menu is body-attached —
    // but the document Escape handler would close it). Escape clears the query
    // first, then closes on a second press.
    menu.addEventListener('keydown', (e) => {
      const inp = e.target.closest('.session-manage-search-input');
      if (!inp) return;
      e.stopPropagation();
      if (e.key === 'Escape') {
        if (inp.value) {
          inp.value = '';
          _setSearchQuery('');
          _clearMsgSearch();
          if (app.currentUserId) populateSessionSelect(app.currentUserId);
        } else {
          closeMenu();
        }
      }
    });
    makeRowsReorderable(menu, {
      rowSelector: '.session-row.pinned:not(.session-child-row)',
      handleSelector: '.session-row.pinned:not(.session-child-row)',
      ignoreSelector: '.session-row-delete, .session-row-kebab, .session-row-title-input, .session-row-visibility, .session-row-expand, .session-row-checkbox',
      onReorder: async (orderedIds) => {
        orderedIds.forEach((id, index) => {
          const session = _sessionsCache.find(s => s.id === id);
          if (session) session.sort_order = index;
        });
        _renderSessionRows();
        _setTriggerLabel();
        try {
          await persistSessionOrder(app.currentUserId, orderedIds);
        } catch (e) {
          console.warn('Failed to persist pinned session order:', e);
          // The optimistic DOM order is no longer trustworthy. Reload the local
          // source of truth (which is also the hybrid mirror) instead of leaving
          // a layout that silently snaps back on the next poll/restart.
          await populateSessionSelect(app.currentUserId);
        }
      },
    });
    attachRowLongPress(menu, {
      rowSelector: '.session-row:not(.session-child-row)',
      ignoreSelector: '.session-row-delete, .session-row-kebab, .session-row-title-input',
      // Only a long-press ON THE SESSION NAME opens rename — not the pin icon,
      // status dot, caret, or empty row gaps.
      requireSelector: '.session-row-title',
      onLongPress: (sid, row) => startRename(sid, row),
    });
  }

  // Guard: a rename input (from long-press) replaces its span with replaceWith(),
  // detaching the old span from the DOM. The click/focusin event that follows can
  // carry the detached span as e.target, which fails Node.contains(). Check the DOM.
  function _hasActiveRenameInput() {
    return !!(dropdown.querySelector('.session-row-title-input') || menu.querySelector('.session-row-title-input'));
  }

  const onDocClick = (e) => {
    // A stale mount's menu may have been replaced by a re-applied control —
    // only close menus that are actually in the document.
    if (!document.body.contains(menu)) return;
    if (_hasActiveRenameInput()) return;
    if (e.target.closest('.session-row-actions')) return;
    if (dropdown && !dropdown.contains(e.target) && !menu.contains(e.target)) closeMenu();
  };
  // Close the menu when focus lands outside the dropdown (e.g. clicking the
  // chat input pill).  The focus event fires before the footer-mode rebuild
  // in switchFooterMode, which can hide the target element and prevent the
  // click event from reaching the document handler above.
  const onDocFocusIn = (e) => {
    if (!document.body.contains(menu)) return;
    if (_hasActiveRenameInput()) return;
    if (e.target.closest('.session-row-actions')) return;
    if (dropdown && !dropdown.contains(e.target) && !menu.contains(e.target)) closeMenu();
  };
  const onDocKeydown = (e) => {
    if (!document.body.contains(menu)) return;
    if (e.key === 'Escape' && menu && !menu.hidden) closeMenu();
  };

  document.addEventListener('click', onDocClick);
  document.addEventListener('focusin', onDocFocusIn);
  document.addEventListener('keydown', onDocKeydown);

  function destroy() {
    _openRequestId++;               // invalidate any in-flight open
    closeMenu();
    document.removeEventListener('click', onDocClick);
    document.removeEventListener('focusin', onDocFocusIn);
    document.removeEventListener('keydown', onDocKeydown);
    _closeRowActions();
    if (root._sessionDropdownController === handle) root._sessionDropdownController = null;
  }

  const handle = {
    open: () => { openMenu(); },
    close: () => { closeMenu(); },
    isOpen: () => !!menu && !menu.hidden,
    destroy,
  };
  root._sessionDropdownController = handle;
  _mounts.set(root, { handle, destroy });
  return handle;
}

export function unmountSessionDropdown(root) {
  const m = root && _mounts.get(root);
  if (m) {
    m.destroy();
    _mounts.delete(root);
  }
}
