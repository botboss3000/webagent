'use strict';

/**
 * Instances — Users tab ("This device").
 *
 * App-wide user management, mirroring the Agents page Members tab
 * (ui/main-panel/agents/js/tab-members.js): an access-policy card on top
 * (the app's access mode — Private / Open Registration — moved here from
 * Data Settings → App Access), then Admins and Users tables with activity
 * stats and approve / restrict / make-admin / reject actions.
 *
 * Data:
 *   GET  /admin/users/stats                 roster + stats (admin only)
 *   GET+POST /admin/settings/app            access mode (auto-save on pick)
 *   POST /admin/users/{uid}/approve|revoke  authorize / lock an account
 *   POST /admin/users/{uid}/credits         set trial / paid credit balance
 *   POST /admin/users/{uid}/set-admin       grant / revoke admin
 *   DELETE /admin/users/{uid}               reject & delete a pending account
 * All mutations are admin-only, guarded by isAdmin()/showRestrictedModal().
 *
 * Styling: users.css — mirrors the agents members-* classes, self-contained
 * in this folder. Mounted by instances.js (_onUsersTabRendered) into
 * #inst-users-host, exactly like the dashboard tab.
 */

import { apiPath } from '../../../shared/js/config.js';
import { isAdmin, showRestrictedModal } from '../../../shared/js/left-login.js';
import { _fetch, _esc, _fmtDate } from '../app-config/utils.js';

let _host = null;
let _accessMode = null;   // last-confirmed access mode (persists across re-renders)
let _creditPopover = null;
let _creditPopoverCleanup = null;

function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }

// ── Entry ────────────────────────────────────────────────────────────────
export function mountUsers(host) {
  _host = host;
  host.innerHTML = _usersSkeleton();
  _load();
}

export function unmountUsers() {
  _closeCreditPopover();
  _host = null;
}

async function _load() {
  if (!_host) return;
  try {
    const [statsRes, modeRes] = await Promise.all([
      _fetch(apiPath('/admin/users/stats?requesting_user_id=' + encodeURIComponent(_uid()))),
      _fetch(apiPath('/admin/settings/app'), { cache: 'no-store' }),
    ]);
    if (!statsRes.ok) throw new Error('HTTP ' + statsRes.status);
    let mode = _accessMode;
    if (modeRes.ok) {
      try { const m = await modeRes.json(); if (m && m.access_mode) mode = m.access_mode; } catch {}
    }
    _accessMode = mode;
    const data = await statsRes.json();
    if (!_host) return;
    _render(data.users || [], data.anonymous_users || []);
  } catch (e) {
    if (_host) _host.innerHTML = '<div class="members-loading" style="color:var(--danger)">Failed to load users: ' + _esc(e.message) + '</div>';
  }
}

function _render(users, anonymousUsers) {
  _closeCreditPopover();
  _host.innerHTML = '';
  _host.appendChild(_buildAccessPolicyControl());
  const notice = document.createElement('div'); notice.className = 'members-notice';
  notice.textContent = 'Activity counts reflect this instance only. Billing credits are app-wide; 1 credit equals 1 cent of usage.';
  _host.appendChild(notice);
  const admins = users.filter(u => u.is_admin);
  const members = users.filter(u => !u.is_admin);
  _host.appendChild(_buildUsersSection('Admins', admins, 'admin'));
  _host.appendChild(_buildUsersSection('Users', members, 'member'));
  _host.appendChild(_buildAnonymousSection(anonymousUsers));
}

