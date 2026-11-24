/* Recherche dans une liste de bénévoles (planning ESOPE).
 *
 * esopeRechercheSelect(select) ajoute au-dessus de la liste un champ de recherche : on tape un morceau du
 * prénom et/ou du nom, dans n'importe quel ordre (« juliette gou » trouve « GOUCHAULT Juliette »), sans tenir
 * compte des accents ni des majuscules. Les listes remplies après coup (fenêtres d'ajout) appellent
 * select._rafraichir() pour reprendre leur contenu.
 */
(function () {
  "use strict";
  function norm(t) { return (t || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase(); }

  window.esopeRechercheSelect = function (select) {
    if (select._recherche) { return select._recherche; }
    var champ = document.createElement("input");
    champ.type = "search"; champ.className = "form-control mb-1"; champ.placeholder = "🔍 Rechercher par prénom ou nom…";
    champ.autocomplete = "off";
    select.parentNode.insertBefore(champ, select);
    select.size = 8;
    var copie = [];   // [{groupe: label|null, options: [{value, text, selected}]}]

    function lire() {
      copie = [];
      Array.prototype.forEach.call(select.children, function (el) {
        if (el.tagName === "OPTGROUP") {
          copie.push({groupe: el.label, options: Array.prototype.map.call(el.children, function (o) { return {value: o.value, text: o.textContent, selected: o.selected}; })});
        } else {
          copie.push({groupe: null, options: [{value: el.value, text: el.textContent, selected: el.selected}]});
        }
      });
    }
    function afficher() {
      var mots = norm(champ.value).split(/\s+/).filter(Boolean);
      var precedent = select.value;
      select.innerHTML = "";
      var premier = null;
      copie.forEach(function (g) {
        var ok = g.options.filter(function (o) {
          if (o.value === "") { return mots.length === 0; }
          var t = norm(o.text);
          return mots.every(function (m) { return t.indexOf(m) !== -1; });
        });
        if (!ok.length) { return; }
        var parent = select;
        if (g.groupe !== null) { parent = document.createElement("optgroup"); parent.label = g.groupe; select.appendChild(parent); }
        ok.forEach(function (o) {
          var opt = document.createElement("option"); opt.value = o.value; opt.textContent = o.text;
          if (o.value !== "" && o.value === precedent) { opt.selected = true; }
          parent.appendChild(opt);
          if (premier === null && o.value !== "") { premier = opt; }
        });
      });
      if (mots.length && premier && !select.value) { premier.selected = true; }   // premier résultat présélectionné
    }
    champ.addEventListener("input", afficher);
    champ.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); if (select.form) { select.form.requestSubmit(); } }
      if (e.key === "ArrowDown") { e.preventDefault(); select.focus(); }
    });
    select._rafraichir = function () { champ.value = ""; lire(); afficher(); };
    select._recherche = champ;
    select._rafraichir();
    return champ;
  };
})();
