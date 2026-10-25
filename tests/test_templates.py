"""Localized quote and forward headers, locale lookup and subject prefix patterns."""

from __future__ import annotations

import re

import pytest

from imap_mcp.compose import prefixed
from imap_mcp.templates import (
    ENGLISH,
    FORWARD_PATTERN,
    FRENCH,
    LOCALES,
    REPLY_PATTERN,
    forward_header,
    get_locale,
    prefix_pattern,
    quote_header,
)


def test_french_quote_header() -> None:
    assert (
        quote_header("fr", "lundi 1 janvier 2024", "Ann")
        == "Le lundi 1 janvier 2024, Ann a écrit :"
    )


def test_english_quote_header() -> None:
    assert quote_header("en", "Mon, 1 Jan 2024", "Ann") == "On Mon, 1 Jan 2024, Ann wrote:"


def test_unknown_locale_uses_english() -> None:
    assert get_locale("de") is ENGLISH
    assert quote_header("de", "d", "w") == quote_header("en", "d", "w")
    assert forward_header("xx", "a", "d", "s", "t") == forward_header("en", "a", "d", "s", "t")


@pytest.mark.parametrize(
    ("code", "locale"),
    [
        ("fr", FRENCH),
        ("FR", FRENCH),
        ("fr-FR", FRENCH),
        ("fr_CA", FRENCH),
        ("en-US", ENGLISH),
        ("", ENGLISH),
    ],
)
def test_locale_codes_are_normalised(code: str, locale: object) -> None:
    assert get_locale(code) is locale


def test_locales_cover_french_and_english() -> None:
    assert set(LOCALES) == {"fr", "en"}


def test_french_forward_header_keeps_the_legacy_text() -> None:
    assert forward_header("fr", "Ann <ann@x.org>", "Mon, 1 Jan 2024", "Hi", "Bob") == (
        "\n\n---------- Message transféré ----------\n"
        "De : Ann <ann@x.org>\n"
        "Date : Mon, 1 Jan 2024\n"
        "Objet : Hi\n"
        "À : Bob\n\n"
    )


def test_english_forward_header() -> None:
    assert forward_header("en", "Ann", "Mon", "Hi", "Bob") == (
        "\n\n---------- Forwarded message ----------\nFrom: Ann\nDate: Mon\nSubject: Hi\nTo: Bob\n\n"
    )


@pytest.mark.parametrize("subject", ["Re: hi", "RE: hi", "réf: hi", "Réf : hi", "RÉF: hi"])
def test_reply_pattern_accepts_english_and_french(subject: str) -> None:
    assert re.match(REPLY_PATTERN, subject)


@pytest.mark.parametrize("subject", ["Fwd: hi", "fwd : hi", "TR: hi", "tr: hi"])
def test_forward_pattern_accepts_english_and_french(subject: str) -> None:
    assert re.match(FORWARD_PATTERN, subject)


@pytest.mark.parametrize("subject", ["Fwd: hi", "Rex: hi", "hi Re: there", ""])
def test_reply_pattern_rejects_other_subjects(subject: str) -> None:
    assert not re.match(REPLY_PATTERN, subject)


@pytest.mark.parametrize("subject", ["Re: hi", "Hi Tr: there"])
def test_forward_pattern_rejects_other_subjects(subject: str) -> None:
    assert not re.match(FORWARD_PATTERN, subject)


def test_prefixed_keeps_an_existing_localized_prefix() -> None:
    assert prefixed("Réf: hi", "Re:", REPLY_PATTERN) == "Réf: hi"
    assert prefixed("Tr: hi", "Fwd:", FORWARD_PATTERN) == "Tr: hi"
    assert prefixed("hi", "Re:", REPLY_PATTERN) == "Re: hi"


def test_prefix_pattern_matches_any_given_prefix_case_insensitively() -> None:
    pattern = prefix_pattern(("foo", "bar"))
    assert re.match(pattern, "BAR : x")
    assert re.match(pattern, "  foo:x")
    assert not re.match(pattern, "baz: x")
