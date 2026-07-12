#!/usr/bin/env python3
"""
📥 Import des réponses du questionnaire "date de naissance / situation"
    (export CSV du Google Form) dans la table `benevoles`.

Les colonnes sont détectées par mot-clé (peu importe le libellé exact de
la question dans le Form, ex : "Merci d'indiquer votre date de naissance
au format JJ/MM/AAAA" est bien reconnu comme la colonne date de
naissance) : une colonne contenant "date" + "naissance", une contenant
"situation", une colonne "id" (exacte), une contenant "nom" et "prenom".
Les colonnes annexes du mode quiz Google Forms (Score, Commentaires,
Horodateur) sont ignorées. L'encodage mal interprété (UTF-8 relu en
Latin-1, ex : "PrÃ©cisez") est corrigé automatiquement à la lecture.

Rapprochement par ID bénévole, avec vérification croisée du prénom/nom
(un email peut être partagé par un couple, donc pas de rapprochement par
email). Les lignes dont le nom ne correspond pas à l'ID, dont l'ID est
inconnu, dont la date est illisible ou dont la situation n'est pas
« Actif »/« Retraité » sont écartées et listées en anomalies au lieu
d'être appliquées.

Par défaut le script tourne en mode rapport seul (aucune écriture).
Ajouter --apply pour appliquer réellement les mises à jour.

Usage :
    python3 importer_reponses_questionnaire_benevoles.py --csv reponses.csv --env dev
    python3 importer_reponses_questionnaire_benevoles.py --csv reponses.csv --env dev --apply
    python3 importer_reponses_questionnaire_benevoles.py --csv reponses.csv --env prod --apply
"""

from pathlib import Path
import argparse
import sys
import sqlite3
import unicodedata

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from utils import write_log, get_db_path_by_env

SITUATIONS_VALIDES = {"actif": "Actif", "retraité": "Retraité", "retraite": "Retraité"}

DB_BASE_DIRS = {"dev": "/srv/ba38/dev", "prod": "/srv/ba38/prod"}


def normaliser_nom(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().split())


