#!/usr/bin/env python3
"""Fusionne mail_debut + mail_fin en un seul champ mail_texte, avec le
repère <<affectations>> à l'endroit où la liste des tournées (demi-journée
/ véhicule / magasin / équipiers) est insérée — sur le même principe que
<<dates>> pour la demande d'autorisation de collecter. Migre le contenu
déjà personnalisé par campagne (pas seulement les valeurs par défaut) avant
de supprimer les anciennes colonnes. Idempotent."""
import sqlite3

from ba38_utilitaires.core import get_db_path


def main():
    with sqlite3.connect(get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        colonnes = {row[1] for row in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}

        if "mail_texte" not in colonnes:
            conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_texte TEXT")
            print("✅ Colonne mail_texte ajoutée")
        else:
            print("➖ Colonne mail_texte déjà présente")

        if "mail_debut" in colonnes or "mail_fin" in colonnes:
            colonnes_select = []
            if "mail_debut" in colonnes:
                colonnes_select.append("mail_debut")
            if "mail_fin" in colonnes:
                colonnes_select.append("mail_fin")
            rows = conn.execute(
                f"SELECT annee, mail_texte, {', '.join(colonnes_select)} FROM collecte_campagnes"
            ).fetchall()
            migres = 0
            for row in rows:
                if row["mail_texte"]:
                    continue  # déjà migré ou déjà personnalisé sur le nouveau champ
                debut = row["mail_debut"] if "mail_debut" in colonnes_select else None
                fin = row["mail_fin"] if "mail_fin" in colonnes_select else None
                if not debut and not fin:
                    continue
                morceaux = []
                if debut and debut.strip():
                    morceaux.append(debut.strip())
                morceaux.append("<<affectations>>")
                if fin and fin.strip():
                    morceaux.append(fin.strip())
                texte = "\n\n".join(morceaux)
                conn.execute(
                    "UPDATE collecte_campagnes SET mail_texte = ? WHERE annee = ?",
                    (texte, row["annee"]),
                )
                migres += 1
            print(f"🔀 {migres} campagne(s) migrée(s) vers mail_texte")

        for ancienne in ("mail_debut", "mail_fin"):
            if ancienne in colonnes:
                conn.execute(f"ALTER TABLE collecte_campagnes DROP COLUMN {ancienne}")
                print(f"🗑️ Ancienne colonne {ancienne} supprimée")

        conn.commit()


if __name__ == "__main__":
    main()
