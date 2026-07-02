'use strict';

/* Admin Dashboard — movable/resizable grid of live metric cards.
   Breadcrumb: ui/admin-tools/dashboard/dashboard.html, ui/admin-tools/dashboard/
   dashboard.css, ui/admin-tools/dashboard/server.py (metrics + layout + AI),
   ui/shared/js/files.js (drop-in view lifecycle), ui/shared/css/design-system.css.

   COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
   No raw hex/rgb here: colours come from CSS classes/vars in dashboard.css.

   The Admin Tools shell imports this module and calls startView / stopView when
   the Dashboard icon is shown / hidden (ui/shared/js/files.js → _startDynAdminView).
   Layout is per-admin (server-side); the LIVE metrics poll a single DB-free
   endpoint so an open dashboard adds no database load. */

import { apiPath } from '../../shared/js/config.js';
import { _esc, _escAttr, _refreshLucideIcons } from '../../shared/js/dom-utils.js';
import { app } from '../../shared/js/state.js';
// Per-card AI star → floating WebAgent (manager) chat, same widget the App
// Settings page-assistant uses. See openCardAssistant + ui/chat-widget/.
import { createChatWidget } from '../../chat-widget/js/chat-widget.js';
// The Sessions & runs monitor card opens a session in the chat side-panel with
// the same action the session list uses (teardown + load + WS resume).
import { switchToSession } from '../../chat-side-panel/js/session-core.js';
// The full-width instance-switcher header (Local Instances feature). Owns its own
// DOM (#dash-inst) + API; we only init it once and feed it the metrics snapshot so
// its DB + health pills track the same data the grid shows. See instances-header.js.
import { initInstancesHeader, onDashboardSnapshot } from './instances-header.js';

const API = '/admin/dashboard';
const COLS = 12;
const ROW_H = 88;   // px per grid row
const GAP = 12;     // px gutter between cards
const POLL_MS = 5000;
// ── responsive flow (mobile) ──────────────────────────────────────────────────
// The 12-column grid keeps cards legible on a wide panel, but on a narrow one
// each of the 12 columns becomes tiny and cards shrink past readability. Below
// FLOW_BREAKPOINT the grid switches to a wrapping-tile layout: cards keep a
// minimum legible width (FLOW_MIN_CARD) and flow left→right, top→bottom in
// reading order — a card that doesn't fit the remaining row width wraps to the
// next line instead of shrinking. See flowActive / computeFlowRects / positionAll.
const FLOW_BREAKPOINT = 720;   // px of grid content width — below this, linearise
const FLOW_MIN_CARD = 240;     // px — a card is never rendered narrower than this

function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch (e) { return ''; } }

// ── state ───────────────────────────────────────────────────────────────────
let _cards = [];
let _snapshot = null;
let _pollTimer = null;
let _editMode = false;
let _window = 3600;
let _started = false;
let _saveTimer = null;
let _ro = null;
const _tsCache = {};   // card id -> { at, points } for chart cards

// ── fetch helpers ────────────────────────────────────────────────────────────
async function _getJSON(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath(API + path + sep + 'requesting_user_id=' + encodeURIComponent(_uid())));
  if (!res.ok) throw new Error('GET ' + path + ' → ' + res.status);
  return res.json();
}
async function _sendJSON(path, method, body) {
  const res = await fetch(apiPath(API + path), {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _uid(), ...(body || {}) }),
  });
  if (!res.ok) {
    let detail = res.status;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(String(detail));
  }
  return res.json();
}

// ── number formatting ────────────────────────────────────────────────────────
function fmtInt(n) { return (n == null) ? '—' : Number(n).toLocaleString(); }
function fmtNum(n) {
  if (n == null) return '—';
  n = Number(n);
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}
function fmtCost(n) { return (n == null) ? '—' : '$' + Number(n).toFixed(Number(n) < 1 ? 4 : 2); }
function fmtMs(n) { return (n == null) ? '—' : (Number(n) >= 1000 ? (n / 1000).toFixed(2) + 's' : Math.round(n) + 'ms'); }
function fmtDur(s) {
  if (s == null) return '—';
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}
function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function agoLabel(s) {
  if (s == null) return '—';
  return s < 60 ? 'just now' : fmtDur(s) + ' ago';
}
function agoIso(ts) {
  const t = Date.parse(ts || '');
  return t ? agoLabel(Math.floor((Date.now() - t) / 1000)) : '';
}

// ── small render helpers (return HTML strings) ───────────────────────────────
function stat(big, sub, tone) {
  return `<div class="dash-stat ${tone ? 'tone-' + tone : ''}">`
    + `<div class="dash-stat-big">${big}</div>`
    + (sub ? `<div class="dash-stat-sub">${sub}</div>` : '') + `</div>`;
}
function gauge(pct, label, tone) {
  pct = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="dash-gauge">`
    + `<div class="dash-gauge-big">${pct.toFixed(0)}<span>%</span></div>`
    + `<div class="dash-gauge-bar"><span class="tone-${tone || 'brand'}" style="width:${pct}%"></span></div>`
    + (label ? `<div class="dash-stat-sub">${label}</div>` : '') + `</div>`;
}
function bars(rows) {
  const max = Math.max(1, ...rows.map(r => r.value || 0));
  return `<div class="dash-bars">` + rows.map(r =>
    `<div class="dash-bar-row"><span class="dash-bar-label">${r.label}</span>`
    + `<span class="dash-bar-track"><span class="tone-${r.tone || 'brand'}" style="width:${Math.round((r.value || 0) / max * 100)}%"></span></span>`
    + `<span class="dash-bar-val">${r.display != null ? r.display : fmtNum(r.value)}</span></div>`
  ).join('') + `</div>`;
}
function list(rows) {
  if (!rows.length) return `<div class="dash-muted">No data yet.</div>`;
  return `<div class="dash-list">` + rows.map(r =>
    `<div class="dash-list-row"><span class="dash-list-k">${r.k}</span><span class="dash-list-v ${r.tone ? 'tone-' + r.tone : ''}">${r.v}</span></div>`
  ).join('') + `</div>`;
}
function sparkline(points, tone) {
  const vals = points.map(p => p.avg_ms || 0);
  const max = Math.max(1, ...vals);
  const W = 100, H = 34;
  const step = vals.length > 1 ? W / (vals.length - 1) : W;
  const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(H - (v / max) * H).toFixed(1)}`).join(' ');
  const last = vals.length ? vals[vals.length - 1] : 0;
  return `<div class="dash-spark">`
    + `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="tone-${tone || 'brand'}">`
    + `<polyline points="${pts}" fill="none" vector-effect="non-scaling-stroke"/></svg>`
    + `<div class="dash-stat-sub">latest ${fmtMs(last)} · peak ${fmtMs(max)}</div></div>`;
}

