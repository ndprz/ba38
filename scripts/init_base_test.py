import sqlite3
import os
from faker import Faker

DB_PATH = "/home/ndprz/ba380/ba380_test.sqlite"

def vider_tables(conn):
    conn.execute("DELETE FROM associations")
    conn.execute("DELETE FROM benevoles")
    conn.commit()

def remplir_associations(conn, fake, nb=50):
    colonnes = [row[1] for row in conn.execute("PRAGMA table_info(associations)").fetchall() if row[1] != "Id"]
    placeholders = ", ".join(["?"] * len(colonnes))
    query = f"INSERT INTO associations ({', '.join(colonnes)}) VALUES ({placeholders})"

    for _ in range(nb):
        data = [fake.text(10) for _ in colonnes]
        data[colonnes.index("nom_association")] = fake.company()
        data[colonnes.index("code_VIF")] = fake.unique.bothify(text="??##??")
        data[colonnes.index("CP")] = fake.postcode()
        data[colonnes.index("COMMUNE")] = fake.city()
        data[colonnes.index("courriel_association")] = fake.email()
        conn.execute(query, data)
    conn.commit()

def remplir_benevoles(conn, fake, nb=50):
    colonnes = [row[1] for row in conn.execute("PRAGMA table_info(benevoles)").fetchall() if row[1] != "id"]
    placeholders = ", ".join(["?"] * len(colonnes))
    query = f"INSERT INTO benevoles ({', '.join(colonnes)}) VALUES ({placeholders})"

    for _ in range(nb):
        data = [fake.text(10) for _ in colonnes]
        data[colonnes.index("nom")] = fake.last_name()
        data[colonnes.index("prenom")] = fake.first_name()
        data[colonnes.index("email")] = fake.email()
        data[colonnes.index("code_postal")] = fake.postcode()
        data[colonnes.index("ville")] = fake.city()
        conn.execute(query, data)
    conn.commit()

def main():
    if not os.path.exists(DB_PATH):
        print(f"❌ Base introuvable : {DB_PATH}")
        return

    fake = Faker("fr_FR")
    conn = sqlite3.connect(DB_PATH)
    try:
        vider_tables(conn)
        remplir_associations(conn, fake)
        remplir_benevoles(conn, fake)
        print("✅ Base test initialisée avec 50 enregistrements anonymisés.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
