#!/usr/bin/env python3
import os

def read_env(path):
    try:
        with open(path, "r") as f:
            content = f.read()
        print(content)
    except Exception as e:
        print(f"❌ Erreur lecture fichier : {e}")

read_env("/home/ndprz/ba380/.env")
