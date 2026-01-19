# migrer_ramasse_benevoles.py
import sys
import os
os.environ["TEST_MODE"] = "0"  # 👈 Forcer la PROD

# 🔧 Ajouter le chemin du projet (dossier dev) au PYTHONPATH
sys.path.append('/home/ndprz/ba380')


from utils import get_db_path, upload_database, write_log

import sqlite3

def migrate_benevoles_ramasse_fields():
    db_path = get_db_path()
    write_log(f"🚧 Début de la migration des champs RAMASSE dans {db_path}")

    fields_to_remove = [
        'chauf_accomp_comb', 'chauf_accomp_viande', 'chauf_accomp_grand_frais',
        'chauf_accomp_meylan', 'chauf_accomp_st_egreve', 'chauf_ramasse_x_agro_alim_aa',
        'ramasse_echi', 'ramasse_meylan', 'ramasse_st_egreve'
    ]

    fields_to_rename = {
        'chauffeur': 'ramasse_chauffeur',
        'equipier': 'ramasse_equipier',
        'responsable': 'ramasse_responsable_tri',
        'lundi': 'ramasse_lundi',
        'mardi': 'ramasse_mardi',
        'mercredi': 'ramasse_mercredi',
        'jeudi': 'ramasse_jeudi',
        'vendredi': 'ramasse_vendredi'
    }

    new_fields = ['ramasse_tri1', 'ramasse_tri2', 'ramasse_tri3', 'ramasse_tri4']

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Lire le schéma de benevoles
        cursor.execute("PRAGMA table_info(benevoles)")
        all_columns = [row['name'] for row in cursor.fetchall()]

        # 2. Construire la nouvelle structure
        kept_columns = [c for c in all_columns if c not in fields_to_remove]
        renamed_columns = [fields_to_rename.get(c, c) for c in kept_columns]

        create_columns = []
        for old_name in kept_columns:
            new_name = fields_to_rename.get(old_name, old_name)
            cursor.execute(f"PRAGMA table_info(benevoles)")
            col_type = next(r['type'] for r in cursor.fetchall() if r['name'] == old_name)
            create_columns.append(f"`{new_name}` {col_type}")

        # Ajout uniquement si le champ n’existe pas déjà
        cursor.execute("PRAGMA table_info(benevoles)")
        existing_fields = [row['name'] for row in cursor.fetchall()]
        for field in new_fields:
            if field not in existing_fields:
                create_columns.append(f"`{field}` TEXT")


        # 3. Recréer benevoles_new
        cursor.execute("DROP TABLE IF EXISTS benevoles_new")
        cursor.execute(f"CREATE TABLE benevoles_new ({', '.join(create_columns)})")

        # 4. Copier les données
        select_clause = ", ".join([f"`{col}`" for col in kept_columns])
        insert_clause = ", ".join([f"`{fields_to_rename.get(col, col)}`" for col in kept_columns])
        cursor.execute(f"""
            INSERT INTO benevoles_new ({insert_clause})
            SELECT {select_clause}
            FROM benevoles
        """)

        # 5. Remplacer l’ancienne table
        cursor.execute("DROP TABLE benevoles")
        cursor.execute("ALTER TABLE benevoles_new RENAME TO benevoles")

        write_log("✅ Table benevoles migrée avec succès.")

        # 6. Mettre à jour field_groups
        for old, new in fields_to_rename.items():
            cursor.execute("UPDATE field_groups SET field_name = ? WHERE field_name = ?", (new, old))
        cursor.execute(f"""
            DELETE FROM field_groups
            WHERE field_name IN ({','.join('?' for _ in fields_to_remove)})
        """, fields_to_remove)

        for i, field in enumerate(new_fields, start=100):
            cursor.execute("""
                INSERT INTO field_groups (field_name, group_name, display_order, appli)
                VALUES (?, ?, ?, ?)
            """, (field, 'Ramasse - Tri', i, 'benevoles'))

        conn.commit()
        write_log("✅ field_groups mis à jour.")
        upload_database()
        write_log("📤 Base sauvegardée sur Google Drive.")

if __name__ == "__main__":
    migrate_benevoles_ramasse_fields()
