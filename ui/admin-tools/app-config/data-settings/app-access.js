'use strict';

/**
 * App Access — App Configuration → Data Settings → App Access card.
 *
 * Chooses how new users get into this instance (the "access mode"): Private
 * (admin approval) vs Open Registration. Uses the SAME backend the old Users-page
 * Access Mode form used — GET/POST /admin/settings/app (the `access_mode` flag,
 * canonical values `admin_approval` / `public_registered`) — moved here so the
 * Users page keeps only Social Sign-In + the user list.
 *
 * The two modes render as a radio-variant of the shared toggle-list: a `.ac-list`
 * of `.ac-ability-row`s with a radio on the right (see docs/claude/ui-guidance.md
 * "Toggle-list"). Picking a mode AUTO-SAVES with the unified on-top save overlay
 * (spinner → ✓ / ⚠-with-revert), mirroring the Models table + abilities tree — no
 * Save button. POST merges server-side, so sending only `access_mode` preserves
 * every other app setting. Admin-only; a non-admin selection is reverted.
 */

import { apiPath } from '../../../shared/js/config.js';
import { isAdmin, showRestrictedModal } from '../../../shared/js/left-login.js';
import { _fetch, _qs, _esc, _fmtDate } from '../utils.js';
import { _markSaving, _flashSaveCheck } from '../../../shared/js/dom-utils.js';

const _MODE_LABEL = { admin_approval: 'Private', public_registered: 'Open' };

let _current = null;   // last-confirmed mode, used to revert a failed/denied change

function _radios() { return document.querySelectorAll('input[name="ac-access-mode"]'); }

/** Reflect `mode` into the radios + the heading badge; remember it as current. */
function _apply(mode) {
  let matched = false;
  _radios().forEach(r => { r.checked = (r.value === mode); if (r.checked) matched = true; });
  const badge = _qs('ac-access-badge');
  if (badge) badge.textContent = _MODE_LABEL[mode] || '—';
  if (matched) _current = mode;
  return matched;
}

/** Read the persisted access_mode and reflect it. no-store so a stale HTTP/SW
 *  cache can never feed an old value; on failure the badge says "unavailable"
 *  rather than silently showing the HTML default. */
async function _load() {
  const badge = _qs('ac-access-badge');
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (!data.access_mode) throw new Error('no access_mode in response');
    _apply(data.access_mode);
    _updatePendingBadge();
  } catch (e) {
    console.warn('app-access: could not load access mode', e);
    if (badge) badge.textContent = 'unavailable';
  }
}

async function _onChange(e) {
  const radio = e.target;
  if (!radio || radio.name !== 'ac-access-mode') return;
  const mode = radio.value;
  const cell = radio.closest('.ac-ability-status') || radio.parentElement;
  if (!isAdmin()) {
    showRestrictedModal();
    _apply(_current || 'admin_approval');   // revert the visual selection
    return;
  }
  _markSaving(cell);
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ access_mode: mode }),   // partial update — merged server-side
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    // The endpoint returns the persisted (normalised) settings — reflect the
    // server's authoritative value so the shown mode is exactly what's stored.
    const saved = await res.json();
    const confirmed = saved && saved.access_mode;
    if (!confirmed) throw new Error('not confirmed by server');
    _apply(confirmed);
    _flashSaveCheck(cell, true);
    try { window.dispatchEvent(new CustomEvent('access-mode-changed', { detail: { access_mode: confirmed } })); } catch {}
  } catch (err) {
    _apply(_current || 'admin_approval');   // revert to the last-confirmed mode
    _flashSaveCheck(cell, false, err.message);
  }
}

// ── Pending-users panel (Private row expandable body) ─────────────────────

function _wirePrivateRow() {
  const row = document.getElementById('ac-row-private');
  const head = document.getElementById('ac-private-head');
  const chevron = document.getElementById('ac-private-chevron');
  if (!row || !head || !chevron) return;

  // Chevron click — toggle expand, load list on open.
  chevron.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = row.classList.toggle('expanded');
    chevron.style.transform = open ? 'rotate(90deg)' : '';
    if (open) _loadPendingUsers();
  });

  // NOTE: the head is intentionally NOT a click target for the radio. Only the
  // radio circle itself selects the mode — clicking the row body (label/desc)
  // does nothing so the whole bar isn't a hidden toggle.
}

