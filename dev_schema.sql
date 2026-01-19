CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE IF NOT EXISTS "field_groups" (
	"id"	INTEGER,
	"field_name"	TEXT NOT NULL,
	"group_name"	TEXT NOT NULL,
	"display_order"	NUMERIC, is_required BOOLEAN DEFAULT 0, type_champ TEXT DEFAULT NULL, appli TEXT,
	PRIMARY KEY("id" AUTOINCREMENT)
);
CREATE TABLE IF NOT EXISTS "parametres" (
	"id"	INTEGER,
	"param_name"	TEXT NOT NULL,
	"param_value"	TEXT,
	"phone"	INTEGER,
	"mail"	INTEGER,
	PRIMARY KEY("id" AUTOINCREMENT)
);
CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        , role TEXT DEFAULT 'user', username TEXT DEFAULT '', actif TEXT DEFAULT 'Oui');
CREATE TABLE associations (
    Id INTEGER PRIMARY KEY AUTOINCREMENT,
    "date_de_la_visite" TEXT, "code_VIF" TEXT, "code_SIRET" TEXT, "nom_association" TEXT, "Code_comptable" TEXT, "siege" TEXT, "date_agrement_regional" TEXT, "date_FIN_habilitation" TEXT, "validite" TEXT, "Modification_par" TEXT, "raison_sociale_VIF" TEXT, "projet" TEXT, "adresse_association_1" TEXT, "adresse_association_2" TEXT, "CP" TEXT, "COMMUNE" TEXT, "tel_association" TEXT, "courriel_association" TEXT, "site_internet" TEXT, "adresse_distribution_1" TEXT, "adresse_distribution_2" TEXT, "CP2" TEXT, "COMMUNE2" TEXT, "tel_distribution" TEXT, "secteur_geographique" TEXT, "nom_president_ou_officiel" TEXT, "tel_president_officiel_1" TEXT, "tel_president_officiel_2" TEXT, "courriel_president" TEXT, "responsable_operationnel" TEXT, "tel_resp_operationnel_1" TEXT, "tel_resp_operationnel_2" TEXT, "courriel_resp_operationnel" TEXT, "responsable_IE" TEXT, "tel_resp_IE" TEXT, "courriel_resp_IE1" TEXT, "courriel_resp_IE2" TEXT, "responsable_distribution" TEXT, "tel_resp_distribution_1" TEXT, "tel_resp_distribution_2" TEXT, "courriel_distribution" TEXT, "responsable_HySA" TEXT, "teL_resp_Hysa_1" TEXT, "tel_resp_Hysa_2" TEXT, "courriel_resp_Hysa" TEXT, "responsable_tresorerie" TEXT, "tel_resp_tresorerie_1" TEXT, "tel_resp_tresorerie_2" TEXT, "courriel_resp_tresorerie" TEXT, "CAR" TEXT, "Visiteur" TEXT, "recu_e__par" TEXT, "fonction" TEXT, "date_precedente_visite" TEXT, "annexe_1_bis_mise_a_jour" TEXT, "logiciel_Ticadi_utilise" TEXT, "participation_a_la_collecte" TEXT, "garde_collecte" TEXT, "date_AG" TEXT, "accueil_magasin_satisfaisant" TEXT, "affiches_dans_le_local" TEXT, "periode_de_fermeture" TEXT, "nbre_moyen_de_beneficiaires_JOUR" TEXT, "nombre_de_foyers_JOUR" TEXT, "envoyes_par" TEXT, "criteres_d_eligibilite_de_l_aide_par_ecrit" TEXT, "quels_sont_ils" TEXT, "duree_de_l_accompagnement" TEXT, "renouvelable" TEXT, "montant_de_la_participation_financiere_par_personne" TEXT, "type_de_personnes_aidees" TEXT, "liste_de_beneficiaires" TEXT, "jour_de_passage_a_la_BAI" TEXT, "menu_sec" TEXT, "menu_frais" TEXT, "heure_de_passage" TEXT, "Emplacement" TEXT, "jour_distribution" TEXT, "heure" TEXT, "frequence" TEXT, "besoins_particuliers" TEXT, "solution_et_alarme" TEXT, "denrees_destines_a" TEXT, "autres_quels" TEXT, "origine_des_denrees_distribuees" TEXT, "lesquels" TEXT, "produits_distribues" TEXT, "sacs_isothermes" TEXT, "combien_de_benevoles" TEXT, "combien_de_benevoles_beneficiaires" TEXT, "combien_de_salaries" TEXT, "stage_HySA" TEXT, "colis" TEXT, "Remarques_sur_les_denrees_BAI" TEXT, "les_locaux_appartiennent_a" TEXT, "etat_local_de_distribution" TEXT, "etat_local_de_stockage" TEXT, "etat_chambre_froide" TEXT, "nbre_refrigerateur" TEXT, "etat_refrigerateur" TEXT, "nbre_congelateurs" TEXT, "etat_congelateur" TEXT, "vehicule_appartient_a" TEXT, "vehicule_adapte_transport_denrees_alimentaires" TEXT, "nettoyage_regulier_vehicule" TEXT, "chauffeur" TEXT, "tel_chauffeur" TEXT, "distance_aller_en_km" TEXT, "temps_moyen_de_transport_des_marchandises_en_MN" TEXT, "transport_des_produits_frais_dans" TEXT, "administratif" TEXT, "financier" TEXT, "formation" TEXT, "sante__prevention" TEXT, "cuisine_et_nutrition" TEXT, "divertissements" TEXT, "divers" TEXT, "autre_action" TEXT, "drive_link" TEXT
);
CREATE TABLE benevoles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commentaire TEXT, civilite TEXT, nom TEXT, prenom TEXT, rue TEXT, complement_adresse TEXT, code_postal TEXT, ville TEXT, telephone_fixe TEXT, telephone_portable TEXT, email TEXT, annee_arrivee_bai TEXT, cotisation_2025 TEXT, convention_signee TEXT, suivi_volontaires TEXT, prep_pesee TEXT, prep_palette TEXT, dist_legumes TEXT, dist_frais TEXT, chauf_accomp_comb TEXT, chauf_accomp_viande TEXT, chauf_accomp_grand_frais TEXT, chauf_accomp_meylan TEXT, chauf_accomp_st_egreve TEXT, chauf_ramasse_x_agro_alim_aa TEXT, ramasse_echi TEXT, ramasse_meylan TEXT, ramasse_st_egreve TEXT, partenariat_assoc TEXT, vif_matin TEXT, vif_apres_midi TEXT, comm TEXT, trois_etoiles TEXT, esope TEXT, autres TEXT, ca_2024 TEXT, collecte_preparation TEXT, cotisation_2020 TEXT, cotisation_2021 TEXT, cotisation_2022 TEXT, cotisation_2023 TEXT, cotisation_2024 TEXT
, chauffeur TEXT NOT NULL DEFAULT 'non' CHECK(chauffeur IN ('oui','non')), equipier TEXT NOT NULL DEFAULT 'non' CHECK(equipier IN ('oui','non')), lundi TEXT NOT NULL DEFAULT 'non' CHECK(lundi IN ('oui','non')), mardi TEXT NOT NULL DEFAULT 'non' CHECK(mardi IN ('oui','non')), mercredi TEXT NOT NULL DEFAULT 'non' CHECK(mercredi IN ('oui','non')), jeudi TEXT NOT NULL DEFAULT 'non' CHECK(jeudi IN ('oui','non')), vendredi TEXT NOT NULL DEFAULT 'non' CHECK(vendredi IN ('oui','non')));
CREATE TABLE photos_benevoles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    benevole_id INTEGER NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    FOREIGN KEY (benevole_id) REFERENCES benevoles(id)
);
CREATE TABLE IF NOT EXISTS "fournisseurs" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT,
    adresse TEXT,
    cp TEXT,
    ville TEXT,
    tel TEXT,
    mail TEXT,
    lundi TEXT CHECK (lundi IN ('oui', 'non')),
    horaire_lundi TEXT, -- Format attendu HH:MM
    mardi TEXT CHECK (mardi IN ('oui', 'non')),
    horaire_mardi TEXT,
    mercredi TEXT CHECK (mercredi IN ('oui', 'non')),
    horaire_mercredi TEXT,
    jeudi TEXT CHECK (jeudi IN ('oui', 'non')),
    horaire_jeudi TEXT
);
CREATE TABLE camions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT,
    immat TEXT
);
CREATE TABLE absences ( id INTEGER PRIMARY KEY AUTOINCREMENT, benevole_id INTEGER, date_debut TEXT, date_fin TEXT, FOREIGN KEY(benevole_id) REFERENCES benevoles(id) );
CREATE TABLE tournees_fournisseurs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournee_id INTEGER,
    fournisseur_id INTEGER, nom TEXT,
    FOREIGN KEY(tournee_id) REFERENCES tournees(id),
    FOREIGN KEY(fournisseur_id) REFERENCES fournisseurs(id)
);
CREATE TABLE planning_standard_ramasse_ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jour TEXT NOT NULL,               -- "lundi", "mardi", etc.
    numero INTEGER NOT NULL,          -- numéro de la tournée dans la journée (1 à 5)
    tournee_id INTEGER,
    chauffeur_id INTEGER,
    responsable_id INTEGER,
    equipier_id INTEGER,
    camion_id INTEGER,
    FOREIGN KEY(tournee_id) REFERENCES tournees(id),
    FOREIGN KEY(chauffeur_id) REFERENCES benevoles(id),
    FOREIGN KEY(responsable_id) REFERENCES benevoles(id),
    FOREIGN KEY(equipier_id) REFERENCES benevoles(id),
    FOREIGN KEY(camion_id) REFERENCES camions(id)
);
