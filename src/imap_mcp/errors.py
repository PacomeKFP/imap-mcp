"""Exceptions of the connector. The message starts with a stable code the agent can match on."""


class MailError(Exception):
    """Business error: folder, message, server, rights or configuration."""

    def __init__(self, message: str, code: str = "error") -> None:
        self.code = code
        super().__init__(f"[{code}] {message}")


class ConfigError(MailError):
    """Missing or invalid configuration or credentials."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="config")
