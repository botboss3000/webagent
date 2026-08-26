'use strict';

// ── Client render recorder (browser flight recorder) ──────────────────────────
//
// Records what the BROWSER actually rendered and felt — one chat baseline,
// frame-grouped DOM differences, lag / long-task stalls, JS errors, console
// warnings and failed/slow network calls — and ships them to
// /api/v1/recordings, stamped with
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
// A capture is intentionally short. It takes one HTML baseline of the chat,
// then records only nodes/text/attributes that differ. Mutations are grouped at
// requestAnimationFrame boundaries so related changes share a precise frame
// timestamp and operation order. The server master switch starts one bounded
// capture per enable cycle; it cannot silently run forever when an operator
// forgets it.
//
// Design rule: the recorder must never throw into the app and must never become
// the source of the lag it is measuring. Every hook is try/wrapped; the baseline
// and mutation payloads are size-capped and the capture automatically stops.

import { app } from '../../shared/js/state.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { authHeaders } from '../../shared/js/left-login.js';

const CONFIG_URL = '/api/v1/recordings/config';
const INGEST_URL = '/api/v1/recordings';
const CONFIG_POLL_MS = 5000;    // short captures must notice an operator toggle promptly

let _clientId = null;
let _seq = 0;
let _buffer = [];
let _active = false;            // currently capturing?
let _cfg = null;                // server-provided capture knobs
let _flushTimer = null;
let _configTimer = null;
let _captureId = null;
let _captureStartedAt = 0;
let _captureStopTimer = null;
let _completedEnableCycle = false;

// Teardown handles for everything we install, so we can fully stop on disable.
let _mutObserver = null;
let _perfObserver = null;
let _origFetch = null;
let _origConsole = {};          // { warn, error }
const _winListeners = [];       // [ [type, fn] ]

