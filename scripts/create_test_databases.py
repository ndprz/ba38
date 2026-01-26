#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import shutil
import sqlite3
import random
import string
from pathlib import Path

# -------------------------------------------------------------------
# PYTHONPATH
# -------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from utils import write_log

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
LIMIT = 10
NO_LIMIT_TABLES = {"field_groups", "parametres"}

SUMMARY = []

def summary(msg):
    SUMMARY.append(msg)
    print(msg)
    write_log(msg)

# -------------------------------------------------------------------
# RANDOM GENERATORS
# -------------------------------------------------------------------
def rnd_txt(prefix="TXT"):
    return f"{prefix}_{''.join(random.choices(string.ascii_uppercase, k=5))}"

def rnd_email():
    return ''.join(random.choices(string.ascii_lowercase, k=8)) + "@example.org"

def rnd_tel():
    return "06" + ''.join(random.choices(string.digits, k=8))

def rnd_cp():
    return ''.join(random.choices(string.digits, k=5))

def rnd_ville():
    return "VILLE_" + ''.join(random.choices(string.ascii_uppercase, k=4))

def rnd_siret():
    return ''.join(random.choices(string.digits, k=14))

# -------------------------------------------------------------------
# PATH RESOLUTION (anti instance/instance)
# -------------------------------------------------------------------
BASE_PROD_DIR = Path("/srv/ba38/prod")
BASE_DEV_DIR = Path("/srv/ba38/dev")

def resolve_db(base_dir: Path, db_name: str) -> Path:
    p = Path(db_name)
    if p.is_absolute():
        return p
    if p.parts and p.parts[0] == "instance":
        return base_dir / p
    return base_dir / "instance" / p

PROD_DB_NAME = os.getenv("SQLITE_DB_PROD", "ba380.sqlite")
PROD_TEST_DB_NAME = os.getenv("SQLITE_DB_PROD_TEST", "ba380_test.sqlite")
DEV_TEST_DB_NAME = os.getenv("SQLITE_DB_DEV_TEST", "ba380dev_test.sqlite")

BASE_PROD = resolve_db(BASE_PROD_DIR, PROD_DB_NAME)
BASE_TEST_PROD = resolve_db(BASE_PROD_DIR, PROD_TEST_DB_NAME)
BASE_TEST_DEV = resolve_db(BASE_DEV_DIR, DEV_TEST_DB_NAME)

