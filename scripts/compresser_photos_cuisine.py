#!/usr/bin/env python3
# ============================================================
# 📷 Recompression des photos Cuisine déjà enregistrées
# ============================================================
# Réduit à 1600 px / JPEG q80 (même réglage que reduire_photo() dans
# ba38_cuisine/utils.py) toutes les photos de
# <base-dir>/uploads/production_cuisine/ (réceptions + traçabilité).
# Chemins et formats inchangés → aucune modification en base.
#
# Les originaux sont d'abord archivés (tar) dans /srv/ba38/backups/ :
# la compression est irréversible et ces photos servent de preuves de
# traçabilité. Aucune photo n'est touchée si l'archive échoue.
#
# Autonome volontairement (pas d'import de l'appli) : --base-dir est
# explicite, pour ne pas dépendre du .env chargé (piège load_dotenv qui
# charge toujours dev/.env depuis dev/scripts/).
#
# Usage :
#   python3 scripts/compresser_photos_cuisine.py --base-dir /srv/ba38/dev [--dry-run]
#   python3 scripts/compresser_photos_cuisine.py --base-dir /srv/ba38/prod
# ============================================================

import argparse
import os
import sys
import tarfile
from datetime import datetime

from PIL import Image, ImageOps

PHOTO_MAX_PX = 1600
PHOTO_QUALITE_JPEG = 80
BACKUP_DIR = "/srv/ba38/backups"


def reduire(abs_path):
    with Image.open(abs_path) as im:
        fmt = im.format
        if fmt not in ("JPEG", "MPO", "PNG", "WEBP") or max(im.size) <= PHOTO_MAX_PX:
            return False
        exif = im.getexif()
        im = ImageOps.exif_transpose(im)
        im.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX), Image.LANCZOS)
        exif[0x0112] = 1
        tmp_path = abs_path + ".tmp"
        if fmt in ("JPEG", "MPO"):
            im.convert("RGB").save(tmp_path, "JPEG", quality=PHOTO_QUALITE_JPEG,
                                   optimize=True, exif=exif.tobytes())
        else:
            im.save(tmp_path, fmt, optimize=True)
    os.replace(tmp_path, abs_path)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-dir", required=True, help="/srv/ba38/dev ou /srv/ba38/prod")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    racine = os.path.join(args.base_dir, "uploads", "production_cuisine")
    if not os.path.isdir(racine):
        sys.exit(f"❌ Dossier introuvable : {racine}")

    fichiers = []
    for root, _dirs, files in os.walk(racine):
        for f in files:
            if f.endswith(".tmp"):
                continue
            p = os.path.join(root, f)
            try:
                with Image.open(p) as im:
                    if im.format in ("JPEG", "MPO", "PNG", "WEBP") and max(im.size) > PHOTO_MAX_PX:
                        fichiers.append(p)
            except Exception:
                pass  # pas une image lisible → ignorée

    taille_avant = sum(os.path.getsize(p) for p in fichiers)
    print(f"📂 {racine}")
    print(f"📷 {len(fichiers)} photo(s) à réduire, {taille_avant / 1048576:.1f} Mo")
    if args.dry_run or not fichiers:
        return

    env = os.path.basename(os.path.normpath(args.base_dir))
    archive = os.path.join(
        BACKUP_DIR, f"photos_cuisine_originales_{env}_{datetime.now():%Y%m%d-%H%M%S}.tar"
    )
    with tarfile.open(archive, "w") as tar:
        for p in fichiers:
            tar.add(p, arcname=os.path.relpath(p, args.base_dir))
    with tarfile.open(archive) as tar:
        if len(tar.getmembers()) != len(fichiers):
            sys.exit(f"❌ Archive incomplète ({archive}), aucune photo modifiée")
    print(f"💾 Originaux archivés : {archive}")

    ok = err = 0
    for p in fichiers:
        try:
            if reduire(p):
                ok += 1
        except Exception as e:
            err += 1
            print(f"⚠️ {p} : {e}")
            if os.path.exists(p + ".tmp"):
                os.remove(p + ".tmp")
    taille_apres = sum(os.path.getsize(p) for p in fichiers)
    print(f"✅ {ok} réduite(s), {err} erreur(s) : "
          f"{taille_avant / 1048576:.1f} Mo → {taille_apres / 1048576:.1f} Mo")


if __name__ == "__main__":
    main()
