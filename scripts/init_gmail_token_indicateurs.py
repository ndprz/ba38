#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génère le jeton OAuth Gmail nécessaire au bouton "Renvoyer via Gmail" de
l'écran indicateurs (envoi direct depuis ba380@banquealimentaire.org, en
alternative à Mailjet — voir mémoire "Envoi indicateurs traçabilité" pour le
contexte des rebonds Microsoft/mail.ru).

⚠️ À LANCER PAR UNE PERSONNE CONNECTÉE AU COMPTE
   ba380@banquealimentaire.org (le flow ouvre un écran de consentement
   Google dans le navigateur — choisir CE compte, pas un autre).

Réutilise le même client OAuth que l'import stocks Gmail
(credentials_gmail_stocks.json) mais avec le scope d'envoi (gmail.send) et
produit un jeton séparé (token_gmail_indicateurs.json), pour ne jamais
mélanger les droits d'envoi avec la lecture seule utilisée par l'import
stocks.

Ce script ouvre un serveur local le temps du consentement : à lancer sur une
machine avec navigateur (ou via redirection de port SSH vers le serveur :
`ssh -L 8765:localhost:8765 <serveur>` puis lancer le script sur le serveur
et ouvrir l'URL affichée depuis le navigateur local).
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

CREDENTIALS_DIR = "/srv/ba38/credentials"
CREDS_FILE = os.path.join(CREDENTIALS_DIR, "credentials_gmail_stocks.json")
TOKEN_FILE = os.path.join(CREDENTIALS_DIR, "token_gmail_indicateurs.json")

if not os.path.exists(CREDS_FILE):
    raise FileNotFoundError(f"Fichier introuvable : {CREDS_FILE}")

print("➡ Ouverture du flow OAuth Google...")
print("⚠️ Connectez-vous bien avec le compte ba380@banquealimentaire.org\n")

flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
creds = flow.run_local_server(port=8765, open_browser=False)

with open(TOKEN_FILE, "w") as f:
    f.write(creds.to_json())

print("\n✅ Jeton généré avec succès")
print(f"📁 Emplacement : {TOKEN_FILE}")
