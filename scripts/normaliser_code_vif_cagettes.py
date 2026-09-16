#!/usr/bin/env python3
"""Normalise le format des Code VIF magasins dans les tables cagettes
(collecte_cagettes_magasins, collecte_cagettes) sur 8 chiffres avec zéro
de tête conservé (ex. '2380249' -> '02380249') — ces tables ont été
alimentées avant que _lire_referentiel_magasins_bai() n'applique
_vif_fmt(), et gardaient donc le code sans son zéro de tête. Idempotent :
un code déjà à 8 chiffres n'est pas modifié."""
import sqlite3

from ba38_utilitaires.core import get_db_path


def _vif_fmt(v):
    s = str(v).strip().split(".")[0]
    return s.zfill(8) if s.isdigit() else s


def main():
    with sqlite3.connect(get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        total_corriges = 0
        for table in ("collecte_cagettes_magasins", "collecte_cagettes"):
            rows = conn.execute(f"SELECT DISTINCT code_vif FROM {table}").fetchall()
            for row in rows:
                ancien = row["code_vif"]
                nouveau = _vif_fmt(ancien)
                if nouveau != ancien:
                    conn.execute(
                        f"UPDATE {table} SET code_vif = ? WHERE code_vif = ?",
                        (nouveau, ancien),
                    )
                    total_corriges += 1
                    print(f"✅ {table} : {ancien!r} -> {nouveau!r}")
        conn.commit()
    print(f"🎯 {total_corriges} valeur(s) de code_vif corrigée(s)")


if __name__ == "__main__":
    main()
