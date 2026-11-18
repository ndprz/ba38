/* Listes de bénévoles remplies à l'ouverture (page Gestion du planning ramasse).
 *
 * Avant : chaque liste contenait tous les bénévoles (43 000 <option>, 10,9 Mo de HTML).
 * Maintenant : le gabarit n'envoie que « -- Aucun -- », « ➕ Ajouter un bénévole… » et la personne affectée ;
 * la liste complète est construite ici, une seule fois par liste, au focus / clic / toucher, à partir de
 * window.BENE_DATA (voir ba38_planning/bene_select_data.py). Le résultat est identique à l'ancien gabarit :
 * mêmes options, même ordre, mêmes mentions et couleurs, même info-bulle.
 *
 * Attributs lus sur chaque <select class="bene-select" data-lazy="..."> :
 *   data-lazy      « groupes » (prioritaires ✅ puis autres) ou « simple » (liste unique, ligne d'ajout)
 *   data-liste     clé de BENE_DATA.roles (chauffeur, equipier, responsable, tri_externe)
 *   data-actif     id de la personne affectée (remplaçant s'il y en a un, sinon titulaire)
 *   data-titulaire / data-remp / data-absent / data-jour / data-sans-affecte : états à afficher
 */
(function () {
  "use strict";
  var D = window.BENE_DATA;
  if (!D) { return; }

  var VERT = "background-color:#d4edda;color:#155724;";
  var GRIS = "color:gray;";
  var ORANGE = "color:orange;";
  var SEPARATEUR = "──────────";

  function nom(id) { var b = D.bene[String(id)]; return b ? b[0] : ""; }
  function titre(id) { var b = D.bene[String(id)]; return b ? "📞 " + b[1] + " | ✉️ " + b[2] : ""; }

  // style + mention d'un bénévole dans CETTE liste (mêmes règles que l'ancien gabarit)
  function etat(sel, id) {
    var ds = sel.dataset, sid = String(id);
    if (ds.remp && sid === ds.remp) { return { style: VERT, mention: " (remplaçant)" }; }
    if (ds.absent === "1" && sid === ds.titulaire) { return { style: GRIS, mention: " (absent)" }; }
    if (!ds.sansAffecte && ds.jour && sid !== ds.actif) {
      var ids = ((D.affectes || {})[ds.role] || {})[ds.jour] || [];
      if (ids.indexOf(Number(id)) !== -1) { return { style: ORANGE, mention: " (déjà affecté)" }; }
    }
    return { style: "", mention: "" };
  }

  function creerOption(valeur, texte) {
    var o = document.createElement("option");
    o.value = valeur; o.textContent = texte;
    return o;
  }
  // sans attribut value, comme l'ancien <option disabled>──────────</option>
  function separateur() { var o = document.createElement("option"); o.disabled = true; o.textContent = SEPARATEUR; return o; }

  function optionBenevole(sel, id, prioritaire, avecEtat) {
    var o = creerOption(id, "");
    var e = avecEtat ? etat(sel, id) : { style: "", mention: "" };
    if (avecEtat) { o.title = titre(id); }   // la ligne d'ajout (liste « simple ») n'avait pas d'info-bulle
    if (e.style) { o.setAttribute("style", e.style); }
    o.textContent = (prioritaire ? "✅ " : "") + nom(id) + e.mention;
    if (sel.dataset.actif && String(id) === sel.dataset.actif) { o.selected = true; }
    return o;
  }

  function entetes(sel) {
    var aucun = creerOption("", "-- Aucun --");
    if (!sel.dataset.actif && sel.dataset.lazy !== "simple") { aucun.selected = true; }
    return [aucun, creerOption("__add__", "➕ Ajouter un bénévole…")];
  }

  // complet = false : « Aucun », « Ajouter » et la personne affectée seulement (état de la page au chargement)
  // complet = true  : la liste entière, identique à l'ancien gabarit
  function remplir(sel, complet) {
    var liste = D.roles[sel.dataset.liste] || [];
    var simple = sel.dataset.lazy === "simple";
    var avecEtat = !simple;
    var frag = document.createDocumentFragment();
    entetes(sel).forEach(function (o) { frag.appendChild(o); });

    if (!complet) {
      if (!simple && sel.dataset.actif) {
        liste.forEach(function (e) {
          if (String(e[0]) === sel.dataset.actif) { frag.appendChild(optionBenevole(sel, e[0], e[1] === 1, true)); }
        });
      }
    } else if (simple) {
      frag.appendChild(separateur());
      liste.forEach(function (e) { frag.appendChild(optionBenevole(sel, e[0], false, false)); });
    } else {
      frag.appendChild(separateur());
      liste.forEach(function (e) { if (e[1] === 1) { frag.appendChild(optionBenevole(sel, e[0], true, true)); } });
      frag.appendChild(separateur());
      liste.forEach(function (e) { if (e[1] !== 1) { frag.appendChild(optionBenevole(sel, e[0], false, true)); } });
    }
    sel.textContent = "";
    sel.appendChild(frag);
  }

  var declencheurs = ["focus", "mousedown", "touchstart", "keydown"];
  function remplirComplet(sel) {
    if (sel.dataset.rempli === "1") { return; }
    sel.dataset.rempli = "1";
    // On garde la valeur courante et les options ajoutées à la volée (bénévole créé par « ➕ Ajouter » avant
    // l'ouverture de la liste : il n'est pas dans BENE_DATA et ne doit pas disparaître).
    var courante = sel.value;
    var connus = {};
    (D.roles[sel.dataset.liste] || []).forEach(function (e) { connus[String(e[0])] = true; });
    var ajoutes = Array.prototype.filter.call(sel.options, function (o) {
      return o.value && o.value !== "__add__" && !connus[o.value];
    });
    remplir(sel, true);
    ajoutes.forEach(function (o) { sel.appendChild(o); });
    if (courante && courante !== "__add__") { sel.value = courante; }
    declencheurs.forEach(function (ev) { sel.removeEventListener(ev, surOuverture, true); });
  }
  function surOuverture(e) { remplirComplet(e.currentTarget); }

  function init() {
    document.querySelectorAll("select.bene-select[data-lazy]").forEach(function (sel) {
      remplir(sel, false);
      declencheurs.forEach(function (ev) { sel.addEventListener(ev, surOuverture, true); });
    });
  }

  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", init); } else { init(); }
  window.BeneLazy = { remplir: remplir, remplirComplet: remplirComplet };
})();
