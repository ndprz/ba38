🛠️ Module ba38_traitements
1. Rôle du module

Le module ba38_traitements.py regroupe les utilitaires administratifs de l’application BA380.
Il est accessible uniquement aux administrateurs depuis le menu 🛠️ Utilitaires.

Il gère notamment le traitement automatisé des fichiers de participation (parsol2l.txt) déposés dans Google Drive.

2. Dépendances

Flask : Blueprint, routes, rendu de templates.

Flask-Login : protection des routes.

utils.py :

get_google_services() pour accéder aux API Google,

write_log() pour tracer les opérations.

Google API (googleapiclient) : accès aux fichiers Drive.

pandas : aperçu de fichiers Excel / CSV.

datetime, re : gestion des dates et expressions régulières.

3. Variables d’environnement

Les variables suivantes doivent être définies dans .env :

# 📁 Dossier source des fichiers participation
DOSSIER_PARTICIPATION=1ne0IX5RCEdIZlkQurTfzWiAEl1oWuhHP

# 🔑 Compte de service Google
SERVICE_ACCOUNT_FILE=/home/ndprz/dev/service_account.json

4. Routes disponibles
/utilitaires

Page listant les outils disponibles.

Réservée aux administrateurs.

Exemple : bouton Traitement fichier de participation.

/traitement_participation

Fonction principale du module.

Étapes :

Liste les fichiers .txt disponibles dans DOSSIER_PARTICIPATION.

L’utilisateur sélectionne le fichier à traiter.

Analyse du contenu :

Suppression des lignes dont la date tombe vendredi, samedi ou dimanche.

Calcul des totaux corrigés et totaux supprimés.

Détermination du trimestre et de l’année (à partir de la 1ère date rencontrée).

Création d’un sous-dossier dans DOSSIER_PARTICIPATION :

TrimN_YYYY


Si le dossier existe déjà, son contenu est supprimé avant de recréer les fichiers.

Export de 3 fichiers dans ce sous-dossier :

xxx_corrigé_TrimN_YYYY.txt

xxx_lignes_supprimees_TrimN_YYYY.txt

xxx_analyse_TrimN_YYYY.txt

5. Fichiers générés

Fichier corrigé : contient toutes les lignes valides et le total général corrigé.

Fichier lignes supprimées : détail des lignes retirées, total par association et total général supprimé.

Fichier analyse : résumé global (montants corrigés et supprimés).

Exemple de structure dans Google Drive après traitement :

DOSSIER_PARTICIPATION/
└── Trim2_2025/
    ├── parsol2l_corrigé_Trim2_2025.txt
    ├── parsol2l_lignes_supprimees_Trim2_2025.txt
    └── parsol2l_analyse_Trim2_2025.txt

6. Points d’attention

Encodage :

Lecture prioritaire en UTF-8, fallback en CP1252.

Export systématique en UTF-8.

Drive partagé :

Toutes les requêtes utilisent supportsAllDrives=True.

Gestion des doublons :

Un seul dossier TrimN_YYYY est conservé.

Son contenu est purgé à chaque nouveau traitement.

7. Exemple d’utilisation

L’administrateur ouvre 🛠️ Utilitaires → Traitement fichier de participation.

Il choisit parsol2l.txt dans la liste des fichiers.

Après traitement, les fichiers résultats sont disponibles dans le Drive, dossier TrimN_YYYY.