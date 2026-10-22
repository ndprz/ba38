"""Remplit les colonnes ams_* de indicateurs_suivi pour les campagnes existantes,
à partir d'un export AMS « Indicateurs État » (qui contient toutes les périodes de l'année).

Ne touche qu'aux colonnes ams_* : statut, exclusions et historique d'envoi restent intacts.

Usage : python scripts/indicateurs_rattrapage_ams.py <base.sqlite> <export_ams.csv> [--simulation]
(chemin de base explicite : load_dotenv() chargerait toujours dev/.env)
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba38_partenaires.indicateurs import (  # noqa: E402
    load_indicateurs_csv, build_valeurs_ams, normalize_code, AMS_INDICATEURS, AMS_MOTIFS,
)

COLONNES = [c for _, c, _ in AMS_INDICATEURS] + [c for _, c in AMS_MOTIFS]


def main(db_path, csv_path, simulation):
    df = load_indicateurs_csv(csv_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    for campagne in conn.execute("SELECT id, periode FROM indicateurs_campagnes ORDER BY id").fetchall():
        valeurs = build_valeurs_ams(df, campagne["periode"])
        if not valeurs or not any(valeurs.values()):
            print(f"— {campagne['periode']} : période absente de l'export, ignorée")
            continue

        lignes = conn.execute("""
            SELECT s.id, a.code_VIF
            FROM indicateurs_suivi s JOIN associations a ON a.id = s.association_id
            WHERE s.campagne_id = ?
        """, (campagne["id"],)).fetchall()

        maj = 0
        for l in lignes:
            ams = valeurs.get(normalize_code(l["code_VIF"]).lstrip("0"))
            if not ams:
                continue
            if not simulation:
                conn.execute(
                    f"UPDATE indicateurs_suivi SET {', '.join(c + ' = ?' for c in COLONNES)} WHERE id = ?",
                    [ams.get(c) for c in COLONNES] + [l["id"]],
                )
            maj += 1
        print(f"✔ {campagne['periode']} : {maj}/{len(lignes)} lignes {'à mettre' if simulation else 'mises'} à jour")

    if not simulation:
        conn.commit()
    conn.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--simulation"]
    if len(args) != 2:
        sys.exit(__doc__)
    main(args[0], args[1], "--simulation" in sys.argv)