// ── Access policy card (the app's access mode) ────────────────────────────
// Mirrors the agents Members tab's policy control: radio cards that auto-save.
// Private = admin approval (pending users appear in the Users table below),
// Open Registration = anyone can join + sign in. Same backend the old
// Data Settings → App Access form used (GET/POST /admin/settings/app).
function _buildAccessPolicyControl() {
  const wrap = document.createElement('div'); wrap.className = 'members-policy';
  const opts = [
    ['admin_approval', 'Private', 'New users register but cannot sign in until an admin approves them.'],
    ['public_registered', 'Open Registration', 'Anyone can join and sign in themselves.'],
  ];
  const title = document.createElement('div'); title.className = 'members-policy-title';
  title.textContent = 'Access policy';
  wrap.appendChild(title);
  const choices = document.createElement('div'); choices.className = 'members-policy-choices';
  for (const [val, label, hint] of opts) {
    const optEl = document.createElement('label'); optEl.className = 'members-policy-opt' + (_accessMode === val ? ' active' : '');
    optEl.innerHTML = '<input type="radio" name="inst-access-mode" value="' + _esc(val) + '"' + (_accessMode === val ? ' checked' : '') + '>'
      + '<div class="members-policy-opt-body">'
      +   '<div class="members-policy-opt-label">' + _esc(label) + '</div>'
      +   '<div class="members-policy-opt-hint">' + _esc(hint) + '</div>'
      + '</div>';
    optEl.querySelector('input').addEventListener('change', async (ev) => {
      if (!isAdmin()) { showRestrictedModal(); _load(); return; }
      const newMode = ev.target.value;
      try {
        const res = await _fetch(apiPath('/admin/settings/app'), {
          method: 'POST',
          cache: 'no-store',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ access_mode: newMode }),   // partial update — merged server-side
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const saved = await res.json();
        _accessMode = (saved && saved.access_mode) || newMode;
      } catch (e) { alert('Failed to update access policy: ' + e.message); }
      _load();   // re-render so the radios reflect the server's authoritative state
    });
    choices.appendChild(optEl);
  }
  wrap.appendChild(choices); return wrap;
}

// ── Admins / Users section tables ─────────────────────────────────────────
// Mirror _buildMembersSection in tab-members.js, app-wide: User | Sessions |
// Messages | Last login | Billing, plus Status + actions for members (Authorize /
// Restrict / Make admin / Reject) and Remove admin for admins.
function _buildUsersSection(title, rows, kind) {
  const sec = document.createElement('div'); sec.className = 'members-section';
  const header = document.createElement('div'); header.className = 'members-section-header';
  const pending = rows.filter(r => !r.is_approved).length;
  header.innerHTML = '<span class="members-section-title">' + _esc(title) + '</span>'
    + '<span class="members-section-count">' + rows.length + '</span>'
    + (pending ? '<span class="members-status pending">' + pending + ' pending</span>' : '');
  sec.appendChild(header);
  if (!rows.length) {
    const empty = document.createElement('div'); empty.className = 'members-empty';
    empty.textContent = kind === 'admin' ? 'No admins on this instance yet.' : 'No registered users yet.';
    sec.appendChild(empty); return sec;
  }
  const showStatus = kind === 'member';
  const wrap = document.createElement('div'); wrap.className = 'members-table-wrap';
  const table = document.createElement('table'); table.className = 'members-table';
  table.innerHTML = '<thead><tr><th>User</th><th class="members-num">Sessions</th><th class="members-num">Messages</th><th>Last login</th><th>Billing</th>'
    + (showStatus ? '<th>Status</th>' : '') + '<th></th></tr></thead><tbody></tbody>';
  const tbody = table.querySelector('tbody');
  const me = _uid();
  for (const r of rows) {
    const tr = document.createElement('tr');
    const name = r.display_name || r.username || r.user_id;
    const subId = r.username && r.username !== name ? r.username : r.user_id;
    const last = r.last_login_at ? _fmtDate(r.last_login_at) : '—';
    const isSelf = r.user_id === me;
    let statusHtml = '', actionHtml = '';
    if (showStatus) {
      const isAuth = !!r.is_approved;
      statusHtml = '<td><span class="members-status ' + (isAuth ? 'ok' : 'pending') + '">' + (isAuth ? 'Authorized' : 'Pending') + '</span></td>';
      actionHtml = '<td class="members-actions">'
        + (isAuth
          ? '<button type="button" class="members-btn restrict" data-act="restrict" data-uid="' + _esc(r.user_id) + '"' + (isSelf ? ' disabled title="You cannot restrict your own account"' : '') + '>Restrict</button>'
            + '<button type="button" class="members-btn make-admin" data-act="make-admin" data-uid="' + _esc(r.user_id) + '">Make admin</button>'
          : '<button type="button" class="members-btn authorize" data-act="authorize" data-uid="' + _esc(r.user_id) + '">Authorize</button>'
            + '<button type="button" class="members-btn reject" data-act="reject" data-uid="' + _esc(r.user_id) + '" data-name="' + _esc(name) + '">Reject</button>')
        + '</td>';
    } else if (!isSelf && r.username !== 'admin') {
      actionHtml = '<td class="members-actions"><button type="button" class="members-btn restrict" data-act="remove-admin" data-uid="' + _esc(r.user_id) + '">Remove admin</button></td>';
    } else {
      actionHtml = '<td class="members-actions"></td>';
    }
    const billingHtml = _billingCell(r, name);
    tr.innerHTML = '<td><div class="members-user-name">' + _esc(name) + '</div><div class="members-user-sub">' + _esc(subId) + '</div></td>'
      + '<td class="members-num">' + (r.session_count ?? 0) + '</td>'
      + '<td class="members-num">' + (r.interaction_count ?? 0) + '</td>'
      + '<td>' + _esc(last) + '</td>' + billingHtml + statusHtml + actionHtml;
    tbody.appendChild(tr);
  }
  tbody.addEventListener('click', async (ev) => {
    const creditBtn = ev.target.closest('button.members-credit');
    if (creditBtn && !creditBtn.disabled) {
      if (!isAdmin()) { showRestrictedModal(); return; }
      _openCreditPopover(creditBtn);
      return;
    }
    const btn = ev.target.closest('button.members-btn');
    if (!btn || btn.disabled) return;
    const act = btn.dataset.act;
    const uid = btn.dataset.uid;
    if (!isAdmin()) { showRestrictedModal(); return; }
    if (act === 'reject' && !confirm('Reject and permanently delete "' + (btn.dataset.name || uid) + '"? This cannot be undone.')) return;
    btn.disabled = true;
    try {
      let res;
      if (act === 'authorize' || act === 'restrict') {
        res = await _fetch(apiPath('/admin/users/' + encodeURIComponent(uid) + '/' + act), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ requesting_user_id: _uid() }),
        });
      } else if (act === 'make-admin' || act === 'remove-admin') {
        res = await _fetch(apiPath('/admin/users/' + encodeURIComponent(uid) + '/set-admin'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ requesting_user_id: _uid(), is_admin: act === 'make-admin' }),
        });
      } else if (act === 'reject') {
        res = await _fetch(apiPath('/admin/users/' + encodeURIComponent(uid) + '?requesting_user_id=' + encodeURIComponent(_uid())), { method: 'DELETE' });
      }
      if (!res || !res.ok) throw new Error(((res && await res.text()) || 'request failed') || 'HTTP error');
      _load();
    } catch (e) { alert('Action failed: ' + e.message); btn.disabled = false; }
  });
  wrap.appendChild(table); sec.appendChild(wrap); return sec;
}

