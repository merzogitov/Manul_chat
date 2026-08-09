const CACHE_NAME = "manul-chat-pwa-v1";

const STATIC_ASSETS = [
  "/static/style.css?v=20260809-prod1",
  "/static/image/logo_min.png?v=20260809-prod1",
  "/static/image/logo_max.png?v=20260809-prod1",
  "/static/image/pwa-192.png",
  "/static/image/pwa-512.png",
  "/static/image/apple-touch-icon.png"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(key => key !== CACHE_NAME)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== "GET") return;

  if (
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/ws") ||
    url.pathname.startsWith("/attachment/") ||
    url.pathname === "/" ||
    url.pathname === "/login" ||
    url.pathname === "/chat" ||
    url.pathname === "/admin"
  ) {
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then(cached => {
        if (cached) return cached;

        return fetch(request).then(response => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
          return response;
        });
      })
    );
  }
});
