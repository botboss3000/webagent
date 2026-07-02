'use strict';

// tabs.js imports NOTHING from a drop-in page folder (ui/<page>/) — every page's
// start/stop is driven dynamically from its catalog descriptor (see below), so
// deleting a page folder can never break this module's load. The only static
// imports are app-shell INFRA that is never a removable page: the pinned Account
// view, the tutorial-hint refresher, and the Admin Tools sub-panel inits (Admin
// Tools is the locked shell tab).
import { startAccount } from './account.js';
import { refreshTutorial } from '../../tutorials/js/tutorial.js';
import { isAdmin } from './left-login.js';
import { initAppConfig } from '../../admin-tools/app-config/index.js';
import { initDbViewer } from './index.js';
import { initDataManagement } from './data-management.js';
import { initRemoteAccess } from './remote-access.js';
import { initTunnelLink } from './tunnel-link.js';
import { showPageAccessGate, hidePageAccessGate } from './page-access-gate.js';

// ── Lazy Admin Tools init ───────────────────────────────────────────────────
// The Admin Tools sub-panels (App Config, Database viewer, Data Management,
// Remote Access) are NOT built at boot anymore — they're wired the first time
// the user actually opens the Admin Tools tab. This keeps startup light,
// especially on mobile where only one panel is ever on screen. All four inits
// are null-safe + idempotent; initFiles() is run by startAdminTools() itself.
let _adminInited = false;
function _ensureAdminInit() {
  if (_adminInited) return;
  _adminInited = true;
  try { initDbViewer(); }       catch (e) { console.error('initDbViewer failed', e); }
  try { initDataManagement(); }  catch (e) { console.error('initDataManagement failed', e); }
  try { initRemoteAccess(); }    catch (e) { console.error('initRemoteAccess failed', e); }
  try { initTunnelLink(); }      catch (e) { console.error('initTunnelLink failed', e); }
  try { initAppConfig(); }       catch (e) { console.error('initAppConfig failed', e); }
}

// Admin Tools is the one admin-only destination (everything privileged lives
// inside it as sidebar views). A non-admin must never be ACTIVATED onto it — not
// an anonymous guest, and not a real admin in the brief boot window before the
// server confirms admin status — because activating it flashes the admin panels
// (App Settings etc.) and then full-screens the "Restricted Access" overlay.
function _isAdminOnlyTab(tabValue) {
  return tabValue === 'admin-tools';
}

// A main page hidden from this visitor by its 3-state visibility setting — a
// signed-out / anonymous guest on a 'registered-only' (auth) page, or anyone on
// an 'off' page. This is the SAME decision header-build uses to hide the tab
// button, so a hidden tab can no longer be silently reached through a ?tab= deep
// link, a middle-click, a restored lastActiveTab, the mobile dropdown, or an
// agent ui_command. When true, activateTab shows the inline sign-in gate IN the
// page area (page-access-gate.js) instead of mounting the page — it is never
// started, so its gated endpoints are never hit. (The server still enforces
// per-row data ownership independently; this is the UI-side access boundary.)
function _pageVisibilityBlocked(tabValue) {
  try {
    return typeof window.__pageHiddenByVisibility === 'function'
      && !!window.__pageHiddenByVisibility(tabValue);
  } catch (_) { return false; }
}

function setChatPanelVisible(visible) {
  const chatPanel = document.getElementById('chat-panel');
  const resizeHandle = document.getElementById('chat-resize-handle');
  if (chatPanel) chatPanel.style.display = visible ? '' : 'none';
  if (resizeHandle) resizeHandle.style.display = visible ? '' : 'none';
}

// ── Page lifecycle dispatch ──────────────────────────────────────────────────
// EVERY page — core or dropped-in — is driven the same way: a DYNAMIC import of
// the module + the export names declared in its page.json descriptor (served on
// window.__pagesCatalog via /api/v1/pages/catalog). So adding OR removing a page
// needs no edit here. The only non-catalog destinations are app-shell views that
// are not pages (the pinned "Manage Account" entry); they live in _SHELL_HOOKS.
const _SHELL_HOOKS = {
  'account': { start: startAccount },
};

