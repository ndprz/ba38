#!/usr/bin/env python3

import os
import shutil
import pypandoc

BASE_DOC = "/srv/ba38/documentation_utilisateur"

DOCS_DIR = os.path.join(BASE_DOC, "docs")
HTML_DIR = os.path.join(BASE_DOC, "html")

BASE_APP = os.getenv("BA38_BASE_DIR")  # dev ou prod

TEMPLATE_TARGET = os.path.join(BASE_APP, "templates", "docsHtml")


def convert_all_docs():

    os.makedirs(HTML_DIR, exist_ok=True)
    os.makedirs(TEMPLATE_TARGET, exist_ok=True)

    converted = 0

    for filename in os.listdir(DOCS_DIR):

        if not filename.lower().endswith(".docx"):
            continue

        docx_path = os.path.join(DOCS_DIR, filename)
        html_filename = filename.replace(".docx", ".html")
        html_path = os.path.join(HTML_DIR, html_filename)

        try:
            print(f"🔄 Conversion : {filename}")

            pypandoc.convert_file(
                source_file=docx_path,
                to="html",
                outputfile=html_path
            )

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