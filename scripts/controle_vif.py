#  SCRIPT DE CONTROLE DONN2ES VIF / DONN2ES BASE ASSOS
# Controle du nom et des données de distribution



import re
import pandas as pd
from datetime import datetime
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

FILE_VIF = "partenaire ADRESS2.txt"
FILE_DATE = "planning par date 202605 202606.txt"
FILE_JOUR = "planning par jour 202605 202606.txt"
FILE_ASSO = "Associations.xlsm"

# =========================
# NORMALISATION
# =========================
def normalize_code(c):
    if pd.isna(c): return ""
    return str(c).replace(".0","").strip().zfill(8)

def normalize_nom(n):
    if pd.isna(n): return ""
    return str(n).upper().replace("-", " ").replace("'", " ").strip()

def normalize_jour(j):
    if pd.isna(j): return ""
    j = str(j).strip().lower()
    mapping = {
        "lundi":"Lundi","mardi":"Mardi","mercredi":"Mercredi",
        "jeudi":"Jeudi","vendredi":"Vendredi",
        "samedi":"Samedi","dimanche":"Dimanche"
    }
    return mapping.get(j, j.capitalize())

def normalize_freq(f):
    if pd.isna(f): return ""
    f = str(f).strip().lower()
    mapping = {
        "7":"HEBDO","hebdomadaire":"HEBDO",
        "14":"BIHEBDO",
        "30":"MENSUEL","mensuel":"MENSUEL"
    }
    return mapping.get(f, f.upper())

def get_jour(date_str):
    try:
        d = datetime.strptime(date_str, "%d/%m/%Y")
        return ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"][d.weekday()]
    except:
        return ""

def extract_menu(line):
    line = line.upper()
    for t in line.split():
        if any(x in t for x in ["SEC","FR","VIAND","POINTEAU","ESOPE"]):
            return t.strip()
    return None

# =========================
# LOADERS
# =========================
def load_vif():
    data = {}
    with open(FILE_VIF, encoding="cp1252", errors="ignore") as f:
        for line in f:
            if "Partenaire :" in line and "-" in line:
                try:
                    left, right = line.split("-",1)
                    code = normalize_code(left.split(":")[1])
                    data[code] = right.strip()
                except:
                    continue
    return data

def load_planning_date():
    data = {}
    current = None
    current_date = None

    with open(FILE_DATE, encoding="cp1252", errors="ignore") as f:
        for line in f:

            if "Partenaire :" in line and "-" in line:
                try:
                    left, right = line.split("-",1)
                    code = normalize_code(left.split(":")[1])
                    current = code
                    data[current] = {
                        "nom": right.strip(),
                        "lignes": [],
                        "benef": 0
                    }
                except:
                    current = None

            elif current:

                if "Nbre Bénéf" in line:
                    val = re.findall(r"\d+", line)
                    if val:
                        data[current]["benef"] = int(val[0])

                parts = line.strip().split()
                if not parts:
                    continue

                if "/" in parts[0]:
                    current_date = parts[0]

                if current_date:
                    m = extract_menu(line)
                    if m:
                        data[current]["lignes"].append({
                            "date": current_date,
                            "menu": m
                        })

    return data

def load_planning_jour():
    data = {}
    with open(FILE_JOUR, encoding="cp1252", errors="ignore") as f:
        for line in f:
            if not re.match(r"\d{8}", line):
                continue

            parts = [p.strip() for p in line.split("\t")]
            if len(parts) < 5:
                continue

            code = normalize_code(parts[0])
            jour = normalize_jour(parts[3])
            freq = normalize_freq(parts[4])

            if code not in data:
                data[code] = {"jours": set(), "frequence": freq}

            data[code]["jours"].add(jour)

    return data

# 🔥 FILTRE VALIDITÉ UNIQUEMENT ICI
def load_base():
    df = pd.read_excel(FILE_ASSO)

    col_code = [c for c in df.columns if "vif" in c.lower()][0]
    col_nom = [c for c in df.columns if "nom" in c.lower()][0]
    col_valid = next((c for c in df.columns if "valid" in c.lower()), None)

    if col_valid:
        df = df[df[col_valid].astype(str).str.lower() == "oui"]

    df = df.rename(columns={
        col_code: "Code",
        col_nom: "BASE_nom"
    })

    df["Code"] = df["Code"].apply(normalize_code)

    return df

