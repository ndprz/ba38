# ============================================================
# 📦 ba38_vif — fichiers d'import VIF (FFBA)
#
#   generation.py : texte CSV des imports VIF, SANS Flask ni session
#                   (réceptions et BL des ramasses partenaires pour l'instant).
#                   Une seule fonction par flux : le téléchargement manuel
#                   et le dépôt SFTP appellent la même, le format ne peut
#                   donc pas diverger.
#   depot.py      : dépôt d'un fichier dans le dossier lu par VIF (SFTP),
#                   écriture atomique, nom horodaté unique.
#
# Étude et décisions : mémoire ba38_vif_import_automatisation_etude.
# ============================================================
