# ba38_planning/bene_select_data.py
"""Données des listes de bénévoles des pages de gestion du planning, envoyées UNE SEULE FOIS au navigateur.

Avant : chaque liste déroulante (150 sur la page ramasse) contenait tous les bénévoles ->
43 000 <option>, 10,9 Mo de HTML. Maintenant : la page ne contient que « -- Aucun -- », « ➕ Ajouter »
et la personne affectée ; la liste complète est construite à l'ouverture par static/js/bene_select_lazy.js
à partir de ce dictionnaire (une ligne par bénévole + l'ordre des listes par rôle).

Structure (envoyée en JSON dans la page, window.BENE_DATA) :
  bene     {id (texte): [« NOM Prénom », téléphone, e-mail]}
  roles    {rôle: [[id, prioritaire 0/1], ...]}  dans l'ordre d'affichage (nom, sans casse)
           rôles : chauffeur, equipier, responsable, tri_externe (= la liste des équipiers, prioritaires
           si ramasse_tri_externe = oui)
  affectes {rôle: {jour: [id, ...]}}  bénévoles déjà affectés ce jour-là (mention « déjà affecté »)
"""


def _oui(valeur):
    # même résultat que le filtre Jinja `valeur|lower == 'oui'` (None -> 'none')
    return str(valeur).lower() == "oui"


def donnees_listes_benevoles(chauffeurs, equipiers, responsables, collections_du_jour=None):
    """Retourne (donnees_json, eligibles) :
    - donnees_json : dictionnaire sérialisable (voir plus haut) ;
    - eligibles    : {rôle: ensemble d'id}, pour savoir côté gabarit si la personne affectée figure
                     dans la liste du rôle (même règle que l'ancien gabarit)."""
    bene = {}
    for collection in (chauffeurs, equipiers, responsables):
        for b in collection:
            bene[str(b["id"])] = [
                f"{b['nom']} {b['prenom']}",                  # comme {{ bene.nom }} {{ bene.prenom }}
                b.get("telephone_portable") or "N/A",
                b.get("email") or "N/A",
            ]

    roles = {
        "chauffeur": [[b["id"], 1 if _oui(b.get("ramasse_chauffeur")) else 0] for b in chauffeurs],
        "equipier": [[b["id"], 1 if _oui(b.get("ramasse_equipier")) else 0] for b in equipiers],
        "responsable": [[b["id"], 1 if _oui(b.get("ramasse_responsable_tri")) else 0] for b in responsables],
        "tri_externe": [[b["id"], 1 if _oui(b.get("ramasse_tri_externe")) else 0] for b in equipiers],
    }
    affectes = {
        role: {jour: sorted(ids) for jour, ids in (par_jour or {}).items()}
        for role, par_jour in (collections_du_jour or {}).items()
    }
    eligibles = {role: {e[0] for e in liste} for role, liste in roles.items()}
    return {"bene": bene, "roles": roles, "affectes": affectes}, eligibles