# =========================
# BUILD ANOMALIES
# =========================
def build_anomalies(vif, plan_date, plan_jour, df_base):

    anomalies = []

    codes_vif = set(vif.keys())
    codes_date = set(plan_date.keys())
    codes_jour = set(plan_jour.keys())
    codes_base = set(df_base["Code"])

    all_codes = codes_vif | codes_date | codes_jour | codes_base

    for code in all_codes:

        nom = (
            plan_date.get(code, {}).get("nom")
            or vif.get(code)
            or df_base.set_index("Code").get("BASE_nom", {}).get(code, "")
        )

        if code in codes_vif and code not in codes_date:
            anomalies.append([code, nom, "CRITIQUE","","","","","Pas de planification"])

        if code in codes_vif and code not in codes_jour:
            anomalies.append([code, nom, "CRITIQUE","","","","","VIF sans planning"])

        if code in codes_base and code not in codes_date:
            anomalies.append([code, nom, "CRITIQUE","","","","","Base sans planning"])

        if code in codes_date and code not in codes_base:
            anomalies.append([code, nom, "CRITIQUE","","","","","Planning sans base"])

        if code in plan_date:
            for l in plan_date[code]["lignes"]:
                menu = l.get("menu")
                if menu:
                    menu = menu.upper()
                    if not (menu.startswith("SEC") or menu.startswith("FR")):
                        anomalies.append([
                            code, nom, "WARNING","","","",
                            plan_jour.get(code, {}).get("frequence",""),
                            f"Menu atypique: {menu}"
                        ])

        if code in plan_date and code in plan_jour:
            dates = [l["date"] for l in plan_date[code]["lignes"]]
            jours_reels = sorted(set(normalize_jour(get_jour(d)) for d in dates))
            jours_attendus = sorted(plan_jour[code]["jours"])

            if set(jours_reels) != set(jours_attendus):
                anomalies.append([
                    code, nom, "INFO","",
                    ",".join(jours_reels),
                    ",".join(jours_attendus),
                    plan_jour[code]["frequence"],
                    "Jour supplémentaire (OK)"
                ])

    return pd.DataFrame(anomalies, columns=[
        "Code","Nom","Niveau","Date","Jour réel","Jour attendu","Fréquence","Description"
    ])

# =========================
# BUILD STATS
# =========================
def normalize_jours_str(s):
    if not s:
        return set()
    return set([normalize_jour(x.strip()) for x in str(s).replace(";",",").split(",") if x.strip()])

def build_stats(plan_date, plan_jour, df_base):

    base = df_base.set_index("Code")
    rows = []

    for code, d in plan_date.items():

        nom = d["nom"]
        lignes = d["lignes"]
        benef = d.get("benef", 0)

        dates = [l["date"] for l in lignes]
        menus = [l["menu"] for l in lignes]

        jours_reels = sorted(set(normalize_jour(get_jour(x)) for x in dates))
        set_jours_reels = set(jours_reels)

        menu_sec = ",".join(sorted(set([m for m in menus if m.startswith("SEC")])))
        menu_frais = ",".join(sorted(set([m for m in menus if m.startswith("FR")])))
        menu_autres = ",".join(sorted(set([m for m in menus if not (m.startswith("SEC") or m.startswith("FR"))])))

        freq_reel = normalize_freq(plan_jour.get(code, {}).get("frequence",""))

        if code in base.index:
            b = base.loc[code]
            set_jours_base = normalize_jours_str(b.get("jour_de_passage_a_la_BAI",""))
            base_sec = str(b.get("menu_sec","")).upper()
            base_frais = str(b.get("menu_frais","")).upper()
            base_freq = normalize_freq(b.get("frequence",""))
            base_nom = str(b.get("BASE_nom",""))
        else:
            set_jours_base = set()
            base_sec = base_frais = base_freq = base_nom = ""

        ecart_jour = "OK" if set_jours_reels == set_jours_base else "ECART"
        ecart_menu = "OK" if (menu_sec == base_sec and menu_frais == base_frais) else "ECART"
        ecart_freq = "OK" if freq_reel == base_freq else "ECART"
        ecart_nom = "OK" if normalize_nom(nom) in normalize_nom(base_nom) else "ECART"

        statut = "KO" if "ECART" in [ecart_jour, ecart_menu, ecart_freq, ecart_nom] else "OK"

        rows.append({
            "Code": code,
            "Nom": nom,
            "Bénéficiaires": benef,
            "Nb passages": len(set(dates)),
            "Jours (complets)": ",".join(jours_reels),
            "Fréquence": freq_reel,
            "Menu_SEC": menu_sec,
            "Menu_FRAIS": menu_frais,
            "Menu_AUTRES": menu_autres,
            "BASE_jour": ",".join(sorted(set_jours_base)),
            "BASE_menu_sec": base_sec,
            "BASE_menu_frais": base_frais,
            "BASE_frequence": base_freq,
            "BASE_nom": base_nom,
            "ECART_JOUR": ecart_jour,
            "ECART_MENU": ecart_menu,
            "ECART_FREQUENCE": ecart_freq,
            "ECART_NOM": ecart_nom,
            "STATUT_GLOBAL": statut
        })

    return pd.DataFrame(rows)

# =========================
# EXPORT
# =========================
def export(df_anom, df_stats):

    writer = pd.ExcelWriter("controle_vif_planning_FINAL.xlsx", engine="openpyxl")

    for lvl in ["CRITIQUE","WARNING","INFO"]:
        df = df_anom[df_anom["Niveau"]==lvl].sort_values("Nom")
        df.to_excel(writer, lvl.capitalize()+"s", index=False)

    df_stats.to_excel(writer, "Stats", index=False)

    writer.close()

# =========================
# MAIN
# =========================
if __name__ == "__main__":

    print("================================")
    print("CONTROLE VIF / PLANNING")
    print("================================")

    vif = load_vif()
    plan_date = load_planning_date()
    plan_jour = load_planning_jour()
    df_base = load_base()

    df_anom = build_anomalies(vif, plan_date, plan_jour, df_base)
    df_stats = build_stats(plan_date, plan_jour, df_base)

    export(df_anom, df_stats)

    print("✔ Fichier généré")
    input("Appuyez sur une touche...")