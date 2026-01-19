#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script : create_test_databases.py
Objectif :
Créer et anonymiser les bases de test DEV et PROD à partir de la base ba380.sqlite,
en limitant chaque table à 100 enregistrements.
⚠️ La table 'users' n'est PAS anonymisée pour garder les connexions valides.
"""

import os
import shutil
import sqlite3
import random
import string

# ========================
# 🔧 Configuration
# ========================
BASE_PROD = "/home/ndprz/ba380/ba380.sqlite"
BASE_TEST_PROD = "/home/ndprz/ba380/ba380_test.sqlite"
BASE_TEST_DEV = "/home/ndprz/dev/ba380dev_test.sqlite"
LIMIT = 100  # Limite de lignes par table

# ========================
# 🧰 Fonctions utilitaires
# ========================

def random_nom():
    return "Nom" + ''.join(random.choices(string.ascii_uppercase, k=4))

def random_prenom():
    return "Prenom" + ''.join(random.choices(string.ascii_uppercase, k=4))

def random_email():
    return ''.join(random.choices(string.ascii_lowercase, k=6)) + "@example.org"

def random_tel():
    return "06" + ''.join(random.choices(string.digits, k=8))

def write_log(msg):
    print(f"[INFO] {msg}")

# ========================
# 🚀 Étapes principales
# ========================

def create_test_copy(source_path, target_path):
    """Copie la base source vers une base de test (overwrite)."""
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Base source introuvable : {source_path}")

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    shutil.copy2(source_path, target_path)
    write_log(f"✅ Copie créée : {target_path}")

def anonymize_and_limit_database(db_path):
    """Anonymise les tables sensibles et limite leur taille à 100 enregistrements."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Liste des tables (hors sqlite_sequence et vues)
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]

    for table in tables:
        try:
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if not cols:
                continue

            # ✅ Limiter à 100 enregistrements
            count = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count > LIMIT:
                write_log(f"📉 Réduction de {table} ({count} → {LIMIT})")
                c.execute(f"""
                    DELETE FROM {table}
                    WHERE id NOT IN (
                        SELECT id FROM {table} ORDER BY id LIMIT {LIMIT}
                    )
                """)
                conn.commit()

            # ======================
            # 👥 Table bénévole
            # ======================
            if table == "benevoles":
                write_log("🧩 Anonymisation table benevoles…")
                if all(col in cols for col in ("nom", "prenom")):
                    rows = c.execute("SELECT id FROM benevoles").fetchall()
                    for r in rows:
                        id_ = r["id"]
                        updates = {}
                        if "nom" in cols: updates["nom"] = random_nom()
                        if "prenom" in cols: updates["prenom"] = random_prenom()
                        if "email" in cols: updates["email"] = random_email()
                        if "telephone" in cols: updates["telephone"] = random_tel()

                        if updates:
                            set_clause = ", ".join([f"{k}=?" for k in updates])
                            c.execute(f"UPDATE benevoles SET {set_clause} WHERE id=?",
                                      list(updates.values()) + [id_])
                    conn.commit()
                else:
                    write_log("⚠️ Colonnes attendues absentes dans benevoles, aucune anonymisation appliquée.")

            # ======================
            # 🏛 Table associations
            # ======================
            elif table == "associations":
                write_log("🏛 Anonymisation table associations…")
                rows = c.execute("SELECT id FROM associations").fetchall()
                for r in rows:
                    id_ = r["id"]
                    updates = {}
                    if "nom_association" in cols: updates["nom_association"] = "Association_" + ''.join(random.choices(string.ascii_uppercase, k=4))
                    if "courriel_association" in cols: updates["courriel_association"] = random_email()
                    if "courriel_president" in cols: updates["courriel_president"] = random_email()
                    if "courriel_resp_operationnel" in cols: updates["courriel_resp_operationnel"] = random_email()
                    if "contact_nom" in cols: updates["contact_nom"] = random_nom()
                    if "contact_prenom" in cols: updates["contact_prenom"] = random_prenom()
                    if "contact_tel" in cols: updates["contact_tel"] = random_tel()

                    if updates:
                        set_clause = ", ".join([f"{k}=?" for k in updates])
                        c.execute(f"UPDATE associations SET {set_clause} WHERE id=?",
                                  list(updates.values()) + [id_])
                conn.commit()

            # ======================
            # 🔒 Table log_connexions
            # ======================
            elif table == "log_connexions":
                write_log("🧹 Nettoyage table log_connexions…")
                c.execute("DELETE FROM log_connexions")
                conn.commit()

            # ======================
            # 👤 Table users
            # ======================
            elif table == "users":
                write_log("Table users conservée telle quelle ✅ (aucune anonymisation).")

        except Exception as e:
            write_log(f"⚠️ Erreur sur la table {table}: {e}")

    conn.close()
    write_log(f"✅ Anonymisation + limitation terminées pour {db_path}")

# ========================
# 🧩 Programme principal
# ========================

if __name__ == "__main__":
    write_log("=== Création des bases de test anonymisées (100 lignes max) ===")

    create_test_copy(BASE_PROD, BASE_TEST_PROD)
    anonymize_and_limit_database(BASE_TEST_PROD)

    create_test_copy(BASE_PROD, BASE_TEST_DEV)
    anonymize_and_limit_database(BASE_TEST_DEV)

    write_log("🎉 Bases de test créées et anonymisées avec succès.")
