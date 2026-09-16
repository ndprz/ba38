#!/usr/bin/env python3

import os
import shutil
import pypandoc

BASE_DOC = "/srv/ba38/documentation_utilisateur"

DOCS_DIR = os.path.join(BASE_DOC, "docs")
HTML_DIR = os.path.join(BASE_DOC, "html")

BASE_APP = os.getenv("BA38_BASE_DIR")  # dev ou prod

TEMPLATE_TARGET = os.path.join(BASE_APP, "templates", "docsHtml")
MEDIA_STATIC_DIR = os.path.join(BASE_APP, "static", "docsHtml_media")
MEDIA_URL_PREFIX = "/static/docsHtml_media"


def convert_all_docs():

    os.makedirs(HTML_DIR, exist_ok=True)
    os.makedirs(TEMPLATE_TARGET, exist_ok=True)
    os.makedirs(MEDIA_STATIC_DIR, exist_ok=True)

    converted = 0

    for filename in os.listdir(DOCS_DIR):

        if not filename.lower().endswith(".docx"):
            continue

        docx_path = os.path.join(DOCS_DIR, filename)
        nom_base = filename[:-len(".docx")]
        html_filename = filename.replace(".docx", ".html")
        html_path = os.path.join(HTML_DIR, html_filename)
        # Chaque document extrait ses images dans son propre sous-dossier
        # (les identifiants rIdXX se répètent d'un docx à l'autre) — servi
        # directement en tant que fichier statique, contrairement à
        # templates/docsHtml qui n'est jamais exposé en HTTP.
        dossier_media_doc = os.path.join(MEDIA_STATIC_DIR, nom_base)

        try:
            print(f"🔄 Conversion : {filename}")

            pypandoc.convert_file(
                source_file=docx_path,
                to="html",
                outputfile=html_path,
                extra_args=[f"--extract-media={dossier_media_doc}"],
            )

            # Les images intégrées au docx sont extraites par pandoc dans
            # <dossier_media_doc>/media/... et référencées dans le HTML par
            # ce même chemin ABSOLU du système de fichiers (pas un chemin
            # relatif "media/..."). Réécrit ce chemin en URL statique pour
            # qu'il fonctionne aussi bien inclus tel quel dans une autre
            # page (modales d'aide) qu'ouvert seul.
            with open(html_path, "r", encoding="utf-8") as f:
                contenu = f.read()
            chemin_media_reel = os.path.join(dossier_media_doc, "media") + os.sep
            url_media = f"{MEDIA_URL_PREFIX}/{nom_base}/media/"
            contenu = contenu.replace(f'src="{chemin_media_reel}', f'src="{url_media}')
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(contenu)

            # 🔥 copie vers templates (clé)
            target_path = os.path.join(TEMPLATE_TARGET, html_filename)
            shutil.copy(html_path, target_path)

            print(f"✅ Copié vers {target_path}")
            converted += 1

        except Exception as e:
            print(f"❌ ERREUR {filename} : {e}")

    print(f"\n🎯 {converted} fichiers convertis")


if __name__ == "__main__":
    convert_all_docs()