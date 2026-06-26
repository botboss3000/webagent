'use strict';

// Canvas tab (visualizer) — AI page builder CORE: page loading/state, a
// first-class shadow-DOM renderer, the new-page dialog, and render_visual event
// handling. The footer control row (chat pill + page selector + new / refresh) is
// a SEPARATE, swappable module — js/canvas-toolbar.js — so a user can redesign the
// toolbar without touching this core. We hand it a small `ctx` bridge and call
// its syncPages() handle whenever page state changes. Markup:
// ui/main-panel/canvas/canvas.html (the footer is an empty mount the toolbar fills).
//
// FIRST-CLASS CANVASES: a canvas is grafted straight into the app inside an open
// shadow root — NOT a sandboxed iframe — so it has real-page powers (live
// webcam/mic, direct calls to the agent). The old postMessage bridge is replaced
// by an `api` toolbox we hand each canvas's mount function. A canvas runs with
// the VIEWER's OWN app trust (it is per-user — you only ever see your own
// canvases), so who may use Canvas follows the Canvas page's VISIBILITY setting,
// enforced server-side (see _fetchCanvasGate → /ui-config canvas_first_class):
// "all" = everyone incl. anonymous, "auth" = signed-in registered users (the
// DEFAULT — registration required, not admin-only), "off" = admins only. Admins
// and "open" single-user mode always pass. The canvas HTTP endpoints re-check the
// same gate + per-user ownership, so a hidden tab can't be bypassed via the API.
// Credentials still go STRAIGHT to the encrypted vault and never through the agent.

import { app } from '../../../shared/js/state.js';
import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { initCanvasToolbar } from './canvas-toolbar.js';

// ── Module state ───────────────────────────────────────────────────────────────

let canvasActive = false;

// Currently displayed page: { slug, title, agent_context, agent_id, url }
// (agent_id = the agent that created/manages this canvas; "" until an agent has
// rendered into it — the footer then falls back to the default webAgent.)
let currentPage = null;

// All loaded pages for the current user
let pages = [];

// The live mounted canvas, replacing the old sandboxed-iframe + postMessage
// bridge: its shadow host element, the shadow root, an optional cleanup() the
// canvas returned (which MUST stop any camera tracks), and the subscriber lists
// wired through the agent toolbox (`api`) we hand the canvas.
let _liveCanvas = null;   // { host, mountEl, shadow, cleanup, themeCbs, statusCbs, slug, _emitStatus }

// Set just before a canvas's <script>s run so WebagentCanvas.register(fn) can
// hand (root, api) to the canvas. Cleared on teardown.
let _pendingMountCtx = null;

// Local-only safety gate: null = not yet known, true/false once /ui-config
// answers. First-class rendering only runs when true; otherwise the canvas is
// disabled (fail-closed). See _fetchCanvasGate.
let _canvasFirstClass = null;

// The footer toolbar handle (js/canvas-toolbar.js). We call _toolbar.syncPages()
// to re-render the page selector whenever page state changes.
let _toolbar = { syncPages() {} };

// Keep the page selector in sync with current state. Thin wrapper so callers
// don't reach through _toolbar directly.
function _syncToolbar() { _toolbar.syncPages(); }

// ── Init & lifecycle ──────────────────────────────────────────────────────────

// Boots the core: registers the live page/visualizer WebSocket handler, mounts
// the swappable footer toolbar (handing it the ctx bridge below), and wires the
// new-page dialog (the toolbar's + button opens it via ctx.newPage).
export function initCanvas() {
  app._canvasHandler = handleEvent;

  // The bridge the toolbar talks to. Everything that mutates page state stays
  // here in the core; the toolbar only reads, triggers, and renders.
  _toolbar = initCanvasToolbar({
    getPages: () => pages,
    getCurrentPage: () => currentPage,
    selectPage: (slug) => switchToPage(slug),
    newPage: () => showNewPageDialog(),
    refresh: () => _refreshCanvas(),
    renamePage: (slug, title) => _renamePage(slug, title),
    deletePage: (slug) => _deletePage(slug),
    buildTaggedPrompt: (text) => buildTaggedCanvasPrompt(text),
    updateStatus: (text, type) => updateStatus(text, type),
  });

  // New-page dialog confirm/cancel (the dialog itself stays in the core).
  const dialogConfirm = document.getElementById('canvas-dialog-confirm');
  const dialogCancel  = document.getElementById('canvas-dialog-cancel');
  const dialogInput   = document.getElementById('canvas-dialog-input');
  if (dialogConfirm) dialogConfirm.addEventListener('click', () => submitNewPage());
  if (dialogCancel)  dialogCancel.addEventListener('click', () => hideNewPageDialog());
  if (dialogInput)   dialogInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitNewPage();
    if (e.key === 'Escape') hideNewPageDialog();
  });

  // Persist the canvas scroll position as the user scrolls (throttled). The host
  // element is stable across page swaps — only its shadow child is replaced — so
  // this one listener covers every page. See _scheduleSaveView / _saveLiveView.
  const host = document.getElementById('canvas-host');
  if (host) host.addEventListener('scroll', () => _scheduleSaveView(), { passive: true });

  // A refresh / tab close should keep the latest state even if the throttle
  // hasn't fired yet — flush view state AND any buffered canvas logs on the way out
  // (keepalive POST), so a page-hide doesn't lose the last burst of console output.
  window.addEventListener('pagehide', () => {
    _saveLiveView();
    if (_liveCanvas) { try { _flushCanvasLogs(_liveCanvas); } catch (_) {} }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      _saveLiveView();
      if (_liveCanvas) { try { _flushCanvasLogs(_liveCanvas); } catch (_) {} }
    }
  });
}

// ── Refresh (reloads ONLY this canvas page) ──────────────────────────────────

// Re-fetch and remount the current page's canvas — not the whole app. showCanvas
// already cache-busts the request, so this picks up the latest rendered HTML.
function _refreshCanvas() {
  if (currentPage && currentPage.url) {
    updateStatus('Refreshing page...');
    showCanvas(currentPage.url, currentPage.title);
  }
}

// ── Page rename / delete (state owners — the toolbar calls these via ctx) ─────

// PATCH a page's title, update local state, and report success so the toolbar
// can re-render. Re-rendering itself is the toolbar's job (it calls syncPages).
async function _renamePage(slug, newTitle) {
  if (!app.currentUserId) return false;
  try {
    const res = await fetch(
      apiPath('/api/v1/canvases/' + encodeURIComponent(slug) + '?user_id=' + encodeURIComponent(app.currentUserId)),
      {
        method: 'PATCH',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle }),
      },
    );
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === 'ok') {
      const page = pages.find(p => p.slug === slug);
      if (page) page.title = newTitle;
      if (currentPage && currentPage.slug === slug) currentPage.title = newTitle;
      return true;
    }
    updateStatus('Rename failed: ' + (data.detail || 'error'), 'error');
    return false;
  } catch (e) {
    updateStatus('Rename failed: ' + e.message, 'error');
    return false;
  }
}

