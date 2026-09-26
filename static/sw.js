// Groove service worker: pequeño y conservador.
//
// - Solo cachea la "carcasa" estática (CSS, JS, iconos y la página de "sin
//   conexión"). Nada dinámico: ni alarmas, ni /playback/state, ni Spotify, ni
//   OAuth, ni formularios POST.
// - Estáticos: primero la red; la caché solo se usa si la red falla.
// - Páginas: siempre la red. Si la Raspberry no responde, se muestra una página
//   honesta de "No hay conexión con Groove" (no hay modo offline falso).
// - El resto de peticiones no se interceptan (van a la red como siempre).

var CACHE = "groove-shell-v1";
var SHELL = [
  "/static/style.css",
  "/static/app.js",
  "/static/offline.html",
  "/static/icons/groove.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png"
];
var OFFLINE_PAGE = "/static/offline.html";

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) { return cache.addAll(SHELL); })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (key) { return key !== CACHE; })
        .map(function (key) { return caches.delete(key); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;  // POST (alarmas, STOP...) nunca se tocan
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Navegación: siempre red; sin red, la página de "sin conexión".
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(function () { return caches.match(OFFLINE_PAGE); })
    );
    return;
  }

  // Solo los estáticos de la carcasa: red primero, caché como respaldo.
  if (SHELL.indexOf(url.pathname) !== -1) {
    event.respondWith(
      fetch(request).then(function (response) {
        if (response.ok) {
          var copy = response.clone();
          caches.open(CACHE).then(function (cache) { cache.put(url.pathname, copy); });
        }
        return response;
      }).catch(function () { return caches.match(url.pathname); })
    );
  }
  // Cualquier otra cosa (/playback/state, /spotify/..., /manifest...): sin intervenir.
});
