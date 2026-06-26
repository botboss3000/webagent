'use strict';

/**
 * debugConsole.js — Floating debug console log panel
 *
 * Intercepts console.log / console.warn / console.error / console.debug and
 * shows the output in a fixed-bottom panel. The panel is hidden by default
 * and toggled via the bug icon button in the header (#debug-console-toggle).
 *
 * Captures all calls (even ones made before this module initialises, if
 * the earlyCapture buffer is used). Filter buttons let you show/hide log
 * levels. Entries can be preserved across page refreshes via sessionStorage.
 */

// ── Early capture buffer ─────────────────────────────────────────────────
// Stores console calls made before the panel DOM is ready, so nothing is lost.
const _earlyBuffer = [];
const _origConsole = {};
let _initialized = false;

// ── Capture console methods ──────────────────────────────────────────────
function _intercept(logLevel, origFn) {
  return function (...args) {
    // Still forward to the real console
    origFn.apply(console, args);

    // Capture the output
    try {
      const text = args.map(a => {
        if (typeof a === 'string') return a;
        if (a === null) return 'null';
        if (a === undefined) return 'undefined';
        try { return JSON.stringify(a, null, 2); } catch (_) { return String(a); }
      }).join(' ');

      // Build a stack trace snippet (omit this wrapper frame)
      let stack = '';
      try { throw new Error(); } catch (e) {
        if (e.stack) {
          const lines = e.stack.split('\n');
          // Skip frames: Error, _intercept, the console wrapper itself
          let skip = 3;
          for (const line of lines) {
            if (skip > 0) { skip--; continue; }
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith('debugConsole')) continue;
            stack = trimmed;
            break;
          }
        }
      }

      // Forward genuine errors to the server diagnostic log (logs.db) via the
      // shared funnel (window.__reportJsError, installed pre-module in index.html),
      // so app/library console.error calls — e.g. canvas's "[canvas] mount
      // error:" — are recorded, not just shown in this panel. window.onerror /
      // unhandledrejection already cover UNCAUGHT errors; this adds the ones code
      // swallows with try/catch + console.error. Skip our own banner marker so an
      // error isn't logged twice, and let the funnel dedupe the rest by message.
      // (Browser-internal render warnings — bad SVG path d=, CSS parse — are NOT
      // routed through console.error and remain uncapturable from JS.)
      if (logLevel === 'error' && text && text.indexOf('[webAgent JS error]') !== 0) {
        try {
          if (typeof window.__reportJsError === 'function') {
            window.__reportJsError({ message: text, source: 'console.error', stack: stack });
          }
        } catch (_) {}
      }

      const entry = { level: logLevel, text, stack, ts: Date.now() };

      if (_initialized) {
        _addEntry(entry);
      } else {
        _earlyBuffer.push(entry);
        if (_earlyBuffer.length > 500) _earlyBuffer.shift();
      }
    } catch (_) {}
  };
}

// ── Panel state ──────────────────────────────────────────────────────────
let _filterState = { log: true, warn: true, error: true, debug: false };
let _entries = [];

// ── DOM refs (set during init) ──
let _panel, _content, _toggle, _close;
let _clearBtn, _countEl, _preserveCheck;
let _filterBtns = {};

// ── Add a single entry to the list ──
function _addEntry(entry) {
  _entries.push(entry);

  // Apply current filter
  if (!_filterState[entry.level]) return;

  const el = document.createElement('div');
  el.className = 'dc-entry ' + entry.level;

  const tag = document.createElement('span');
  tag.className = 'dc-tag ' + entry.level;
  tag.textContent = entry.level;
  el.appendChild(tag);

  const msg = document.createElement('span');
  msg.className = 'dc-msg';
  msg.textContent = entry.text;
  el.appendChild(msg);

  if (entry.stack) {
    const stackEl = document.createElement('span');
    stackEl.className = 'dc-stack';
    stackEl.textContent = entry.stack;
    // Click to expand/collapse stack trace
    stackEl.addEventListener('click', () => stackEl.classList.toggle('expanded'));
    el.appendChild(stackEl);
  }

  // Remove empty hint
  const empty = _content.querySelector('.debug-console-empty');
  if (empty) empty.remove();

  _content.appendChild(el);
  _content.scrollTop = _content.scrollHeight;
  _updateCount();
}

// ── Re-filter all entries ──
function _reFilter() {
  // Remove all entry elements
  const entries = _content.querySelectorAll('.dc-entry');
  entries.forEach(e => e.remove());

  // Re-add empty hint if needed
  const hasVisible = _entries.some(e => _filterState[e.level]);
  const empty = _content.querySelector('.debug-console-empty');
  if (!hasVisible) {
    if (!empty) {
      const hint = document.createElement('div');
      hint.className = 'debug-console-empty';
      hint.textContent = 'No entries match the current filter.';
      _content.appendChild(hint);
    }
  } else {
    if (empty) empty.remove();
  }

  // Re-add matching entries
  let visibleCount = 0;
  for (const entry of _entries) {
    if (!_filterState[entry.level]) continue;
    visibleCount++;

    const el = document.createElement('div');
    el.className = 'dc-entry ' + entry.level;

    const tag = document.createElement('span');
    tag.className = 'dc-tag ' + entry.level;
    tag.textContent = entry.level;
    el.appendChild(tag);

    const msg = document.createElement('span');
    msg.className = 'dc-msg';
    msg.textContent = entry.text;
    el.appendChild(msg);

    if (entry.stack) {
      const stackEl = document.createElement('span');
      stackEl.className = 'dc-stack';
      stackEl.textContent = entry.stack;
      stackEl.addEventListener('click', () => stackEl.classList.toggle('expanded'));
      el.appendChild(stackEl);
    }

    _content.appendChild(el);
  }

  if (visibleCount > 0) {
    _content.scrollTop = _content.scrollHeight;
  }
  _updateCount();
}