async function _deletePage(slug) {
  try {
    const res = await fetch(
      apiPath('/api/v1/canvases/' + encodeURIComponent(slug) + '?user_id=' + encodeURIComponent(app.currentUserId)),
      { method: 'DELETE', headers: authHeaders() },
    );
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === 'ok') {
      if (currentPage && currentPage.slug === slug) currentPage = null;
      await loadPages();   // reloads + re-syncs the toolbar
    } else {
      updateStatus('Delete failed: ' + (data.detail || 'error'), 'error');
    }
  } catch (e) {
    updateStatus('Delete failed: ' + e.message, 'error');
  }
}

export function startCanvas() {
  canvasActive = true;
  // Confirm the local-only gate before rendering any agent-authored canvas.
  _fetchCanvasGate().then(() => loadPages());
}

export function stopCanvas() {
  canvasActive = false;
  // Leaving the Canvas tab: release the live canvas (stops its camera tracks).
  _teardownLiveCanvas();
}

export function canvasSessionChanged() {
  if (!canvasActive) return;
  // Reset view but keep the page list; the pages are per-user not per-session
  loadPages();
}

// ── Page loading ──────────────────────────────────────────────────────────────

async function loadPages() {
  const userId = app.currentUserId;
  if (!userId) { showPlaceholder(); return; }

  try {
    const res  = await fetch(apiPath(`/api/v1/canvases?user_id=${encodeURIComponent(userId)}`), { headers: authHeaders() });
    const data = await res.json();
    pages = data.canvases || [];

    // Try to keep the current page selected; otherwise restore the last page the
    // user had open (persisted in localStorage), then fall back to home.
    const savedSlug = _savedLastSlug();
    const target = currentPage
      ? pages.find(p => p.slug === currentPage.slug)
      : (savedSlug && pages.find(p => p.slug === savedSlug))
        || pages.find(p => p.slug === 'home')
        || pages[0];

    if (target) {
      currentPage = target;
      showCanvas(target.url, target.title);
    } else {
      currentPage = null;
      showPlaceholder();
    }
    _syncToolbar();
  } catch (e) {
    showPlaceholder();
    _syncToolbar();
    updateStatus('Failed to load pages: ' + e.message, 'error');
  }
}

function switchToPage(slug) {
  const page = pages.find(p => p.slug === slug);
  if (!page) return;
  currentPage = page;
  showCanvas(page.url, page.title);
  _syncToolbar();
  updateStatus('');
}

// ── Sending prompts ───────────────────────────────────────────────────────────

// Build the canvas-tagged prompt for a piece of user text. Shared by the toolbar
// chat pill (via ctx.buildTaggedPrompt) AND the in-canvas action bridge so both
// tag identically.
// ═══════════════════════════════════════════════════════════════════════
// PROMPT / PRETEXT NOTE:
// The message sent to the agent is built from
// app/defaults/app-prompts.json → ui_handoffs.canvas_handoff.
// To change the tag format, edit that JSON file — NOT the fallback below.
// ═══════════════════════════════════════════════════════════════════════
async function buildTaggedCanvasPrompt(text) {
  const pageSlug = currentPage ? currentPage.slug : 'home';
  const agentCtx = currentPage ? (currentPage.agent_context || '') : '';
  try {
    const resp = await fetch(apiPath('/api/v1/app-prompts'));
    if (resp.ok) {
      const data = await resp.json();
      const cfg = (data.ui_handoffs || {}).canvas_handoff || {};
      if (agentCtx && cfg.template_with_context) {
        return cfg.template_with_context
          .replace(/\{slug\}/g, pageSlug)
          .replace(/\{agent_context\}/g, agentCtx)
          .replace(/\{text\}/g, text);
      }
      if (cfg.template_without_context) {
        return cfg.template_without_context
          .replace(/\{slug\}/g, pageSlug)
          .replace(/\{text\}/g, text);
      }
    }
  } catch (_) { /* fall through to inline fallback */ }
  return agentCtx
    ? `[User → UI Agent → Canvas: "${pageSlug}" | Context: "${agentCtx}"]: ${text}`
    : `[User → UI Agent → Canvas: "${pageSlug}"]: ${text}`;
}

// Hand a tagged prompt off to the shared webAgent on the main chat.
// opts.continueIfActive: when the chat is ALREADY on the webAgent with a live
// session, keep that session (so an interactive canvas's messages stay one
// conversation — login → search → refresh) instead of starting fresh. The
// Canvas chat pill omits it and keeps its "fresh session per prompt" behavior.
async function handoffToWebagent(taggedPrompt, opts) {
  let onWebagent = false;
  if (opts && opts.continueIfActive && typeof app.ensureWebagentAgent === 'function') {
    try {
      const waId = await app.ensureWebagentAgent(app.currentUserId);
      onWebagent = !!waId && app.currentAgentId === waId && !!app.currentSessionId;
    } catch (_) { /* fall back to a fresh session */ }
  }
  if (!onWebagent) {
    await app.startWebagentSession();
  }
  if (app.chatInput && app.chatSend) {
    app.chatInput.value = taggedPrompt;
    // Notify the chat's input listener so the send button enables and
    // the input row picks up the `has-text` class.
    app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    app.chatSend.click();
  }
}

// ── Canvas visibility gate ───────────────────────────────────────────────────
// First-class canvases run agent-authored code with the caller's OWN app trust,
// so who may use Canvas follows its page VISIBILITY (registration required by
// default; "all" includes anonymous; "off" is admins-only; admins + "open" mode
// always pass). The server resolves this per-caller and reports it as
// `canvas_first_class` on /ui-config. Fail closed: if we can't confirm, disable.
async function _fetchCanvasGate() {
  if (_canvasFirstClass !== null) return _canvasFirstClass;
  try {
    // Send the caller's auth token: the gate is identity-based (it depends on who
    // you are and the Canvas page's visibility), so the server needs to see who
    // we are. Without this header it would treat us as anonymous and likely disable.
    const res = await fetch(apiPath('/api/v1/auth/ui-config'), { headers: authHeaders() });
    const data = await res.json();
    _canvasFirstClass = !!data.canvas_first_class;
  } catch (_) {
    _canvasFirstClass = false;
  }
  return _canvasFirstClass;
}

