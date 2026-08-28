import os

BA380_SHARED_DRIVE_ID = os.getenv("BA380_SHARED_DRIVE_ID")

if not BA380_SHARED_DRIVE_ID:
    raise RuntimeError("BA380_SHARED_DRIVE_ID non défini dans l'environnement")

MAX_TEST_PREVIEW = 9999
MAX_TEST_SEND = 1
DATE_X = 360
DATE_Y = 100

# Ancien dossier générique (pour tests)
FOLDER_ID_TRAITEMENTS = "1_RiRtqyjwxcgCo9csqL8ckjePmaFwviy"

# Les paramètres d'identité de l'organisme (nom, adresse, IBAN, SIREN...)
# vivent désormais dans la table `organisation` — voir
# ba38_utilitaires.organisation.get_organisation() et
# scripts/migrate_organisation.py.
