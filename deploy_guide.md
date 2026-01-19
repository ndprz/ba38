# ✅ Check-list de mise en production BA380

## 🧪 Avant le déploiement

- [ ] Travailler dans le dossier `/home/ndprz/dev/`
- [ ] Vérifier que tout fonctionne localement :
  ```bash
  python3 app.py
  ```
- [ ] Tester les routes principales (`/`, `/login`, export, etc.)
- [ ] Nettoyer les `write_log()` et `print()` temporaires
- [ ] Vérifier que `.env` est bien présent et correct

---

## 🛠 Déploiement

- [ ] Activer le mode maintenance :
  ```bash
  ./enable_maintenance.sh
  ```

- [ ] Sauvegarder la version actuelle :
  ```bash
  ./backup_prod.sh
  ```

- [ ] Déployer la nouvelle version :
  ```bash
  ./deploy_to_prod.sh
  ```

---

## 🔁 Après déploiement

- [ ] Recharger l’application :
  ```bash
  touch /var/www/www_ba380_org_wsgi.py
  ```
  _(ou via l’interface Web)_

- [ ] Désactiver le mode maintenance :
  ```bash
  ./disable_maintenance.sh
  ```

- [ ] Vérifier que le site fonctionne :
  [https://www.ba380.org](https://www.ba380.org)

- [ ] Vérifier les logs :
  ```bash
  ./status_site.sh
  ```

---

## 💡 En cas de problème

- [ ] Lire les erreurs :
  ```bash
  tail -n 50 /var/log/www.ba380.org.error.log
  ```

- [ ] Restaurer une sauvegarde si nécessaire
