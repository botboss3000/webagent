'use strict';

// Per-instance terminal factory. Each call to `createTerminalInstance` spawns
// an independent xterm + WebSocket so multiple terminal tabs can coexist in
// the file viewer without sharing a single PTY.

import { termWsUrl, apiPath } from './config.js';

const MAX_RECONNECT_DELAY = 30000; // 30s max
const INITIAL_RECONNECT_DELAY = 500; // 500ms first retry

// Liveness: server pings every 25s. If we haven't seen ANY frame from the
// server in this many ms we assume the socket is half-open (mobile sleep,
// NAT rebind, hung proxy) and force-close so onclose fires and reconnect
// kicks in. Without this the WS stays readyState===OPEN and keystrokes
// vanish into the void.
const LIVENESS_TIMEOUT_MS = 60000;
const LIVENESS_CHECK_MS = 10000;

const DEFAULT_FONT_SIZE = 14;
const MIN_FONT_SIZE = 8;
const MAX_FONT_SIZE = 32;

export function createTerminalInstance(container, sessionId, opts) {
  if (!sessionId) {
    throw new Error('createTerminalInstance: sessionId is required');
  }
  opts = opts || {};
  // Command typed into the shell once, on the first successful WS open.
  // Reconnects (network blip, refresh) do NOT re-type it — the PTY already
  // has it in its history.
  const initialCommand = typeof opts.initialCommand === 'string' ? opts.initialCommand : '';
  let initialCommandSent = false;
  const initialFontSize = (typeof opts.fontSize === 'number' && opts.fontSize >= MIN_FONT_SIZE && opts.fontSize <= MAX_FONT_SIZE)
    ? opts.fontSize : DEFAULT_FONT_SIZE;

  const term = new Terminal({
    cursorBlink: true,
    cursorStyle: 'block',
    fontSize: initialFontSize,
    fontFamily:
      '"Fira Code", Menlo, Monaco, "Courier New", monospace',
    allowTransparency: true,
    theme: {
      background: '#0d0d1a',
      foreground: '#c0caf5',
      cursor: '#c0caf5',
      selectionBackground: '#2a2a4a',
      black: '#1d202f',
      red: '#f7768e',
      green: '#9ece6a',
      yellow: '#e0af68',
      blue: '#7aa2f7',
      magenta: '#bb9af7',
      cyan: '#7dcfff',
      white: '#c0caf5',
      brightBlack: '#565f89',
      brightRed: '#f7768e',
      brightGreen: '#9ece6a',
      brightYellow: '#e0af68',
      brightBlue: '#7aa2f7',
      brightMagenta: '#bb9af7',
      brightCyan: '#7dcfff',
      brightWhite: '#c0caf5',
    },
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.loadAddon(new WebLinksAddon.WebLinksAddon());
  // Search addon loaded lazily — if the CDN script failed, the rest of
  // the terminal still works, just without Ctrl+F highlights.
  let searchAddon = null;
  try {
    if (typeof SearchAddon !== 'undefined' && SearchAddon.SearchAddon) {
      searchAddon = new SearchAddon.SearchAddon();
      term.loadAddon(searchAddon);
    }
  } catch (_) {}
  term.open(container);
  // First fit happens further down, after the wrap/fontSize state is
  // declared (so the no-wrap path can pin cols correctly on restore).

  let ws = null;
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let disposed = false;
  let lastServerFrameTs = 0;
  let livenessTimer = null;

  // Connection state machine used to drive the per-tab status dot in the UI.
  // 'connecting' = WS opening for the first time / scheduled reconnect in flight
  // 'connected'  = WS open, live PTY stream
  // 'reconnecting' = WS dropped after having been open; retry is queued
  // 'error'      = auth failure or other permanent stop (no further retries)
  let state = 'connecting';
  let stateListeners = [];
  function setState(next) {
    if (state === next) return;
    state = next;
    for (const cb of stateListeners) {
      try { cb(state); } catch (_) {}
    }
  }

  term.onData((data) => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
  });
  term.onResize(({ rows, cols }) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', rows, cols }));
    }
  });

  function cancelReconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function _armLiveness() {
    _disarmLiveness();
    lastServerFrameTs = Date.now();
    livenessTimer = setInterval(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (Date.now() - lastServerFrameTs > LIVENESS_TIMEOUT_MS) {
        // Force-close so onclose fires and scheduleReconnect runs. The
        // server may have already gone but the client hasn't been told.
        try { ws.close(); } catch (_) {}
      }
    }, LIVENESS_CHECK_MS);
  }

  function _disarmLiveness() {
    if (livenessTimer) {
      clearInterval(livenessTimer);
      livenessTimer = null;
    }
  }

  function scheduleReconnect() {
    if (disposed) return;
    cancelReconnect();
    const delay = Math.min(
      INITIAL_RECONNECT_DELAY * Math.pow(2, reconnectAttempts),
      MAX_RECONNECT_DELAY,
    );
    reconnectAttempts++;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    if (disposed) return;
    cancelReconnect();
    if (ws) {
      ws.onclose = null;
      ws.onerror = null;
      try { ws.close(); } catch (_) {}
    }
    // The WS endpoint enforces admin auth — pass the JWT as ?token=. We do
    // this every connect (not just the first one) so a token refreshed in
    // a long-lived session is picked up on the next reconnect.
    const tokenParam = (() => {
      try {
        const t = localStorage.getItem('auth_token');
        return t ? '&token=' + encodeURIComponent(t) : '';
      } catch (_) { return ''; }
    })();
    ws = new WebSocket(termWsUrl() + '?session_id=' + encodeURIComponent(sessionId) + tokenParam);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      reconnectAttempts = 0;
      setState('connected');
      _armLiveness();
      try { term.focus(); } catch (_) {}
      try {
        ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
      } catch (_) {}
      if (initialCommand && !initialCommandSent) {
        initialCommandSent = true;
        // Small delay so the shell finishes printing its prompt first.
        setTimeout(() => {
          try {
            if (ws && ws.readyState === WebSocket.OPEN) ws.send(initialCommand + '\n');
          } catch (_) {}
        }, 200);
      }
    };

    ws.onmessage = (ev) => {
      // Any frame is proof of life for the liveness watchdog.
      lastServerFrameTs = Date.now();
      if (ev.data instanceof ArrayBuffer) {
        const data = new Uint8Array(ev.data);
        if (data.length === 0) {
          term.write('\r\n\x1b[31m[Process exited — reconnecting]\x1b[0m\r\n');
        } else {
          term.write(data);
        }
      } else if (typeof ev.data === 'string') {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'ping') {
            // Reply so the server's inbound-silence watchdog sees us alive.
            try {
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'pong' }));
              }
            } catch (_) {}
            return;
          }
        } catch {
          term.write(ev.data);
        }
      }
    };

    ws.onclose = (ev) => {
      _disarmLiveness();
      if (disposed) return;
      // 4401 = auth failed, 4002 = per-user session cap exceeded. Both are
      // hard stops; retrying just spams the server with rejected handshakes.
      if (ev && (ev.code === 4401 || ev.code === 4002)) {
        const msg = (ev.reason && ev.reason.trim()) || (
          ev.code === 4401
            ? 'Authentication failed — refresh and sign in again'
            : 'Session cap exceeded — close another terminal first'
        );
        term.write('\r\n\x1b[31m[' + msg + ']\x1b[0m\r\n');
        disposed = true;
        setState('error');
        return;
      }
      setState('reconnecting');
      scheduleReconnect();
    };
    ws.onerror = () => { /* onclose fires after onerror; reconnect is scheduled there */ };
  }

  let wrap = opts.wrap !== false;   // default true
  let fontSize = initialFontSize;

  function _sendResize() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
    }
  }

  function fit() {
    // Always size to the host element. In WRAP mode the host width equals
    // the scroll wrapper, so cols match the visible area and shell output
    // wraps naturally. In NO-WRAP mode the host is sized wider than its
    // parent via CSS (.files-terminal-host-nowrap), so fitAddon computes
    // a larger cols value and the scroll wrapper exposes the overflow as
    // a horizontal scrollbar.
    try {
      fitAddon.fit();
      _sendResize();
    } catch (_) {}
  }

  function setWrap(on) {
    // Wrap mode is now driven entirely by CSS classes on the host + its
    // scroll wrapper (set by files.js). This setter just tracks the
    // current state so getWrap() reports correctly and a subsequent fit()
    // measures the updated host width.
    wrap = !!on;
    fit();
  }
  function getWrap() { return wrap; }

  function setFontSize(n) {
    n = Math.round(n);
    if (!Number.isFinite(n)) return;
    if (n < MIN_FONT_SIZE) n = MIN_FONT_SIZE;
    if (n > MAX_FONT_SIZE) n = MAX_FONT_SIZE;
    if (n === fontSize) return;
    fontSize = n;
    try { term.options.fontSize = n; } catch (_) {}
    fit();
  }
  function getFontSize() { return fontSize; }
  function zoomIn()   { setFontSize(fontSize + 1); }
  function zoomOut()  { setFontSize(fontSize - 1); }
  function resetZoom() { setFontSize(DEFAULT_FONT_SIZE); }

  function focus() {
    try { term.focus(); } catch (_) {}
  }

  function reconnect() {
    if (disposed) return;
    reconnectAttempts = 0;
    connect();
  }

  // Mobile/laptop sleep, switching tabs, or losing wifi all leave the WS in
  // states where the browser won't tell us the connection died until much
  // later (or ever). When the tab becomes visible again or the browser says
  // we're back online, kick a reconnect immediately if the socket isn't OPEN
  // — much faster than waiting for the exponential backoff to tick around.
  const _onVisibility = () => {
    if (disposed) return;
    if (document.visibilityState !== 'visible') return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      reconnectAttempts = 0;
      cancelReconnect();
      connect();
    }
  };
  const _onOnline = () => {
    if (disposed) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      reconnectAttempts = 0;
      cancelReconnect();
      connect();
    }
  };
  document.addEventListener('visibilitychange', _onVisibility);
  window.addEventListener('online', _onOnline);

  function dispose() {
    if (disposed) return;
    disposed = true;
    cancelReconnect();
    _disarmLiveness();
    document.removeEventListener('visibilitychange', _onVisibility);
    window.removeEventListener('online', _onOnline);
    if (ws) {
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      try { ws.close(); } catch (_) {}
      ws = null;
    }
    try { term.dispose(); } catch (_) {}
  }

  // Ask the backend to kill the PTY for this session id. Returns a promise
  // that resolves on a clean 2xx (the shell is gone) and rejects on any
  // network / server error so the caller can keep the tab open and let the
  // user retry. A 10s timeout guards against a hung server pinning the UI.
  async function closeBackendSession() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const token = localStorage.getItem('auth_token');
      const headers = token ? { Authorization: 'Bearer ' + token } : {};
      const res = await fetch(
        apiPath('/api/v1/terminal/sessions/' + encodeURIComponent(sessionId)),
        { method: 'DELETE', headers, signal: controller.signal },
      );
      if (!res.ok) {
        let detail = res.statusText || ('HTTP ' + res.status);
        try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (_) {}
        throw new Error(detail);
      }
      // Body is { closed: true|false }. False means the backend didn't have
      // a session under that id — also fine, the UI can drop the tab.
    } finally {
      clearTimeout(timer);
    }
  }

  function onStateChange(cb) {
    if (typeof cb !== 'function') return () => {};
    stateListeners.push(cb);
    // Fire once immediately so subscribers don't need a separate read.
    try { cb(state); } catch (_) {}
    return () => {
      stateListeners = stateListeners.filter((x) => x !== cb);
    };
  }
  function getState() { return state; }

  // Search — no-ops when the CDN addon failed to load. The boolean return
  // tells the find bar whether to flag "not found" to the user.
  function findNext(query, opts) {
    if (!searchAddon || !query) return false;
    try { return !!searchAddon.findNext(query, opts || {}); } catch (_) { return false; }
  }
  function findPrevious(query, opts) {
    if (!searchAddon || !query) return false;
    try { return !!searchAddon.findPrevious(query, opts || {}); } catch (_) { return false; }
  }
  function clearSearch() {
    if (!searchAddon) return;
    try { searchAddon.clearDecorations(); } catch (_) {}
  }

  // Paste arbitrary text into the PTY input stream. Used by the
  // drag-file-onto-terminal flow in files.js — writes a properly-quoted
  // path at the current shell prompt.
  function paste(text) {
    if (!text || disposed) return;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(text);
  }

  // CSS classes for wrap mode are applied by the caller (files.js) on the
  // host and its scroll wrapper before term.open() runs, so the initial
  // fit() below already measures the right host width.
  fit();

  connect();

  return {
    term, fitAddon, fit, focus, reconnect, dispose, closeBackendSession,
    onStateChange, getState,
    findNext, findPrevious, clearSearch,
    paste,
    setWrap, getWrap,
    setFontSize, getFontSize, zoomIn, zoomOut, resetZoom,
  };
}