function _updateCount() {
  if (_countEl) {
    const visible = _entries.filter(e => _filterState[e.level]).length;
    _countEl.textContent = visible + ' / ' + _entries.length + ' entries';
  }
}

function _clearLog() {
  _entries = [];
  const entryEls = _content.querySelectorAll('.dc-entry');
  entryEls.forEach(e => e.remove());
  const empty = _content.querySelector('.debug-console-empty');
  if (!empty) {
    const hint = document.createElement('div');
    hint.className = 'debug-console-empty';
    hint.textContent = 'Console log output will appear here.';
    _content.appendChild(hint);
  }
  _updateCount();

  // Also clear preserved storage
  try {
    sessionStorage.removeItem('debugConsoleEntries');
  } catch (_) {}
}

function _saveToSession() {
  try {
    // Only save if preserve is checked and there are entries
    if (_preserveCheck && _preserveCheck.checked && _entries.length > 0) {
      // Trim to last 200 entries to keep storage sane
      const toSave = _entries.slice(-200);
      sessionStorage.setItem('debugConsoleEntries', JSON.stringify(toSave));
    }
  } catch (_) {}
}

function _loadFromSession() {
  try {
    const saved = sessionStorage.getItem('debugConsoleEntries');
    if (saved) {
      const parsed = JSON.parse(saved);
      if (Array.isArray(parsed)) {
        _entries = parsed;
        // Re-render (respecting current filters)
        _reFilter();
      }
    }
  } catch (_) {}
}

// ── Init ─────────────────────────────────────────────────────────────────
export function initDebugConsole() {
  if (_initialized) return;

  _panel = document.getElementById('debug-console-panel');
  _content = document.getElementById('debug-console-content');
  _toggle = document.getElementById('debug-console-toggle');
  _close = document.getElementById('debug-console-close');
  _clearBtn = document.getElementById('debug-console-clear');
  _countEl = document.getElementById('debug-console-count');
  _preserveCheck = document.getElementById('debug-console-preserve');

  if (!_panel || !_content || !_toggle) return;

  // Restore visibility from session
  try {
    const wasOpen = sessionStorage.getItem('debugConsoleOpen') === 'true';
    if (wasOpen) _panel.classList.add('open');
  } catch (_) {}

  // Toggle button
  _toggle.addEventListener('click', () => {
    _panel.classList.toggle('open');
    try {
      sessionStorage.setItem('debugConsoleOpen', String(_panel.classList.contains('open')));
    } catch (_) {}
    if (_panel.classList.contains('open')) {
      _toggle.classList.add('active');
      _content.scrollTop = _content.scrollHeight;
    } else {
      _toggle.classList.remove('active');
    }
  });

  // If it started open, mark the toggle active
  if (_panel.classList.contains('open')) {
    _toggle.classList.add('active');
  }

  // Close button (minimise)
  if (_close) {
    _close.addEventListener('click', () => {
      _panel.classList.remove('open');
      _toggle.classList.remove('active');
      try { sessionStorage.setItem('debugConsoleOpen', 'false'); } catch (_) {}
    });
  }

  // Clear button
  if (_clearBtn) {
    _clearBtn.addEventListener('click', _clearLog);
  }

  // Preserve checkbox → save on change
  if (_preserveCheck) {
    _preserveCheck.addEventListener('change', () => {
      if (!_preserveCheck.checked) {
        try { sessionStorage.removeItem('debugConsoleEntries'); } catch (_) {}
      } else {
        _saveToSession();
      }
    });
  }

  // Filter buttons
  ['log', 'warn', 'error', 'debug'].forEach(level => {
    const btn = document.getElementById('debug-console-filter-' + level);
    if (!btn) return;
    _filterBtns[level] = btn;
    btn.addEventListener('click', () => {
      const isActive = btn.classList.toggle('active');
      _filterState[level] = isActive;
      _reFilter();
    });
  });

  // Save entries to sessionStorage before unload
  window.addEventListener('pagehide', _saveToSession);
  window.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') _saveToSession();
  });

  // ── Intercept console methods ──
  ['log', 'warn', 'error', 'debug'].forEach(level => {
    _origConsole[level] = console[level];
    console[level] = _intercept(level, _origConsole[level]);
  });

  _initialized = true;

  // Flush early buffer
  for (const entry of _earlyBuffer) {
    _addEntry(entry);
  }
  _earlyBuffer.length = 0;

  // Load any preserved entries from session
  _loadFromSession();
}