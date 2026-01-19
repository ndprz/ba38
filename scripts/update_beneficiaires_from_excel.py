import sqlite3
import pandas as pd

# Fichier Excel
excel_path = "/home/ndprz/dev/20250724 partenaires benefs.xlsx"  # chemin à ajuster
df = pd.read_excel(excel_path, sheet_name="benef")

# Base de données
db_path = "/home/ndprz/ba380/ba380_test.sqlite"  # adapter à PROD si besoin
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Mise à jour des bénéficiaires
for _, row in df.iterrows():
    code_vif = str(row["Partenaire"]).strip()
    # Forcer 8 caractères (ajout de 0 si besoin)
    code_vif = code_vif.zfill(8)

    try:
        nbre = int(row["nbre_beneficiaires"])
    except ValueError:
        continue  # ignorer si non numérique

    cursor.execute(
        "UPDATE associations SET nbre_beneficaires = ? WHERE code_vif = ?",
        (nbre, code_vif)
    )

conn.commit()
conn.close()
print("✅ Mise à jour terminée !")
