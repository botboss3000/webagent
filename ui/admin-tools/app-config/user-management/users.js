'use strict';

/**
 * Users tab — User CRUD, access modes, approve/promote/delete.
 */

import { apiPath } from '../../../shared/js/config.js';
import { isAdmin } from '../../../shared/js/left-login.js';
import { registerSectionHook } from '../nav.js';
import { _fetch, _qs, _esc, _fmtDate } from '../utils.js';

export function init() {
  _qs('ac-um-save')?.addEventListener('click', _saveUserManagement);
  _qs('ac-um-users-header')?.addEventListener('click', _toggleUsersCard);
  _qs('ac-um-access-header')?.addEventListener('click', _toggleAccessModeCard);
  // Re-sync the access-mode radio from the server every time this section is
  // shown — not just on whole-view activation. Without this, navigating to the
  // Users tab leaves the radio at whatever it was last set to (or the HTML
  // default "Private"), so a saved change can appear to "revert" on the next
  // visit even though the server has the new value. Re-reading on show makes the
  // displayed mode always match what's actually persisted.
  registerSectionHook('user-management', () => { _refreshAccessMode(); });
}

export async function load() {
  await _refreshAccessMode();
  if (isAdmin()) {
    await _loadUsersList();
  }
}

/** Set the access-mode radio group to `mode` (selecting nothing if unknown). */
function _applyAccessMode(mode) {
  const radios = document.querySelectorAll('input[name="ac-um-access-mode"]');
  let matched = false;
  radios.forEach(r => { r.checked = (r.value === mode); if (r.checked) matched = true; });
  return matched;
}

/** Read the persisted access_mode from the server and reflect it in the radio.
 *  Uses no-store so a stale HTTP/SW cache can never feed an old value, and on
 *  failure leaves a visible note rather than silently showing the HTML default
 *  (which would always read as "Private" and look like the setting reverted). */
async function _refreshAccessMode() {
  const statusEl = _qs('ac-um-status');
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const mode = data.access_mode;
    if (!mode) throw new Error('no access_mode in response');
    _applyAccessMode(mode);
  } catch (e) {
    console.warn('user-management: could not load access mode', e);
    if (statusEl) {
      statusEl.textContent = 'Could not load the current access mode — try again.';
      statusEl.style.color = 'var(--danger)';
      statusEl.style.display = 'inline';
    }
  }
}

function _toggleAccessModeCard() {
  const body = _qs('ac-um-access-body');
  const chev = _qs('ac-um-access-chevron');
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  if (chev) chev.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
}

