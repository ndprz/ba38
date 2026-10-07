# =========================================
# 🏛️ Chorus Pro — dépôt de factures via l'API (PISTE)
# =========================================
# Client technique uniquement (pas de route) : utilisé par le module
# Participation (ba38_tresorerie/participation_chorus.py) pour déposer les
# factures des partenaires publics (associations.chorus_pro = 'oui').
#
# Voir mémoire "Chorus Pro" pour le raccordement (PISTE, comptes
# techniques, matelas de données de qualification).

from ba38_chorus.client import ClientChorus, ChorusErreur, environnement_chorus  # noqa: F401
