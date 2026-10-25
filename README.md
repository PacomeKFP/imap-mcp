# imap-mcp

Serveur MCP qui donne à un LLM (Claude, etc.) un accès complet à une boîte mail via IMAP/SMTP : lecture, recherche, fils de discussion, pièces jointes, dossiers, brouillons, envoi, réponse et transfert.

Compatible avec Zimbra et avec tout serveur mail exposant IMAP (SSL) et SMTP (STARTTLS ou SSL).

## Installation

Prérequis : [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/PacomeKFP/imap-mcp
```

Depuis un dossier local : `uv tool install /chemin/vers/imap-mcp`.

Vérifier l'installation :

```powershell
imap-mcp --help
```

Si `imap-mcp` n'est pas reconnu, lancer `uv tool update-shell`, puis rouvrir le terminal.

La commande `imap-mcp` lance le serveur MCP (stdio). Sous-commandes : `imap-mcp setup` (identifiants) et `imap-mcp check` (test de connexion). Sans sous-commande, elle démarre le serveur.

## Configuration

Variables d'environnement :

| Variable | Obligatoire | Défaut | Rôle |
|---|---|---|---|
| `MAIL_HOST` | oui | | Serveur IMAP (ex. `imap.example.com`) |
| `MAIL_PORT` | non | `993` | Port IMAP (SSL) |
| `MAIL_SMTP_HOST` | non | = `MAIL_HOST` | Serveur SMTP |
| `MAIL_SMTP_PORT` | non | `587` | `587` = STARTTLS, `465` = SSL |
| `MAIL_USER` | non | | Adresse mail (sinon trousseau système) |
| `MAIL_PASSWORD` | non | | Mot de passe (sinon trousseau système) |
| `MAIL_READONLY` | non | `0` | `1` désactive toute écriture (envoi, déplacement, etc.) |
| `MAIL_DOWNLOAD_DIR` | non | `~/Downloads/imap-mcp` | Dossier de téléchargement des pièces jointes |
| `MAIL_MAX_BODY` | non | `20000` | Taille max du corps renvoyé au LLM (caractères) |

### Identifiants

Option 1 : variables d'environnement `MAIL_USER` et `MAIL_PASSWORD`.

Option 2 (recommandée) : enregistrer les identifiants dans le trousseau système une seule fois.

```bash
imap-mcp setup
```

Le mot de passe est saisi masqué et n'est écrit dans aucun fichier.

### Connexion à un client MCP

Copier `.mcp.example.json` en `.mcp.json` (ce fichier est ignoré par Git) et adapter `MAIL_HOST`. `uvx` télécharge et lance le package automatiquement.

```json
{
  "mcpServers": {
    "imap": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/PacomeKFP/imap-mcp", "imap-mcp"],
      "env": {"MAIL_HOST": "imap.example.com"}
    }
  }
}
```

## Configurer le serveur mail (MAIL_HOST)

`MAIL_HOST` est le nom du serveur IMAP de votre messagerie (ex. celui de votre webmail). Il se trouve dans la documentation de votre fournisseur ou dans les paramètres IMAP de votre webmail. Il n'est écrit dans aucun fichier du projet : il se définit par variable d'environnement.

PowerShell, pour la session en cours :

```powershell
$env:MAIL_HOST = "imap.example.com"
```

PowerShell, de façon permanente (rouvrir le terminal ensuite) :

```powershell
[Environment]::SetEnvironmentVariable("MAIL_HOST", "imap.example.com", "User")
```

bash (session courante) : `export MAIL_HOST=imap.example.com`.

Valeurs par défaut : IMAP en SSL sur le port `993`, SMTP sur le port `587` (STARTTLS). Pour un SMTP différent ou un port SSL 465, utiliser `MAIL_SMTP_HOST` et `MAIL_SMTP_PORT`.

## Vérifier la connexion

```powershell
imap-mcp check
```

Si tout est correct, la commande affiche la connexion TLS, la liste des dossiers et le volume du dossier de brouillons. Elle ne modifie rien dans la boîte.

En cas d'échec :

- `Variable d'environnement MAIL_HOST manquante` : définir `MAIL_HOST` (section ci-dessus).
- `Identifiants absents` : lancer `imap-mcp setup`.
- Erreur TLS ou délai dépassé : vérifier `MAIL_HOST` et `MAIL_PORT`.
- Authentification refusée : vérifier le mot de passe, et que l'accès IMAP est activé pour votre compte.

## Mode lecture seule (MAIL_READONLY)

`MAIL_READONLY=1` bloque toute écriture sur la boîte : envoi, brouillons, réponses, transferts, déplacements, copies, archivage, corbeille, indésirables, marquage (lu, suivi, répondu), création, renommage et suppression de dossiers, et `read_mail` avec `mark_read=true`.

La lecture, la recherche, la liste des dossiers et le téléchargement de pièces jointes sur le disque restent possibles. Ce réglage est une protection côté client : pour une garantie stricte, utiliser un compte en lecture seule côté serveur de messagerie.

## Ligne de commande

La CLI ne propose pas de commandes de messagerie : `imap-mcp` lance le serveur MCP, et `setup` et `check` servent à la configuration. Lire, rechercher ou envoyer des mails passe par un client MCP.

## Guide pour un agent

Si vous donnez le lien du dépôt à un agent (Claude, Copilot, etc.), il doit suivre ces étapes :

1. Vérifier que `uv` est installé (`uv --version`), sinon l'installer depuis https://docs.astral.sh/uv/.
2. Installer le paquet : `uv tool install git+https://github.com/PacomeKFP/imap-mcp`, puis vérifier avec `imap-mcp --help`.
3. Demander à l'utilisateur l'adresse IMAP du serveur (`MAIL_HOST`) et, si besoin, les ports. Ne jamais inventer ces valeurs.
4. Demander à l'utilisateur de lancer lui-même `imap-mcp setup` dans son terminal. L'agent ne doit jamais demander, afficher ni écrire le mot de passe.
5. Lancer `imap-mcp check` et corriger selon le résultat (section ci-dessus).
6. Ajouter cette configuration au client MCP, avec `MAIL_READONLY` à `1` pour les premiers essais :

```json
{
  "mcpServers": {
    "imap": {
      "type": "stdio",
      "command": "imap-mcp",
      "env": { "MAIL_HOST": "imap.example.com", "MAIL_READONLY": "1" }
    }
  }
}
```

7. Tester dans cet ordre : `whoami`, puis `list_folders`, puis `search_mails` avec `limit: 5`.
8. Ne jamais envoyer de message sans confirmation explicite de l'utilisateur : les envois sont irréversibles.

## Outils exposés

- Comptes et dossiers : `whoami`, `list_folders`, `get_quota`, `create_folder`, `rename_folder`, `delete_folder`
- Lecture : `search_mails`, `read_mail`, `get_thread`, `download_attachment`
- État et déplacement : `mark_mails`, `move_mails`, `copy_mails`, `archive_mails`, `trash_mails`, `mark_spam`
- Composition : `create_draft`, `update_draft`, `send_mail`, `send_draft`, `reply_mail`, `forward_mail`

Les envois et réponses sont irréversibles. `reply_mail` et `forward_mail` créent un brouillon par défaut.

## Sécurité

- Aucun identifiant ni nom de serveur n'est écrit dans le code.
- `.env` et `.mcp.json` sont exclus de Git.
- Aucune suppression définitive : « supprimer » déplace vers la corbeille.
- Utiliser `MAIL_READONLY=1` pour une utilisation en lecture seule.

## Développement

```bash
git clone https://github.com/PacomeKFP/imap-mcp
cd imap-mcp
uv sync --extra dev
uv run pytest
uv run imap-mcp check
```

`imap-mcp check` teste la connexion réelle en lecture seule (variables `MAIL_*` ou trousseau).

## Licence

À compléter.
