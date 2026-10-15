#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : module Planning ESOPE (épicerie sociale), phase 0.

Crée :
- esope_fonctions            : fonctions (Accueil, Caisse-Pesée, Support…)
- esope_creneaux_types       : un poste un jour de la semaine (horaires, min/max)
- esope_benevoles_fonctions  : fonctions ESOPE possibles d'un bénévole
- esope_modele               : affectations du planning modèle (+ récurrence)
- esope_creneaux             : créneaux datés du planning réel + événements
- esope_affectations         : un bénévole sur un créneau daté
- esope_historique           : journal des actions sur les affectations
- esope_fermetures           : jours sans planning (fériés, congés)

Ajoute à benevoles (et benevoles_inactifs) : esope_jour_dispo, esope_commentaire.
Ajoute l'application 'planning_esope' (droit lecture / écriture).
Pré-remplit fonctions et créneaux types avec les horaires du cahier des
charges (seulement si esope_fonctions est vide).

Chemins explicites (pas de load_dotenv, cf. piège dev/.env). Idempotent.
Phase 1 (confirmation en ligne par les bénévoles) :
- esope_periodes  : statut du planning d'un mois (brouillon / envoyé / verrouillé)
- esope_liens     : lien personnel d'un bénévole pour un mois (jeton, envoi, réponses)
- esope_affectations : + reponse_le, reponse_commentaire, alerte, rappel_envoye_le
- modeles_emails  : esope_preplanning, esope_relance, esope_rappel (si absents)

