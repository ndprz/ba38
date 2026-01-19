import pandas as pd
import sqlite3
import os
import re

# Définition du chemin absolu du script
base_dir = os.path.dirname(os.path.abspath(__file__))
excel_file = os.path.join(base_dir, "Benevoles salariésBAI.xlsm")
db_file = os.path.join(base_dir, "benevoles.sqlite")
sheet_name = "bénévoles actifs+salariés "

# Vérification de la présence d'openpyxl
try:
    import openpyxl
except ImportError:
    print("❌ Erreur : 'openpyxl' est requis pour lire les fichiers Excel.\n➡️ Installez-le avec : pip install openpyxl")
    exit(1)

# Chargement des données depuis Excel
df = pd.read_excel(excel_file, sheet_name=sheet_name, dtype=str)

# Nettoyage des noms de colonnes pour SQLite (éviter les caractères spéciaux et les chiffres en début de nom)
def clean_column_name(col_name):
    col_name = re.sub(r'[^a-zA-Z0-9_]', '_', col_name)  # Remplace tout sauf lettres, chiffres et underscore
    if col_name[0].isdigit():
        col_name = "_" + col_name  # Ajoute un underscore si le nom commence par un chiffre
    return col_name

df.columns = [clean_column_name(col) for col in df.columns]

# Nettoyage des numéros de téléphone (suppression des points et espaces)
if "Telephone_fixe" in df.columns:
    df["Telephone_fixe"] = df["Telephone_fixe"].str.replace(r'[^0-9]', '', regex=True)
if "Telephone_portable" in df.columns:
    df["Telephone_portable"] = df["Telephone_portable"].str.replace(r'[^0-9]', '', regex=True)

# Vérification et nettoyage des emails (suppression des espaces avant/après et des valeurs vides)
if "Mail" in df.columns:
    df["Mail"] = df["Mail"].str.strip()
    df["Mail"] = df["Mail"].replace("", None)

# Affichage des colonnes après nettoyage
print("🛠️ Colonnes après nettoyage :", df.columns.tolist())

# Connexion à SQLite
conn = sqlite3.connect(db_file)
cursor = conn.cursor()

# Création de la table si elle n'existe pas
create_table_query = f'''
CREATE TABLE IF NOT EXISTS benevoles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {', '.join([f'{col} TEXT' for col in df.columns])}
);
'''
cursor.execute(create_table_query)
conn.commit()

# Insertion des données en évitant les doublons
df.to_sql("benevoles", conn, if_exists="replace", index=False)

print(f"✅ Importation terminée avec succès ! Base créée : {db_file}")

# Fermeture de la connexion
conn.close()