let _activePageId = null;
const _dynMods = {};   // page id → Promise<module> (cached after first import)

function _catalogPage(id) {
  try {
    const c = window.__pagesCatalog;
    const list = (c && Array.isArray(c.main)) ? c.main : [];
    return list.find((p) => p.id === id) || null;
  } catch (_) { return null; }
}

function _dynModule(id, entry) {
  if (!_dynMods[id]) {
    try { _dynMods[id] = import(new URL(entry, document.baseURI).href); }
    catch (e) { _dynMods[id] = Promise.reject(e); }
  }
  return _dynMods[id];
}

function _startPage(id) {
  _activePageId = id;
  const h = _SHELL_HOOKS[id];
  if (h) {
    try { if (h.start) h.start(); } catch (e) { console.error('start ' + id + ' failed', e); }
    return;
  }
  const p = _catalogPage(id);
  if (p && p.entry && p.start) {
    _dynModule(id, p.entry)
      .then((m) => { const fn = m && m[p.start]; if (typeof fn === 'function') fn(); })
      .catch((e) => console.error('start ' + id + ' failed', e));
  }
}

function _stopPage(id) {
  const h = _SHELL_HOOKS[id];
  if (h) {
    if (h.stop) { try { h.stop(); } catch (_) {} }
    return;
  }
  const p = _catalogPage(id);
  if (p && p.entry && p.stop && _dynMods[id]) {
    _dynMods[id]
      .then((m) => { const fn = m && m[p.stop]; if (typeof fn === 'function') fn(); })
      .catch(() => {});
  }
}

// Only the previously-active page can be "running", so stopping it (when it
// isn't the new target) is sufficient and idempotent. Admin Tools' stop hook
// (stopAdminTools) owns its sidebar sub-views' lifecycle, so leaving Admin Tools
// tears them down here too.
function _stopAllExcept(target) {
  if (_activePageId && _activePageId !== target) {
    _stopPage(_activePageId);
  }
}

// ── Address-bar sync ─────────────────────────────────────────────────────────
// Each tab "owns" any deep-link sub-params it puts in the URL. When the user
// navigates to a DIFFERENT tab, those params must be dropped so a hotlink like
// ?tab=wiki&article=foo doesn't leave a stale &article= behind. (While a page is
// active it keeps its own param fresh — e.g. wiki.js mirrors the open article and
// files.js mirrors the Admin Tools sidebar view via history.replaceState.) Add a
// row here when a new page gains a hotlink sub-param.
const _PAGE_PARAMS = { wiki: ['article'], 'admin-tools': ['view'] };

// The app's canonical landing page — the first content tab a no-param load of
// "/app" opens: the lowest-ordered VISIBLE non-admin page (today that's Agents).
// Computed live from the DOM so it tracks the catalog AND per-visitor visibility
// (header-build hides 'auth'/'off' tabs with display:none); Admin Tools (admin-
// only) and the pinned Account entry are skipped. Returns '' when nothing is open
// to this visitor (e.g. every page registration-only for an anonymous guest).
// Shared by _safeDefaultTab (redirect target) and _syncTabUrl (clean-URL test).
function _computeDefaultTab() {
  const btns = Array.from(document.querySelectorAll('#main-tabs .main-tab[data-value]'));
  for (const b of btns) {
    const v = b.dataset.value;
    if (!v || v === 'admin-tools' || v === 'account') continue;
    if (b.style.display === 'none') continue;  // hidden by per-page visibility
    return v;
  }
  const tabSelect = document.getElementById('main-tab-select');
  const opts = tabSelect ? Array.from(tabSelect.options).map((o) => o.value) : [];
  return opts.find((v) => v && v !== 'admin-tools' && v !== 'account'
    && !(typeof window.__pageHiddenByVisibility === 'function'
         && window.__pageHiddenByVisibility(v))) || '';
}

