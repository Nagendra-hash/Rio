/* JARVIS dashboard service worker.
 *
 * Deliberately a PASS-THROUGH worker: it intercepts fetches but never serves
 * from a cache. The dashboard is a live control surface — a stale HTML/JS
 * bundle would be worse than an offline error — and every API call needs the
 * current bearer token, which caching would silently break.
 *
 * Why have it at all? Two reasons:
 *   1. Installability — Chrome's PWA criteria require a fetch handler with a
 *      valid response fallback before offering "Install app".
 *   2. Keeping the SW alive while the app is installed keeps the browser
 *      process (and its audio focus) warm; installed standalone apps get
 *      backgrounding treatment closer to a native app than a tab does.
 */

const SW_VERSION = 'jarvis-sw-v1';

self.addEventListener('install', (event) => {
  // No precache — nothing is worth serving stale.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  // Clear anything an older, caching version might have left behind.
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Only same-origin GETs are even considered; everything else passes untouched.
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Network-first with a tiny same-session cache only for immutable static
  // assets (icons, vendored JS libs). Never for HTML, APIs or websockets.
  const isImmutableAsset =
    url.pathname.startsWith('/static/icons/') ||
    url.pathname === '/static/lightweight-charts.standalone.js' ||
    url.pathname === '/static/crypto.js';

  if (isImmutableAsset) {
    event.respondWith(
      caches.open(SW_VERSION).then(async (cache) => {
        const hit = await cache.match(req);
        if (hit) return hit;
        try {
          const resp = await fetch(req);
          if (resp.ok) cache.put(req, resp.clone());
          return resp;
        } catch (e) {
          return hit || Response.error();
        }
      })
    );
    return;
  }

  // Everything else: straight to the network, passthrough.
  event.respondWith(fetch(req));
});
