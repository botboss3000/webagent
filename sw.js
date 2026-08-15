/* WebAgent Service Worker — v2
 *
 * Strategy:
 *   - App shell (HTML, CSS, JS, icons): precache on install
 *   - API calls (/api/*): network-only (never cache stale data)
 *   - CDN assets: stale-while-revalidate
 *   - Executable app assets (JS/CSS/JSON): network-first, cache fallback. ES
 *     modules must come from one coherent checkout version; serving a cached
 *     dependency beside a fresh importer can abort the whole module graph.
 *   - Passive static assets (images/fonts): stale-while-revalidate.
 *   - Navigation: network-first, fall back to cached index.html
 *
 * Bump CACHE on each release so the activate handler drops the prior cache.
 */

const CACHE = "webagent-v232";
const STATIC_PATTERN = /\.(css|js|json|svg|png|ico|woff2?)$/;
const CODE_PATTERN = /\.(css|js|json)$/;
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
const PRECACHE = [
  "/",
  "/app",
  "/index.html",
  "/ui/diagnostics.html",
  "/ui/manifest.json",
  "/ui/favicon.svg",
  "/ui/icons/icon-192x192.png",
  "/ui/icons/icon-192x192-maskable.png",
  "/ui/icons/icon-512x512.png",
  "/ui/icons/icon-512x512-maskable.png",
  "/ui/shared/css/app1.css",
  "/ui/shared/css/app2.css",
  "/ui/shared/css/app3.css",
  "/ui/shared/css/app-control-point.css",
  "/ui/shared/css/index.css",
  "/ui/shared/css/design-system.css",
  // Core boot JS — the stable top of the module chain that gates first paint.
  // Precaching these means a normal reload serves them instantly instead of
  // re-fetching. Kept to the few files that rarely move; every OTHER module is
  // runtime-cached on first use by staleWhileRevalidate below, so this list
  // never needs to track the full (139-file) import graph. (A hard refresh
  // bypasses the SW entirely — this helps ordinary reloads + repeat visits.)
  "/ui/shared/js/appearance.js",
  "/ui/shared/js/header-build.js",
  "/ui/shared/js/partial-loader.js",
  "/ui/shared/js/main.js",
  "/ui/shared/js/debugConsole.js",
  "/ui/shared/js/clipboard.js",
  "/ui/shared/js/config.js",
  "/ui/shared/js/left-login.js",
  "/ui/shared/js/db-select.js",
  "/ui/shared/js/device-picker.js",
  "/ui/main-panel/agents/agent-loop/loop.css",
  "/ui/main-panel/agents/agent-loop/loop-visual.css",
  "/ui/main-panel/genui/genui.css",
  "/ui/main-panel/agents/agents.css",
  "/ui/main-panel/admin-tools/files.css",
  "/ui/tutorials/tutorial.css",
  // Self-hosted third-party libs + fonts (were CDN-loaded before the offline
  // change). Precaching the core ones means the shell renders fully — icons,
  // markdown, terminal, fonts — even when the app is opened as an installed PWA
  // with no server reachable. Prism grammar components (290+ files) are NOT
  // listed here; they're runtime-cached on first use by staleWhileRevalidate.
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

  // App code must be fetched as a coherent set while the server is reachable.
  // A stale-while-revalidate module graph can combine a new importer with an
  // old dependency (for example, importing an export that the cached module
  // does not have), which aborts boot before the UI can recover.
  if (url.origin === self.location.origin && CODE_PATTERN.test(url.pathname)) {
    e.respondWith(networkFirstStatic(request));
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
  const cached = await caches.match(request);
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

async function networkFirstStatic(request) {
  try {
    return await fetchAndCache(request);
  } catch {
    const cached = await caches.match(request);
    return cached || new Response("Offline", { status: 503 });
  }
}

async function networkFirstNavigation(request) {
  try {
    return await fetchAndCache(request);
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    const fallback = await caches.match("/index.html");
    return fallback || new Response("Offline", { status: 503 });
  }
}
