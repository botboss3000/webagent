'use strict';

// ── Web page: an in-app, AI-augmented browser ──────────────────────────────
// Markup lives in ui/web.html (injected into #tab-web by ui/js/partial-loader.js);
// styles in ui/css/web.css. This module wires the page to the live stream from
// app/api/browser_stream.py: it paints the server-side Playwright page (the SAME
// page the agent's `browser_action` tool drives) and forwards the user's mouse /
// keyboard / navigation back to it.
//
// The page is keyed to a BROWSER SESSION (bs_id) — a first-class, persistent tab
// that lives beside chat (not tied to the current chat session). It survives
// chat switches and server restarts (logins persist). The per-tab "Share with
// agent" toggle flips the row's `shared` flag: when on, the linked agent sees
// and can act on this same page; when off, it's private to the user.

import { app } from './state.js';
import { browserWsUrl, apiPath } from './config.js';
import { getAuthToken } from './left-login.js';

// Must match VIEW_W / VIEW_H in app/api/browser_stream.py.
const VIEW_W = 1280;
const VIEW_H = 720;

let ws = null;
let mounted = false;
let active = false;
let connectedBs = null;     // bs_id the live socket is streaming
let currentBs = null;       // the full browser-session row {id, shared, agent_id, …}
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
    share: root.querySelector('.web-share'),
    sessionsToggle: root.querySelector('.web-sessions-toggle'),
    newSessionBtn: root.querySelector('.web-new-session'),
    sessionsPopover: root.querySelector('.web-sessions-popover'),
    sessionsList: root.querySelector('.web-sessions-list'),
    promptRow: root.querySelector('.web-prompt-row'),
    promptInput: root.querySelector('.web-prompt-input'),
    send: root.querySelector('.web-send'),
    voice: root.querySelector('.web-voice'),
    attach: root.querySelector('.web-attach'),
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

// Bottom chat pill submit → chat with the agent about the page you're browsing.
// We push the text into the app's existing chat composer and trigger its send,
// so the whole pipeline (auth gate, outbox, message bubble, agent WebSocket) is
// reused — the agent's reply appears in the chat panel, and because the tab is
// shared+linked it can act on this same page live.
function submitPrompt() {
  if (!els || !els.promptInput) return;
  const text = els.promptInput.value.trim();
  if (!text) return;
  if (app.chatInput && app.chatSend) {
    app.chatInput.value = text;
    try { app.chatInput.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) {}
    app.chatSend.click();
  }
  els.promptInput.value = '';
  els.promptInput.style.height = 'auto';
  if (els.promptRow) els.promptRow.classList.remove('has-text');
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
    b.addEventListener('click', (e) => {
      const act = b.dataset.act;
      if (act === 'go') navigate();
      else if (act === 'back') send({ action: 'back' });
      else if (act === 'forward') send({ action: 'forward' });
      else if (act === 'reload') send({ action: 'reload' });
      else if (act === 'sessions') toggleSessionsPopover(e);
      else if (act === 'new-session') openNewSession();
      else if (act === 'share') toggleShare();
    });
  });
  els.url.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); navigate(); }
  });

  // ── Chat pill (shared design): autosize + mic↔send toggle + Enter-to-send ──
  if (els.promptInput) {
    const autosize = () => {
      els.promptInput.style.height = 'auto';
      els.promptInput.style.height = els.promptInput.scrollHeight + 'px';
      const has = els.promptInput.value.trim().length > 0;
      if (els.promptRow) els.promptRow.classList.toggle('has-text', has);
    };
    els.promptInput.addEventListener('input', autosize);
    els.promptInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitPrompt(); }
    });
  }
  if (els.send) els.send.addEventListener('click', submitPrompt);
}

// ── Browser-session REST (manage tabs + the share flag) ─────────────────────
async function apiJson(path, opts = {}) {
  const token = getAuthToken();
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: 'Bearer ' + token } : {}),
    ...(opts.headers || {}),
  };
  const r = await fetch(apiPath(path), { ...opts, headers });
  if (!r.ok) throw new Error('http ' + r.status);
  return r.status === 204 ? null : r.json();
}

