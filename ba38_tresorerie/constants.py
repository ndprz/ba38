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

# ============================
# PARAMÈTRES FIXES BAI
# ============================

BAI_NOM = "BANQUE ALIMENTAIRE DE L'ISÈRE"
BAI_ADRESSE = "11, allée de la Pinéa\n38600 FONTAINE"
BAI_TEL = "04 76 85 92 50"
BAI_MAIL = "ba380@banquealimentaire.org"
BAI_IBAN = "FR76 1027 8089 2200 0598 3594 087"
BAI_BIC = "CMCIFR2A"
BAI_SIREN = "388 092 132 00033"
BAI_NAF = "8899B"
