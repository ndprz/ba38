"""Constantes du module CERFA (abandon de frais bénévoles)."""

MOIS = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]

MOIS_FR_MINUSCULE = [m.lower() for m in MOIS]

STATUTS = {
    "a_inviter": "À inviter",
    "invite": "Invité",
    "en_cours": "En cours de saisie",
    "soumis": "Déclaration transmise",
    "valide": "Validé (document à envoyer)",
    "termine": "CERFA envoyé — terminé",
    "courrier_envoye": "Courrier envoyé — à rembourser",
    "rembourse": "Remboursé — terminé",
    "decline": "Ne participe pas",
}

# Statuts d'une déclaration validée (numéro attribué, montant figé)
STATUTS_VALIDES = ("valide", "termine", "courrier_envoye", "rembourse")

TYPES_VEHICULE = {
    "voiture": "Voiture",
    "moto": "Moto (plus de 50 cm³)",
    "cyclomoteur": "Cyclomoteur (50 cm³ et moins)",
}

TYPES_PIECE = {
    "carte_grise": "Carte grise",
    "avis_non_imposition": "Avis de non-imposition",
    "autre": "Autre document",
}

EXTENSIONS_PIECES = {"pdf", "jpg", "jpeg", "png", "heic", "heif", "webp"}
TAILLE_MAX_PIECE = 10 * 1024 * 1024  # 10 Mo par fichier

MAX_ENVOIS_TEST = 2

# Barème kilométrique fiscal (BOI-BAREME-000001), formule par tranche de
# distance annuelle d : tranche 1 = d × a1 ; tranche 2 = d × a2 + b2 ;
# tranche 3 = d × a3. Valeurs des revenus 2023/2024 (inchangées depuis) :
# à vérifier et mettre à jour dans les paramètres de chaque campagne au
# moment d'établir les CERFA.
BAREME_DEFAUT = {
    "voiture": {
        "seuils": [5000, 20000],
        "lignes": [
            {"label": "3 CV et moins", "cv_max": 3, "a1": 0.529, "a2": 0.316, "b2": 1065, "a3": 0.370},
            {"label": "4 CV", "cv_max": 4, "a1": 0.606, "a2": 0.340, "b2": 1330, "a3": 0.407},
            {"label": "5 CV", "cv_max": 5, "a1": 0.636, "a2": 0.357, "b2": 1395, "a3": 0.427},
            {"label": "6 CV", "cv_max": 6, "a1": 0.665, "a2": 0.374, "b2": 1457, "a3": 0.447},
            {"label": "7 CV et plus", "cv_max": None, "a1": 0.697, "a2": 0.394, "b2": 1515, "a3": 0.470},
        ],
    },
    "moto": {
        "seuils": [3000, 6000],
        "lignes": [
            {"label": "1 ou 2 CV", "cv_max": 2, "a1": 0.395, "a2": 0.099, "b2": 891, "a3": 0.248},
            {"label": "3, 4 ou 5 CV", "cv_max": 5, "a1": 0.468, "a2": 0.082, "b2": 1158, "a3": 0.275},
            {"label": "Plus de 5 CV", "cv_max": None, "a1": 0.606, "a2": 0.079, "b2": 1583, "a3": 0.343},
        ],
    },
    "cyclomoteur": {
        "seuils": [3000, 6000],
        "lignes": [
            {"label": "Cyclomoteur", "cv_max": None, "a1": 0.315, "a2": 0.079, "b2": 711, "a3": 0.198},
        ],
    },
}

BAREME_SOURCE_DEFAUT = "Barème kilométrique fiscal (revenus 2024) — à vérifier sur impots.gouv.fr"

PRIX_TICKET_TAG_DEFAUT = 1.80

EXPEDITEUR_DEFAUT = None  # None = MAILJET_SENDER (expéditeur validé Mailjet)
REPLY_TO_DEFAUT = "ba380.fraistrajets@banquealimentaire.org"
SIGNATAIRE_QUALITE_DEFAUT = "La Présidente"

