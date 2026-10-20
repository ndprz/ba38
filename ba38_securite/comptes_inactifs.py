"""
Désactivation des comptes inactifs (tâche planifiée quotidienne).

    <venv>/bin/python <racine>/ba38_securite/comptes_inactifs.py [--dry-run]

- 5 mois sans connexion → email d'avertissement au titulaire ;
- 30 jours après l'avertissement, toujours aucune connexion → compte
  désactivé (actif=0), titulaire prévenu, récapitulatif à l'admin.
Une connexion annule l'avertissement (cf. apres_connexion_reussie).
Une réactivation par un admin remet le compteur à zéro (update_users_batch).

Jamais concernés : admins, comptes test_only, comptes "exempt_inactivite",
comptes "mode_tablette" (postes partagés en kiosque).

L'environnement (base, mails) est celui de l'arborescence où se trouve le
script : /srv/ba38/prod/ba38_securite/... → PROD. Le .env est chargé
explicitement depuis la racine du script (piège load_dotenv / cwd).
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(RACINE / ".env", override=True)

from ba38_securite.core import CONTACT_SECURITE  # noqa: E402
from ba38_utilitaires.core import envoyer_mail, write_log  # noqa: E402

JOURS_AVANT_AVERTISSEMENT = 150   # ~5 mois
JOURS_AVANT_DESACTIVATION = 30    # après l'avertissement (~6 mois au total)
FMT = "%Y-%m-%d %H:%M:%S"


def main(dry_run):
    db = os.path.join(os.environ["BA38_BASE_DIR"], os.environ["SQLITE_DB"])
    env = os.getenv("ENVIRONMENT", "?")
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Comptes inactifs — {env} — {db}{' (dry-run)' if dry_run else ''}")

    now = datetime.now()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # Compte jamais vu (nouveau, ou créé à la main) : le compteur démarre aujourd'hui
    if not dry_run:
        conn.execute("UPDATE users SET derniere_connexion=? WHERE derniere_connexion IS NULL", (now.strftime(FMT),))
        conn.commit()

    users = conn.execute("""
        SELECT * FROM users
        WHERE LOWER(CAST(actif AS TEXT)) IN ('1', 'oui', 'true')
          AND COALESCE(role, '') != 'admin'
          AND COALESCE(test_only, 0) = 0
          AND COALESCE(exempt_inactivite, 0) = 0
          AND COALESCE(mode_tablette, 0) = 0
          AND derniere_connexion IS NOT NULL
    """).fetchall()

    avertis, desactives = [], []
    for u in users:
        derniere = datetime.strptime(u["derniere_connexion"][:19], FMT)
        avert = u["avertissement_inactivite_le"]

        if avert:
            avert_dt = datetime.strptime(avert[:19], FMT)
            if now >= avert_dt + timedelta(days=JOURS_AVANT_DESACTIVATION) and derniere < avert_dt:
                desactives.append(u)
                if not dry_run:
                    conn.execute(
                        "UPDATE users SET actif=0, desactive_inactivite_le=? WHERE id=?",
                        (now.strftime(FMT), u["id"]),
                    )
                    conn.commit()
                    _mail_desactivation(u)
        elif now >= derniere + timedelta(days=JOURS_AVANT_AVERTISSEMENT):
            avertis.append(u)
            if not dry_run:
                conn.execute(
                    "UPDATE users SET avertissement_inactivite_le=? WHERE id=?", (now.strftime(FMT), u["id"])
                )
                conn.commit()
                _mail_avertissement(u, now + timedelta(days=JOURS_AVANT_DESACTIVATION))
    conn.close()

    for titre, liste in (("Avertis", avertis), ("Désactivés", desactives)):
        print(f"  {titre} : {len(liste)}")
        for u in liste:
            print(f"    - {u['email']} (dernière connexion {u['derniere_connexion'][:10]})")

    if (avertis or desactives) and not dry_run:
        lignes = ["Comptes avertis (désactivation dans 30 jours sans connexion) :"]
        lignes += [f"  - {u['username'] or ''} <{u['email']}> — dernière connexion {u['derniere_connexion'][:10]}" for u in avertis] or ["  (aucun)"]
        lignes += ["", "Comptes désactivés ce jour :"]
        lignes += [f"  - {u['username'] or ''} <{u['email']}> — dernière connexion {u['derniere_connexion'][:10]}" for u in desactives] or ["  (aucun)"]
        lignes += ["", "Réactivation : gestion des utilisateurs, colonne Actif = Oui.",
                   "Bénévole saisonnier : cocher « Jamais désactivé si inactif »."]
        envoyer_mail(sujet=f"Comptes inactifs — {len(avertis)} avertis, {len(desactives)} désactivés",
                     destinataires=[CONTACT_SECURITE], texte="\n".join(lignes), sender_name="BA38 Sécurité")
        write_log(f"🕒 Comptes inactifs : {len(avertis)} avertis, {len(desactives)} désactivés")


def _mail_avertissement(u, date_limite):
    envoyer_mail(
        sujet="Votre compte BA38 va être désactivé",
        destinataires=[u["email"]],
        sender_name="BA38 Sécurité",
        texte=f"""Bonjour {u['username'] or ''},

Vous ne vous êtes pas connecté(e) à l'application BA38 depuis le {datetime.strptime(u['derniere_connexion'][:10], '%Y-%m-%d'):%d/%m/%Y}.

Par sécurité, les comptes inutilisés sont désactivés. Votre compte le sera
le {date_limite:%d/%m/%Y} si vous ne vous connectez pas d'ici là.

👉 Pour le garder, il suffit de vous connecter une fois avant cette date.

Vous n'en avez plus besoin ? Vous n'avez rien à faire.

Une question : {CONTACT_SECURITE}

L'équipe informatique BA38
""",
    )


def _mail_desactivation(u):
    envoyer_mail(
        sujet="Votre compte BA38 a été désactivé",
        destinataires=[u["email"]],
        sender_name="BA38 Sécurité",
        texte=f"""Bonjour {u['username'] or ''},

Faute de connexion depuis plus de 6 mois, votre compte BA38 a été désactivé
par sécurité.

Si vous en avez de nouveau besoin, écrivez simplement à {CONTACT_SECURITE} :
il sera réactivé.

L'équipe informatique BA38
""",
    )


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
