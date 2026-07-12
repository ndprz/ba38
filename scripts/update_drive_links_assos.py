#!/usr/bin/env python3
"""
Script interactif : mise à jour du champ drive_link des associations
depuis le fichier "Liste sous-dossiers PARTENARIAT.xlsx".

Usage :
    python3 update_drive_links_assos.py [--env dev|prod] [--dry-run]

Options :
    --env     Environnement cible (défaut : dev)
    --dry-run Simulation sans écriture en base
"""

import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime
from difflib import SequenceMatcher

import openpyxl

EXCEL_PATH = "/srv/ba38/uploads/Liste sous-dossiers PARTENARIAT.xlsx"
DB_PATHS = {
    "dev":  "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
}
LOG_DIR = "/srv/ba38/logs"

SEUIL_BON = 0.82


def normalize(s):
    return s.strip().lower()


def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def find_best_match(nom_dossier, associations):
    best, best_score = None, 0.0
    for asso in associations:
        score = similarity(nom_dossier, asso["nom_association"])
        if score > best_score:
            best_score = score
            best = asso
    return best, best_score


def choisir_manuellement(nom_dossier, lien, associations, writer, dry_run, conn, stats):
    print("        Entrez quelques lettres pour chercher une association :")
    while True:
        recherche = input("        Recherche (vide = ignorer) : ").strip()
        if not recherche:
            print("        ⏭️  Ignoré.")
            stats["ignores"] += 1
            writer.writerow([nom_dossier, "", "", "", lien, "IGNORE", datetime.now().isoformat()])
            return

        resultats = sorted(
            [a for a in associations if recherche.lower() in a["nom_association"].lower()],
            key=lambda a: a["nom_association"],
        )
        if not resultats:
            print("        Aucun résultat, réessayez.")
            continue

        for j, a in enumerate(resultats[:10], 1):
            dl = "✓ lien déjà présent" if a["drive_link"] else ""
            print(f"          {j}. {a['nom_association']} (ID {a['Id']}) {dl}")
        if len(resultats) > 10:
            print(f"          … et {len(resultats) - 10} autre(s)")

        choix = input("        Numéro (0 ou vide = réessayer, s = ignorer) : ").strip().lower()
        if not choix or choix == "0":
            continue
        if choix == "s":
            print("        ⏭️  Ignoré.")
            stats["ignores"] += 1
            writer.writerow([nom_dossier, "", "", "", lien, "IGNORE", datetime.now().isoformat()])
            return
        try:
            idx = int(choix) - 1
            if 0 <= idx < min(10, len(resultats)):
                asso = resultats[idx]
                appliquer_mise_a_jour(nom_dossier, lien, asso, "manuel", dry_run, conn, stats, writer)
                return
            print("        Numéro hors plage.")
        except ValueError:
            print("        Entrée invalide.")


def appliquer_mise_a_jour(nom_dossier, lien, asso, score_str, dry_run, conn, stats, writer):
    if not dry_run:
        conn.execute(
            "UPDATE associations SET drive_link = ? WHERE Id = ?",
            (lien, asso["Id"]),
        )
        conn.commit()
        asso["drive_link"] = lien
    suffixe = " [simulation]" if dry_run else ""
    print(f"        ✅ Mis à jour : « {asso['nom_association']} »{suffixe}")
    stats["mis_a_jour"] += 1
    writer.writerow([nom_dossier, asso["nom_association"], asso["Id"], score_str, lien, "MIS_A_JOUR", datetime.now().isoformat()])


