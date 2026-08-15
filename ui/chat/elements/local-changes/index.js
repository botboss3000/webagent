'use strict';

// Chat element: local_changes — compact working-tree summary above the pill.
// Shows the same /chat-changes data as the session changes panel
// (#chat-changes-panel, wired in ui/chat/js/chat-session-changes.js) using the
// same row markup (.chat-change-row/.chat-change-path/.chat-change-stat from
// ui/shared/css/app1.css). The panel's renderer broadcasts every refresh via
// the 'chat-changes:updated' CustomEvent, so this band and the panel stay in
// lockstep from a single fetch — no duplicate polling.
// Enabled via data/config/chat_ui.json → active_footer.above_pill rows.
// The commit button (top right, admin-gated like the panel) reveals a small
// note field + send button; submitting commits exactly this session's files
// via POST /api/v1/github/chat-changes/commit, then re-refreshes.
// Band size is configurable: controls.local_changes.width / max_lines in
// chat_ui.json (applied below as --lc-width / --lc-max-lines).

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';

// ── Mobile keyboard preservation ───────────────────────────────────────────
// Taps inside this panel (grip drag/click, commit/send buttons) must not
// dismiss the keyboard while the chat pill is focused: focusable taps blur
// the pill, and some mobile browsers (older iOS Safari) ignore the
// pointerdown preventDefault that normally keeps focus put. So the panel
// cancels the focus-shift default itself AND, as a bulletproof fallback,
// hands focus back to the pill after the tap once the click has run.
// Editable fields (the commit-note input) legitimately take focus — they
// are exempt. Works alongside the global keepPillFocusOnFooterTap guard in
// ui/shared/js/dom-utils.js; the local copy is immune to zone/mount
// reordering and adds the focus-restore fallback.
const _isEditable = (el) => {
  if (!el || el.nodeType !== 1) return false;
  const tag = el.tagName.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || !!el.isContentEditable;
};
const _chatInputEl = () => (app && app.chatInput) || document.getElementById('chat-input');

export function html(cfg = {}) {
  return `<div class="chat-local-changes" data-element-name="local_changes" hidden>
    <button type="button" class="chat-local-changes-grip" title="Drag to hide or show the changes panel" aria-label="Hide or show the local changes panel" aria-expanded="true">
      <div class="chat-local-changes-grip-line"></div>
    </button>
    <div class="chat-local-changes-head">
      <span class="chat-local-changes-count">0</span>
      <button type="button" class="chat-local-changes-commit" title="Commit these files" hidden>
        <i data-lucide="git-commit-horizontal" style="width:13px;height:13px;"></i>
      </button>
    </div>
    <div class="chat-local-changes-list" aria-label="Files changed in this session"></div>
    <form class="chat-local-changes-form" hidden>
      <input type="text" class="chat-local-changes-note" placeholder="Commit note" maxlength="2000" autocomplete="off" />
      <button type="submit" class="chat-local-changes-send" title="Commit"><i data-lucide="send" style="width:12px;height:12px;"></i></button>
    </form>
    <div class="chat-local-changes-status" hidden></div>
  </div>`;
}

