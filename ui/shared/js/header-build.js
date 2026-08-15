/*
 * header-build.js — classic (non-module) script, loaded BEFORE the carousel
 * wiring so the header tab strip can be painted synchronously from the cached
 * page catalog (no flash for returning users). The SAME builder is then called
 * again by partial-loader.js with the freshly-fetched catalog (authoritative).
 *
 * The set of pages is NOT hardcoded here — it comes from GET /api/v1/pages/catalog,
 * which is built from the drop-in ui/<page>/page.json descriptors. This file only
 * knows HOW to render a page list into buttons / <select> options / content
 * mounts; WHAT the pages are is data. Everything is exposed on window so both the
 * inline pre-paint (classic) and partial-loader/tabs.js (modules) share one impl.
 *
 * FALLBACK_MAIN below is a last-resort seed used only when the catalog API is
 * unreachable AND there is no cache (first-ever offline boot) — it mirrors the
 * seed descriptors so the shell still renders. It is not the source of truth.
 */
(function () {
  'use strict';

  var CACHE_KEY = 'pagesCatalogCache';
  var CATALOG_URL = '/api/v1/pages/catalog';

  // Last-resort seed: ONLY the locked Admin Tools shell, so a degraded boot
  // (catalog API 500s AND no cache) still renders something useful (Admin Tools
  // hosts diagnostics). Real feature pages are never hardcoded here — they come
  // from the catalog — so a removed page can never be resurrected by this seed.
  var FALLBACK_MAIN = [
    { id: 'admin-tools', label: 'Admin Tools', icon: 'wrench', order: 0, locked: true, mount: '#tab-admin-tools' }
  ];

  window.__pagesFallback = function () { return { main: FALLBACK_MAIN.slice(), admin: [] }; };

  window.__readPagesCache = function () {
    try { var raw = localStorage.getItem(CACHE_KEY); if (raw) return JSON.parse(raw); } catch (e) {}
    return null;
  };
  window.__writePagesCache = function (cat) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(cat)); } catch (e) {}
  };

  // Memoized catalog + in-flight promise so partial-loader and tabs.js share one fetch.
  window.__pagesCatalog = null;
  var _loadPromise = null;
  window.__loadPagesCatalog = function () {
    if (window.__pagesCatalog) return Promise.resolve(window.__pagesCatalog);
    if (_loadPromise) return _loadPromise;
    _loadPromise = fetch(CATALOG_URL, { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (cat) {
        if (!cat || !Array.isArray(cat.main)) throw new Error('bad catalog shape');
        window.__pagesCatalog = cat;
        window.__writePagesCache(cat);
        _loadPromise = null;
        return cat;
      })
      .catch(function (e) {
        _loadPromise = null;
        var cached = window.__readPagesCache();
        var fallback = cached || window.__pagesFallback();
        window.__pagesCatalog = fallback;
        console.warn('[pages] catalog fetch failed (' + e + ') — using ' + (cached ? 'cache' : 'fallback'));
        return fallback;
      });
    return _loadPromise;
  };

  // Force a fresh catalog fetch (after an admin edit), update the cache + memo,
  // rebuild the header, and resolve to the new catalog. Falls back to cache on error.
  window.__reloadPagesCatalog = function () {
    window.__pagesCatalog = null;
    _loadPromise = null;
    return window.__loadPagesCatalog().then(function (cat) {
      try { window.__ensurePageStyles(cat); } catch (e) {}
      try { window.__buildHeader(cat.main); } catch (e) {}
      return cat;
    });
  };

  // Inject each catalog page's own stylesheet(s) as <link> tags, RELATIVE to the
  // page folder (descriptor `css`, served at /ui/<dir>/<file>). Idempotent —
  // keyed by href so calling it from the pre-paint (cache) and again with the
  // fresh catalog only adds new ones. This is what kills the per-page <link>
  // hardcoding in index.html: a page carries its own CSS, so dropping the folder
  // in / removing it loads / unloads its styling with zero edits to index.html.
  window.__ensurePageStyles = function (cat) {
    if (!cat) return;
    var pages = [].concat(cat.main || [], cat.admin || []);
    var head = document.head || document.getElementsByTagName('head')[0];
    if (!head) return;
    pages.forEach(function (p) {
      if (!p || !p.dir || !p.css || !p.css.length) return;
      p.css.forEach(function (rel) {
        if (!rel) return;
        var href = './ui/' + p.dir + '/' + rel;
        if (head.querySelector('link[data-page-css][href="' + href + '"]')) return;
        var link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = href;
        link.setAttribute('data-page-css', p.id || p.dir);
        head.appendChild(link);
      });
    });
  };

  // Read a JWT's `exp` claim (epoch ms) without verifying the signature — for
  // display gating only; the server is the real authority. Returns null when the
  // token is missing/malformed or carries no exp. Mirrors _decodeJwtExpMs in
  // auth-refresh.js (this file is a classic script and can't import it).
  function tokenExpMs(token) {
    if (!token || typeof token !== 'string') return null;
    var parts = token.split('.');
    if (parts.length < 2) return null;
    try {
      var b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
      while (b64.length % 4) b64 += '=';
      var claims = JSON.parse(atob(b64));
      return typeof claims.exp === 'number' ? claims.exp * 1000 : null;
    } catch (e) { return null; }
  }

  // True only when the token's exp is PROVABLY in the past. Conservative on
  // purpose (matches auth-refresh.js): a token we can't read the exp of is NOT
  // treated as expired here, so a legitimate member is never falsely hidden by a
  // parsing quirk — genuinely-broken tokens are handled by the auto-sign-out.
  function tokenExpired(token) {
    var exp = tokenExpMs(token);
    return typeof exp === 'number' && exp <= Date.now();
  }

  // A visitor counts as "signed in" (a real member) once a member auth token is
  // stored (the login/logout flow reloads the page, so reading it at build time
  // is enough — the header is rebuilt from scratch on every auth transition).
  // An Open Registration *guest* also holds a token, but it's an anonymous
  // (anon_*) identity — NOT a member — so 'auth'-visibility pages stay hidden
  // from guests just as they are from logged-out visitors.
  //
  // An EXPIRED token does NOT count as signed in: a dead token left in storage
  // used to keep 'auth'-visibility pages (Sessions, Browser, Automations, …)
  // showing even though every server call they make is rejected — the same
  // "signed-in but powerless" zombie state that hid Admin Tools. Gating on a
  // provably-expired token here keeps the strip honest until the auto-sign-out
  // (auth-refresh.js → main.js) clears the token and reloads.
  function isSignedIn() {
    try {
      var token = localStorage.getItem('auth_token');
      if (!token) return false;
      if (tokenExpired(token)) return false;
      var uid = localStorage.getItem('auth_user_id') || '';
      return uid.indexOf('anon_') !== 0;
    } catch (e) { return false; }
  }

  // 3-state page visibility: "all" (always) / "auth" (signed-in only) / "off"
  // (hidden from everyone). A locked page (Admin Tools / Admin Configuration) can
  // never be fully hidden — it would lock the user out of the app's config — so
  // "off" is ignored for it; it may still be "auth" (signed-in only). Falls back
  // to the legacy boolean `hidden` for catalogs served before this field existed.
  function shouldHidePage(p) {
    var vis = p.visibility || (p.hidden ? 'off' : 'all');
    if (vis === 'off') return !p.locked;           // off hides everyone; a locked page can't be off
    if (vis === 'auth' && !isSignedIn()) return true;
    return false;
  }

  // Single source of truth for "is this page hidden from this visitor?", reused
  // by the navigation layer (tabs.js) so it can REFUSE to activate a page whose
  // tab is hidden — closing the hole where a gated page could still be mounted
  // via a ?tab= deep link, a middle-click, a restored lastActiveTab, or an agent
  // ui_command even though its header tab is correctly hidden. It looks the page
  // up in the live catalog (the authoritative memo, falling back to the cached
  // copy so it also works in the brief pre-paint window) and applies the EXACT
  // same shouldHidePage() rule that paints the strip, so the gate and the strip
  // can never disagree. Unknown page / no catalog → false (treat as visible):
  // fail-open to the prior behaviour, since the server-side ownership gates are
  // the real data boundary and a transient catalog miss must never strand a
  // legitimate user on a blank screen.
  window.__pageHiddenByVisibility = function (pageId) {
    if (!pageId) return false;
    try {
      var cat = window.__pagesCatalog || window.__readPagesCache();
      var list = (cat && Array.isArray(cat.main)) ? cat.main : [];
      for (var i = 0; i < list.length; i++) {
        if (list[i] && list[i].id === pageId) return shouldHidePage(list[i]);
      }
    } catch (e) {}
    return false;
  };

  function lucideIcon(name) {
    var i = document.createElement('i');
    i.setAttribute('data-lucide', name || 'square');
    i.className = 'lucide-icon';
    return i;
  }

  function sortPages(pages) {
    return pages.slice().sort(function (a, b) {
      var la = a.locked ? 0 : 1, lb = b.locked ? 0 : 1;
      if (la !== lb) return la - lb;
      var oa = a.order | 0, ob = b.order | 0;
      if (oa !== ob) return oa - ob;
      return String(a.label).toLowerCase() < String(b.label).toLowerCase() ? -1 : 1;
    });
  }

  // Admin Tools is wrapped in a group span carrying the health dot (other code
  // targets #admin-health-dot). Every other page is a bare .main-tab button.
  function buildButton(p) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'main-tab';
    btn.setAttribute('role', 'tab');
    btn.dataset.value = p.id;
    btn.dataset.generated = '1';
    btn.appendChild(lucideIcon(p.icon));
    btn.appendChild(document.createTextNode(p.label));
    if (p.id === 'admin-tools') {
      var dot = document.createElement('span');
      dot.id = 'admin-health-dot';
      dot.className = 'health-dot health-dot-red';
      dot.title = 'Server status unknown';
      btn.appendChild(dot);
      var group = document.createElement('span');
      group.className = 'main-tab-group';
      group.id = 'admin-tools-group';
      group.dataset.generated = '1';
      group.appendChild(btn);
      return group;
    }
    return btn;
  }

  function ensureContentMount(p, panel) {
    var existing = document.getElementById('tab-' + p.id);
    if (existing) return existing;
    var div = document.createElement('div');
    div.id = 'tab-' + p.id;
    div.className = 'tab-content';
    div.dataset.generated = '1';
    if (p.iframe) {
      var f = document.createElement('iframe');
      f.src = p.iframe;
      f.className = 'terminal-frame';
      f.title = p.label || p.id;
      f.loading = 'lazy';
      div.appendChild(f);
    }
    // Insert before #tab-account when present so account stays the last mount.
    var anchor = document.getElementById('tab-account');
    panel.insertBefore(div, anchor || null);
    return div;
  }

  // Build the header tab strip, hidden <select> options, and #tab-<id> content
  // mounts from a main-pages list. Idempotent: removes its own prior output
  // (data-generated) and preserves the user-dropdown, #tab-account and chat nodes.
  window.__buildHeader = function (mainPages) {
    var tabBar = document.getElementById('main-tabs');
    var sel = document.getElementById('main-tab-select');
    var panel = document.getElementById('main-panel');
    if (!tabBar || !panel || !mainPages || !mainPages.length) return;

    var pages = sortPages(mainPages);

    tabBar.querySelectorAll('[data-generated="1"]').forEach(function (el) { el.remove(); });
    pages.forEach(function (p) {
      var node = buildButton(p);
      if (shouldHidePage(p)) node.style.display = 'none';
      tabBar.appendChild(node);
    });

    if (sel) {
      var accountOpt = sel.querySelector('option[value="account"]');
      sel.querySelectorAll('option[data-generated="1"]').forEach(function (o) { o.remove(); });
      pages.forEach(function (p) {
        // Keep gated pages out of the mobile <select> too, mirroring the hidden
        // tab buttons — so a visitor can neither see nor pick a page the admin's
        // visibility setting excludes them from.
        if (shouldHidePage(p)) return;
        var o = document.createElement('option');
        o.value = p.id; o.textContent = p.label; o.dataset.generated = '1';
        sel.appendChild(o);
      });
      if (!accountOpt) {
        accountOpt = document.createElement('option');
        accountOpt.value = 'account'; accountOpt.textContent = 'Manage Account';
      }
      sel.appendChild(accountOpt); // account always last
    }

    pages.forEach(function (p) { ensureContentMount(p, panel); });

    // Re-highlight the currently active tab button after a rebuild.  The
    // observer in index.html guards against redundant same-tab syncs, so it
    // won't re-fire for the same id — we must explicitly set .active here
    // or the strip shows no selection until the user clicks a tab.
    var activeContent = panel.querySelector('.tab-content.active');
    if (activeContent) {
      var activeId = activeContent.id.replace('tab-', '');
      tabBar.querySelectorAll('.main-tab').forEach(function (b) {
        b.classList.toggle('active', b.dataset.value === activeId);
      });
    }

    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      try { window.lucide.createIcons(); } catch (e) {}
    }
    if (typeof window.__refreshTabStrip === 'function') {
      try { window.__refreshTabStrip(); } catch (e) {}
    }
  };

  // ── Admin Tools sidebar strip ─────────────────────────────────────────────
  // The Admin Tools left strip (the 38px icon column) is the sister of the main
  // header: its view-switch icons are built from the SAME catalog (the `admin`
  // array) so a dropped-in ui/admin-tools/<view>/page.json appears with zero
  // edits to admin-tools.html. Each button carries data-view=<id>; files.js
  // delegates clicks on .files-strip-view and swaps the matching #files-<id>-main.
  function buildStripBtn(p, activeView) {
    var btn = document.createElement('button');
    btn.type = 'button';
    // The settings button keeps its legacy class — a tutorial step targets it.
    btn.className = 'files-strip-btn files-strip-view'
      + (p.id === 'settings' ? ' files-settings-toggle-btn' : '');
    btn.dataset.view = p.id;
    btn.dataset.generated = '1';
    btn.title = p.label || p.id;
    var active = (p.id === activeView);
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
    if (shouldHidePage(p)) btn.style.display = 'none';
    btn.appendChild(lucideIcon(p.icon));
    // Title label — hidden in icon mode, revealed when the strip is expanded
    // into a labelled rail (see .files-strip-label / .rail-expanded in files.css).
    var lbl = document.createElement('span');
    lbl.className = 'files-strip-label';
    lbl.textContent = p.label || p.id;
    btn.appendChild(lbl);
    return btn;
  }

  // ── Expandable icon rail ──────────────────────────────────────────────────
  // The 38px strip can widen to show each view's title next to its icon and
  // collapse back. The choice persists per browser and is re-applied on every
  // rebuild so it survives catalog refreshes. Orthogonal to the split/strip
  // width states — the .rail-expanded class on #files-sidebar drives the CSS.
  var LS_RAIL = 'files.adminRailExpanded';
  function railExpanded() {
    try { return localStorage.getItem(LS_RAIL) === '1'; } catch (e) { return false; }
  }
  function applyRailState(sidebar) {
    var on = railExpanded();
    sidebar.classList.toggle('rail-expanded', on);
    var t = sidebar.querySelector('.files-strip-rail-toggle');
    if (t) {
      t.setAttribute('aria-expanded', on ? 'true' : 'false');
      t.title = on ? 'Collapse sidebar' : 'Expand sidebar';
      var ic = t.querySelector('[data-lucide]');
      if (ic) {
        ic.setAttribute('data-lucide', on ? 'panel-left-close' : 'panel-left-open');
        // Mark this deliberately changed icon as pending. The shared Lucide
        // guard skips stable rendered SVGs so clicks elsewhere remain intact.
        ic.classList.remove('lucide');
      }
    }
  }
  function ensureRailToggle(strip) {
    if (strip.querySelector('.files-strip-rail-toggle')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'files-strip-btn files-strip-rail-toggle';
    btn.title = 'Expand sidebar';
    btn.appendChild(lucideIcon('panel-left-open'));
    var lbl = document.createElement('span');
    lbl.className = 'files-strip-label';
    lbl.textContent = 'Collapse';
    btn.appendChild(lbl);
    btn.addEventListener('click', function () {
      var on = !railExpanded();
      try { localStorage.setItem(LS_RAIL, on ? '1' : '0'); } catch (e) {}
      var sb = document.getElementById('files-sidebar');
      if (sb) applyRailState(sb);
      if (window.lucide && typeof window.lucide.createIcons === 'function') {
        try { window.lucide.createIcons(); } catch (e) {}
      }
    });
    strip.insertBefore(btn, strip.firstChild);
  }

  // Build (or rebuild) the strip's view-switch buttons from the admin page list.
  // Idempotent: removes its own prior output (data-generated) and preserves any
  // non-generated controls. Re-applies the active highlight from the sidebar's
  // current data-view so highlighting survives a rebuild.
  window.__buildAdminStrip = function (adminPages) {
    var sidebar = document.getElementById('files-sidebar');
    if (!sidebar || !adminPages || !adminPages.length) return;
    var strip = sidebar.querySelector('.files-sidebar-strip');
    if (!strip) return;
    var activeView = sidebar.dataset.view || 'explorer';
    var pages = sortPages(adminPages);
    strip.querySelectorAll('[data-generated="1"]').forEach(function (el) { el.remove(); });
    pages.forEach(function (p) { strip.appendChild(buildStripBtn(p, activeView)); });
    ensureRailToggle(strip);
    applyRailState(sidebar);
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      try { window.lucide.createIcons(); } catch (e) {}
    }
  };
})();