Usage : migrate_planning_esope.py dev dev_test [prod prod_test]
"""

import argparse
import sqlite3
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

TABLES = [
    """
    CREATE TABLE IF NOT EXISTS esope_fonctions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      nom TEXT NOT NULL UNIQUE,
      ordre INTEGER DEFAULT 0,
      couleur TEXT DEFAULT '#6c757d',
      actif INTEGER DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS esope_creneaux_types (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fonction_id INTEGER NOT NULL REFERENCES esope_fonctions(id),
      poste TEXT NOT NULL DEFAULT '',
      jour INTEGER NOT NULL CHECK (jour BETWEEN 0 AND 6),
      heure_debut TEXT NOT NULL,
      heure_fin TEXT NOT NULL,
      nb_min INTEGER NOT NULL DEFAULT 1,
      nb_max INTEGER NOT NULL DEFAULT 1,
      libelle TEXT,
      actif INTEGER DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS esope_benevoles_fonctions (
      benevole_id INTEGER NOT NULL,
      fonction_id INTEGER NOT NULL REFERENCES esope_fonctions(id),
      PRIMARY KEY (benevole_id, fonction_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS esope_modele (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      creneau_type_id INTEGER NOT NULL REFERENCES esope_creneaux_types(id),
      benevole_id INTEGER NOT NULL,
      frequence INTEGER NOT NULL DEFAULT 1,
      date_reference TEXT,
      heure_debut TEXT,
      heure_fin TEXT,
      commentaire TEXT,
      cree_le TEXT DEFAULT (datetime('now','localtime')),
      user_creation TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS esope_creneaux (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      date TEXT NOT NULL,
      creneau_type_id INTEGER REFERENCES esope_creneaux_types(id),
      fonction_id INTEGER REFERENCES esope_fonctions(id),
      poste TEXT DEFAULT '',
      libelle TEXT,
      lieu TEXT,
      heure_debut TEXT NOT NULL,
      heure_fin TEXT NOT NULL,
      nb_min INTEGER NOT NULL DEFAULT 1,
      nb_max INTEGER NOT NULL DEFAULT 1,
      ferme INTEGER DEFAULT 0,
      commentaire TEXT,
      cree_le TEXT DEFAULT (datetime('now','localtime')),
      user_creation TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS esope_affectations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      creneau_id INTEGER NOT NULL REFERENCES esope_creneaux(id) ON DELETE CASCADE,
      benevole_id INTEGER NOT NULL,
      statut TEXT NOT NULL DEFAULT 'prevu',
      heure_debut TEXT,
      heure_fin TEXT,
      commentaire TEXT,
      origine TEXT DEFAULT 'manuel',
      cree_le TEXT DEFAULT (datetime('now','localtime')),
      modifie_le TEXT,
      user_modif TEXT,
      UNIQUE (creneau_id, benevole_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS esope_historique (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      date_heure TEXT DEFAULT (datetime('now','localtime')),
      creneau_id INTEGER,
      affectation_id INTEGER,
      benevole_id INTEGER,
      auteur TEXT,
      action TEXT NOT NULL,
      ancien TEXT,
      nouveau TEXT,
      commentaire TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS esope_fermetures (
      date TEXT PRIMARY KEY,
      motif TEXT
    )
    """,
]

TABLES_PHASE1 = [
    """
    CREATE TABLE IF NOT EXISTS esope_periodes (
      mois TEXT PRIMARY KEY,
      statut TEXT NOT NULL DEFAULT 'brouillon',
      envoye_le TEXT,
      envoye_par TEXT,
      verrouille_le TEXT,
      verrouille_par TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS esope_liens (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      benevole_id INTEGER NOT NULL,
      mois TEXT NOT NULL,
      token TEXT NOT NULL UNIQUE,
      cree_le TEXT DEFAULT (datetime('now','localtime')),
      envoye_le TEXT,
      envoye_par TEXT,
      envoi_mode_test INTEGER DEFAULT 0,
      envoi_erreur TEXT,
      mailjet_ids TEXT,
      relance_le TEXT,
      nb_relances INTEGER DEFAULT 0,
      premiere_ouverture_le TEXT,
      derniere_reponse_le TEXT,
      disponibilites TEXT,
      disponibilites_le TEXT,
      disponibilites_vu INTEGER DEFAULT 1,
      UNIQUE (benevole_id, mois)
    )
    """,
]

COLONNES_AFFECTATIONS = [
    ("reponse_le", "TEXT"),            # dernière réponse du bénévole (lien personnel)
    ("reponse_commentaire", "TEXT"),
    ("alerte", "INTEGER DEFAULT 0"),   # 1 = réponse du bénévole pas encore vue par le gestionnaire
    ("rappel_envoye_le", "TEXT"),
]

MODELES_MAILS = [
    ("esope_preplanning", "ESOPE – votre planning de <<mois>>", """<p>Bonjour <<prenom>>,</p>
<p>Voici le planning de l'épicerie sociale ESOPE pour <b><<mois>></b>.</p>
<<creneaux>>
<p>Merci de <b>confirmer ou d'annuler</b> chacun de vos créneaux en cliquant sur votre lien personnel :</p>
<p><a href="<<lien>>"><<lien>></a></p>
<p>Vous pouvez aussi y indiquer vos disponibilités ou proposer votre aide sur d'autres créneaux.
Le planning complet du mois est joint à ce mail.</p>
<p>Merci pour votre engagement,<br>L'équipe ESOPE – Banque Alimentaire de l'Isère</p>"""),
    ("esope_relance", "ESOPE – merci de confirmer votre planning de <<mois>>", """<p>Bonjour <<prenom>>,</p>
<p>Nous n'avons pas encore votre réponse pour le planning ESOPE de <b><<mois>></b>.</p>
<<creneaux>>
<p>Merci de confirmer ou d'annuler vos créneaux avec votre lien personnel :</p>
<p><a href="<<lien>>"><<lien>></a></p>
<p>Merci,<br>L'équipe ESOPE – Banque Alimentaire de l'Isère</p>"""),
    ("esope_rappel", "ESOPE – rappel : vous êtes attendu(e) demain", """<p>Bonjour <<prenom>>,</p>
<p>Petit rappel : vous êtes attendu(e) demain à l'épicerie sociale ESOPE.</p>
<<creneaux>>
<p>En cas d'empêchement, prévenez-nous dès que possible avec votre lien personnel :</p>
<p><a href="<<lien>>"><<lien>></a></p>
<p>Merci et à demain,<br>L'équipe ESOPE – Banque Alimentaire de l'Isère</p>"""),
]

INDEX = [
    # un seul créneau daté par créneau type et par jour (les événements ont creneau_type_id NULL)
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_esope_creneaux_type_date "
    "ON esope_creneaux(date, creneau_type_id) WHERE creneau_type_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_esope_creneaux_date ON esope_creneaux(date)",
    "CREATE INDEX IF NOT EXISTS idx_esope_affectations_creneau ON esope_affectations(creneau_id)",
    "CREATE INDEX IF NOT EXISTS idx_esope_affectations_benevole ON esope_affectations(benevole_id)",
    "CREATE INDEX IF NOT EXISTS idx_esope_modele_ct ON esope_modele(creneau_type_id)",
    "CREATE INDEX IF NOT EXISTS idx_esope_historique_creneau ON esope_historique(creneau_id)",
    "CREATE INDEX IF NOT EXISTS idx_esope_liens_mois ON esope_liens(mois)",
]

COLONNES_BENEVOLES = [("esope_jour_dispo", "TEXT"), ("esope_commentaire", "TEXT")]

APPLICATION = (
    "planning_esope", "Planning ESOPE", "esope.index", "Exploitation", 5, "🛒", 2, 1,
)

# ── Données de départ (cahier des charges 2026-10-14) ─────────────────────
# jours : 0=lundi … 5=samedi
MAR, MER, JEU, VEN, SAM, LUN = 1, 2, 3, 4, 5, 0
OUVERTURE = {
    MAR: ("14:45", "18:30"), MER: ("13:45", "17:30"), JEU: ("14:45", "18:30"),
    VEN: ("12:45", "16:30"), SAM: ("08:45", "12:00"),
}

FONCTIONS = [
    # (nom, couleur)
    ("Accueil", "#0d6efd"),
    ("Caisse-Pesée", "#198754"),
    ("Support", "#6f42c1"),
    ("Fruits légumes", "#20c997"),
    ("Nettoyage", "#6c757d"),
    ("Mise en rayon", "#fd7e14"),
    ("Support dossiers", "#0dcaf0"),
    ("Préparation commandes", "#d63384"),
]


def _creneaux_depart():
    """(fonction, poste, jour, debut, fin, min, max, libelle)"""
    c = []
    for j, (d, f) in OUVERTURE.items():
        c.append(("Accueil", "", j, d, f, 1, 1, None))
    # Caisse-Pesée : 3 postes de 2 bénévoles interchangeables
    poste2 = {**OUVERTURE, MAR: ("14:45", "16:30"), JEU: ("14:45", "16:30")}
    poste3 = {**OUVERTURE, MAR: ("16:00", "19:00"), JEU: ("16:00", "19:00")}
    for nom_poste, horaires in (("Poste 1", OUVERTURE), ("Poste 2", poste2), ("Poste 3", poste3)):
        for j, (d, f) in horaires.items():
            c.append(("Caisse-Pesée", nom_poste, j, d, f, 2, 2, None))
    support = {MAR: ("14:45", "19:00"), MER: ("13:45", "18:00"), JEU: ("14:45", "19:00"),
               VEN: ("12:45", "17:00"), SAM: ("08:45", "12:30")}
    for j, (d, f) in support.items():
        c.append(("Support", "", j, d, f, 1, 2, None))
    for j, (d, f) in OUVERTURE.items():
        c.append(("Fruits légumes", "", j, d, f, 1, 1, None))
    c.append(("Nettoyage", "", MAR, "12:00", "13:00", 1, 1, None))
    c.append(("Mise en rayon", "", MAR, "13:00", "15:00", 1, 5, None))
    c.append(("Mise en rayon", "", MER, "12:00", "13:45", 1, 5, "sushis"))
    c.append(("Mise en rayon", "", JEU, "13:00", "15:00", 1, 5, None))
    for j, (d, f) in OUVERTURE.items():
        c.append(("Support dossiers", "", j, d, f, 1, 1, None))
    for j in (LUN, MAR, JEU):
        c.append(("Préparation commandes", "", j, "08:30", "11:30", 1, 1, None))
    return c


def migrer(label, path):
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.cursor()
        for sql in TABLES + TABLES_PHASE1:
            cur.execute(sql)
        for sql in INDEX:
            cur.execute(sql)

        for table in ("benevoles", "benevoles_inactifs"):
            existantes = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
            if not existantes:
                continue
            for col, typ in COLONNES_BENEVOLES:
                if col not in existantes:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    print(f"  {label}: + {table}.{col}")

        existantes = {r[1] for r in cur.execute("PRAGMA table_info(esope_affectations)")}
        for col, typ in COLONNES_AFFECTATIONS:
            if col not in existantes:
                cur.execute(f"ALTER TABLE esope_affectations ADD COLUMN {col} {typ}")
                print(f"  {label}: + esope_affectations.{col}")

        cols_modeles = {r[1] for r in cur.execute("PRAGMA table_info(modeles_emails)")}
        for code, sujet, corps in MODELES_MAILS:
            if not cur.execute("SELECT 1 FROM modeles_emails WHERE code_modele = ?", (code,)).fetchone():
                if "is_html" in cols_modeles:
                    cur.execute("INSERT INTO modeles_emails (code_modele, sujet, corps, date_modification, type_periode, "
                                "is_html) VALUES (?, ?, ?, datetime('now','localtime'), 'esope', 1)", (code, sujet, corps))
                else:
                    cur.execute("INSERT INTO modeles_emails (code_modele, sujet, corps, date_modification) "
                                "VALUES (?, ?, ?, datetime('now','localtime'))", (code, sujet, corps))
                print(f"  {label}: + modèle de mail {code}")

        if not cur.execute("SELECT 1 FROM applications WHERE appli = ?", (APPLICATION[0],)).fetchone():
            cur.execute(
                "INSERT INTO applications (appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", APPLICATION)
            print(f"  {label}: + application planning_esope")

        if cur.execute("SELECT COUNT(*) FROM esope_fonctions").fetchone()[0] == 0:
            ids = {}
            for ordre, (nom, couleur) in enumerate(FONCTIONS, start=1):
                cur.execute("INSERT INTO esope_fonctions (nom, ordre, couleur) VALUES (?, ?, ?)",
                            (nom, ordre * 10, couleur))
                ids[nom] = cur.lastrowid
            creneaux = _creneaux_depart()
            for (fct, poste, jour, d, f, mn, mx, lib) in creneaux:
                cur.execute(
                    "INSERT INTO esope_creneaux_types (fonction_id, poste, jour, heure_debut, heure_fin, "
                    "nb_min, nb_max, libelle) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ids[fct], poste, jour, d, f, mn, mx, lib))
            print(f"  {label}: {len(FONCTIONS)} fonctions, {len(creneaux)} créneaux types pré-remplis")

        conn.commit()
    print(f"✅ {label} ({path})")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bases", nargs="+", choices=sorted(DB_PATHS))
    args = parser.parse_args()
    for b in args.bases:
        migrer(b, DB_PATHS[b])


if __name__ == "__main__":
    main()
