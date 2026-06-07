const CACHE_VERSION = "bookhub-v4";

self.addEventListener("install", (e) => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Never intercept API calls or non-GET requests.
  if (url.pathname.startsWith("/api") || e.request.method !== "GET") return;

  // Network-first for all navigations and static assets so deploys propagate.
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
