import requests

# ✅ Nouvelle URL correcte correspondant à la route existante
API_URL = "https://www.ba380.org/export_publipostage/all"
TOKEN = "AZERTY123456"  # 🔐 Ton token d'accès défini dans SECRET_EXPORT_TOKEN

try:
    response = requests.post(f"{API_URL}?token={TOKEN}")
    response.raise_for_status()  # Déclenche une exception si la réponse HTTP est une erreur

    # ✅ Vérification que le contenu est bien du JSON
    if "application/json" in response.headers.get("Content-Type", ""):
        data = response.json()
        print(f"✅ Export automatique exécuté avec succès :\n{data['message']}")
    else:
        print(f"⚠️ Réponse inattendue (non-JSON) :\n{response.text}")

except requests.exceptions.RequestException as e:
    print(f"❌ Erreur HTTP lors de l'exécution de l'export : {e}")

except ValueError as e:
    print(f"❌ Erreur JSON : la réponse n'est pas au format JSON valide.\nContenu brut :\n{response.text}")
