'use strict';

// ── Web page: an in-app, AI-augmented browser ──────────────────────────────
// Markup lives in ui/web.html (injected into #tab-web by ui/js/partial-loader.js);
// styles in ui/css/web.css. This module wires the page to the live stream from
// app/api/browser_stream.py: it paints the server-side Playwright page (the SAME
// page the agent's `browser_action` tool drives, keyed by user+session) and
// forwards the user's mouse / keyboard / navigation back to it. Whatever the
// agent does shows here live, and the user can take over any time — one shared
// browser.

import { app } from './state.js';
import { browserWsUrl } from './config.js';
import { getAuthToken } from './left-login.js';

// Must match VIEW_W / VIEW_H in app/api/browser_stream.py.
const VIEW_W = 1280;
const VIEW_H = 720;

let ws = null;
let mounted = false;
let active = false;
let connectedSession = null;
let reconnectTimer = null;
let els = null;

function mount() {
  const root = document.getElementById('tab-web');
  if (!root) return false;
  els = {
    root,
    url: root.querySelector('.web-url'),
    frame: root.querySelector('.web-frame'),
    empty: root.querySelector('.web-empty'),
    stage: root.querySelector('.web-stage'),
    dot: root.querySelector('.web-dot'),
    statusText: root.querySelector('.web-status-text'),
  };
  if (!els.stage || !els.frame) { els = null; return false; }
  wireInput();
  return true;
}

function setStatus(state, text) {
  if (!els) return;
  els.dot.className = 'web-dot' + (state === 'live' ? ' live' : state === 'down' ? ' down' : '');
  els.statusText.textContent = text;
}

function mapXY(e) {
  if (!els) return null;
  const r = els.frame.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return null;
  let x = (e.clientX - r.left) / r.width * VIEW_W;
  let y = (e.clientY - r.top) / r.height * VIEW_H;
  x = Math.max(0, Math.min(VIEW_W, x));
  y = Math.max(0, Math.min(VIEW_H, y));
  return { x, y };
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify(obj)); } catch (_) {}
  }
}

function navigate() {
  if (!els) return;
  const v = els.url.value.trim();
  if (!v) return;
  send({ action: 'navigate', url: v });
  els.stage.focus();
}

function wireInput() {
  if (!els) return;
  const stage = els.stage;
  let down = false, dragStarted = false, startPt = null;

  els.frame.addEventListener('dragstart', (e) => e.preventDefault());

  stage.addEventListener('pointerdown', (e) => {
    const p = mapXY(e); if (!p) return;
    stage.focus();
    down = true; dragStarted = false; startPt = p;
    try { stage.setPointerCapture(e.pointerId); } catch (_) {}
  });
  stage.addEventListener('pointermove', (e) => {
    if (!down) return;
    const p = mapXY(e); if (!p) return;
    if (!dragStarted) {
      if (Math.hypot(p.x - startPt.x, p.y - startPt.y) > 4) {
        dragStarted = true;
        send({ action: 'mousedown', x: startPt.x, y: startPt.y });
      } else {
        return;
      }
    }
    send({ action: 'mousemove', x: p.x, y: p.y });
  });
  const endPointer = (e) => {
    if (!down) return;
    down = false;
    const p = mapXY(e) || startPt;
    if (dragStarted) send({ action: 'mouseup', x: p.x, y: p.y });
    else if (startPt) send({ action: 'click', x: startPt.x, y: startPt.y });
  };
  stage.addEventListener('pointerup', endPointer);
  stage.addEventListener('pointercancel', endPointer);

  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    send({ action: 'scroll', dx: e.deltaX, dy: e.deltaY });
  }, { passive: false });

  const SPECIAL = {
    Enter: 'Enter', Backspace: 'Backspace', Tab: 'Tab', Delete: 'Delete',
    ArrowUp: 'ArrowUp', ArrowDown: 'ArrowDown', ArrowLeft: 'ArrowLeft', ArrowRight: 'ArrowRight',
    Escape: 'Escape', Home: 'Home', End: 'End', PageUp: 'PageUp', PageDown: 'PageDown',
  };
  stage.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return; // leave shortcuts to the browser
    if (e.key && e.key.length === 1) {
      e.preventDefault();
      send({ action: 'type', text: e.key });
    } else if (SPECIAL[e.key]) {
      e.preventDefault();
      send({ action: 'key', key: SPECIAL[e.key] });
    }
  });

  els.root.querySelectorAll('.web-btn').forEach((b) => {
    b.addEventListener('click', () => {
      const act = b.dataset.act;
      if (act === 'go') navigate();
      else if (act === 'back') send({ action: 'back' });
      else if (act === 'forward') send({ action: 'forward' });
      else if (act === 'reload') send({ action: 'reload' });
    });
  });
  els.url.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); navigate(); }
  });
}

function connect() {
  if (!app.currentUserId) {
    setStatus('down', 'sign in to browse');
    return;
  }
  clearTimeout(reconnectTimer);
  connectedSession = app.currentSessionId || '';
  setStatus('idle', 'connecting…');
  const qs = `?token=${encodeURIComponent(getAuthToken() || '')}` +
             `&session_id=${encodeURIComponent(connectedSession)}`;
  try {
    ws = new WebSocket(browserWsUrl() + qs);
  } catch (_) {
    setStatus('down', 'connection failed');
    return;
  }
  ws.onopen = () => setStatus('idle', 'starting browser…');
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (m.type === 'frame') {
      els.frame.src = 'data:image/jpeg;base64,' + m.data;
      if (!els.frame.classList.contains('has-frame')) {
        els.frame.classList.add('has-frame');
        els.empty.style.display = 'none';
      }
      setStatus('live', 'live');
    } else if (m.type === 'ready') {
      if (m.url && m.url !== 'about:blank') els.url.value = m.url;
      setStatus('idle', 'ready');
    } else if (m.type === 'nav') {
      if (m.url && m.url !== 'about:blank') els.url.value = m.url;
    } else if (m.type === 'error') {
      setStatus('down', m.message || 'error');
    }
  };
  ws.onclose = () => {
    setStatus('down', 'disconnected');
    if (active) reconnectTimer = setTimeout(() => { if (active) connect(); }, 1500);
  };
  ws.onerror = () => setStatus('down', 'error');
}

function disconnect() {
  clearTimeout(reconnectTimer);
  if (ws) {
    try { ws.onclose = null; ws.onerror = null; ws.close(); } catch (_) {}
    ws = null;
  }
}

export function startWeb() {
  active = true;
  if (!mounted) {
    if (!mount()) { active = false; return; }
    mounted = true;
  }
  // (Re)connect if there's no live socket or the active chat session changed,
  // so the Web page always shares the CURRENT session's browser with the agent.
  if (!ws || ws.readyState > WebSocket.OPEN || connectedSession !== (app.currentSessionId || '')) {
    disconnect();
    connect();
  }
}

export function stopWeb() {
  active = false;
  disconnect();
}
