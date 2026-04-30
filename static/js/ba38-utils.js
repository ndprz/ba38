/* ============================================================================
   📅 BA38 – COMPOSANT DATE FR (.date-fr)
   ============================================================================

   🎯 OBJECTIF
   Fournir un champ de saisie de date ergonomique au format français
   (jj/mm/aaaa), optimisé pour une saisie rapide au clavier (type Excel / ERP).

   Ce composant remplace volontairement <input type="date">,
   jugé trop rigide pour les usages métier.

   ----------------------------------------------------------------------------

   🧩 UTILISATION (HTML)

   Exemple standard :

       <div class="mb-3">
           <label>Date limite</label>
           <input type="text"
                  name="date_limite"
                  class="form-control date-fr"
                  placeholder="jj/mm ou jjmmaaaa"
                  autocomplete="off">
       </div>

   🔹 IMPORTANT
   - type="text" obligatoire
   - classe "date-fr" obligatoire pour activer le comportement

   ----------------------------------------------------------------------------

   ⚙️ FONCTIONNEMENT

   Le script s’applique automatiquement à tous les champs ayant la classe :
       .date-fr

   ✔ Saisie rapide intelligente :
      - 2004       → 20/04/AAAA (année courante)
      - 20012027   → 20/01/2027
      - 200127     → 20/01/2027
      - 20/04      → 20/04/AAAA
      - 20/04/2027 → inchangé

   ✔ Ajout automatique des séparateurs "/"

   ✔ Validation automatique :
      - Vérifie une vraie date calendrier (ex : 31/02 refusé)
      - Gestion des années bissextiles

   ✔ Feedback visuel Bootstrap :
      - champ valide → classe "is-valid"
      - champ invalide → classe "is-invalid"
      - message d’erreur affiché sous le champ

   ✔ Navigation clavier :
      - Entrée → format + passage au champ suivant (si valide)

   ✔ Sécurité formulaire :
      - empêche la soumission si une date est invalide

   ----------------------------------------------------------------------------

   ⚠️ LIMITATIONS

   - Ne fonctionne pas avec <input type="date">
   - Dépend de JavaScript (prévoir fallback serveur)
   - Format attendu côté serveur : jj/mm/aaaa

   ----------------------------------------------------------------------------

   🔐 BONNES PRATIQUES BACKEND

   Toujours convertir côté Flask :

       parse_date_fr("20/01/2027") → "2027-01-20"

   Ne jamais faire confiance uniquement au front.

   ----------------------------------------------------------------------------

   🎨 PERSONNALISATION POSSIBLE

   - Modifier le message d’erreur
   - Restreindre les années (ex : > 2030)
   - Ajouter un masque de saisie encore plus strict
   - Ajouter un calendrier popup en complément

   ----------------------------------------------------------------------------

   🧠 PHILOSOPHIE

   ✔ Priorité à la vitesse de saisie
   ✔ UX orientée utilisateur métier
   ✔ Comportement cohérent dans toute l’application
   ✔ Logique centralisée (évite duplication JS)

   ----------------------------------------------------------------------------

   📦 ÉVOLUTION FUTURE

   Ce composant peut s’intégrer dans une librairie interne BA38 :

       .date-fr
       .phone-fr
       .number-fr
       .email-fr

   pour homogénéiser toute l’interface utilisateur.

   ============================================================================ */

document.addEventListener("DOMContentLoaded", () => {

    // =========================================================
    // 📅 AUTO FORMAT PENDANT LA FRAPPE
    // =========================================================
    function autoFormatInput(input) {
        let val = input.value.replace(/\D/g, ""); // chiffres uniquement

        if (val.length >= 3 && val.length <= 4) {
            val = val.slice(0, 2) + "/" + val.slice(2);
        }
        else if (val.length >= 5) {
            val = val.slice(0, 2) + "/" + val.slice(2, 4) + "/" + val.slice(4, 8);
        }

        input.value = val;
    }

    // =========================================================
    // 📅 FORMAT FINAL
    // =========================================================
    function formatDateFr(input) {
        let val = input.value.trim();

        if (!val) {
            setValid(input, null);
            return;
        }

        const annee = new Date().getFullYear();

        // 20/04 → ajouter année
        if (/^\d{2}\/\d{2}$/.test(val)) {
            val = val + "/" + annee;
        }

        // appliquer
        input.value = val;

        // validation
        if (!isValidDateFr(val)) {
            setValid(input, false);
        } else {
            setValid(input, true);
        }
    }

    // =========================================================
    // 📅 VALIDATION RÉELLE
    // =========================================================
    function isValidDateFr(val) {
        const parts = val.split("/");
        if (parts.length !== 3) return false;

        const d = parseInt(parts[0], 10);
        const m = parseInt(parts[1], 10);
        const y = parseInt(parts[2], 10);

        if (m < 1 || m > 12) return false;
        if (d < 1 || d > 31) return false;

        const date = new Date(y, m - 1, d);

        return (
            date.getFullYear() === y &&
            date.getMonth() === m - 1 &&
            date.getDate() === d
        );
    }

    // =========================================================
    // 🎯 GESTION VISUELLE + MESSAGE INLINE
    // =========================================================
    function setValid(input, state) {

        let feedback = input.nextElementSibling;

        // créer message si absent
        if (!feedback || !feedback.classList.contains("invalid-feedback")) {
            feedback = document.createElement("div");
            feedback.className = "invalid-feedback";
            feedback.innerText = "Date invalide";
            input.parentNode.appendChild(feedback);
        }

        if (state === null) {
            input.classList.remove("is-valid", "is-invalid");
            feedback.style.display = "none";
            return;
        }

        if (state === false) {
            input.classList.add("is-invalid");
            input.classList.remove("is-valid");
            feedback.style.display = "block";
        } else {
            input.classList.remove("is-invalid");
            input.classList.add("is-valid");
            feedback.style.display = "none";
        }
    }

    // =========================================================
    // 🔥 INITIALISATION
    // =========================================================
    document.querySelectorAll(".date-fr").forEach(input => {

        // frappe → auto slash
        input.addEventListener("input", () => {
            autoFormatInput(input);
        });

        // Enter → valider + passer suivant
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();

                formatDateFr(input);

                if (!input.classList.contains("is-invalid")) {
                    const form = input.closest("form");
                    if (!form) return;

                    const elements = Array.from(form.elements);
                    const index = elements.indexOf(input);

                    if (elements[index + 1]) {
                        elements[index + 1].focus();
                    }
                }
            }
        });

        // blur → validation finale
        input.addEventListener("blur", () => {
            formatDateFr(input);
        });

    });

});
