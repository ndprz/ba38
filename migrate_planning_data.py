import sqlite3

def get_id_by_fullname(cursor, table, value):
    if not value or value.strip() == "-":
        return None
    value = value.strip()

    if table == "benevoles":
        cursor.execute("""
            SELECT id FROM benevoles
            WHERE LOWER(TRIM(nom || ' ' || prenom)) = LOWER(?)
        """, (value,))
    elif table == "camions":
        cursor.execute("""
            SELECT id FROM camions
            WHERE LOWER(nom) = LOWER(?)
        """, (value,))
    elif table == "fournisseurs":
        cursor.execute("""
            SELECT id FROM fournisseurs
            WHERE LOWER(nom) = LOWER(?)
        """, (value,))
    elif table == "tournees":
        cursor.execute("""
            SELECT id FROM tournees
            WHERE LOWER(nom) = LOWER(?)
        """, (value,))
    else:
        return None

    row = cursor.fetchone()
    return row[0] if row else None

def migrate_planning_model():
    print("📋 Migration du modèle planning_standard_ramasse...")

    conn = sqlite3.connect("ba380dev.sqlite")
    cursor = conn.cursor()

    # 🔄 Réinitialisation de la table
    cursor.execute("DELETE FROM planning_standard_ramasse_ids")

    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    not_found = {"tournees": [], "benevoles": [], "camions": []}
    lignes_inserees = 0

    row = cursor.execute("SELECT * FROM planning_standard_ramasse WHERE id = 1").fetchone()
    columns = [col[1] for col in cursor.execute("PRAGMA table_info(planning_standard_ramasse)")]

    for jour in jours:
        for i in range(1, 6):
            suffix = f"{jour}_tournee{i}"
            if suffix not in columns:
                continue

            tournee = row[columns.index(suffix)]
            chauffeur = row[columns.index(f"{suffix}_chauffeur")]
            responsable = row[columns.index(f"{suffix}_responsable")]
            equipier = row[columns.index(f"{suffix}_equipier")]
            camion = row[columns.index(f"{suffix}_camion")]

            tournee_id = get_id_by_fullname(cursor, "tournees", tournee)
            chauffeur_id = get_id_by_fullname(cursor, "benevoles", chauffeur)
            responsable_id = get_id_by_fullname(cursor, "benevoles", responsable)
            equipier_id = get_id_by_fullname(cursor, "benevoles", equipier)
            camion_id = get_id_by_fullname(cursor, "camions", camion)

            if tournee and not tournee_id:
                not_found["tournees"].append(tournee)
            if chauffeur and not chauffeur_id:
                not_found["benevoles"].append(chauffeur)
            if responsable and not responsable_id:
                not_found["benevoles"].append(responsable)
            if equipier and not equipier_id:
                not_found["benevoles"].append(equipier)
            if camion and not camion_id:
                not_found["camions"].append(camion)

            cursor.execute("""
                INSERT INTO planning_standard_ramasse_ids (
                    jour, numero, tournee_id,
                    chauffeur_id, responsable_id, equipier_id, camion_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (jour, i, tournee_id, chauffeur_id, responsable_id, equipier_id, camion_id))
            lignes_inserees += 1

    conn.commit()
    conn.close()

    print(f"\n✅ {lignes_inserees} lignes insérées dans planning_standard_ramasse_ids.")

    for table, valeurs in not_found.items():
        if valeurs:
            uniques = sorted(set(valeurs))
            print(f"\n❌ {len(uniques)} valeurs non trouvées dans '{table}':")
            for val in uniques:
                print("   -", val)

def migrate_tournees_fournisseurs():
    print("\n📋 Migration des fournisseurs de tournées...")
    conn = sqlite3.connect("ba380dev.sqlite")
    cursor = conn.cursor()

    # 🔄 Réinitialisation
    cursor.execute("DELETE FROM tournees_fournisseurs")

    rows = cursor.execute("""
        SELECT id, fournisseur1, fournisseur2, fournisseur3, fournisseur4, fournisseur5
        FROM tournees
    """).fetchall()

    lignes_inserees = 0
    not_found = []

    for tournee_id, *fournisseurs in rows:
        for nom in fournisseurs:
            if nom and nom.strip():
                fournisseur_id = get_id_by_fullname(cursor, "fournisseurs", nom)
                if fournisseur_id:
                    cursor.execute("""
                        INSERT INTO tournees_fournisseurs (tournee_id, fournisseur_id)
                        VALUES (?, ?)
                    """, (tournee_id, fournisseur_id))
                    lignes_inserees += 1
                else:
                    not_found.append(nom)

    conn.commit()
    conn.close()

    print(f"\n✅ {lignes_inserees} enregistrements insérés dans tournees_fournisseurs.")

    if not_found:
        uniques = sorted(set(not_found))
        print(f"\n❌ {len(uniques)} fournisseurs non trouvés :")
        for nom in uniques:
            print("   -", nom)

if __name__ == "__main__":
    migrate_planning_model()
    migrate_tournees_fournisseurs()
