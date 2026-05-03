'use strict';

import { app } from './state.js';
import { termWsUrl } from './config.js';

let reconnectTimer = null;
let reconnectAttempts = 0;
const MAX_RECONNECT_DELAY = 30000; // 30s max
const INITIAL_RECONNECT_DELAY = 500; // 500ms first retry

export function setTermStatus(state) {
  app.tDot.className = 'status-dot ' + state;
  app.tStat.textContent =
    state === 'green' ? 'Terminal' : state === 'yellow' ? 'Connecting...' : 'Disconnected';
}

export function cancelTermReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectAttempts = 0;
}

function scheduleTermReconnect() {
  cancelTermReconnect();
  const delay = Math.min(
    INITIAL_RECONNECT_DELAY * Math.pow(2, reconnectAttempts),
    MAX_RECONNECT_DELAY
  );
  reconnectAttempts++;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectTerminal();
  }, delay);
}

export function initTerminal() {
  app.term = new Terminal({
    cursorBlink: true,
    cursorStyle: 'block',
    fontSize: 14,
    fontFamily:
      '"Cascadia Code", "Fira Code", Menlo, Monaco, "Courier New", monospace',
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
  app.fitAddon = new FitAddon.FitAddon();
  app.term.loadAddon(app.fitAddon);
  app.term.loadAddon(new WebLinksAddon.WebLinksAddon());
  app.term.open(app.container);
  app.fitAddon.fit();

  app.term.onData((data) => {
    if (app.termWs && app.termWs.readyState === WebSocket.OPEN) app.termWs.send(data);
  });
  app.term.onResize(({ rows, cols }) => {
    if (app.termWs && app.termWs.readyState === WebSocket.OPEN) {
      app.termWs.send(JSON.stringify({ type: 'resize', rows, cols }));
    }
  });
  window.addEventListener('resize', () => app.fitAddon.fit());
}

export function connectTerminal() {
  setTermStatus('yellow');
  cancelTermReconnect();
  if (app.termWs) {
    app.termWs.onclose = null;
    app.termWs.onerror = null;
    app.termWs.close();
  }
  app.termWs = new WebSocket(termWsUrl());
  app.termWs.binaryType = 'arraybuffer';

  app.termWs.onopen = () => {
    setTermStatus('green');
    reconnectAttempts = 0; // Reset backoff on successful connection
    app.term.focus();
    app.termWs.send(
      JSON.stringify({ type: 'resize', rows: app.term.rows, cols: app.term.cols }),
    );
  };

  app.termWs.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      const data = new Uint8Array(ev.data);
      if (data.length === 0) {
        app.term.write(
          '\r\n\x1b[31m[Process exited — refresh to reconnect]\x1b[0m\r\n',
        );
      } else {
        app.term.write(data);
      }
    } else if (typeof ev.data === 'string') {
      // Handle JSON messages (e.g., server heartbeat pings)
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'ping') return; // Ignore heartbeats
      } catch {
        app.term.write(ev.data);
      }
    }
  };

  app.termWs.onclose = () => {
    setTermStatus('red');
    scheduleTermReconnect();
  };
  app.termWs.onerror = () => {
    setTermStatus('red');
    // onclose fires after onerror, which triggers scheduleTermReconnect
  };
}