// ── card registry ────────────────────────────────────────────────────────────
// Each entry: { title, icon, w, h, live?, render(snap, card) → HTML }.
// `live: true` marks a card that reads the always-fresh in-memory section.
const CARD_TYPES = {
  cost: {
    title: 'Estimated Spend', icon: 'wallet', w: 3, h: 2,
    render: (s) => stat(fmtCost(getPath(s, 'tokens.cost_usd')), `${fmtInt(getPath(s, 'tokens.calls'))} calls · ${windowLabel()}`, 'brand'),
  },
  tokens: {
    title: 'Token Usage', icon: 'coins', w: 3, h: 2,
    render: (s) => `<div class="dash-duo"><div>${stat(fmtNum(getPath(s, 'tokens.in')), 'tokens in', 'purple')}</div>`
      + `<div>${stat(fmtNum(getPath(s, 'tokens.out')), 'tokens out', 'brand')}</div></div>`,
  },
  active_runs: {
    title: 'Active Agent Runs', icon: 'activity', w: 3, h: 2, live: true,
    render: (s) => stat(fmtInt(getPath(s, 'active_runs')), `${fmtNum(getPath(s, 'live.llm.rate_per_min'))} LLM calls/min`, 'success'),
  },
  db_mode: {
    title: 'Database', icon: 'database', w: 3, h: 2, live: true,
    render: (s) => {
      const h = s.db_health || {};
      const tone = h.degraded ? 'danger' : (h.ok ? 'success' : 'warning');
      const badge = h.degraded ? 'DEGRADED → local' : (h.remote ? 'remote · connected' : 'local');
      return stat((h.actual || '—').toUpperCase(), `<span class="dash-pill tone-${tone}">${badge}</span>`, tone);
    },
  },
  cpu: {
    title: 'CPU', icon: 'cpu', w: 3, h: 2, live: true,
    render: (s) => gauge(getPath(s, 'live.cpu_percent'), 'process utilisation', cpuTone(getPath(s, 'live.cpu_percent'))),
  },
  memory: {
    title: 'Memory', icon: 'memory-stick', w: 3, h: 2, live: true,
    render: (s) => stat(fmtNum(getPath(s, 'memory_mb')) + ' <span class="dash-unit">MB</span>', 'resident set', 'purple'),
  },
  db_latency: {
    title: 'Database Latency', icon: 'gauge', w: 6, h: 3, live: true,
    render: (s) => {
      const d = getPath(s, 'live.db') || {};
      const h = s.db_health || {};
      const prov = h.provider || 'sqlite';
      const host = h.host || 'localhost';
      const hybrid = h.hybrid ? ' · hybrid' : '';
      const tone = h.degraded ? 'danger' : (h.ok ? 'success' : 'warning');
      const statusLabel = h.degraded ? 'degraded'
        : h.remote ? 'connected' : 'local';
      return `<div class="dash-dblat">`
        + `<div class="dash-trio">`
        + `<div>${stat(fmtMs(d.avg_ms), 'avg', latTone(d.avg_ms))}</div>`
        + `<div>${stat(fmtMs(d.p95_ms), 'p95', latTone(d.p95_ms))}</div>`
        + `<div>${stat(fmtNum(d.rate_per_min), 'calls/min', 'brand')}</div></div>`
        + `<div class="dash-dblat-conn">`
        + `<span class="dash-pill tone-brand">${_esc(prov)}</span>`
        + `<span class="dash-dblat-host">${_esc(host)}</span>`
        + `<span class="dash-pill tone-${tone}">${_esc(statusLabel)}${hybrid}</span>`
        + `</div></div>`;
    },
  },
  db_latency_chart: {
    title: 'DB Latency — trend', icon: 'line-chart', w: 6, h: 3, live: true, chart: 'db',
    render: (s, card) => {
      const pts = (_tsCache[card.id] || {}).points || [];
      if (!pts.length) return `<div class="dash-muted">Collecting samples…</div>`;
      return sparkline(pts, 'brand');
    },
  },
  loop_split: {
    title: 'Where the loop spends time', icon: 'pie-chart', w: 3, h: 3, live: true,
    render: (s) => {
      const sp = getPath(s, 'live.loop_split') || {};
      return bars([
        { label: 'LLM', value: sp.llm_pct, display: (sp.llm_pct || 0) + '%', tone: 'purple' },
        { label: 'Database', value: sp.db_pct, display: (sp.db_pct || 0) + '%', tone: 'brand' },
      ]);
    },
  },
  context: {
    title: 'Context Window', icon: 'brain', w: 3, h: 3,
    render: (s) => {
      const c = s.context || {};
      return list([
        { k: 'Model', v: _esc(c.model || '—') },
        { k: 'Max input', v: c.max_input ? fmtNum(c.max_input) + ' tok' : '—' },
        { k: 'Max output', v: c.max_output ? fmtNum(c.max_output) + ' tok' : '—' },
      ]);
    },
  },
  tokens_by_agent: {
    title: 'Tokens by Agent', icon: 'users', w: 4, h: 3,
    render: (s) => {
      const rows = (getPath(s, 'tokens.by_agent') || []).map(a => ({
        label: _esc(String(a.agent).slice(0, 16)), value: (a.in || 0) + (a.out || 0),
        display: fmtNum((a.in || 0) + (a.out || 0)), tone: 'purple',
      }));
      return rows.length ? bars(rows) : `<div class="dash-muted">No usage in ${windowLabel()}.</div>`;
    },
  },
  devices: {
    title: 'Devices', icon: 'monitor-smartphone', w: 4, h: 3, live: true,
    render: (s) => {
      const ds = s.devices || [];
      if (!ds.length) return `<div class="dash-muted">No devices reporting.</div>`;
      return list(ds.map(d => ({
        k: `<span class="dash-dot ${d.online ? 'on' : 'off'}"></span>${_esc(d.label)} <span class="dash-muted-inline">${_esc(d.platform)}</span>`,
        v: d.online ? 'online' : (d.last_seen_s != null ? fmtDur(d.last_seen_s) + ' ago' : 'offline'),
        tone: d.online ? 'success' : null,
      })));
    },
  },
  failures: {
    title: 'Recent Failures', icon: 'triangle-alert', w: 4, h: 3, live: true,
    render: (s) => {
      const f = s.failures || {};
      const head = `<div class="dash-fail-head"><span class="dash-stat-big tone-${(f.count ? 'danger' : 'success')}">${fmtInt(f.count)}</span><span class="dash-stat-sub">errors · ${windowLabel()}</span></div>`;
      const rows = (f.recent || []).slice(0, 6).map(r =>
        `<div class="dash-fail-row"><span class="dash-pill tone-${r.level === 'critical' ? 'danger' : 'warning'}">${_esc(r.category || r.level)}</span><span class="dash-fail-msg">${_esc(r.message || '')}</span></div>`
      ).join('');
      return head + (rows ? `<div class="dash-fail-list">${rows}</div>` : `<div class="dash-muted">No failures — all clear.</div>`);
    },
  },
  uptime: {
    title: 'Uptime', icon: 'timer', w: 3, h: 2, live: true,
    render: (s) => stat(fmtDur(getPath(s, 'live.uptime_s')), 'since last restart', 'success'),
  },
  storage: {
    title: 'Storage', icon: 'hard-drive', w: 3, h: 3,
    render: (s) => {
      const st = s.storage || {};
      const rows = (st.files || []).map(f => ({ k: _esc(f.name), v: f.mb + ' MB' }));
      if (st.disk_free_gb != null) rows.push({ k: 'Disk free', v: st.disk_free_gb + ' / ' + st.disk_total_gb + ' GB', tone: 'success' });
      return list(rows);
    },
  },
  token_rate: {
    title: 'Token Throughput', icon: 'gauge', w: 3, h: 2, live: true,
    render: (s) => stat(fmtNum(getPath(s, 'live.llm.tokens_per_min')), 'tokens / min', 'purple'),
  },
  // ── operational cards (agents / users / sessions / health / tools / security) ──
  // Row-based cards over the snapshot's operational sections (server.py). Rows
  // and buttons carry data-act attributes handled by the delegated grid click
  // (handleCardAction): open an agent chat, open/stop a session, approve users.
  agents_panel: {
    title: 'Agents', icon: 'bot', w: 4, h: 4, live: true,
    render: (s) => {
      const a = s.agents || {};
      const rows = a.list || [];
      if (!rows.length) return `<div class="dash-muted">No agents yet.</div>`;
      return `<div class="dash-opwrap"><div class="dash-oplist">`
        + rows.map(ag =>
          `<div class="dash-oprow dash-op-click" data-act="open-agent" data-id="${_escAttr(ag.id)}" title="Open a chat with this agent">`
          + `<span class="dash-oprow-ico"><i data-lucide="${_escAttr(ag.icon || 'bot')}" style="width:15px;height:15px;"></i></span>`
          + `<span class="dash-oprow-main"><span class="dash-oprow-name">${_esc(ag.name)}</span>`
          + `<span class="dash-oprow-sub">${_esc(ag.model || 'inherited model')}</span></span>`
          + (ag.running ? `<span class="dash-pill tone-success">${fmtInt(ag.running)} live</span>` : '')
          + `<span class="dash-oprow-val">${fmtNum(ag.tokens)} tok<span class="dash-oprow-vsub">${fmtCost(ag.cost_usd)}</span></span>`
          + `</div>`).join('')
        + `</div><div class="dash-oplist-foot">${fmtInt(a.total)} agents · usage over ${windowLabel()}</div></div>`;
    },
  },
  user_mgmt: {
    title: 'User Management', icon: 'users', w: 4, h: 4, live: true,
    render: (s) => {
      const u = s.users || {};
      const head = `<div class="dash-ophead">`
        + `<span><b>${fmtInt(u.total)}</b> users</span>`
        + `<span><b>${fmtInt(u.admins)}</b> admins</span>`
        + `<span class="${u.pending ? 'tone-warning' : ''}"><b>${fmtInt(u.pending)}</b> pending</span>`
        + (u.new_in_window ? `<span class="tone-success"><b>+${fmtInt(u.new_in_window)}</b> ${windowLabel()}</span>` : '')
        + `</div>`;
      const rows = (u.recent || []).map(r =>
        `<div class="dash-oprow">`
        + `<span class="dash-oprow-ico"><i data-lucide="${r.admin ? 'shield' : 'user'}" style="width:15px;height:15px;"></i></span>`
        + `<span class="dash-oprow-main"><span class="dash-oprow-name">${_esc(r.name)}</span>`
        + `<span class="dash-oprow-sub">${_esc(r.username)}</span></span>`
        + (r.admin ? `<span class="dash-pill tone-brand">admin</span>` : '')
        + (!r.approved
          ? `<span class="dash-oprow-acts">`
            + `<button type="button" class="dash-oprow-btn ok" data-act="approve-user" data-id="${_escAttr(r.user_id)}" title="Approve this account"><i data-lucide="check" style="width:13px;height:13px;"></i></button>`
            + `<button type="button" class="dash-oprow-btn danger" data-act="reject-user" data-id="${_escAttr(r.user_id)}" title="Reject &amp; delete this account"><i data-lucide="x" style="width:13px;height:13px;"></i></button>`
            + `</span>`
          : `<span class="dash-oprow-val">${agoLabel(r.last_login_s)}</span>`)
        + `</div>`).join('');
      return `<div class="dash-opwrap">${head}<div class="dash-oplist">${rows || '<div class="dash-muted">No users yet.</div>'}</div></div>`;
    },
  },
  sessions_monitor: {
    title: 'Sessions & Runs', icon: 'messages-square', w: 6, h: 4, live: true,
    render: (s) => {
      const se = s.sessions || {};
      const rows = se.list || [];
      if (!rows.length) return `<div class="dash-muted">No sessions yet.</div>`;
      return `<div class="dash-opwrap"><div class="dash-oplist">`
        + rows.map(r => {
          const dot = r.running
            ? `<span class="dash-dot ${r.stale ? 'stale' : 'run'}"></span>`
            : `<span class="dash-dot off"></span>`;
          const right = r.running
            ? `<span class="dash-oprow-val tone-success">${fmtDur(r.running_s || 0)}${r.stale ? ' · stalled?' : ' · live'}<span class="dash-oprow-vsub">${fmtCost(r.cost_usd)}</span></span>`
              + `<button type="button" class="dash-oprow-btn danger" data-act="stop-run" data-id="${_escAttr(r.id)}" title="Stop this run"><i data-lucide="square" style="width:12px;height:12px;"></i></button>`
            : `<span class="dash-oprow-val">${agoLabel(r.updated_s)}<span class="dash-oprow-vsub">${fmtCost(r.cost_usd)}</span></span>`;
          return `<div class="dash-oprow dash-op-click" data-act="open-session" data-id="${_escAttr(r.id)}" title="Open this session in chat">`
            + dot
            + `<span class="dash-oprow-main"><span class="dash-oprow-name">${_esc(r.title)}</span>`
            + `<span class="dash-oprow-sub">${_esc(r.agent_id || '—')} · ${_esc(String(r.user_id || '').slice(0, 18))}</span></span>`
            + right + `</div>`;
        }).join('')
        + `</div><div class="dash-oplist-foot">${fmtInt(se.active)} running · cost over ${windowLabel()}</div></div>`;
    },
  },
  health_board: {
    title: 'System Health', icon: 'heart-pulse', w: 4, h: 4, live: true,
    render: (s) => {
      const rows = s.health || [];
      if (!rows.length) return `<div class="dash-muted">No health checks reported.</div>`;
      return `<div class="dash-opwrap"><div class="dash-oplist">`
        + rows.map(h =>
          `<div class="dash-oprow">`
          + `<span class="dash-dot hb-${_escAttr(h.state || 'off')}"></span>`
          + `<span class="dash-oprow-main"><span class="dash-oprow-name">${_esc(h.label)}</span>`
          + (h.detail ? `<span class="dash-oprow-sub">${_esc(h.detail)}</span>` : '')
          + `</span>`
          + `<span class="dash-oprow-val">${_esc(h.value == null ? '—' : String(h.value))}</span>`
          + `</div>`).join('')
        + `</div></div>`;
    },
  },
  tool_usage: {
    title: 'Tool & Model Usage', icon: 'wrench', w: 6, h: 4, live: true,
    render: (s) => {
      const t = s.tool_usage || {};
      const tools = t.tools || [];
      const models = t.models || [];
      const maxCalls = Math.max(1, ...tools.map(x => x.calls || 0));
      const toolRows = tools.map(x =>
        `<div class="dash-tm-row">`
        + `<span class="dash-tm-name">${_esc(x.name)}</span>`
        + `<span class="dash-bar-track"><span class="tone-brand" style="width:${Math.round((x.calls || 0) / maxCalls * 100)}%"></span></span>`
        + `<span class="dash-tm-val">${fmtInt(x.calls)}${x.failures ? ` <span class="tone-danger">· ${x.fail_pct}% fail</span>` : ''} · ${fmtMs(x.avg_ms)}</span>`
        + `</div>`).join('');
      const modelRows = models.map(m =>
        `<div class="dash-tm-row no-track">`
        + `<span class="dash-tm-name">${_esc(m.model)}</span>`
        + `<span class="dash-tm-val">${fmtInt(m.calls)} calls · ${fmtNum(m.tokens)} tok · ${fmtCost(m.cost_usd)}</span>`
        + `</div>`).join('');
      if (!tools.length && !models.length) return `<div class="dash-muted">No tool or model activity in ${windowLabel()}.</div>`;
      return `<div class="dash-opwrap"><div class="dash-oplist">`
        + (tools.length ? `<div class="dash-oplist-cap">Tools · ${fmtInt(t.tool_calls)} calls${t.tool_failures ? ` · <span class="tone-danger">${fmtInt(t.tool_failures)} failed</span>` : ''}</div>` + toolRows : '')
        + (models.length ? `<div class="dash-oplist-cap">Models</div>` + modelRows : '')
        + `</div><div class="dash-oplist-foot">${windowLabel()}</div></div>`;
    },
  },
  security: {
    title: 'Security & Sign-ins', icon: 'shield-check', w: 4, h: 4, live: true,
    render: (s) => {
      const sec = s.security || {};
      const head = `<div class="dash-ophead">`
        + `<span class="tone-success"><b>${fmtInt(sec.signins)}</b> sign-ins</span>`
        + `<span class="${sec.failed ? 'tone-danger' : ''}"><b>${fmtInt(sec.failed)}</b> failed / blocked</span>`
        + `</div>`;
      const rows = (sec.recent || []).map(r => {
        const bad = ['warning', 'error', 'critical'].includes(r.level);
        return `<div class="dash-oprow">`
          + `<span class="dash-dot ${bad ? 'hb-warn' : 'hb-ok'}"></span>`
          + `<span class="dash-oprow-main"><span class="dash-oprow-name dash-oprow-wrap">${_esc(r.message)}</span></span>`
          + `<span class="dash-oprow-val">${_esc(agoIso(r.ts))}</span>`
          + `</div>`;
      }).join('');
      return `<div class="dash-opwrap">${head}<div class="dash-oplist">${rows || `<div class="dash-muted">No sign-in activity in ${windowLabel()}.</div>`}</div></div>`;
    },
  },
  // The full-width "Metrics over time" hero chart. Like the adder tile, its body
  // is built ONCE (controls + SVG) — buildMetricChart — and refreshed in place by
  // refreshMetricChart, so the 5s metrics poll never wipes its controls. See the
  // "Metrics-over-time chart" section below.
  metric_chart: {
    title: 'Metrics over time', icon: 'line-chart', w: 12, h: 5, live: true, hero: true, render: () => '',
  },
  custom: {
    title: 'Custom', icon: 'sparkles', w: 3, h: 2,
    render: (s, card) => renderCustom(s, card),
  },
  // The "Add a card" tile — a permanent card (not a popup) whose + button opens
  // an inline card-type picker and whose chat pill generates an AI card. Its body
  // is built once by buildAdder() (see renderCardBody) so polling never wipes it.
  add_card: {
    title: 'Add a card', icon: 'plus', w: 4, h: 3, adder: true, render: () => '',
  },
};

// Types offered in the picker (custom is AI-only; live-internal ones included).
const PICKER_TYPES = ['metric_chart', 'agents_panel', 'sessions_monitor', 'user_mgmt', 'health_board',
  'tool_usage', 'security', 'cost', 'tokens', 'token_rate', 'active_runs', 'db_mode', 'cpu', 'memory',
  'db_latency', 'db_latency_chart', 'loop_split', 'context', 'tokens_by_agent', 'devices',
  'failures', 'uptime', 'storage'];

// ── the "Metrics over time" hero chart — series + palette ────────────────────
// Selectable series for the full-width chart. Series are grouped by UNIT into
// stacked LANES that share one time axis (small multiples — never two scales on
// one plot): `cost` is a stacked-bar lane (one segment per model), every other
// unit group is a line lane with its own real, zero-based Y axis.
// `tone` is the series' FIXED identity colour — a design-system --chart-* token
// (validated categorical set; see design-system.css). It follows the series id,
// never the current selection, so toggling series never repaints survivors.
// `errors` deliberately wears the status danger hue: that colour MEANS "bad".
const CHART_SERIES = [
  { id: 'cost',        label: 'Cost per model',   kind: 'bars', unit: '$' },
  { id: 'tokens_in',   label: 'Tokens in',        kind: 'line', unit: 'tok',   tone: '--chart-1' },
  { id: 'tokens_out',  label: 'Tokens out',       kind: 'line', unit: 'tok',   tone: '--chart-2' },
  { id: 'llm_calls',   label: 'LLM calls',        kind: 'line', unit: 'calls', tone: '--chart-3' },
  { id: 'errors',      label: 'Errors',           kind: 'line', unit: 'calls', tone: '--danger' },
  { id: 'active_runs', label: 'Active runs',      kind: 'line', unit: 'runs',  tone: '--chart-6' },
  { id: 'db_avg_ms',   label: 'DB latency (avg)', kind: 'line', unit: 'ms',    tone: '--chart-1' },
  { id: 'db_p95_ms',   label: 'DB latency (p95)', kind: 'line', unit: 'ms',    tone: '--chart-5' },
  { id: 'cpu',         label: 'CPU',              kind: 'line', unit: '%',     tone: '--chart-4' },
  { id: 'ram',         label: 'RAM',              kind: 'line', unit: 'MB',    tone: '--chart-5' },
];
const CHART_DEFAULT_SERIES = ['tokens_in', 'tokens_out', 'cost'];
// Colour slots for the per-model COST bar segments — the validated categorical
// set from design-system.css, assigned in cost order (largest first); the
// folded "Other" bucket wears the muted de-emphasis ink instead.
const CHART_TONES = ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6'];
// Lane order (top → bottom) when several unit groups are visible.
const CHART_LANE_ORDER = ['$', 'tok', 'calls', 'runs', 'ms', '%', 'MB'];
const CHART_LANE_LABEL = {
  '$': 'Cost', tok: 'Tokens', calls: 'Count', runs: 'Active runs',
  ms: 'Latency ms', '%': 'CPU %', MB: 'RAM MB',
};
const CHART_WINDOWS = [
  { s: 300, label: '5m' }, { s: 3600, label: '1h' },
  { s: 86400, label: '24h' }, { s: 604800, label: '7d' },
];
const _chartCache = {};   // card id -> last /chart response (for redraw without refetch)
const _chartUi = {};      // card id -> transient UI state { hidden:Set, zoomStack:[] }

function chartUi(card) {
  if (!_chartUi[card.id]) _chartUi[card.id] = { hidden: new Set(), zoomStack: [] };
  return _chartUi[card.id];
}

function cpuTone(v) { v = Number(v) || 0; return v > 85 ? 'danger' : (v > 60 ? 'warning' : 'success'); }
function latTone(v) { v = Number(v) || 0; return v > 250 ? 'danger' : (v > 120 ? 'warning' : 'success'); }
function windowLabel() {
  return ({ 300: 'last 5m', 3600: 'last hour', 86400: 'last 24h', 604800: 'last 7d' })[_window] || 'window';
}

function renderCustom(s, card) {
  const fields = card.fields || [];
  if (card.viz === 'stat' && fields.length) {
    const f = fields[0];
    return stat(fmtNum(getPath(s, f.path)) + (f.unit ? ' <span class="dash-unit">' + _esc(f.unit) + '</span>' : ''), _esc(f.label), 'brand');
  }
  return list(fields.map(f => {
    let v = getPath(s, f.path);
    if (typeof v === 'boolean') v = v ? 'yes' : 'no';
    else if (typeof v === 'number') v = fmtNum(v);
    else if (Array.isArray(v)) v = v.length;
    else v = (v == null ? '—' : _esc(String(v)));
    return { k: _esc(f.label), v: v + (f.unit ? ' ' + _esc(f.unit) : '') };
  }));
}

// ── DOM refs ─────────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }
let elGrid, elWrap, elEmpty;

// ── grid geometry + rendering ────────────────────────────────────────────────
function gridContentW() {
  // Measure the GRID's own content box, not the scroll wrap: elWrap.clientWidth
  // still includes the wrap's 14px L/R padding (28px), whereas the cards live
  // inside .dash-grid (width:100% of that content box). elGrid.clientWidth also
  // already subtracts the vertical scrollbar, so the layout can't spill past the
  // right edge. Fall back to the wrap minus its padding if the grid isn't
  // measurable yet (0-width during the Admin Tools view switch-in).
  const gw = elGrid && elGrid.clientWidth;
  return (gw > 6 ? gw : (((elWrap && elWrap.clientWidth) || 1000) - 28)) - 2;
}
function cellW() {
  const cw = (gridContentW() - GAP * (COLS - 1)) / COLS;
  return cw > 6 ? cw : 6;   // never negative/tiny — a hidden/0-width panel would
                            // otherwise write negative card widths (rejected by
                            // the browser) and stack every card at the origin.
}
// True when the panel is too narrow for the 12-col grid to stay legible, so the
// grid linearises into wrapping tiles (see computeFlowRects). Measured once the
// panel has a real width — a 0-width switch-in reports the fallback (1000px),
// which is wide, so we don't flash the flow layout during the view transition.
function flowActive() {
  const w = gridContentW();
  return w > 6 && w < FLOW_BREAKPOINT;
}
// Lay every card out as a wrapping tile: cards flow left→right, top→bottom in
// reading order (their saved y, then x), each keeping a minimum legible width.
// The panel is split into as many equal columns as fit at FLOW_MIN_CARD; an
// authored 12-col width maps to a proportional column span (never below 1, never
// past the row), and a card that doesn't fit the remaining columns wraps to the
// next line. Returns a card.id → {left, top, width, height} map + the total
// height. Heights are preserved (card.h rows) so card internals stay readable.
function computeFlowRects() {
  const availW = Math.max(FLOW_MIN_CARD, gridContentW());
  const fcols = Math.max(1, Math.floor((availW + GAP) / (FLOW_MIN_CARD + GAP)));
  const colW = (availW - GAP * (fcols - 1)) / fcols;
  const order = _cards.slice().sort((a, b) => (a.y - b.y) || (a.x - b.x));
  const rects = new Map();
  let rowTop = 0, col = 0, rowMaxH = 0;
  for (const card of order) {
    let span = Math.max(1, Math.min(fcols, Math.round(card.w / COLS * fcols)));
    if (col > 0 && col + span > fcols) { rowTop += rowMaxH + GAP; col = 0; rowMaxH = 0; }
    const width = Math.round(span * colW + (span - 1) * GAP);
    const height = Math.round(card.h * ROW_H + (card.h - 1) * GAP);
    rects.set(card.id, { left: Math.round(col * (colW + GAP)), top: rowTop, width, height });
    col += span;
    if (height > rowMaxH) rowMaxH = height;
    if (col >= fcols) { rowTop += rowMaxH + GAP; col = 0; rowMaxH = 0; }
  }
  return { rects, totalH: rowTop + rowMaxH };
}
// The panel is often still 0-width while the Admin Tools view is switching in;
// positioning then is wrong. Defer the first render until it has a real width.
function sized() { return !!(elWrap && elWrap.clientWidth > 120); }
function whenSized(cb, tries) {
  tries = tries || 0;
  if (sized() || tries > 60) { cb(); return; }
  requestAnimationFrame(() => whenSized(cb, tries + 1));
}
function place(el, card) {
  const cw = cellW();
  el.style.left = Math.round(card.x * (cw + GAP)) + 'px';
  el.style.top = Math.round(card.y * (ROW_H + GAP)) + 'px';
  el.style.width = Math.round(card.w * cw + (card.w - 1) * GAP) + 'px';
  el.style.height = Math.round(card.h * ROW_H + (card.h - 1) * GAP) + 'px';
}
function gridHeight() {
  const maxRow = _cards.reduce((m, c) => Math.max(m, c.y + c.h), 6);
  return maxRow * (ROW_H + GAP);
}

