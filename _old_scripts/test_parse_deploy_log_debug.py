import re

from utils import get_log_path


log_path = get_log_path("deploy.log")


print("📄 Fichier analysé :", log_path)

print("--------------------------------------------------")


regex_date = re.compile(r".*Déploiement.*?:\s*(.+)")


historiques = []


with open(log_path, "r", encoding="utf-8", errors="ignore") as f:

    for i, raw_line in enumerate(f, start=1):

        line = raw_line.rstrip("\n")


        # Affichage brut (repr = vérité absolue)

        print(f"LIGNE {i:03d} RAW :", repr(line))


        m = regex_date.match(line)

        if m:

            print(f"   ✅ MATCH → date = {m.group(1)}")

            historiques.append(m.group(1))

        else:

            print("   ❌ pas de match")


print("--------------------------------------------------")

print("📊 TOTAL MATCHES =", len(historiques))