// ── How a canvas mounts: register() OR drop-in (no registration) ────────────
// A canvas runs inside its OWN shadow root, so its code needs a handle to that
// root — its normal document.* would hit the whole app, not the canvas. The app
// hands that over by ONE of three equivalent forms (taught in the Visualizer
// skill); a canvas author picks whichever they like:
//
//   1. register()  — WebagentCanvas.register(function (root, api) { …; return cleanup }).
//      Explicit, and the only form that returns a cleanup() directly.
//   2. drop-in fn  — just define a top-level  function mount(root, api) { … }
//      (no register, no wrapper). The app auto-calls it after the page's scripts
//      run. Hand back a teardown with WebagentCanvas.onCleanup(fn) if needed.
//   3. drop-in inline — use WebagentCanvas.root / .api / .getData() directly in a
//      plain inline <script>; they're placed before the page's code runs.
//
//   • root — the canvas's shadow root; query ITS OWN dom via root.* (root.getElementById
//     / root.querySelector), never document.* and never window-level key/pointer
//     listeners (those would hit the whole app).
//   • api  — the toolbox below: theme + status subscriptions, chat/action/refresh to
//     talk to the agent, getData() for the page's baked-in data, storeCredential → vault,
//     callWithKey(keyId, opts) → call a service using a vault secret attached server-side.
//   • cleanup — optional; the app calls it on teardown. It SHOULD stop any getUserMedia
//     tracks so the camera light goes off (the app also force-stops any <video>/<audio>
//     streams in _teardownLiveCanvas as a safety net, so a drop-in page is covered too).
window.WebagentCanvas = window.WebagentCanvas || {
  // The current mount's root + toolbox — placed just before the page's scripts run
  // (and cleared on teardown), so a drop-in page can use them with no handshake.
  root: null,
  api: null,
  getData() { return _readCanvasData(); },
  register(mountFn) { _runCanvasMount(mountFn); },
  // Let a drop-in page (one that never called register) still hand back a teardown.
  onCleanup(fn) {
    const ctx = _pendingMountCtx;
    if (ctx && typeof fn === 'function') ctx.live.cleanup = fn;
  },
};

// Run a canvas's mount function against the pending mount context, marking the
// page as mounted (so the drop-in fallback below can't double-fire) and keeping
// any cleanup() it returns. Shared by register() and the top-level-mount fallback.
function _runCanvasMount(mountFn) {
  const ctx = _pendingMountCtx;
  if (!ctx || typeof mountFn !== 'function') return false;
  ctx.live.mounted = true;  // it's the chosen entry point — don't also try the fallback
  try {
    const cleanup = mountFn(ctx.root, ctx.api);
    if (typeof cleanup === 'function') ctx.live.cleanup = cleanup;
    return true;
  } catch (e) {
    // eslint-disable-next-line no-console
    console.error('[canvas] mount error:', e);
    // Also record it on the canvas's own log so get_canvas_logs surfaces mount
    // failures (register()/drop-in mount throws don't go through the scoped console).
    try {
      if (ctx.live && ctx.live.logs) {
        ctx.live.logs.push({
          ts: Date.now(), level: 'error',
          text: 'mount error: ' + ((e && e.message) || e),
          stack: (e && e.stack) || undefined,
        });
        _scheduleCanvasLogFlush(ctx.live);
      }
    } catch (_) {}
    updateStatus('Canvas script error: ' + (e && e.message || e), 'error');
    return false;
  }
}

// Read the canvas's DATA bag — the content the server baked into the page as
// window.__CANVAS_DATA (from the canvas's data.json). Lets a page read its
// records via api.getData() instead of hardcoding them into the markup, so the
// agent updates data without rewriting the page. Always returns an object;
// {} when the canvas has no data file. (Reset to null per mount below so one
// canvas's data can never leak into the next.)
function _readCanvasData() {
  try {
    const d = window.__CANVAS_DATA;
    return (d && typeof d === 'object') ? d : {};
  } catch (_) { return {}; }
}

// Build the toolbox handed to a canvas's mount(root, api).
function _buildCanvasApi(live) {
  const emitStatus = (state, extra) => {
    for (const cb of live.statusCbs) {
      try { cb(Object.assign({ state }, extra || {})); } catch (_) {}
    }
  };
  live._emitStatus = emitStatus;

  const sendAction = async (verb, text) => {
    if (!app.currentUserId) { updateStatus('Sign in to use this canvas', 'error'); return; }
    let t = (text == null ? '' : String(text)).trim();
    if (!t) {
      if (verb === 'refresh') t = 'Refresh this canvas with my latest data.';
      else return;
    }
    // Only a bounded plain-text instruction reaches the agent — never a secret
    // (those use storeCredential → the vault, never the agent's context).
    if (t.length > 4000) t = t.slice(0, 4000);
    emitStatus('working');
    updateStatus('webAgent is on the chat →');
    try {
      const tagged = await buildTaggedCanvasPrompt(t);
      // Keep one conversation across the dashboard's actions (login → search → reply).
      await handoffToWebagent(tagged, { continueIfActive: true });
    } catch (err) {
      updateStatus('Could not reach webAgent: ' + (err && err.message || err), 'error');
      emitStatus('error', { error: String(err && err.message || err) });
    }
  };

  return {
    get theme() { return currentTheme(); },
    getTheme: () => currentTheme(),
    onTheme: (cb) => { if (typeof cb === 'function') live.themeCbs.push(cb); },
    onStatus: (cb) => { if (typeof cb === 'function') live.statusCbs.push(cb); },
    chat: (text) => sendAction('chat', text),
    send: (text) => sendAction('chat', text),
    action: (verb, text) => sendAction(String(verb || 'chat'), text),
    refresh: () => sendAction('refresh', ''),
    getData: () => _readCanvasData(),
    storeCredential: (ability, values) => _storeCredentialToVault(live, ability, values),
    callWithKey: (keyId, opts) => _callWithVaultKey(live, keyId, opts),
  };
}

// Make an authenticated call to a service using a VAULT KEY by id — the secret
// is attached SERVER-SIDE (the agent reserved the key with request_credential;
// the user filled it via the secure card). The plaintext never reaches this page.
// opts: { path | url, method, headers, query, json, body }. Returns the parsed
// service response { http_status, json?, text? }, or null on failure.
async function _callWithVaultKey(live, keyId, opts) {
  const id = (typeof keyId === 'string' && keyId) ? keyId : null;
  if (!id) return null;
  const o = (opts && typeof opts === 'object') ? opts : {};
  const payload = {
    key_id: id,
    url: o.url || null,
    path: o.path || null,
    method: o.method || 'GET',
    headers: (o.headers && typeof o.headers === 'object') ? o.headers : null,
    query: (o.query && typeof o.query === 'object') ? o.query : null,
    json_body: (o.json !== undefined) ? o.json : null,
    body: (typeof o.body === 'string') ? o.body : null,
  };
  try {
    const resp = await fetch(apiPath('/api/v1/canvases/vault/proxy'), {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      let detail = '';
      try { detail = (await resp.json()).detail || ''; } catch (_) {}
      updateStatus('Vault call failed: ' + (detail || resp.status), 'error');
      return null;
    }
    return await resp.json();
  } catch (err) {
    updateStatus('Vault call failed: ' + (err && err.message || err), 'error');
    return null;
  }
}

// Secure credential capture — values go STRAIGHT to the encrypted vault, NEVER
// to the agent. The agent later uses them by reference (server-side reads), so
// the plaintext never enters its context. Same guarantee as the old bridge's
// store-credential path; now a direct api.storeCredential(ability, values) call.
async function _storeCredentialToVault(live, ability, values) {
  const ab = (typeof ability === 'string' && ability) ? ability : 'browser_control';
  const vals = (values && typeof values === 'object') ? values : null;
  const emit = (live && live._emitStatus) || (() => {});
  if (!vals) { emit('error', { error: 'no values' }); return false; }
  emit('working');
  updateStatus('Saving credentials to the vault…');
  try {
    const resp = await fetch(apiPath('/api/v1/abilities/' + encodeURIComponent(ab) + '/credentials'), {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ values: vals }),
    });
    const ok = resp.ok;
    emit(ok ? 'stored' : 'error');
    updateStatus(ok ? 'Credentials saved securely ✓' : 'Could not save credentials', ok ? '' : 'error');
    return ok;
  } catch (err) {
    emit('error', { error: String(err && err.message || err) });
    updateStatus('Could not save credentials: ' + (err && err.message || err), 'error');
    return false;
  }
}

