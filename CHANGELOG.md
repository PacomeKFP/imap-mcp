# Changelog

Toutes les modifications notables de ce projet sont documentées ici.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).

## [Unreleased]

### Security

- **Rupture :** les identifiants ne sont plus lus depuis les variables d'environnement `MAIL_USER` et `MAIL_PASSWORD`, qui sont ignorées. Les identifiants se lisent uniquement dans le trousseau système ; lancer `imap-mcp setup` pour les enregistrer.
- Les certificats TLS du serveur IMAP sont désormais vérifiés. `MAIL_TLS_INSECURE=1` permet de désactiver cette vérification (à réserver aux serveurs de test).
- Correction d'une injection de commandes IMAP : les caractères de contrôle dans les valeurs envoyées au serveur (chaînes entre guillemets, littéraux de recherche, ensembles d'UID) sont rejetés.
- Correction d'un contournement des droits par alias de dossier.
- Les lectures contrôlent désormais les droits sur le dossier source.

### Changed

- **Rupture :** l'outil `delete_mails` est renommé `trash_mails` (il déplace déjà vers `Agent Trash`). Mettre à jour les noms d'outils dans les prompts et la configuration des clients.
- **Rupture :** les outils de modification (`mark_mails`, `move_mails`, `copy_mails`, `archive_mails`, `mark_spam`, `trash_mails`, `create_folder`, `rename_folder`) renvoient des objets structurés au lieu de phrases : `{count, folder, destination}`, `{count, folder, changes}`, `{folder}` ou `{old_name, new_name}`.
- `search_mails` renvoie `uidvalidity` ; les UID ne sont stables que tant qu'il ne change pas.
- La recherche et les déplacements exploitent les nouvelles API d'`imap` : tri décroissant côté serveur, dossiers spéciaux via le cache des rôles.
- La création et le renommage de dossiers vident le cache des dossiers spéciaux.
- Les outils ouvrent une seule connexion IMAP par appel (`get_thread` en ouvrait deux).
- Les paramètres des outils de modification au-delà du premier identifiant sont désormais nommés (mot-clé uniquement).
- Les identifiants du trousseau sont mis en cache pendant la durée du processus : redémarrer le serveur MCP après un `imap-mcp setup`.

- **Rupture :** les variables `MAIL_USER` et `MAIL_PASSWORD` ne sont plus utilisées. Migration : `imap-mcp setup`, puis retirer ces variables de la configuration du client MCP.
- Les identifiants du trousseau sont mis en cache pendant la durée du processus : redémarrer le serveur MCP après un `imap-mcp setup`.
- Les réglages sont chargés à la première utilisation et non plus à l'import : `imap-mcp --help` fonctionne même si la configuration est invalide.
- Une configuration invalide (ou des identifiants absents) affiche `Failed: ...` et quitte avec le code 1, au lieu d'une trace d'erreur.
- Les ports (`MAIL_PORT`, `MAIL_SMTP_PORT`) doivent être compris entre 1 et 65535 ; `MAIL_MAX_BODY` et `MAIL_TIMEOUT` doivent être positifs. Une valeur invalide produit une erreur explicite.

### Fixed

- Les envois non idempotents (envoi, réponse, transfert) signalent un avertissement au lieu d'une erreur lorsque le message a été remis mais qu'une étape ultérieure a échoué.
- `MAIL_DOWNLOAD_DIR` accepte le préfixe `~` (développé vers le dossier personnel).

### Added

- Variable `MAIL_TIMEOUT` (défaut `40` secondes) pour régler le délai des connexions IMAP et SMTP.
- Intégration continue GitHub Actions : lint, vérification du format, vérification des types et tests sur Ubuntu, Windows et macOS, avec Python 3.10, 3.12 et 3.14.
- Hooks pre-commit (ruff et ruff-format).
- Configuration de ruff, mypy et coverage dans `pyproject.toml`.
- Marqueur `py.typed` pour les utilisateurs typés.
- Version unique, lue depuis `src/imap_mcp/__init__.py`.
- Variable d'environnement `MAIL_TLS_INSECURE` (défaut `0`).
- Fixture partagée `make_settings` pour les tests.
- Fichier `SECURITY.md` (procédure de signalement privé).