let _pendingMutationOps = [];
let _mutationFrameHandle = null;
let _mutationFrame = 0;
let _capturedMutationBytes = 0;
let _capturedMutationOps = 0;
let _mutationOverflowed = false;
let _pendingMutationLimitReason = null;
let _nextNodeId = 1;
let _nodeIds = new WeakMap();

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
      recording_id: _captureId,
      elapsed_ms: _captureStartedAt ? Math.max(0, Math.round(performance.now() - _captureStartedAt)) : 0,
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
    fetch(INGEST_URL, {
      method: 'POST',
      headers: Object.assign(
        { 'Content-Type': 'application/json' },
        authHeaders(),
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
        if (!text.startsWith('[WebAgent JS error]')) {
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

// ── Capture: one chat baseline + frame-coalesced DOM differences ───────────--

const DECORATIVE_BACKGROUND_SELECTOR = [
  '#stargaze-bg',
  '.stargaze-genui',
  '.stargaze-overlay',
  '#cursor-glow',
  '[data-wa-bg-surface]',
].join(',');
const RECORDER_INTERNAL_SELECTOR = '#render-recorder-control';

function _excludeDecorativeBackgrounds() {
  return !(_cfg && _cfg.capture_decorative_animations === true);
}

function _isDecorativeNode(node) {
  if (!_excludeDecorativeBackgrounds() || !node) return false;
  try {
    const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return !!(el && (el.matches(DECORATIVE_BACKGROUND_SELECTOR)
      || el.closest(DECORATIVE_BACKGROUND_SELECTOR)));
  } catch (_) {
    return false;
  }
}

function _isExcludedCaptureNode(node) {
  if (!node) return false;
  try {
    const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    if (el && (el.matches(RECORDER_INTERNAL_SELECTOR) || el.closest(RECORDER_INTERNAL_SELECTOR))) {
      return true;
    }
  } catch (_) {}
  return _isDecorativeNode(node);
}

function _stripDecorativeClone(clone) {
  if (!clone) return clone;
  try {
    if (clone.matches && clone.matches(RECORDER_INTERNAL_SELECTOR)) return null;
    if (clone.querySelectorAll) {
      clone.querySelectorAll(RECORDER_INTERNAL_SELECTOR).forEach((node) => node.remove());
    }
    if (!_excludeDecorativeBackgrounds()) return clone;
    if (clone.matches && clone.matches(DECORATIVE_BACKGROUND_SELECTOR)) return null;
    if (clone.querySelectorAll) {
      clone.querySelectorAll(DECORATIVE_BACKGROUND_SELECTOR).forEach((node) => node.remove());
    }
  } catch (_) {}
  return clone;
}

function _snapshotRoot() {
  if (_cfg && _cfg.capture_whole_page) return document.body;
  return document.getElementById('chat-messages-inner')
    || document.getElementById('chat-messages');
}

function _takeSnapshot(reason) {
  if (!_active) return;
  try {
    const root = _snapshotRoot();
    if (!root) return;
    const clone = _stripDecorativeClone(root.cloneNode(true));
    if (!clone) return;
    _maskSensitiveClone(clone);
    let html = clone.outerHTML || '';
    const maxBytes = (_cfg && _cfg.snapshot_max_bytes) || 400000;
    if (html.length > maxBytes) html = html.slice(0, maxBytes) + '<!-- trimmed -->';
    _capturedMutationBytes += html.length;
    _record('snapshot', {
      label: reason || 'snapshot',
      value_num: html.length,
      html,
      detail: {
        root: root.id || root.tagName,
        scroll_y: window.scrollY,
        viewport: window.innerWidth + 'x' + window.innerHeight,
        theme: document.body.classList.contains('light-mode') ? 'light' : 'dark',
      },
    });
  } catch (_) {}
}

function _clip(value, max) {
  const text = value == null ? '' : String(value);
  return text.length <= max ? text : text.slice(0, max) + `… [+${text.length - max} chars]`;
}

function _nodeId(node) {
  if (!node) return null;
  let id = _nodeIds.get(node);
  if (!id) {
    id = _nextNodeId++;
    _nodeIds.set(node, id);
  }
  return id;
}

// A compact, agent-readable identity. Recorder ids remain stable when a live
// node is removed and reinserted, which makes accidental moves obvious.
function _nodeRef(node) {
  if (!node) return null;
  if (node.nodeType === Node.TEXT_NODE) {
    return {
      recorder_id: _nodeId(node),
      type: 'text',
      parent_recorder_id: _nodeId(node.parentNode),
      text: _clip(node.textContent, 160),
    };
  }
  if (node.nodeType !== Node.ELEMENT_NODE) {
    return { recorder_id: _nodeId(node), type: `node-${node.nodeType}` };
  }
  return {
    recorder_id: _nodeId(node),
    tag: node.tagName.toLowerCase(),
    id: node.id || null,
    class: _clip(node.className || '', 180) || null,
    turn_id: node.dataset ? (node.dataset.turnId || null) : null,
    session_seq: node.dataset ? (node.dataset.sessionSeq || null) : null,
    component: node.dataset ? (node.dataset.component || null) : null,
    load_state: node.dataset ? (node.dataset.loadState || null) : null,
  };
}

function _maskSensitiveClone(root) {
  try {
    const nodes = [];
    if (root.matches && root.matches('input,textarea,[contenteditable="true"]')) nodes.push(root);
    if (root.querySelectorAll) nodes.push(...root.querySelectorAll('input,textarea,[contenteditable="true"]'));
    for (const el of nodes) {
      const type = String(el.getAttribute && el.getAttribute('type') || '').toLowerCase();
      const sensitive = type === 'password' || (el.matches && el.matches('[data-recorder-mask], [autocomplete="current-password"], [autocomplete="new-password"]'));
      if (sensitive) {
        if (el.hasAttribute && el.hasAttribute('value')) el.setAttribute('value', '[masked]');
        if (el.tagName === 'TEXTAREA' || el.getAttribute('contenteditable') === 'true') el.textContent = '[masked]';
      }
    }
  } catch (_) {}
  return root;
}

function _nodeHtml(node) {
  try {
    const max = Math.max(500, Number((_cfg && _cfg.mutation_node_max_bytes) || 12000));
    if (node.nodeType === Node.TEXT_NODE) return _clip(node.textContent, max);
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const clone = _stripDecorativeClone(node.cloneNode(true));
    if (!clone) return '';
    _maskSensitiveClone(clone);
    return _clip(clone.outerHTML || '', max);
  } catch (_) {
    return '';
  }
}

function _childIndex(parent, node) {
  try { return Array.prototype.indexOf.call(parent.childNodes, node); } catch (_) { return -1; }
}

function _componentLedger() {
  try {
    const root = _snapshotRoot();
    if (!root) return [];
    return Array.from(root.children)
      .filter((node) => !_isExcludedCaptureNode(node))
      .slice(0, 100)
      .map((node, index) => ({
      index,
      ..._nodeRef(node),
      text: _clip(node.textContent, 80),
      }));
  } catch (_) {
    return [];
  }
}

function _mutationOp(m) {
  if (_isExcludedCaptureNode(m.target)) return null;
  if (m.type === 'attributes') {
    return {
      op: 'attribute',
      target: _nodeRef(m.target),
      name: m.attributeName,
      old_value: _clip(m.oldValue, 500),
      value: _clip(m.target.getAttribute(m.attributeName), 500),
    };
  }
  if (m.type === 'characterData') {
    return {
      op: 'text',
      target: _nodeRef(m.target),
      old_value: _clip(m.oldValue, 1000),
      value: _clip(m.target.textContent, 1000),
    };
  }
  if (m.type === 'childList') {
    const addedNodes = Array.from(m.addedNodes).filter((node) => !_isExcludedCaptureNode(node));
    const removedNodes = Array.from(m.removedNodes).filter((node) => !_isExcludedCaptureNode(node));
    if (!addedNodes.length && !removedNodes.length) return null;
    return {
      op: 'children',
      parent: _nodeRef(m.target),
      previous: _isExcludedCaptureNode(m.previousSibling) ? null : _nodeRef(m.previousSibling),
      next: _isExcludedCaptureNode(m.nextSibling) ? null : _nodeRef(m.nextSibling),
      added: addedNodes.map((node) => ({
        index: _childIndex(m.target, node),
        node: _nodeRef(node),
        html: _nodeHtml(node),
      })),
      removed: removedNodes.map((node) => ({
        node: _nodeRef(node),
        html: _nodeHtml(node),
      })),
    };
  }
  return null;
}

function _markMutationOverflow(reason) {
  if (_mutationOverflowed) return;
  _mutationOverflowed = true;
  try { if (_mutObserver) _mutObserver.disconnect(); } catch (_) {}
  _record('meta', {
    level: 'warning',
    label: 'mutation-capture-limit',
    detail: { reason, operations: _capturedMutationOps, bytes: _capturedMutationBytes },
  });
}

function _emitMutationFrame() {
  _mutationFrameHandle = null;
  if (!_active || !_pendingMutationOps.length || _mutationOverflowed) return;
  const operations = _pendingMutationOps;
  _pendingMutationOps = [];
  _mutationFrame += 1;

  const maxBatchBytes = Math.max(4000, Number((_cfg && _cfg.mutation_batch_max_bytes) || 100000));
  const maxCaptureBytes = Math.max(maxBatchBytes, Number((_cfg && _cfg.capture_max_bytes) || 2000000));
  const hasOrderChange = operations.some((op) => op.op === 'children');
  let chunk = [];
  let chunkBytes = 0;
  let chunkIndex = 0;

  const emit = () => {
    if (!chunk.length || _mutationOverflowed) return;
    const payload = JSON.stringify({
      version: 1,
      frame: _mutationFrame,
      chunk: chunkIndex++,
      at_ms: Math.max(0, Math.round(performance.now() - _captureStartedAt)),
      operations: chunk,
      component_order: hasOrderChange && chunkIndex === 1 ? _componentLedger() : undefined,
    });
    if (_capturedMutationBytes + payload.length > maxCaptureBytes) {
      _markMutationOverflow('capture_max_bytes');
      return;
    }
    _capturedMutationBytes += payload.length;
    _capturedMutationOps += chunk.length;
    _record('mutation', {
      label: 'mutation-frame',
      value_num: chunk.length,
      html: payload,
      detail: { frame: _mutationFrame, chunk: chunkIndex - 1, operations: chunk.length, bytes: payload.length },
    });
    chunk = [];
    chunkBytes = 0;
  };

  for (const op of operations) {
    const opBytes = JSON.stringify(op).length;
    if (chunk.length && chunkBytes + opBytes > maxBatchBytes) emit();
    if (_mutationOverflowed) break;
    chunk.push(op);
    chunkBytes += opBytes;
  }
  emit();
  if (_pendingMutationLimitReason) {
    const reason = _pendingMutationLimitReason;
    _pendingMutationLimitReason = null;
    _markMutationOverflow(reason);
  }
}

function _scheduleMutationFrame() {
  if (_mutationFrameHandle != null) return;
  if (window.requestAnimationFrame) _mutationFrameHandle = window.requestAnimationFrame(_emitMutationFrame);
  else _mutationFrameHandle = window.setTimeout(_emitMutationFrame, 16);
}

function _onMutations(mutations) {
  try {
    const maxOps = Math.max(100, Number((_cfg && _cfg.capture_max_operations) || 5000));
    for (const m of mutations) {
      if (_capturedMutationOps + _pendingMutationOps.length >= maxOps) {
        _pendingMutationLimitReason = 'capture_max_operations';
        try { if (_mutObserver) _mutObserver.disconnect(); } catch (_) {}
        break;
      }
      const op = _mutationOp(m);
      if (op) _pendingMutationOps.push(op);
    }
    if (_pendingMutationOps.length) _scheduleMutationFrame();
  } catch (_) {}
}

function _installSnapshotCapture() {
  try {
    const root = _snapshotRoot();
    if (!root) {
      _record('meta', { level: 'warning', label: 'chat-root-not-found' });
      return;
    }
    _mutObserver = new MutationObserver(_onMutations);
    _mutObserver.observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeOldValue: true,
      characterData: true,
      characterDataOldValue: true,
    });
    // The only normal full snapshot. All subsequent rows contain differences.
    _takeSnapshot('baseline');
  } catch (_) {}
}

// ── Start / stop ──────────────────────────────────────────────────────────--

// Browser timers overflow at roughly 24.8 days. Re-arm in chunks so any
// practical configured duration remains accurate instead of stopping early.
function _scheduleDurationStop(durationMs) {
  if (!(durationMs > 0)) return;
  const deadline = Date.now() + durationMs;
  const arm = () => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      _captureStopTimer = null;
      _completedEnableCycle = true;
      _stop('duration-limit');
      return;
    }
    _captureStopTimer = setTimeout(arm, Math.min(remaining, 2147483647));
  };
  arm();
}

