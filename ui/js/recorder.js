'use strict';

// ── Client render recorder (browser flight recorder) ──────────────────────────
//
// Records what the BROWSER actually rendered and felt — HTML snapshots,
// DOM-mutation bursts, lag / long-task stalls, JS errors, console warnings and
// failed/slow network calls — and ships them to /api/v1/recordings, stamped with
// session_seq so each row joins exactly to the interaction / diagnostics row
// beside it on the server. This is the client-side blind spot the server logs
// can never see (an event arrives + logs clean, then the JS that renders it
// throws or stutters).
//
// OFF by default. The server's render_recording_enabled flag gates everything:
// when off, /config returns {enabled:false} and this module stays completely
// passive (no observers, no listeners, no flush). It is an investigation tool —
// flip it on while chasing a UI/lag bug, off again when done.
//
// LEVEL 1 (now): periodic full HTML snapshots + signals. LEVEL 2 (later): the
// MutationObserver callback already computes per-burst deltas (see _onMutations)
// — emitting them as kind:'mutation' rows would give full scrub-able replay
// without any server/schema change.
//
// Design rule: the recorder must never throw into the app and must never become
// the source of the lag it is measuring. Every hook is try/wrapped; snapshots
// are throttled, size-capped, and taken in idle time.

import { app } from './state.js';
import { randomUUID } from './uuid.js';

const CONFIG_URL = '/api/v1/recordings/config';
const INGEST_URL = '/api/v1/recordings';
const CONFIG_POLL_MS = 60000;   // re-check the on/off flag so toggling needs no reload

let _clientId = null;
let _seq = 0;
let _buffer = [];
let _active = false;            // currently capturing?
let _cfg = null;                // server-provided capture knobs
let _flushTimer = null;
let _configTimer = null;

// Teardown handles for everything we install, so we can fully stop on disable.
let _mutObserver = null;
let _perfObserver = null;
let _origFetch = null;
let _origConsole = {};          // { warn, error }
const _winListeners = [];       // [ [type, fn] ]

let _lastSnapshotAt = 0;
let _snapshotScheduled = false;
let _mutAccum = null;           // running mutation summary since last snapshot

// ── Helpers ───────────────────────────────────────────────────────────────────

function _nowIso() { return new Date().toISOString(); }

function _ensureClientId() {
  if (_clientId) return _clientId;
  try {
    _clientId = sessionStorage.getItem('webagent.recorder.clientId');
    if (!_clientId) {
      _clientId = randomUUID();
      sessionStorage.setItem('webagent.recorder.clientId', _clientId);
    }
  } catch (_) {
    _clientId = randomUUID();
  }
  return _clientId;
}

// Common correlation coordinates pulled live from app state at capture time.
function _coords() {
  let sessionSeq = null;
  try {
    const sid = app.currentSessionId;
    if (sid && app.lastSessionSeq && typeof app.lastSessionSeq[sid] === 'number') {
      sessionSeq = app.lastSessionSeq[sid];
    }
  } catch (_) {}
  return {
    session_id: (app && app.currentSessionId) || null,
    session_seq: sessionSeq,
    user_id: (app && app.currentUserId) || null,
    agent_id: (app && app.currentAgentId) || null,
    client_id: _ensureClientId(),
    url: location.pathname + location.search,
  };
}

// Push one record onto the outgoing buffer. Never throws.
function _record(kind, fields) {
  if (!_active) return;
  try {
    _buffer.push(Object.assign({
      kind,
      ts: _nowIso(),
      seq: _seq++,
    }, _coords(), fields || {}));
    // Hard safety cap so a runaway loop can't grow the buffer without bound.
    if (_buffer.length > 1000) _buffer.splice(0, _buffer.length - 1000);
  } catch (_) {}
}

// ── Flush ───────────────────────────────────────────────────────────────────--