// Mirror the active main tab into the address bar (?tab=<id>) without reloading,
// and strip every OTHER page's deep-link params. This is what makes a refresh
// re-open the page you're actually on instead of snapping back to whatever
// ?tab=/&article= you first arrived through. Best-effort — never throws.
//
// Two refinements keep the bar honest:
//  • CLEAN DEFAULT — when the target IS the canonical landing page, ?tab= adds
//    nothing (a bare /app opens there anyway), so we DROP the param (and that
//    page's own sub-params) rather than let a stale "?tab=agents" linger.
//  • OWN SUB-PARAMS PRESERVED — a non-default page keeps its own hotlink params
//    (wiki's &article=, Admin Tools' &view=); only sibling pages' params are
//    stripped. The page itself refreshes its sub-param via its own start hook /
//    window.__setMainSubView.
function _syncTabUrl(tabValue) {
  try {
    const url = new URL(location.href);
    const isDefault = tabValue === _computeDefaultTab();
    if (isDefault) url.searchParams.delete('tab');
    else url.searchParams.set('tab', tabValue);
    for (const owner in _PAGE_PARAMS) {
      // Keep the active non-default page's OWN params; strip everyone else's
      // (and, on the clean default URL, strip them all).
      if (owner === tabValue && !isDefault) continue;
      for (const p of _PAGE_PARAMS[owner]) url.searchParams.delete(p);
    }
    history.replaceState(history.state, '', url);
  } catch (_) { /* address-bar sync is best-effort */ }
}

