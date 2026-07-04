'use strict';

/**
 * Agents — Members tab.
 * Shows admin/member access control and activity stats.
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import {
  _isMockAgent,
} from './state.js';
import { _esc, _timeAgo } from './utils.js';

export async function _renderMembersTab(body, agent) {
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to manage members.</div>';
    return;
  }
  body.innerHTML = _membersSkeleton();

  let admins = [], members = [], userMode = agent.user_mode || 'anonymous';
  try {
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/members?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    if (!res.ok) { const detail = await res.text(); body.innerHTML = `<div class="members-loading" style="color:var(--danger)">Failed to load members: ${_esc(detail || res.statusText)}</div>`; return; }
    const data = await res.json();
    admins = data.admins || []; members = data.members || [];
    userMode = data.user_mode || userMode; agent.user_mode = userMode;
  } catch (e) { body.innerHTML = `<div class="members-loading" style="color:var(--danger)">Failed to load members: ${_esc(e.message)}</div>`; return; }

  body.innerHTML = '';
  body.appendChild(_buildAccessPolicyControl(agent, userMode, body));

  const notice = document.createElement('div'); notice.className = 'members-notice';
  notice.textContent = 'Activity counts reflect this agent only.';
  body.appendChild(notice);

  body.appendChild(_buildMembersSection(agent, 'Admins', admins, 'admin', body));
  body.appendChild(_buildMembersSection(agent, 'Members', members, 'member', body));
}

function _buildAccessPolicyControl(agent, currentMode, panelBody) {
  const wrap = document.createElement('div'); wrap.className = 'members-policy';
  const opts = [
    ['anonymous', 'Anonymous', 'Anyone with the link can chat. No registration needed.'],
    ['register', 'Registered', 'Users must have a registered account to chat.'],
    ['authorized', 'Authorized', 'Registered users must be authorized by an admin before they can chat.'],
  ];
  const title = document.createElement('div'); title.className = 'members-policy-title';
  title.textContent = 'Access policy';
  wrap.appendChild(title);
  const choices = document.createElement('div'); choices.className = 'members-policy-choices';
  for (const [val, label, hint] of opts) {
    const id = `acp-${agent.id}-${val}`;
    const optEl = document.createElement('label'); optEl.className = 'members-policy-opt' + (currentMode === val ? ' active' : '');
    optEl.htmlFor = id;
    optEl.innerHTML = `<input type="radio" id="${_esc(id)}" name="acp-${_esc(agent.id)}" value="${_esc(val)}" ${currentMode === val ? 'checked' : ''}>
      <div class="members-policy-opt-body">
        <div class="members-policy-opt-label">${_esc(label)}</div>
        <div class="members-policy-opt-hint">${_esc(hint)}</div>
      </div>`;
    optEl.querySelector('input').addEventListener('change', async (ev) => {
      const newMode = ev.target.value;
      try {
        const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/user-mode`, {
          method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ user_id: app.currentUserId, user_mode: newMode }),
        });
        if (!res.ok) throw new Error(await res.text());
        agent.user_mode = newMode; _renderMembersTab(panelBody, agent);
      } catch (e) { alert('Failed to update access policy: ' + e.message); _renderMembersTab(panelBody, agent); }
    });
    choices.appendChild(optEl);
  }
  wrap.appendChild(choices); return wrap;
}

function _buildMembersSection(agent, title, rows, kind, panelBody) {
  const sec = document.createElement('div'); sec.className = 'members-section';
  const header = document.createElement('div'); header.className = 'members-section-header';
  header.innerHTML = `<span class="members-section-title">${_esc(title)}</span><span class="members-section-count">${rows.length}</span>`;
  sec.appendChild(header);
  if (!rows.length) {
    const empty = document.createElement('div'); empty.className = 'members-empty';
    empty.textContent = kind === 'admin' ? 'No admins assigned to this agent yet.' : 'No members have used this agent yet.';
    sec.appendChild(empty); return sec;
  }
  const showActions = kind === 'member';
  const table = document.createElement('table'); table.className = 'members-table';
  table.innerHTML = `<thead><tr><th>User</th><th>Channel</th><th class="members-num">Sessions</th><th class="members-num">Messages</th><th>Last login</th>${showActions ? '<th>Status</th><th></th>' : ''}</tr></thead><tbody></tbody>`;
  const tbody = table.querySelector('tbody');
  for (const r of rows) {
    const tr = document.createElement('tr');
    const name = r.display_name || r.username || r.user_id;
    const subId = r.username && r.username !== name ? r.username : r.user_id;
    const channel = r.channel || (r.username ? 'web' : '\u2014');
    const last = r.last_login_at ? _timeAgo(r.last_login_at) : '\u2014';
    let statusHtml = '', actionHtml = '';
    if (showActions) {
      const isAuth = !!r.is_authorized;
      statusHtml = `<td><span class="members-status ${isAuth ? 'ok' : 'pending'}">${isAuth ? 'Authorized' : 'Pending'}</span></td>`;
      actionHtml = `<td class="members-actions"><button class="members-btn ${isAuth ? 'restrict' : 'authorize'}" data-act="${isAuth ? 'restrict' : 'authorize'}" data-uid="${_esc(r.user_id)}">${isAuth ? 'Restrict' : 'Authorize'}</button></td>`;
    }
    tr.innerHTML = `<td><div class="members-user-name">${_esc(name)}</div><div class="members-user-sub">${_esc(subId)}</div></td><td>${_esc(channel)}</td><td class="members-num">${r.session_count ?? 0}</td><td class="members-num">${r.interaction_count ?? 0}</td><td>${_esc(last)}</td>${statusHtml}${actionHtml}`;
    tbody.appendChild(tr);
  }
  if (showActions) {
    tbody.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button.members-btn');
      if (!btn) return;
      const uid = btn.dataset.uid; const act = btn.dataset.act; btn.disabled = true;
      try {
        const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/members/${encodeURIComponent(uid)}/${act}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ user_id: app.currentUserId }),
        });
        if (!res.ok) throw new Error(await res.text());
        _renderMembersTab(panelBody, agent);
      } catch (e) { alert('Action failed: ' + e.message); btn.disabled = false; }
    });
  }
  sec.appendChild(table); return sec;
}

// Loading skeleton — echoes the access-policy card + two member sections so the
// tab has structure while the /members fetch resolves (shared .sk-shimmer in app3.css).
function _membersSkeleton() {
  const dot = '<span class="mem-sk sk-shimmer" style="width:14px;height:14px;border-radius:50%;flex:none;"></span>';
  const opt = (w) => `<div class="mem-sk-opt">${dot}<span class="mem-sk sk-shimmer" style="width:${w};"></span></div>`;
  const row = (w) => `<div class="mem-sk-row"><span class="mem-sk sk-shimmer" style="width:${w};"></span></div>`;
  const section = (titleW, rows) => `<div class="mem-sk-section">
      <span class="mem-sk sk-shimmer mem-sk-head" style="width:${titleW};"></span>
      ${rows.map(row).join('')}
    </div>`;
  return `<div class="members-skeleton" aria-hidden="true">
    <div class="mem-sk-policy">
      <span class="mem-sk sk-shimmer mem-sk-head" style="width:90px;"></span>
      ${opt('62%')}${opt('54%')}${opt('68%')}
    </div>
    ${section('64px', ['78%', '66%'])}
    ${section('82px', ['84%', '70%', '58%'])}
  </div>`;
}