# ============================================================
# ba38_utilitaires/taches_fond.py
# Lancement des tâches en arrière-plan (envois emails/SMS, analyses)
# ============================================================
#
# Chaque tâche lancée via lancer_tache_fond() dépose un fichier témoin
# dans <BA38_BASE_DIR>/run/taches/ pendant toute sa durée d'exécution,
# supprimé à la fin (même en cas d'erreur).
#
# scripts/deploy_to_prod.sh lit ces témoins avant de toucher PROD : un
# redémarrage de gunicorn tuerait la tâche en cours (envoi partiel).
# Un témoin dont le PID n'existe plus (worker planté/tué) est ignoré par
# le script, donc aucun blocage possible par un témoin orphelin.
# ============================================================

import json
import os
import uuid
from datetime import datetime
from threading import Thread


def _dossier_taches():
    base_dir = os.getenv("BA38_BASE_DIR", ".")
    dossier = os.path.join(base_dir, "run", "taches")
    os.makedirs(dossier, exist_ok=True)
    return dossier


def lancer_tache_fond(target, args=(), nom=None, utilisateur=None, daemon=False):
    """
    Remplace Thread(target=..., args=...).start() en ajoutant le fichier
    témoin lu par le script de déploiement.

    nom         : libellé lisible affiché par le script de déploiement
    utilisateur : email de la personne qui a lancé la tâche
    """
    nom = nom or target.__name__
    chemin = None
    try:
        chemin = os.path.join(_dossier_taches(), f"{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "nom": nom,
                "utilisateur": utilisateur,
                "debut": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, f, ensure_ascii=False)
    except OSError:
        # Le témoin ne doit jamais empêcher l'envoi lui-même
        chemin = None

    def _executer():
        try:
            target(*args)
        finally:
            if chemin:
                try:
                    os.remove(chemin)
                except OSError:
                    pass

    thread = Thread(target=_executer, name=nom, daemon=daemon)
    try:
        thread.start()
    except Exception:
        if chemin:
            try:
                os.remove(chemin)
            except OSError:
                pass
        raise
    return thread