function cardShell(card) {
  const def = CARD_TYPES[card.type] || CARD_TYPES.custom;
  const el = document.createElement('div');
  el.className = 'dash-card';
  el.dataset.id = card.id;
  el.dataset.type = card.type;
  // The adder tile is permanent — draggable/resizable, but no remove button so
  // an admin can't accidentally delete their only way to add cards.
  const removeBtn = def.adder ? ''
    : `<button class="dash-card-btn dash-card-remove" title="Remove card"><i data-lucide="x" style="width:14px;height:14px;"></i></button>`;
  // AI star — ALWAYS visible (it lives OUTSIDE .dash-card-tools, which only shows
  // in edit mode). Opens a WebAgent chat pre-primed with this exact card's identity
  // + where its front/back-end live, so the admin can just say what to change
  // (openCardAssistant). The adder tile has its own AI pill → no star on it.
  const aiBtn = def.adder ? ''
    : `<button class="dash-card-btn dash-card-ai" title="Ask WebAgent to update this card"><i data-lucide="sparkles" style="width:14px;height:14px;"></i></button>`;
  el.innerHTML =
    `<div class="dash-card-head">`
    + `<span class="dash-card-title"><i data-lucide="${_escAttr((card.icon || def.icon))}" style="width:14px;height:14px;"></i>${_esc(card.title || def.title)}</span>`
    + `<span class="dash-card-tools">`
    + `<button class="dash-card-btn dash-card-drag" title="Drag to move"><i data-lucide="grip-vertical" style="width:14px;height:14px;"></i></button>`
    + removeBtn
    + `</span>`
    + aiBtn
    + `</div>`
    + `<div class="dash-card-body"></div>`
    + `<span class="dash-card-resize" title="Drag to resize"></span>`;
  place(el, card);
  return el;
}

// ── per-card loading gate ─────────────────────────────────────────────────────
// A card shows a spinner until the snapshot section(s) it reads have arrived, so
// cards fill in AS their data lands instead of all at once. The fast first poll
// (/metrics?live=1) returns only the in-memory / filesystem sections — live,
// memory_mb, db_health, storage — so those cards populate near-instantly while
// the DB-backed cards keep spinning until the full poll() returns. Keys below are
// top-level snapshot sections (server.py _build_snapshot); keep in sync when a
// card starts reading a new section. Cards not listed (hero chart, adder, the
// timeseries spark) build their own body and are never gated here.
const CARD_NEEDS = {
  cost: ['tokens'], tokens: ['tokens'], tokens_by_agent: ['tokens'],
  active_runs: ['active_runs'], db_mode: ['db_health'],
  cpu: ['live'], memory: ['memory_mb'], db_latency: ['live', 'db_health'],
  loop_split: ['live'], uptime: ['live'], token_rate: ['live'], storage: ['storage'],
  context: ['context'], devices: ['devices'], failures: ['failures'],
  agents_panel: ['agents'], user_mgmt: ['users'], sessions_monitor: ['sessions'],
  health_board: ['health'], tool_usage: ['tool_usage'], security: ['security'],
};
function cardNeeds(card) {
  if (card.type === 'custom') {
    // A custom card is ready once the section(s) behind its fields have arrived.
    return [...new Set((card.fields || [])
      .map(f => String(f.path || '').split('.')[0]).filter(Boolean))];
  }
  return CARD_NEEDS[card.type] || [];
}
// True once every snapshot section this card reads is present. Until then the
// per-card spinner shows. Cards with no declared needs (e.g. db_latency_chart)
// become ready as soon as ANY snapshot lands — their render() then shows its own
// "collecting…" placeholder.
function cardReady(card) {
  if (!_snapshot) return false;
  return cardNeeds(card).every(k => _snapshot[k] != null);
}
const _CARD_SPINNER = '<div class="dash-loading-card"><span class="dash-spinner"></span></div>';

function renderCardBody(el, card) {
  const def = CARD_TYPES[card.type] || CARD_TYPES.custom;
  const body = el.querySelector('.dash-card-body');
  if (!body) return;
  if (def.hero) {
    // Build the chart shell + controls once; the metrics poll must NOT re-render
    // it (that would tear down the SVG / close the metrics menu). refreshMetricChart
    // re-fetches and redraws the SVG in place on its own cadence.
    if (!body.dataset.heroBuilt) { buildMetricChart(body, card); body.dataset.heroBuilt = '1'; refreshMetricChart(card); }
    return;
  }
  if (def.adder) {
    // Build the pill + inline picker once; the 5s metrics poll must NOT re-render
    // it (that would clear a half-typed prompt or close the open picker).
    if (!body.dataset.adderBuilt) { buildAdder(body); body.dataset.adderBuilt = '1'; }
    return;
  }
  if (!cardReady(card)) {
    // Data for this card hasn't arrived yet — show the spinner. Inject it only
    // ONCE (re-writing it every 5s poll would restart the CSS animation); it
    // stays until this card's snapshot section lands.
    if (!body.classList.contains('is-loading')) {
      body.classList.add('is-loading');
      body.innerHTML = _CARD_SPINNER;
    }
    return;
  }
  body.classList.remove('is-loading');
  try {
    body.innerHTML = def.render(_snapshot, card);
  } catch (e) {
    body.innerHTML = `<div class="dash-muted">Card error.</div>`;
  }
  _refreshLucideIcons();
}

function renderAll() {
  if (!elGrid) return;
  elGrid.innerHTML = '';
  for (const card of _cards) {
    const el = cardShell(card);
    elGrid.appendChild(el);
    renderCardBody(el, card);
  }
  // cardShell placed each card for the wide 12-col grid; repositionAll re-lays
  // them (and sets the grid height) honouring flow mode when the panel is narrow.
  repositionAll();
  elGrid.classList.toggle('edit', _editMode);
  if (elEmpty) elEmpty.hidden = _cards.length > 0;
  _refreshLucideIcons();
}

function refreshBodies() {
  if (!elGrid) return;
  for (const card of _cards) {
    const el = elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
    if (el) renderCardBody(el, card);
  }
}

function repositionAll() {
  if (!elGrid) return;
  const flow = flowActive();
  elGrid.classList.toggle('flow', flow);
  if (flow) {
    // Narrow panel: linearise into wrapping tiles that keep a legible min width.
    const { rects, totalH } = computeFlowRects();
    for (const card of _cards) {
      const el = elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
      const r = el && rects.get(card.id);
      if (r) {
        el.style.left = r.left + 'px'; el.style.top = r.top + 'px';
        el.style.width = r.width + 'px'; el.style.height = r.height + 'px';
      }
    }
    elGrid.style.height = totalH + 'px';
    return;
  }
  for (const card of _cards) {
    const el = elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
    if (el) place(el, card);
  }
  elGrid.style.height = gridHeight() + 'px';
}

// ── collision-aware reflow ───────────────────────────────────────────────────
// When a card is moved or resized it must not sit on top of others. `reflow`
// re-flows the whole grid so cards make way and settle upward, like a magnetic
// layout. The active card is "pinned" — it holds exactly where the user put it,
// and every other card drops from the top into the first slot that clears both
// the pinned card and the cards already settled this pass. Dropping-until-clear
// (rather than only checking the row above) is what guarantees zero overlaps
// even with mixed-width cards that can only rise partway.
function overlaps(a, b) {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}
function reflow(activeId) {
  const placed = [];
  const pin = _cards.find(c => c.id === activeId);
  if (pin) placed.push(pin);            // pinned card holds its slot, always avoided
  const rest = _cards.filter(c => c.id !== activeId)
    .sort((a, b) => (a.y - b.y) || (a.x - b.x));
  for (const c of rest) {
    let y = 0;
    while (placed.some(p => overlaps({ x: c.x, y, w: c.w, h: c.h }, p))) y++;
    c.y = y;
    placed.push(c);
  }
}

// ── drag + resize (edit mode) ────────────────────────────────────────────────
let _drag = null;
function onPointerDown(e) {
  if (!_editMode) return;
  // In flow mode cards are auto-arranged (wrapping tiles), so drag-to-move and
  // corner-resize don't map to the 12-col grid — leave them inert there.
  if (flowActive()) return;
  const dragH = e.target.closest('.dash-card-drag');
  const resizeH = e.target.closest('.dash-card-resize');
  if (!dragH && !resizeH) return;
  const el = e.target.closest('.dash-card');
  if (!el) return;
  const card = _cards.find(c => c.id === el.dataset.id);
  if (!card) return;
  e.preventDefault();
  el.classList.add('dragging');
  const cw = cellW();
  _drag = {
    el, card, mode: resizeH ? 'resize' : 'move', cw,
    startX: e.clientX, startY: e.clientY,
    ox: card.x, oy: card.y, ow: card.w, oh: card.h,
  };
  document.addEventListener('pointermove', onPointerMove);
  document.addEventListener('pointerup', onPointerUp, { once: true });
}
function onPointerMove(e) {
  if (!_drag) return;
  const { card, cw, mode } = _drag;
  const dx = Math.round((e.clientX - _drag.startX) / (cw + GAP));
  const dy = Math.round((e.clientY - _drag.startY) / (ROW_H + GAP));
  if (mode === 'move') {
    card.x = Math.max(0, Math.min(COLS - card.w, _drag.ox + dx));
    card.y = Math.max(0, _drag.oy + dy);
  } else {
    card.w = Math.max(2, Math.min(COLS - card.x, _drag.ow + dx));
    card.h = Math.max(1, Math.min(12, _drag.oh + dy));
  }
  reflow(card.id);        // flow other cards out of the way + settle upward
  repositionAll();        // redraw every card at its (possibly shuffled) slot
}
function onPointerUp() {
  if (!_drag) return;
  _drag.el.classList.remove('dragging');
  const wasResize = _drag.mode === 'resize';
  _drag = null;
  document.removeEventListener('pointermove', onPointerMove);
  if (wasResize) { refreshBodies(); redrawMetricChartsFromCache(); }   // re-fit at new size
  scheduleSave();
}

// ── persistence ──────────────────────────────────────────────────────────────
function scheduleSave() {
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    _saveTimer = null;
    _sendJSON('/layout', 'PUT', { cards: slimCards() }).catch(e => console.error('dashboard save failed', e));
  }, 600);
}

// ── add / remove cards ───────────────────────────────────────────────────────
function nextFreeSpot(w, h) {
  // Simple placement: first row-major slot that doesn't overlap an existing card.
  for (let y = 0; y < 200; y++) {
    for (let x = 0; x <= COLS - w; x++) {
      if (!_cards.some(c => x < c.x + c.w && x + w > c.x && y < c.y + c.h && y + h > c.y)) return { x, y };
    }
  }
  return { x: 0, y: 0 };
}
function addCard(spec) {
  const def = CARD_TYPES[spec.type] || CARD_TYPES.custom;
  const w = spec.w || def.w, h = spec.h || def.h;
  const spot = nextFreeSpot(w, h);
  const card = { id: 'c-' + Math.random().toString(36).slice(2, 9), type: spec.type, x: spot.x, y: spot.y, w, h };
  if (spec.type === 'custom') { card.title = spec.title; card.icon = spec.icon; card.viz = spec.viz; card.fields = spec.fields; }
  if (spec.type === 'metric_chart') {
    card.series = (Array.isArray(spec.series) && spec.series.length) ? spec.series.slice() : CHART_DEFAULT_SERIES.slice();
    card.range = spec.range || { window: 3600 };
  }
  _cards.push(card);
  renderAll();
  scheduleSave();
  if (def.chart) refreshChart(card);
}
function removeCard(id) {
  _cards = _cards.filter(c => c.id !== id);
  renderAll();
  scheduleSave();
}

