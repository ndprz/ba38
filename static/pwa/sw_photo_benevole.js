// Service worker minimal : rend la page "Photo bénévole" installable
// (raccourci sur l'écran d'accueil) sans mise en cache hors-ligne, car
// chaque page nécessite une session et un jeton CSRF à jour.

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});
