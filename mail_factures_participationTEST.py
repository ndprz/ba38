import fitz  # PyMuPDF
import os
import re
import tkinter as tk
from tkinter import filedialog
import hashlib

# --- Fonction pour choisir le fichier PDF via dialogue graphique ---
def choisir_fichier_pdf():
    root = tk.Tk()
    root.withdraw()  # Cacher la fenêtre principale
    chemin_fichier = filedialog.askopenfilename(
        title="Choisir un fichier PDF",
        filetypes=[("Fichiers PDF", "*.pdf")]
    )
    return chemin_fichier

# --- Nettoyer le nom d'association pour en faire un nom de fichier valide ---
def nettoyer_nom_fichier(nom):
    nom = nom.strip()
    nom = re.sub(r'[\\/*?:"<>|]', "", nom)  # Supprime caractères invalides Windows
    nom = nom.replace(" ", "_")
    return nom

# --- Extraire le nom de l'association à partir du texte de la page ---
def extraire_nom_assoc(page_text):
    lignes = page_text.splitlines()
    if len(lignes) > 13:
        nom = lignes[13].strip()
        print(f"➡️ Association détectée (ligne 14) : {nom}")
        return nom
    else:
        print("⚠️ Le texte ne contient pas assez de lignes pour extraire l'association.")
        return None

# --- Extraire email en respectant la règle ---
import re

def extraire_email_depuis_texte(texte):
    # Limiter aux 10 premières pages si séparation par \f
    pages = texte.split('\f')[:10]
    print(f"Nombre de pages traitées : {len(pages)}")

    email_pattern = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+')

    for i, page in enumerate(pages, start=1):
        print(f"--- Page {i} ---")
        for line_no, line in enumerate(page.splitlines(), start=1):
            line_strip = line.strip()
            print(f"Ligne {line_no} : '{line_strip}'")

            # Trouver toutes les adresses email dans la ligne (souvent 1 max)
            emails_trouves = email_pattern.findall(line_strip)
            for email in emails_trouves:
                email_lower = email.lower()
                print(f"  -> Email trouvé : '{email_lower}'")
                if email_lower != "ba380.comptable@banquealimentaire.org":
                    print(f"Email retourné : {email_lower}")
                    return email_lower
    print("Aucun email valide trouvé")
    return None





# --- Enregistrer PDF et simuler envoi mail ---
def enregistrer_et_envoyer(nom_assoc, email, pdf_document, dossier_sortie, page_debut, page_fin):
    nom_assoc_nettoye = nettoyer_nom_fichier(nom_assoc)
    # Hash sur nom + pages pour éviter doublons
    hash_input = f"{nom_assoc}_{page_debut}_{page_fin}".encode('utf-8')
    hsh = hashlib.md5(hash_input).hexdigest()[:8]
    nom_fichier = f"{nom_assoc_nettoye}_{hsh}.pdf"
    chemin_sortie = os.path.join(dossier_sortie, nom_fichier)
    pdf_document.save(chemin_sortie)
    print(f"Enregistrement PDF de l'association '{nom_assoc}' pages {page_debut + 1} à {page_fin + 1} sous : {chemin_sortie}")
    print(f"📤 Envoi simulé à {email} — fichier : {chemin_sortie}")

# --- Fonction principale de découpage et d'envoi ---
def decouper_pdf_et_envoyer(pdf_source):
    print(f"Ouverture du fichier PDF : {pdf_source}")
    doc = fitz.open(pdf_source)

    dossier_sortie = os.path.join(os.path.dirname(pdf_source), "factures_individuelles")
    os.makedirs(dossier_sortie, exist_ok=True)

    nom_assoc_courant = None
    email_assoc_courant = None
    pdf_facture_courant = fitz.open()  # PDF vide pour accumuler les pages
    page_debut_facture = 0

    for i in range(len(doc)):
        page = doc.load_page(i)
        texte = page.get_text()

        # Extraire nom association de la page
        nom_assoc = extraire_nom_assoc(texte)
        if not nom_assoc:
            # Si on ne trouve pas le nom à la ligne 14, on réutilise nom précédent si existant
            if nom_assoc_courant is None:
                nom_assoc = "Association"
                print(f"⚠️ Nom d'association non trouvé à la page {i+1}, nom générique utilisé")
            else:
                nom_assoc = nom_assoc_courant
                print(f"ℹ️ Réutilisation du nom d'association précédent pour la page {i+1} : {nom_assoc}")
        else:
            print(f"➡️ Association détectée (page {i+1}): {nom_assoc}")

        # Extraire email de la page
        email_trouve = extraire_email_depuis_texte(texte)
        if email_trouve:
            email_assoc_courant = email_trouve
            print(f"➡️ Email détecté à la page {i+1} : {email_assoc_courant}")
        else:
            if email_assoc_courant:
                print(f"ℹ️ Pas d'email détecté à la page {i+1}, on garde l'email précédent : {email_assoc_courant}")
            else:
                print(f"⚠️ Email non trouvé à la page {i+1}, utilisation email générique")
                email_assoc_courant = "contact@generique.org"

        # Si changement d'association (nom différent du courant) ou dernière page, on sauvegarde le PDF courant
        if nom_assoc != nom_assoc_courant and nom_assoc_courant is not None:
            # Enregistrer et envoyer le PDF accumulé
            if pdf_facture_courant.page_count > 0:
                enregistrer_et_envoyer(nom_assoc_courant, email_assoc_courant, pdf_facture_courant, dossier_sortie, page_debut_facture, i - 1)
            # Réinitialiser pour nouvelle association
            pdf_facture_courant = fitz.open()
            page_debut_facture = i

        # Ajouter la page actuelle au PDF en cours
        pdf_facture_courant.insert_pdf(doc, from_page=i, to_page=i)

        nom_assoc_courant = nom_assoc

    # Enregistrer le dernier PDF à la fin
    if pdf_facture_courant.page_count > 0:
        enregistrer_et_envoyer(nom_assoc_courant, email_assoc_courant, pdf_facture_courant, dossier_sortie, page_debut_facture, len(doc) - 1)

    print("✅ Traitement terminé.")

# --- Fonction main ---
def main():
    chemin_pdf = choisir_fichier_pdf()
    if not chemin_pdf:
        print("Aucun fichier PDF sélectionné. Fin du programme.")
        return
    decouper_pdf_et_envoyer(chemin_pdf)

if __name__ == "__main__":
    main()
