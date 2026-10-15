"""
Migration du schéma pour ba38_securite (idempotente).

    python ba38_securite/migration.py <chemin.sqlite> [<chemin.sqlite> ...]

À passer sur la base réelle ET la base de test de chaque environnement,
AVANT de déployer le code. Chemins explicites (pas de .env) pour éviter
le piège load_dotenv qui viserait toujours la base DEV.

derniere_connexion est initialisée depuis log_connexions, à défaut à la
date de migration (personne n'est réputé inactif avant 5 mois).
"""

import sqlite3
import sys
from datetime import datetime

COLONNES_USERS = {
    "derniere_connexion": "TEXT",
    "avertissement_inactivite_le": "TEXT",
    "desactive_inactivite_le": "TEXT",
    "exempt_inactivite": "INTEGER DEFAULT 0",
    "etranger_autorise_jusqu": "TEXT",
    "jeton_session": "TEXT",
}


def migrer(chemin):
    conn = sqlite3.connect(chemin)
    existantes = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for col, typ in COLONNES_USERS.items():
        if col not in existantes:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
            print(f"  + users.{col}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users_appareils (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            jeton_hash TEXT,
            user_agent TEXT,
            ip TEXT,
            pays TEXT,
            cree_le TEXT,
            vu_le TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_appareils_user ON users_appareils(user_id)")
    if "confiance_jusqu" not in {r[1] for r in conn.execute("PRAGMA table_info(users_appareils)")}:
        conn.execute("ALTER TABLE users_appareils ADD COLUMN confiance_jusqu TEXT")
        print("  + users_appareils.confiance_jusqu")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users_telephones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            libelle TEXT NOT NULL,
            numero TEXT NOT NULL,
            cree_le TEXT,
            cree_par TEXT,
            verifie_le TEXT,
            utilise_le TEXT,
            UNIQUE (user_id, numero)
        )""")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='log_connexions'").fetchone():
        if "via" not in {r[1] for r in conn.execute("PRAGMA table_info(log_connexions)")}:
            conn.execute("ALTER TABLE log_connexions ADD COLUMN via TEXT")
            print("  + log_connexions.via")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS securite_alertes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cle TEXT NOT NULL,
            envoye_le TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_securite_alertes_cle ON securite_alertes(cle, envoye_le)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT, login TEXT, timestamp REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_login ON login_attempts(login, timestamp)")

    # Dernière connexion connue (log_connexions.timestamp est en ISO UTC)
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    a_log = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='log_connexions'"
    ).fetchone()
    if a_log:
        conn.execute("""
            UPDATE users SET derniere_connexion = (
                SELECT replace(substr(MAX(l.timestamp), 1, 19), 'T', ' ')
                FROM log_connexions l
                WHERE LOWER(l.email) = LOWER(users.email) AND l.action = 'login'
            ) WHERE derniere_connexion IS NULL""")
    n = conn.execute(
        "UPDATE users SET derniere_connexion=? WHERE derniere_connexion IS NULL", (maintenant,)
    ).rowcount
    print(f"  derniere_connexion : {n} compte(s) sans historique initialisé(s) à aujourd'hui")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for chemin in sys.argv[1:]:
        print(f"→ {chemin}")
        migrer(chemin)
