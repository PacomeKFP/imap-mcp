"""Exceptions du connecteur, renvoyées telles quelles au client MCP."""


class MailError(Exception):
    """Erreur métier : dossier, message, serveur ou droits."""


class ConfigError(MailError):
    """Configuration ou identifiants manquants."""