// Resolve the tab to stream. With an active agent, ask the backend for the SAME
// tab the agent's `browser_action` resolves to (its shared+linked tab, auto-
// created if none) — so the Web panel shows exactly what the agent is driving,
// with no manual "Share with agent" step. Without an agent, fall back to the
// user's first tab (a private one is created if they have none). Returns the row
// {id, shared, agent_id, …}.
async function ensureBrowserSession() {
  const agentId = app.currentAgentId;
  if (agentId) {
    try {
      const row = await apiJson('/api/v1/browser/sessions/resolve', {
        method: 'POST',
        body: JSON.stringify({ agent_id: agentId }),
      });
      if (row && row.id) return row;
    } catch (_) { /* fall through to the no-agent path */ }
  }
  let list = [];
  try {
    const d = await apiJson('/api/v1/browser/sessions');
    list = (d && d.sessions) || [];
  } catch (_) {}
  if (list.length) return list[0];
  return apiJson('/api/v1/browser/sessions', {
    method: 'POST',
    body: JSON.stringify({ title: 'My browser' }),
  });
}

function updateShareUi() {
  if (!els || !els.share) return;
  const shared = !!(currentBs && currentBs.shared);
  els.share.classList.toggle('on', shared);
  els.share.setAttribute('aria-pressed', shared ? 'true' : 'false');
  els.share.textContent = shared ? 'Shared with agent' : 'Private';
  els.share.title = shared
    ? 'This tab is visible to the linked agent — click to make it private'
    : 'This tab is private — click to share it with the agent';
}

// ── Sessions popover: list / create / switch browser tabs ───────────────────
async function refreshSessionsPopover() {
  if (!els || !els.sessionsList) return;
  let sessions = [];
  try {
    const d = await apiJson('/api/v1/browser/sessions');
    sessions = (d && d.sessions) || [];
  } catch (_) {}
  // Sort: "No Agent" first, then by updated_at desc
  const noAgent = sessions.filter(s => !s.agent_id);
  const withAgent = sessions.filter(s => s.agent_id);
  const sortByDate = (a, b) => (b.updated_at || '').localeCompare(a.updated_at || '');
  const sorted = [...noAgent.sort(sortByDate), ...withAgent.sort(sortByDate)];
  const top = sorted.slice(0, 10);

  els.sessionsList.innerHTML = '';
  // "No Agent" pseudo-entry (the user's private session without an agent)
  const noAgentItem = document.createElement('div');
  noAgentItem.className = 'web-session-item' + (!currentBs || !currentBs.agent_id ? ' active' : '');
  noAgentItem.innerHTML = `<span class="web-session-icon">&#128274;</span><span class="web-session-label">No Agent</span><span class="web-session-meta">private</span>`;
  noAgentItem.addEventListener('click', () => switchSession(null));
  els.sessionsList.appendChild(noAgentItem);

  for (const s of top) {
    const isActive = currentBs && currentBs.id === s.id;
    const item = document.createElement('div');
    item.className = 'web-session-item' + (isActive ? ' active' : '');
    const agentLabel = s.agent_id ? '(' + (s.title || s.agent_id.slice(0, 8) + '…') + ')' : '';
    item.innerHTML = `<span class="web-session-icon">${s.shared ? '&#128101;' : '&#128274;'}</span>` +
      `<span class="web-session-label">${s.title || 'Untitled'}</span>` +
      `<span class="web-session-meta">${s.agent_id ? s.agent_id.slice(0, 6) + '…' : 'private'}</span>`;
    item.addEventListener('click', () => switchSession(s.id));
    els.sessionsList.appendChild(item);
  }
}

