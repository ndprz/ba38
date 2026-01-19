import sqlite3

conn = sqlite3.connect("ba380dev.sqlite")
cursor = conn.cursor()

# Obtenir tous les noms complets de benevoles
cursor.execute("SELECT TRIM(prenom || ' ' || nom) FROM benevoles")
benevoles_noms = set([r[0].strip() for r in cursor.fetchall()])

# Liste des noms du modèle
noms_modele = [
    "BADIN André", "CHERUBIN Guy", "CLEYET Serge", "DALL'ERTA Robert", "DAMATO Robert",
    "FERLAT Claudine", "GERLAT Gilles", "GIGAN Jean Louis", "GRAND Joël", "JUYOUX Eric",
    "LIAUD Raymond", "MARTIN Francis", "MERICHE Boudjema", "MORELLO Guy", "MORIN Michel",
    "OHL Jean", "PROIETTO Jean-Claude", "RENAUD Denis", "REZZA Roger", "SARRUT Michel", "THIREAU Hervé"
]

# Afficher ceux qui ne sont pas strictement trouvés
print("🔍 Comparaison avec noms concaténés en base :\n")
for nom in noms_modele:
    nom_clean = nom.strip()
    if nom_clean not in benevoles_noms:
        print(f"❌ Introuvable : {nom_clean}")
    else:
        print(f"✅ OK : {nom_clean}")
