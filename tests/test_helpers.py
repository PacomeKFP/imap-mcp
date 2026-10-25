import pytest

from imap_mcp import utf7
from imap_mcp.compose import prefixed
from imap_mcp.content import html_to_text
from imap_mcp.errors import MailError
from imap_mcp.imap import uid_set


@pytest.mark.parametrize("name", ["INBOX", "Éléments envoyés", "Projets/Alpha & Co"])
def test_utf7_round_trip(name: str) -> None:
    assert utf7.decode(utf7.encode(name)) == name


def test_uid_set_accepts_integers() -> None:
    assert uid_set(["1", " 2 ", "3"]) == "1,2,3"


@pytest.mark.parametrize("bad", [[], ["1 FETCH"], ["abc"]])
def test_uid_set_rejects_invalid(bad: list[str]) -> None:
    with pytest.raises(MailError):
        uid_set(bad)


def test_prefixed_adds_only_once() -> None:
    pattern = r"(?i)^(re|réf)\s*:"
    assert prefixed("Hello", "Re:", pattern) == "Re: Hello"
    assert prefixed("re: Hello", "Re:", pattern) == "re: Hello"


def test_html_to_text_drops_scripts() -> None:
    assert html_to_text("<p>Bonjour</p><script>alert(1)</script>") == "Bonjour"
