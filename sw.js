/* webAgent Service Worker — v1
 *
 * Strategy:
 *   - App shell (HTML, CSS, JS, icons): cache on install, serve from cache
 *   - API calls (/api/*): network-only (never cache stale data)
 *   - CDN assets: stale-while-revalidate
 *   - All other static assets: cache-first
 *   - Navigation: network-first, fall back to cached index.html
 */

const CACHE = "webagent-v1";
const STATIC_PATTERN = /\.(css|js|json|svg|png|ico|woff2?)$/;
const CDN_PATTERN = /^(https?:)?\/\/(fonts\.googleapis|cdn\.jsdelivr|unpkg)\./;
const API_PATTERN = /^\/api\//;
const WS_PATTERN = /^\/api\/v1\/agent\/ws/;

/* ── Install: precache the app shell ── */
self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((cache) =>
      cache.addAll([
        "/",
        "/index.html",
        "/ui/manifest.json",
        "/ui/favicon.svg",
        "/ui/icons/icon-192x192.png",
        "/ui/icons/icon-192x192-maskable.png",
        "/ui/icons/icon-512x512.png",
        "/ui/icons/icon-512x512-maskable.png",
        "/ui/css/app1.css",
        "/ui/css/app2.css",
        "/ui/css/app3.css",
        "/ui/css/loop.css",
        "/ui/css/loop-visual.css",
        "/ui/css/autoagent.css",
        "/ui/css/agents.css",
        "/ui/css/files.css",
        "/ui/css/tutorial.css",
        "/ui/css/design-system.css",
      ])
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

  // CDN assets — stale-while-revalidate
  if (CDN_PATTERN.test(url.hostname)) {
    e.respondWith(staleWhileRevalidate(request));
    return;
  }

  // Static assets — cache-first
  if (STATIC_PATTERN.test(url.pathname)) {
    e.respondWith(cacheFirst(request));
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

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then((cache) => cache.put(request, copy));
    }
    return response;
  } catch {
    return new Response("Offline", { status: 503 });
  }
}

async function staleWhileRevalidate(request) {
  const cached = await caches.match(request);
  const fetchPromise = fetch(request)
    .then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
      }
      return response;
    })
    .catch(() => null);
  return cached || (await fetchPromise) || new Response("Offline", { status: 503 });
}

async function networkFirstNavigation(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then((cache) => cache.put(request, copy));
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    const fallback = await caches.match("/index.html");
    return fallback || new Response("Offline", { status: 503 });
  }
}
