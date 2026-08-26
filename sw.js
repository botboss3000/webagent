/* WebAgent Service Worker — v2
 *
 * Strategy:
 *   - App shell (HTML, CSS, JS, icons): precache on install
 *   - API calls (/api/*): network-only (never cache stale data)
 *   - CDN assets: stale-while-revalidate
 *   - Executable app assets (JS/CSS/JSON): cache-first within one versioned
 *     worker generation. The boot loader installs/activates an update before
 *     importing modules, so a generation is coherent without paying a network
 *     round-trip for every module on every visit.
 *   - Passive static assets (images/fonts): stale-while-revalidate.
 *   - Navigation: network-first, fall back to cached index.html
 *
 * Bump CACHE on each release so the activate handler drops the prior cache.
 */

const CACHE = "webagent-v280";
const STATIC_PATTERN = /\.(css|js|json|svg|png|ico|woff2?)$/;
const CODE_PATTERN = /\.(css|js|json)$/;
const HTML_PATTERN = /\.html$/;
// Kept for defence-in-depth only: as of the offline-vendoring change the app
// no longer requests anything from these CDNs (fonts + JS libs are self-hosted
// under /ui/vendor/). Any stray CDN request still gets stale-while-revalidate.
const CDN_PATTERN = /^(https?:)?\/\/(fonts\.googleapis|cdn\.jsdelivr|unpkg)\./;
const API_PATTERN = /^\/api\//;
const WS_PATTERN = /^\/api\/v1\/agent\/ws/;

/* ── Install: precache the app shell ──
 * Paths must match the live `ui/` layout. The CSS moved into `ui/shared/css/`
 * and per-page folders during the UI restructure; the old `/ui/css/...` paths
 * here 404'd.
 *
 * IMPORTANT — resilient precache: each asset is added INDIVIDUALLY via
 * Promise.allSettled, not cache.addAll(). addAll() rejects the WHOLE batch if a
 * single URL 404s, which failed the install and PINNED the previous service
 * worker (and its stale cached CSS) — the real cause of "I edited the CSS but
 * the old version keeps showing / it needs several refreshes". With allSettled
 * the new worker always installs + activates; any asset that can't be fetched is
 * simply skipped here and still runtime-cached on first use (staleWhileRevalidate
 * below). So a future file move degrades gracefully instead of freezing updates. */
// Keep install bounded to the bootstrap shell and its pre-controller assets.
// The full executable module graph is still cached by cacheFirst as the live
// page requests it; precaching that entire graph made workers take tens of
// seconds to install. The small set below guarantees that one successful
// online visit is enough to open the shell and chat partial offline.
const PRECACHE = [
  "/index.html",
  "/ui/manifest.json",
  "/ui/favicon.svg",
  "/ui/shared/css/index.css",
  "/ui/shared/css/design-system.css",
  "/ui/shared/css/app1.css",
  "/ui/shared/css/app2.css",
  "/ui/shared/css/app3.css",
  "/ui/shared/css/app-control-point.css",
  "/ui/shared/js/appearance.js",
  "/ui/shared/js/header-build.js?v=3",
  "/ui/vendor/fonts/fonts.css",
  "/ui/vendor/lucide/lucide.min.js",
  "/ui/vendor/marked/marked.min.js",
  "/ui/vendor/dompurify/purify.min.js",
  "/ui/vendor/prismjs/prism-core.min.js",
  "/ui/vendor/prismjs/prism-autoloader.min.js",
  "/ui/vendor/xterm/xterm.css",
  "/ui/vendor/xterm/xterm.js",
  "/ui/vendor/xterm/addon-fit.js",
  "/ui/vendor/xterm/addon-web-links.js",
  "/ui/vendor/xterm/addon-search.js",
  "/ui/shared/js/db-select.js",
  "/ui/shared/js/device-picker.js",
  "/ui/shared/js/partial-loader.js",
  "/ui/chat/chat-side-panel.html",
  "/ui/main-panel/agents/agents.html",
  "/ui/main-panel/wiki/wiki.html",
];
self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((cache) =>
      Promise.allSettled(PRECACHE.map((u) => cache.add(u)))
    )
  );
  self.skipWaiting();
});

/* ── Activate: clean old caches ── */
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  // Tell all clients the current cache version so the debug console can show it
  self.clients.matchAll().then((clients) => {
    clients.forEach((c) => c.postMessage({ type: "sw-version", cache: CACHE }));
  });
  self.clients.claim();
});

/* ── Fetch strategy ── */
self.addEventListener("fetch", (e) => {
  const { request } = e;
  const url = new URL(request.url);

  // WebSocket upgrade — never intercept
  if (WS_PATTERN.test(url.pathname)) return;

  // API calls — network-only
  if (API_PATTERN.test(url.pathname)) return;

  // web-terminal — a self-contained app embedded as the Terminal tab. Never
  // SW-cache it, so a deploy is never a version stale (its .js/.jsx are served
  // no-store anyway). Without this, .js assets hit staleWhileRevalidate below.
  if (url.pathname.startsWith("/web-terminal")) return;

  // CDN assets — stale-while-revalidate
  if (CDN_PATTERN.test(url.hostname)) {
    e.respondWith(staleWhileRevalidate(request));
    return;
  }

  // App code is immutable inside a CACHE generation. Boot waits briefly for an
  // updated worker before importing the graph, and activation deletes older
  // generations, so cache-first is both coherent and dramatically faster on a
  // returning visit. Development/release changes must bump CACHE above.
  if (url.origin === self.location.origin && CODE_PATTERN.test(url.pathname)) {
    e.respondWith(cacheFirst(request));
    return;
  }

  // UI partials are the markup half of the executable shell. They are fetched
  // with cache:no-store by the live loader, but an offline cold start still
  // needs the last coherent worker generation. API/server-rendered HTML never
  // reaches this branch because only static .html paths qualify.
  if (url.origin === self.location.origin && HTML_PATTERN.test(url.pathname)) {
    e.respondWith(cacheFirst(request));
    return;
  }

  // Passive static assets — stale-while-revalidate.
  if (STATIC_PATTERN.test(url.pathname)) {
    e.respondWith(staleWhileRevalidate(request));
    return;
  }

  // Navigation requests — network-first, fall back to index.html
  if (request.mode === "navigate") {
    e.respondWith(networkFirstNavigation(request));
    return;
  }

  // Everything else — network-only, don't block
});

/* ── Cache strategies ── */

/* ── Shared helper — fetches and caches the response if OK ── */
async function fetchAndCache(request) {
  const response = await fetch(request);
  if (response.ok) {
    const copy = response.clone();
    caches.open(CACHE).then((cache) => cache.put(request, copy));
  }
  return response;
}

async function cacheFirst(request) {
  // Never search every CacheStorage generation here. During activate(), an old
  // cache can coexist briefly with the new worker; caches.match() may then hand
  // a new importer an old dependency and create a broken mixed module graph.
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    return await fetchAndCache(request);
  } catch {
    return new Response("Offline", { status: 503 });
  }
}

async function staleWhileRevalidate(request) {
  const cached = await caches.match(request);
  const fetchPromise = fetchAndCache(request)
    .catch(() => null);
  return cached || (await fetchPromise) || new Response("Offline", { status: 503 });
}

async function networkFirstNavigation(request) {
  try {
    return await fetchAndCache(request);
  } catch {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(request);
    if (cached) return cached;
    const fallback = await cache.match("/index.html");
    return fallback || new Response("Offline", { status: 503 });
  }
}