function _billingCell(r, name) {
  const status = r.billing_status || 'none';
  const labels = { trial: 'Trial', paid: 'Paid', exempt: 'Exempt', none: 'No credits' };
  const paid = Math.max(0, Number(r.paid_credits) || 0);
  const trial = Math.max(0, Number(r.trial_credits) || 0);
  const uid = _esc(r.user_id);
  const safeName = _esc(name);
  const creditButton = (type, amount, label, title) => '<button type="button" class="members-credit"'
    + ' data-credit-type="' + type + '" data-current="' + amount + '" data-uid="' + uid + '" data-name="' + safeName + '"'
    + ' aria-haspopup="dialog" title="' + _esc(title) + '">' + amount.toLocaleString() + ' ' + label + '</button>';
  let credits = '';
  if (r.has_trial_grant) {
    credits += creditButton('trial', trial, 'trial credits', 'Click to set the total remaining credits across current trial grants');
  }
  credits += creditButton('paid', paid, 'paid credits', 'Click to set the purchased-credit wallet balance');
  const hold = Math.max(0, Number(r.paid_hold_credits) || 0);
  if (hold) credits += '<span class="members-credit-hold" title="Reserved by active runs">' + hold.toLocaleString() + ' reserved</span>';
  return '<td><div class="members-billing">'
    + '<span class="members-billing-status ' + _esc(status) + '">' + _esc(labels[status] || labels.none) + '</span>'
    + '<div class="members-credit-list">' + credits + '</div>'
    + '</div></td>';
}