def corriger_mojibake(s: str) -> str:
    """Corrige un UTF-8 relu en Latin-1 (ex: 'PrÃ©cisez' -> 'Précisez')."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def detecter_colonnes(colonnes):
    # Exclut les colonnes annexes du mode quiz Google Forms (Score, Commentaires)
    normalisees = {
        c: normaliser_nom(c) for c in colonnes
        if "score" not in normaliser_nom(c) and "commentaire" not in normaliser_nom(c)
    }

    def chercher(*mots_requis):
        for col, norm in normalisees.items():
            if all(mot in norm for mot in mots_requis):
                return col
        return None

    col_id = next((c for c, norm in normalisees.items() if norm == "id"), None)

    mapping = {
        "id": col_id,
        "nom_prenom": chercher("nom", "prenom"),
        "date_naissance": chercher("date", "naissance"),
        "situation": chercher("situation"),
    }
    manquantes = [k for k, v in mapping.items() if v is None]
    if manquantes:
        raise ValueError(
            f"Colonnes introuvables dans le CSV : {manquantes}. "
            f"Colonnes disponibles : {list(colonnes)}"
        )
    return mapping


def parser_date(valeur: str):
    try:
        d = pd.to_datetime(valeur, dayfirst=True, errors="raise")
    except Exception:
        return None
    age = (pd.Timestamp.now() - d).days / 365.25
    if not (15 <= age <= 110):
        return None
    return d.strftime("%Y-%m-%d")


def charger_benevoles(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("SELECT id, nom, prenom FROM benevoles").fetchall()
    conn.close()
    return {r["id"]: dict(r) for r in rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Chemin du CSV exporté du Google Form")
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    parser.add_argument("--apply", action="store_true", help="Applique réellement les mises à jour (sinon : rapport seul)")
    args = parser.parse_args()

    db_path = get_db_path_by_env(args.env, force_base_dir=DB_BASE_DIRS[args.env])
    benevoles = charger_benevoles(db_path)

    df = pd.read_csv(args.csv, dtype=str).fillna("")
    df.columns = [corriger_mojibake(c) for c in df.columns]
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].map(corriger_mojibake)

    try:
        colonnes = detecter_colonnes(df.columns)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    a_appliquer = []
    anomalies = []

    for _, row in df.iterrows():
        raw_id = row[colonnes["id"]].strip()
        nom_prenom_reponse = row[colonnes["nom_prenom"]].strip()
        date_brute = row[colonnes["date_naissance"]].strip()
        situation_brute = row[colonnes["situation"]].strip()

        try:
            benevole_id = int(raw_id)
        except ValueError:
            anomalies.append((raw_id, nom_prenom_reponse, "ID bénévole non numérique"))
            continue

        benevole = benevoles.get(benevole_id)
        if not benevole:
            anomalies.append((raw_id, nom_prenom_reponse, "ID bénévole inconnu en base"))
            continue

        attendu = normaliser_nom(f"{benevole['prenom']} {benevole['nom']}")
        recu = normaliser_nom(nom_prenom_reponse)
        if attendu != recu and normaliser_nom(f"{benevole['nom']} {benevole['prenom']}") != recu:
            anomalies.append((raw_id, nom_prenom_reponse,
                               f"Nom/prénom ne correspond pas à la fiche (« {benevole['prenom']} {benevole['nom']} »)"))
            continue

        date_naissance = parser_date(date_brute)
        if not date_naissance:
            anomalies.append((raw_id, nom_prenom_reponse, f"Date de naissance illisible ou peu plausible (âge attendu 15-110 ans) : « {date_brute} »"))
            continue

        situation = SITUATIONS_VALIDES.get(normaliser_nom(situation_brute))
        if not situation:
            anomalies.append((raw_id, nom_prenom_reponse, f"Situation invalide : « {situation_brute} »"))
            continue

        a_appliquer.append((benevole_id, date_naissance, situation))

    # En cas de réponses multiples valides pour un même bénévole (ex: correction
    # après une erreur de saisie), on ne garde que la dernière (ordre du CSV =
    # ordre chronologique des soumissions Google Forms).
    derniere_par_id = {}
    doublons = set()
    for benevole_id, date_naissance, situation in a_appliquer:
        if benevole_id in derniere_par_id:
            doublons.add(benevole_id)
        derniere_par_id[benevole_id] = (date_naissance, situation)
    a_appliquer = [(bid, d, s) for bid, (d, s) in derniere_par_id.items()]

    if doublons:
        print(f"ℹ️ {len(doublons)} bénévole(s) avaient plusieurs réponses valides, seule la dernière est conservée : {sorted(doublons)}")

    print(f"✅ {len(a_appliquer)} réponse(s) valide(s) sur {len(df)} ligne(s).")
    if anomalies:
        print(f"⚠️ {len(anomalies)} anomalie(s) à vérifier manuellement :")
        for raw_id, nom, motif in anomalies:
            print(f"  - ID={raw_id!r} ({nom}) : {motif}")

    if not args.apply:
        print("\nℹ️ Mode rapport seul (aucune écriture). Relancer avec --apply pour appliquer.")
        return

    if args.env == "prod":
        confirm = input(f"⚠️  Vous allez modifier {len(a_appliquer)} bénévole(s) en PROD. Taper 'confirmer' pour continuer : ")
        if confirm.strip().lower() != "confirmer":
            print("❌ Annulé.")
            sys.exit(1)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for benevole_id, date_naissance, situation in a_appliquer:
        cur.execute(
            "UPDATE benevoles SET date_naissance=?, situation=? WHERE id=?",
            (date_naissance, situation, benevole_id),
        )
    conn.commit()
    conn.close()

    write_log(f"[IMPORT QUESTIONNAIRE BENEVOLES] env={args.env} maj={len(a_appliquer)} anomalies={len(anomalies)}")
    print(f"🎉 {len(a_appliquer)} bénévole(s) mis à jour dans {args.env}.")


if __name__ == "__main__":
    main()
