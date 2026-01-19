import sqlite3
import pandas as pd

# 📂 Connexion à la base de données
# 🔥 Remplacer ce chemin par le chemin du fichier partagé sur votre Google Drive local
db_path = "/chemin/vers/ba380.sqlite"

conn = sqlite3.connect(db_path)

# 📋 Requête SQL avec concaténation
query = """
SELECT 
    code_VIF,
    nom_association,
    besoins_particuliers,
    validite,
    heure_de_passage,
    (COALESCE(emplacement, '') || ' ' || COALESCE(heure_de_passage, '')) AS emplacement_et_heure
FROM associations
ORDER BY nom_association
"""

# 📥 Lire la base de données dans un DataFrame
df = pd.read_sql_query(query, conn)

# 📄 Exporter vers un fichier Excel
output_file = "export_associations.xlsx"
df.to_excel(output_file, index=False, engine="openpyxl")

# 🔥 Important
print(f"✅ Fichier Excel généré : {output_file}")

# 📕 Fermer la connexion
conn.close()
