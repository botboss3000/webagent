'use strict';

/**
 * Event-sources engine panel — runtime status, source plugins, recent deliveries.
 *
 * Relocated from the former App Config "Event Sources" tab; now rendered inside
 * Agent Settings → "Automation Engine" (the event-trigger half). Wired by
 * agent-settings.js, which calls init() once and load() lazily on first expand.
 */

import { _fetch, _qs, _esc } from '../utils.js';

async function _loadEvents() {
  await Promise.all([
    _loadEventsRuntime(),
    _loadEventsSources(),
    _loadEventsDeliveries(),
  ]);
}

export function init() {
  _qs('ac-events-refresh')?.addEventListener('click', _loadEvents);
}

function _adminUserId() {
  return (window.app && window.app.currentUserId) || localStorage.getItem('webagent_active_user_id') || '';
}

export async function load() {
  await Promise.all([
    _loadEventsRuntime(),
    _loadEventsSources(),
    _loadEventsDeliveries(),
  ]);
}

async function _loadEventsRuntime() {
  const badge = _qs('ac-events-runtime-badge');
  const info  = _qs('ac-events-runtime-info');
  if (!badge || !info) return;
  badge.textContent = 'loading…';
  try {
    const r = await fetch('/admin/events/runtime-status?requesting_user_id=' + encodeURIComponent(_adminUserId()));
    if (!r.ok) {
      badge.textContent = r.status === 403 ? 'admin only' : `HTTP ${r.status}`;
      info.innerHTML = '';
      return;
    }
    const d = await r.json();
    // Runtime health = the two in-process loops that drive event triggers: the
    // poller (poll-only sources) + the renewer (refreshes push subscriptions).
    const poller = !!d.poller_running, renewer = !!d.renewer_running;
    const running = poller && renewer;
    badge.textContent = running ? 'running' : (poller || renewer ? 'partial' : 'idle');
    badge.style.color = running ? 'var(--success)' : (poller || renewer ? 'var(--warning)' : 'var(--fg-3)');
    info.innerHTML =
      `<div style="font-size:11px;margin:2px 0;">· Poller: <span style="color:${poller ? 'var(--success)' : 'var(--fg-3)'};">${poller ? 'running' : 'stopped'}</span></div>` +
      `<div style="font-size:11px;margin:2px 0;">· Renewer: <span style="color:${renewer ? 'var(--success)' : 'var(--fg-3)'};">${renewer ? 'running' : 'stopped'}</span></div>` +
      `<div style="font-size:11px;margin:2px 0;color:var(--fg-4);">${d.subscriptions_expiring_within_24h || 0} subscription(s) expiring within 24h</div>`;
  } catch (e) {
    badge.textContent = 'error';
    badge.style.color = 'var(--danger)';
    info.innerHTML = `<div style="font-size:11px;color:var(--danger);">${_esc(e.message)}</div>`;
  }
}

async function _loadEventsSources() {
  const wrap = _qs('ac-events-sources');
  if (!wrap) return;
  try {
    const r = await fetch('/admin/events/sources?requesting_user_id=' + encodeURIComponent(_adminUserId()));
    if (!r.ok) { wrap.innerHTML = `<div>${r.status === 403 ? 'Admin access required.' : 'Could not load event sources.'}</div>`; return; }
    const d = await r.json();
    const rows = d.sources || [];
    if (!rows.length) { wrap.innerHTML = '<div>(no event sources registered)</div>'; return; }
    // _renderEventSourceRow returns a DOM node (it wires button click handlers),
    // so append the nodes — never `.map().join('')`, which would stringify each
    // node to "[object HTMLDivElement]".
    wrap.innerHTML = '';
    rows.forEach(s => wrap.appendChild(_renderEventSourceRow(s)));
  } catch (e) { wrap.innerHTML = `<div>Error: ${e.message}</div>`; }
}