function _flush(useBeacon) {
  if (!_buffer.length) return;
  const records = _buffer;
  _buffer = [];
  const body = JSON.stringify({ records });
  try {
    if (useBeacon) {
      // Unload path only. sendBeacon (and keepalive fetch) cap the body at ~64KB,
      // so a whole-page HTML snapshot would be rejected — split it: ship the
      // oversized snapshots' metadata sans html, and beacon the rest. Best-effort;
      // the periodic fetch path (below) is what reliably persists big snapshots.
      const small = [], big = [];
      for (const r of records) {
        if (r.html && r.html.length > 50000) big.push(r); else small.push(r);
      }
      if (small.length && navigator.sendBeacon) {
        navigator.sendBeacon(INGEST_URL, new Blob([JSON.stringify({ records: small })], { type: 'application/json' }));
      }
      // For big snapshots at unload, drop the html (keep the timing/metadata row)
      // rather than lose the record entirely to the 64KB beacon cap.
      if (big.length && navigator.sendBeacon) {
        const stripped = big.map((r) => Object.assign({}, r, { html: null, html_bytes: r.html ? r.html.length : null, label: (r.label || 'snapshot') + ' (html dropped at unload)' }));
        navigator.sendBeacon(INGEST_URL, new Blob([JSON.stringify({ records: stripped })], { type: 'application/json' }));
      }
      return;
    }
    // Periodic path: a plain fetch (NO keepalive) has no 64KB body cap, so full
    // whole-page snapshots persist. Recordings are best-effort — drop on failure.
    const token = localStorage.getItem('auth_token');
    fetch(INGEST_URL, {
      method: 'POST',
      headers: Object.assign(
        { 'Content-Type': 'application/json' },
        token ? { Authorization: `Bearer ${token}` } : {},
      ),
      body,
    }).catch(() => { /* drop on failure — recordings are best-effort */ });
  } catch (_) {}
}

// ── Capture: JS errors ─────────────────────────────────────────────────────--

function _installErrorCapture() {
  const onErr = (e) => {
    try {
      const src = e.filename ? (e.filename.split('/').pop() + ':' + e.lineno + ':' + e.colno) : 'unknown';
      _record('js_error', {
        level: 'error',
        label: String(e.message || 'error').slice(0, 300),
        detail: { source: src, stack: (e.error && e.error.stack) ? String(e.error.stack).slice(0, 4000) : null },
      });
    } catch (_) {}
  };
  const onRej = (e) => {
    try {
      const reason = e.reason;
      const msg = (reason && reason.message) ? reason.message : String(reason);
      _record('js_error', {
        level: 'error',
        label: ('unhandledrejection: ' + msg).slice(0, 300),
        detail: { stack: (reason && reason.stack) ? String(reason.stack).slice(0, 4000) : null },
      });
    } catch (_) {}
  };
  window.addEventListener('error', onErr);
  window.addEventListener('unhandledrejection', onRej);
  _winListeners.push(['error', onErr], ['unhandledrejection', onRej]);
}

// ── Capture: console warnings/errors ───────────────────────────────────────--

function _installConsoleCapture() {
  ['warn', 'error'].forEach((lvl) => {
    _origConsole[lvl] = console[lvl];
    console[lvl] = function (...args) {
      try {
        const text = args.map((a) => {
          if (typeof a === 'string') return a;
          try { return JSON.stringify(a); } catch (_) { return String(a); }
        }).join(' ').slice(0, 600);
        // Skip our own error-banner echo to avoid recursion noise.
        if (!text.startsWith('[webAgent JS error]')) {
          _record('console', { level: lvl === 'warn' ? 'warning' : 'error', label: text });
        }
      } catch (_) {}
      return _origConsole[lvl].apply(console, args);
    };
  });
}

// ── Capture: network (fetch) ───────────────────────────────────────────────--

