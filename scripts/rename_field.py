import os
import sqlite3
from flask import Blueprint, request, render_template, redirect, url_for, flash
from dotenv import dotenv_values
from utils import write_log

# ---------------------------------------------------------------------------
# Chargement des deux fichiers .env (DEV et PROD)
# Chaque environnement a son propre BA38_BASE_DIR qui sert à résoudre
# les chemins relatifs déclarés dans les variables SQLITE_DB*.
# ---------------------------------------------------------------------------

ENV_DEV  = dotenv_values('/srv/ba38/dev/.env')
ENV_PROD = dotenv_values('/srv/ba38/prod/.env')  # adapte si le chemin diffère

# Répertoires racine de chaque environnement
BASE_DIR_DEV  = ENV_DEV.get("BA38_BASE_DIR",  "/srv/ba38/dev")
BASE_DIR_PROD = ENV_PROD.get("BA38_BASE_DIR", "/srv/ba38/prod")


def resoudre_chemin(base_dir, chemin_relatif):
    """
    Construit le chemin absolu d'une base SQLite.
    - Si chemin_relatif est None ou vide  → retourne None
    - Si chemin_relatif est déjà absolu   → le retourne tel quel
    - Sinon                               → joint base_dir + chemin_relatif
    Exemples :
        resoudre_chemin("/srv/ba38/dev",  "instance/ba380dev.sqlite")
        → "/srv/ba38/dev/instance/ba380dev.sqlite"
        resoudre_chemin("/srv/ba38/prod", "instance/ba380.sqlite")
        → "/srv/ba38/prod/instance/ba380.sqlite"
    """
    if not chemin_relatif:
        return None
    if os.path.isabs(chemin_relatif):
        return chemin_relatif
    return os.path.join(base_dir, chemin_relatif)


# ---------------------------------------------------------------------------
# Dictionnaire des bases à traiter, avec leur chemin absolu résolu.
# Structure : { "nom_affiché": "chemin/absolu/vers/base.sqlite" }
#
# DEV  → lit ENV_DEV  + préfixe BASE_DIR_DEV  (/srv/ba38/dev/instance/…)
# PROD → lit ENV_PROD + préfixe BASE_DIR_PROD (/srv/ba38/prod/instance/…)
# ---------------------------------------------------------------------------

BASES = {
    # --- Environnement DEV ---
    "ba380dev":      resoudre_chemin(BASE_DIR_DEV,  ENV_DEV.get("SQLITE_DB")),           # instance/ba380dev.sqlite
    "ba380dev_test": resoudre_chemin(BASE_DIR_DEV,  ENV_DEV.get("SQLITE_DB_DEV_TEST")),  # instance/ba380dev_test.sqlite

    # --- Environnement PROD ---
    "ba380":         resoudre_chemin(BASE_DIR_PROD, ENV_PROD.get("SQLITE_DB")),           # instance/ba380.sqlite
    "ba380_test":    resoudre_chemin(BASE_DIR_PROD, ENV_PROD.get("SQLITE_DB_TEST")),      # instance/ba380_test.sqlite
}

# Seules les vraies tables utilisateurs sont modifiables
TABLES_AUTORISEES = ["benevoles", "associations"]

rename_bp = Blueprint("rename", __name__)


# ---------------------------------------------------------------------------
# Fonction de renommage bas niveau (opère sur une seule base SQLite)
# ---------------------------------------------------------------------------

def rename_column(db_path, table, old_col, new_col):
    """
    Renomme la colonne old_col en new_col dans table de la base db_path.

    SQLite ne supporte pas ALTER TABLE … RENAME COLUMN avant la version 3.25.
    Pour garantir la compatibilité, on utilise la méthode classique :
      1. Créer une table temporaire avec la nouvelle définition
      2. Copier les données
      3. Supprimer l'ancienne table
      4. Renommer la temporaire

    Retourne une chaîne décrivant le résultat (✅ succès, ❌ erreur, ⚠️ avertissement).
    """
    write_log(f"🔧 rename_column - db: {db_path}, table: {table}, old: {old_col}, new: {new_col}")

    # Garde-fou : new_col ne doit jamais être vide ou "none"
    if not new_col or str(new_col).strip().lower() == "none":
        write_log("❌ Abandon : new_col est invalide (None ou vide)")
        raise ValueError("new_col est vide ou 'None'")

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # Récupère la définition complète de la table (cid, name, type, …)
        cursor.execute(f"PRAGMA table_info({table})")
        columns_info = cursor.fetchall()
        column_names = [col[1] for col in columns_info]
        write_log(f"📋 Colonnes existantes : {column_names}")

        if old_col not in column_names:
            return f"❌ {old_col} introuvable dans {table}"

        if new_col in column_names:
            return f"⚠️ {new_col} existe déjà dans {table}"

        # Reconstruit la définition des colonnes en remplaçant old_col par new_col
        new_columns_def = [
            f"{(new_col if col[1] == old_col else col[1])} {col[2]}"
            for col in columns_info
        ]
        columns_sql = ", ".join(new_columns_def)

        # Clause SELECT pour recopier les données avec l'alias de renommage
        columns_copy = ", ".join([
            f"{old_col} AS {new_col}" if col[1] == old_col else col[1]
            for col in columns_info
        ])

        temp_table = f"{table}_temp"
        write_log(f"📐 Création table temp avec : {columns_sql}")

        try:
            cursor.execute(f"CREATE TABLE {temp_table} ({columns_sql})")
            cursor.execute(f"INSERT INTO {temp_table} SELECT {columns_copy} FROM {table}")
            cursor.execute(f"DROP TABLE {table}")
            cursor.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")
            conn.commit()
            return f"✅ Colonne renommée dans {table}"
        except Exception as e:
            conn.rollback()
            write_log(f"❌ Exception dans rename_column : {e}")
            return f"❌ Erreur : {e}"


