/* webAgent Service Worker — v2
 *
 * Strategy:
 *   - App shell (HTML, CSS, JS, icons): precache on install
 *   - API calls (/api/*): network-only (never cache stale data)
 *   - CDN assets: stale-while-revalidate
 *   - Static assets (JS/CSS/etc.): stale-while-revalidate — serve cached for an
 *     instant load, but always revalidate in the background so a code change
 *     shows up on the next reload (cache-first never revalidated, which left
 *     stale JS pinned until the cache name changed — a dev-workflow trap).
 *   - Navigation: network-first, fall back to cached index.html
 *
 * Bump CACHE on each release so the activate handler drops the prior cache.
 */

const CACHE = "webagent-v113";
const STATIC_PATTERN = /\.(css|js|json|svg|png|ico|woff2?)$/;
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
  "/ui/main-panel/agents/agent-loop/loop.css",
  "/ui/main-panel/agents/agent-loop/loop-visual.css",
  "/ui/main-panel/canvas/canvas.css",
  "/ui/main-panel/agents/agents.css",
  "/ui/main-panel/admin-tools/files.css",
  "/ui/tutorials/tutorial.css",
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

  // Static assets — stale-while-revalidate (fast load + picks up code changes)
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
