"""Tests for the review policy filter (render.reviewed_rules) and the 9-row sign-off table."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from bcp import claims as claims_mod
from bcp.manifest import Domain
from bcp.render import ANCHORS_PREFIX, NO_RULES, RULES_HEADER, anchors_line, reviewed_rules
from bcp.review import ReviewData, effective_status

TEXT = (
    "## BR-1: First rule\n- Source: `A.b`.\nbody one\n\n"
    "## BR-2: Second rule\nbody two\n\n"
    "## BR-3: Third rule\nbody three\n"
)
CLAIMS = claims_mod.parse(TEXT)
SHA = {c.rule_id: c.body_sha256 for c in CLAIMS}
DOM = Domain(slug="alpha", name="Alpha")


def rec(rule: str, verdict: str, rtype: str = "kiro", **extra: Any) -> dict[str, Any]:
    entry = {"verdict": verdict, "body_sha256": SHA.get(rule, "x"), "reviewer": "Bo", "reviewer_type": rtype,
             "reviewed_at": "2026-01-01T10:00:00Z", "note": "n", "correction": None, "suspected_defect": False,
             "evidence": [{"kind": "range", "path": "src/A.java", "start": 3, "end": 5, "quote": "q"}]}
    entry.update(extra)
    return entry


def so(rtype: str, decision: str, exceptions: list[str] | None = None) -> dict[str, Any]:
    return {"reviewer": "Ana", "role": "Architect", "reviewer_type": rtype, "date": "2026-01-02",
            "decision": decision, "exceptions": exceptions or []}


ROWS = [
    (1, None, None, ("unreviewed", "not reviewed")),
    (2, rec("BR-1", "verified"), so("sme", "rejected"), ("unreviewed", "SME rejected this domain's review (Ana, 2026-01-02)")),
    (3, rec("BR-1", "verified"), so("sme", "approved-with-exceptions", ["BR-1"]),
     ("needs-sme", "SME exception: not approved by Ana (2026-01-02)")),
    (4, rec("BR-1", "verified"), so("sme", "approved"), ("verified", "SME-signed by Ana (Architect, 2026-01-02)")),
    (5, rec("BR-1", "verified"), so("kiro", "rejected"), ("unreviewed", "AI review rejected, not SME-signed")),
    (6, rec("BR-1", "verified", "sme"), so("kiro", "approved"),
     ("verified", "SME-reviewed claim (Bo, 2026-01-01), domain not SME-signed")),
    (7, rec("BR-1", "verified"), so("kiro", "approved"), ("verified", "AI-reviewed, not SME-signed")),
    (8, rec("BR-1", "verified", "sme"), None, ("verified", "SME-reviewed claim (Bo, 2026-01-01), review not signed off")),
    (9, rec("BR-1", "verified"), None, ("verified", "AI-reviewed, not SME-signed (review not signed off)")),
]


@pytest.mark.parametrize(("row", "record", "signoff", "expected"), ROWS, ids=[f"row{r[0]}" for r in ROWS])
def test_signoff_table(row: int, record: dict | None, signoff: dict | None, expected: tuple[str, str]) -> None:  # type: ignore[type-arg]
    assert effective_status(record, signoff, "BR-1", SHA["BR-1"]) == expected
    review = ReviewData(record={"claims": {"alpha/BR-1": record} if record else {}},
                        signoff={"domains": {"alpha": signoff} if signoff else {}}, present=True)
    text, _ = reviewed_rules(DOM, CLAIMS, review)
    status_line = text.split("## BR-1: First rule\n", 1)[1].split("\n", 1)[0]
    assert status_line.startswith("> Review: ") and f"· {expected[1]} ·" in status_line


def test_sme_exception_keeps_needs_edit() -> None:
    record = rec("BR-1", "needs-edit", replacement="r", what_changed="w")
    verdict, _ = effective_status(record, so("sme", "approved-with-exceptions", ["BR-1"]), "BR-1", SHA["BR-1"])
    assert verdict == "needs-edit"


def test_row3_keeps_incorrect() -> None:
    record = rec("BR-1", "incorrect", correction="it does X")
    assert effective_status(record, so("sme", "approved-with-exceptions", ["BR-1"]), "BR-1", SHA["BR-1"])[0] == "incorrect"


def _review(**claims: dict[str, Any]) -> ReviewData:
    return ReviewData(record={"claims": {f"alpha/{k.replace('_', '-')}": v for k, v in claims.items()}},
                      present=True)


def test_labels_and_corrections() -> None:
    review = _review(
        BR_1=rec("BR-1", "verified", suspected_defect=True),
        BR_2=rec("BR-2", "needs-sme", note="ask the owner", evidence=[]),
        BR_3=rec("BR-3", "incorrect", correction="the code does Y"),
    )
    text, counts = reviewed_rules(DOM, CLAIMS, review)
    assert "> Review: VERIFIED, SUSPECTED DEFECT · AI-reviewed, not SME-signed (review not signed off) · evidence: src/A.java:L3-L5" in text
    assert "> Review: NEEDS SME (ask the owner)" in text and "evidence: none" in text
    assert "## BR-3" not in text
    assert text.rstrip().endswith("- BR-3: Third rule — reviewer correction: the code does Y")
    assert "## Known BRE corrections" in text
    assert counts["verified"] == 1 and counts["incorrect"] == 1 and counts["suspected"] == 1
    # verbatim body kept
    assert "- Source: `A.b`.\nbody one" in text


JAVA_EV = {"kind": "range", "path": "src/main/java/x/A.java", "start": 10, "end": 13,
           "quote": "\t/** doc */\n\t@Override\n\tpublic void stop(Date max) {\n\t\tif (max == null) return;"}


def test_default_design_has_no_anchor_lines() -> None:
    review = _review(BR_1=rec("BR-1", "verified", evidence=[JAVA_EV]))
    text, _ = reviewed_rules(DOM, CLAIMS, review)
    head = text.split("## BR-1: First rule\n", 1)[1].split("\n")
    assert head[0].startswith("> Review: VERIFIED") and head[1] == "- Source: `A.b`."
    assert "> Anchors" not in text and "cite them without opening the code" not in text
    assert RULES_HEADER + "\n" in text


def test_verified_rule_gets_anchor_line() -> None:
    review = _review(BR_1=rec("BR-1", "verified", evidence=[JAVA_EV]),
                     BR_2=rec("BR-2", "needs-sme", note="ask", evidence=[JAVA_EV]))
    text, _ = reviewed_rules(DOM, CLAIMS, review, anchors=True)
    head = text.split("## BR-1: First rule\n", 1)[1].split("\n")
    assert head[0].startswith("> Review: VERIFIED") and "evidence: src/main/java/x/A.java:L10-L13" in head[0]
    assert head[1] == ANCHORS_PREFIX + "A.java:L12 `A.stop` `public void stop(Date max) {`"
    assert head[2] == "- Source: `A.b`."  # verbatim body follows the anchors
    # anchors only for VERIFIED rules
    br2 = text.split("## BR-2: Second rule\n", 1)[1].split("\n")
    assert br2[0].startswith("> Review: NEEDS SME") and not br2[1].startswith("> Anchors")
    assert "cite them without opening the code" in text


def test_anchor_line_quote_backticks_and_multiple_ranges() -> None:
    ev2 = {"kind": "range", "path": "lib/u.py", "start": 1, "end": 2, "quote": "def run(x):\n    return `x`"}
    search = {"kind": "search", "pattern": "x", "scope": "*.py", "hit_count": 0}
    review = _review(BR_1=rec("BR-1", "verified", evidence=[JAVA_EV, search, ev2]))
    line = anchors_line(review.claim("alpha/BR-1"))
    assert line == ANCHORS_PREFIX + "A.java:L12 `A.stop` `public void stop(Date max) {` · u.py:L1 `u.run` `def run(x):`"
    assert anchors_line(rec("BR-1", "verified", evidence=[search])) is None
    assert anchors_line(None) is None


def test_search_evidence_summary() -> None:
    ev = [{"kind": "search", "pattern": r"\block", "scope": "src/*.java", "hit_count": 0, "files_scanned": 2}]
    text, _ = reviewed_rules(DOM, CLAIMS, _review(BR_1=rec("BR-1", "verified", evidence=ev)))
    assert "evidence: search /\\block/ in src/*.java: 0 hits" in text


def test_unreviewed_flag_and_exclude() -> None:
    text, _ = reviewed_rules(DOM, CLAIMS, ReviewData())
    assert text.count("UNREVIEWED — treat as unconfirmed extraction") == 3
    text, counts = reviewed_rules(DOM, CLAIMS, _review(BR_1=rec("BR-1", "verified")), "exclude")
    assert "## BR-1" in text and "## BR-2" not in text and counts["excluded"] == 2


def test_stale_hash_downgrades(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        text, counts = reviewed_rules(DOM, CLAIMS, _review(BR_1=rec("BR-1", "verified", body_sha256="old")))
    assert counts["stale"] == ["alpha/BR-1"] and counts["verified"] == 0
    assert "stale" in caplog.text
    assert "## BR-1: First rule\n> Review: UNREVIEWED" in text


def test_no_rules() -> None:
    text, _ = reviewed_rules(DOM, [], ReviewData())
    assert NO_RULES in text


def test_needs_edit_renders_full_replacement_not_original() -> None:
    review = _review(BR_2=rec("BR-2", "needs-edit", replacement="## BR-2: Second rule\nfixed body\n## Inner heading\ntail",
                              what_changed="the body was wrong about X"))
    text, counts = reviewed_rules(DOM, CLAIMS, review)
    head = text.split("## BR-2: Second rule\n", 1)[1].split("\n")
    assert head[0].startswith("> Review: NEEDS EDIT (corrected text below) · AI-reviewed")
    assert head[1] == "> What changed: the body was wrong about X"
    assert head[2:5] == ["fixed body", "### Inner heading", "tail"]
    assert "body two" not in text  # the original body is only in the review record
    assert text.count("## BR-2:") == 1 and counts["needs-edit"] == 1
    assert "## Known BRE corrections" not in text


def test_legacy_incorrect_still_listed_as_correction() -> None:
    """A delta-only incorrect entry (old records) keeps rendering under Known BRE corrections."""
    text, counts = reviewed_rules(DOM, CLAIMS, _review(BR_3=rec("BR-3", "incorrect", correction="delta")))
    assert "## BR-3" not in text and "- BR-3: Third rule — reviewer correction: delta" in text
    assert "Each correction is itself a reviewed claim" in text and counts["incorrect"] == 1


def test_rules_header_wording() -> None:
    assert "verbatim" not in RULES_HEADER and "source of truth" not in RULES_HEADER
    for status in ("VERIFIED", "NEEDS EDIT", "NEEDS SME", "UNREVIEWED", "code is the truth"):
        assert status in RULES_HEADER
    text, _ = reviewed_rules(DOM, CLAIMS, ReviewData())
    assert text.split("\n")[1].startswith("> Trust: Tier 2")
