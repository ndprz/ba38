import os
import psutil
import platform
import subprocess
from datetime import datetime

# Fonction pour lire les temperatures
try:
    temps = psutil.sensors_temperatures()
    cpu_temp = None
    if temps:
        for name, entries in temps.items():
            for entry in entries:
                if 'cpu' in entry.label.lower() or 'package id' in entry.label.lower():
                    cpu_temp = entry.current
except Exception as e:
    cpu_temp = None

# Fonction pour lire l'état du disque
def check_disk():
    try:
        result = subprocess.check_output("wmic diskdrive get status", shell=True)
        result = result.decode()
        return result.strip()
    except Exception as e:
        return f"Erreur disque: {e}"

# Fonction pour générer le rapport batterie
def check_battery():
    try:
        battery = psutil.sensors_battery()
        if battery:
            percent = battery.percent
            plugged = battery.power_plugged
            return f"Charge: {percent}%, Branché: {plugged}"
        else:
            return "Pas de batterie détectée."
    except Exception as e:
        return f"Erreur batterie: {e}"

# Générer le rapport final
rapport = []
rapport.append(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
rapport.append(f"Système: {platform.system()} {platform.release()} ({platform.version()})")
rapport.append("------------------------")

# Température CPU
if cpu_temp:
    rapport.append(f"Température CPU: {cpu_temp:.1f}°C")
else:
    rapport.append("Température CPU: Non disponible")

# État disque
rapport.append("\nÉtat du disque:")
rapport.append(check_disk())

# Batterie
rapport.append("\nÉtat de la batterie:")
rapport.append(check_battery())

# Affichage final
print("\n".join(rapport))

# Pause pour lire tranquillement
input("\nAppuyez sur Entrée pour quitter...")
