"""Enregistre l'adresse et le mot de passe dans le trousseau systeme (Windows, macOS ou Linux).

A lancer une fois : python setup_credentials.py
Le mot de passe est saisi de facon masquee et n'apparait dans aucun fichier.
"""
import getpass

import keyring

from core import SERVICE

user = input("Adresse mail : ").strip()
password = getpass.getpass("Mot de passe : ")
keyring.set_password(SERVICE, "user", user)
keyring.set_password(SERVICE, user, password)
print("Identifiants enregistres dans le trousseau systeme.")