async function _saveUserManagement() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const selected = document.querySelector('input[name="ac-um-access-mode"]:checked');
  const mode = selected ? selected.value : 'admin_approval';
  const extendCb = _qs('ac-extend-llm-to-agents');

  const statusEl = _qs('ac-um-status');
  if (statusEl) {
    statusEl.textContent = 'Saving…';
    statusEl.style.color = 'var(--fg-muted)';
    statusEl.style.display = 'inline';
  }
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        access_mode: mode,
        extend_llm_to_agents: extendCb ? extendCb.checked : true,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    // The endpoint returns the persisted (and normalized) settings. Confirm the
    // change actually took before saying "Saved" — and reflect the server's
    // authoritative value back into the radio, so the displayed mode is exactly
    // what's stored (never a premature confirmation that later "reverts").
    const saved = await res.json();
    const confirmed = saved && saved.access_mode;
    if (confirmed) _applyAccessMode(confirmed);
    if (confirmed !== mode) {
      throw new Error(confirmed ? `server stored "${confirmed}"` : 'not confirmed by server');
    }
    if (statusEl) {
      statusEl.textContent = 'Saved';
      statusEl.style.color = 'var(--success)';
      statusEl.style.display = 'inline';
      setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
    }
    try {
      window.dispatchEvent(new CustomEvent('access-mode-changed', { detail: { access_mode: confirmed } }));
    } catch {}
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Error: ${e.message}`;
      statusEl.style.color = 'var(--danger)';
      statusEl.style.display = 'inline';
    }
  }
}

function _toggleUsersCard() {
  const body = _qs('ac-um-users-body');
  const chev = _qs('ac-um-users-chevron');
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  if (chev) chev.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
  if (open) _loadUsersList();
}

async function _loadUsersList() {
  const tbody = _qs('ac-um-users-tbody');
  const summary = _qs('ac-um-users-summary');
  if (!tbody) return;

  const userId = localStorage.getItem('auth_user_id') || '';
  if (!userId) {
    tbody.innerHTML = `<tr><td colspan="8" class="ac-table-empty">Sign in as admin to view users.</td></tr>`;
    if (summary) summary.textContent = '—';
    return;
  }

  try {
    const res = await _fetch(apiPath('/admin/users/stats?requesting_user_id=' + encodeURIComponent(userId)));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const users = data.users || [];

    const pending = users.filter(u => !u.is_approved).length;
    if (summary) {
      summary.textContent = `${users.length} user${users.length !== 1 ? 's' : ''}` +
        (pending ? ` — ${pending} awaiting approval` : '');
    }

    if (!users.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="ac-table-empty">No registered users.</td></tr>`;
      return;
    }

    tbody.innerHTML = users.map(u => {
      const role = u.is_admin
        ? `<span class="ac-um-badge ac-um-badge-admin">admin</span>`
        : `<span class="ac-um-badge ac-um-badge-member">member</span>`;
      const status = u.is_approved
        ? `<span class="ac-um-badge ac-um-badge-approved">active</span>`
        : `<span class="ac-um-badge ac-um-badge-pending">pending</span>`;
      const isSelf = u.user_id === userId;
      const isBuiltinAdmin = u.username === 'admin';

      let actions = '';
      if (!u.is_approved) {
        actions += `<button class="ac-um-action-btn" data-act="approve" data-uid="${_esc(u.user_id)}" title="Authorize this account to sign in">Authorize</button>`;
      } else if (!isSelf && !isBuiltinAdmin) {
        actions += `<button class="ac-um-action-btn" data-act="revoke" data-uid="${_esc(u.user_id)}" title="Restrict this account (block sign-in)">Restrict</button>`;
      }
      if (!isSelf && !isBuiltinAdmin && u.is_approved) {
        if (u.is_admin) {
          actions += `<button class="ac-um-action-btn" data-act="demote" data-uid="${_esc(u.user_id)}" title="Remove admin role">Demote</button>`;
        } else {
          actions += `<button class="ac-um-action-btn" data-act="promote" data-uid="${_esc(u.user_id)}" title="Grant admin role">Make admin</button>`;
        }
      }
      if (!isSelf && !isBuiltinAdmin) {
        actions += `<button class="ac-um-action-btn danger" data-act="delete" data-uid="${_esc(u.user_id)}" data-name="${_esc(u.username)}">Delete</button>`;
      }
      if (!actions) actions = '<span style="color:var(--fg-3);font-size:11px;">—</span>';

      return `<tr>
        <td>
          <div style="font-weight:600;color:var(--fg-1);">${_esc(u.display_name || u.username)}</div>
          <div style="font-size:10px;color:var(--fg-3);">${_esc(u.username)}</div>
        </td>
        <td style="text-align:center;">${role}</td>
        <td style="text-align:center;">${status}</td>
        <td style="text-align:right;">${u.session_count || 0}</td>
        <td style="text-align:right;">${u.interaction_count || 0}</td>
        <td style="font-size:10px;color:var(--fg-3);">${_fmtDate(u.created_at)}</td>
        <td style="font-size:10px;color:var(--fg-3);">${_fmtDate(u.last_login_at)}</td>
        <td style="text-align:center;white-space:nowrap;">${actions}</td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('.ac-um-action-btn').forEach(btn => {
      btn.addEventListener('click', () => _userAction(btn.dataset.act, btn.dataset.uid, btn.dataset.name));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="ac-table-empty" style="color:var(--danger);">Error loading users: ${_esc(e.message)}</td></tr>`;
    if (summary) summary.textContent = 'Error';
  }
}

async function _userAction(act, uid, name) {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const requesting = localStorage.getItem('auth_user_id') || '';

  if (act === 'delete') {
    if (!confirm(`Permanently delete user "${name}"? This cannot be undone.`)) return;
    try {
      const res = await _fetch(apiPath(`/admin/users/${encodeURIComponent(uid)}?requesting_user_id=${encodeURIComponent(requesting)}`), {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) { alert('Delete failed: ' + e.message); return; }
  } else if (act === 'promote' || act === 'demote') {
    try {
      const res = await _fetch(apiPath(`/admin/users/${encodeURIComponent(uid)}/set-admin`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: requesting, is_admin: act === 'promote' }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) { alert(`${act} failed: ` + e.message); return; }
  } else {
    const path = act === 'approve' ? 'approve' : 'revoke';
    try {
      const res = await _fetch(apiPath(`/admin/users/${encodeURIComponent(uid)}/${path}`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: requesting }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) { alert(`${path} failed: ` + e.message); return; }
  }
  _loadUsersList();
}