export function init(el, cfg = {}) {
  const list = el.querySelector('.chat-local-changes-list');
  if (!list) return;
  const count = el.querySelector('.chat-local-changes-count');
  const commitBtn = el.querySelector('.chat-local-changes-commit');
  const form = el.querySelector('.chat-local-changes-form');
  const note = el.querySelector('.chat-local-changes-note');
  const send = el.querySelector('.chat-local-changes-send');
  const status = el.querySelector('.chat-local-changes-status');
  const grip = el.querySelector('.chat-local-changes-grip');

  // ── Minimize/restore via the top grab bar ──
  // Mirrors the footer drag handle (ui/chat/js/chat-ui.js → #chat-footer-handle):
  // drag the panel's top edge UP to minimize it (the body disappears, leaving
  // only the grip line floating above the composer pill), drag DOWN or click to
  // restore. State is per-session in localStorage like chat_footer_expanded_*.
  let minimized = false;
  const _lcStorageKey = () => `chat_lc_minimized_${app.currentSessionId || ''}`;
  const _applyMinimized = (value) => {
    minimized = !!value;
    el.classList.toggle('minimized', minimized);
    if (grip) grip.setAttribute('aria-expanded', String(!minimized));
    if (minimized) {
      // The collapse hides everything but the grip. If focus was inside the
      // panel body (commit-note field / buttons), the browser blurs it and
      // the keyboard drops — re-anchor focus on the chat pill to keep the
      // keyboard engaged.
      const input = _chatInputEl();
      const ae = document.activeElement;
      if (input && ae && el.contains(ae)) input.focus({ preventScroll: true });
    }
    try { localStorage.setItem(_lcStorageKey(), minimized ? '1' : '0'); } catch (_) {}
  };
  const _syncMinimizedState = () => {
    let saved = null;
    try { saved = localStorage.getItem(_lcStorageKey()); } catch (_) {}
    _applyMinimized(saved === '1');
  };

  if (grip) {
    let _dragStartY = 0;
    let _dragWasMinimized = false;
    let _dragOffset = 0;
    const _dragThreshold = 40; // px of vertical drag to trigger minimize/restore
    const _dragMax = 80;       // px of linear travel before rubber band kicks in
    const _rubberResistance = 40;

    const _rubberBand = (value, limit, resistance) => {
      const sign = value < 0 ? -1 : 1;
      const abs = Math.abs(value);
      if (abs <= limit) return value;
      return sign * (limit + (abs - limit) * resistance / ((abs - limit) + resistance));
    };

    grip.addEventListener('pointerdown', (e) => {
      _dragStartY = e.clientY;
      _dragWasMinimized = minimized;
      _dragOffset = 0;
      grip.classList.remove('spring-back');
      grip.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    grip.addEventListener('pointermove', (e) => {
      if (!grip.hasPointerCapture(e.pointerId)) return;
      const dy = _dragStartY - e.clientY; // positive = dragging up
      const banded = _rubberBand(dy, _dragMax, _rubberResistance);
      _dragOffset = banded;
      grip.style.setProperty('--lc-grip-drag-y', (-banded) + 'px');

      // Drag up → minimize (panel slides away); drag down → restore.
      if (!_dragWasMinimized && dy > _dragThreshold) {
        _applyMinimized(true);
        _dragWasMinimized = true;
        _dragStartY = e.clientY;
      } else if (_dragWasMinimized && dy < -_dragThreshold) {
        _applyMinimized(false);
        _dragWasMinimized = false;
        _dragStartY = e.clientY;
      }
    });

    const _endGripDrag = (e) => {
      if (grip.hasPointerCapture(e.pointerId)) grip.releasePointerCapture(e.pointerId);
      grip.classList.add('spring-back');
      grip.style.removeProperty('--lc-grip-drag-y');
      _dragOffset = 0;
    };
    grip.addEventListener('pointerup', _endGripDrag);
    grip.addEventListener('pointercancel', _endGripDrag);

    // Click toggles when there was no significant drag.
    grip.addEventListener('click', (e) => {
      if (Math.abs(_dragOffset) > 4) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      e.preventDefault();
      _applyMinimized(!minimized);
    });

    _syncMinimizedState();
  }

  // ── Keep the mobile keyboard engaged while interacting with the panel ──
  // See the module-level comment above. Only focusable taps (button /
  // [role=button] / a / [tabindex]) can blur the pill, so only those are
  // intercepted; plain row taps keep working (and stay selectable).
  let _pillWasFocused = false;
  const _focusStealer = (t) => t && typeof t.closest === 'function' && t.closest('button, [role="button"], a, [tabindex]');
  const _restorePillFocus = () => {
    const input = _chatInputEl();
    if (!input || !_pillWasFocused) return;
    if (document.activeElement === input) return;
    if (_isEditable(document.activeElement)) return; // commit-note field took focus legitimately
    input.focus({ preventScroll: true });
  };
  el.addEventListener('pointerdown', (e) => {
    _pillWasFocused = !!_chatInputEl() && document.activeElement === _chatInputEl();
    if (!_pillWasFocused || _isEditable(e.target) || !_focusStealer(e.target)) return;
    e.preventDefault(); // stop the focus-shift default; the click still runs
  }, true);
  el.addEventListener('mousedown', (e) => {
    if (!_pillWasFocused || _isEditable(e.target) || !_focusStealer(e.target)) return;
    e.preventDefault(); // desktop browsers shift focus on mousedown
  }, true);
  // Fallback for browsers that ignore the pointerdown cancel: the tap blurred
  // the pill anyway — put focus back now that the click has run, so the
  // keyboard stays up. Both events are listened to for idempotent coverage.
  el.addEventListener('pointerup', _restorePillFocus, true);
  el.addEventListener('click', _restorePillFocus, true);

  // Config-driven sizing — chat_ui.json → active_footer.above_pill.controls.local_changes.
  if (cfg.width) el.style.setProperty('--lc-width', cfg.width);
  if (cfg.max_lines != null) el.style.setProperty('--lc-max-lines', cfg.max_lines);

  let files = [];
  let canCommit = false;
  let isAdmin = false;

  const setStatus = (text, isError = false) => {
    if (!status) return;
    status.textContent = text || '';
    status.hidden = !text;
    status.classList.toggle('error', !!isError);
  };

  const render = (next = []) => {
    files = next || [];
    list.replaceChildren();
    if (!files.length) {
      el.hidden = true;
      return;
    }
    for (const file of files) {
      const row = document.createElement('div');
      row.className = 'chat-change-row';
      const path = document.createElement('span');
      path.className = 'chat-change-path';
      path.textContent = file.path;
      const stat = document.createElement('span');
      stat.className = 'chat-change-stat';
      if (file.conflict) {
        stat.textContent = 'shared';
      } else if (!file.added && !file.removed) {
        stat.textContent = 'changed';
      } else {
        const add = document.createElement('span');
        add.className = 'chat-change-stat-add';
        add.textContent = file.added ? `+${file.added}` : '';
        const del = document.createElement('span');
        del.className = 'chat-change-stat-del';
        del.textContent = file.removed ? `-${file.removed}` : '';
        stat.append(add);
        if (file.added && file.removed) stat.append(' ');
        stat.append(del);
      }
      row.classList.toggle('conflict', !!file.conflict);
      row.append(path, stat);
      list.append(row);
    }
    canCommit = files.some((f) => !f.conflict);
    if (count) count.textContent = String(files.length);
    if (commitBtn) commitBtn.hidden = !(isAdmin && canCommit);
    el.hidden = false;
  };

  // Admin gate — the commit endpoint requires admin (same check the session
  // changes panel uses in initSessionChanges before enabling anything).
  fetch('/api/v1/github/check-access', { headers: { ...authHeaders() } })
    .then((r) => r.json().catch(() => ({})))
    .then((d) => {
      isAdmin = !!d.is_admin;
      if (commitBtn) commitBtn.hidden = !(isAdmin && canCommit);
    })
    .catch(() => {});

  commitBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!form) return;
    form.hidden = !form.hidden;
    if (!form.hidden) {
      setStatus('');
      note?.focus();
    }
  });

  form?.addEventListener('submit', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!canCommit || !app.currentSessionId) return;
    if (send) send.disabled = true;
    try {
      const res = await fetch('/api/v1/github/chat-changes/commit', {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: app.currentSessionId,
          paths: files.filter((f) => !f.conflict).map((f) => f.path),
          message: note?.value || '',
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || body.message || `Commit failed (${res.status})`);
      setStatus(body.status === 'committed' ? `Committed ${body.hash}.` : (body.message || 'Nothing to commit.'));
      if (note) note.value = '';
      if (form) form.hidden = true;
      if (typeof app.refreshSessionChanges === 'function') {
        try { await app.refreshSessionChanges(); } catch (_) {}
      }
    } catch (err) {
      setStatus(err.message || 'Commit failed.', true);
    } finally {
      if (send) send.disabled = false;
    }
  });

  let received = false;
  const onUpdated = (e) => {
    received = true;
    // Re-read per-session state — keeps the minimize preference in sync when
    // the active session changes (the element persists across session switches).
    _syncMinimizedState();
    render(e.detail && e.detail.files);
  };
  document.addEventListener('chat-changes:updated', onUpdated);

  // Boot-order backstop: initSessionChanges (ui/chat/js/chat-session-changes.js)
  // starts before this element mounts, so a restored session's first render may
  // fire before we're listening. If no event has arrived shortly after mount,
  // pull once via the panel's own refresh — it re-renders and re-broadcasts.
  let destroyed = false;
  const backstop = setTimeout(() => {
    if (!destroyed && !received && typeof app.refreshSessionChanges === 'function') {
      try { app.refreshSessionChanges(); } catch (_) {}
    }
  }, 500);

  el.__localChangesCleanup = () => {
    destroyed = true;
    clearTimeout(backstop);
    document.removeEventListener('chat-changes:updated', onUpdated);
  };
}

export function destroy(el) {
  if (typeof el.__localChangesCleanup === 'function') el.__localChangesCleanup();
}

export function style() { return ''; }
