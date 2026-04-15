import os
import re
import subprocess

# =========================================================
# ⚙️ CONFIGURATION
# =========================================================
BASE_DIR = "/srv/ba38/dev"
DOC_DIR = "/srv/ba38/documentation_technique/02_application_flask"

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
SCRIPTS_TACHES_DIR = "/srv/ba38/scripts_taches"

EXCLUDE_DIRS = {"__pycache__", ".git", "venv", ".venv"}
EXCLUDE_EXT = {".pyc", ".log", ".tmp"}
EXCLUDE_FILES = {"__init__.py"}

# =========================================================
# 📁 TREE GENERIQUE
# =========================================================
def generate_tree(start_path, prefix=""):
    lines = []

    try:
        entries = sorted(os.listdir(start_path))
    except Exception:
        return lines

    # filtrage AVANT
    entries = [
        name for name in entries
        if name not in EXCLUDE_DIRS
        and not any(name.endswith(ext) for ext in EXCLUDE_EXT)
    ]

    for i, name in enumerate(entries):
        path = os.path.join(start_path, name)

        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "

        lines.append(prefix + connector + name)

        if os.path.isdir(path):
            extension = "    " if is_last else "│   "
            lines.extend(generate_tree(path, prefix + extension))

    return lines

# =========================================================
# 🔍 CRON
# =========================================================
def get_crontab_content():
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True
        )
        return result.stdout
    except Exception:
        return ""

# =========================================================
# 🔍 ANALYSE SCRIPTS
# =========================================================
def analyze_scripts_usage():

    scripts = []

    # scripts dev
    for f in os.listdir(SCRIPTS_DIR):
        if f.endswith(".py"):
            scripts.append(("dev", f))

    # scripts_taches
    if os.path.exists(SCRIPTS_TACHES_DIR):
        for f in os.listdir(SCRIPTS_TACHES_DIR):
            if f.endswith(".py"):
                scripts.append(("taches", f))

    used_import = set()
    used_cron = set()

    # 🔍 imports Python
    for root, _, files in os.walk(BASE_DIR):
        for file in files:
            if not file.endswith(".py"):
                continue

            path = os.path.join(root, file)

            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            for origin, script in scripts:
                module = script.replace(".py", "")

                if module == file.replace(".py", ""):
                    continue

                if f"import {module}" in content or f"from {module}" in content:
                    used_import.add(script)

    # 🔍 cron
    cron_content = get_crontab_content()

    for origin, script in scripts:
        if script in cron_content:
            used_cron.add(script)

    return scripts, used_import, used_cron

# =========================================================
# 📄 DOC SCRIPTS
# =========================================================
def generate_scripts_doc():

    if not os.path.exists(SCRIPTS_DIR):
        return

    tree = generate_tree(SCRIPTS_DIR)

    content = "# 🛠️ Structure des scripts\n\n"
    content += f"Répertoire : `{SCRIPTS_DIR}`\n\n"

    content += "```\n"
    content += "scripts/\n"
    content += "\n".join(tree)
    content += "\n```\n"

    # 🔍 analyse
    scripts, used_import, used_cron = analyze_scripts_usage()

    content += "\n## 🔍 Utilisation des scripts\n\n"

    for origin, script in sorted(scripts):

        label = f"{script} ({origin})"

        if script in used_cron:
            content += f"- <span class='badge bg-success'>CRON</span> {label}\n"

        elif script in used_import:
            content += f"- <span class='badge bg-primary'>IMPORT</span> {label}\n"

        elif "test" in script or "debug" in script:
            content += f"- <span class='badge bg-danger'>TEST</span> {label}\n"

        elif "migrate" in script or "update" in script or "sync" in script:
            content += f"- <span class='badge bg-warning text-dark'>MIGRATION</span> {label}\n"

        else:
            content += f"- <span class='badge bg-secondary'>MANUEL</span> {label}\n"

    with open(os.path.join(DOC_DIR, "structure_scripts.md"), "w", encoding="utf-8") as f:
        f.write(content)

# =========================================================
# 📄 DOC TEMPLATES
# =========================================================
def generate_templates_doc():
    tree = generate_tree(TEMPLATES_DIR)

    content = "# 📁 Structure des templates\n\n"
    content += f"Répertoire : `{TEMPLATES_DIR}`\n\n"

    content += "```\n"
    content += "templates/\n"
    content += "\n".join(tree)
    content += "\n```\n"

    with open(os.path.join(DOC_DIR, "structure_templates.md"), "w", encoding="utf-8") as f:
        f.write(content)