function _installNetworkCapture() {
  _origFetch = window.fetch;
  const slowMs = (_cfg && _cfg.network_slow_ms) || 2500;
  window.fetch = function (input, init) {
    let url = '';
    try { url = (typeof input === 'string') ? input : (input && input.url) || ''; } catch (_) {}
    // Never record the recorder's own intake/config traffic (feedback loop).
    const ours = url.indexOf('/api/v1/recordings') !== -1;
    const t0 = (performance && performance.now) ? performance.now() : Date.now();
    const p = _origFetch.apply(this, arguments);
    if (ours) return p;
    return p.then((resp) => {
      try {
        const dt = ((performance && performance.now) ? performance.now() : Date.now()) - t0;
        const method = (init && init.method) || 'GET';
        if (!resp.ok || dt >= slowMs) {
          _record('network', {
            level: resp.ok ? 'warning' : 'error',
            label: `${method} ${resp.status} ${url}`.slice(0, 400),
            value_num: Math.round(dt),
            detail: { ok: resp.ok, status: resp.status, slow: dt >= slowMs },
          });
        }
      } catch (_) {}
      return resp;
    }).catch((err) => {
      try {
        const dt = ((performance && performance.now) ? performance.now() : Date.now()) - t0;
        const method = (init && init.method) || 'GET';
        _record('network', {
          level: 'error',
          label: `${method} FAILED ${url}`.slice(0, 400),
          value_num: Math.round(dt),
          detail: { error: String(err && err.message || err) },
        });
      } catch (_) {}
      throw err;
    });
  };
}

// ── Capture: lag / long tasks ──────────────────────────────────────────────--

function _installLagCapture() {
  try {
    if (typeof PerformanceObserver === 'undefined') return;
    const minMs = (_cfg && _cfg.longtask_min_ms) || 80;
    _perfObserver = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        try {
          if (entry.duration >= minMs) {
            _record('lag', {
              level: entry.duration >= 250 ? 'warning' : 'info',
              label: `longtask ${Math.round(entry.duration)}ms`,
              value_num: Math.round(entry.duration),
            });
          }
        } catch (_) {}
      }
    });
    _perfObserver.observe({ entryTypes: ['longtask'] });
  } catch (_) {}

  // One-shot navigation timing baseline (DOMContentLoaded / load durations).
  try {
    const nav = performance.getEntriesByType && performance.getEntriesByType('navigation')[0];
    if (nav) {
      _record('nav', {
        label: 'navigation-timing',
        value_num: Math.round(nav.duration || 0),
        detail: {
          dom_content_loaded: Math.round(nav.domContentLoadedEventEnd || 0),
          load_event: Math.round(nav.loadEventEnd || 0),
          type: nav.type,
        },
      });
    }
  } catch (_) {}
}

// ── Capture: HTML snapshots via MutationObserver ───────────────────────────--

function _snapshotRoot() {
  return (_cfg && _cfg.capture_whole_page)
    ? document.body
    : (document.getElementById('chat-messages-inner') || document.body);
}

function _takeSnapshot(reason) {
  if (!_active) return;
  try {
    const root = _snapshotRoot();
    if (!root) return;
    let html = root.outerHTML || '';
    const maxBytes = (_cfg && _cfg.snapshot_max_bytes) || 400000;
    if (html.length > maxBytes) html = html.slice(0, maxBytes) + '<!-- trimmed -->';
    const mut = _mutAccum;
    _mutAccum = null;
    _record('snapshot', {
      label: reason || 'snapshot',
      value_num: html.length,
      html,
      detail: {
        root: root.id || root.tagName,
        scroll_y: window.scrollY,
        viewport: window.innerWidth + 'x' + window.innerHeight,
        theme: document.body.classList.contains('light-mode') ? 'light' : 'dark',
        mutations: mut,   // {added, removed, attrs, chars} since the previous snapshot
      },
    });
    _lastSnapshotAt = Date.now();
  } catch (_) {}
}

// Schedule a throttled snapshot in idle time (never more often than the
// configured min interval, never on the synchronous mutation path).
function _scheduleSnapshot(reason) {
  if (_snapshotScheduled) return;
  const minGap = (_cfg && _cfg.snapshot_min_interval_ms) || 4000;
  const wait = Math.max(0, minGap - (Date.now() - _lastSnapshotAt));
  _snapshotScheduled = true;
  setTimeout(() => {
    _snapshotScheduled = false;
    const run = () => _takeSnapshot(reason);
    if (window.requestIdleCallback) window.requestIdleCallback(run, { timeout: 1000 });
    else run();
  }, wait);
}