function _start(cfg) {
  if (_active) return;
  _cfg = cfg || {};
  _captureId = randomUUID();
  _captureStartedAt = performance.now();
  _pendingMutationOps = [];
  _mutationFrameHandle = null;
  _mutationFrame = 0;
  _capturedMutationBytes = 0;
  _capturedMutationOps = 0;
  _mutationOverflowed = false;
  _pendingMutationLimitReason = null;
  _nextNodeId = 1;
  _nodeIds = new WeakMap();
  _active = true;
  _ensureClientId();
  const configuredDuration = Number(_cfg.capture_duration_ms);
  const durationMs = Number.isFinite(configuredDuration) ? Math.max(0, configuredDuration) : 30000;
  const root = _snapshotRoot();
  _record('meta', {
    label: 'capture-start',
    detail: {
      ua: navigator.userAgent,
      duration_ms: durationMs || null,
      unlimited: durationMs === 0,
      root: root && (root.id || root.tagName),
      decorative_backgrounds: !_excludeDecorativeBackgrounds(),
    },
  });

  if (_cfg.capture_js_errors !== false) _installErrorCapture();
  if (_cfg.capture_console !== false) _installConsoleCapture();
  if (_cfg.capture_network !== false) _installNetworkCapture();
  if (_cfg.capture_lag !== false) _installLagCapture();
  if (_cfg.capture_snapshots !== false) _installSnapshotCapture();

  const flushMs = (_cfg.flush_interval_ms) || 5000;
  _flushTimer = setInterval(() => _flush(false), flushMs);
  _scheduleDurationStop(durationMs);

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

function _stop(reason) {
  if (!_active) return;
  if (_mutationFrameHandle != null) {
    try { window.cancelAnimationFrame(_mutationFrameHandle); } catch (_) {}
    try { clearTimeout(_mutationFrameHandle); } catch (_) {}
    _mutationFrameHandle = null;
  }
  try { _emitMutationFrame(); } catch (_) {}
  _record('meta', {
    label: 'capture-stop',
    detail: {
      reason: reason || 'disabled',
      duration_ms: Math.max(0, Math.round(performance.now() - _captureStartedAt)),
      mutation_frames: _mutationFrame,
      mutation_operations: _capturedMutationOps,
      captured_bytes: _capturedMutationBytes,
      overflowed: _mutationOverflowed,
    },
  });
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
  if (_captureStopTimer) { clearTimeout(_captureStopTimer); _captureStopTimer = null; }
  _buffer = [];
  _pendingMutationOps = [];
  _captureId = null;
  _captureStartedAt = 0;
}

// ── Config polling (gates everything) ──────────────────────────────────────--

async function _checkConfig() {
  try {
    const r = await fetch(CONFIG_URL, { cache: 'no-store' });
    if (!r.ok) return;
    _applyConfigState(await r.json());
  } catch (_) { /* server unreachable — stay passive */ }
}

function _applyConfigState(data) {
  const enabled = !!(data && data.enabled);
  if (enabled) {
    if (!_active && !_completedEnableCycle) _start(data.config || {});
    return;
  }
  if (_active) _stop('disabled');
  _completedEnableCycle = false;
}

export function initRecorder() {
  if (_configTimer) return;
  // The header toggle broadcasts its POST response so capture begins/stops in
  // the same click, without waiting for the defensive config poll below.
  window.addEventListener('render-recorder-changed', (event) => {
    _applyConfigState(event.detail || {});
  });
  // Probe once on load, then poll so an admin toggling the flag takes effect
  // within a few seconds without anyone reloading. Cheap when off (one tiny GET).
  _checkConfig();
  _configTimer = setInterval(_checkConfig, CONFIG_POLL_MS);
}
