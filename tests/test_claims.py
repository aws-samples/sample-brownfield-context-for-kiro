"""Tests for bcp.claims (sections, citations, token classes, body hash)."""

from __future__ import annotations

import logging

import pytest

from bcp import claims
from bcp.claims import body_sha256, classify

EXT = [".java", ".py", ".xml"]


def test_heading_variants_and_boundaries() -> None:
    text = "# T\n\n## BR-1: One\nbody1\n## BR-2 — Two\nbody2\n## Notes\nnot a rule\n## BR-3. Three\n"
    parsed = claims.parse(text)
    assert [(c.rule_id, c.title) for c in parsed] == [("BR-1", "One"), ("BR-2", "Two"), ("BR-3", "Three")]
    assert "not a rule" not in parsed[1].text


def test_domain_segmented_rule_ids() -> None:
    # AWS Transform extraction (Mode A tested and Mode B) writes BR-<DOMAIN>-NNN ids, e.g. BR-ALT-001.
    text = (
        "## BR-ALT-001: Saving an alert requires a privilege\nbody a\n"
        "## BR-MSG-012 — Sending a message\nbody m\n"
        "## BR-ALT-002. Purging an alert\nbody p\n"
    )
    parsed = claims.parse(text)
    assert [(c.rule_id, c.title) for c in parsed] == [
        ("BR-ALT-001", "Saving an alert requires a privilege"),
        ("BR-MSG-012", "Sending a message"),
        ("BR-ALT-002", "Purging an alert"),
    ]


def test_duplicate_ids_keep_first(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        parsed = claims.parse("## BR-1: A\nfirst\n## BR-1: B\nsecond\n")
    assert len(parsed) == 1 and parsed[0].title == "A"
    assert "duplicate" in caplog.text


def test_no_source_line() -> None:
    parsed = claims.parse("## BR-1: A\nplain text `Foo.bar`\n")
    assert parsed[0].citations == [] and parsed[0].tokens == []


def test_multiple_and_wrapped_citations() -> None:
    text = (
        "## BR-1: A\n"
        "- Source: `VisitService.getActive`,\n"
        "  `VisitServiceImpl.save`.\n"
        "- Next bullet `Other.x`\n"
        "- Source: `A.b`\n"
        "\n"
        "  `NotJoined.c`\n"
        "- Source: `C.d`\n"
        "  ```\n"
        "  `Fenced.e`\n"
    )
    parsed = claims.parse(text, extensions=EXT)[0]
    assert parsed.citations == [
        "`VisitService.getActive`, `VisitServiceImpl.save`.",
        "`A.b`",
        "`C.d`",
    ]
    assert [t.raw for t in parsed.tokens] == ["VisitService.getActive", "VisitServiceImpl.save", "A.b", "C.d"]


def test_wrapped_stops_at_heading() -> None:
    parsed = claims.parse("## BR-1: A\n- Source: `X.y`\n  ### sub\n", extensions=EXT)[0]
    assert parsed.citations == ["`X.y`"]


def test_evidence_citation_line_is_parsed() -> None:
    """The AWS Transform BRE writes ``Evidence:`` bullets (not ``Source:``); both resolve by default."""
    text = (
        "## BR-1: A\n"
        "* Evidence: `api/src/main/java/app/api/impl/VisitServiceImpl.java#178-184`\n"
        "* Confidence: high\n"
        "## BR-2: B\n"
        "* Evidence: `api/.../VisitServiceImpl.java#207-217`; the handlers at\n"
        "  `api/.../VisitVoidHandler.java`\n"
    )
    parsed = claims.parse(text, extensions=EXT)
    assert parsed[0].citations == ["`api/src/main/java/app/api/impl/VisitServiceImpl.java#178-184`"]
    assert parsed[0].tokens[0].kind == "path"
    # a wrapped Evidence line joins its continuation, and "Confidence:" is not a citation
    assert parsed[1].citations == ["`api/.../VisitServiceImpl.java#207-217`; the handlers at `api/.../VisitVoidHandler.java`"]


@pytest.mark.parametrize(
    ("raw", "kind", "cls", "method", "package"),
    [
        ("api/src/Foo.java", "path", None, None, None),
        ("Foo.hbm.xml", "path", None, None, None),
        ("org.acme.tasks.AutoClose", "fqn", "AutoClose", None, "org/acme/tasks"),
        ("org.acme.Foo.bar", "fqn", "Foo", "bar", "org/acme"),
        ("Foo", "class", "Foo", None, None),
        ("Foo.bar", "class", "Foo", "bar", None),
        ("Foo.bar(Baz, int)", "class", "Foo", "bar", None),
        ("Foo#bar", "class", "Foo", "bar", None),
        ("visit.purge.inUse", "text", None, None, None),
        ("SELECT * FROM x", "text", None, None, None),
    ],
)
def test_token_shapes(raw: str, kind: str, cls: str | None, method: str | None, package: str | None) -> None:
    tok = classify(raw, EXT, None)
    assert (tok.kind, tok.cls, tok.method, tok.package_path) == (kind, cls, method, package)


def test_bare_method_bound_to_previous_class() -> None:
    parsed = claims.parse("## BR-1: A\n- Source: `VisitServiceImpl.voidVisit` / `unvoidVisit`.\n", extensions=EXT)
    second = parsed[0].tokens[1]
    assert (second.kind, second.cls, second.method, second.bound) == ("class", "VisitServiceImpl", "unvoidVisit", True)


def test_bare_method_without_class_is_text() -> None:
    """N1: a bare method with no preceding class is kept as text, not a candidate."""
    parsed = claims.parse("## BR-1: A\n- Source: `unvoidVisit` only.\n", extensions=EXT)
    assert parsed[0].tokens[0].kind == "text"
    assert parsed[0].citations == ["`unvoidVisit` only."]


def test_body_hash_crlf_and_trailing_whitespace() -> None:
    lf = "## BR-1: A\nline one\nline two\n\n"
    crlf = "## BR-1: A   \r\nline one\t\r\nline two\r\n\r\n\r\n"
    assert body_sha256(lf) == body_sha256(crlf)
    assert claims.parse(lf)[0].body_sha256 == claims.parse(crlf)[0].body_sha256
    assert body_sha256(lf) != body_sha256("## BR-1: A\nline one\nline 2\n")


def test_fixture_rules(bre_mini) -> None:  # type: ignore[no-untyped-def]
    text = (bre_mini / "detailed-domain-bre/domain-alpha/business-rules.md").read_text(encoding="utf-8")
    parsed = claims.parse(text, extensions=EXT)
    assert len(parsed) == 8
    assert parsed[4].citations == []
