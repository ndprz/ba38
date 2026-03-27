import re
from pathlib import Path

FILE_PATH = Path("/srv/ba38/dev/ba38_partenaires.py")
BACKUP_PATH = FILE_PATH.with_suffix(".bak.py")

def backup():
    BACKUP_PATH.write_text(FILE_PATH.read_text(), encoding="utf-8")
    print("✅ Backup OK")

def migrate():
    lines = FILE_PATH.read_text(encoding="utf-8").split("\n")
    new = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # détecter @login_required
        if line.strip() == "@login_required":

            func_line = lines[i+1] if i+1 < len(lines) else ""
            access_line = lines[i+2] if i+2 < len(lines) else ""

            if "def " in func_line and "has_access(" in access_line:

                match = re.search(r'has_access\("([^"]+)",\s*"([^"]+)"\)', access_line)

                if match:
                    appli = match.group(1)
                    niveau = match.group(2)

                    print(f"🔧 {func_line.strip()} → {appli}/{niveau}")

                    new.append(line)
                    new.append(f'@require_access("{appli}", "{niveau}")')
                    new.append(func_line)

                    # skip bloc if
                    i += 3

                    # skip lignes indentées (flash, return...)
                    while i < len(lines) and lines[i].startswith(" " * 4):
                        i += 1

                    continue

        new.append(line)
        i += 1

    FILE_PATH.write_text("\n".join(new), encoding="utf-8")
    print("✅ Migration terminée")

if __name__ == "__main__":
    backup()
    migrate()