# ---------------------------------------------------------------------------
# Route Flask
# ---------------------------------------------------------------------------

@rename_bp.route("/rename_field", methods=["GET", "POST"])
def rename_field():
    """
    Route de renommage de champ.

    GET  : affiche le formulaire.
    POST : applique le renommage sur TOUTES les bases (DEV + PROD)
           pour la table et la colonne indiquées.

    Comportement si new_col est vide :
      → génère automatiquement un nom "libreX" (X = premier entier disponible)
        propre à chaque base (l'index peut différer selon les bases).

    Après le renommage SQL, met à jour la table field_groups si la colonne
    y est référencée (field_name + appli).
    """
    resultats = []

    if request.method == "POST":
        table   = request.form.get("table", "").strip()
        old_col = request.form.get("old_col", "").strip()
        raw_new = request.form.get("new_col", "").strip()

        # Normalise new_col : None si vide ou littéralement "none"
        new_col_saisi = raw_new if raw_new and raw_new.lower() != "none" else None

        write_log(f"📨 Formulaire reçu : table={table}, old_col={old_col}, new_col={new_col_saisi}")

        # Sécurité : on n'autorise que les tables déclarées
        if table not in TABLES_AUTORISEES:
            flash("❌ Table non autorisée", "danger")
            return redirect(url_for("rename.rename_field"))

        # Parcours de toutes les bases (DEV + PROD)
        for nom_base, chemin in BASES.items():

            write_log(f"🔍 Traitement base {nom_base} → {chemin}")

            # Vérifie que le chemin a pu être résolu et que le fichier existe
            if not chemin:
                resultats.append(f"⚠️ {nom_base} : chemin non configuré dans .env")
                continue
            if not os.path.exists(chemin):
                resultats.append(f"⚠️ {nom_base} : fichier introuvable ({chemin})")
                continue

            try:
                # ----------------------------------------------------------
                # 1) Déterminer le nom de la colonne cible
                #    Si new_col n'a pas été saisi, générer "libreX"
                #    (calcul indépendant par base, car les colonnes libres
                #     peuvent différer entre DEV et PROD)
                # ----------------------------------------------------------
                if not new_col_saisi:
                    with sqlite3.connect(chemin) as conn:
                        conn.row_factory = sqlite3.Row
                        cursor = conn.cursor()
                        existing_cols = [
                            col[1] for col in cursor.execute(f"PRAGMA table_info({table})")
                        ]
                        i = 1
                        while f"libre{i}" in existing_cols:
                            i += 1
                        new_col = f"libre{i}"
                        write_log(f"🆕 {nom_base} : nom auto-généré → {new_col}")
                else:
                    new_col = new_col_saisi

                # ----------------------------------------------------------
                # 2) Mettre à jour field_groups si la colonne y est référencée
                # ----------------------------------------------------------
                with sqlite3.connect(chemin) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    fg = cursor.execute("""
                        SELECT * FROM field_groups
                        WHERE field_name = ? AND appli = ?
                        LIMIT 1
                    """, (old_col, table)).fetchone()

                    if fg:
                        # Réinitialise display_order si on repasse en "libreX"
                        display_order = None if new_col.startswith("libre") else fg["display_order"]

                        cursor.execute("""
                            UPDATE field_groups
                            SET field_name = ?, display_order = ?
                            WHERE field_name = ? AND appli = ?
                        """, (new_col, display_order, old_col, table))
                        conn.commit()
                        write_log(
                            f"✏️ {nom_base} field_groups mis à jour : "
                            f"{old_col} → {new_col}, display_order = {display_order}"
                        )
                    else:
                        write_log(f"ℹ️ {nom_base} : {old_col} absent de field_groups, pas de mise à jour")

                # ----------------------------------------------------------
                # 3) Renommage effectif de la colonne dans la base SQLite
                # ----------------------------------------------------------
                result = rename_column(chemin, table, old_col, new_col)
                resultats.append(f"{nom_base} → {result}")

            except Exception as e:
                write_log(f"❌ Exception globale sur {nom_base} : {e}")
                resultats.append(f"{nom_base} → ❌ Erreur : {e}")

    return render_template("rename_field.html", tables=TABLES_AUTORISEES, resultats=resultats)