async function switchSession(bsId) {
  if (els.sessionsPopover) els.sessionsPopover.style.display = 'none';
  if (bsId && currentBs && currentBs.id === bsId) return; // already on it
  // Disconnect current WS
  disconnect();
  if (bsId) {
    // Use the target session directly — look it up from the popover data or
    // fetch it fresh. We bypass ensureBrowserSession and connect the WS to
    // this specific session.
    currentBs = await ensureSessionById(bsId) || { id: bsId };
  } else {
    // "No Agent" — get the user's first private tab, or create one
    try {
      const d = await apiJson('/api/v1/browser/sessions');
      const list = (d && d.sessions) || [];
      const first = list.find(s => !s.agent_id);
      if (first) currentBs = first;
      else currentBs = null;
    } catch (_) { currentBs = null; }
  }
  connectedBs = null;
  // Connect directly — don't re-resolve via ensureBrowserSession
  doConnect();
}

async function ensureSessionById(bsId) {
  // Pull the session list and find our id
  try {
    const d = await apiJson('/api/v1/browser/sessions');
    const list = (d && d.sessions) || [];
    return list.find(s => s.id === bsId) || null;
  } catch (_) { return null; }
}

async function openNewSession() {
  // Create a fresh private session, then switch to it
  try {
    const row = await apiJson('/api/v1/browser/sessions', {
      method: 'POST',
      body: JSON.stringify({ title: 'New tab' }),
    });
    if (row && row.id) {
      if (els.sessionsPopover) els.sessionsPopover.style.display = 'none';
      disconnect();
      currentBs = row;
      connectedBs = null;
      doConnect();
    }
  } catch (_) {}
}

function toggleSessionsPopover(e) {
  e.stopPropagation();
  if (!els || !els.sessionsPopover) return;
  const showing = els.sessionsPopover.style.display !== 'none';
  if (showing) {
    els.sessionsPopover.style.display = 'none';
  } else {
    refreshSessionsPopover();
    els.sessionsPopover.style.display = 'block';
  }
}

// Close the popover on outside click
document.addEventListener('click', (e) => {
  if (els && els.sessionsPopover && els.sessionsPopover.style.display !== 'none') {
    if (!els.sessionsPopover.contains(e.target) &&
        els.sessionsToggle && !els.sessionsToggle.contains(e.target)) {
      els.sessionsPopover.style.display = 'none';
    }
  }
});

async function toggleShare() {
  if (!currentBs || !currentBs.id) return;
  const next = !currentBs.shared;
  // Sharing also LINKS the tab to the current agent, so the agent's
  // browser_action resolves to THIS tab (the one you're watching) instead of a
  // separate one. Un-sharing unlinks it again.
  const body = { shared: next, agent_id: next ? (app.currentAgentId || null) : null };
  try {
    const updated = await apiJson(
      '/api/v1/browser/sessions/' + encodeURIComponent(currentBs.id),
      { method: 'PATCH', body: JSON.stringify(body) },
    );
    currentBs = { ...currentBs, ...(updated || {}) };
  } catch (_) {
    return; // leave the UI as-is on failure
  }
  updateShareUi();
}

// Connect to a specific browser session already set in currentBs.
function doConnect() {
  if (!currentBs || !currentBs.id) {
    setStatus('down', 'no session');
    return;
  }
  connectedBs = currentBs.id;
  updateShareUi();
  const qs = `?token=${encodeURIComponent(getAuthToken() || '')}` +
             `&bs_id=${encodeURIComponent(currentBs.id)}`;
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

async function connect() {
  if (!app.currentUserId) {
    setStatus('down', 'sign in to browse');
    return;
  }
  clearTimeout(reconnectTimer);
  setStatus('idle', 'connecting…');
  try {
    currentBs = await ensureBrowserSession();
  } catch (_) {
    setStatus('down', 'connection failed');
    return;
  }
  if (!currentBs || !currentBs.id) {
    setStatus('down', 'connection failed');
    return;
  }
  doConnect();
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
  // (Re)connect only if there's no live socket. The browser session is
  // independent of the chat session now, so switching chats must NOT drop or
  // re-key the browser — it persists across chats (and restarts).
  if (!ws || ws.readyState > WebSocket.OPEN) {
    disconnect();
    connect();
  }
}

export function stopWeb() {
  active = false;
  disconnect();
}
