#!/usr/bin/env python3
import os
import sqlite3
import pandas as pd

# --- Configurations ---
# Fichier Excel à importer
EXCEL_FILE = "/home/ndprz/dev/20250724 partenaires benefs.xlsx"
SHEET_NAME = "benef"

# Détection de l'environnement
env = os.getenv("ENVIRONMENT", "dev").lower()
if env == "prod":
    db_path = "/home/ndprz/ba380/ba380.sqlite"
else:
    db_path = "/home/ndprz/dev/ba380dev.sqlite"

print(f"📂 Utilisation de la base : {db_path}")

# --- Chargement du fichier Excel ---
if not os.path.exists(EXCEL_FILE):
    print(f"❌ Fichier Excel introuvable : {EXCEL_FILE}")
    exit(1)

try:
    df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME)
except Exception as e:
    print(f"❌ Erreur lors de la lecture du fichier Excel : {e}")
    exit(1)

# --- Connexion à la base ---
if not os.path.exists(db_path):
    print(f"❌ Base de données introuvable : {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# --- Mise à jour des données ---
updates = 0
for _, row in df.iterrows():
    code_vif = str(row["Partenaire"]).strip().zfill(8)
    try:
        nbre = int(row["nbre_beneficiaires"])
    except (ValueError, TypeError):
        print(f"⚠️ Valeur invalide ignorée pour Partenaire={code_vif} : {row['nbre_beneficiaires']}")
        continue

    cursor.execute(
        "UPDATE associations SET nbre_beneficaires = ? WHERE code_vif = ?",
        (nbre, code_vif)
    )
    if cursor.rowcount > 0:
        print(f"✅ {code_vif} → {nbre}")
        updates += 1
    else:
        print(f"⚠️ Aucun enregistrement trouvé pour code_vif {code_vif}")

conn.commit()
conn.close()

print(f"---
Mise à jour terminée : {updates} lignes modifiées.")
