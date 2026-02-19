document.addEventListener("DOMContentLoaded", function() {

    /* ============================
       AUTO-FERMETURE FLASH
    ============================ */
    setTimeout(function () {
        document.querySelectorAll('.alert').forEach(function (alert) {
            if (bootstrap.Alert) {
                new bootstrap.Alert(alert).close();
            }
        });
    }, 5000);


    /* ============================
       BOUTON AIDE
    ============================ */
    const btn = document.getElementById("btnAide");

    if (btn) {
        btn.addEventListener("click", function() {

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

});
