#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration sûre pour enrichir la table 'fournisseurs' (utilisée par la ramasse),
ajouter 'fournisseurs_contacts', index & triggers, et (optionnel) la vue 'magasins'.

- Idempotent (rejouable sans casse)
- DDL en autocommit (SQLite impose souvent un commit implicite)
- DML (backfill) dans une petite transaction dédiée
"""

import os
import sys
import sqlite3
from contextlib import closing

# ---------------------------------------------------------------------
# Localisation DB : essaie get_db_path(), sinon fallback DEV
# ---------------------------------------------------------------------
def get_db_path_safe():
    try:
        sys.path.append("/home/ndprz/ba380")
        from utils import get_db_path
        return get_db_path()
    except Exception:
        return "/home/ndprz/ba380/ba380.sqlite"

DB_PATH = get_db_path_safe()

# ---------------------------------------------------------------------
# DDL / SQL
# ---------------------------------------------------------------------
COLS_TO_ADD = [
    ("enseigne", "TEXT"),
    ("societe", "TEXT"),
    ("tel_mobile", "TEXT"),
    ("tel_fixe", "TEXT"),
    ("email", "TEXT"),
    ("adresse1", "TEXT"),
    ("adresse2", "TEXT"),
    ("cp", "TEXT"),
    ("ville", "TEXT"),
    ("notes", "TEXT"),
    ("actif", "TEXT"),          # défaut via trigger + backfill
    ("date_creation", "TEXT"),  # défaut via trigger + backfill
    ("date_modif", "TEXT"),
    ("user_modif", "TEXT"),
]

CREATE_CONTACTS_SQL = """
CREATE TABLE IF NOT EXISTS fournisseurs_contacts (
  id INTEGER PRIMARY KEY,
  fournisseur_id INTEGER NOT NULL,
  prenom TEXT,
  nom TEXT,
  fonction TEXT,
  tel_mobile TEXT,
  tel_fixe TEXT,
  email TEXT,
  adresse1 TEXT,
  adresse2 TEXT,
  cp TEXT,
  ville TEXT,
  est_referent TEXT DEFAULT 'non',
  actif TEXT DEFAULT 'oui',
  notes TEXT,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  date_modif TEXT,
  user_modif TEXT,
  FOREIGN KEY (fournisseur_id) REFERENCES fournisseurs(id) ON DELETE CASCADE
);
"""

INDEX_SQL_LIST = [
    "CREATE INDEX IF NOT EXISTS idx_fournisseurs_societe  ON fournisseurs(societe)",
    "CREATE INDEX IF NOT EXISTS idx_fournisseurs_ville    ON fournisseurs(ville)",
    "CREATE INDEX IF NOT EXISTS idx_fournisseurs_enseigne ON fournisseurs(enseigne)",
    "CREATE INDEX IF NOT EXISTS idx_contacts_fournisseur  ON fournisseurs_contacts(fournisseur_id)",
    "CREATE INDEX IF NOT EXISTS idx_contacts_nom          ON fournisseurs_contacts(nom)",
    "CREATE INDEX IF NOT EXISTS idx_contacts_email        ON fournisseurs_contacts(email)",
]

TRIGGERS_SQL = """
DROP TRIGGER IF EXISTS fournisseurs_ai_defaults;
DROP TRIGGER IF EXISTS fournisseurs_au_datemodif;

CREATE TRIGGER fournisseurs_ai_defaults
AFTER INSERT ON fournisseurs
FOR EACH ROW
BEGIN
  UPDATE fournisseurs
     SET actif = COALESCE(NULLIF(TRIM(NEW.actif), ''), 'oui'),
         date_creation = COALESCE(NULLIF(TRIM(NEW.date_creation), ''), datetime('now','utc'))
   WHERE id = NEW.id;
END;

CREATE TRIGGER fournisseurs_au_datemodif
AFTER UPDATE ON fournisseurs
FOR EACH ROW
BEGIN
  UPDATE fournisseurs
     SET date_modif = datetime('now','utc')
   WHERE id = NEW.id;
END;
"""

VIEW_MAGASINS_SQL = """
DROP VIEW IF EXISTS magasins;
CREATE VIEW magasins AS
SELECT
  id,
  COALESCE(enseigne, nom) AS enseigne,
  societe, tel_mobile, tel_fixe, email,
  adresse1, adresse2, cp, ville, notes, actif,
  date_creation, date_modif, user_modif
FROM fournisseurs;
"""

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def table_exists(conn, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None

def view_exists(conn, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (name,))
    return cur.fetchone() is not None

def table_columns(conn, table: str):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}

# ---------------------------------------------------------------------
# Étapes
# ---------------------------------------------------------------------
def ensure_fournisseurs_columns(conn: sqlite3.Connection):
    if not table_exists(conn, "fournisseurs"):
        raise RuntimeError("La table 'fournisseurs' est absente (requise par la ramasse).")
    existing = table_columns(conn, "fournisseurs")
    added = []
    for col, decl in COLS_TO_ADD:
        if col not in existing:
            conn.execute(f"ALTER TABLE fournisseurs ADD COLUMN {col} {decl}")
            added.append(col)
    if added:
        print("➕ Colonnes ajoutées dans 'fournisseurs': " + ", ".join(added))
    else:
        print("✓ Aucune colonne à ajouter dans 'fournisseurs'.")

def backfill_fournisseurs(conn: sqlite3.Connection):
    # Exécuter dans une petite transaction dédiée (DML uniquement)
    conn.execute("BEGIN")
    try:
        conn.execute("""
            UPDATE fournisseurs
               SET enseigne = COALESCE(NULLIF(TRIM(enseigne), ''), nom)
             WHERE enseigne IS NULL OR TRIM(enseigne) = ''
        """)
        conn.execute("""
            UPDATE fournisseurs
               SET actif = 'oui'
             WHERE actif IS NULL OR TRIM(actif) = ''
        """)
        conn.execute("""
            UPDATE fournisseurs
               SET date_creation = datetime('now','utc')
             WHERE date_creation IS NULL OR TRIM(date_creation) = ''
        """)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

def ensure_contacts_table(conn: sqlite3.Connection):
    conn.executescript(CREATE_CONTACTS_SQL)

def ensure_indexes(conn: sqlite3.Connection):
    for sql in INDEX_SQL_LIST:
        conn.execute(sql)

def ensure_triggers(conn: sqlite3.Connection):
    conn.executescript(TRIGGERS_SQL)

def ensure_view_magasins(conn: sqlite3.Connection):
    conn.executescript(VIEW_MAGASINS_SQL)

# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    if not os.path.exists(DB_PATH):
        print(f"❌ Base introuvable : {DB_PATH}")
        sys.exit(1)

    print(f"➡ Migration sur : {DB_PATH}")

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA recursive_triggers = OFF")

        # DDL (autocommit implicite)
        ensure_fournisseurs_columns(conn)
        ensure_contacts_table(conn)
        ensure_indexes(conn)
        ensure_triggers(conn)
        ensure_view_magasins(conn)

        # DML (petite transaction dédiée)
        backfill_fournisseurs(conn)

    print("✅ Migration 'fournisseurs' terminée avec succès.")

if __name__ == "__main__":
    main()
