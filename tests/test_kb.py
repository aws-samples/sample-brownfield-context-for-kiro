"""Tests for bcp.kb (``bcp kb export``): the export filter, metadata size and the source hash."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from bcp import cli, kb
from bcp.claims import body_sha256
from bcp.config import BcpConfig

WIDGET = [f"    line {n} of widget" for n in range(1, 31)]
GADGET = [f"    line {n} of gadget" for n in range(1, 11)]
BR1_HEAD = "## BR-1: Widgets are saved after the check"
BR1_BODY = ["", "The check runs before the save.", "", "- Source: `WidgetService.save`"]

RULES_MD = "\n".join([
    "<!-- generated-by: test -->",
    "# Reviewed business rules: Alpha (`alpha`)",
    "",
    "Intro text.",
    "",
    BR1_HEAD,
    "> Review: VERIFIED, SUSPECTED DEFECT · AI-reviewed, not SME-signed · evidence: x",
    *BR1_BODY,
    "",
    "## BR-2: Gadgets are copied",
    "> Review: NEEDS EDIT (corrected text below) · AI stand-in for SME (Kiro), not SME-signed · evidence: x",
    "> What changed: copy, not move",
    "Gadgets are copied, then the original is voided.",
    "",
    "## BR-4: Something only an SME knows",
    "> Review: NEEDS SME (ask the owner) · AI-reviewed, not SME-signed · evidence: none",
    "",
    "Unconfirmed text.",
    "",
    "## BR-5: Nobody looked at this",
    "> Review: UNREVIEWED — treat as unconfirmed extraction · not reviewed · evidence: none",
    "",
    "Unconfirmed text.",
    "",
    "## BR-8: Verified but its text changed",
    "> Review: VERIFIED · AI-reviewed, not SME-signed · evidence: x",
    "",
    "Text that differs from the reviewed version.",
    "",
    "## Known BRE corrections",
    "",
    "- BR-6: Wrong rule — reviewer correction: it is the other way round.",
    "",
])


def _range(path: str, lines: list[str], start: int, end: int) -> dict[str, Any]:
    return {"kind": "range", "path": path, "start": start, "end": end, "quote": "\n".join(lines[start - 1:end]),
            "file_sha256": "0" * 64}


def _entry(verdict: str, evidence: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    base = {"verdict": verdict, "body_sha256": "f" * 64, "reviewer": "reviewer", "reviewer_type": "kiro",
            "reviewed_at": "2026-01-02T03:04:05Z", "note": "n", "correction": None, "suspected_defect": False,
            "evidence": evidence}
    base.update(extra)
    return base


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "src" / "app" / "WidgetService.java").write_text("\n".join(WIDGET) + "\n", encoding="utf-8")
    (repo / "src" / "app" / "Gadget.java").write_text("\n".join(GADGET) + "\n", encoding="utf-8")
    context = repo / ".kiro" / "context"
    (context / "domains" / "alpha").mkdir(parents=True)
    (context / "domains" / "alpha" / "reviewed-rules.md").write_text(RULES_MD, encoding="utf-8")
    (context / "file-index.json").write_text(json.dumps({"src/app/WidgetService.java": ["alpha"],
                                                         "src/app/Gadget.java": ["beta"]}), encoding="utf-8")
    w, g = "src/app/WidgetService.java", "src/app/Gadget.java"
    claims = {
        "alpha/BR-1": _entry("verified", [_range(w, WIDGET, 3, 5)], suspected_defect=True,
                             body_sha256=body_sha256("\n".join([BR1_HEAD] + BR1_BODY))),
        "alpha/BR-2": _entry("needs-edit", [_range(g, GADGET, 1, 2), _range(w, WIDGET, 10, 12)],
                             reviewer_type="kiro (AI stand-in for SME)",
                             replacement="## BR-2: Gadgets are copied\nGadgets are copied, then the original is voided.",
                             what_changed="copy, not move"),
        "alpha/BR-4": _entry("needs-sme", []),
        "alpha/BR-6": _entry("incorrect", [_range(w, WIDGET, 20, 21)], correction="the other way round"),
        "alpha/BR-7": _entry("verified", [_range(w, WIDGET, 1, 1)]),
        "alpha/BR-8": _entry("verified", [_range(w, WIDGET, 7, 8)]),
        "alpha/BR-9": {"verdict": "unreviewed", "body_sha256": "a" * 64, "evidence": []},
    }
    review = repo / ".kiro" / "bre-review"
    review.mkdir(parents=True)
    (review / "review-record.json").write_text(json.dumps({"schema": "bcp.review/3", "repo_head": "abc1234",
                                                           "claims": claims}), encoding="utf-8")
    return repo, tmp_path / "out"


def _audit(out: Path) -> dict[str, dict[str, Any]]:
    data = json.loads((out / kb.AUDIT_FILE).read_text(encoding="utf-8"))
    return {row["claim"]: row for row in data["rules"]}


# --------------------------------------------------------------------------- filter


@pytest.mark.parametrize(("entry", "reason"), [
    (None, kb.R_UNREVIEWED),
    ({"verdict": "unreviewed"}, kb.R_UNREVIEWED),
    ({"verdict": None}, kb.R_UNREVIEWED),
    ({"verdict": "incorrect", "correction": "x"}, kb.R_INCORRECT),
    ({"verdict": "needs-sme"}, kb.R_NEEDS_SME),
    ({"verdict": "needs-edit"}, kb.R_NO_REPLACEMENT),
    ({"verdict": "needs-edit", "replacement": None}, kb.R_NO_REPLACEMENT),
    ({"verdict": "needs-edit", "replacement": "   \n"}, kb.R_NO_REPLACEMENT),
    ({"verdict": "verified", "auto": True}, kb.R_AUTO),
    ({"verdict": "verified", "passes": [{"pass": "counterexample", "outcome": "counterexample-found"}]},  # nosec B105 - review-pass schema field, not a credential
     kb.R_COUNTEREXAMPLE),
    ({"verdict": "something-else"}, kb.R_UNKNOWN),
])
def test_filter_refuses(entry: dict[str, Any] | None, reason: str) -> None:
    ok, got, _detail = kb.record_filter(entry)
    assert not ok and got == reason


def test_filter_refuses_legacy_incorrect_with_replacement() -> None:
    upgraded = {"verdict": "needs-edit", "replacement": "full text"}
    assert kb.record_filter(upgraded, raw_verdict="incorrect")[:2] == (False, kb.R_INCORRECT)


@pytest.mark.parametrize("entry", [
    {"verdict": "verified"},
    {"verdict": "needs-edit", "replacement": "Full corrected rule."},
    {"verdict": "verified", "passes": [{"pass": "counterexample", "outcome": "upheld"}]},  # nosec B105 - review-pass schema field, not a credential
])
def test_filter_accepts(entry: dict[str, Any]) -> None:
    assert kb.record_filter(entry)[0]


def test_export_writes_only_reviewed_facts(workspace: tuple[Path, Path]) -> None:
    repo, out = workspace
    res = kb.export_facts(repo, out, BcpConfig(), domains=["alpha"])
    rules = {f.fact_id: f for f in res.facts if f.fact_type == "rule"}
    assert sorted(rules) == ["alpha-BR-1", "alpha-BR-2"]
    assert rules["alpha-BR-1"].status == "verified" and rules["alpha-BR-2"].status == "needs-edit"
    written = sorted(p.name for p in (out / "verified" / "alpha").iterdir())
    for name in ("alpha-BR-1.md", "alpha-BR-1.md.metadata.json", "alpha-BR-2.md", "alpha-BR-2.md.metadata.json"):
        assert name in written
    exported_text = "".join(p.read_text(encoding="utf-8") for p in (out / "verified").rglob("*") if p.is_file())
    assert "Unconfirmed text" not in exported_text
    assert "other way round" not in exported_text
    assert "differs from the reviewed version" not in exported_text
    for bad in ("BR-4", "BR-5", "BR-6", "BR-7", "BR-8", "BR-9"):
        assert not list((out / "verified").rglob(f"*-{bad}.md"))

    audit = _audit(out)
    assert set(audit) == {f"alpha/BR-{n}" for n in (1, 2, 4, 5, 6, 7, 8, 9)}
    reasons = {cid: row["reason"] for cid, row in audit.items()}
    assert reasons == {"alpha/BR-1": "exported", "alpha/BR-2": "exported", "alpha/BR-4": kb.R_NEEDS_SME,
                       "alpha/BR-5": kb.R_UNREVIEWED, "alpha/BR-6": kb.R_INCORRECT, "alpha/BR-7": kb.R_CONTEXT,
                       "alpha/BR-8": kb.R_STALE, "alpha/BR-9": kb.R_UNREVIEWED}
    assert [cid for cid, row in audit.items() if row["exported"]] == ["alpha/BR-1", "alpha/BR-2"]


def test_export_texts_and_metadata(workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    repo, out = workspace
    monkeypatch.setattr(kb, "git_head", lambda _repo: None)
    kb.export_facts(repo, out, BcpConfig(), domains=["alpha"])
    doc1 = (out / "verified" / "alpha" / "alpha-BR-1.md").read_text(encoding="utf-8")
    assert "The check runs before the save." in doc1 and "> Review:" not in doc1
    doc2 = (out / "verified" / "alpha" / "alpha-BR-2.md").read_text(encoding="utf-8")
    assert "Gadgets are copied, then the original is voided." in doc2 and "## BR-2" not in doc2
    meta = json.loads((out / "verified" / "alpha" / "alpha-BR-2.md.metadata.json").read_text(encoding="utf-8"))
    attrs = meta["metadataAttributes"]
    assert set(meta) == {"metadataAttributes"}
    assert attrs["fact_id"] == "alpha-BR-2" and attrs["domain"] == "alpha" and attrs["status"] == "needs-edit"
    assert attrs["components"] == ["Gadget", "WidgetService"]
    assert attrs["evidence"] == ["src/app/Gadget.java#L1-L2", "src/app/WidgetService.java#L10-L12"]
    assert attrs["reviewer_type"] == "kiro (AI stand-in for SME)"
    assert attrs["commit"] == "abc1234"  # no git HEAD: falls back to the record's repo_head
    feature = out / "verified" / "alpha" / "alpha-FEATURE-MAP.md"
    assert feature.is_file() and "alpha-BR-1" in feature.read_text(encoding="utf-8")
    assert (out / "verified" / "beta" / "GLOSSARY-Gadget.md").is_file()  # owner from the file index
    gloss = json.loads((out / "verified" / "alpha" / "GLOSSARY-WidgetService.md.metadata.json")
                       .read_text(encoding="utf-8"))["metadataAttributes"]
    assert gloss["evidence"] == ["src/app/WidgetService.java#L3-L5,L10-L12"]  # one spec per file, merged
    assert gloss["status"] == "derived" and gloss["fact_type"] == "glossary"


def test_export_removes_facts_that_are_no_longer_exported(workspace: tuple[Path, Path]) -> None:
    repo, out = workspace
    stale = out / "verified" / "alpha" / "alpha-BR-6.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    res = kb.export_facts(repo, out, BcpConfig(), domains=["alpha"])
    assert not stale.exists() and stale in res.removed


# --------------------------------------------------------------------------- metadata size


def test_all_metadata_files_fit(workspace: tuple[Path, Path]) -> None:
    repo, out = workspace
    kb.export_facts(repo, out, BcpConfig(), domains=["alpha"])
    metas = list((out / "verified").rglob("*.md.metadata.json"))
    assert metas
    for path in metas:
        assert len(path.read_bytes()) <= kb.METADATA_MAX_BYTES


def test_fit_metadata_truncates_deterministically() -> None:
    attrs = {"fact_id": "x-BR-1", "domain": "x", "status": "verified",
             "components": [f"Component{n:02d}" for n in range(60)],
             "evidence": [f"src/main/very/long/package/path/File{n:02d}.java#L1-L2,L10-L20" for n in range(30)],
             "source_hash": "0" * 64}
    first, cut = kb.fit_metadata(attrs)
    second, _ = kb.fit_metadata(attrs)
    assert cut and first == second
    assert len(kb.metadata_text(first).encode("utf-8")) <= kb.METADATA_MAX_BYTES
    assert first["truncated"] is True and first["evidence_total"] == 30 and first["components_total"] == 60
    kept = first["evidence"]
    assert kept[:-1] == attrs["evidence"][:len(kept) - 1]
    assert attrs["evidence"][len(kept) - 1].startswith(kept[-1])  # the last kept item may lose tail ranges
    assert first["components"] == attrs["components"][:len(first["components"])]


def test_fit_metadata_keeps_small_metadata_untouched() -> None:
    attrs = {"fact_id": "x", "components": ["A"], "evidence": ["a.java#L1-L2"]}
    fitted, cut = kb.fit_metadata(attrs)
    assert not cut and "truncated" not in fitted and fitted == attrs


def test_fit_metadata_trims_ranges_before_components() -> None:
    long_spec = "src/F.java#" + ",".join(f"L{n}-L{n + 1}" for n in range(1, 400, 2))
    attrs = {"fact_id": "x", "components": [f"C{n}" for n in range(10)], "evidence": [long_spec]}
    fitted, cut = kb.fit_metadata(attrs)
    assert cut and fitted["components"] == attrs["components"]
    assert long_spec.startswith(fitted["evidence"][0]) and fitted["evidence"][0].startswith("src/F.java#L1-L2")


# --------------------------------------------------------------------------- hash


def test_source_hash_is_sha256_of_cited_lines(workspace: tuple[Path, Path]) -> None:
    repo, out = workspace
    res = kb.export_facts(repo, out, BcpConfig(), domains=["alpha"])
    fact = next(f for f in res.facts if f.fact_id == "alpha-BR-2")
    expected = hashlib.sha256((
        "src/app/Gadget.java#L1-L2\n" + "\n".join(GADGET[0:2]) + "\n"
        + "src/app/WidgetService.java#L10-L12\n" + "\n".join(WIDGET[9:12]) + "\n"
    ).encode("utf-8")).hexdigest()
    assert fact.source_hash == expected
    meta = json.loads((out / "verified" / "alpha" / "alpha-BR-2.md.metadata.json").read_text(encoding="utf-8"))
    assert meta["metadataAttributes"]["source_hash"] == expected


def test_source_hash_ignores_line_endings(tmp_path: Path) -> None:
    (tmp_path / "A.java").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    items, changed = kb.cited_lines(tmp_path, [{"kind": "range", "path": "A.java", "start": 2, "end": 3,
                                                "quote": "two\nthree"}])
    assert not changed
    assert kb.source_hash(items) == hashlib.sha256(b"A.java#L2-L3\ntwo\nthree\n").hexdigest()


def test_content_hash_is_position_independent(workspace: tuple[Path, Path]) -> None:
    repo, out = workspace
    res = kb.export_facts(repo, out, BcpConfig(), domains=["alpha"])
    fact = next(f for f in res.facts if f.fact_id == "alpha-BR-1")
    assert fact.content_hash and fact.content_hash != fact.source_hash
    expected = kb.content_hash([("src/app/WidgetService.java#L3-L5", "\n".join(WIDGET[2:5]))])
    assert fact.content_hash == expected
    meta = json.loads((out / "verified" / "alpha" / "alpha-BR-1.md.metadata.json").read_text(encoding="utf-8"))
    assert meta["metadataAttributes"]["content_hash"] == expected
    assert "Content hash" in (out / "verified" / "alpha" / "alpha-BR-1.md").read_text(encoding="utf-8")


def test_content_hash_unchanged_when_lines_move() -> None:
    moved_a = kb.content_hash([("A.java#L3-L5", "one\ntwo\nthree")])
    moved_b = kb.content_hash([("A.java#L10-L12", "one\ntwo\nthree")])  # moved, same text
    assert moved_a == moved_b  # position-independent
    assert kb.source_hash([("A.java#L3-L5", "one\ntwo\nthree")]) != \
        kb.source_hash([("A.java#L10-L12", "one\ntwo\nthree")])  # source hash bakes in the line numbers


def test_content_hash_normalises_whitespace() -> None:
    # trailing whitespace stripped, runs of blank lines collapsed to one, leading/trailing blanks dropped
    a = kb.content_hash([("A.java#L1-L6", "\nfoo  \n\n\nbar   \n\n")])
    b = kb.content_hash([("A.java#L1-L3", "foo\n\nbar")])
    assert a == b
    assert kb.normalise_code("\nfoo  \n\n\nbar   \n\n") == "foo\n\nbar"


def test_metadata_without_content_hash_still_loads(workspace: tuple[Path, Path]) -> None:
    # a fact exported before 1.3.0 has no content_hash; attributes/document omit it and nothing breaks
    fact = kb.Fact(fact_id="x", fact_type="rule", domain="d", title="t", status="verified",
                   reviewer_type="kiro", components=["C"], evidence=["a.java#L1-L2"], source_hash="0" * 64,
                   body="b")
    assert "content_hash" not in fact.attributes("commit")
    assert "Content hash" not in fact.document("commit")


def test_changed_cited_code_is_not_exported(workspace: tuple[Path, Path]) -> None:
    repo, out = workspace
    lines = list(WIDGET)
    lines[3] = "    changed after the review"
    (repo / "src" / "app" / "WidgetService.java").write_text("\n".join(lines) + "\n", encoding="utf-8")
    kb.export_facts(repo, out, BcpConfig(), domains=["alpha"])
    audit = _audit(out)
    assert audit["alpha/BR-1"]["reason"] == kb.R_CODE_CHANGED and not audit["alpha/BR-1"]["exported"]
    assert audit["alpha/BR-2"]["exported"]  # its cited lines (10-12) are unchanged
    assert not (out / "verified" / "alpha" / "alpha-BR-1.md").exists()


# --------------------------------------------------------------------------- CLI


def test_cli_kb_export_json(workspace: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    repo, out = workspace
    code = cli.main(["kb", "export", "--repo", str(repo), "--out", str(out), "--domains", "alpha", "--json"])
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"]["alpha"]["verified"] == 1 and summary["counts"]["alpha"]["needs-edit"] == 1
    assert summary["skipped"][kb.R_INCORRECT] == 1 and summary["skipped"][kb.R_NEEDS_SME] == 1


def test_cli_kb_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.main(["kb"])