function _buildAnonymousSection(rows) {
  const sec = document.createElement('div'); sec.className = 'members-section';
  const header = document.createElement('div'); header.className = 'members-section-header';
  header.innerHTML = '<span class="members-section-title">Anonymous users</span>'
    + '<span class="members-section-count">' + rows.length + '</span>';
  sec.appendChild(header);
  if (!rows.length) {
    const empty = document.createElement('div'); empty.className = 'members-empty';
    empty.textContent = 'No anonymous user accounts have been created yet.';
    sec.appendChild(empty); return sec;
  }
  const wrap = document.createElement('div'); wrap.className = 'members-table-wrap';
  const table = document.createElement('table'); table.className = 'members-table members-anonymous-table';
  table.innerHTML = '<thead><tr><th>Anonymous user</th><th>Channel</th><th class="members-num">Sessions</th>'
    + '<th class="members-num">Messages</th><th>Last seen</th><th>Billing</th></tr></thead><tbody></tbody>';
  const tbody = table.querySelector('tbody');
  for (const r of rows) {
    const tr = document.createElement('tr');
    const name = r.display_name || 'Anonymous user';
    const channels = (r.channels || []).join(', ') || 'Unknown';
    const tiers = (r.identity_tiers || []).map(t => t.replaceAll('_', ' ')).join(', ');
    const externalIds = (r.external_ids || []).join(', ');
    const lastSeen = r.last_seen_at ? _fmtDate(r.last_seen_at) : '—';
    tr.innerHTML = '<td><div class="members-user-name">' + _esc(name) + '</div>'
      + '<div class="members-user-sub">' + _esc(r.user_id) + '</div></td>'
      + '<td><div class="members-channel">' + _esc(channels) + '</div>'
      + (tiers ? '<div class="members-anon-tier">' + _esc(tiers) + '</div>' : '')
      + (externalIds ? '<div class="members-user-sub" title="' + _esc(externalIds) + '">' + _esc(externalIds) + '</div>' : '') + '</td>'
      + '<td class="members-num">' + (r.session_count ?? 0) + '</td>'
      + '<td class="members-num">' + (r.interaction_count ?? 0) + '</td>'
      + '<td>' + _esc(lastSeen) + '</td>'
      + _billingCell(r, name);
    tbody.appendChild(tr);
  }
  tbody.addEventListener('click', (ev) => {
    const creditBtn = ev.target.closest('button.members-credit');
    if (!creditBtn || creditBtn.disabled) return;
    if (!isAdmin()) { showRestrictedModal(); return; }
    _openCreditPopover(creditBtn);
  });
  wrap.appendChild(table); sec.appendChild(wrap); return sec;
}

function _closeCreditPopover() {
  if (_creditPopoverCleanup) _creditPopoverCleanup();
  _creditPopoverCleanup = null;
  if (_creditPopover) _creditPopover.remove();
  _creditPopover = null;
  document.querySelectorAll('.members-credit[aria-expanded="true"]').forEach(btn => btn.setAttribute('aria-expanded', 'false'));
}

