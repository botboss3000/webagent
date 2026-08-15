// Fetches HTML partials at boot and injects them into placeholder slots in
// index.html before the main JS modules run. Two-phase load:
//   Phase 1: top-level tab pages (Gen UI / Agents / Admin Tools) — each
//            partial's body replaces the inner HTML of its mount.
//   Phase 2: admin sub-pages — each partial uses <template data-slot="...">
//            blocks whose content is appended into the matching selector
//            in the parent document (the slots live inside the admin-tools
//            shell loaded in phase 1).
//
// Resolves the exported `partialsReady` promise once all partials are in
// the DOM, after which the bootstrap in index.html dynamically imports the
// main JS modules so every getElementById call still resolves.

// The top-level page partials are derived from the page catalog at boot (drop-in
// ui/<page>/page.json descriptors) rather than hardcoded — adding a page folder
// makes its partial load with no edit here. The chat side-panel is the one fixed
// (non-page) partial. Pages without an `html` (e.g. the iframe-only Terminal)
// contribute no partial; their content mount is built by __buildHeader.
const CHAT_PARTIAL = { url: './ui/chat/chat-side-panel.html', mount: '#chat-panel' };

function topLevelFromCatalog(mainPages) {
  const list = [];
  for (const p of (mainPages || [])) {
    if (p.html && p.mount && p.dir) {
      list.push({ url: `./ui/${p.dir}/${p.html}`, mount: p.mount });
    }
    // A main page may ALSO ship extra <template data-slot> partials (its page.json
    // `partials` list, e.g. Instances' App Config sections). These are injected
    // with the same slot-appending used for admin sub-page partials, after the
    // page's own html has been mounted (see injectSlotsFromHtml in phase 1).
    for (const extra of (p.partials || [])) {
      if (extra) list.push({ url: `./ui/${p.dir}/${extra}`, slot: true });
    }
  }
  list.push(CHAT_PARTIAL);
  return list;
}

const ADMIN_SUB_PAGES = [
  // Empty: EVERY admin view now ships its partial inside its own
  // ui/admin-tools/<id>/ folder and loads from the catalog via
  // dropInAdminPartials(). Explorer + Terminal were the last fixed-path
  // built-ins; their HTML now lives in explorer/ and terminal/ like the rest.
  // (Their JS lifecycle is still the shared inline "Files" engine in files.js —
  //  that engine split is a separate, later phase — but nothing loads from a
  //  fixed root path any more.) A new admin view = drop a folder; no edit here.
];

// Every admin view is a DROP-IN: it carries its own partial(s) inside its
// ui/admin-tools/<id>/ folder and loads from its descriptor — so a new folder
// needs no edit here. Its main partial (`html`) must expose <template
// data-slot="#admin-tools"> (a <main class="files-main" id="files-<id>-main"
// data-view="<id>">) and optionally a <template data-slot="#files-sidebar">
// panel. A multi-section view (Settings) additionally lists section partials in
// its descriptor `partials` array; those load right after its `html`.
//
// This set is the escape hatch for any view whose partial should NOT be loaded
// from its folder here (it ships elsewhere). It is empty now that Explorer +
// Terminal moved their HTML into explorer/ and terminal/ — those two still
// share one inline JS engine (files.js), but their partials load like any other.
const BUILTIN_ADMIN_IDS = new Set([]);

function dropInAdminPartials(adminPages) {
  const urls = [];
  for (const p of (adminPages || [])) {
    if (BUILTIN_ADMIN_IDS.has(p.id)) continue;
    if (!p.dir) continue;
    // The page's own partial first (it creates any slot targets its section
    // partials append into), then each extra partial in declared order.
    if (p.html) urls.push(`./ui/${p.dir}/${p.html}`);
    for (const extra of (p.partials || [])) {
      if (extra) urls.push(`./ui/${p.dir}/${extra}`);
    }
  }
  return urls;
}

// Import + start each `kind:"splash"` plugin from the catalog. Mirrors the page
// lifecycle dispatch in ui/shared/js/tabs.js: a dynamic import of the descriptor's
// `entry` module, then a call to its `start` export. The welcome screen is now a
// server-rendered landing page at / (app/main.py), so `start` no longer mounts an
// overlay — it just exposes window.WA_SPLASH for the account "Show welcome" toggle.
// Resolves the entry path against document.baseURI (entries are repo-root-relative,
// e.g. "ui/splash/splash-page/js/splash-page.js"). Per-plugin failures are isolated.
function bootSplashPlugins(splashPages) {
  for (const p of (splashPages || [])) {
    if (!p || !p.entry || !p.start) continue;
    try {
      import(new URL(p.entry, document.baseURI).href)
        .then((m) => { const fn = m && m[p.start]; if (typeof fn === 'function') fn(p); })
        .catch((e) => console.error('splash start ' + p.id + ' failed', e));
    } catch (e) {
      console.error('splash import ' + p.id + ' failed', e);
    }
  }
}