# =========================================================
# 🧠 ROUTES FLASK
# =========================================================
def extract_routes(content):

    pattern = re.compile(
        r'((?:@\w+.*\n)+)\s*def\s+(\w+)\(',
        re.MULTILINE
    )

    matches = pattern.findall(content)
    routes = []

    for decorators, func_name in matches:

        route_match = re.search(r'\.route\(\s*"([^"]+)"', decorators)
        route = route_match.group(1) if route_match else None

        if not route:
            continue

        methods_match = re.search(r'methods\s*=\s*\[([^\]]+)\]', decorators)

        if methods_match:
            methods = [
                m.strip().replace('"', '').replace("'", "")
                for m in methods_match.group(1).split(",")
            ]
        else:
            methods = ["GET"]

        access_match = re.search(
            r'@require_access\("([^"]+)",\s*"([^"]+)"',
            decorators
        )

        admin_only = "@require_admin_global" in decorators

        access = None
        if access_match:
            access = {
                "appli": access_match.group(1),
                "niveau": access_match.group(2)
            }

        # template
        func_pattern = re.search(
            rf'def\s+{func_name}\([^)]*\):([\s\S]*?)(?=\n@|\Z)',
            content
        )

        template = None
        if func_pattern:
            template_match = re.search(
                r'render_template\("([^"]+)"',
                func_pattern.group(1)
            )
            if template_match:
                template = template_match.group(1)

        routes.append({
            "route": route,
            "methods": methods,
            "access": access,
            "admin_only": admin_only,
            "template": template
        })

    return routes

# =========================================================
# 🧠 ANALYSE CODE PYTHON
# =========================================================
def analyze_python_files():

    results = []

    for filename in os.listdir(BASE_DIR):

        if not filename.endswith(".py"):
            continue

        if filename in EXCLUDE_FILES:
            continue

        path = os.path.join(BASE_DIR, filename)

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        blueprint_match = re.findall(
            r'(\w+)\s*=\s*Blueprint\("([^"]+)"',
            content
        )

        blueprint_name = blueprint_match[0][1] if blueprint_match else None
        routes = extract_routes(content)

        results.append({
            "file": filename,
            "blueprint_name": blueprint_name,
            "routes": routes
        })

    return results

# =========================================================
# 📄 STRUCTURE CODE
# =========================================================
def generate_code_doc(analysis):

    content = "# 🧩 Structure du code Python\n\n```\n"

    for item in analysis:
        content += f"├── {item['file']}\n"

    content += "```\n\n"

    content += "## 🧠 Blueprints\n\n"

    for item in analysis:
        if item["blueprint_name"]:
            content += f"- `{item['blueprint_name']}` → `{item['file']}`\n"

    with open(os.path.join(DOC_DIR, "structure_code.md"), "w", encoding="utf-8") as f:
        f.write(content)

# =========================================================
# 📄 ROUTES DETAILLEES
# =========================================================
def generate_routes_doc(analysis):

    content = "# 📊 Architecture des routes Flask\n\n"

    for item in analysis:

        if not item["routes"]:
            continue

        prefix = f"/{item['blueprint_name']}" if item["blueprint_name"] else ""

        content += f"## 📄 {item['file']}\n\n"

        for r in item["routes"]:

            full_route = prefix + r["route"]
            methods = ", ".join(r["methods"])

            content += f"- **{methods} {full_route}**\n"

            if r["template"]:
                content += f"  - 📄 template : `{r['template']}`\n"

            if r["admin_only"]:
                content += f"  - 🔐 accès : ADMIN GLOBAL\n"

            elif r["access"]:
                content += f"  - 🔐 accès : `{r['access']['appli']}` → `{r['access']['niveau']}`\n"

        content += "\n"

    with open(os.path.join(DOC_DIR, "architecture_routes.md"), "w", encoding="utf-8") as f:
        f.write(content)

# =========================================================
# MAIN
# =========================================================
if __name__ == "__main__":

    generate_templates_doc()

    analysis = analyze_python_files()

    generate_code_doc(analysis)
    generate_routes_doc(analysis)
    generate_scripts_doc()

    print("✅ Documentation complète générée")