function _openCreditPopover(btn) {
  if (_creditPopover && _creditPopover.dataset.uid === btn.dataset.uid
      && _creditPopover.dataset.creditType === btn.dataset.creditType) {
    _closeCreditPopover();
    return;
  }
  _closeCreditPopover();
  const type = btn.dataset.creditType;
  const current = Number(btn.dataset.current) || 0;
  const name = btn.dataset.name || btn.dataset.uid;
  const scope = type === 'trial' ? 'total remaining trial credits' : 'paid credit balance';
  const popover = document.createElement('div');
  popover.className = 'members-credit-popover';
  popover.dataset.uid = btn.dataset.uid;
  popover.dataset.creditType = type;
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-modal', 'false');
  popover.innerHTML = '<form class="members-credit-form">'
    + '<div class="members-credit-popover-head"><div><div class="members-credit-popover-title"></div>'
    + '<div class="members-credit-popover-user"></div></div>'
    + '<button type="button" class="members-credit-close" aria-label="Close">×</button></div>'
    + '<label class="members-credit-label">Credits<input class="members-credit-input" type="number" min="0" step="1" required></label>'
    + '<div class="members-credit-help">1 credit equals 1 cent of usage.</div>'
    + '<div class="members-credit-error" role="alert" hidden></div>'
    + '<div class="members-credit-popover-actions"><button type="button" class="members-credit-cancel">Cancel</button>'
    + '<button type="submit" class="members-credit-save">Save balance</button></div></form>';
  popover.querySelector('.members-credit-popover-title').textContent = type === 'trial' ? 'Adjust trial credits' : 'Adjust paid credits';
  popover.querySelector('.members-credit-popover-user').textContent = name + ' · ' + scope;
  const input = popover.querySelector('.members-credit-input');
  input.value = String(current);
  document.body.appendChild(popover);
  _creditPopover = popover;
  btn.setAttribute('aria-expanded', 'true');

  const rect = btn.getBoundingClientRect();
  const margin = 10;
  const left = Math.min(window.innerWidth - popover.offsetWidth - margin, Math.max(margin, rect.left));
  const below = rect.bottom + 6;
  const top = below + popover.offsetHeight <= window.innerHeight - margin
    ? below : Math.max(margin, rect.top - popover.offsetHeight - 6);
  popover.style.left = left + 'px';
  popover.style.top = top + 'px';

  const onOutside = (ev) => { if (!popover.contains(ev.target) && ev.target !== btn) _closeCreditPopover(); };
  const onKey = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); _closeCreditPopover(); btn.focus(); } };
  const onViewport = () => _closeCreditPopover();
  document.addEventListener('pointerdown', onOutside);
  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', onViewport);
  window.addEventListener('scroll', onViewport, true);
  _creditPopoverCleanup = () => {
    document.removeEventListener('pointerdown', onOutside);
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', onViewport);
    window.removeEventListener('scroll', onViewport, true);
  };
  popover.querySelector('.members-credit-close').addEventListener('click', _closeCreditPopover);
  popover.querySelector('.members-credit-cancel').addEventListener('click', _closeCreditPopover);
  popover.querySelector('form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    await _saveCreditAdjustment(btn, popover, input.value.trim());
  });
  input.focus(); input.select();
}

async function _saveCreditAdjustment(btn, popover, value) {
  const type = btn.dataset.creditType;
  const error = popover.querySelector('.members-credit-error');
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value))) {
    error.textContent = 'Enter a non-negative whole number of credits.';
    error.hidden = false;
    return;
  }
  const save = popover.querySelector('.members-credit-save');
  const input = popover.querySelector('.members-credit-input');
  save.disabled = true; input.disabled = true; error.hidden = true;
  try {
    const res = await _fetch(apiPath('/admin/users/' + encodeURIComponent(btn.dataset.uid) + '/credits'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        requesting_user_id: _uid(),
        credit_type: type,
        balance_credits: Number(value),
      }),
    });
    if (!res.ok) {
      let detail = await res.text();
      try { detail = JSON.parse(detail).detail || detail; } catch {}
      throw new Error(detail || ('HTTP ' + res.status));
    }
    _closeCreditPopover();
    await _load();
  } catch (e) {
    error.textContent = e.message || 'Credit adjustment failed.';
    error.hidden = false;
    save.disabled = false; input.disabled = false; input.focus();
  }
}

// Loading skeleton — echoes the access-policy card + two member sections so the
// tab has structure while the stats fetch resolves (shared .sk-shimmer in app3.css).
function _usersSkeleton() {
  const dot = '<span class="mem-sk sk-shimmer" style="width:14px;height:14px;border-radius:50%;flex:none;"></span>';
  const opt = (w) => '<div class="mem-sk-opt">' + dot + '<span class="mem-sk sk-shimmer" style="width:' + w + ';"></span></div>';
  const row = (w) => '<div class="mem-sk-row"><span class="mem-sk sk-shimmer" style="width:' + w + ';"></span></div>';
  const section = (titleW, rows) => '<div class="mem-sk-section">'
      + '<span class="mem-sk sk-shimmer mem-sk-head" style="width:' + titleW + ';"></span>'
      + rows.map(row).join('')
    + '</div>';
  return '<div class="members-skeleton" aria-hidden="true">'
    + '<div class="mem-sk-policy">'
    +   '<span class="mem-sk sk-shimmer mem-sk-head" style="width:90px;"></span>'
    +   opt('62%') + opt('54%')
    + '</div>'
    + section('64px', ['78%', '66%'])
    + section('82px', ['84%', '70%', '58%'])
    + section('118px', ['88%', '74%', '62%'])
    + '</div>';
}