// ── Per-canvas console capture (CANVAS-CONSOLE-LOG) ─────────────────────────
// A canvas's scripts run in the page's GLOBAL scope, so their console.* calls would
// be indistinguishable from the app's own. We give each live canvas its OWN console
// object and inject it as the `console` parameter of every inline classic script's
// IIFE wrapper (see the script loop — CANVAS-SCRIPT-SCOPE). Lexical scoping means ALL
// code inside that script — including event handlers, intervals and promise callbacks
// it defines later — resolves `console` to this object, so we capture exactly that one
// canvas's output and none of the app's. Each call is still forwarded to the REAL
// console (the dev panel keeps showing it), buffered on the live canvas, and flushed in
// small batches to that canvas's page-scoped log file on the server
// (POST …/{slug}/logs) — where the design agent reads it back with get_canvas_logs,
// without any logs.db / codebase-admin access. (Module and external `src` scripts keep
// their own scope and are NOT instrumented — agent canvas code is inline classic.)
const _CANVAS_LOG_LEVELS = ['log', 'info', 'warn', 'error', 'debug'];
const _CANVAS_LOG_BUFFER_MAX = 300;   // cap the in-browser buffer between flushes
const _CANVAS_LOG_FLUSH_MS = 1500;    // debounce: collect a burst, then POST once

function _stringifyLogArg(a) {
  if (typeof a === 'string') return a;
  if (a === null) return 'null';
  if (a === undefined) return 'undefined';
  if (a instanceof Error) return (a.stack || a.message || String(a));
  try { return JSON.stringify(a); } catch (_) { return String(a); }
}

// A console bound to ONE live canvas: forwards to the real console and records into
// live.logs. Built on Object.create(realConsole) so every method we don't override
// (table, dir, group, assert…) still delegates to the real console unchanged.
function _makeCanvasConsole(live) {
  const real = window.console || {};
  const scoped = Object.create(real);
  const record = (level, args) => {
    try {
      const text = Array.prototype.map.call(args, _stringifyLogArg).join(' ');
      let stack = '';
      for (const a of args) { if (a instanceof Error && a.stack) { stack = a.stack; break; } }
      live.logs.push({ ts: Date.now(), level, text: text.slice(0, 4000), stack: stack || undefined });
      if (live.logs.length > _CANVAS_LOG_BUFFER_MAX) live.logs.shift();
      _scheduleCanvasLogFlush(live);
    } catch (_) {}
  };
  _CANVAS_LOG_LEVELS.forEach((level) => {
    const fn = (typeof real[level] === 'function') ? real[level] : (real.log || function () {});
    scoped[level] = function () {
      try { fn.apply(real, arguments); } catch (_) {}
      record(level, arguments);
    };
  });
  return scoped;
}

// Shadow-scoped `document` for a canvas's inline scripts (CANVAS-SCOPED-DOCUMENT).
// The body markup lives inside THIS canvas's shadow root, but classic inline
// scripts run in the page's GLOBAL scope — so a naive `document.getElementById(id)`
// (or querySelector / getElementsBy*) queries the MAIN page, never the shadow, and
// returns null. Agent canvases overwhelmingly write
// `document.getElementById('x').addEventListener(...)`, which then throws
// "Cannot read properties of null (reading 'addEventListener')" and aborts mount.
// We hand each inline script a Proxy over the real document whose element-lookup
// methods search this shadow root FIRST and fall back to the real document only
// when the shadow has nothing (so a canvas that legitimately reaches into the app
// DOM still works). Everything else — createElement, body, addEventListener,
// title, cookie, … — delegates straight to the real document unchanged. We also
// reroute a `DOMContentLoaded` listener to fire immediately: by the time a canvas
// mounts the page loaded long ago, so the event already fired and the handler
// (a near-universal "wait until the DOM is ready, then init" wrapper) would never
// run otherwise.
function _makeCanvasDocument(shadowRoot) {
  const real = document;
  if (!shadowRoot) return real;
  const esc = (s) => ((window.CSS && CSS.escape) ? CSS.escape(String(s)) : String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&'));
  // Run a shadow-root query; if it finds nothing, fall back to the real document.
  const shadowFirst = (shadowFn, realFn, emptyIsHit) => {
    try {
      const r = shadowFn();
      if (r && (emptyIsHit || r.length || r.nodeType)) return r;
    } catch (_) {}
    return realFn();
  };
  const overrides = {
    getElementById(id) {
      try {
        if (typeof shadowRoot.getElementById === 'function') {
          const el = shadowRoot.getElementById(id);
          if (el) return el;
        }
      } catch (_) {}
      return real.getElementById(id);
    },
    querySelector(sel) {
      return shadowFirst(() => shadowRoot.querySelector(sel), () => real.querySelector(sel));
    },
    querySelectorAll(sel) {
      return shadowFirst(() => shadowRoot.querySelectorAll(sel), () => real.querySelectorAll(sel));
    },
    // ShadowRoot has no getElementsBy* — emulate them with querySelectorAll so the
    // same shadow-first resolution applies (returns a static NodeList, which is a
    // drop-in for the live HTMLCollection in every realistic canvas use).
    getElementsByClassName(cls) {
      const sel = String(cls).trim().split(/\s+/).filter(Boolean).map((c) => '.' + esc(c)).join('');
      if (!sel) return real.getElementsByClassName(cls);
      return shadowFirst(() => shadowRoot.querySelectorAll(sel), () => real.getElementsByClassName(cls));
    },
    getElementsByTagName(tag) {
      const sel = (String(tag) === '*') ? '*' : String(tag);
      return shadowFirst(() => shadowRoot.querySelectorAll(sel), () => real.getElementsByTagName(tag));
    },
    getElementsByName(name) {
      const sel = '[name="' + String(name).replace(/"/g, '\\"') + '"]';
      return shadowFirst(() => shadowRoot.querySelectorAll(sel), () => real.getElementsByName(name));
    },
    addEventListener(type, listener, options) {
      // The canvas DOM is fully laid out before its scripts run, so a "ready"
      // listener has already missed its event — fire it on the next tick instead.
      if ((type === 'DOMContentLoaded' || type === 'readystatechange') && typeof listener === 'function') {
        setTimeout(() => { try { listener.call(real, { type }); } catch (_) {} }, 0);
        return;
      }
      return real.addEventListener(type, listener, options);
    },
  };
  return new Proxy(real, {
    get(target, prop) {
      if (Object.prototype.hasOwnProperty.call(overrides, prop)) return overrides[prop];
      const val = target[prop];
      return (typeof val === 'function') ? val.bind(target) : val;
    },
    set(target, prop, value) { try { target[prop] = value; } catch (_) {} return true; },
  });
}

// Debounced flush so a burst of logs becomes one POST.
function _scheduleCanvasLogFlush(live) {
  if (!live || live._logFlushT) return;
  live._logFlushT = setTimeout(() => { live._logFlushT = null; _flushCanvasLogs(live); }, _CANVAS_LOG_FLUSH_MS);
}

// Ship the buffered entries to this canvas's page-scoped log file. keepalive so it
// still goes out during teardown / page-hide. Best-effort: logs are a debugging
// side-channel — a failed POST just drops them, never disrupts the canvas.
function _flushCanvasLogs(live) {
  if (!live || !live.logs || !live.logs.length) return;
  const slug = live.slug;
  const userId = app.currentUserId;
  if (!userId || !slug) { live.logs.length = 0; return; }
  const batch = live.logs.splice(0, live.logs.length);
  try {
    fetch(
      apiPath('/api/v1/canvases/' + encodeURIComponent(userId) + '/' + encodeURIComponent(slug) + '/logs'),
      {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ entries: batch }),
        keepalive: true,
      },
    ).catch(() => {});
  } catch (_) { /* best-effort */ }
}