// ── per-card AI assistant (the star button) ──────────────────────────────────
// Every card's star opens a floating WebAgent (manager) chat that is PRE-PRIMED
// with which card this is and where its front-end renderer + back-end metrics
// live — so the admin can just say “change the title”, “swap the icon”, “point
// this at a different number” and the agent knows exactly what to edit. The
// priming text rides the admin's FIRST message invisibly (transformMessage): the
// chat bubble shows only what they typed, not the wall of file pointers.
function cardAssistantMessage(card) {
  const def = CARD_TYPES[card.type] || CARD_TYPES.custom;
  const title = card.title || def.title;
  const L = [];
  L.push('[WebAgent Request | Source: Admin Tools → Dashboard]');
  L.push('The admin clicked the AI star on ONE dashboard card and wants to change that specific card. You are editing the WebAgent codebase.');
  L.push('');
  L.push('CARD BEING UPDATED');
  L.push('• Title on the card: "' + title + '"');
  L.push('• Card type id: ' + card.type);
  if (card.type === 'custom' && Array.isArray(card.fields) && card.fields.length) {
    L.push('• Custom-card fields (label ← data path):');
    card.fields.forEach(f => L.push('    - "' + (f.label || '') + '" ← ' + (f.path || '') + (f.unit ? ' [' + f.unit + ']' : '')));
    if (card.viz) L.push('• Visualisation: ' + card.viz);
  }
  L.push('');
  L.push('WHERE THIS CARD IS DEFINED');
  L.push("• Front-end renderer + registry: ui/admin-tools/dashboard/dashboard.js → CARD_TYPES['" + card.type + "'] — its render() builds the card body; the default title, Lucide icon and grid size are in the same entry.");
  L.push('• Back-end data: ui/admin-tools/dashboard/server.py → _build_snapshot() and its section builders (tokens, db_health, active_runs, devices, failures, context, storage, agents, users, sessions, health, tool_usage, security). The always-fresh live gauges come from app/metrics.py; the metric paths a card may read are listed in server.py as _AI_FIELDS.');
  L.push('• Styling: ui/admin-tools/dashboard/dashboard.css.');
  L.push('• Saved layout: per-admin in data/config/dashboard-layouts.json; the default seed is app/defaults/dashboard.json (server.py _default_cards / save_as_default).');
  L.push('');
  L.push('Briefly confirm which card you are working on, then ask what they would like to change (or just make the change if they have already said).');
  return L.join('\n');
}

function openCardAssistant(card) {
  const def = CARD_TYPES[card.type] || CARD_TYPES.custom;
  const title = card.title || def.title;
  const context = cardAssistantMessage(card);
  let primed = false;
  const w = createChatWidget({
    title: 'Dashboard · ' + title,
    iconName: 'sparkles',
    ensureAgent: app.startWebagentSession,
    // Wrap the admin's FIRST message only: their words go to the agent under the
    // card context so it knows the card; later replies pass through untouched.
    transformMessage: async (text) => {
      if (primed) return text;
      primed = true;
      return context + '\n\nThe admin asked:\n' + text;
    },
  });
  w.open();
}

// ── operational card actions (rows + buttons carrying data-act) ─────────────
// Reveal the chat side-panel if hidden (same best-effort helpers the Agents
// page uses — they live in index.html).
function revealChatPanel() {
  try {
    if (typeof window.__getChatVisible === 'function' && !window.__getChatVisible()
        && typeof window.__applyChatVisible === 'function') {
      window.__applyChatVisible(true);
    }
  } catch (e) { /* best-effort */ }
}

async function _adminUsersPost(path, method) {
  const res = await fetch(apiPath('/admin/users/' + path
    + (method === 'DELETE' ? '?requesting_user_id=' + encodeURIComponent(_uid()) : '')), {
    method: method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: method === 'DELETE' ? undefined : JSON.stringify({ requesting_user_id: _uid() }),
  });
  if (!res.ok) {
    let detail = res.status;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* keep status */ }
    throw new Error(String(detail));
  }
}

function handleCardAction(btn) {
  const act = btn.dataset.act;
  const id = btn.dataset.id || '';
  const busy = (on) => { btn.classList.toggle('busy', on); if (btn.tagName === 'BUTTON') btn.disabled = on; };
  const refreshSoon = () => { setTimeout(poll, 400); };
  try {
    if (act === 'open-agent') {
      revealChatPanel();
      if (typeof app.switchToAgent === 'function') app.switchToAgent(id);
      return;
    }
    if (act === 'open-session') {
      revealChatPanel();
      switchToSession(id);
      return;
    }
    if (act === 'stop-run') {
      if (!window.confirm('Stop this agent run?')) return;
      busy(true);
      _sendJSON('/runs/stop', 'POST', { session_id: id })
        .then(refreshSoon)
        .catch(e => console.error('dashboard stop-run failed', e))
        .finally(() => busy(false));
      return;
    }
    if (act === 'approve-user' || act === 'reject-user') {
      if (act === 'reject-user'
          && !window.confirm('Reject and delete this pending account?')) return;
      busy(true);
      const p = act === 'approve-user'
        ? _adminUsersPost(encodeURIComponent(id) + '/approve', 'POST')
        : _adminUsersPost(encodeURIComponent(id), 'DELETE');
      p.then(refreshSoon)
        .catch(e => console.error('dashboard user action failed', e))
        .finally(() => busy(false));
    }
  } catch (e) {
    console.error('dashboard card action failed', e);
  }
}

// Delegated so it survives every renderAll() rebuild and works whether or not the
// grid is in edit mode (the star is not a drag handle, so onPointerDown ignores it).
function onGridClick(e) {
  const rmBtn = e.target.closest('.dash-card-remove');
  if (rmBtn) {
    const el = rmBtn.closest('.dash-card');
    const card = el && _cards.find(c => c.id === el.dataset.id);
    if (card) removeCard(card.id);
    return;
  }
  // Operational rows/buttons (data-act). Button clicks must not ALSO fire the
  // row's own action, so the closest data-act element wins.
  const actEl = e.target.closest('[data-act]');
  if (actEl && elGrid.contains(actEl)) {
    e.stopPropagation();
    handleCardAction(actEl);
    return;
  }
  const aiBtn = e.target.closest('.dash-card-ai');
  if (!aiBtn) return;
  const el = aiBtn.closest('.dash-card');
  const card = el && _cards.find(c => c.id === el.dataset.id);
  if (card) openCardAssistant(card);
}

// Serialize the current grid to the slim shape the layout endpoints store (same
// fields scheduleSave persists). Shared by the per-admin save and save-as-default.
function slimCards() {
  return _cards.map(c => {
    const o = { id: c.id, type: c.type, x: c.x, y: c.y, w: c.w, h: c.h };
    if (c.type === 'custom') { o.title = c.title; o.icon = c.icon; o.viz = c.viz; o.fields = c.fields; }
    if (c.type === 'metric_chart') { o.series = c.series; o.range = c.range; }
    return o;
  });
}

// Save the CURRENT arrangement as the default everyone gets (fresh admins + anyone
// who hits "reset to default"). Writes the server-side default override. A confirm()
// guards it since it changes the default for all admins, not just this one.
const _SAVE_ICON = '<i data-lucide="save" style="width:15px;height:15px;"></i><span>Save as default</span>';
async function saveAsDefault() {
  if (!window.confirm('Make the current layout the default dashboard? New admins — and anyone who resets — will get this arrangement.')) return;
  const btn = $('dash-savedefault-btn');
  try {
    await _sendJSON('/layout/default', 'POST', { cards: slimCards() });
    // Brief inline confirmation: swap the whole icon to a check, then revert.
    // (Rebuild via innerHTML — Lucide has already replaced the original <i> with
    // an <svg>, so mutating an <i> in place wouldn't take.)
    if (btn) {
      btn.innerHTML = '<i data-lucide="check" style="width:15px;height:15px;"></i><span>Saved</span>';
      btn.classList.add('dash-saved-ok');
      _refreshLucideIcons();
      setTimeout(() => { btn.innerHTML = _SAVE_ICON; btn.classList.remove('dash-saved-ok'); _refreshLucideIcons(); }, 1400);
    }
  } catch (e) {
    console.error('dashboard save-as-default failed', e);
    if (btn) { const t = btn.title; btn.title = 'Could not save default: ' + e.message; setTimeout(() => { btn.title = t; }, 2500); }
  }
}

// Reset to the default layout: drop the saved arrangement server-side and render
// the seed cards it returns. A confirm() guards against wiping a custom layout by
// accident. We do NOT scheduleSave afterwards — leaving the save cleared keeps the
// grid on "default" until the admin next moves something.
async function resetLayout() {
  if (!window.confirm('Reset the dashboard to its default layout? This discards your current arrangement.')) return;
  try {
    const j = await _sendJSON('/layout/reset', 'POST', {});
    _cards = Array.isArray(j.cards) ? j.cards.map(c => ({ ...c })) : [];
    renderAll();
    refreshCharts();
  } catch (e) {
    console.error('dashboard reset failed', e);
  }
}

// ── chart cards (timeseries) ─────────────────────────────────────────────────
async function refreshChart(card) {
  const def = CARD_TYPES[card.type];
  if (!def || !def.chart) return;
  try {
    const j = await _getJSON('/metrics/timeseries?kind=' + def.chart + '&window=1800&buckets=30');
    _tsCache[card.id] = { at: Date.now(), points: j.points || [] };
    const el = elGrid && elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
    if (el) renderCardBody(el, card);
  } catch (e) { /* best-effort */ }
}
function refreshCharts() {
  for (const c of _cards) {
    const def = CARD_TYPES[c.type] || {};
    if (def.chart) refreshChart(c);
    else if (def.hero) refreshMetricChart(c);
  }
}

// ── Metrics-over-time chart (the full-width hero card) ────────────────────────
// A single plot with a shared time X axis: per-model COST as stacked bars (stack
// height = total), and any of tokens-in/out/CPU/RAM as overlaid LINES — each line
// normalised to its own min/max, real numbers shown in the legend. Data comes from
// GET /admin/dashboard/chart (durable tokens/cost from usage_events; live CPU/RAM
// from the in-memory ring). Controls (metrics menu, presets + date range) are built
// once; only the SVG + legend redraw.
function chartSeriesIds(card) {
  return (Array.isArray(card.series) && card.series.length) ? card.series : CHART_DEFAULT_SERIES.slice();
}
function chartQuery(card) {
  const p = new URLSearchParams();
  p.set('series', chartSeriesIds(card).join(','));
  p.set('buckets', '60');
  const r = card.range || { window: 3600 };
  if (r.from && r.to) { p.set('from', r.from); p.set('to', r.to); }
  else { p.set('window', String(r.window || 3600)); }
  return p.toString();
}
function visibleSeries(card) {
  const ids = chartSeriesIds(card);
  const hidden = chartUi(card).hidden;
  return CHART_SERIES.filter(s => ids.includes(s.id) && !hidden.has(s.id));
}

