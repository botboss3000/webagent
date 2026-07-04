'use strict';

// Gen UI tab (visualizer) — AI page builder CORE: page loading/state, a
// first-class shadow-DOM renderer, the new-page dialog, and render_visual event
// handling. The footer control row (chat pill + page selector + new / refresh) is
// a SEPARATE, swappable module — js/genui-toolbar.js — so a user can redesign the
// toolbar without touching this core. We hand it a small `ctx` bridge and call
// its syncPages() handle whenever page state changes. Markup:
// ui/main-panel/genui/genui.html (the footer is an empty mount the toolbar fills).
//
// FIRST-CLASS GENUI: a genui is grafted straight into the app inside an open
// shadow root — NOT a sandboxed iframe — so it has real-page powers (live
// webcam/mic, direct calls to the agent). The old postMessage bridge is replaced
// by an `api` toolbox we hand each genui's mount function. A genui runs with
// the VIEWER's OWN app trust (it is per-user — you only ever see your own
// genui), so who may use Gen UI follows the Gen UI page's VISIBILITY setting,
// enforced server-side (see _fetchGenuiGate → /ui-config genui_first_class):
// "all" = everyone incl. anonymous, "auth" = signed-in registered users (the
// DEFAULT — registration required, not admin-only), "off" = admins only. Admins
// and "open" single-user mode always pass. The genui HTTP endpoints re-check the
// same gate + per-user ownership, so a hidden tab can't be bypassed via the API.
// Credentials still go STRAIGHT to the encrypted vault and never through the agent.

import { app } from '../../../shared/js/state.js';
import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { initGenuiToolbar } from './genui-toolbar.js';

// ── Module state ───────────────────────────────────────────────────────────────

let genuiActive = false;

// Currently displayed page: { slug, title, agent_context, agent_id, url }
// (agent_id = the agent that created/manages this genui; "" until an agent has
// rendered into it — the footer then falls back to the default WebAgent.)
let currentPage = null;

// All loaded pages for the current user
let pages = [];

// The live mounted genui, replacing the old sandboxed-iframe + postMessage
// bridge: its shadow host element, the shadow root, an optional cleanup() the
// genui returned (which MUST stop any camera tracks), and the subscriber lists
// wired through the agent toolbox (`api`) we hand the genui.
let _liveGenui = null;   // { host, mountEl, shadow, cleanup, themeCbs, statusCbs, slug, _emitStatus }

// Set just before a genui's <script>s run so WebagentGenui.register(fn) can
// hand (root, api) to the genui. Cleared on teardown.
let _pendingMountCtx = null;

// Local-only safety gate: null = not yet known, true/false once /ui-config
// answers. First-class rendering only runs when true; otherwise the genui is
// disabled (fail-closed). See _fetchGenuiGate.
let _genuiFirstClass = null;

// The footer toolbar handle (js/genui-toolbar.js). We call _toolbar.syncPages()
// to re-render the page selector whenever page state changes.
let _toolbar = { syncPages() {} };

// Keep the page selector in sync with current state. Thin wrapper so callers
// don't reach through _toolbar directly.
function _syncToolbar() { _toolbar.syncPages(); }

// ── Init & lifecycle ──────────────────────────────────────────────────────────

// Boots the core: registers the live page/visualizer WebSocket handler, mounts
// the swappable footer toolbar (handing it the ctx bridge below), and wires the
// new-page dialog (the toolbar's + button opens it via ctx.newPage).
export function initGenui() {
  app._genuiHandler = handleEvent;

  // The bridge the toolbar talks to. Everything that mutates page state stays
  // here in the core; the toolbar only reads, triggers, and renders.
  _toolbar = initGenuiToolbar({
    getPages: () => pages,
    getCurrentPage: () => currentPage,
    selectPage: (slug) => switchToPage(slug),
    newPage: () => showNewPageDialog(),
    refresh: () => _refreshGenui(),
    renamePage: (slug, title) => _renamePage(slug, title),
    deletePage: (slug) => _deletePage(slug),
    buildTaggedPrompt: (text) => buildTaggedGenuiPrompt(text),
    updateStatus: (text, type) => updateStatus(text, type),
  });

  // New-page dialog confirm/cancel (the dialog itself stays in the core).
  const dialogConfirm = document.getElementById('genui-dialog-confirm');
  const dialogCancel  = document.getElementById('genui-dialog-cancel');
  const dialogInput   = document.getElementById('genui-dialog-input');
  if (dialogConfirm) dialogConfirm.addEventListener('click', () => submitNewPage());
  if (dialogCancel)  dialogCancel.addEventListener('click', () => hideNewPageDialog());
  if (dialogInput)   dialogInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitNewPage();
    if (e.key === 'Escape') hideNewPageDialog();
  });

  // Persist the genui scroll position as the user scrolls (throttled). The host
  // element is stable across page swaps — only its shadow child is replaced — so
  // this one listener covers every page. See _scheduleSaveView / _saveLiveView.
  const host = document.getElementById('genui-host');
  if (host) host.addEventListener('scroll', () => _scheduleSaveView(), { passive: true });

  // A refresh / tab close should keep the latest state even if the throttle
  // hasn't fired yet — flush view state AND any buffered genui logs on the way out
  // (keepalive POST), so a page-hide doesn't lose the last burst of console output.
  window.addEventListener('pagehide', () => {
    _saveLiveView();
    if (_liveGenui) { try { _flushGenuiLogs(_liveGenui); } catch (_) {} }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      _saveLiveView();
      if (_liveGenui) { try { _flushGenuiLogs(_liveGenui); } catch (_) {} }
    }
  });
}

