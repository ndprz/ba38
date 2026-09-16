#!/usr/bin/env python3
"""Ajoute les colonnes éditables en ligne de la demande d'autorisation de
collecter — même principe que mail_debut/mail_fin pour les
chauffeurs/équipiers :
- autorisation_texte_lettre : corps de la lettre (repère <<dates>> pour les
  dates de collecte, <<centre>> pour centrer les lignes suivantes comme la
  signature) ;
- autorisation_texte_coupon : coupon-réponse en bas de page (repères
  <<Nom>>, <<CodeVIF>>, <<Adresse>>, <<CP>>, <<VILLE>>, <<Telephone>>,
  <<Email>>, et <<autorisation>> pour la ligne OUI/NON avec cases à cocher) ;
- autorisation_texte_mail : corps de l'e-mail lui-même (distinct du texte de
  la lettre PDF jointe), repères <<Nom>> et <<Annee>>.
NULL par défaut (texte par défaut codé en dur). Idempotent.

L'affiche des produits (2ᵉ page du PDF), elle, n'est PAS stockée en base :
déposée dans l'emplacement partagé MODELES_GARDEE_DIR (comme les modèles
Word du module), pour rester disponible d'une campagne et d'une session à
l'autre sans redépôt annuel (cf. _chemin_image_produits_autorisation).

Remplace l'essai à deux colonnes (autorisation_texte_intro/_cloture) du
15/09/2026 et la colonne autorisation_image_produits (stockage par
campagne, abandonné le 16/09/2026 au profit du stockage partagé) — jamais
déployées en prod, supprimées ici si présentes."""
import sqlite3

from ba38_utilitaires.core import get_db_path


def main():
    with sqlite3.connect(get_db_path()) as conn:
        colonnes = {row[1] for row in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}

        for nouvelle in ("autorisation_texte_lettre", "autorisation_texte_coupon", "autorisation_texte_mail"):
            if nouvelle not in colonnes:
                conn.execute(f"ALTER TABLE collecte_campagnes ADD COLUMN {nouvelle} TEXT")
                print(f"✅ Colonne {nouvelle} ajoutée")
            else:
                print(f"➖ Colonne {nouvelle} déjà présente")

        for ancienne in ("autorisation_texte_intro", "autorisation_texte_cloture", "autorisation_image_produits"):
            if ancienne in colonnes:
                conn.execute(f"ALTER TABLE collecte_campagnes DROP COLUMN {ancienne}")
                print(f"🗑️ Ancienne colonne {ancienne} supprimée")

        conn.commit()


if __name__ == "__main__":
    main()