# Variables communes : <<civilite>> <<prenom>> <<nom>> <<annee_frais>>
# <<annee_declaration>> <<date_limite>> <<lien>> <<numero>> <<montant>>
MAIL_INVITATION_SUJET = "Vos frais de bénévole <<annee_frais>> : déclaration en ligne pour votre réduction d'impôt"
MAIL_INVITATION_CORPS = """Bonjour <<prenom>>,

Afin de pouvoir obtenir une éventuelle réduction d'impôt sur votre déclaration <<annee_declaration>> (revenus <<annee_frais>>), nous vous invitons à déclarer en ligne les frais de déplacement engagés en <<annee_frais>> pour votre activité de bénévole à la Banque Alimentaire de l'Isère (trajets domicile / La Pinéa, ramasse, cuisine, épicerie...), et dont vous renoncez au remboursement.

Votre questionnaire personnel (ne le transmettez pas, il vous est réservé) :
<<lien>>

- Si vous êtes imposable : munissez-vous d'une copie de votre carte grise. Nous vous adresserons ensuite le reçu fiscal (CERFA) à utiliser pour votre déclaration de revenus.
- Si vous n'êtes pas imposable : munissez-vous de votre avis de non-imposition. Vos trajets pourront vous être remboursés forfaitairement sur la base de 2 tickets TAG par journée.

Merci de compléter le questionnaire avant le <<date_limite>>.

À votre disposition pour toute question éventuelle.
Cordialement,

Banque Alimentaire de l'Isère
"""

MAIL_RELANCE_SUJET = "Rappel : vos frais de bénévole <<annee_frais>> à déclarer avant le <<date_limite>>"
MAIL_RELANCE_CORPS = """Bonjour <<prenom>>,

Sauf erreur de notre part, vous n'avez pas encore transmis votre déclaration de frais de bénévole <<annee_frais>>.

Pour en bénéficier (reçu fiscal si vous êtes imposable, remboursement en tickets TAG sinon), merci de compléter votre questionnaire avant le <<date_limite>> :
<<lien>>

Si vous ne souhaitez pas faire de déclaration, vous pouvez l'indiquer en bas du questionnaire : vous ne recevrez plus de rappel.

Cordialement,

Banque Alimentaire de l'Isère
"""

MAIL_CERFA_SUJET = "Votre reçu fiscal CERFA <<numero>> — frais de bénévole <<annee_frais>>"
MAIL_CERFA_CORPS = """Bonjour <<prenom>>,

Suite à votre déclaration d'abandon de frais kilométriques dans le cadre de votre activité régulière de bénévole au sein de la Banque Alimentaire de l'Isère pour l'année <<annee_frais>>, vous trouverez en pièce jointe le reçu fiscal (CERFA n° <<numero>>, <<montant>> €) vous permettant de renseigner votre prochaine déclaration de revenus.

Restant à votre disposition pour toute information complémentaire, bien cordialement.

Banque Alimentaire de l'Isère
"""

MAIL_REMBOURSEMENT_SUJET = "Remboursement de vos trajets de bénévole <<annee_frais>>"
MAIL_REMBOURSEMENT_CORPS = """Bonjour <<prenom>>,

Suite à votre déclaration de frais de bénévole pour l'année <<annee_frais>>, vous trouverez en pièce jointe le courrier détaillant le remboursement forfaitaire de vos trajets (<<montant>> €).

Bien cordialement,

Banque Alimentaire de l'Isère
"""

# Variables supplémentaires : <<total_journees>> <<nb_tickets>> <<prix_ticket>>
COURRIER_REMBOURSEMENT_TEXTE = """<<civilite>>,

Vous nous avez indiqué ne pas avoir été imposable sur vos revenus <<annee_frais>> et avoir effectué <<total_journees>> journée(s) d'activité bénévole au profit de la Banque Alimentaire de l'Isère au cours de l'année <<annee_frais>>.

Ne pouvant bénéficier de la réduction d'impôt prévue à l'article 200 du Code général des impôts, vous serez remboursé(e) forfaitairement de vos trajets sur la base de <<nb_tickets>> tickets de transport TAG par journée, au prix unitaire de <<prix_ticket>> €, soit un montant total de <<montant>> €.

Nous vous remercions chaleureusement pour votre engagement à nos côtés.

Veuillez agréer, <<civilite>>, l'expression de nos salutations distinguées."""
