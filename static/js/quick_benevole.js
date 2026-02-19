/**
 * quick_benevole.js
 * Ajout rapide de bénévole – version propre (AJAX uniquement)
 */

let lastTriggeredSelect = null;

/* =========================================================
   Détection du choix "➕ Ajouter un bénévole…"
========================================================= */
document.addEventListener("change", function (e) {

  const select = e.target;

  if (!select.classList.contains("bene-select")) return;

  if (select.value === "__add__") {
    select.value = "";
    lastTriggeredSelect = select;

    const role = select.dataset.role || "";
    ouvrirModaleAjoutBenevole(role);
  }
});

/* =========================================================
   Marquer le planning comme modifié
========================================================= */
function markPlanningDirty() {
  const warning = document.getElementById("unsaved-warning");
  const saveButtons = document.querySelectorAll(".save-btn");

  saveButtons.forEach(btn => btn.disabled = false);
  if (warning) warning.style.display = "block";
}

/* =========================================================
   Ouverture de la modale
========================================================= */
function ouvrirModaleAjoutBenevole(role) {
  const modalEl = document.getElementById("quickBenevoleModal");
  if (!modalEl) return;

  modalEl.querySelector("#qb_role").value = role;
  new bootstrap.Modal(modalEl).show();
}

/* =========================================================
   Soumission AJAX (AUCUN submit HTML)
========================================================= */
document.addEventListener("DOMContentLoaded", () => {

  const modalEl = document.getElementById("quickBenevoleModal");
  const submitBtn = document.getElementById("qb_submit");

  if (!modalEl || !submitBtn) return;

  submitBtn.addEventListener("click", async function () {

    const apiUrl = modalEl.dataset.apiUrl;

    const data = {
      nom: document.getElementById("qb_nom").value.trim(),
      prenom: document.getElementById("qb_prenom").value.trim(),
      civilite: document.getElementById("qb_civilite").value,
      type_benevole: document.getElementById("qb_type").value,
      telephone_portable: document.getElementById("qb_tel").value.trim(),
      role: document.getElementById("qb_role").value
    };

    console.log("QB submit clicked");
    console.log("API URL =", apiUrl);
    console.log("DATA =", data);

    if (!data.nom || !data.prenom || !data.civilite || !data.type_benevole) {
      alert("Merci de renseigner tous les champs obligatoires.");
      return;
    }

    let res;
    try {
      res = await fetch(apiUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data)
      });
    } catch (err) {
      alert("Erreur réseau lors de la création du bénévole.");
      return;
    }

    const result = await res.json();

    if (!result.success) {
      alert(result.error || "Erreur création bénévole");
      return;
    }

    /* =====================================================
       Injection du bénévole créé dans le select d’origine
    ===================================================== */
    if (lastTriggeredSelect) {
      const opt = document.createElement("option");
      opt.value = result.id;
      opt.textContent = `${result.nom} ${result.prenom}`;
      opt.selected = true;

      lastTriggeredSelect.appendChild(opt);
      lastTriggeredSelect.value = result.id;
    }

    /* =====================================================
       Marquer le planning comme modifié
    ===================================================== */
    markPlanningDirty();

    /* =====================================================
       Fermeture + reset
    ===================================================== */
    bootstrap.Modal.getInstance(modalEl).hide();
    document.getElementById("quickBenevoleForm").reset();
  });
});
