#!/usr/bin/env python3
"""Supprime les colonnes mail_image et mail_pieces_jointes de
collecte_campagnes, devenues inutiles : l'image et les pièces jointes du
mail chauffeurs/équipiers sont désormais déposées dans l'emplacement
PARTAGÉ du module (MODELES_GARDEE_DIR), comme l'affiche des produits de la
demande d'autorisation — conservées d'une campagne et d'une session à
l'autre sans redépôt annuel, plus besoin de les référencer en base.

Si un fichier avait déjà été déposé pour une campagne (ancien mécanisme par
année), il est repris vers l'emplacement partagé avant suppression des
colonnes, pour ne rien perdre. Idempotent."""
import json
import os
import shutil
import sqlite3

from ba38_utilitaires.core import get_db_path

MODELES_GARDEE_DIR = "/srv/ba38/uploads/collecte_fichiers_source"
PIECE_JOINTE_MAIL_PREFIXE = "mail_chauffeurs_equipiers_pj_"


def dossier_annee(annee):
    return f"/srv/ba38/uploads/collecte/{annee}"


def main():
    with sqlite3.connect(get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        colonnes = {row[1] for row in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}

        if "mail_image" in colonnes:
            rows = conn.execute("SELECT annee, mail_image FROM collecte_campagnes WHERE mail_image IS NOT NULL").fetchall()
            for row in rows:
                source = os.path.join(dossier_annee(row["annee"]), row["mail_image"])
                if os.path.exists(source):
                    extension = row["mail_image"].rsplit(".", 1)[-1]
                    cible = os.path.join(MODELES_GARDEE_DIR, f"mail_chauffeurs_equipiers.{extension}")
                    if not os.path.exists(cible):
                        shutil.copy(source, cible)
                        print(f"🔀 Image {row['annee']} reprise vers {cible}")
            conn.execute("ALTER TABLE collecte_campagnes DROP COLUMN mail_image")
            print("🗑️ Colonne mail_image supprimée")
        else:
            print("➖ Colonne mail_image déjà absente")

        if "mail_pieces_jointes" in colonnes:
            rows = conn.execute("SELECT annee, mail_pieces_jointes FROM collecte_campagnes WHERE mail_pieces_jointes IS NOT NULL").fetchall()
            for row in rows:
                noms = json.loads(row["mail_pieces_jointes"]) if row["mail_pieces_jointes"] else []
                for nom in noms:
                    source = os.path.join(dossier_annee(row["annee"]), nom)
                    if os.path.exists(source):
                        cible = os.path.join(MODELES_GARDEE_DIR, f"{PIECE_JOINTE_MAIL_PREFIXE}{nom}")
                        if not os.path.exists(cible):
                            shutil.copy(source, cible)
                            print(f"🔀 Pièce jointe {row['annee']}/{nom} reprise vers {cible}")
            conn.execute("ALTER TABLE collecte_campagnes DROP COLUMN mail_pieces_jointes")
            print("🗑️ Colonne mail_pieces_jointes supprimée")
        else:
            print("➖ Colonne mail_pieces_jointes déjà absente")

        conn.commit()


if __name__ == "__main__":
    main()