# -------------------------------------------------------------------
def create_copy(src: Path, dst: Path):
    if not src.exists():
        raise FileNotFoundError(f"❌ Base source introuvable : {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    summary(f"✅ Copie créée : {dst}")

# -------------------------------------------------------------------
def anonymize_database(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    tables = [r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]

    for table in tables:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        pk = next((r["name"] for r in c.execute(f"PRAGMA table_info({table})") if r["pk"]), None)

        # ---------------- LIMIT ----------------
        if table not in NO_LIMIT_TABLES and pk:
            count = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count > LIMIT:
                c.execute(f"""
                    DELETE FROM {table}
                    WHERE {pk} NOT IN (
                        SELECT {pk} FROM {table}
                        ORDER BY {pk}
                        LIMIT {LIMIT}
                    )
                """)
                conn.commit()
                summary(f"📉 {table} limité à {LIMIT}")

        # ---------------- BENEVOLES ----------------
        if table == "benevoles":
            summary("🧩 Anonymisation benevoles")
            ids = [r["id"] for r in c.execute("SELECT id FROM benevoles").fetchall()]
            for bid in ids:
                c.execute("""
                    UPDATE benevoles SET
                        nom=?, prenom=?, email=?,
                        telephone_fixe=?, telephone_portable=?,
                        code_postal=?, ville=?, rue=?
                    WHERE id=?
                """, (
                    rnd_txt("Nom"), rnd_txt("Prenom"), rnd_email(),
                    rnd_tel(), rnd_tel(),
                    rnd_cp(), rnd_ville(), rnd_txt("Rue"),
                    bid
                ))
            conn.commit()

        elif table == "benevoles_inactifs":
            c.execute("DELETE FROM benevoles_inactifs")
            conn.commit()
            summary("🧹 Table benevoles_inactifs vidée")

        # ---------------- ASSOCIATIONS ----------------
        elif table == "associations":
            summary("🏛 Anonymisation associations")

            ids = [r["Id"] for r in c.execute("SELECT Id FROM associations").fetchall()]

            for aid in ids:
                c.execute("""
                    UPDATE associations SET
                        nom_association=?,
                        code_SIRET=?,
                        raison_sociale_VIF=?,
                        adresse_siege=?,
                        adresse_association_1=?,
                        adresse_association_2=?,
                        CP=?,
                        COMMUNE=?,
                        tel_association=?,
                        courriel_association=?,

                        -- Courriels
                        courriel_distribution=?,
                        courriel_president=?,
                        courriel_resp_accueil=?,
                        courriel_resp_collecte=?,
                        courriel_resp_Hysa=?,
                        courriel_resp_IE1=?,
                        courriel_resp_IE2=?,
                        courriel_resp_operationnel=?,
                        courriel_resp_proxidon=?,
                        courriel_resp_tresorerie=?,
                        courriel_resp_tresorerie2=?,

                        -- Responsables / noms
                        nom_president_ou_officiel=?,
                        responsable_accueil=?,
                        responsable_collecte=?,
                        responsable_distribution=?,
                        responsable_HySA=?,
                        responsable_IE=?,
                        responsable_operationnel=?,
                        responsable_proxidon=?,
                        responsable_tresorerie=?,
                        responsable_tresorerie2=?,

                        -- Téléphones
                        tel_president_officiel_1=?,
                        tel_president_officiel_2=?,
                        tel_resp_accueil=?,
                        tel_resp_collecte=?,
                        tel_resp_distribution_1=?,
                        tel_resp_distribution_2=?,
                        teL_resp_Hysa_1=?,
                        tel_resp_Hysa_2=?,
                        tel_resp_IE=?,
                        tel_resp_operationnel_1=?,
                        tel_resp_operationnel_2=?,
                        tel_resp_proxidon=?,
                        tel_resp_tresorerie_1=?,
                        tel_resp_tresorerie_2=?

                    WHERE Id=?
                """, (
                    rnd_txt("Association"),
                    rnd_siret(),
                    rnd_txt("RS"),
                    rnd_txt("Adresse"),
                    rnd_txt("Adresse"),
                    rnd_txt("Adresse"),
                    rnd_cp(),
                    rnd_ville(),
                    rnd_tel(),
                    rnd_email(),

                    # Courriels
                    rnd_email(),
                    rnd_email(),
                    rnd_email(),
                    rnd_email(),
                    rnd_email(),
                    rnd_email(),
                    rnd_email(),
                    rnd_email(),
                    rnd_email(),
                    rnd_email(),
                    rnd_email(),

                    # Responsables / noms
                    rnd_txt("Nom"),
                    rnd_txt("Resp"),
                    rnd_txt("Resp"),
                    rnd_txt("Resp"),
                    rnd_txt("Resp"),
                    rnd_txt("Resp"),
                    rnd_txt("Resp"),
                    rnd_txt("Resp"),
                    rnd_txt("Resp"),
                    rnd_txt("Resp"),

                    # Téléphones
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),
                    rnd_tel(),

                    aid
                ))

            conn.commit()

        # ---------------- FOURNISSEURS ----------------
        elif table == "fournisseurs":
            summary("🚚 Anonymisation fournisseurs")
            ids = [r["id"] for r in c.execute("SELECT id FROM fournisseurs").fetchall()]
            for fid in ids:
                c.execute("""
                    UPDATE fournisseurs SET
                        nom=?, adresse=?, cp=?, ville=?, tel=?, mail=?
                    WHERE id=?
                """, (
                    rnd_txt("Fournisseur"), rnd_txt("Adresse"),
                    rnd_cp(), rnd_ville(), rnd_tel(), rnd_email(),
                    fid
                ))
            conn.commit()

        # ---------------- FOURNISSEURS CONTACTS ----------------
        elif table == "fournisseurs_contacts":
            summary("👥 Anonymisation fournisseurs_contacts")
            ids = [r["id"] for r in c.execute("SELECT id FROM fournisseurs_contacts").fetchall()]
            for cid in ids:
                c.execute("""
                    UPDATE fournisseurs_contacts SET
                        prenom=?, nom=?, tel_mobile=?, tel_fixe=?, email=?,
                        adresse1=?, adresse2=?, cp=?, ville=?
                    WHERE id=?
                """, (
                    rnd_txt("Prenom"), rnd_txt("Nom"),
                    rnd_tel(), rnd_tel(), rnd_email(),
                    rnd_txt("Adresse"), rnd_txt("Adresse"),
                    rnd_cp(), rnd_ville(),
                    cid
                ))
            conn.commit()

    conn.close()
    summary(f"✅ Anonymisation terminée : {db_path}")

# -------------------------------------------------------------------
def create_test_databases():
    SUMMARY.clear()
    summary("🧩 Création des bases de test anonymisées")

    if not BASE_PROD.exists():
        raise FileNotFoundError(
            f"❌ Base PROD introuvable : {BASE_PROD}\n"
            "Vérifie SQLITE_DB_PROD et le dossier instance/"
        )

    create_copy(BASE_PROD, BASE_TEST_PROD)
    anonymize_database(BASE_TEST_PROD)

    create_copy(BASE_PROD, BASE_TEST_DEV)
    anonymize_database(BASE_TEST_DEV)

    summary("🎉 Bases de test créées avec succès")
    return "\n".join(SUMMARY)

# -------------------------------------------------------------------
if __name__ == "__main__":
    create_test_databases()