function _onMutations(mutations) {
  // Coalesce the burst into a cheap summary (Level-2 deltas would slot in here)
  // and schedule a full snapshot. We deliberately do NOT serialize per-mutation
  // detail in Level 1 — that's the heavy part deferred to replay mode.
  try {
    if (!_mutAccum) _mutAccum = { added: 0, removed: 0, attrs: 0, chars: 0 };
    for (const m of mutations) {
      if (m.type === 'childList') {
        _mutAccum.added += m.addedNodes.length;
        _mutAccum.removed += m.removedNodes.length;
      } else if (m.type === 'attributes') {
        _mutAccum.attrs += 1;
      } else if (m.type === 'characterData') {
        _mutAccum.chars += 1;
      }
    }
    _scheduleSnapshot('mutation');
  } catch (_) {}
}

function _installSnapshotCapture() {
  try {
    _mutObserver = new MutationObserver(_onMutations);
    _mutObserver.observe(document.body, {
      childList: true, subtree: true, attributes: true, characterData: true,
    });
    // Baseline snapshot of the initial render.
    _takeSnapshot('baseline');
  } catch (_) {}
}

// ── Start / stop ──────────────────────────────────────────────────────────--

function _start(cfg) {
  if (_active) return;
  _cfg = cfg || {};
  _active = true;
  _ensureClientId();
  _record('meta', { label: 'session-start', detail: { ua: navigator.userAgent } });

  if (_cfg.capture_js_errors !== false) _installErrorCapture();
  if (_cfg.capture_console !== false) _installConsoleCapture();
  if (_cfg.capture_network !== false) _installNetworkCapture();
  if (_cfg.capture_lag !== false) _installLagCapture();
  if (_cfg.capture_snapshots !== false) _installSnapshotCapture();

  const flushMs = (_cfg.flush_interval_ms) || 5000;
  _flushTimer = setInterval(() => _flush(false), flushMs);

  // Flush hard when the tab is hidden or unloading — that's exactly when the
  // bug-before-close evidence would otherwise be lost.
  const onHide = () => { if (document.visibilityState === 'hidden') _flush(true); };
  const onPageHide = () => _flush(true);
  document.addEventListener('visibilitychange', onHide);
  window.addEventListener('pagehide', onPageHide);
  _winListeners.push(['pagehide', onPageHide]);
  // (visibilitychange is on document; tracked separately for teardown)
  _recorderHideHandler = onHide;
}

let _recorderHideHandler = null;

function _stop() {
  if (!_active) return;
  _active = false;
  try { _flush(false); } catch (_) {}
  try { if (_mutObserver) _mutObserver.disconnect(); } catch (_) {}
  _mutObserver = null;
  try { if (_perfObserver) _perfObserver.disconnect(); } catch (_) {}
  _perfObserver = null;
  if (_origFetch) { try { window.fetch = _origFetch; } catch (_) {} _origFetch = null; }
  ['warn', 'error'].forEach((lvl) => {
    if (_origConsole[lvl]) { try { console[lvl] = _origConsole[lvl]; } catch (_) {} }
  });
  _origConsole = {};
  for (const [type, fn] of _winListeners) {
    try { window.removeEventListener(type, fn); } catch (_) {}
  }
  _winListeners.length = 0;
  if (_recorderHideHandler) {
    try { document.removeEventListener('visibilitychange', _recorderHideHandler); } catch (_) {}
    _recorderHideHandler = null;
  }
  if (_flushTimer) { clearInterval(_flushTimer); _flushTimer = null; }
  _buffer = [];
  _mutAccum = null;
}

// ── Config polling (gates everything) ──────────────────────────────────────--

async function _checkConfig() {
  try {
    const r = await fetch(CONFIG_URL, { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    if (data && data.enabled) {
      if (!_active) _start(data.config || {});
    } else if (_active) {
      _stop();
    }
  } catch (_) { /* server unreachable — stay passive */ }
}

export function initRecorder() {
  // Probe once on load, then poll so an admin toggling the flag takes effect
  // within ~a minute without anyone reloading. Cheap when off (one tiny GET).
  _checkConfig();
  _configTimer = setInterval(_checkConfig, CONFIG_POLL_MS);
}