let _serverDownWarned = false;

async function fetchHtml(url) {
  let resp;
  try {
    resp = await fetch(url, { cache: 'no-store' });
  } catch (err) {
    // Connection refused / network down — every partial will fail with the same
    // cause, so emit a single clear message instead of one per partial.
    if (!_serverDownWarned) {
      _serverDownWarned = true;
      const host = location.host || 'localhost:8080';
      console.error(
        `[WebAgent] Cannot reach the backend at ${host}.`,
        '\n  The server appears to be down — start it (e.g. `python -m app.main`) then refresh the page.'
      );
    }
    throw err;
  }
  if (!resp.ok) throw new Error(`Failed to fetch ${url}: ${resp.status}`);
  return resp.text();
}

function injectIntoMount(html, mountSelector) {
  const mount = document.querySelector(mountSelector);
  if (!mount) throw new Error(`Mount not found: ${mountSelector}`);
  mount.innerHTML = html;
}

function injectSlotsFromHtml(html, sourceUrl) {
  // Parse as a document fragment to access top-level <template data-slot>.
  const wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  const templates = wrapper.querySelectorAll('template[data-slot]');
  if (!templates.length) {
    throw new Error(`No <template data-slot> blocks found in ${sourceUrl}`);
  }
  for (const tpl of templates) {
    const sel = tpl.getAttribute('data-slot');
    const target = document.querySelector(sel);
    if (!target) {
      throw new Error(`Slot target "${sel}" not found (from ${sourceUrl})`);
    }
    target.appendChild(tpl.content.cloneNode(true));
  }
}

export const partialsReady = (async () => {
  // Phase 0: fetch the page catalog and (authoritatively) build the header tab
  // strip + content mounts from it before loading any partials. __buildHeader /
  // __loadPagesCatalog come from the classic header-build.js loaded earlier; if
  // it is somehow absent we degrade to a minimal fallback so boot still works.
  let catalog = { main: [], admin: [] };
  try {
    catalog = (typeof window.__loadPagesCatalog === 'function')
      ? await window.__loadPagesCatalog()
      : (window.__pagesFallback ? window.__pagesFallback() : catalog);
  } catch (_) {
    catalog = window.__pagesFallback ? window.__pagesFallback() : catalog;
  }
  // Inject each page's own stylesheet(s) from the (authoritative) catalog before
  // any partial HTML is mounted — so dropped-in pages style correctly with no
  // <link> edit to index.html. Idempotent with the pre-paint cache injection.
  if (typeof window.__ensurePageStyles === 'function') {
    try { window.__ensurePageStyles(catalog); } catch (e) { console.error('ensurePageStyles failed', e); }
  }
  if (typeof window.__buildHeader === 'function') {
    try { window.__buildHeader(catalog.main); } catch (e) { console.error('buildHeader failed', e); }
  }

  // Splash plugin (drop-in `kind:"splash"` — the welcome screen). It is NOT a tab
  // and NOT an admin view: its `entry` module is dynamically imported and its
  // `start` export is called. The welcome screen itself is now a server-rendered
  // landing page at / (app/main.py); `start` only exposes window.WA_SPLASH so the
  // account "Show welcome screen" toggle works. When no splash folder exists
  // `catalog.splash` is empty and this is a no-op — so the whole feature comes and
  // goes with its `ui/splash/<id>/` folder.
  bootSplashPlugins(catalog.splash);

  const TOP_LEVEL = topLevelFromCatalog(catalog.main);

  // Phase 1: load top-level pages in parallel. A page's own partial replaces the
  // inner HTML of its mount; any extra <template data-slot> partials (page.json
  // `partials`, flagged `slot: true` by topLevelFromCatalog) are appended into
  // their slot targets — the same slot-injection admin sub-pages use — so a main
  // page can ship multi-section markup (e.g. Instances' App Config) with no edit
  // to index.html. Order is preserved: html first, then its slot partials.
  const topHtml = await Promise.all(TOP_LEVEL.map(p => fetchHtml(p.url)));
  TOP_LEVEL.forEach((p, i) => {
    if (p.slot) {
      injectSlotsFromHtml(topHtml[i], p.url);
    } else {
      injectIntoMount(topHtml[i], p.mount);
    }
  });

  // Phase 2: load admin sub-pages in parallel (slots now exist) — every admin
  // view partial is discovered from the catalog (ADMIN_SUB_PAGES is empty).
  const adminUrls = ADMIN_SUB_PAGES.concat(dropInAdminPartials(catalog.admin));
  const subHtml = await Promise.all(adminUrls.map(fetchHtml));
  adminUrls.forEach((url, i) => injectSlotsFromHtml(subHtml[i], url));

  // Render lucide icons for the freshly injected DOM.
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }
})();
