// Diagnostics sub-page (Admin Tools → Diagnostics).
//
// Reads the in-app flight-recorder via GET /api/v1/diagnostics (admin-only)
// and renders a filterable, optionally-live feed of recent records: server
// errors + tracebacks, agent-loop pipeline problems, run outcomes and tool
// errors. Mirrors the Interactions / Runtime-Loop sub-page lifecycle
// (start / stop / renderSidebar) wired in files.js.

import { apiPath } from './config.js';

const LEVELS = [
  { key: 'error', label: 'Errors' },
  { key: 'warning', label: 'Warnings' },
  { key: 'info', label: 'Info' },
];
const CATEGORIES = [
  { key: 'server', label: 'Server' },
  { key: 'loop', label: 'Loop' },
  { key: 'run', label: 'Runs' },
  { key: 'tool', label: 'Tools' },
];

const state = {
  levels: new Set(['error', 'warning']),  // default: the problems
  categories: new Set(),                   // empty = all categories
  search: '',
  sinceMinutes: 60,
  live: false,
  timer: null,
  wiredSidebar: false,
  wiredMain: false,
};

const POLL_MS = 5000;

function _qs(id) { return document.getElementById(id); }

function authHeaders() {
  const t = localStorage.getItem('auth_token');
  return t ? { Authorization: 'Bearer ' + t } : {};
}

// ── Lifecycle ────────────────────────────────────────────────────────────

export function startDiagnostics() {
  renderDiagnosticsSidebar();
  _wireMain();
  fetchAndRender();
  _applyLiveTimer();
}

export function stopDiagnostics() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

export function renderDiagnosticsSidebar() {
  _renderChips('diag-levels', LEVELS, state.levels);
  _renderChips('diag-categories', CATEGORIES, state.categories);
  if (state.wiredSidebar) return;
  const search = _qs('diag-search');
  if (search) {
    search.value = state.search;
    search.addEventListener('input', _debounce(() => {
      state.search = search.value.trim();
      fetchAndRender();
    }, 300));
  }
  const win = _qs('diag-window');
  if (win) {
    win.value = String(state.sinceMinutes);
    win.addEventListener('change', () => {
      state.sinceMinutes = win.value === '' ? null : Number(win.value);
      fetchAndRender();
    });
  }
  const live = _qs('diag-live');
  if (live) {
    live.checked = state.live;
    live.addEventListener('change', () => {
      state.live = live.checked;
      _applyLiveTimer();
      if (state.live) fetchAndRender();
    });
  }
  const refresh = _qs('diag-refresh');
  if (refresh && !refresh.dataset.wired) {
    refresh.dataset.wired = '1';
    refresh.addEventListener('click', () => fetchAndRender());
  }
  state.wiredSidebar = true;
}

function _wireMain() {
  if (state.wiredMain) return;
  const r = _qs('diag-refresh-main');
  if (r) r.addEventListener('click', () => fetchAndRender());
  state.wiredMain = true;
}

function _applyLiveTimer() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  const dot = _qs('diag-live-dot');
  if (dot) dot.hidden = !state.live;
  if (state.live) state.timer = setInterval(fetchAndRender, POLL_MS);
}

// ── Filter chips ──────────────────────────────────────────────────────────

function _renderChips(containerId, defs, selectedSet) {
  const box = _qs(containerId);
  if (!box) return;
  box.innerHTML = '';
  for (const d of defs) {
    const b = document.createElement('button');
    b.className = 'diag-chip' + (selectedSet.has(d.key) ? ' active' : '');
    b.dataset.key = d.key;
    b.textContent = d.label;
    b.addEventListener('click', () => {
      if (selectedSet.has(d.key)) selectedSet.delete(d.key);
      else selectedSet.add(d.key);
      b.classList.toggle('active', selectedSet.has(d.key));
      fetchAndRender();
    });
    box.appendChild(b);
  }
}

// ── Fetch + render ─────────────────────────────────────────────────────────

function _buildQuery() {
  const p = new URLSearchParams();
  // "Errors" implies critical too.
  const levels = new Set(state.levels);
  if (levels.has('error')) levels.add('critical');
  if (levels.size) p.set('levels', [...levels].join(','));
  if (state.categories.size) p.set('categories', [...state.categories].join(','));
  if (state.search) p.set('search', state.search);
  if (state.sinceMinutes != null) p.set('since_minutes', String(state.sinceMinutes));
  p.set('limit', '300');
  return p.toString();
}

