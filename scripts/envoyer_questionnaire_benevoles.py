#!/usr/bin/env python3
"""
📧 Envoi du questionnaire "date de naissance / situation" aux bénévoles.

Construit, pour chaque bénévole, un lien Google Form personnalisé
(ID bénévole + prénom/nom préremplis) et envoie un email individuel
via utils.envoyer_mail.

Avant tout envoi réel :
1. Créer le Google Form (champs : ID bénévole, Prénom et nom,
   Date de naissance, Situation).
2. Récupérer le lien prérempli ("⋮" > Obtenir un lien prérempli") et
   renseigner FORM_BASE_URL, ENTRY_ID, ENTRY_NOM_PRENOM ci-dessous.
3. Lancer d'abord avec --dry-run puis --test (envoi limité à
   TEST_EMAIL) avant --send (envoi réel à tous les bénévoles).

Usage :
    python3 envoyer_questionnaire_benevoles.py --env dev --dry-run
    python3 envoyer_questionnaire_benevoles.py --env prod --test
    python3 envoyer_questionnaire_benevoles.py --env prod --send
"""

from pathlib import Path
import argparse
import sys
import sqlite3
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from utils import write_log, get_db_path_by_env, envoyer_mail

# ============================================================
# ⚙️ À renseigner une fois le Google Form créé
# ============================================================
FORM_BASE_URL = "https://docs.google.com/forms/d/e/1FAIpQLSeQ9-jN81wTJ6DxtOj4SC0AQT1NuOHUjO5rzg6wXaxzb2YREQ/viewform"
ENTRY_ID = "entry.1092073297"            # champ "id"
ENTRY_NOM_PRENOM = "entry.987311474"     # champ "Nom et prénom"

TEST_EMAIL = "ba380.informatique2@banquealimentaire.org"

DB_BASE_DIRS = {"dev": "/srv/ba38/dev", "prod": "/srv/ba38/prod"}

SUJET = "Questionnaire bénévoles BA38 : date de naissance et situation"

MESSAGE_TEMPLATE = """Chers bénévoles,

Peut-être savez vous que le financement de notre type d'association n'est pas facile, et nous sommes en recherche permanente de soutiens financiers.
Pour les besoins d'une certaine demande de subvention pour notre association, nous devons communiquer des statistiques sur nos bénévoles :
Nbre d'adultes par tranches d'âge
Nbre de retraités
etc..

Pour cela nous devons disposer de votre date de naissance et de votre situation : actif ou retraité
Nous n'en disposons pas pour l'instant dans le fichier de nos bénévoles.
Accepteriez-vous de remplir le questionnaire ci-joint, et que nous ajoutions ces informations à votre fiche ?

{lien}

Nous vous en remercions par avance

Chantal VIVIER, présidente
Yves MARKOWICZ, chargé de la recherche des subventions
"""


def build_form_url(benevole_id: int, nom_prenom: str) -> str:
    sep = "&" if "?" in FORM_BASE_URL else "?"
    return (
        f"{FORM_BASE_URL}{sep}{ENTRY_ID}={quote(str(benevole_id))}"
        f"&{ENTRY_NOM_PRENOM}={quote(nom_prenom)}"
    )


def charger_benevoles(db_path: str, benevole_id: int | None = None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    query = (
        "SELECT id, nom, prenom, email FROM benevoles "
        "WHERE email IS NOT NULL AND TRIM(email) != '' "
    )
    params = []
    if benevole_id is not None:
        query += "AND id = ? "
        params.append(benevole_id)
    else:
        query += "AND type_benevole = 'benevole' AND cotisation_2026 = 'oui' "
    query += "ORDER BY nom COLLATE NOCASE"
    rows = cur.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    parser.add_argument("--id", type=int, help="Ne renvoyer qu'à ce seul bénévole (par ID), ex: relance après un rebond email")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="N'envoie aucun email, affiche seulement les liens générés")
    mode.add_argument("--test", action="store_true", help="Envoie un seul email de test à TEST_EMAIL")
    mode.add_argument("--send", action="store_true", help="Envoi réel à tous les bénévoles")
    args = parser.parse_args()

    if "REMPLACER_MOI" in FORM_BASE_URL + ENTRY_ID + ENTRY_NOM_PRENOM:
        print("❌ Merci de renseigner FORM_BASE_URL / ENTRY_ID / ENTRY_NOM_PRENOM en tête de script avant de continuer.")
        sys.exit(1)

    db_path = get_db_path_by_env(args.env, force_base_dir=DB_BASE_DIRS[args.env])
    write_log(f"[QUESTIONNAIRE BENEVOLES] env={args.env} db={db_path} mode="
              f"{'dry-run' if args.dry_run else 'test' if args.test else 'send'}")

    benevoles = charger_benevoles(db_path, benevole_id=args.id)
    if args.id and not benevoles:
        print(f"❌ Aucun bénévole avec un email trouvé pour l'ID {args.id} dans {args.env}.")
        sys.exit(1)
    print(f"👥 {len(benevoles)} bénévole(s) avec email trouvé(s) dans {args.env}"
          f"{f' (ID={args.id})' if args.id else ''}.")

    if args.send:
        confirm = input(
            f"⚠️  Vous allez envoyer un email RÉEL à {len(benevoles)} bénévole(s) "
            f"({args.env}). Taper 'confirmer' pour continuer : "
        )
        if confirm.strip().lower() != "confirmer":
            print("❌ Annulé.")
            sys.exit(1)

    envoyes = 0
    for b in benevoles:
        nom_prenom = f"{b['prenom']} {b['nom']}".strip()
        lien = build_form_url(b["id"], nom_prenom)
        texte = MESSAGE_TEMPLATE.format(lien=lien)

        if args.dry_run:
            print(f"[{b['id']}] {nom_prenom} <{b['email']}> -> {lien}")
            continue

        if args.test:
            print(f"[TEST] {nom_prenom} <{b['email']}> -> envoi à {TEST_EMAIL}")
            envoyer_mail(SUJET, [TEST_EMAIL], texte)
            envoyes += 1
            break  # un seul email de test suffit

        envoyer_mail(SUJET, [b["email"]], texte)
        write_log(f"[QUESTIONNAIRE BENEVOLES] envoyé à id={b['id']} {nom_prenom} <{b['email']}>")
        envoyes += 1

    print(f"✅ Terminé : {envoyes} email(s) envoyé(s)." if not args.dry_run else "✅ Dry-run terminé, aucun email envoyé.")


if __name__ == "__main__":
    main()
