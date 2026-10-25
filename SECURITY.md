# Politique de sécurité

## Versions supportées

Seule la dernière version de la branche principale reçoit des corrections de sécurité.

## Signaler une vulnérabilité

Ne pas ouvrir d'issue publique pour une vulnérabilité.

Signaler le problème en privé via un *security advisory* GitHub :

1. Ouvrir l'onglet **Security** du dépôt.
2. Cliquer sur **Report a vulnerability**.

Lien direct : https://github.com/PacomeKFP/imap-mcp/security/advisories/new

Préciser si possible :

- la description du problème et son impact ;
- les étapes pour le reproduire ;
- la version ou le commit concerné ;
- la configuration utilisée (sans mot de passe ni adresse de boîte réelle).

## Modèle de menace

Les limites connues (injection de prompt par le contenu des mails, nature client-side du contrôle des droits, identifiants lisibles par tout processus du même utilisateur système) sont décrites dans la section « Modèle de menace et limites » du [README](README.md). Un signalement qui porte sur ces limites reste bienvenu, mais elles sont déjà documentées.

## Stockage des identifiants

Le mot de passe est stocké uniquement dans le trousseau système, via `imap-mcp setup`. Il n'est jamais lu depuis les variables d'environnement (`MAIL_USER` et `MAIL_PASSWORD` sont ignorées) et n'est écrit dans aucun fichier du projet.

Le trousseau est accessible à tout processus lancé sous le même compte utilisateur système. Un agent disposant d'un accès shell peut donc le lire et contourner le serveur. La protection réelle consiste à limiter le compte côté serveur de messagerie (compte en lecture seule ou mot de passe d'application limité).
