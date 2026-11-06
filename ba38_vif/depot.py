# ============================================================
# Dépôt d'un fichier d'import dans le dossier lu par VIF (SFTP)
#
# VIF vient chercher les fichiers dont le nom correspond à un motif
# (ex. « blba38* »). Deux risques à éviter :
#   - qu'il lise un fichier à moitié écrit  -> écriture dans un fichier
#     temporaire « .tmp_… » (hors motif), puis publication d'un coup ;
#   - qu'un fichier en écrase un autre / soit déposé deux fois -> nom
#     horodaté unique, jamais d'écrasement (os.link échoue si le nom existe).
#
# Garde-fou DEV : une instance DEV ne dépose jamais sous la racine SFTP
# réelle (RACINE_SFTP), même si on lui donne ce dossier par erreur.
# ============================================================

import os
from datetime import datetime
from pathlib import Path

RACINE_SFTP = Path("/srv/sftp/vif")


class DepotVifError(Exception):
    pass


def nom_fichier(prefixe, maintenant=None, extension=".csv"):
    """« blba38_20261130_103000.csv » (heure locale du serveur)."""
    maintenant = maintenant or datetime.now()
    return f"{prefixe}_{maintenant:%Y%m%d_%H%M%S}{extension}"


def _verifier_destination(dossier):
    if not dossier.is_dir():
        raise DepotVifError(f"Dossier de dépôt introuvable : {dossier}")
    if os.getenv("ENVIRONMENT", "").upper() == "DEV":
        reel = dossier.resolve()
        racine = RACINE_SFTP.resolve()
        if reel == racine or racine in reel.parents:
            raise DepotVifError("Dépôt refusé : l'instance DEV ne dépose pas dans le SFTP réel.")


def deposer(contenu, dossier, prefixe, *, extension=".csv", encoding="utf-8", maintenant=None):
    """Dépose `contenu` (texte, fins de ligne déjà incluses) dans `dossier`.

    Retourne le Path du fichier publié. Ne crée jamais le dossier, n'écrase
    jamais un fichier existant, ne laisse pas de fichier temporaire.
    """
    if not contenu:
        raise DepotVifError("Contenu vide : rien à déposer.")
    dossier = Path(dossier)
    _verifier_destination(dossier)

    final = dossier / nom_fichier(prefixe, maintenant, extension)
    temporaire = dossier / f".tmp_{final.name}"
    octets = contenu.encode(encoding)  # octets exacts : aucune conversion de fin de ligne

    try:
        with open(temporaire, "xb") as f:
            f.write(octets)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporaire, 0o664)
        try:
            os.link(temporaire, final)  # publication atomique, échoue si le nom existe
        except FileExistsError:
            raise DepotVifError(f"Un fichier du même nom existe déjà : {final.name}")
    except FileExistsError:
        raise DepotVifError(f"Fichier temporaire déjà présent : {temporaire.name}")
    finally:
        try:
            os.unlink(temporaire)
        except FileNotFoundError:
            pass
    return final