async function fetchAndRender() {
  const feed = _qs('diag-feed');
  if (!feed) return;
  try {
    const url = apiPath('/api/v1/diagnostics?' + _buildQuery());
    const res = await fetch(url, { headers: authHeaders() });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}
      feed.innerHTML = '';
      feed.appendChild(_hint(res.status === 403
        ? 'Admin access required to view diagnostics.'
        : ('Could not load diagnostics: ' + msg)));
      _setStats(null);
      return;
    }
    const data = await res.json();
    _renderRecords(data.records || []);
  } catch (e) {
    feed.innerHTML = '';
    feed.appendChild(_hint('Could not reach the diagnostics endpoint: ' + (e && e.message ? e.message : e)));
    _setStats(null);
  }
}

function _renderRecords(records) {
  const feed = _qs('diag-feed');
  if (!feed) return;
  feed.innerHTML = '';
  if (!records.length) {
    feed.appendChild(_hint('No diagnostics match these filters. Widen the window, add severities, or clear the category filter.'));
    _setStats({ shown: 0, error: 0, warning: 0 });
    return;
  }
  let nErr = 0, nWarn = 0;
  for (const r of records) {
    const lvl = (r.level || 'info').toLowerCase();
    if (lvl === 'error' || lvl === 'critical') nErr++;
    else if (lvl === 'warning') nWarn++;
    feed.appendChild(_recordRow(r, lvl));
  }
  _setStats({ shown: records.length, error: nErr, warning: nWarn });
}

function _recordRow(r, lvl) {
  const row = document.createElement('div');
  row.className = 'diag-row lvl-' + lvl;

  const head = document.createElement('div');
  head.className = 'diag-row-head';

  const badge = document.createElement('span');
  badge.className = 'diag-badge lvl-' + lvl;
  badge.textContent = lvl;
  head.appendChild(badge);

  const cat = document.createElement('span');
  cat.className = 'diag-cat';
  cat.textContent = r.category || '';
  head.appendChild(cat);

  const msg = document.createElement('span');
  msg.className = 'diag-msg';
  msg.textContent = r.message || '';
  head.appendChild(msg);

  const ts = document.createElement('span');
  ts.className = 'diag-ts';
  ts.textContent = _fmtTime(r.ts);
  head.appendChild(ts);

  row.appendChild(head);

  const meta = [];
  if (r.source) meta.push('source: ' + r.source);
  if (r.session_id) meta.push('session: ' + String(r.session_id).slice(0, 12));
  if (r.agent_id) meta.push('agent: ' + String(r.agent_id).slice(0, 12));
  const hasDetail = r.detail != null && (typeof r.detail !== 'object' || Object.keys(r.detail).length);

  if (meta.length || hasDetail) {
    const sub = document.createElement('div');
    sub.className = 'diag-row-sub';
    if (meta.length) {
      const m = document.createElement('span');
      m.className = 'diag-meta';
      m.textContent = meta.join('  ·  ');
      sub.appendChild(m);
    }
    if (hasDetail) {
      const toggle = document.createElement('button');
      toggle.className = 'diag-detail-toggle';
      toggle.textContent = 'detail';
      const pre = document.createElement('pre');
      pre.className = 'diag-detail';
      pre.hidden = true;
      pre.textContent = typeof r.detail === 'string' ? r.detail : JSON.stringify(r.detail, null, 2);
      toggle.addEventListener('click', () => {
        pre.hidden = !pre.hidden;
        toggle.classList.toggle('active', !pre.hidden);
      });
      sub.appendChild(toggle);
      row.appendChild(sub);
      row.appendChild(pre);
    } else {
      row.appendChild(sub);
    }
  }
  return row;
}

function _setStats(s) {
  const el = _qs('diag-stats');
  if (!el) return;
  if (!s) { el.textContent = ''; return; }
  const bits = [s.shown + ' shown'];
  if (s.error) bits.push(s.error + ' error' + (s.error === 1 ? '' : 's'));
  if (s.warning) bits.push(s.warning + ' warning' + (s.warning === 1 ? '' : 's'));
  el.textContent = bits.join('  ·  ');
}

// ── Small helpers ───────────────────────────────────────────────────────────

function _hint(text) {
  const d = document.createElement('div');
  d.className = 'diag-hint';
  d.textContent = text;
  return d;
}

function _fmtTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    return sameDay ? t : (d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + t);
  } catch (_) {
    return iso;
  }
}

function _debounce(fn, ms) {
  let h = null;
  return (...a) => { if (h) clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}
