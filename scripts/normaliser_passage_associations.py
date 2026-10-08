#!/usr/bin/env python3
"""
Normalisation des champs de passage à la BAI de la table associations
(voir ba38_partenaires/passage.py) :

- field_groups : type_champ jours_semaine / heure_passage / parking sur
  jour_de_passage_a_la_BAI / heure_de_passage / Emplacement
- parametres   : ajout de ces 3 valeurs à la liste type_champ
- associations : conversion des valeurs existantes au format normalisé ;
  les valeurs non convertibles sont laissées telles quelles et listées
  pour correction manuelle.

Simulation par défaut ; --appliquer écrit en base après une copie de
sauvegarde à côté de la base (<nom>_avant_passage_AAAAMMJJ_HHMMSS.sqlite).

Usage :
    venv/bin/python scripts/normaliser_passage_associations.py <chemin_base.sqlite> [--appliquer]

Le chemin de la base est obligatoire et explicite (pas de .env) pour ne
jamais viser PROD par erreur.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ba38_partenaires.passage import (  # noqa: E402
    normaliser_jours, normaliser_heure, normaliser_parking,
)

CHAMPS = {
    "jour_de_passage_a_la_BAI": ("jours_semaine", normaliser_jours),
    "heure_de_passage": ("heure_passage", normaliser_heure),
    "Emplacement": ("parking", normaliser_parking),
}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    appliquer = "--appliquer" in sys.argv
    if len(args) != 1:
        print(__doc__)
        sys.exit(1)

    db_path = Path(args[0]).resolve()
    if not db_path.is_file():
        sys.exit(f"❌ Base introuvable : {db_path}")

    print(f"Base : {db_path}")
    print("Mode : " + ("APPLICATION" if appliquer else "simulation (rien n'est écrit)"))

    if appliquer:
        sauvegarde = db_path.with_name(
            f"{db_path.stem}_avant_passage_{datetime.now():%Y%m%d_%H%M%S}.sqlite"
        )
        with sqlite3.connect(db_path) as src, sqlite3.connect(sauvegarde) as dst:
            src.backup(dst)
        print(f"Sauvegarde : {sauvegarde}")

    # simulation : base ouverte en lecture seule, aucune écriture tentée
    conn = sqlite3.connect(db_path) if appliquer else sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ---- field_groups + parametres -------------------------------------
    for fname, (type_champ, _) in CHAMPS.items():
        row = cur.execute(
            "SELECT id, type_champ FROM field_groups WHERE field_name = ? AND appli = 'associations'",
            (fname,),
        ).fetchone()
        if not row:
            print(f"⚠️  field_groups : {fname} absent")
            continue
        if row["type_champ"] != type_champ:
            print(f"field_groups : {fname} type_champ {row['type_champ']!r} → {type_champ!r}")
            if appliquer:
                cur.execute("UPDATE field_groups SET type_champ = ? WHERE id = ?", (type_champ, row["id"]))
        existe = cur.execute(
            "SELECT 1 FROM parametres WHERE param_name = 'type_champ' AND param_value = ?",
            (type_champ,),
        ).fetchone()
        if not existe:
            print(f"parametres : ajout type_champ {type_champ!r}")
            if appliquer:
                cur.execute(
                    "INSERT INTO parametres (param_name, param_value, categorie) VALUES ('type_champ', ?, 'liste')",
                    (type_champ,),
                )

    # ---- valeurs ---------------------------------------------------------
    rows = cur.execute(f"""
        SELECT id, nom_association, validite, {", ".join(f"`{c}`" for c in CHAMPS)}
        FROM associations ORDER BY nom_association COLLATE NOCASE
    """).fetchall()

    conversions = 0
    a_revoir = []
    for r in rows:
        modifs = {}
        for fname, (_, normaliser) in CHAMPS.items():
            brut = r[fname]
            if brut is None:
                continue
            try:
                canon = normaliser(brut)
            except ValueError:
                inactive = (r["validite"] or "").strip().lower() == "non"
                a_revoir.append((r["nom_association"], fname, brut, inactive))
                continue
            if canon != brut:
                modifs[fname] = canon
        if modifs:
            conversions += 1
            detail = ", ".join(f"{k}: {r[k]!r} → {v!r}" for k, v in modifs.items())
            print(f"  ✔ {r['nom_association']} — {detail}")
            if appliquer:
                cur.execute(
                    f"UPDATE associations SET {', '.join(f'`{k}` = ?' for k in modifs)} WHERE id = ?",
                    (*modifs.values(), r["id"]),
                )

    print(f"\n{conversions} association(s) convertie(s).")

    print(f"\n{len(a_revoir)} valeur(s) à corriger à la main :")
    for nom, fname, brut, inactive in a_revoir:
        print(f"  ✘ {nom} — {fname} = {brut!r}{'  (association inactive)' if inactive else ''}")

    # ---- associations au quai incomplètes ---------------------------------
    incomplets = cur.execute("""
        SELECT nom_association, jour_de_passage_a_la_BAI, heure_de_passage, Emplacement
        FROM associations
        WHERE REPLACE(UPPER(Emplacement), ' ', '') IN ('P1', 'P6')
          AND (validite IS NULL OR LOWER(TRIM(validite)) != 'non')
          AND (COALESCE(TRIM(jour_de_passage_a_la_BAI), '') = ''
               OR COALESCE(TRIM(heure_de_passage), '') = '')
        ORDER BY nom_association COLLATE NOCASE
    """).fetchall()
    print(f"\n{len(incomplets)} association(s) P1/P6 sans jour ou sans heure (bloquées à la prochaine modification) :")
    for r in incomplets:
        print(f"  ! {r['nom_association']} — {r['Emplacement']} jour={r['jour_de_passage_a_la_BAI']!r} heure={r['heure_de_passage']!r}")

    if appliquer:
        conn.commit()
        print("\n✅ Modifications enregistrées.")
    else:
        print("\nSimulation terminée : relancer avec --appliquer pour écrire.")
    conn.close()


if __name__ == "__main__":
    main()