function _lastNonZero(vals) {
  for (let i = vals.length - 1; i >= 0; i--) if (vals[i]) return vals[i];
  return vals.length ? vals[vals.length - 1] : 0;
}
function fmtUnitVal(unit, v) {
  if (v == null) return '—';
  if (unit === '$') return fmtCost(v);
  if (unit === '%') return Number(v).toFixed(0) + '%';
  if (unit === 'ms') return fmtMs(v);
  if (unit === 'MB') return fmtNum(v) + ' MB';
  return fmtNum(v);
}
// A series' one-number legend summary: totals for volume series (tokens, calls),
// the latest reading for level series (%, MB, runs, latency).
function _seriesSummary(s, vals) {
  if (['%', 'MB', 'runs', 'ms'].includes(s.unit)) return fmtUnitVal(s.unit, _lastNonZero(vals));
  return fmtUnitVal(s.unit, vals.reduce((a, b) => a + b, 0));
}

// "Nice" axis ceiling: 1 / 2 / 2.5 / 5 × 10^k at or above the data max.
function niceCeil(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const m = v / p;
  const f = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10;
  return f * p;
}

// Adaptive time ticks: pick a natural step (minute/hour/day multiples) that
// yields ~4-7 ticks, aligned to round times; label format follows the step.
const _TICK_STEPS = [60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400];
function timeTicks(start, end) {
  const span = Math.max(1, end - start);
  let step = _TICK_STEPS[_TICK_STEPS.length - 1];
  for (const s of _TICK_STEPS) { if (span / s <= 7) { step = s; break; } }
  const ticks = [];
  for (let t = Math.ceil(start / step) * step; t <= end; t += step) ticks.push(t);
  return { ticks, step, span };
}
function fmtTick(epoch, span, step) {
  const d = new Date(epoch * 1000);
  const p = (x) => String(x).padStart(2, '0');
  if (step >= 86400 || span > 2 * 86400) return (d.getMonth() + 1) + '/' + d.getDate();
  return p(d.getHours()) + ':' + p(d.getMinutes());
}
function fmtTipTime(epoch, span) {
  const d = new Date(epoch * 1000);
  const p = (x) => String(x).padStart(2, '0');
  const hm = p(d.getHours()) + ':' + p(d.getMinutes());
  if (span > 20 * 3600) return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm;
  return hm;
}

function _legChip(tone, label, val, seriesId, off) {
  const sw = tone ? `background:var(${tone})` : 'background:var(--fg-4)';
  const attrs = seriesId
    ? ` data-series="${_escAttr(seriesId)}" role="button" tabindex="0" title="Show / hide this series"`
    : '';
  return `<span class="dash-leg${seriesId ? ' dash-leg-btn' : ''}${off ? ' off' : ''}"${attrs}>`
    + `<span class="dash-leg-sw" style="${sw}"></span>${_esc(label)} <b>${_esc(String(val))}</b></span>`;
}

// Legend: one chip per SELECTED series (hidden ones render dimmed — click any
// chip to show/hide), plus per-model cost chips when the cost lane is on.
function chartLegend(data, card) {
  const ids = chartSeriesIds(card);
  const hidden = chartUi(card).hidden;
  const chips = [];
  for (const s of CHART_SERIES) {
    if (!ids.includes(s.id)) continue;
    const off = hidden.has(s.id);
    if (s.kind === 'bars') {
      const models = data.cost_models || [];
      let total = 0;
      models.forEach(m => { total += (m.values || []).reduce((a, b) => a + b, 0); });
      chips.push(_legChip('--chart-1', 'Cost', fmtCost(total), 'cost', off));
      if (!off) {
        models.forEach((m, i) => {
          const t = (m.values || []).reduce((a, b) => a + b, 0);
          chips.push(_legChip(m.model === 'Other' ? null : CHART_TONES[i % CHART_TONES.length],
            m.model, fmtCost(t)));
        });
      }
      continue;
    }
    const ln = (data.lines || []).find(l => l.id === s.id);
    const vals = (ln && ln.values) || [];
    chips.push(_legChip(s.tone, s.label, _seriesSummary(s, vals), s.id, off));
  }
  return chips.join('') || `<span class="dash-muted">No metrics selected — use “Metrics”.</span>`;
}

// Group the visible series into unit LANES (small multiples over one X axis).
function chartLanes(data, card) {
  const vis = visibleSeries(card);
  const lanes = [];
  for (const unit of CHART_LANE_ORDER) {
    const ss = vis.filter(s => s.unit === unit);
    if (!ss.length) continue;
    if (unit === '$') {
      lanes.push({ unit, kind: 'bars', models: data.cost_models || [] });
    } else {
      const lines = ss
        .map(s => ({ s, ln: (data.lines || []).find(l => l.id === s.id) }))
        .filter(x => x.ln);
      if (lines.length) lanes.push({ unit, kind: 'lines', lines });
    }
  }
  return lanes;
}

// Rect with only the TOP corners rounded (the bar's data-end); baseline stays square.
function _roundTopRect(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h));
  return `M${(x).toFixed(1)},${(y + h).toFixed(1)} L${x.toFixed(1)},${(y + r).toFixed(1)} `
    + `Q${x.toFixed(1)},${y.toFixed(1)} ${(x + r).toFixed(1)},${y.toFixed(1)} `
    + `L${(x + w - r).toFixed(1)},${y.toFixed(1)} Q${(x + w).toFixed(1)},${y.toFixed(1)} ${(x + w).toFixed(1)},${(y + r).toFixed(1)} `
    + `L${(x + w).toFixed(1)},${(y + h).toFixed(1)} Z`;
}

