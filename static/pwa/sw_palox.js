// Service worker — mode hors-ligne de la pesée palox (coupure réseau ou
// panne de Basilic lui-même : le navigateur ne fait pas la différence,
// les deux se traduisent par un échec de fetch()). Voir
// documentation_interne/Securisation_Saisie_Pesee_Palox.docx, solution 4.3.
//
// Portée volontairement limitée à /collecte/ (cf. enregistrement côté
// page avec {scope: '/collecte/'}) : ce service worker ne doit jamais
// intercepter le reste de l'application.
//
// Principe :
// - GET des pages/données palox : réseau en priorité, repli sur le cache
//   si le réseau échoue (la page/le poste ont déjà été ouverts au moins
//   une fois en ligne pour remplir ce cache).
// - POST d'enregistrement d'une pesée : réseau en priorité ; si ça échoue,
//   la pesée est mise de côté dans IndexedDB (numéro de palox attribué
//   localement à partir du dernier "prochain numéro" connu pour cette
//   catégorie) et une réponse JSON de succès est renvoyée à la page,
//   identique à celle du serveur — la page n'a donc rien à changer pour
//   gérer ce cas, l'étiquette s'imprime comme d'habitude.
// - Resynchronisation automatique dès que le réseau revient (événement
//   'sync' si disponible, sinon message 'tenter-sync' envoyé par la page
//   sur son propre événement 'online' + une tentative périodique) : les
//   pesées en attente sont renvoyées avec leur numéro déjà attribué ; en
//   cas de numéro déjà pris entre-temps côté serveur, l'entrée est
//   marquée "conflit" pour vérification manuelle, jamais réessayée toute
//   seule (elle ne doit pas changer de numéro après coup : l'étiquette
//   déjà imprimée porte ce numéro précis).

const CACHE_NAME = 'palox-v1';
const DELAI_RESEAU_MS = 6000; // au-delà, on considère le serveur injoignable
                              // (connexion acceptée mais jamais de réponse)
const DB_NAME = 'palox_offline';
const DB_VERSION = 1;

const RE_PAGE = /^\/collecte\/(\d+)\/palox\/(\d+)$/;
const RE_DONNEES = /^\/collecte\/(\d+)\/palox\/(\d+)\/donnees$/;
const RE_ENREGISTRER = /^\/collecte\/(\d+)\/palox\/(\d+)\/enregistrer$/;
const RE_CATEGORIES = /^\/collecte\/(\d+)\/palox\/categories-actives$/;
const RE_SYNTHESE = /^\/collecte\/(\d+)\/palox\/synthese$/;
const RE_SYNTHESE_DONNEES = /^\/collecte\/(\d+)\/palox\/synthese\/donnees$/;

// La page de synthèse (Tabulator) charge sa bibliothèque JS/CSS depuis un
// CDN externe — sans ça, la page reste blanche hors-ligne même si son HTML
// et ses données sont bien en cache. Réponses "opaques" (cross-origin,
// mode no-cors) : impossibles à inspecter, mais peuvent être mises en
// cache et resservies telles quelles.
const RESSOURCES_CDN = [
  'https://unpkg.com/tabulator-tables@6.3.0/dist/css/tabulator.min.css',
  'https://unpkg.com/tabulator-tables@6.3.0/dist/js/tabulator.min.js',
];