function _renderEventSourceRow(s) {
  const name = s.name || s.source_id || '';
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;align-items:center;gap:12px;padding:8px 10px;border-bottom:var(--border-width) solid var(--border);';

  const left = document.createElement('div');
  left.style.cssText = 'flex:1;';
  const stateColor = s.enabled ? 'var(--success)' : 'var(--fg-4)';
  const subInfo = `${s.subscription_enabled || 0}/${s.subscription_total || 0} active`;
  const errInfo = s.subscription_errored
    ? ` · <span style="color:var(--danger);">${s.subscription_errored} errored</span>` : '';
  left.innerHTML = `<div style="font-weight:600;font-size:13px;">${_esc(name)}</div>`
    + `<div style="font-size:11px;color:var(--fg-4);">${_esc(s.description || '')}</div>`
    + `<div style="font-size:11px;color:var(--fg-4);"><span style="color:${stateColor};">${s.enabled ? 'enabled' : 'disabled'}</span> · ${subInfo}${errInfo}</div>`;

  const right = document.createElement('div');
  right.style.cssText = 'display:flex;align-items:center;gap:8px;';

  const result = document.createElement('span');
  result.style.cssText = 'font-size:11px;min-width:60px;';

  // Both actions operate on the whole source (re-register / unregister every
  // subscription on it) and take requesting_user_id as a QUERY param.
  const rerunBtn = document.createElement('button');
  rerunBtn.className = 'ac-btn ac-btn-xs';
  rerunBtn.textContent = 'Re-register';
  rerunBtn.title = 'Re-register every enabled subscription on this source (after recreating provider infrastructure)';
  rerunBtn.addEventListener('click', async () => {
    rerunBtn.disabled = true; rerunBtn.textContent = '…';
    try {
      const rr = await fetch(`/admin/events/sources/${encodeURIComponent(name)}/re-register-all?requesting_user_id=${encodeURIComponent(_adminUserId())}`, { method: 'POST' });
      if (!rr.ok) throw new Error(`HTTP ${rr.status}`);
      const d = await rr.json();
      result.textContent = `✓ ${d.succeeded || 0} ok`;
      result.style.color = 'var(--success)';
    } catch (e) {
      result.textContent = `✗ ${e.message}`;
      result.style.color = 'var(--danger)';
    }
    rerunBtn.disabled = false; rerunBtn.textContent = 'Re-register';
  });

  const stopBtn = document.createElement('button');
  stopBtn.className = 'ac-btn ac-btn-xs';
  stopBtn.textContent = 'Stop all';
  stopBtn.title = 'Unregister every subscription on this source (before deleting provider infrastructure)';
  stopBtn.addEventListener('click', async () => {
    stopBtn.disabled = true; stopBtn.textContent = '…';
    try {
      const rr = await fetch(`/admin/events/sources/${encodeURIComponent(name)}/stop-all?requesting_user_id=${encodeURIComponent(_adminUserId())}`, { method: 'POST' });
      if (!rr.ok) throw new Error(`HTTP ${rr.status}`);
      const d = await rr.json();
      result.textContent = `✓ ${d.stopped || 0} stopped`;
      result.style.color = 'var(--warning)';
    } catch (e) {
      result.textContent = `✗ ${e.message}`;
      result.style.color = 'var(--danger)';
    }
    stopBtn.disabled = false; stopBtn.textContent = 'Stop all';
  });

  right.appendChild(rerunBtn);
  right.appendChild(stopBtn);
  right.appendChild(result);
  row.appendChild(left);
  row.appendChild(right);
  return row;
}

async function _loadEventsDeliveries() {
  const wrap = _qs('ac-events-deliveries');
  if (!wrap) return;
  try {
    const r = await fetch(`/admin/events/deliveries?requesting_user_id=${encodeURIComponent(_adminUserId())}&limit=50`);
    if (!r.ok) {
      wrap.innerHTML = `<div>${r.status === 403 ? 'Admin access required.' : 'Could not load deliveries.'}</div>`;
      return;
    }
    const d = await r.json();
    const rows = d.deliveries || [];
    if (!rows.length) {
      wrap.innerHTML = '<div>(no deliveries yet)</div>';
      return;
    }
    const colorByStatus = {
      ok: 'var(--success)',
      test: 'var(--brand)',
      duplicate: 'var(--fg-3)',
      pending: 'var(--warning)',
      error: 'var(--danger)',
    };
    wrap.innerHTML = rows.map(r => {
      const color = colorByStatus[r.status] || 'var(--fg-3)';
      const errPart = r.error ? `  ${r.error.substr(0, 80)}` : '';
      const t = (r.created_at || '').replace('T', ' ').substr(0, 19);
      return `<div style="margin-bottom:3px;"><span style="color:var(--muted);">${t}</span>  <span style="color:${color};font-weight:600;">${r.status.padEnd(9)}</span> <span style="color:var(--accent);">${r.source}.${r.event_type}</span> evt=${(r.event_external_id || '').substr(0, 36)} sub=${(r.subscription_id || '').substr(0, 8)}${errPart}</div>`;
    }).join('');
  } catch (e) {
    wrap.innerHTML = `<div>Error: ${e.message}</div>`;
  }
}

export function stop() {
  // Event Sources has no persistent state to clean up.
}