def main():
    parser = argparse.ArgumentParser(description="Mise à jour drive_link associations depuis Excel")
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = DB_PATHS[args.env]

    if args.env == "prod":
        print(f"\n⚠️  ATTENTION : vous allez modifier la base de PRODUCTION")
        print(f"   Chemin : {db_path}")
        rep = input("   Confirmer ? (oui/non) : ").strip().lower()
        if rep not in ("oui", "o"):
            print("Annulé.")
            sys.exit(0)

    if args.dry_run:
        print("\n[DRY-RUN] Aucune modification ne sera écrite en base.\n")

    # ── Lecture Excel ──────────────────────────────────────────────────────────
    wb = openpyxl.load_workbook(EXCEL_PATH)
    ws = wb.active
    entries = [
        (str(r[0]).strip(), str(r[1]).strip())
        for r in ws.iter_rows(min_row=2, values_only=True)
        if r[0] and str(r[0]).strip() and not str(r[0]).strip().startswith(".")
    ]
    print(f"\n📄 Fichier Excel : {len(entries)} dossiers à traiter")

    # ── Lecture base ───────────────────────────────────────────────────────────
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    associations = [
        dict(a)
        for a in conn.execute(
            "SELECT Id, nom_association, drive_link FROM associations ORDER BY nom_association COLLATE NOCASE"
        ).fetchall()
    ]
    print(f"🗃️  Base [{args.env}] : {len(associations)} associations")
    print(f"\n{'─' * 70}\n")

    # ── Journal ────────────────────────────────────────────────────────────────
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"update_drive_links_{args.env}_{ts}.csv")
    stats = {"mis_a_jour": 0, "ignores": 0, "deja_ok": 0}

    with open(log_path, "w", newline="", encoding="utf-8") as logfile:
        writer = csv.writer(logfile)
        writer.writerow(["Nom dossier Excel", "Association BD", "ID", "Score", "Lien", "Action", "Horodatage"])

        for i, (nom_dossier, lien) in enumerate(entries, 1):
            print(f"[{i}/{len(entries)}] « {nom_dossier} »")
            print(f"        Lien : {lien}")

            best, score = find_best_match(nom_dossier, associations)

            if score >= SEUIL_BON:
                emoji = "✅" if score >= 0.95 else "🟡"
                print(f"        {emoji} Match ({score:.0%}) → « {best['nom_association']} » (ID {best['Id']})")

                if best["drive_link"]:
                    if best["drive_link"] == lien:
                        print("        ℹ️  Lien identique déjà en base. Ignoré automatiquement.")
                        stats["deja_ok"] += 1
                        writer.writerow([nom_dossier, best["nom_association"], best["Id"], f"{score:.2f}", lien, "DEJA_OK", datetime.now().isoformat()])
                        print()
                        continue
                    else:
                        print(f"        ⚠️  Lien actuel  : {best['drive_link']}")

                rep = input("        [o=oui / n=ignorer / c=choisir manuellement / q=quitter] : ").strip().lower()
            else:
                if score > 0.5:
                    print(f"        🔴 Match faible ({score:.0%}) → « {best['nom_association'] if best else '—'} »")
                else:
                    print("        ❌ Aucun match satisfaisant")
                rep = input("        [c=choisir manuellement / n=ignorer / q=quitter] : ").strip().lower()
                if rep == "o":
                    rep = "c"

            print()

            if rep == "q":
                print("Interruption par l'utilisateur.")
                break
            elif rep in ("o", "oui"):
                appliquer_mise_a_jour(nom_dossier, lien, best, f"{score:.2f}", args.dry_run, conn, stats, writer)
            elif rep == "c":
                choisir_manuellement(nom_dossier, lien, associations, writer, args.dry_run, conn, stats)
            else:
                print("        ⏭️  Ignoré.")
                stats["ignores"] += 1
                nom_bd = best["nom_association"] if best else ""
                id_bd = best["Id"] if best else ""
                writer.writerow([nom_dossier, nom_bd, id_bd, f"{score:.2f}", lien, "IGNORE", datetime.now().isoformat()])

            print()

    conn.close()

    print(f"\n{'═' * 70}")
    print(f"  RÉSUMÉ")
    print(f"{'─' * 70}")
    print(f"  Mis à jour    : {stats['mis_a_jour']}")
    print(f"  Déjà à jour   : {stats['deja_ok']}")
    print(f"  Ignorés/passés: {stats['ignores']}")
    print(f"{'─' * 70}")
    print(f"  Journal CSV   : {log_path}")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
