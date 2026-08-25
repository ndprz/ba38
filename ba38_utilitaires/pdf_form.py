from pypdf import PdfReader, PdfWriter

from datetime import datetime

from pypdf import PdfReader, PdfWriter
from datetime import datetime

def remplir_pdf_indicateurs(template_path, output_path, association, campagne):

    reader = PdfReader(template_path)
    writer = PdfWriter()

    writer.clone_document_from_reader(reader)

    periode = campagne["periode"]

    parts = periode.split()
    trimestre = parts[0] if len(parts) > 0 else ""
    annee = parts[1] if len(parts) > 1 else ""

    # 🔥 FORMAT DATE
    date_limite = campagne["date_limite"]

    if date_limite:
        try:
            date_limite = datetime.strptime(date_limite, "%Y-%m-%d").strftime("%d/%m/%Y")
        except:
            pass

    data = {
        "trimestre": trimestre,
        "annee": annee,
        "date_limite": date_limite,

        "nom_association": association.get("nom_association", ""),
        "code_vif": association.get("code_vif", ""),
        "responsable_IE": association.get("responsable_IE", ""),
        "tel_resp_ie": association.get("tel_resp_IE", ""),

        "courriel_resp_IE1": (association.get("courriel_resp_IE1") or ""),
        "courriel_resp_IE2": (association.get("courriel_resp_IE2") or ""),

        "CAR": association.get("CAR", ""),
    }

    writer.update_page_form_field_values(writer.pages[0], data)

    with open(output_path, "wb") as f:
        writer.write(f)

    return output_path