// The full SVG: stacked unit lanes sharing one time axis. Each lane gets a real,
// zero-based Y scale with nice ticks + hairline gridlines (never two scales on
// one lane). Geometry is stashed on chartUi(card).geom for the hover layer.
function chartSvg(data, card, W, H) {
  const buckets = data.buckets || [];
  const n = buckets.length;
  const lanes = chartLanes(data, card);
  if (!n || !lanes.length) {
    chartUi(card).geom = null;   // no plot → no crosshair mapping
    return `<div class="dash-muted">${n ? 'No metrics visible — pick some under “Metrics”, or click a dimmed legend chip.' : 'No data in this range yet.'}</div>`;
  }

  const L = 8, R = 58, T = 6, B = 20, GAP_L = 16;
  const plotW = Math.max(10, W - L - R);
  const plotH = Math.max(10, H - T - B);
  const laneH = Math.max(24, (plotH - GAP_L * (lanes.length - 1)) / lanes.length);
  const slot = plotW / n;
  const parts = [];
  const geomLanes = [];
  const span = (data.end || 0) - (data.start || 0);

  // Shared vertical time gridlines + labels (adaptive natural steps).
  const tt = timeTicks(data.start || 0, data.end || 1);
  for (const t of tt.ticks) {
    const x = L + ((t - data.start) / Math.max(1, span)) * plotW;
    parts.push(`<line x1="${x.toFixed(1)}" y1="${T}" x2="${x.toFixed(1)}" y2="${(T + plotH).toFixed(1)}" class="dash-chart-grid-v"/>`);
    parts.push(`<text x="${x.toFixed(1)}" y="${(H - 6).toFixed(1)}" class="dash-chart-ax" text-anchor="middle">${_esc(fmtTick(t, tt.span, tt.step))}</text>`);
  }

  lanes.forEach((lane, li) => {
    const y0 = T + li * (laneH + GAP_L);
    const yB = y0 + laneH;

    // Lane max → nice zero-based scale (CPU is pinned to 0-100).
    let max = 0;
    if (lane.kind === 'bars') {
      const totals = new Array(n).fill(0);
      lane.models.forEach(m => (m.values || []).forEach((v, i) => { totals[i] += v; }));
      max = Math.max(...totals, 0);
    } else {
      lane.lines.forEach(x => (x.ln.values || []).forEach(v => { if (v > max) max = v; }));
    }
    const niceMax = lane.unit === '%' ? 100 : niceCeil(max);

    // Horizontal gridlines + right-gutter tick labels (0 · half · max).
    [[yB, 0], [y0 + laneH / 2, niceMax / 2], [y0, niceMax]].forEach(([yy, val], ti) => {
      parts.push(`<line x1="${L}" y1="${yy.toFixed(1)}" x2="${(L + plotW).toFixed(1)}" y2="${yy.toFixed(1)}" class="${ti === 0 ? 'dash-chart-base' : 'dash-chart-grid'}"/>`);
      parts.push(`<text x="${(W - R + 6).toFixed(1)}" y="${(yy + 3).toFixed(1)}" class="dash-chart-ax">${_esc(fmtUnitVal(lane.unit, val))}</text>`);
    });
    // Lane caption (unit group), tucked inside the lane's top-left corner —
    // drawn inside (not above) so the first lane's caption can't clip at T.
    parts.push(`<text x="${(L + 2).toFixed(1)}" y="${(y0 + 9).toFixed(1)}" class="dash-chart-lane">${_esc(CHART_LANE_LABEL[lane.unit] || lane.unit)}</text>`);

    if (lane.kind === 'bars') {
      // Stacked per-model cost bars: ≤24px wide, 2px surface gaps between
      // segments, rounded top on the stack's data-end, square baseline.
      const barW = Math.min(slot * 0.62, 24);
      for (let i = 0; i < n; i++) {
        const bx = L + i * slot + (slot - barW) / 2;
        let yCur = yB;
        const segs = [];
        lane.models.forEach((m, mi) => {
          const v = (m.values || [])[i] || 0;
          if (v <= 0) return;
          const hh = (v / niceMax) * laneH;
          yCur -= hh;
          segs.push({ y: yCur, h: hh, mi, other: m.model === 'Other' });
        });
        segs.forEach((sg, si) => {
          const isTop = si === segs.length - 1;
          const gap = si > 0 ? 2 : 0;               // 2px surface gap between segments
          const h = Math.max(1, sg.h - gap);
          const fill = sg.other ? 'var(--fg-4)' : `var(${CHART_TONES[sg.mi % CHART_TONES.length]})`;
          if (isTop && h > 3) {
            parts.push(`<path d="${_roundTopRect(bx, sg.y, barW, h, 3)}" fill="${fill}" opacity="0.9"/>`);
          } else {
            parts.push(`<rect x="${bx.toFixed(1)}" y="${sg.y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="${fill}" opacity="0.9"/>`);
          }
        });
      }
    } else {
      // 2px lines at bucket centres + an end marker with a surface ring.
      for (const { s, ln } of lane.lines) {
        const vals = ln.values || [];
        const pts = vals.map((v, i) => {
          const x = L + (i + 0.5) * slot;
          const y = yB - (Math.max(0, v) / niceMax) * laneH;
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
        parts.push(`<polyline points="${pts}" fill="none" stroke="var(${s.tone})" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>`);
        if (vals.length) {
          const lastX = L + (vals.length - 0.5) * slot;
          const lastY = yB - (Math.max(0, vals[vals.length - 1]) / niceMax) * laneH;
          parts.push(`<circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5" fill="var(${s.tone})" stroke="var(--bg-elev)" stroke-width="2"/>`);
        }
      }
    }
    geomLanes.push({ unit: lane.unit, kind: lane.kind, y0, laneH, niceMax });
  });

  // Hover geometry for the crosshair/tooltip + drag-zoom layer.
  chartUi(card).geom = {
    L, R, T, B, W, H, plotW, plotH, slot, n,
    start: data.start || 0, end: data.end || 1, lanes: geomLanes,
  };
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" class="dash-chart-svg">${parts.join('')}</svg>`;
}

function drawMetricChart(card) {
  const el = elGrid && elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
  if (!el) return;
  const plot = el.querySelector('.dash-chart-plot');
  const legend = el.querySelector('.dash-chart-legend');
  const data = _chartCache[card.id];
  if (!plot || !data) return;
  const W = plot.clientWidth, H = plot.clientHeight;
  if (W < 40 || H < 30) return;   // panel not laid out yet — a later tick redraws
  // Rebuild the SVG but keep the persistent hover layer (cross + tip + zoombox).
  const svgHtml = chartSvg(data, card, W, H);
  const old = plot.querySelector('.dash-chart-svg, .dash-muted');
  if (old) old.remove();
  plot.insertAdjacentHTML('afterbegin', svgHtml);
  plot.classList.remove('refetching');
  if (legend) legend.innerHTML = chartLegend(data, card);
  const zo = el.querySelector('.dash-chart-zoomout');
  if (zo) zo.hidden = !chartUi(card).zoomStack.length;
}

async function refreshMetricChart(card) {
  // Hold the previous render at reduced opacity while refetching — no flash.
  const el = elGrid && elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
  const plot = el && el.querySelector('.dash-chart-plot');
  if (plot && plot.querySelector('.dash-chart-svg')) plot.classList.add('refetching');
  try {
    const j = await _getJSON('/chart?' + chartQuery(card));
    _chartCache[card.id] = j;
    drawMetricChart(card);
  } catch (e) { if (plot) plot.classList.remove('refetching'); }
}
function redrawMetricChartsFromCache() {
  for (const c of _cards) if ((CARD_TYPES[c.type] || {}).hero && _chartCache[c.id]) drawMetricChart(c);
}

function updateChartControls(body, card) {
  const r = card.range || { window: 3600 };
  const isRange = !!(r.from && r.to);
  body.querySelectorAll('.dash-chart-win').forEach(b =>
    b.classList.toggle('active', !isRange && Number(b.dataset.window) === Number(r.window)));
  const fromI = body.querySelector('.dash-chart-from'), toI = body.querySelector('.dash-chart-to');
  const dateOnly = (v) => /^\d{4}-\d{2}-\d{2}$/.test(String(v || ''));
  if (isRange && dateOnly(r.from) && dateOnly(r.to)) {
    if (fromI) fromI.value = r.from;
    if (toI) toI.value = r.to;
  }
  body.querySelectorAll('input[data-series]').forEach(cb =>
    cb.checked = chartSeriesIds(card).includes(cb.dataset.series));
  const zo = body.querySelector('.dash-chart-zoomout');
  if (zo) zo.hidden = !chartUi(card).zoomStack.length;
}

// ── hover layer: crosshair + all-series tooltip + drag-to-zoom ───────────────
function _tipRow(toneCss, name, val) {
  return `<div class="dash-tip-row"><span class="dash-tip-key" style="background:${toneCss}"></span>`
    + `<span class="dash-tip-name">${_esc(name)}</span><b>${_esc(val)}</b></div>`;
}

function _chartTipHtml(card, bi) {
  const data = _chartCache[card.id];
  if (!data) return '';
  const span = (data.end || 0) - (data.start || 0);
  const rows = [];
  for (const s of visibleSeries(card)) {
    if (s.kind === 'bars') {
      let total = 0;
      (data.cost_models || []).forEach((m, mi) => {
        const v = (m.values || [])[bi] || 0;
        total += v;
        if (v > 0) {
          const tone = m.model === 'Other' ? 'var(--fg-4)' : `var(${CHART_TONES[mi % CHART_TONES.length]})`;
          rows.push(_tipRow(tone, m.model, fmtCost(v)));
        }
      });
      rows.push(_tipRow('var(--fg-4)', 'Total cost', fmtCost(total)));
    } else {
      const ln = (data.lines || []).find(l => l.id === s.id);
      const v = ((ln && ln.values) || [])[bi];
      rows.push(_tipRow(`var(${s.tone})`, s.label, fmtUnitVal(s.unit, v || 0)));
    }
  }
  const ts = (data.buckets || [])[bi] || 0;
  return `<div class="dash-tip-head">${_esc(fmtTipTime(ts, span))}</div>` + rows.join('');
}

function wireChartHover(plot, card) {
  const cross = document.createElement('div');
  cross.className = 'dash-chart-cross';
  cross.hidden = true;
  const tip = document.createElement('div');
  tip.className = 'dash-chart-tip';
  tip.hidden = true;
  const zbox = document.createElement('div');
  zbox.className = 'dash-chart-zoombox';
  zbox.hidden = true;
  plot.append(cross, tip, zbox);

  let dragX0 = null;

  const hide = () => { cross.hidden = true; tip.hidden = true; };
  const geom = () => (chartUi(card).geom || null);

  plot.addEventListener('pointerleave', () => { hide(); zbox.hidden = true; dragX0 = null; });

  plot.addEventListener('pointerdown', (e) => {
    const g = geom();
    if (!g || e.button !== 0) return;
    const rect = plot.getBoundingClientRect();
    const px = e.clientX - rect.left;
    if (px < g.L || px > g.L + g.plotW) return;
    dragX0 = px;
    try { plot.setPointerCapture(e.pointerId); } catch (err) { /* best-effort */ }
  });

  plot.addEventListener('pointermove', (e) => {
    const g = geom();
    if (!g) return;
    const rect = plot.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    if (dragX0 != null && Math.abs(px - dragX0) > 6) {
      // Drag-to-zoom selection band.
      const x0 = Math.max(g.L, Math.min(dragX0, px));
      const x1 = Math.min(g.L + g.plotW, Math.max(dragX0, px));
      zbox.style.left = x0 + 'px';
      zbox.style.width = (x1 - x0) + 'px';
      zbox.style.top = g.T + 'px';
      zbox.style.height = g.plotH + 'px';
      zbox.hidden = false;
      tip.hidden = true;
      cross.hidden = true;
      return;
    }

    if (px < g.L || px > g.L + g.plotW) { hide(); return; }
    const bi = Math.max(0, Math.min(g.n - 1, Math.floor((px - g.L) / g.slot)));
    const cx = g.L + (bi + 0.5) * g.slot;
    cross.style.left = cx + 'px';
    cross.style.top = g.T + 'px';
    cross.style.height = g.plotH + 'px';
    cross.hidden = false;

    tip.innerHTML = _chartTipHtml(card, bi);
    tip.hidden = false;
    // Flip the tooltip to whichever side of the crosshair has room.
    const tw = tip.offsetWidth || 160;
    const left = (cx + 14 + tw > plot.clientWidth) ? (cx - 14 - tw) : (cx + 14);
    tip.style.left = Math.max(2, left) + 'px';
    tip.style.top = Math.max(2, Math.min(py + 12, plot.clientHeight - tip.offsetHeight - 4)) + 'px';
  });

  plot.addEventListener('pointerup', (e) => {
    const g = geom();
    const wasDrag = dragX0 != null;
    const rect = plot.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const x0 = dragX0;
    dragX0 = null;
    zbox.hidden = true;
    if (!g || !wasDrag || Math.abs(px - x0) <= 6) return;
    // Zoom into the dragged time span (transient — not saved to the layout;
    // the Zoom-out button unwinds, presets reset).
    const t = (x) => g.start + (Math.max(g.L, Math.min(x, g.L + g.plotW)) - g.L) / g.plotW * (g.end - g.start);
    const t0 = Math.floor(Math.min(t(x0), t(px)));
    const t1 = Math.ceil(Math.max(t(x0), t(px)));
    if (t1 - t0 < 30) return;   // too narrow to be meaningful
    chartUi(card).zoomStack.push(card.range || { window: 3600 });
    card.range = {
      from: new Date(t0 * 1000).toISOString(),
      to: new Date(t1 * 1000).toISOString(),
    };
    const body = plot.closest('.dash-card-body');
    if (body) updateChartControls(body, card);
    refreshMetricChart(card);
  });
}

function buildMetricChart(body, card) {
  body.innerHTML =
    `<div class="dash-chart">`
    + `<div class="dash-chart-ctl">`
    + `<div class="dash-chart-times">`
    + CHART_WINDOWS.map(w => `<button type="button" class="dash-chart-win" data-window="${w.s}">${w.label}</button>`).join('')
    + `<span class="dash-chart-range">`
    + `<input type="date" class="dash-chart-from dash-select" title="Range start">`
    + `<span class="dash-chart-dash">–</span>`
    + `<input type="date" class="dash-chart-to dash-select" title="Range end">`
    + `<button type="button" class="dash-chart-apply dash-btn">Apply</button>`
    + `</span>`
    + `<button type="button" class="dash-chart-zoomout dash-btn" hidden title="Undo the last drag-zoom"><i data-lucide="undo-2" style="width:13px;height:13px;"></i><span>Zoom out</span></button>`
    + `</div>`
    + `<div class="dash-chart-metrics">`
    + `<button type="button" class="dash-chart-metrics-btn dash-btn"><i data-lucide="sliders-horizontal" style="width:14px;height:14px;"></i><span>Metrics</span></button>`
    + `<div class="dash-chart-menu" hidden>`
    + CHART_SERIES.map(s => `<label class="dash-chart-opt"><input type="checkbox" data-series="${s.id}"><span>${_esc(s.label)}</span></label>`).join('')
    + `</div></div>`
    + `</div>`
    + `<div class="dash-chart-legend"></div>`
    + `<div class="dash-chart-plot"></div>`
    + `</div>`;

  const q = (sel) => body.querySelector(sel);
  const menu = q('.dash-chart-menu');
  const plot = q('.dash-chart-plot');

  body.querySelectorAll('.dash-chart-win').forEach(b => b.addEventListener('click', () => {
    card.range = { window: Number(b.dataset.window) || 3600 };
    chartUi(card).zoomStack = [];
    updateChartControls(body, card); scheduleSave(); refreshMetricChart(card);
  }));
  q('.dash-chart-apply').addEventListener('click', () => {
    const from = q('.dash-chart-from').value, to = q('.dash-chart-to').value;
    if (!from || !to) return;
    card.range = { from, to };
    chartUi(card).zoomStack = [];
    updateChartControls(body, card); scheduleSave(); refreshMetricChart(card);
  });
  q('.dash-chart-zoomout').addEventListener('click', () => {
    const stack = chartUi(card).zoomStack;
    if (stack.length) card.range = stack.pop();
    updateChartControls(body, card); refreshMetricChart(card);
  });
  q('.dash-chart-metrics-btn').addEventListener('click', () => { menu.hidden = !menu.hidden; });
  menu.querySelectorAll('input[data-series]').forEach(cb => cb.addEventListener('change', () => {
    const ids = new Set(chartSeriesIds(card));
    if (cb.checked) ids.add(cb.dataset.series); else ids.delete(cb.dataset.series);
    card.series = CHART_SERIES.map(s => s.id).filter(id => ids.has(id));
    chartUi(card).hidden.delete(cb.dataset.series);   // re-adding always shows it
    scheduleSave(); refreshMetricChart(card);
  }));
  // Legend chips toggle series visibility in place (delegated — the legend is
  // re-rendered on every draw).
  q('.dash-chart-legend').addEventListener('click', (e) => {
    const chip = e.target.closest('.dash-leg-btn');
    if (!chip) return;
    const sid = chip.dataset.series;
    const hidden = chartUi(card).hidden;
    if (hidden.has(sid)) hidden.delete(sid); else hidden.add(sid);
    drawMetricChart(card);
  });
  wireChartHover(plot, card);

  updateChartControls(body, card);
  _refreshLucideIcons();
}

// ── polling ──────────────────────────────────────────────────────────────────
// The FAST first fill: /metrics?live=1 is DB-free, so the live / db-health /
// storage cards populate almost immediately (even on a slow remote DB) while the
// heavier DB-backed cards keep their spinner until the full poll() lands. Merged
// (not replaced) so the sections it carries appear the instant they're back.
async function pollLive() {
  try {
    const live = await _getJSON('/metrics?live=1&window=' + _window);
    _snapshot = Object.assign(_snapshot || {}, live);
    const dot = $('dash-live-dot');
    if (dot) dot.classList.add('on');
    onDashboardSnapshot(_snapshot);
    refreshBodies();
  } catch (e) { /* the full poll below fills everything anyway */ }
}
async function poll() {
  try {
    const full = await _getJSON('/metrics?window=' + _window);
    // Merge so a section the full snapshot happens to omit doesn't blank a card
    // the fast poll already filled; the full poll carries every section normally.
    _snapshot = Object.assign(_snapshot || {}, full);
    const dot = $('dash-live-dot');
    if (dot) dot.classList.add('on');
    onDashboardSnapshot(_snapshot);   // keep the instance header's DB/health pills live
    refreshBodies();
  } catch (e) {
    const dot = $('dash-live-dot');
    if (dot) dot.classList.remove('on');
  }
}
function startPolling() {
  stopPolling();
  pollLive();   // instant partial fill (live / db-health / storage cards)
  poll();       // full snapshot (DB-backed cards fill when it returns)
  refreshCharts();
  _pollTimer = setInterval(() => {
    if (document.hidden) return;
    poll();
  }, POLL_MS);
  _chartTimer = setInterval(() => { if (!document.hidden) refreshCharts(); }, 30000);
}
let _chartTimer = null;
function stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  if (_chartTimer) { clearInterval(_chartTimer); _chartTimer = null; }
}

// ── the "Add a card" tile (inline — no modal) ────────────────────────────────
// Builds the card body: a chat pill (shared .chat-pill design) whose + button
// reveals an inline grid of built-in card types, and whose send button turns a
// plain-English prompt into an AI-designed card. Wired once per card element.
function buildAdder(body) {
  body.innerHTML =
    `<div class="dash-adder">`
    + `<div class="dash-adder-pill chat-pill chat-pill-1line no-voice">`
    + `<button type="button" class="chat-pill-attach dash-adder-plus" title="Browse card types"><i data-lucide="plus" style="width:18px;height:18px;"></i></button>`
    + `<textarea class="chat-pill-input dash-adder-input" rows="1" placeholder="Describe a card and AI will build it — e.g. “tokens used per agent today”" autocomplete="off"></textarea>`
    + `<button type="button" class="chat-pill-send dash-adder-send" title="Generate with AI"><i data-lucide="sparkles" style="width:16px;height:16px;"></i></button>`
    + `</div>`
    + `<div class="dash-adder-status" hidden></div>`
    + `<div class="dash-adder-picker" hidden><div class="dash-adder-grid"></div></div>`
    + `</div>`;
  const pill = body.querySelector('.dash-adder-pill');
  const plus = body.querySelector('.dash-adder-plus');
  const input = body.querySelector('.dash-adder-input');
  const send = body.querySelector('.dash-adder-send');
  const picker = body.querySelector('.dash-adder-picker');
  const grid = body.querySelector('.dash-adder-grid');

  grid.innerHTML = PICKER_TYPES.map(t => {
    const d = CARD_TYPES[t];
    return `<button class="dash-picker-item" data-type="${t}">`
      + `<i data-lucide="${_escAttr(d.icon)}" style="width:18px;height:18px;"></i>`
      + `<span>${_esc(d.title)}</span></button>`;
  }).join('');
  grid.querySelectorAll('.dash-picker-item').forEach(b =>
    b.addEventListener('click', () => addCard({ type: b.dataset.type })));

  plus.addEventListener('click', () => {
    const show = picker.hidden;
    picker.hidden = !show;
    plus.classList.toggle('active', show);
    if (show) _refreshLucideIcons();
  });
  input.addEventListener('input', () => pill.classList.toggle('has-text', !!input.value.trim()));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); adderGenerate(body); }
  });
  send.addEventListener('click', () => adderGenerate(body));
  _refreshLucideIcons();
}

