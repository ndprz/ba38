"""
Réglages SQLite communs (concurrence en écriture).

install() remplace sqlite3.connect par une version qui impose un délai
d'attente de 30 s (au lieu de 5 s) quand l'appelant n'en précise pas.
Couvre d'un coup toutes les connexions directes du code.

snapshot_sqlite() produit une copie cohérente via l'API backup de SQLite :
indispensable en mode WAL (copier le seul fichier .sqlite serait incomplet).
"""
import os
import sqlite3
import tempfile

DEFAULT_TIMEOUT = 30
_installed = False
_orig_connect = sqlite3.connect


def _connect(database, timeout=DEFAULT_TIMEOUT, *args, **kwargs):
    return _orig_connect(database, timeout, *args, **kwargs)


def install():
    global _installed
    if not _installed:
        sqlite3.connect = _connect
        _installed = True


def snapshot_sqlite(src_path, dest_dir=None):
    """Copie cohérente de la base (compatible WAL). Retourne le chemin du fichier temporaire."""
    fd, dest = tempfile.mkstemp(suffix=".sqlite", dir=dest_dir)
    os.close(fd)
    src = _orig_connect(f"file:{src_path}?mode=ro", uri=True, timeout=DEFAULT_TIMEOUT)
    dst = _orig_connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest
