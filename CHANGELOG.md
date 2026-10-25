# Changelog

Toutes les modifications notables de ce projet sont documentées ici.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).

## [Unreleased]

### Security

- Les certificats TLS du serveur IMAP sont désormais vérifiés. `MAIL_TLS_INSECURE=1` permet de désactiver cette vérification (à réserver aux serveurs de test).
- Correction d'une injection de commandes IMAP : les caractères de contrôle dans les valeurs envoyées au serveur (chaînes entre guillemets, littéraux de recherche, ensembles d'UID) sont rejetés.
- Correction d'un contournement des droits par alias de dossier.
- Les lectures contrôlent désormais les droits sur le dossier source.

### Fixed

- Les envois non idempotents (envoi, réponse, transfert) signalent un avertissement au lieu d'une erreur lorsque le message a été remis mais qu'une étape ultérieure a échoué.

### Added

- Intégration continue GitHub Actions : lint, vérification du format, vérification des types et tests sur Ubuntu, Windows et macOS, avec Python 3.10, 3.12 et 3.14.
- Hooks pre-commit (ruff et ruff-format).
- Configuration de ruff, mypy et coverage dans `pyproject.toml`.
- Marqueur `py.typed` pour les utilisateurs typés.
- Version unique, lue depuis `src/imap_mcp/__init__.py`.
- Variable d'environnement `MAIL_TLS_INSECURE` (défaut `0`).
- Fixture partagée `make_settings` pour les tests.
- Fichier `SECURITY.md` (procédure de signalement privé).
