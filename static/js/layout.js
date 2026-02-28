/* ============================================================
   LAYOUT.JS – BA38
   Fichier JS global chargé sur toutes les pages
   ============================================================ */

document.addEventListener("DOMContentLoaded", function () {

    /* ============================================================
       AUTO-FERMETURE DES MESSAGES FLASH
       ============================================================ */
    setTimeout(function () {
        document.querySelectorAll('.alert').forEach(function (alert) {
            if (typeof bootstrap !== "undefined" && bootstrap.Alert) {
                try {
                    new bootstrap.Alert(alert).close();
                } catch (e) {
                    console.warn("Erreur fermeture alert :", e);
                }
            }
        });
    }, 5000);


    /* ============================================================
       INITIALISATION SELECT2 (si présent sur la page)
       ============================================================ */
    function initSelect2() {

        if (
            window.jQuery &&
            window.jQuery.fn &&
            typeof window.jQuery.fn.select2 === "function"
        ) {

            window.jQuery('.select2').each(function () {
                window.jQuery(this).select2({
                    placeholder: "Rechercher un email",
                    allowClear: true,
                    width: '100%'
                });
            });

        } else {
            // Si Select2 pas encore chargé, on réessaie
            setTimeout(initSelect2, 100);
        }
    }

    initSelect2();

});


/* ============================
   BOUTON AIDE
============================ */
const btn = document.getElementById("btnAide");

if (btn) {
    btn.addEventListener("click", function () {

        const page = window.location.pathname.split("/").pop();

        fetch(`/aide/${page}`)
            .then(r => {
                if (!r.ok) throw new Error();
                return r.text();
            })
            .then(md => {

                document.getElementById("aide-content").innerHTML =
                    marked.parse(md);

                new bootstrap.Modal(
                    document.getElementById("aideModal")
                ).show();

            })
            .catch(() => {
                document.getElementById("aide-content").innerHTML =
                    "<p>Aucune aide disponible pour cette page.</p>";

                new bootstrap.Modal(
                    document.getElementById("aideModal")
                ).show();
            });
    });
}