/** "Sign in options" sub-head — expand/collapse the social-provider list below.
 *  Toggling `.expanded` on the head drives the adjacent-sibling reveal + the
 *  left-chevron rotate (app3.css). Collapsed by default. */
function _wireSocialHead() {
  const head = document.getElementById('ac-social-head');
  if (!head || head.dataset.wired) return;
  head.dataset.wired = '1';
  const toggle = () => {
    const open = head.classList.toggle('expanded');
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  head.addEventListener('click', toggle);
  head.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });
}

async function _updatePendingBadge() {
  const badge = document.getElementById('ac-private-pending-badge');
  if (!badge) return;
  const userId = localStorage.getItem('auth_user_id') || '';
  if (!userId) return;
  try {
    const res = await _fetch(apiPath('/admin/users/stats?requesting_user_id=' + encodeURIComponent(userId)));
    if (!res.ok) return;
    const data = await res.json();
    const n = (data.users || []).filter(u => !u.is_approved).length;
    badge.textContent = n;
    badge.style.display = n ? '' : 'none';
    // If the body is already open, refresh the rendered list too.
    const row = document.getElementById('ac-row-private');
    if (row && row.classList.contains('expanded')) _loadPendingUsers();
  } catch (_) {}
}

async function _loadPendingUsers() {
  const list = document.getElementById('ac-private-users-list');
  const badge = document.getElementById('ac-private-pending-badge');
  if (!list) return;
  const userId = localStorage.getItem('auth_user_id') || '';
  list.innerHTML = '<div class="ac-hint" style="padding:6px 0;">Loading…</div>';
  try {
    const res = await _fetch(apiPath('/admin/users/stats?requesting_user_id=' + encodeURIComponent(userId)));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const pending = (data.users || []).filter(u => !u.is_approved);

    if (badge) { badge.textContent = pending.length; badge.style.display = pending.length ? '' : 'none'; }

    if (!pending.length) {
      list.innerHTML = '<div class="ac-hint" style="padding:6px 0;color:var(--fg-3);">No users awaiting approval.</div>';
      return;
    }

    list.innerHTML = pending.map(u => `
      <div class="ac-ability-row" style="padding:8px 0;gap:10px;">
        <div class="ac-ability-label">
          <div class="ac-ability-name" style="font-size:13px;">${_esc(u.display_name || u.username)}</div>
          <div class="ac-ability-desc">${_esc(u.username)} · registered ${_fmtDate(u.created_at)}</div>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0;">
          <button class="ac-btn ac-btn-primary ac-prv-approve" data-uid="${_esc(u.user_id)}">Authorize</button>
          <button class="ac-btn ac-prv-reject" data-uid="${_esc(u.user_id)}" data-name="${_esc(u.username)}">Reject</button>
        </div>
      </div>`).join('');

    list.querySelectorAll('.ac-prv-approve').forEach(btn =>
      btn.addEventListener('click', () => _pendingAction('approve', btn.dataset.uid)));
    list.querySelectorAll('.ac-prv-reject').forEach(btn =>
      btn.addEventListener('click', () => _pendingAction('delete', btn.dataset.uid, btn.dataset.name)));
  } catch (e) {
    list.innerHTML = `<div class="ac-hint" style="color:var(--danger);">Error: ${_esc(e.message)}</div>`;
  }
}

async function _pendingAction(act, uid, name) {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const requesting = localStorage.getItem('auth_user_id') || '';
  try {
    if (act === 'delete') {
      if (!confirm(`Reject and permanently delete "${name}"? This cannot be undone.`)) return;
      const res = await _fetch(apiPath(`/admin/users/${encodeURIComponent(uid)}?requesting_user_id=${encodeURIComponent(requesting)}`), { method: 'DELETE' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
    } else {
      const res = await _fetch(apiPath(`/admin/users/${encodeURIComponent(uid)}/approve`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: requesting }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
    }
  } catch (e) { alert(`Action failed: ${e.message}`); return; }
  _loadPendingUsers();
}

export function initAppAccess() {
  const list = _qs('ac-access-list');
  if (list && !list.dataset.wired) {
    list.dataset.wired = '1';
    list.addEventListener('change', _onChange);
  }
  _wirePrivateRow();
  _wireSocialHead();
  _load();
  // Re-read whenever the Data Settings section is shown (wired in nav.js), so the
  // selection always matches what's actually persisted.
  window.__refreshAppAccess = _load;
}