async function adderGenerate(body) {
  const input = body.querySelector('.dash-adder-input');
  const status = body.querySelector('.dash-adder-status');
  const send = body.querySelector('.dash-adder-send');
  if (!input) return;
  const prompt = input.value.trim();
  if (!prompt) { input.focus(); return; }
  if (status) { status.hidden = false; status.className = 'dash-adder-status'; status.textContent = 'Designing your card…'; }
  if (send) send.disabled = true;
  try {
    const spec = await _sendJSON('/ai-card', 'POST', { prompt });
    input.value = '';
    addCard(spec);   // addCard → renderAll rebuilds a fresh (empty) adder; the new
                     // card appearing is the confirmation.
  } catch (e) {
    if (status) { status.hidden = false; status.className = 'dash-adder-status err'; status.textContent = 'Could not generate: ' + e.message; }
    if (send) send.disabled = false;
  }
}

// Toolbar / empty-state entry point: make sure an adder tile exists, then bring
// it into view and focus its prompt. Existing saved layouts (from before the
// adder tile) get one added here on demand.
function ensureAdder() {
  let card = _cards.find(c => c.type === 'add_card');
  if (!card) { addCard({ type: 'add_card' }); card = _cards.find(c => c.type === 'add_card'); }
  if (!card) return;
  const el = elGrid && elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  el.classList.add('dash-flash');
  setTimeout(() => el.classList.remove('dash-flash'), 900);
  const inp = el.querySelector('.dash-adder-input');
  if (inp) setTimeout(() => inp.focus(), 120);
}

// ── edit mode toggle ─────────────────────────────────────────────────────────
function toggleEdit() {
  _editMode = !_editMode;
  const btn = $('dash-edit-btn');
  if (btn) btn.classList.toggle('active', _editMode);
  if (elGrid) elGrid.classList.toggle('edit', _editMode);
}

// ── "More" actions dropdown ─────────────────────────────────────────────────
// Edit / Add card / Save as default / Reset live here so the bar keeps only the
// time window + refresh. Anchored to the ⋮ button; closes on item click or an
// outside click (see the document click handler in wire()).
function toggleMoreMenu() {
  const m = $('dash-more-menu'), b = $('dash-more-btn');
  if (!m) return;
  const open = m.hidden;   // was hidden → we're opening it
  m.hidden = !open;
  if (b) { b.setAttribute('aria-expanded', String(open)); b.classList.toggle('active', open); }
}
function closeMoreMenu() {
  const m = $('dash-more-menu'), b = $('dash-more-btn');
  if (m) m.hidden = true;
  if (b) { b.setAttribute('aria-expanded', 'false'); b.classList.remove('active'); }
}

// ── lifecycle ────────────────────────────────────────────────────────────────
let _wired = false;
function wire() {
  if (_wired) return;
  _wired = true;
  elGrid = $('dash-grid'); elWrap = $('dash-grid-wrap'); elEmpty = $('dash-empty');
  const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
  on('dash-add-btn', 'click', ensureAdder);
  on('dash-empty-add', 'click', ensureAdder);
  on('dash-edit-btn', 'click', toggleEdit);
  on('dash-savedefault-btn', 'click', saveAsDefault);
  on('dash-reset-btn', 'click', resetLayout);
  on('dash-refresh-btn', 'click', () => { poll(); refreshCharts(); });
  // "More" dropdown: toggle on the ⋮ button, close after any item click.
  on('dash-more-btn', 'click', (e) => { e.stopPropagation(); toggleMoreMenu(); });
  on('dash-more-menu', 'click', (e) => { if (e.target.closest('button')) closeMoreMenu(); });
  const sel = $('dash-window');
  if (sel) sel.addEventListener('change', () => { _window = Number(sel.value) || 3600; poll(); });
  if (elGrid) elGrid.addEventListener('pointerdown', onPointerDown);
  if (elGrid) elGrid.addEventListener('click', onGridClick);
  // Close any open chart Metrics menu — and the toolbar "More" menu — when
  // clicking outside its button/menu.
  document.addEventListener('click', (e) => {
    document.querySelectorAll('.dash-chart-menu').forEach(m => {
      if (m.hidden) return;
      const wrap = m.closest('.dash-chart-metrics');
      if (wrap && !wrap.contains(e.target)) m.hidden = true;
    });
    const more = $('dash-more');
    if (more && !more.contains(e.target)) closeMoreMenu();
  });
  if (elWrap && 'ResizeObserver' in window) {
    _ro = new ResizeObserver(() => { repositionAll(); redrawMetricChartsFromCache(); });
    _ro.observe(elWrap);
  }
  // The instance-switcher header lives above the grid; it owns its own DOM + API.
  // Init once here; poll() feeds it the snapshot so its DB/health pills stay live.
  initInstancesHeader();
}

// Full-panel spinner shown while the layout is loading — i.e. BEFORE the card
// sizes are even known. Once renderAll() has laid the cards out, this hides and
// the per-card spinners (renderCardBody) take over until each card's data lands.
function showGridLoading(on) {
  const el = $('dash-loading');
  if (el) el.hidden = !on;
}

export async function startView() {
  wire();
  if (_started) {
    // Returning to the view: the panel width may have changed while away.
    whenSized(() => { repositionAll(); refreshBodies(); redrawMetricChartsFromCache(); });
    startPolling();
    return;
  }
  // First open: show the overall spinner until the layout is fetched and the grid
  // is laid out (the point where per-card spinners can take over).
  showGridLoading(true);
  // Load the saved layout. Only mark the view "started" on SUCCESS, so a
  // transient failure (e.g. the auth wrapper not yet installed right at boot)
  // retries on the next activation instead of sticking on an empty grid.
  try {
    const j = await _getJSON('/layout');
    _cards = Array.isArray(j.cards) ? j.cards : [];
    _started = true;
  } catch (e) {
    console.error('dashboard layout load failed (will retry on next open)', e);
  }
  // Render the card shells (each with a per-card spinner) as soon as the panel
  // has a real width — do NOT wait on the first metrics poll, which can take
  // seconds on a slow remote DB. Hide the overall spinner now that the sizes are
  // known, then startPolling() fires the fast + full polls that fill the bodies.
  // The ResizeObserver keeps cards positioned afterwards (rail toggle / window
  // resize / a panel that widens after the tab activates).
  whenSized(() => { renderAll(); showGridLoading(false); startPolling(); });
}

export function stopView() {
  stopPolling();
}