// ── Refresh (reloads ONLY this genui page) ──────────────────────────────────

// Re-fetch and remount the current page's genui — not the whole app. showGenui
// already cache-busts the request, so this picks up the latest rendered HTML.
function _refreshGenui() {
  if (currentPage && currentPage.url) {
    updateStatus('Refreshing page...');
    showGenui(currentPage.url, currentPage.title);
  }
}

// ── Page rename / delete (state owners — the toolbar calls these via ctx) ─────

// PATCH a page's title, update local state, and report success so the toolbar
// can re-render. Re-rendering itself is the toolbar's job (it calls syncPages).
async function _renamePage(slug, newTitle) {
  if (!app.currentUserId) return false;
  try {
    const res = await fetch(
      apiPath('/api/v1/genui/' + encodeURIComponent(slug) + '?user_id=' + encodeURIComponent(app.currentUserId)),
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
      apiPath('/api/v1/genui/' + encodeURIComponent(slug) + '?user_id=' + encodeURIComponent(app.currentUserId)),
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

export function startGenui() {
  genuiActive = true;
  // Show the spinner immediately so the viewport isn't blank while the gate
  // check + page-list fetch (a multi-step chain) are in flight.
  showLoading();
  // Confirm the local-only gate before rendering any agent-authored genui.
  _fetchGenuiGate().then(() => loadPages());
}

export function stopGenui() {
  genuiActive = false;
  // Leaving the Gen UI tab: release the live genui (stops its camera tracks).
  _teardownLiveGenui();
}

export function genuiSessionChanged() {
  if (!genuiActive) return;
  // Reset view but keep the page list; the pages are per-user not per-session
  loadPages();
}

// ── Page loading ──────────────────────────────────────────────────────────────

async function loadPages() {
  const userId = app.currentUserId;
  if (!userId) { showPlaceholder(); return; }

  // Keep the spinner up while the page list loads (also covers session
  // switches, which re-enter here without going through startGenui).
  showLoading();
  try {
    const res  = await fetch(apiPath(`/api/v1/genui?user_id=${encodeURIComponent(userId)}`), { headers: authHeaders() });
    const data = await res.json();
    pages = data.genui || [];

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
      showGenui(target.url, target.title);
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
  showGenui(page.url, page.title);
  _syncToolbar();
  updateStatus('');
}

// ── Sending prompts ───────────────────────────────────────────────────────────

// Build the genui-tagged prompt for a piece of user text. Shared by the toolbar
// chat pill (via ctx.buildTaggedPrompt) AND the in-genui action bridge so both
// tag identically.
// ═══════════════════════════════════════════════════════════════════════
// PROMPT / PRETEXT NOTE:
// The message sent to the agent is built from
// app/defaults/app-prompts.json → ui_handoffs.genui_handoff.
// To change the tag format, edit that JSON file — NOT the fallback below.
// ═══════════════════════════════════════════════════════════════════════
async function buildTaggedGenuiPrompt(text) {
  const pageSlug = currentPage ? currentPage.slug : 'home';
  const agentCtx = currentPage ? (currentPage.agent_context || '') : '';
  try {
    const resp = await fetch(apiPath('/api/v1/app-prompts'));
    if (resp.ok) {
      const data = await resp.json();
      const cfg = (data.ui_handoffs || {}).genui_handoff || {};
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
    ? `[User → UI Agent → Gen UI: "${pageSlug}" | Context: "${agentCtx}"]: ${text}`
    : `[User → UI Agent → Gen UI: "${pageSlug}"]: ${text}`;
}

// Hand a tagged prompt off to the shared WebAgent on the main chat.
// opts.continueIfActive: when the chat is ALREADY on the WebAgent with a live
// session, keep that session (so an interactive genui's messages stay one
// conversation — login → search → refresh) instead of starting fresh. The
// Gen UI chat pill omits it and keeps its "fresh session per prompt" behavior.
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

// ── Gen UI visibility gate ───────────────────────────────────────────────────
// First-class genui run agent-authored code with the caller's OWN app trust,
// so who may use Gen UI follows its page VISIBILITY (registration required by
// default; "all" includes anonymous; "off" is admins-only; admins + "open" mode
// always pass). The server resolves this per-caller and reports it as
// `genui_first_class` on /ui-config. Fail closed: if we can't confirm, disable.
async function _fetchGenuiGate() {
  if (_genuiFirstClass !== null) return _genuiFirstClass;
  try {
    // Send the caller's auth token: the gate is identity-based (it depends on who
    // you are and the Gen UI page's visibility), so the server needs to see who
    // we are. Without this header it would treat us as anonymous and likely disable.
    const res = await fetch(apiPath('/api/v1/auth/ui-config'), { headers: authHeaders() });
    const data = await res.json();
    _genuiFirstClass = !!data.genui_first_class;
  } catch (_) {
    _genuiFirstClass = false;
  }
  return _genuiFirstClass;
}

// ── How a genui mounts: register() OR drop-in (no registration) ────────────
// A genui runs inside its OWN shadow root, so its code needs a handle to that
// root — its normal document.* would hit the whole app, not the genui. The app
// hands that over by ONE of three equivalent forms (taught in the Visualizer
// skill); a genui author picks whichever they like:
//
//   1. register()  — WebagentGenui.register(function (root, api) { …; return cleanup }).
//      Explicit, and the only form that returns a cleanup() directly.
//   2. drop-in fn  — just define a top-level  function mount(root, api) { … }
//      (no register, no wrapper). The app auto-calls it after the page's scripts
//      run. Hand back a teardown with WebagentGenui.onCleanup(fn) if needed.
//   3. drop-in inline — use WebagentGenui.root / .api / .getData() directly in a
//      plain inline <script>; they're placed before the page's code runs.
//
//   • root — the genui's shadow root; query ITS OWN dom via root.* (root.getElementById
//     / root.querySelector), never document.* and never window-level key/pointer
//     listeners (those would hit the whole app).
//   • api  — the toolbox below: theme + status subscriptions, chat/action/refresh to
//     talk to the agent, getData() for the page's baked-in data, storeCredential → vault,
//     callWithKey(keyId, opts) → call a service using a vault secret attached server-side.
//   • cleanup — optional; the app calls it on teardown. It SHOULD stop any getUserMedia
//     tracks so the camera light goes off (the app also force-stops any <video>/<audio>
//     streams in _teardownLiveGenui as a safety net, so a drop-in page is covered too).
window.WebagentGenui = window.WebagentGenui || {
  // The current mount's root + toolbox — placed just before the page's scripts run
  // (and cleared on teardown), so a drop-in page can use them with no handshake.
  root: null,
  api: null,
  getData() { return _readGenuiData(); },
  register(mountFn) { _runGenuiMount(mountFn); },
  // Let a drop-in page (one that never called register) still hand back a teardown.
  onCleanup(fn) {
    const ctx = _pendingMountCtx;
    if (ctx && typeof fn === 'function') ctx.live.cleanup = fn;
  },
};

// ── Back-compat alias (Canvas → Gen UI rename) ─────────────────────────────────
// Pages authored under the old name call `window.WebagentCanvas.register(...)`,
// so keep the legacy global pointing at the SAME object. Stored genui bodies thus
// keep working unchanged. Safe to drop once no stored page uses the old name.
window.WebagentCanvas = window.WebagentGenui;

// Run a genui's mount function against the pending mount context, marking the
// page as mounted (so the drop-in fallback below can't double-fire) and keeping
// any cleanup() it returns. Shared by register() and the top-level-mount fallback.
function _runGenuiMount(mountFn) {
  const ctx = _pendingMountCtx;
  if (!ctx || typeof mountFn !== 'function') return false;
  ctx.live.mounted = true;  // it's the chosen entry point — don't also try the fallback
  try {
    const cleanup = mountFn(ctx.root, ctx.api);
    if (typeof cleanup === 'function') ctx.live.cleanup = cleanup;
    return true;
  } catch (e) {
    // eslint-disable-next-line no-console
    console.error('[genui] mount error:', e);
    // Also record it on the genui's own log so get_genui_logs surfaces mount
    // failures (register()/drop-in mount throws don't go through the scoped console).
    try {
      if (ctx.live && ctx.live.logs) {
        ctx.live.logs.push({
          ts: Date.now(), level: 'error',
          text: 'mount error: ' + ((e && e.message) || e),
          stack: (e && e.stack) || undefined,
        });
        _scheduleGenuiLogFlush(ctx.live);
      }
    } catch (_) {}
    updateStatus('Gen UI script error: ' + (e && e.message || e), 'error');
    return false;
  }
}

// Read the genui's DATA bag — the content the server baked into the page as
// window.__GENUI_DATA (from the genui's data.json). Lets a page read its
// records via api.getData() instead of hardcoding them into the markup, so the
// agent updates data without rewriting the page. Always returns an object;
// {} when the genui has no data file. (Reset to null per mount below so one
// genui's data can never leak into the next.)
function _readGenuiData() {
  try {
    const d = window.__GENUI_DATA || window.__CANVAS_DATA;  // legacy name back-compat
    return (d && typeof d === 'object') ? d : {};
  } catch (_) { return {}; }
}

// Build the toolbox handed to a genui's mount(root, api).
function _buildGenuiApi(live) {
  const emitStatus = (state, extra) => {
    for (const cb of live.statusCbs) {
      try { cb(Object.assign({ state }, extra || {})); } catch (_) {}
    }
  };
  live._emitStatus = emitStatus;

  const sendAction = async (verb, text) => {
    if (!app.currentUserId) { updateStatus('Sign in to use this genui', 'error'); return; }
    let t = (text == null ? '' : String(text)).trim();
    if (!t) {
      if (verb === 'refresh') t = 'Refresh this genui with my latest data.';
      else return;
    }
    // Only a bounded plain-text instruction reaches the agent — never a secret
    // (those use storeCredential → the vault, never the agent's context).
    if (t.length > 4000) t = t.slice(0, 4000);
    emitStatus('working');
    updateStatus('WebAgent is on the chat →');
    try {
      const tagged = await buildTaggedGenuiPrompt(t);
      // Keep one conversation across the dashboard's actions (login → search → reply).
      await handoffToWebagent(tagged, { continueIfActive: true });
    } catch (err) {
      updateStatus('Could not reach WebAgent: ' + (err && err.message || err), 'error');
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
    getData: () => _readGenuiData(),
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
    const resp = await fetch(apiPath('/api/v1/genui/vault/proxy'), {
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

// ── Per-genui console capture (GENUI-CONSOLE-LOG) ─────────────────────────
// A genui's scripts run in the page's GLOBAL scope, so their console.* calls would
// be indistinguishable from the app's own. We give each live genui its OWN console
// object and inject it as the `console` parameter of every inline classic script's
// IIFE wrapper (see the script loop — GENUI-SCRIPT-SCOPE). Lexical scoping means ALL
// code inside that script — including event handlers, intervals and promise callbacks
// it defines later — resolves `console` to this object, so we capture exactly that one
// genui's output and none of the app's. Each call is still forwarded to the REAL
// console (the dev panel keeps showing it), buffered on the live genui, and flushed in
// small batches to that genui's page-scoped log file on the server
// (POST …/{slug}/logs) — where the design agent reads it back with get_genui_logs,
// without any logs.db / codebase-admin access. (Module and external `src` scripts keep
// their own scope and are NOT instrumented — agent genui code is inline classic.)
const _GENUI_LOG_LEVELS = ['log', 'info', 'warn', 'error', 'debug'];
const _GENUI_LOG_BUFFER_MAX = 300;   // cap the in-browser buffer between flushes
const _GENUI_LOG_FLUSH_MS = 1500;    // debounce: collect a burst, then POST once

function _stringifyLogArg(a) {
  if (typeof a === 'string') return a;
  if (a === null) return 'null';
  if (a === undefined) return 'undefined';
  if (a instanceof Error) return (a.stack || a.message || String(a));
  try { return JSON.stringify(a); } catch (_) { return String(a); }
}

// A console bound to ONE live genui: forwards to the real console and records into
// live.logs. Built on Object.create(realConsole) so every method we don't override
// (table, dir, group, assert…) still delegates to the real console unchanged.
function _makeGenuiConsole(live) {
  const real = window.console || {};
  const scoped = Object.create(real);
  const record = (level, args) => {
    try {
      const text = Array.prototype.map.call(args, _stringifyLogArg).join(' ');
      let stack = '';
      for (const a of args) { if (a instanceof Error && a.stack) { stack = a.stack; break; } }
      live.logs.push({ ts: Date.now(), level, text: text.slice(0, 4000), stack: stack || undefined });
      if (live.logs.length > _GENUI_LOG_BUFFER_MAX) live.logs.shift();
      _scheduleGenuiLogFlush(live);
    } catch (_) {}
  };
  _GENUI_LOG_LEVELS.forEach((level) => {
    const fn = (typeof real[level] === 'function') ? real[level] : (real.log || function () {});
    scoped[level] = function () {
      try { fn.apply(real, arguments); } catch (_) {}
      record(level, arguments);
    };
  });
  return scoped;
}

// Shadow-scoped `document` for a genui's inline scripts (GENUI-SCOPED-DOCUMENT).
// The body markup lives inside THIS genui's shadow root, but classic inline
// scripts run in the page's GLOBAL scope — so a naive `document.getElementById(id)`
// (or querySelector / getElementsBy*) queries the MAIN page, never the shadow, and
// returns null. Agent genui overwhelmingly write
// `document.getElementById('x').addEventListener(...)`, which then throws
// "Cannot read properties of null (reading 'addEventListener')" and aborts mount.
// We hand each inline script a Proxy over the real document whose element-lookup
// methods search this shadow root FIRST and fall back to the real document only
// when the shadow has nothing (so a genui that legitimately reaches into the app
// DOM still works). Everything else — createElement, body, addEventListener,
// title, cookie, … — delegates straight to the real document unchanged. We also
// reroute a `DOMContentLoaded` listener to fire immediately: by the time a genui
// mounts the page loaded long ago, so the event already fired and the handler
// (a near-universal "wait until the DOM is ready, then init" wrapper) would never
// run otherwise.
function _makeGenuiDocument(shadowRoot) {
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
    // drop-in for the live HTMLCollection in every realistic genui use).
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
      // The genui DOM is fully laid out before its scripts run, so a "ready"
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
function _scheduleGenuiLogFlush(live) {
  if (!live || live._logFlushT) return;
  live._logFlushT = setTimeout(() => { live._logFlushT = null; _flushGenuiLogs(live); }, _GENUI_LOG_FLUSH_MS);
}

// Ship the buffered entries to this genui's page-scoped log file. keepalive so it
// still goes out during teardown / page-hide. Best-effort: logs are a debugging
// side-channel — a failed POST just drops them, never disrupts the genui.
function _flushGenuiLogs(live) {
  if (!live || !live.logs || !live.logs.length) return;
  const slug = live.slug;
  const userId = app.currentUserId;
  if (!userId || !slug) { live.logs.length = 0; return; }
  const batch = live.logs.splice(0, live.logs.length);
  try {
    fetch(
      apiPath('/api/v1/genui/' + encodeURIComponent(userId) + '/' + encodeURIComponent(slug) + '/logs'),
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
// genui scroll position, and a best-effort snapshot of the agent-page's own
// "panel" state (open <details>, aria-expanded toggles, checkbox/radio/select).
// Everything is keyed by user + page slug and lives ONLY in the browser's
// localStorage. SECRETS NEVER PERSIST: we deliberately skip text/password/email
// inputs (credential fields go straight to the vault, never to disk here).
const _LS_LAST = 'webagent.genui.lastPage.v1';   // { [userId]: slug }
const _LS_VIEW = 'webagent.genui.viewState.v1';  // { 'userId::slug': {st,sl,inner} }

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

// Throttled save of the live genui's current view state.
let _saveTimer = null;
function _scheduleSaveView() {
  if (_saveTimer) return;
  _saveTimer = setTimeout(() => { _saveTimer = null; _saveLiveView(); }, 400);
}
function _saveLiveView() {
  const live = _liveGenui;
  if (!live || !live.shadow || !live.slug) return;
  const host = document.getElementById('genui-host');
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
// target is reached and bail if the genui was torn down or the user scrolled.
function _scheduleRestoreView(slug, shadow) {
  const state = _savedView(slug);
  if (!state) return;
  _restoreInnerStruct(shadow, state.inner);
  const host = document.getElementById('genui-host');
  const want = state.st || 0;
  [0, 60, 150, 320, 650, 1100].forEach((delay) => {
    setTimeout(() => {
      // Bail if the genui was torn down/swapped, or the target is already met
      // (so a late retry can't yank the user back after they've scrolled away).
      if (!_liveGenui || _liveGenui.shadow !== shadow) return;
      if (host && want && Math.abs(host.scrollTop - want) > 2) {
        if (state.sl) host.scrollLeft = state.sl;
        host.scrollTop = want;
      }
      _restoreInnerScroll(shadow, state.inner);
    }, delay);
  });
}

// ── First-class genui rendering (shadow DOM) ───────────────────────────────

// Base style injected into every genui's shadow root BEFORE the agent's own
// styles (so the agent's CSS wins). A box reset, host sizing, a TRANSPARENT
// default background (so the app's own animated background shows through unless
// the agent paints one), and a slim themed scrollbar.
const _GENUI_BASE_STYLE =
  // Custom properties inherit through the shadow boundary, so the app's themed
  // --font-sans / --fg-1 / --brand-rgb resolve here and track the live theme.
  ':host{display:block;width:100%;min-height:100%;background:transparent;' +
  "font-family:var(--font-sans,'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif);" +
  'color:var(--fg-1);-webkit-font-smoothing:antialiased;}' +
  '*,*::before,*::after{box-sizing:border-box;}' +
  // The app's own body wrapper uses a PRIVATE class so an agent that styles its
  // OWN `.genui-root` (a name they reach for) can't collide with ours and double
  // its grid/padding — the old half-width / fat-margin bug. (grep: WA-GENUI-BODY)
  '.wa-genui-body{min-height:100%;}' +
  '*{scrollbar-width:thin;scrollbar-color:rgba(var(--brand-rgb),0.32) transparent;}' +
  '*::-webkit-scrollbar{width:6px;height:6px;}' +
  '*::-webkit-scrollbar-track{background:transparent;}' +
  '*::-webkit-scrollbar-thumb{background:rgba(var(--brand-rgb),0.38);border-radius:999px;}' +
  '*::-webkit-scrollbar-thumb:hover{background:rgba(var(--brand-rgb),0.62);}';

// Toggle the theme class on the shadow host so the agent's :host(.light) /
// :host(.dark) selectors respond.
function _applyGenuiTheme(mountEl, theme) {
  if (!mountEl) return;
  mountEl.classList.toggle('light', theme === 'light');
  mountEl.classList.toggle('dark', theme !== 'light');
}

// Tear the live genui down: let it release its own resources (camera!),
// defensively stop any lingering media so the webcam light goes off, then empty
// the host. Always called before mounting a new genui or leaving the tab.
function _teardownLiveGenui() {
  const live = _liveGenui;
  // Capture the outgoing genui's view state before we drop it, so switching
  // away and back (or a refresh mid-session) restores scroll + open panels.
  if (live) {
    try { _saveLiveView(); } catch (_) {}
    // Flush any console output this genui buffered before we let it go.
    try {
      if (live._logFlushT) { clearTimeout(live._logFlushT); live._logFlushT = null; }
      _flushGenuiLogs(live);
    } catch (_) {}
  }
  _liveGenui = null;
  _pendingMountCtx = null;
  // Drop the drop-in globals so the outgoing genui's root/api can't leak into
  // the next mount (or into app code between genui).
  try { window.WebagentGenui.root = null; window.WebagentGenui.api = null; } catch (_) {}
  try { window.mount = undefined; } catch (_) {}
  try { window.__genuiConsole = null; } catch (_) {}
  try { window.__genuiDocument = null; } catch (_) {}
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

// Graft an agent-authored genui document into the app inside a fresh shadow
// root, then run its scripts so register(fn) hands (shadow, api) to the genui.
function mountGenui(html, title) {
  const host = document.getElementById('genui-host');
  if (!host) return;
  _teardownLiveGenui();

  // Fresh shadow each mount: attachShadow throws if one already exists, so we
  // host it on a new child we can replace wholesale.
  host.innerHTML = '';
  const mountEl = document.createElement('div');
  mountEl.className = 'genui-mount';
  mountEl.style.cssText = 'display:block;width:100%;min-height:100%;';
  host.appendChild(mountEl);
  const shadow = mountEl.attachShadow({ mode: 'open' });

  const live = {
    host, mountEl, shadow, cleanup: null,
    themeCbs: [], statusCbs: [],
    mounted: false,  // set true once register() or the drop-in fallback mounts the page
    slug: currentPage ? currentPage.slug : 'home',
    logs: [],        // this genui's captured console output, flushed to its log file
  };
  const api = _buildGenuiApi(live);

  // Remember this as the page to reopen after a refresh, and persist the page's
  // own panel state (toggles, sub-scrolls) as the user interacts with it.
  _rememberLastSlug(live.slug);
  shadow.addEventListener('scroll', () => _scheduleSaveView(), { capture: true, passive: true });
  shadow.addEventListener('change', () => _scheduleSaveView(), true);
  shadow.addEventListener('toggle', () => _scheduleSaveView(), true);
  shadow.addEventListener('click',  () => _scheduleSaveView(), true);

  const base = document.createElement('style');
  base.textContent = _GENUI_BASE_STYLE;
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

    // Body markup → our private .wa-genui-body wrapper (scripts parked inert by
    // innerHTML are stripped; we re-create them below so they actually execute).
    // Private class name so an agent's own `.genui-root` can't collide. (WA-GENUI-BODY)
    const root = document.createElement('div');
    root.className = 'wa-genui-body';
    root.innerHTML = doc.body ? doc.body.innerHTML : (html || '');
    root.querySelectorAll('script').forEach((s) => s.remove());
    shadow.appendChild(root);

    _applyGenuiTheme(mountEl, currentTheme());

    // Prime the handshake, then run the scripts. Inline scripts execute
    // synchronously on append; register(fn) fires during that and calls
    // fn(shadow, api). Kept referenced via _liveGenui for late (CDN) registers.
    _liveGenui = live;
    _pendingMountCtx = { root: shadow, api, live };
    // Clear any previous genui's baked data before running this one's scripts.
    // The page's injected <head> data block (window.__GENUI_DATA=…) runs first
    // in the loop below and repopulates it; a genui with no data file keeps {}.
    try { window.__GENUI_DATA = null; window.__CANVAS_DATA = null; } catch (_) {}
    // Place the mount context as ready globals so a DROP-IN genui can use
    // WebagentGenui.root / .api / .getData() with no register() handshake, and
    // clear any previous page's top-level mount() so a stale one can't fire below.
    try { window.WebagentGenui.root = shadow; window.WebagentGenui.api = api; } catch (_) {}
    try { window.mount = undefined; } catch (_) {}
    // Expose this genui's console so each inline script's IIFE wrapper picks it up
    // as its `console` parameter (GENUI-CONSOLE-LOG) — capturing the page's output
    // into live.logs for the per-genui log file the agent reads with get_genui_logs.
    try { window.__genuiConsole = _makeGenuiConsole(live); } catch (_) {}
    // Shadow-scoped `document` so each inline script's `document.getElementById`/
    // querySelector resolves inside THIS genui's shadow root (GENUI-SCOPED-DOCUMENT),
    // picked up as the script IIFE's `document` parameter below.
    try { window.__genuiDocument = _makeGenuiDocument(shadow); } catch (_) {}
    doc.querySelectorAll('script').forEach((os) => {
      const s = document.createElement('script');
      for (const a of os.attributes) s.setAttribute(a.name, a.value);
      // Inline classic scripts run in the page's GLOBAL lexical scope, so a
      // top-level `const ICON = …` (or let/class/function) becomes a realm-wide
      // binding that PERSISTS after the script element is removed. Re-mounting
      // the same genui (switching pages back), or a genui with two such blocks,
      // then re-declares it and throws "Identifier 'ICON' has already been
      // declared" — which aborts the rest of that script, so the genui's mount
      // never runs (cascading into null-element errors). Wrap each inline classic
      // script in a private IIFE so its top-level declarations are scoped to one
      // execution and can't collide. register()/WebagentGenui.* still work (they
      // are globals), and a drop-in top-level `mount(root, api)` is re-exposed on
      // window so the fallback below still finds it. External (src) and module
      // scripts already have their own scope, so they are appended untouched.
      // (GENUI-SCRIPT-SCOPE)
      const type = (os.getAttribute('type') || '').toLowerCase();
      const isInlineClassic = !os.hasAttribute('src') && type !== 'module'
        && (type === '' || type === 'text/javascript' || type === 'application/javascript')
        && os.textContent.trim() !== '';
      if (isInlineClassic) {
        // The IIFE takes a `console` parameter bound to THIS genui (GENUI-CONSOLE-LOG):
        // the page's console.* is captured to its own log file, and a synchronous throw
        // is caught + recorded as an error (attributed to this genui) instead of just
        // aborting silently — the real console still sees it, so the dev panel/logs.db
        // are unaffected.
        // `document` is bound to this genui's shadow-scoped document (GENUI-SCOPED-DOCUMENT)
        // so getElementById/querySelector inside the script find the genui's own elements.
        s.textContent =
          ';(function(console, document){\ntry{\n' + os.textContent +
          '\n;try { if (typeof mount === "function") window.mount = mount; } catch (_e) {}\n' +
          '}catch(__cerr){ try { console.error("Uncaught script error:", (__cerr && __cerr.stack) || String(__cerr)); } catch (_e2) {} }\n' +
          '})(window.__genuiConsole || console, window.__genuiDocument || document);';
      } else {
        s.textContent = os.textContent;
      }
      shadow.appendChild(s);
    });
    // Drop-in fallback: a page that defined a top-level mount(root, api) but never
    // called register() is auto-mounted here. register() fired synchronously during
    // the loop above (setting live.mounted); a page that did its work inline against
    // WebagentGenui.root has also already run and defines no window.mount — so this
    // only ever catches the plain "function mount(root, api){…}" form.
    if (!live.mounted && typeof window.mount === 'function') {
      _runGenuiMount(window.mount);
    }
  } else {
    _liveGenui = live;
  }

  // Replay the saved scroll position + open-panel state for this page (retries
  // briefly while the agent's own scripts finish laying out the content).
  _scheduleRestoreView(live.slug, shadow);

  _setStage('page');
  updateStatus(title || 'Ready');
}

// Show the "Gen UI not enabled for you" notice in place of a genui (the
// server-side visibility gate refused this caller — see _fetchGenuiGate).
function _showGenuiDisabledNotice() {
  const host        = document.getElementById('genui-host');
  const placeholder = document.getElementById('genui-placeholder');
  const loading     = document.getElementById('genui-loading');
  _teardownLiveGenui();
  if (loading) loading.style.display = 'none';
  if (host)    host.style.display    = 'none';
  if (placeholder) {
    placeholder.style.display = 'flex';
    const text = placeholder.querySelector('.genui-placeholder-text');
    const hint = placeholder.querySelector('.genui-placeholder-hint');
    if (text) text.textContent = 'Gen UI isn’t enabled for your account';
    if (hint) hint.textContent = 'Gen UI run real code with your own app access, so who can use Gen UI follows its page visibility (an admin sets this in App Settings — sign-in is required by default).';
  }
  updateStatus('Gen UI disabled for this account', 'error');
}

// ── WebSocket event handler ───────────────────────────────────────────────────

function handleEvent(event) {
  if (!genuiActive) return;

  const isToolEvent = event.type === 'tool_result' || event.type === 'tool_call';
  const toolName    = event.tool || event.tool_name || '';

  // render_visual (full rewrite) AND edit_genui (surgical edit) both save through
  // the same path and return {path, slug, title} — so both drive the live reload.
  if (isToolEvent && (toolName === 'render_visual' || toolName === 'edit_genui')) {
    if (event.type === 'tool_call') {
      showLoading();
      updateStatus(toolName === 'edit_genui' ? 'Applying edit...' : 'Rendering page...');
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
            showGenui(result.path, result.title || currentPage.title);
          } else {
            // Refresh page list to pick up any new pages, then switch to the rendered one
            loadPages().then(() => {
              const p = pages.find(q => q.slug === renderedSlug);
              if (p) {
                currentPage = p;
                showGenui(p.url, p.title);
                _syncToolbar();
              }
            });
          }
        }
      } catch (_) { /* ignore parse errors */ }
    }
  }

  // Handle create_genui tool results — refresh genui list
  if (isToolEvent && toolName === 'create_genui' && event.type === 'tool_result') {
    try {
      const result = typeof event.result === 'string'
        ? JSON.parse(event.result)
        : event.result;
      if (result && result.status === 'ok' && result.genui) {
        loadPages().then(() => {
          const newSlug = result.genui.slug;
          const p = pages.find(q => q.slug === newSlug);
          if (p) {
            currentPage = p;
            showGenui(p.url, p.title);
            _syncToolbar();
          }
        });
      }
    } catch (_) { /* ignore */ }
  }

  // Handle delete_genui tool results — refresh genui list
  if (isToolEvent && toolName === 'delete_genui' && event.type === 'tool_result') {
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
  const dialog = document.getElementById('genui-new-page-dialog');
  const input  = document.getElementById('genui-dialog-input');
  if (!dialog) return;
  dialog.style.display = 'flex';
  if (input) { input.value = ''; input.focus(); }
}

function hideNewPageDialog() {
  const dialog = document.getElementById('genui-new-page-dialog');
  if (dialog) dialog.style.display = 'none';
}

async function submitNewPage() {
  const input = document.getElementById('genui-dialog-input');
  if (!input) return;
  const title = input.value.trim();
  if (!title) return;

  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'page';
  hideNewPageDialog();

  try {
    const res = await fetch(apiPath('/api/v1/genui'), {
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
      const p = pages.find(q => q.slug === data.genui.slug);
      if (p) {
        currentPage = p;
        showGenui(p.url, p.title);
        _syncToolbar();
      }
    } else {
      updateStatus('Could not create page: ' + (data.detail || data.message || 'error'), 'error');
    }
  } catch (e) {
    updateStatus('Failed to create page: ' + e.message, 'error');
  }
}

// ── Gen UI / display helpers ──────────────────────────────────────────────────

// Load a genui by its html-endpoint URL and graft it into the app. Gated to
// local mode (fail-closed). `url` is the page's `.url` (or a render_visual
// `.path`); both point at /api/v1/genui/{user}/{slug}/html.
async function showGenui(url, title) {
  const host = document.getElementById('genui-host');

  if (_genuiFirstClass === false) { _showGenuiDisabledNotice(); return; }

  if (!host || !url) { showPlaceholder(); return; }
  // Show the spinner while the page HTML is fetched — otherwise the viewport
  // goes blank between hiding the placeholder and mountGenui() painting the
  // page. mountGenui() flips the stage to 'page' once the content is mounted.
  showLoading();

  try {
    const bust = url + (url.includes('?') ? '&' : '?') + '_t=' + Date.now();
    const res  = await fetch(bust, { headers: authHeaders() });
    if (!res.ok) { showError('Gen UI not found'); return; }
    const html = await res.text();
    mountGenui(html, title);
  } catch (e) {
    showError('Failed to load genui: ' + (e && e.message || e));
  }
}

function currentTheme() {
  return document.body.classList.contains('light-mode') ? 'light' : 'dark';
}

// Forward app theme changes to the live genui whenever body.class toggles:
// flip the shadow host's theme class and notify the genui's onTheme subscribers.
(function watchTheme() {
  if (typeof MutationObserver === 'undefined') return;
  const obs = new MutationObserver(() => {
    const live = _liveGenui;
    if (!live) return;
    const theme = currentTheme();
    _applyGenuiTheme(live.mountEl, theme);
    for (const cb of live.themeCbs) { try { cb(theme); } catch (_) {} }
  });
  obs.observe(document.body, { attributes: true, attributeFilter: ['class'] });
})();

// The stage shows exactly ONE of three things: a spinner (loading), the
// "nothing here yet" placeholder (empty), or the live genui host (page).
// _setStage is the single display switch the show* helpers below route through
// (mirrors browser.js _setStage).
function _setStage(state) {  // 'loading' | 'empty' | 'page'
  const host        = document.getElementById('genui-host');
  const placeholder = document.getElementById('genui-placeholder');
  const loading     = document.getElementById('genui-loading');
  if (host)        host.style.display        = state === 'page'    ? 'block' : 'none';
  if (placeholder) placeholder.style.display = state === 'empty'   ? 'flex'  : 'none';
  if (loading)     loading.style.display     = state === 'loading' ? 'flex'  : 'none';
}

function showLoading() {
  _setStage('loading');
}

function showPlaceholder() {
  _teardownLiveGenui();
  _setStage('empty');
  updateStatus('');
}

function showError(message) {
  showPlaceholder();
  updateStatus('⚠ ' + message, 'error');
}

function updateStatus(text, type) {
  const status = document.getElementById('genui-status');
  if (!status) return;
  status.textContent = text || '';
  status.className   = 'genui-status';
  if (type === 'error') status.classList.add('genui-status-error');
}
