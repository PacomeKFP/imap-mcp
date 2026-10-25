"""Test de connexion IMAP en lecture seule vers n'importe quel serveur IMAP/SSL.

Variables d'environnement :
  MAIL_USER            adresse mail complete
  MAIL_PASSWORD        mot de passe (jamais ecrit dans le code)
  MAIL_HOST            serveur IMAP (obligatoire)
  MAIL_PORT            optionnel, defaut 993
  MAIL_DRAFTS_FOLDER   optionnel, defaut Drafts
"""
import imaplib
import os
import sys

host = os.environ.get("MAIL_HOST", "")
port = int(os.environ.get("MAIL_PORT", "993"))
user = os.environ.get("MAIL_USER")
password = os.environ.get("MAIL_PASSWORD")
DRAFTS = os.environ.get("MAIL_DRAFTS_FOLDER", "Drafts")

if not host or not user or not password:
    sys.exit(
        "Definir MAIL_HOST, MAIL_USER et MAIL_PASSWORD avant de lancer le script.")

try:
    imap = imaplib.IMAP4_SSL(host, port, timeout=20)
    print(f"Connexion TLS OK vers {host}:{port}")
    imap.login(user, password)
    print("Authentification OK")
    status, folders = imap.list()
    print(f"Dossiers ({status}) :")
    for f in folders:
        print("  ", f.decode(errors="replace"))
    status, data = imap.select("INBOX", readonly=True)
    print(f"INBOX ouverte en lecture seule, {data[0].decode()} messages")
    # Volume des brouillons (lecture seule, tailles uniquement, aucun contenu lu)
    status, data = imap.select(DRAFTS, readonly=True)
    count = int(data[0])
    total = 0
    if count:
        status, rows = imap.fetch(f"1:{count}", "(RFC822.SIZE)")
        for r in rows:
            if isinstance(r, bytes) and b"RFC822.SIZE" in r:
                total += int(r.split(b"RFC822.SIZE")[1].strip(b" )"))
    print(f"{DRAFTS} : {count} messages, {total / 1024:.1f} Ko au total")
    imap.logout()
except imaplib.IMAP4.error as e:
    sys.exit(f"Echec IMAP (identifiants refuses ou IMAP desactive) : {e}")
except OSError as e:
    sys.exit(f"Echec reseau/TLS : {e}")
