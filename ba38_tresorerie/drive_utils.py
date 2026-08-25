import time

from utils import write_log, get_drive_folder_id_from_path

from ba38_tresorerie.constants import BA380_SHARED_DRIVE_ID


def get_pdf_by_code_vif(service, folder_id, code_vif_8):
    """
    Recherche un PDF FACTURE_<code_vif_8>_*.pdf
    Compatible Shared Drive
    """

    query = (
        f"'{folder_id}' in parents "
        f"and name contains 'FACTURE_{code_vif_8}_' "
        f"and trashed = false"
    )

    results = service.files().list(
        q=query,
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()

    files = results.get("files", [])

    if not files:
        return None, None

    return files[0]["id"], files[0]["name"]


# ===============================================
# 🧹 Supprimer complètement le contenu d’un dossier Drive
# (Drive partagé, pagination, attente réelle)
# ===============================================
def delete_drive_folder_contents(drive_path, wait_until_empty=True, timeout=30):
    """
    Supprime TOUS les fichiers d'un dossier Google Drive existant
    (Drive partagé compatible), avec pagination.
    Optionnellement attend que le dossier soit réellement vide.
    """

    import os
    import time
    from utils import write_log, SERVICE_ACCOUNT_FILE
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    try:
        if not BA380_SHARED_DRIVE_ID:
            write_log("❌ BA380_SHARED_DRIVE_ID non défini")
            return

        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/drive"]
        )

        service = build("drive", "v3", credentials=credentials)

        # 🔎 Résolution du dossier (sans création)
        folder_id = get_drive_folder_id_from_path(
            drive_path,
            BA380_SHARED_DRIVE_ID
        )

        if not folder_id:
            write_log(f"⚠️ Dossier Drive inexistant : {drive_path}")
            return

        write_log(f"🧹 Nettoyage dossier Drive : {drive_path}")

        # ============================
        # 🔁 LISTE COMPLÈTE (pagination)
        # ============================
        files = []
        page_token = None

        while True:
            response = service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                corpora="drive",
                driveId=BA380_SHARED_DRIVE_ID,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                fields="nextPageToken, files(id, name)",
                pageSize=1000,
                pageToken=page_token
            ).execute()

            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")

            if not page_token:
                break

        if not files:
            write_log("ℹ️ Aucun fichier à supprimer.")
            return

        write_log(f"🗑️ {len(files)} fichiers à supprimer…")

        # ============================
        # 🗑️ SUPPRESSION
        # ============================
        for i, f in enumerate(files, start=1):
            service.files().delete(
                fileId=f["id"],
                supportsAllDrives=True
            ).execute()
            write_log(f"🗑️ [{i}/{len(files)}] Supprimé : {f['name']}")

        write_log("✅ Suppression demandée pour tous les fichiers.")

        # ============================
        # ⏳ ATTENTE VIDAGE RÉEL
        # ============================
        if wait_until_empty:
            write_log("⏳ Attente vidage réel du dossier Drive…")
            start = time.time()

            while time.time() - start < timeout:
                remaining = service.files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    corpora="drive",
                    driveId=BA380_SHARED_DRIVE_ID,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    fields="files(id)",
                    pageSize=10
                ).execute().get("files", [])

                if not remaining:
                    write_log("✅ Dossier Drive confirmé vide.")
                    return

                time.sleep(1)

            write_log("⚠️ Timeout attente vidage Drive (poursuite quand même)")

    except Exception as e:
        write_log(f"❌ Erreur delete_drive_folder_contents : {e}")


def wait_until_drive_folder_empty(service, folder_id, drive_id, timeout=30):
    """
    Attend que le dossier Drive soit réellement vide
    (cohérence Drive), avec timeout en secondes.
    """
    import time

    start = time.time()

    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            corpora="drive",
            driveId=drive_id,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id)"
        ).execute()

        if not results.get("files"):
            return True

        if time.time() - start > timeout:
            return False

        time.sleep(1)