export function initTabs() {
  const tabSelect = document.getElementById('main-tab-select');
  if (!tabSelect) return;

  const savedTab = localStorage.getItem('lastActiveTab');
  if (savedTab && tabSelect.querySelector(`option[value="${savedTab}"]`)) {
    tabSelect.value = savedTab;
  }

  // Where a non-admin lands when their saved/requested tab was Admin Tools — the
  // app's canonical landing page. The live DOM logic (first VISIBLE non-admin tab,
  // skipping Account; '' when every page is gated for this visitor) lives in the
  // shared module-level _computeDefaultTab so _syncTabUrl can reuse it for the
  // clean-URL test; this thin wrapper keeps the redirect call sites readable.
  function _safeDefaultTab() {
    return _computeDefaultTab();
  }

  function activateTab(tabValue, userInitiated) {
    // Back-compat: 'files' was the legacy id for what is now 'admin-tools'.
    // Saved state in older browsers will still hold 'files'.
    if (tabValue === 'files') tabValue = 'admin-tools';
    // Back-compat: 'database' was its own top-level tab; it now lives as a
    // sidebar view inside Admin Tools. Redirect and request that sub-view.
    if (tabValue === 'database') {
      tabValue = 'admin-tools';
      try { localStorage.setItem('files.sidebarView', 'database'); } catch (_) {}
    }
    // Back-compat: 'flow' and 'loop-visual' top-level tabs moved into Admin
    // Tools as the Interactions and Runtime Loop strip views.
    if (tabValue === 'stream' || tabValue === 'flow') {
      tabValue = 'admin-tools';
      try { localStorage.setItem('files.sidebarView', 'interactions'); } catch (_) {}
    }
    if (tabValue === 'loop-visual') {
      tabValue = 'admin-tools';
      try { localStorage.setItem('files.sidebarView', 'runtime-loop'); } catch (_) {}
    }

    // Admin Tools boot-race guard. Admin status isn't known yet at boot (the
    // /check-access probe is async), so a not-yet-confirmed admin is redirected
    // to a safe, visible page rather than flashing the admin panels. A real admin
    // is bounced back to Admin Tools by the admin-status-loaded handler once the
    // server confirms — see the deferred restore after the initial activate. (Its
    // own panel shows a Restricted treatment for confirmed non-admins, so Admin
    // Tools keeps the redirect rather than the inline sign-in gate below.)
    if (_isAdminOnlyTab(tabValue) && !isAdmin()) {
      tabValue = _safeDefaultTab();
      if (tabSelect.querySelector(`option[value="${tabValue}"]`)) tabSelect.value = tabValue;
    }

    // Visibility gate. A main page hidden from this visitor (anonymous guest on a
    // registered-only page, anyone on an 'off' page) is NOT redirected — instead
    // we leave the page unmounted and show the inline sign-in gate over the main
    // panel, so the visitor signs in right where they tried to go. Covers every
    // entry point (?tab= deep link, dropdown, restored lastActiveTab, agent
    // ui_command). Signing in reloads and re-runs this check.
    const gated = !!tabValue && _pageVisibilityBlocked(tabValue);

    document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
    if (!gated) {
      const targetContent = document.getElementById('tab-' + tabValue);
      if (targetContent) targetContent.classList.add('active');
    }

    if (tabValue) localStorage.setItem('lastActiveTab', tabValue);

    // Keep the address bar in step with the active tab so a refresh re-opens the
    // page you're actually on — not whatever ?tab=/&article= you arrived via.
    // Only on real navigation (dropdown change, tab click, agent ui_command);
    // the initial localStorage restore passes userInitiated=false so a plain
    // reload of "/" stays clean. For a gated page this keeps ?tab=<id> set so the
    // post-sign-in reload returns the visitor to the page they tried to open.
    if (userInitiated) _syncTabUrl(tabValue);

    // Mobile: explicit page pick from dropdown hides chat (reveals page / gate).
    // Initial load on mobile must respect saved chat-visibility — don't override.
    // Desktop: always reflect saved chat toggle preference.
    const isMobile = typeof window.__isMobileChatLayout === 'function'
      ? window.__isMobileChatLayout() : (window.innerWidth <= 800);
    if (isMobile) {
      if (userInitiated) {
        if (typeof window.__applyChatVisible === 'function') {
          window.__applyChatVisible(false);
        } else {
          setChatPanelVisible(false);
        }
      }
    } else {
      const stored = localStorage.getItem('chatPanelVisible');
      const visible = stored === null ? true : stored !== 'false';
      setChatPanelVisible(visible);
    }

    if (gated) {
      // Stop whatever page was running and show the sign-in gate instead of
      // starting the gated one — no gated endpoint is ever called for a visitor
      // who can't see the page.
      _stopAllExcept(null);
      _activePageId = null;
      showPageAccessGate(tabValue);
      try { refreshTutorial(tabValue); } catch (_) {}
      return;
    }

    // Normal page: ensure any prior gate is torn down, then dispatch. Flow +
    // Runtime Loop are sidebar views inside Admin Tools, so stopAdminTools() (the
    // admin-tools page's stop hook) owns their lifecycle when leaving admin-tools.
    // Admin Tools needs its lazy sub-panel init run once before its start hook.
    hidePageAccessGate();
    _stopAllExcept(tabValue);
    if (tabValue === 'admin-tools') {
      _ensureAdminInit();
    }
    _startPage(tabValue);

    // Re-render tutorial hint badges for the newly active tab. Defer a tick
    // so the tab's start*() routine has populated dynamic content first.
    try { refreshTutorial(tabValue); } catch (_) {}
  }

  // On load: if ?tab=<tabValue> is in the URL, activate that tab. Used when
  // middle-clicking a header button opens a new window — and now also the path a
  // direct link to a GATED page travels: validate against the page's content
  // mount (created for every catalog page, gated or not) rather than the <select>
  // option, which is deliberately absent for gated pages. That way a deep link to
  // a registered-only page lands on the inline sign-in gate instead of being
  // silently ignored. `_deepLinkHandled` then suppresses the default initial
  // activation below so it can't overwrite the gate (or the deep-linked page).
  let _deepLinkHandled = false;
  let _deepLinkTab = null;
  (function () {
    try {
      var p = new URLSearchParams(location.search);
      var t = p.get('tab');
      if (t && document.getElementById('tab-' + t)) {
        _deepLinkTab = t;
        // Sub-view deep link: ?tab=admin-tools&view=<id> should open that sidebar
        // view. Seed the saved-view preference BEFORE activating so startAdminTools
        // restores it (applySidebarView coerces an unknown id back to Explorer).
        // This only writes a UI preference — admin access is still gated by
        // activateTab + the server, so a non-admin's link can never reach the view.
        if (t === 'admin-tools') {
          var v = p.get('view');
          if (v) { try { localStorage.setItem('files.sidebarView', v); } catch (_) {} }
        }
        if (tabSelect.querySelector('option[value="' + t + '"]')) tabSelect.value = t;
        activateTab(t, true);
        _deepLinkHandled = true;
      }
    } catch (_) {}
  })();

  // Programmatic view switch for the App-control ability (agent-driven, via a
  // `ui_command` WebSocket event). Mirrors a user picking from the dropdown:
  // sync the select (when the option exists) then activate the tab.
  window.__setMainTab = function (view) {
    if (!view) return;
    try {
      if (tabSelect.querySelector(`option[value="${view}"]`)) {
        tabSelect.value = view;
      }
      activateTab(view, true);
    } catch (_) { /* ignore */ }
  };

  // Reflect a page's active SUB-VIEW in the address bar (e.g. the Admin Tools
  // sidebar view → ?tab=admin-tools&view=database) so a sub-view is shareable and
  // survives a refresh. The page that owns the sub-view (files.js' applySidebarView)
  // calls this whenever the view changes. Guards keep the bar honest:
  //  • only the LIVE page may write (a background re-render can't hijack the bar),
  //  • the param must be declared in _PAGE_PARAMS for that page,
  //  • &view= is never written unless ?tab=<page> is already present, and
  //  • the page's DEFAULT sub-view is the bare ?tab=<page> (param omitted).
  // SECURITY: this only MIRRORS UI state into the URL — it grants nothing. The
  // Admin Tools sub-views stay gated by activateTab (a non-admin is redirected to
  // the default page, never activated onto admin-tools) and by the server, where
  // every admin endpoint is independently _require_admin. Reading the param back
  // (the deep-link handler below) likewise only seeds a UI preference.
  window.__setMainSubView = function (pageId, viewId, defaultView) {
    try {
      if (_activePageId !== pageId) return;
      const owned = _PAGE_PARAMS[pageId];
      if (!owned || owned.indexOf('view') === -1) return;
      const url = new URL(location.href);
      if (url.searchParams.get('tab') !== pageId) return;
      const cur = url.searchParams.get('view') || '';
      const next = (!viewId || viewId === (defaultView || 'explorer')) ? '' : viewId;
      if (cur === next) return;
      if (next) url.searchParams.set('view', next);
      else url.searchParams.delete('view');
      history.replaceState(history.state, '', url);
    } catch (_) { /* address-bar sync is best-effort */ }
  };

  tabSelect.addEventListener('change', (e) => {
    activateTab(e.target.value, true);
  });

  // The loop-visualizer controls (level filters + autoscroll) are driven by the
  // agent-loop module under ui/agents/. Import it LAZILY + defensively so this
  // module never fails to load when that page folder is absent (drop-in safety).
  import('../../main-panel/agents/agent-loop/js/loop.js').then((loop) => {
    document.querySelectorAll('.loop-filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.loop-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        loop.setLoopLevel(btn.dataset.level);
      });
    });
    const autoScrollBtn = document.getElementById('loop-autoscroll');
    if (autoScrollBtn) {
      autoScrollBtn.addEventListener('click', loop.toggleAutoScroll);
    }
  }).catch(() => {});

  // Skip the default initial activation when a ?tab= deep link already activated
  // a page (or its gate) above — re-activating tabSelect.value here would drop the
  // gate and snap to the default tab.
  if (!_deepLinkHandled) activateTab(tabSelect.value);

  // Deferred Admin Tools restore. The guard above redirected admin-tools → a safe
  // default because admin status isn't known yet at boot (the /check-access probe
  // is async). This covers BOTH ways an admin can request Admin Tools at boot: a
  // restored lastActiveTab OR a ?tab=admin-tools deep link. Once the server
  // confirms admin, send them (back) to Admin Tools — userInitiated:true so the
  // address bar reflects ?tab=admin-tools(&view=) and the link stays shareable. A
  // confirmed NON-admin gets is_admin=false here and simply stays on the default.
  if ((_isAdminOnlyTab(savedTab) || _isAdminOnlyTab(_deepLinkTab)) && !isAdmin()) {
    const _restoreAdminTab = (e) => {
      window.removeEventListener('admin-status-loaded', _restoreAdminTab);
      if (e && e.detail && e.detail.is_admin) activateTab('admin-tools', true);
    };
    window.addEventListener('admin-status-loaded', _restoreAdminTab);
  }

  // ── First-ever landing: send a brand-new admin to Data Settings ──
  // On the VERY FIRST load of this browser (after the setup wizard + splash, once
  // the admin password is set and the gate is passed) the first content page an
  // admin should see is Admin Configuration → Data Settings, not the usual Agents
  // landing. It fires AT MOST ONCE per browser and only for a confirmed admin.
  //
  // Mechanics: land = Admin Tools tab (lastActiveTab) → 'settings' sidebar view
  // (files.sidebarView) → 'data-settings' section (appConfig_activeSection). We
  // seed the latter two localStorage keys BEFORE activating admin-tools so that
  // files.js' applySidebarView and nav.js' module-eval both read the fresh values
  // when Admin Tools first lazily mounts. Admin status is async (the /check-access
  // probe), so — like the deferred restore above — we wait for admin-status-loaded.
  //
  // State machine (localStorage 'webagent.firstLanding.v1'):
  //   absent  → first time this code runs in this browser. If the browser already
  //             has a saved tab or arrived via a ?tab= deep link, it's an existing/
  //             intentional visit → mark 'done' (suppress, never yank them). A truly
  //             fresh browser → 'pending' and arm the listener.
  //   pending → armed but no admin has signed in yet (e.g. the admin signs in on a
  //             later load); keep waiting so their first authenticated load still
  //             lands on Data Settings. A non-admin visitor just leaves it pending.
  //   done    → already landed (or suppressed) — do nothing.
  (function () {
    const KEY = 'webagent.firstLanding.v1';
    let state;
    try { state = localStorage.getItem(KEY); } catch (_) { return; }
    if (state === 'done') return;
    if (state === null) {
      if (savedTab || _deepLinkHandled) {
        try { localStorage.setItem(KEY, 'done'); } catch (_) {}
        return;
      }
      try { localStorage.setItem(KEY, 'pending'); } catch (_) {}
    }
    const _firstLanding = (e) => {
      window.removeEventListener('admin-status-loaded', _firstLanding);
      if (!(e && e.detail && e.detail.is_admin)) return;  // stay 'pending' for non-admins
      try { localStorage.setItem('files.sidebarView', 'settings'); } catch (_) {}
      try { localStorage.setItem('appConfig_activeSection', 'data-settings'); } catch (_) {}
      try { localStorage.setItem(KEY, 'done'); } catch (_) {}
      activateTab('admin-tools', true);
    };
    window.addEventListener('admin-status-loaded', _firstLanding);
  })();

  // ── Middle-click on a header tab opens that page in a new browser tab ──
  // (The new-session + button's own middle-click handler moved with the button
  // into ui/chat-side-panel/js/session-init.js when it relocated to the chat
  // header — that partial mounts after this shell-init runs.)
  (function () {
    var base = location.pathname.replace(/\/+$/, '') || '/';
    document.querySelectorAll('#main-tabs .main-tab').forEach(function (btn) {
      btn.addEventListener('auxclick', function (e) {
        if (e.button !== 1) return;  // middle-click only
        e.preventDefault();
        var val = btn.dataset.value;
        if (val) {
          window.open(base + '?tab=' + encodeURIComponent(val), '_blank');
        }
      });
    });
  })();

  // ── If this tab was opened with ?agent=X&new=1, start a fresh session ──
  (function () {
    try {
      var p = new URLSearchParams(location.search);
      var agentId = p.get('agent');
      if (agentId && p.get('new') === '1') {
        // Clear localStorage saved session so the new tab doesn't load an old one
        try { localStorage.removeItem('lastActiveTab'); } catch (_) {}
        // Wait for the app state to be ready, then switch
        var _trySwitch = function () {
          if (typeof app !== 'undefined' && typeof app.switchToAgent === 'function' && app.currentUserId) {
            app.switchToAgent(agentId, { forceNewSession: true, silent: true });
          } else {
            setTimeout(_trySwitch, 100);
          }
        };
        setTimeout(_trySwitch, 0);
      }
    } catch (_) {}
  })();

  // The spinner was shown in the header during init (pre-paint script
  // set body.is-booting). Now that the tab content is mounted, clear
  // is-booting so the spinner fades out immediately.
  document.body.classList.remove('is-booting');
}