// ── Persisted view state (localStorage) ────────────────────────────────────
// Make a refresh feel like nothing happened: remember which page was open, the
// canvas scroll position, and a best-effort snapshot of the agent-page's own
// "panel" state (open <details>, aria-expanded toggles, checkbox/radio/select).
// Everything is keyed by user + page slug and lives ONLY in the browser's
// localStorage. SECRETS NEVER PERSIST: we deliberately skip text/password/email
// inputs (credential fields go straight to the vault, never to disk here).
const _LS_LAST = 'webagent.canvas.lastPage.v1';   // { [userId]: slug }
const _LS_VIEW = 'webagent.canvas.viewState.v1';  // { 'userId::slug': {st,sl,inner} }

// _lsGet/_lsSet wrap a try/catch JSON round-trip and are reused across several
// callers, so they stay as helpers. The trivial one-expression wrappers _lsUser
// (`app.currentUserId || 'anon'`) and _viewKey (`user::slug`) are inlined below.
function _lsGet(key) { try { return JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (_) { return {}; } }
function _lsSet(key, obj) { try { localStorage.setItem(key, JSON.stringify(obj)); } catch (_) {} }

// Last page the user was viewing (so a refresh reopens it, not always Home).
function _savedLastSlug() { return _lsGet(_LS_LAST)[app.currentUserId || 'anon'] || null; }
function _rememberLastSlug(slug) {
  if (!slug) return;
  const m = _lsGet(_LS_LAST); m[app.currentUserId || 'anon'] = slug; _lsSet(_LS_LAST, m);
}

function _savedView(slug) { return _lsGet(_LS_VIEW)[(app.currentUserId || 'anon') + '::' + slug] || null; }
function _saveView(slug, state) {
  if (!slug) return;
  const m = _lsGet(_LS_VIEW); m[(app.currentUserId || 'anon') + '::' + slug] = state; _lsSet(_LS_VIEW, m);
}

// A stable position path from the shadow root to an element, using element-child
// indexes. The agent's HTML is rebuilt identically on each load, so the same
// path resolves to the same node — letting us re-apply saved state after remount.
function _elPath(el, root) {
  const parts = [];
  let node = el;
  while (node && node !== root) {
    const parent = node.parentNode;
    if (!parent || !parent.children) break;
    parts.unshift(Array.prototype.indexOf.call(parent.children, node));
    node = parent;
  }
  return parts.join('/');
}
function _resolvePath(path, root) {
  let node = root;
  for (const seg of path.split('/')) {
    if (!node || !node.children) return null;
    node = node.children[+seg];
  }
  return node || null;
}

// Snapshot the agent-page's panel/scroll state from its shadow DOM. Captures
// only non-sensitive, structural state (see secrets note above).
function _captureInner(shadow) {
  const inner = {};
  const put = (el, data) => { const p = _elPath(el, shadow); inner[p] = Object.assign(inner[p] || {}, data); };
  let all; try { all = shadow.querySelectorAll('*'); } catch (_) { return inner; }
  all.forEach((el) => {
    if (el.scrollTop || el.scrollLeft) put(el, { st: el.scrollTop, sl: el.scrollLeft });
    const tag = el.tagName;
    if (tag === 'DETAILS') put(el, { open: !!el.open });
    if (el.hasAttribute && el.hasAttribute('aria-expanded')) put(el, { ax: el.getAttribute('aria-expanded') });
    if (tag === 'SELECT') put(el, { val: el.value });
    if (tag === 'INPUT') {
      const t = (el.type || '').toLowerCase();
      if (t === 'checkbox' || t === 'radio') put(el, { ck: !!el.checked });
      // text/password/email/etc are intentionally NOT saved (may hold secrets).
    }
  });
  return inner;
}

// Re-apply the saved panel state (structural keys). Scroll is handled separately
// by _restoreInnerScroll because it needs the content to have laid out first.
function _restoreInnerStruct(shadow, inner) {
  if (!inner) return;
  for (const p of Object.keys(inner)) {
    const el = _resolvePath(p, shadow);
    if (!el) continue;
    const d = inner[p];
    if ('open' in d && el.tagName === 'DETAILS') el.open = d.open;
    if ('ax' in d && el.setAttribute) el.setAttribute('aria-expanded', d.ax);
    if ('ck' in d && el.tagName === 'INPUT') {
      el.checked = d.ck;
      try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch (_) {}
    }
    if ('val' in d && el.tagName === 'SELECT') {
      el.value = d.val;
      try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch (_) {}
    }
  }
}
function _restoreInnerScroll(shadow, inner) {
  if (!inner) return;
  for (const p of Object.keys(inner)) {
    const d = inner[p];
    if (!('st' in d) && !('sl' in d)) continue;
    const el = _resolvePath(p, shadow);
    if (!el) continue;
    if (d.sl) el.scrollLeft = d.sl;
    if (d.st) el.scrollTop = d.st;
  }
}

// Throttled save of the live canvas's current view state.
let _saveTimer = null;
function _scheduleSaveView() {
  if (_saveTimer) return;
  _saveTimer = setTimeout(() => { _saveTimer = null; _saveLiveView(); }, 400);
}
function _saveLiveView() {
  const live = _liveCanvas;
  if (!live || !live.shadow || !live.slug) return;
  const host = document.getElementById('canvas-host');
  _saveView(live.slug, {
    st: host ? host.scrollTop : 0,
    sl: host ? host.scrollLeft : 0,
    inner: _captureInner(live.shadow),
  });
}

// After a remount, replay the saved state for this slug. Structural panel state
// is applied immediately (it exists in the initial markup); the main + inner
// scroll positions are re-applied on a short retry schedule because the agent's
// own scripts may still be building/growing the content. We stop early once the
// target is reached and bail if the canvas was torn down or the user scrolled.
function _scheduleRestoreView(slug, shadow) {
  const state = _savedView(slug);
  if (!state) return;
  _restoreInnerStruct(shadow, state.inner);
  const host = document.getElementById('canvas-host');
  const want = state.st || 0;
  [0, 60, 150, 320, 650, 1100].forEach((delay) => {
    setTimeout(() => {
      // Bail if the canvas was torn down/swapped, or the target is already met
      // (so a late retry can't yank the user back after they've scrolled away).
      if (!_liveCanvas || _liveCanvas.shadow !== shadow) return;
      if (host && want && Math.abs(host.scrollTop - want) > 2) {
        if (state.sl) host.scrollLeft = state.sl;
        host.scrollTop = want;
      }
      _restoreInnerScroll(shadow, state.inner);
    }, delay);
  });
}

// ── First-class canvas rendering (shadow DOM) ───────────────────────────────

// Base style injected into every canvas's shadow root BEFORE the agent's own
// styles (so the agent's CSS wins). A box reset, host sizing, a TRANSPARENT
// default background (so the app's own animated background shows through unless
// the agent paints one), and a slim themed scrollbar.
const _CANVAS_BASE_STYLE =
  // Custom properties inherit through the shadow boundary, so the app's themed
  // --font-sans / --fg-1 / --brand-rgb resolve here and track the live theme.
  ':host{display:block;width:100%;min-height:100%;background:transparent;' +
  "font-family:var(--font-sans,'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif);" +
  'color:var(--fg-1);-webkit-font-smoothing:antialiased;}' +
  '*,*::before,*::after{box-sizing:border-box;}' +
  // The app's own body wrapper uses a PRIVATE class so an agent that styles its
  // OWN `.canvas-root` (a name they reach for) can't collide with ours and double
  // its grid/padding — the old half-width / fat-margin bug. (grep: WA-CANVAS-BODY)
  '.wa-canvas-body{min-height:100%;}' +
  '*{scrollbar-width:thin;scrollbar-color:rgba(var(--brand-rgb),0.32) transparent;}' +
  '*::-webkit-scrollbar{width:6px;height:6px;}' +
  '*::-webkit-scrollbar-track{background:transparent;}' +
  '*::-webkit-scrollbar-thumb{background:rgba(var(--brand-rgb),0.38);border-radius:999px;}' +
  '*::-webkit-scrollbar-thumb:hover{background:rgba(var(--brand-rgb),0.62);}';

// Toggle the theme class on the shadow host so the agent's :host(.light) /
// :host(.dark) selectors respond.
function _applyCanvasTheme(mountEl, theme) {
  if (!mountEl) return;
  mountEl.classList.toggle('light', theme === 'light');
  mountEl.classList.toggle('dark', theme !== 'light');
}

// Tear the live canvas down: let it release its own resources (camera!),
// defensively stop any lingering media so the webcam light goes off, then empty
// the host. Always called before mounting a new canvas or leaving the tab.
function _teardownLiveCanvas() {
  const live = _liveCanvas;
  // Capture the outgoing canvas's view state before we drop it, so switching
  // away and back (or a refresh mid-session) restores scroll + open panels.
  if (live) {
    try { _saveLiveView(); } catch (_) {}
    // Flush any console output this canvas buffered before we let it go.
    try {
      if (live._logFlushT) { clearTimeout(live._logFlushT); live._logFlushT = null; }
      _flushCanvasLogs(live);
    } catch (_) {}
  }
  _liveCanvas = null;
  _pendingMountCtx = null;
  // Drop the drop-in globals so the outgoing canvas's root/api can't leak into
  // the next mount (or into app code between canvases).
  try { window.WebagentCanvas.root = null; window.WebagentCanvas.api = null; } catch (_) {}
  try { window.mount = undefined; } catch (_) {}
  try { window.__canvasConsole = null; } catch (_) {}
  try { window.__canvasDocument = null; } catch (_) {}
  if (!live) return;
  if (typeof live.cleanup === 'function') { try { live.cleanup(); } catch (_) {} }
  try {
    const media = live.shadow ? live.shadow.querySelectorAll('video,audio') : [];
    media.forEach((el) => {
      const s = el.srcObject;
      if (s && typeof s.getTracks === 'function') {
        s.getTracks().forEach((t) => { try { t.stop(); } catch (_) {} });
      }
      try { el.srcObject = null; } catch (_) {}
    });
  } catch (_) {}
  if (live.host) { try { live.host.innerHTML = ''; } catch (_) {} }
}

// Graft an agent-authored canvas document into the app inside a fresh shadow
// root, then run its scripts so register(fn) hands (shadow, api) to the canvas.
function mountCanvas(html, title) {
  const host = document.getElementById('canvas-host');
  if (!host) return;
  _teardownLiveCanvas();

  // Fresh shadow each mount: attachShadow throws if one already exists, so we
  // host it on a new child we can replace wholesale.
  host.innerHTML = '';
  const mountEl = document.createElement('div');
  mountEl.className = 'canvas-mount';
  mountEl.style.cssText = 'display:block;width:100%;min-height:100%;';
  host.appendChild(mountEl);
  const shadow = mountEl.attachShadow({ mode: 'open' });

  const live = {
    host, mountEl, shadow, cleanup: null,
    themeCbs: [], statusCbs: [],
    mounted: false,  // set true once register() or the drop-in fallback mounts the page
    slug: currentPage ? currentPage.slug : 'home',
    logs: [],        // this canvas's captured console output, flushed to its log file
  };
  const api = _buildCanvasApi(live);

  // Remember this as the page to reopen after a refresh, and persist the page's
  // own panel state (toggles, sub-scrolls) as the user interacts with it.
  _rememberLastSlug(live.slug);
  shadow.addEventListener('scroll', () => _scheduleSaveView(), { capture: true, passive: true });
  shadow.addEventListener('change', () => _scheduleSaveView(), true);
  shadow.addEventListener('toggle', () => _scheduleSaveView(), true);
  shadow.addEventListener('click',  () => _scheduleSaveView(), true);

  const base = document.createElement('style');
  base.textContent = _CANVAS_BASE_STYLE;
  shadow.appendChild(base);

  let doc = null;
  try { doc = new DOMParser().parseFromString(html || '', 'text/html'); } catch (_) { doc = null; }

  if (doc) {
    // Lift the agent's <style> blocks and any CDN stylesheet links.
    doc.querySelectorAll('style').forEach((st) => {
      const s = document.createElement('style');
      s.textContent = st.textContent;
      shadow.appendChild(s);
    });
    doc.querySelectorAll('link[rel="stylesheet"]').forEach((ln) => {
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      if (ln.getAttribute('href')) l.href = ln.getAttribute('href');
      shadow.appendChild(l);
    });

    // Body markup → our private .wa-canvas-body wrapper (scripts parked inert by
    // innerHTML are stripped; we re-create them below so they actually execute).
    // Private class name so an agent's own `.canvas-root` can't collide. (WA-CANVAS-BODY)
    const root = document.createElement('div');
    root.className = 'wa-canvas-body';
    root.innerHTML = doc.body ? doc.body.innerHTML : (html || '');
    root.querySelectorAll('script').forEach((s) => s.remove());
    shadow.appendChild(root);

    _applyCanvasTheme(mountEl, currentTheme());

    // Prime the handshake, then run the scripts. Inline scripts execute
    // synchronously on append; register(fn) fires during that and calls
    // fn(shadow, api). Kept referenced via _liveCanvas for late (CDN) registers.
    _liveCanvas = live;
    _pendingMountCtx = { root: shadow, api, live };
    // Clear any previous canvas's baked data before running this one's scripts.
    // The page's injected <head> data block (window.__CANVAS_DATA=…) runs first
    // in the loop below and repopulates it; a canvas with no data file keeps {}.
    try { window.__CANVAS_DATA = null; } catch (_) {}
    // Place the mount context as ready globals so a DROP-IN canvas can use
    // WebagentCanvas.root / .api / .getData() with no register() handshake, and
    // clear any previous page's top-level mount() so a stale one can't fire below.
    try { window.WebagentCanvas.root = shadow; window.WebagentCanvas.api = api; } catch (_) {}
    try { window.mount = undefined; } catch (_) {}
    // Expose this canvas's console so each inline script's IIFE wrapper picks it up
    // as its `console` parameter (CANVAS-CONSOLE-LOG) — capturing the page's output
    // into live.logs for the per-canvas log file the agent reads with get_canvas_logs.
    try { window.__canvasConsole = _makeCanvasConsole(live); } catch (_) {}
    // Shadow-scoped `document` so each inline script's `document.getElementById`/
    // querySelector resolves inside THIS canvas's shadow root (CANVAS-SCOPED-DOCUMENT),
    // picked up as the script IIFE's `document` parameter below.
    try { window.__canvasDocument = _makeCanvasDocument(shadow); } catch (_) {}
    doc.querySelectorAll('script').forEach((os) => {
      const s = document.createElement('script');
      for (const a of os.attributes) s.setAttribute(a.name, a.value);
      // Inline classic scripts run in the page's GLOBAL lexical scope, so a
      // top-level `const ICON = …` (or let/class/function) becomes a realm-wide
      // binding that PERSISTS after the script element is removed. Re-mounting
      // the same canvas (switching pages back), or a canvas with two such blocks,
      // then re-declares it and throws "Identifier 'ICON' has already been
      // declared" — which aborts the rest of that script, so the canvas's mount
      // never runs (cascading into null-element errors). Wrap each inline classic
      // script in a private IIFE so its top-level declarations are scoped to one
      // execution and can't collide. register()/WebagentCanvas.* still work (they
      // are globals), and a drop-in top-level `mount(root, api)` is re-exposed on
      // window so the fallback below still finds it. External (src) and module
      // scripts already have their own scope, so they are appended untouched.
      // (CANVAS-SCRIPT-SCOPE)
      const type = (os.getAttribute('type') || '').toLowerCase();
      const isInlineClassic = !os.hasAttribute('src') && type !== 'module'
        && (type === '' || type === 'text/javascript' || type === 'application/javascript')
        && os.textContent.trim() !== '';
      if (isInlineClassic) {
        // The IIFE takes a `console` parameter bound to THIS canvas (CANVAS-CONSOLE-LOG):
        // the page's console.* is captured to its own log file, and a synchronous throw
        // is caught + recorded as an error (attributed to this canvas) instead of just
        // aborting silently — the real console still sees it, so the dev panel/logs.db
        // are unaffected.
        // `document` is bound to this canvas's shadow-scoped document (CANVAS-SCOPED-DOCUMENT)
        // so getElementById/querySelector inside the script find the canvas's own elements.
        s.textContent =
          ';(function(console, document){\ntry{\n' + os.textContent +
          '\n;try { if (typeof mount === "function") window.mount = mount; } catch (_e) {}\n' +
          '}catch(__cerr){ try { console.error("Uncaught script error:", (__cerr && __cerr.stack) || String(__cerr)); } catch (_e2) {} }\n' +
          '})(window.__canvasConsole || console, window.__canvasDocument || document);';
      } else {
        s.textContent = os.textContent;
      }
      shadow.appendChild(s);
    });
    // Drop-in fallback: a page that defined a top-level mount(root, api) but never
    // called register() is auto-mounted here. register() fired synchronously during
    // the loop above (setting live.mounted); a page that did its work inline against
    // WebagentCanvas.root has also already run and defines no window.mount — so this
    // only ever catches the plain "function mount(root, api){…}" form.
    if (!live.mounted && typeof window.mount === 'function') {
      _runCanvasMount(window.mount);
    }
  } else {
    _liveCanvas = live;
  }

  // Replay the saved scroll position + open-panel state for this page (retries
  // briefly while the agent's own scripts finish laying out the content).
  _scheduleRestoreView(live.slug, shadow);

  _setStage('page');
  updateStatus(title || 'Ready');
}

// Show the "Canvas not enabled for you" notice in place of a canvas (the
// server-side visibility gate refused this caller — see _fetchCanvasGate).
function _showCanvasDisabledNotice() {
  const host        = document.getElementById('canvas-host');
  const placeholder = document.getElementById('canvas-placeholder');
  const loading     = document.getElementById('canvas-loading');
  _teardownLiveCanvas();
  if (loading) loading.style.display = 'none';
  if (host)    host.style.display    = 'none';
  if (placeholder) {
    placeholder.style.display = 'flex';
    const text = placeholder.querySelector('.canvas-placeholder-text');
    const hint = placeholder.querySelector('.canvas-placeholder-hint');
    if (text) text.textContent = 'Canvas isn’t enabled for your account';
    if (hint) hint.textContent = 'Canvases run real code with your own app access, so who can use Canvas follows its page visibility (an admin sets this in App Settings — sign-in is required by default).';
  }
  updateStatus('Canvas disabled for this account', 'error');
}

// ── WebSocket event handler ───────────────────────────────────────────────────

function handleEvent(event) {
  if (!canvasActive) return;

  const isToolEvent = event.type === 'tool_result' || event.type === 'tool_call';
  const toolName    = event.tool || event.tool_name || '';

  // render_visual (full rewrite) AND edit_canvas (surgical edit) both save through
  // the same path and return {path, slug, title} — so both drive the live reload.
  if (isToolEvent && (toolName === 'render_visual' || toolName === 'edit_canvas')) {
    if (event.type === 'tool_call') {
      showLoading();
      updateStatus(toolName === 'edit_canvas' ? 'Applying edit...' : 'Rendering page...');
    } else if (event.type === 'tool_result') {
      try {
        const result = typeof event.result === 'string'
          ? JSON.parse(event.result)
          : event.result;
        if (result && result.status === 'ok' && result.path) {
          // If the rendered page matches current, reload it
          // If it's a different page (agent created/updated a different one), refresh list
          const renderedSlug = result.slug || 'home';
          if (currentPage && currentPage.slug === renderedSlug) {
            showCanvas(result.path, result.title || currentPage.title);
          } else {
            // Refresh page list to pick up any new pages, then switch to the rendered one
            loadPages().then(() => {
              const p = pages.find(q => q.slug === renderedSlug);
              if (p) {
                currentPage = p;
                showCanvas(p.url, p.title);
                _syncToolbar();
              }
            });
          }
        }
      } catch (_) { /* ignore parse errors */ }
    }
  }

  // Handle create_canvas tool results — refresh canvas list
  if (isToolEvent && toolName === 'create_canvas' && event.type === 'tool_result') {
    try {
      const result = typeof event.result === 'string'
        ? JSON.parse(event.result)
        : event.result;
      if (result && result.status === 'ok' && result.canvas) {
        loadPages().then(() => {
          const newSlug = result.canvas.slug;
          const p = pages.find(q => q.slug === newSlug);
          if (p) {
            currentPage = p;
            showCanvas(p.url, p.title);
            _syncToolbar();
          }
        });
      }
    } catch (_) { /* ignore */ }
  }

  // Handle delete_canvas tool results — refresh canvas list
  if (isToolEvent && toolName === 'delete_canvas' && event.type === 'tool_result') {
    loadPages();
  }

  if (event.type === 'pipeline') {
    if (event.step === 'llm_call_start') {
      updateStatus('Page agent thinking...');
    } else if (event.step === 'execute_start' || event.step === 'execute_batch_start') {
      updateStatus('Building page...');
    }
  }

  if (event.type === 'error') {
    showError(event.error || event.message || 'Unknown error');
  }
}

// ── New page dialog ───────────────────────────────────────────────────────────

function showNewPageDialog() {
  const dialog = document.getElementById('canvas-new-page-dialog');
  const input  = document.getElementById('canvas-dialog-input');
  if (!dialog) return;
  dialog.style.display = 'flex';
  if (input) { input.value = ''; input.focus(); }
}

function hideNewPageDialog() {
  const dialog = document.getElementById('canvas-new-page-dialog');
  if (dialog) dialog.style.display = 'none';
}

async function submitNewPage() {
  const input = document.getElementById('canvas-dialog-input');
  if (!input) return;
  const title = input.value.trim();
  if (!title) return;

  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'page';
  hideNewPageDialog();

  try {
    const res = await fetch(apiPath('/api/v1/canvases'), {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: app.currentUserId,
        slug,
        title,
      }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      await loadPages();
      const p = pages.find(q => q.slug === data.canvas.slug);
      if (p) {
        currentPage = p;
        showCanvas(p.url, p.title);
        _syncToolbar();
      }
    } else {
      updateStatus('Could not create page: ' + (data.detail || data.message || 'error'), 'error');
    }
  } catch (e) {
    updateStatus('Failed to create page: ' + e.message, 'error');
  }
}

// ── Canvas / display helpers ──────────────────────────────────────────────────

// Load a canvas by its html-endpoint URL and graft it into the app. Gated to
// local mode (fail-closed). `url` is the page's `.url` (or a render_visual
// `.path`); both point at /api/v1/canvases/{user}/{slug}/html.
async function showCanvas(url, title) {
  const host        = document.getElementById('canvas-host');
  const placeholder = document.getElementById('canvas-placeholder');
  const loading     = document.getElementById('canvas-loading');

  if (_canvasFirstClass === false) { _showCanvasDisabledNotice(); return; }

  if (placeholder) placeholder.style.display = 'none';
  if (loading)     loading.style.display     = 'none';
  if (!host || !url) { showPlaceholder(); return; }

  try {
    const bust = url + (url.includes('?') ? '&' : '?') + '_t=' + Date.now();
    const res  = await fetch(bust, { headers: authHeaders() });
    if (!res.ok) { showError('Canvas not found'); return; }
    const html = await res.text();
    mountCanvas(html, title);
  } catch (e) {
    showError('Failed to load canvas: ' + (e && e.message || e));
  }
}

function currentTheme() {
  return document.body.classList.contains('light-mode') ? 'light' : 'dark';
}

// Forward app theme changes to the live canvas whenever body.class toggles:
// flip the shadow host's theme class and notify the canvas's onTheme subscribers.
(function watchTheme() {
  if (typeof MutationObserver === 'undefined') return;
  const obs = new MutationObserver(() => {
    const live = _liveCanvas;
    if (!live) return;
    const theme = currentTheme();
    _applyCanvasTheme(live.mountEl, theme);
    for (const cb of live.themeCbs) { try { cb(theme); } catch (_) {} }
  });
  obs.observe(document.body, { attributes: true, attributeFilter: ['class'] });
})();

// The stage shows exactly ONE of three things: a spinner (loading), the
// "nothing here yet" placeholder (empty), or the live canvas host (page).
// _setStage is the single display switch the show* helpers below route through
// (mirrors browser.js _setStage).
function _setStage(state) {  // 'loading' | 'empty' | 'page'
  const host        = document.getElementById('canvas-host');
  const placeholder = document.getElementById('canvas-placeholder');
  const loading     = document.getElementById('canvas-loading');
  if (host)        host.style.display        = state === 'page'    ? 'block' : 'none';
  if (placeholder) placeholder.style.display = state === 'empty'   ? 'flex'  : 'none';
  if (loading)     loading.style.display     = state === 'loading' ? 'flex'  : 'none';
}

function showLoading() {
  _setStage('loading');
}

function showPlaceholder() {
  _teardownLiveCanvas();
  _setStage('empty');
  updateStatus('');
}

function showError(message) {
  showPlaceholder();
  updateStatus('⚠ ' + message, 'error');
}

function updateStatus(text, type) {
  const status = document.getElementById('canvas-status');
  if (!status) return;
  status.textContent = text || '';
  status.className   = 'canvas-status';
  if (type === 'error') status.classList.add('canvas-status-error');
}