// ── IndexedDB ────────────────────────────────────────────────────────────
function ouvrirDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('queue')) {
        db.createObjectStore('queue', { keyPath: 'id', autoIncrement: true });
      }
      if (!db.objectStoreNames.contains('compteurs')) {
        db.createObjectStore('compteurs', { keyPath: 'categorie_id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx(db, store, mode) {
  return db.transaction(store, mode).objectStore(store);
}

// Prend une FABRIQUE de fetch (pas un fetch déjà lancé) pour pouvoir
// vraiment annuler la requête réseau au bout du délai (AbortController) —
// sans ça, un serveur qui répond juste après le délai (lenteur, pas vraie
// panne) laissait la requête d'origine aboutir quand même côté serveur
// pendant que l'appelant était déjà passé en mode hors-ligne, avec risque de
// double-enregistrement de la même pesée (audit 2026-10-28/29).
function avecDelai(executerFetch) {
  const controleur = new AbortController();
  const promesseFetch = executerFetch(controleur.signal);
  return new Promise((resolve, reject) => {
    const minuteur = setTimeout(() => {
      controleur.abort();
      reject(new Error('délai dépassé'));
    }, DELAI_RESEAU_MS);
    promesseFetch.then(
      (v) => { clearTimeout(minuteur); resolve(v); },
      (e) => { clearTimeout(minuteur); reject(e); },
    );
  });
}

function promesse(requete) {
  return new Promise((resolve, reject) => {
    requete.onsuccess = () => resolve(requete.result);
    requete.onerror = () => reject(requete.error);
  });
}

async function lireCompteur(db, categorieId) {
  const row = await promesse(tx(db, 'compteurs', 'readonly').get(categorieId));
  return row ? row.prochain_numero : null;
}

async function ecrireCompteur(db, categorieId, prochainNumero) {
  await promesse(tx(db, 'compteurs', 'readwrite').put({ categorie_id: categorieId, prochain_numero: prochainNumero }));
}

async function ajouterEnAttente(entree) {
  const db = await ouvrirDB();
  const id = await promesse(tx(db, 'queue', 'readwrite').add(entree));
  await diffuserEtat();
  return id;
}

async function listeAttente() {
  const db = await ouvrirDB();
  return promesse(tx(db, 'queue', 'readonly').getAll());
}

async function supprimerEntree(id) {
  const db = await ouvrirDB();
  await promesse(tx(db, 'queue', 'readwrite').delete(id));
}

async function marquerConflit(id, erreur) {
  const db = await ouvrirDB();
  const store = tx(db, 'queue', 'readwrite');
  const entree = await promesse(store.get(id));
  if (entree) {
    entree.statut = 'conflit';
    entree.erreur = erreur;
    await promesse(store.put(entree));
  }
}

async function diffuserEtat() {
  const attente = await listeAttente();
  const enAttente = attente.filter((e) => e.statut !== 'conflit').length;
  const conflits = attente.filter((e) => e.statut === 'conflit').length;
  const clients = await self.clients.matchAll({ includeUncontrolled: true });
  clients.forEach((c) => c.postMessage({ type: 'palox-etat', enAttente, conflits }));
}

// ── Pré-remplissage du cache (déclenché par message de la page) ──────────
async function precacher(annee, categories) {
  const cache = await caches.open(CACHE_NAME);
  for (const cat of categories) {
    const urlPage = `/collecte/${annee}/palox/${cat.id}`;
    const urlDonnees = `/collecte/${annee}/palox/${cat.id}/donnees`;
    for (const url of [urlPage, urlDonnees]) {
      try {
        const reponse = await fetch(url, { credentials: 'same-origin' });
        if (reponse.ok) {
          await cache.put(url, reponse.clone());
          if (url === urlDonnees) {
            const data = await reponse.json();
            if (data && data.success) {
              const db = await ouvrirDB();
              await ecrireCompteur(db, cat.id, data.prochain_numero);
            }
          }
        }
      } catch (e) { /* page/donnée restera absente du cache si jamais jointe en ligne */ }
    }
  }

  // Page de synthèse (modale de pesée) + son flux de données Tabulator —
  // second point d'entrée confirmé utilisé en parallèle de la saisie
  // par catégorie.
  for (const url of [`/collecte/${annee}/palox/synthese`, `/collecte/${annee}/palox/synthese/donnees`]) {
    try {
      const reponse = await fetch(url, { credentials: 'same-origin' });
      if (reponse.ok) await cache.put(url, reponse.clone());
    } catch (e) { /* idem */ }
  }

  for (const url of RESSOURCES_CDN) {
    try {
      const reponse = await fetch(url, { mode: 'no-cors' });
      await cache.put(url, reponse.clone());
    } catch (e) { /* idem */ }
  }
}

// ── Enregistrement hors-ligne ──────────────────────────────────────────
async function enregistrerHorsLigne(annee, categorieId, corps, csrf) {
  const db = await ouvrirDB();
  const prochain = await lireCompteur(db, categorieId);
  if (prochain === null) {
    // Jamais mise en cache (catégorie ouverte pour la première fois
    // pendant la panne) : impossible de garantir un numéro sans conflit,
    // mieux vaut le signaler clairement que d'inventer un numéro.
    return new Response(JSON.stringify({
      success: false,
      erreur: "Catégorie jamais ouverte en ligne — hors-ligne impossible pour celle-ci, noter la pesée à la main.",
    }), { status: 409, headers: { 'Content-Type': 'application/json' } });
  }

  const numeroPalox = corps.numero_palox || prochain;
  const maintenant = new Date();
  const dateHeure = maintenant.getFullYear() + '-' + String(maintenant.getMonth() + 1).padStart(2, '0') + '-'
    + String(maintenant.getDate()).padStart(2, '0') + ' ' + String(maintenant.getHours()).padStart(2, '0') + ':'
    + String(maintenant.getMinutes()).padStart(2, '0') + ':' + String(maintenant.getSeconds()).padStart(2, '0');

  await ajouterEnAttente({
    annee, categorie_id: categorieId, numero_palox: numeroPalox,
    poids_kg: corps.poids_kg, date_heure: dateHeure, statut: 'en_attente', csrf,
  });
  // Math.max(..., prochain) : un numéro saisi manuellement plus bas que le
  // prochain numéro attendu (correction d'une erreur de frappe pendant une
  // panne, par ex.) ne doit jamais faire reculer le compteur local — sinon
  // les numéros déjà attribués en ligne entre-temps pourraient être
  // réutilisés à la resynchronisation (audit 2026-10-29).
  await ecrireCompteur(db, categorieId, Math.max(numeroPalox + 1, prochain));

  // Données de catégorie pour l'étiquette : reprises du cache (déjà
  // posées par precacher()), à défaut valeurs minimales.
  let categorie = { libelle: '', code_vif: '', libelle_vif: '' };
  try {
    const cache = await caches.open(CACHE_NAME);
    const reponseCache = await cache.match(`/collecte/${annee}/palox/${categorieId}/donnees`);
    if (reponseCache) {
      const data = await reponseCache.json();
      if (data && data.categorie) categorie = data.categorie;
    }
  } catch (e) { /* étiquette avec libellé vide plutôt que bloquer l'impression */ }

  return new Response(JSON.stringify({
    success: true,
    numero_palox: numeroPalox,
    prochain_numero: numeroPalox + 1,
    poids_kg: corps.poids_kg,
    date_heure: dateHeure,
    categorie,
    annee,
    hors_ligne: true,
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

// Au-delà de ce nombre de tentatives ambiguës consécutives (ni succès net,
// ni refus net — ex. réponse non-JSON type page de connexion après session
// expirée), on bascule en conflit pour que l'opérateur soit enfin alerté,
// plutôt que de retenter silencieusement à l'infini sans jamais le signaler
// (audit 2026-10-28).
const TENTATIVES_AVANT_CONFLIT = 5;

async function incrementerTentatives(id, tentatives) {
  const db = await ouvrirDB();
  const store = tx(db, 'queue', 'readwrite');
  const entree = await promesse(store.get(id));
  if (entree) {
    entree.tentatives = tentatives;
    await promesse(store.put(entree));
  }
}

// ── Resynchronisation ───────────────────────────────────────────────────
// Garde anti-réentrance : l'événement 'online', le setInterval périodique et
// le retour de visibilité d'onglet peuvent chacun déclencher un message
// 'tenter-sync' à quelques millisecondes d'écart — sans ce verrou, deux
// passages concurrents de tenterSync() pouvaient tous les deux tenter
// d'envoyer la MÊME pesée en attente (POST dupliqué) et écrire sur le
// compteur de tentatives sans séquencement (audit 2026-10-29). Si une
// nouvelle demande arrive pendant qu'un passage est déjà en cours, elle est
// mémorisée puis rejouée juste après, plutôt que silencieusement perdue.
let syncEnCours = false;
let syncRedemandee = false;

async function tenterSync() {
  if (syncEnCours) { syncRedemandee = true; return; }
  syncEnCours = true;
  try {
    await tenterSyncInterne();
  } finally {
    syncEnCours = false;
    if (syncRedemandee) {
      syncRedemandee = false;
      tenterSync();
    }
  }
}

async function tenterSyncInterne() {
  const attente = await listeAttente();
  for (const entree of attente) {
    if (entree.statut === 'conflit') continue;
    try {
      // avecDelai() : sans timeout, une connexion qui pend (ex. portail
      // captif) bloquait cet await indéfiniment, empêchant toute tentative
      // pour le reste de la file d'attente (audit 2026-10-28).
      const reponse = await avecDelai((signal) => fetch(`/collecte/${entree.annee}/palox/${entree.categorie_id}/enregistrer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': entree.csrf || '' },
        body: JSON.stringify({ poids_kg: entree.poids_kg, numero_palox: entree.numero_palox }),
        credentials: 'same-origin',
        signal,
      }));
      const data = await reponse.json().catch(() => null);
      if (reponse.ok && data && data.success) {
        await supprimerEntree(entree.id);
      } else if (data && !data.success) {
        // Refus net du serveur (ex. numéro déjà pris) : à vérifier à la
        // main, ne jamais réessayer tout seul avec un autre numéro — cf.
        // point 2 du document (l'étiquette déjà imprimée porte ce numéro).
        await marquerConflit(entree.id, data.erreur || 'Refusé par le serveur');
      } else {
        // Ambigu (ex. réponse HTML de connexion après session/CSRF expirée,
        // non-JSON) : quelques essais au cas où ce serait transitoire, puis
        // conflit explicite plutôt qu'une attente silencieuse indéfinie.
        const tentatives = (entree.tentatives || 0) + 1;
        if (tentatives >= TENTATIVES_AVANT_CONFLIT) {
          await marquerConflit(entree.id, `Échec répété (${tentatives} tentatives, réponse inattendue du serveur) — reconnectez-vous puis vérifiez manuellement`);
        } else {
          await incrementerTentatives(entree.id, tentatives);
        }
      }
    } catch (e) {
      break; // toujours hors-ligne : inutile d'essayer les suivantes maintenant
    }
  }
  await diffuserEtat();
}

// ── Cycle de vie ─────────────────────────────────────────────────────────
self.addEventListener('install', (event) => { self.skipWaiting(); });
self.addEventListener('activate', (event) => { event.waitUntil(self.clients.claim()); });

self.addEventListener('message', (event) => {
  const msg = event.data || {};
  if (msg.type === 'precacher') {
    event.waitUntil(precacher(msg.annee, msg.categories));
  } else if (msg.type === 'tenter-sync') {
    event.waitUntil(tenterSync());
  } else if (msg.type === 'etat') {
    event.waitUntil(diffuserEtat());
  } else if (msg.type === 'lister-attente') {
    event.waitUntil(listeAttente().then((attente) => {
      event.source.postMessage({ type: 'palox-liste-attente', attente });
    }));
  } else if (msg.type === 'supprimer-attente') {
    event.waitUntil(supprimerEntree(msg.id).then(diffuserEtat));
  } else if (msg.type === 'maj-compteur') {
    // Tient le compteur local à jour en continu après CHAQUE pesée réussie
    // en ligne (pas seulement au chargement de page ou toutes les 45 s) —
    // sans ça, une série de pesées en ligne via la modale (qui ne recharge
    // jamais la page) laisse le compteur hors-ligne en retard, et c'est ce
    // retard, pas un second poste, qui provoque un conflit de numéro si le
    // réseau tombe juste après.
    event.waitUntil(ouvrirDB().then((db) => ecrireCompteur(db, msg.categorie_id, msg.prochain_numero)));
  } else if (msg.type === 'lire-compteur') {
    // Permet à la page d'afficher le numéro suggéré le plus à jour possible
    // (ce compteur interne, tenu à jour en continu) plutôt que de se fier
    // uniquement à la réponse GET /donnees, qui peut être servie depuis le
    // cache et donc périmée une fois hors-ligne.
    event.waitUntil(
      ouvrirDB()
        .then((db) => lireCompteur(db, msg.categorie_id))
        .then((prochain) => {
          if (event.ports && event.ports[0]) event.ports[0].postMessage({ prochain_numero: prochain });
        })
    );
  }
});

self.addEventListener('sync', (event) => {
  if (event.tag === 'palox-sync') {
    event.waitUntil(tenterSync());
  }
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const chemin = url.pathname;

  if (event.request.method === 'POST' && RE_ENREGISTRER.test(chemin)) {
    const [, annee, categorieId] = chemin.match(RE_ENREGISTRER);
    event.respondWith((async () => {
      const corpsTexte = await event.request.clone().text();
      let corps = {};
      try { corps = JSON.parse(corpsTexte); } catch (e) {}
      const csrf = event.request.headers.get('X-CSRFToken') || '';
      try {
        const reponse = await avecDelai((signal) => fetch(event.request.clone(), { signal }));
        return reponse;
      } catch (e) {
        return enregistrerHorsLigne(parseInt(annee, 10), parseInt(categorieId, 10), corps, csrf);
      }
    })());
    return;
  }

  if (event.request.method !== 'GET') return; // jamais d'autre POST intercepté (corriger/supprimer/réinitialiser)

  const estPage = RE_PAGE.test(chemin);
  const estDonnees = RE_DONNEES.test(chemin);
  const estCategories = RE_CATEGORIES.test(chemin);
  const estSynthese = RE_SYNTHESE.test(chemin);
  const estSyntheseDonnees = RE_SYNTHESE_DONNEES.test(chemin);
  const estCdnTabulator = RESSOURCES_CDN.includes(event.request.url);
  if (!estPage && !estDonnees && !estCategories && !estSynthese && !estSyntheseDonnees && !estCdnTabulator) return;

  event.respondWith((async () => {
    try {
      const reponse = await avecDelai((signal) => fetch(event.request, { signal }));
      // reponse.ok est TOUJOURS false pour une réponse "opaque" (CDN
      // cross-origin, mode no-cors) : on la met quand même en cache.
      if (reponse && (reponse.ok || reponse.type === 'opaque')) {
        const cache = await caches.open(CACHE_NAME);
        cache.put(event.request, reponse.clone());
      }
      return reponse;
    } catch (e) {
      const cache = await caches.open(CACHE_NAME);
      const depuisCache = await cache.match(event.request);
      if (depuisCache) return depuisCache;
      throw e;
    }
  })());
});
