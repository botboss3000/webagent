'use strict';

// Per-instance terminal factory. Each call to `createTerminalInstance` spawns
// an independent xterm + WebSocket so multiple terminal tabs can coexist in
// the file viewer without sharing a single PTY.

import { termWsUrl } from './config.js';

const MAX_RECONNECT_DELAY = 30000; // 30s max
const INITIAL_RECONNECT_DELAY = 500; // 500ms first retry

export function createTerminalInstance(container) {
  const term = new Terminal({
    cursorBlink: true,
    cursorStyle: 'block',
    fontSize: 14,
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
  term.open(container);
  try { fitAddon.fit(); } catch (_) {}

  let ws = null;
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let disposed = false;

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
    ws = new WebSocket(termWsUrl());
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      reconnectAttempts = 0;
      try { term.focus(); } catch (_) {}
      try {
        ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
      } catch (_) {}
    };

    ws.onmessage = (ev) => {
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
          if (msg.type === 'ping') return;
        } catch {
          term.write(ev.data);
        }
      }
    };

    ws.onclose = () => { if (!disposed) scheduleReconnect(); };
    ws.onerror = () => { /* onclose fires after onerror; reconnect is scheduled there */ };
  }

  function fit() {
    try {
      fitAddon.fit();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
      }
    } catch (_) {}
  }

  function focus() {
    try { term.focus(); } catch (_) {}
  }

  function reconnect() {
    if (disposed) return;
    reconnectAttempts = 0;
    connect();
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    cancelReconnect();
    if (ws) {
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      try { ws.close(); } catch (_) {}
      ws = null;
    }
    try { term.dispose(); } catch (_) {}
  }

  connect();

  return { term, fitAddon, fit, focus, reconnect, dispose };
}
