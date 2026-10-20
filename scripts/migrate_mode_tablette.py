#!/usr/bin/env python3
# ============================================================
# 📱 Mode tablette : colonnes users.mode_tablette + identifiant_tablette
# ============================================================
# Un compte "mode tablette" (poste partagé verrouillé en kiosque) :
#   - se connecte avec un identifiant court sans @ (ex. "tab1"),
#   - garde sa session 30 jours au lieu de 24 h,
#   - n'est jamais désactivé pour inactivité.
#
# ⚠️ À lancer sur PROD (et prod_test) AVANT le déploiement du code :
# la table users n'est pas dans EXCLUDE_TABLES, donc si la colonne
# n'existe pas encore en PROD, migrate_schema_and_data_dev_to_prod.py la
# crée ET la remplit par id depuis DEV — or les id d'utilisateurs ne
# désignent pas les mêmes comptes en DEV et en PROD. Une fois la colonne
# présente en PROD, le script de synchro n'y touche plus.
#
# Comptes passés en mode tablette par email (jamais par id), idempotent.
# Chemins de base explicites (pas de .env : piège load_dotenv).
#
# Usage : python3 scripts/migrate_mode_tablette.py --env dev|dev_test|prod|prod_test
# ============================================================

import argparse
import sqlite3

BASES = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

COMPTES_TABLETTE = {
    "ba380.tab1@banquealimentaire.org": "tab1",
    "ba380.tab2@banquealimentaire.org": "tab2",
    "ba380.tab3@banquealimentaire.org": "tab3",
    "ba380.tab4@banquealimentaire.org": "tab4",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=BASES)
    args = ap.parse_args()
    db = BASES[args.env]
    print(f"📂 {db}")

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "mode_tablette" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN mode_tablette INTEGER DEFAULT 0")
        print("➕ users.mode_tablette")
    if "identifiant_tablette" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN identifiant_tablette TEXT")
        print("➕ users.identifiant_tablette")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_identifiant_tablette "
        "ON users (LOWER(identifiant_tablette)) WHERE identifiant_tablette IS NOT NULL"
    )

    for email, identifiant in COMPTES_TABLETTE.items():
        n = conn.execute(
            """UPDATE users SET mode_tablette = 1, identifiant_tablette = ?, exempt_inactivite = 1
               WHERE LOWER(email) = ?""",
            (identifiant, email),
        ).rowcount
        print(f"{'✅' if n else '⏭️ absent :'} {email} → {identifiant}")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
