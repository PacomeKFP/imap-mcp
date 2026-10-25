"""Localized text inserted into outgoing mail: quote and forward headers, subject prefixes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Locale:
    quote: str  # placeholders: {when}, {who}
    forward_title: str
    from_label: str
    date_label: str
    subject_label: str
    to_label: str


FRENCH = Locale(
    quote="Le {when}, {who} a écrit :",
    forward_title="---------- Message transféré ----------",
    from_label="De :",
    date_label="Date :",
    subject_label="Objet :",
    to_label="À :",
)

ENGLISH = Locale(
    quote="On {when}, {who} wrote:",
    forward_title="---------- Forwarded message ----------",
    from_label="From:",
    date_label="Date:",
    subject_label="Subject:",
    to_label="To:",
)

LOCALES: dict[str, Locale] = {"fr": FRENCH, "en": ENGLISH}

REPLY_PREFIXES = ("re", "réf")
FORWARD_PREFIXES = ("fwd", "tr")


def get_locale(code: str) -> Locale:
    """Locale for a language code such as 'fr' or 'fr-FR'; unknown codes give English."""
    language = code.replace("_", "-").split("-", 1)[0].strip().lower()
    return LOCALES.get(language, ENGLISH)


def quote_header(locale: str, when: str, who: str) -> str:
    return get_locale(locale).quote.format(when=when, who=who)


def forward_header(locale: str, sender: str, date: str, subject: str, to: str) -> str:
    lang = get_locale(locale)
    return (
        f"\n\n{lang.forward_title}\n"
        f"{lang.from_label} {sender}\n"
        f"{lang.date_label} {date}\n"
        f"{lang.subject_label} {subject}\n"
        f"{lang.to_label} {to}\n\n"
    )


def prefix_pattern(prefixes: tuple[str, ...]) -> str:
    """Case-insensitive regex matching a subject that starts with one of the prefixes."""
    alternatives = "|".join(prefixes)
    return rf"(?i)^\s*(?:{alternatives})\s*:"


REPLY_PATTERN = prefix_pattern(REPLY_PREFIXES)
FORWARD_PATTERN = prefix_pattern(FORWARD_PREFIXES)
