/* ============================================================
   LAYOUT.JS – BA38
   Fichier JS global chargé sur toutes les pages
   ============================================================ */


/* ============================================================
    AUTO-FERMETURE DES MESSAGES FLASH
============================================================ */

document.addEventListener("DOMContentLoaded", function () {

    setTimeout(function () {

        document.querySelectorAll('.alert').forEach(function (alert) {

            // On ne ferme PAS les erreurs importantes
            if (alert.classList.contains('alert-danger') ||
                alert.classList.contains('alert-warning')) {
                return;
            }

            if (bootstrap.Alert) {
                new bootstrap.Alert(alert).close();
            }

        });

    }, 5000);

});


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


document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
    new bootstrap.Tooltip(el);
});

function initTooltips() {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
        new bootstrap.Tooltip(el);
    });
}

document.addEventListener("DOMContentLoaded", initTooltips);