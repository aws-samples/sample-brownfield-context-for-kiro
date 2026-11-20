"""Tests for bcp.review (step 2 tooling) including the D2 needs-SME fallback."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bcp import review
from bcp.config import BcpConfig
from bcp.errors import BcpInputError, ReviewRecordError
from conftest import load_json

CFG = BcpConfig()
REVIEWER = {"reviewer": "Kiro (test)", "reviewer_type": "kiro", "config": CFG}


@pytest.fixture
def prepared(tmp_path: Path, bre_mini: Path, repo_copy: Path) -> tuple[Path, Path]:
    out = tmp_path / "review"
    review.prepare(bre_mini, repo_copy, out, ["alpha", "beta"], CFG)
    return out, repo_copy


def test_prepare_writes_worksheets_and_record(prepared: tuple[Path, Path]) -> None:
    out, _ = prepared
    sheet = load_json(out / "claims-alpha.json")
    assert sheet["schema"] == "bcp.claims/1"
    assert sheet["prepared_at"].endswith("Z")
    assert sheet["repo_head"] is None  # not a git repo
    assert len(sheet["claims"]) == 8
    rec = load_json(out / "review-record.json")
    assert rec["schema"] == "bcp.review/3" and rec["bre_root"] == "bre-mini"
    assert rec["claims"]["alpha/BR-1"]["verdict"] == "unreviewed"


def test_citation_counts_tally_every_claim(prepared: tuple[Path, Path]) -> None:
    """``review prepare`` reports the citation triage; the three buckets cover every claim."""
    out, _ = prepared
    counts = review.citation_counts(out)
    total = len(load_json(out / "claims-alpha.json")["claims"]) + len(load_json(out / "claims-beta.json")["claims"])
    assert set(counts) == {"verified-citation", "needs-sme", "no-citation"}
    assert sum(counts.values()) == total
    assert counts["needs-sme"] >= 1  # the fixture has unfound citations
    alpha_only = review.citation_counts(out, ["alpha"])
    assert sum(alpha_only.values()) == len(load_json(out / "claims-alpha.json")["claims"])


def test_unfound_citation_falls_back_to_needs_sme(prepared: tuple[Path, Path]) -> None:
    """D2(a): unfound citations make the claim needs-SME, never verified or incorrect."""
    out, _ = prepared
    rec = load_json(out / "review-record.json")["claims"]
    sheet = {c["rule_id"]: c for c in load_json(out / "claims-alpha.json")["claims"]}
    assert sheet["BR-4"]["fallback_verdict"] == "needs-sme"
    assert sheet["BR-4"]["investigations"][0]["outcome"] == "field-not-method"
    assert rec["alpha/BR-4"]["verdict"] == "needs-sme"
    assert rec["alpha/BR-4"]["auto"] is True
    assert "Why not found" in rec["alpha/BR-4"]["note"]
    assert rec["alpha/BR-7"]["verdict"] == "needs-sme"  # moved
    beta = load_json(out / "review-record.json")["claims"]
    assert beta["beta/BR-3"]["verdict"] == "needs-sme"  # renamed
    assert beta["beta/BR-4"]["verdict"] == "needs-sme"  # deleted
    assert beta["beta/BR-5"]["verdict"] == "unreviewed"  # non-code → text only
    for entry in rec.values():
        assert entry["verdict"] in ("unreviewed", "needs-sme")


def test_reviewer_overrides_fallback_and_prepare_keeps_it(prepared: tuple[Path, Path], bre_mini: Path) -> None:
    out, repo = prepared
    review.record(out, "alpha/BR-4", "incorrect", "status is a field", evidence=["src/main/java/x/Alpha.java:7-7"],
                  correction="Alpha.status is a field", repo=repo, **REVIEWER)
    review.prepare(bre_mini, repo, out, ["alpha"], CFG)
    assert load_json(out / "review-record.json")["claims"]["alpha/BR-4"]["verdict"] == "incorrect"


def test_record_range_captures_quote(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    entry = review.record(out, "alpha/BR-2", "verified", "throws when in use",
                          evidence=["src/main/java/x/AlphaService.java:16-20"], repo=repo, **REVIEWER)
    ev = entry["evidence"][0]
    assert ev["kind"] == "range" and ev["start"] == 16 and ev["end"] == 20
    assert 'throw new APIException("Alpha.purge.inUse");' in ev["quote"]
    assert len(ev["file_sha256"]) == 64
    text = (out / "review-record.json").read_text(encoding="utf-8")
    assert str(repo) not in text and str(repo).replace("\\", "/") not in text


def test_record_search_zero_and_nonzero(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    entry = review.record(out, "alpha/BR-1", "verified", "no lock methods",
                          searches=[(r"\block\w*\s*\(", "src/main/java/x/*Service.java"),
                                    (r"saveAlpha", "src/main/java/x/AlphaService.java")],
                          repo=repo, **REVIEWER)
    zero, some = entry["evidence"]
    assert zero["hit_count"] == 0 and zero["files_scanned"] == 2
    assert sorted(zero["files_sha256"]) == ["src/main/java/x/AlphaService.java", "src/main/java/x/BetaService.java"]
    assert some["hit_count"] == 3 and some["hits"][0]["line"] == 7


def test_search_hits_capped_and_scope_limits(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    (repo / "lib" / "many.py").write_text("\n".join(f"x{i} = 1" for i in range(30)), encoding="utf-8")
    entry = review.record(out, "alpha/BR-1", "verified", "n", searches=[(r"^x\d", "lib/many.py")], repo=repo,
                          **REVIEWER)
    assert entry["evidence"][0]["hit_count"] == 30 and len(entry["evidence"][0]["hits"]) == 20
    for i in range(201):
        (repo / "lib" / f"gen{i}.py").write_text("", encoding="utf-8")
    with pytest.raises(BcpInputError, match="narrow the scope"):
        review.record(out, "alpha/BR-1", "verified", "n", searches=[("x", "lib/gen*.py")], repo=repo, **REVIEWER)
    with pytest.raises(BcpInputError, match="matched no files"):
        review.record(out, "alpha/BR-1", "verified", "n", searches=[("x", "nope/*.py")], repo=repo, **REVIEWER)


def test_scope_star_crosses_directories(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    entry = review.record(out, "alpha/BR-1", "needs-sme", "n", searches=[("def", "app/*handler*.py")], repo=repo,
                          **REVIEWER)
    assert list(entry["evidence"][0]["files_sha256"]) == ["app/services/sub/x_handler.py"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"evidence": ["src/main/java/x/Alpha.java:5-1"]}, "outside"),
        ({"evidence": ["src/main/java/x/Alpha.java:1-99"]}, "outside"),
        ({"evidence": ["../outside.java:1-2"]}, "unsafe"),
        ({"evidence": ["src/main/java/x/Nope.java:1-2"]}, "not found"),
        ({"searches": [("(", "lib/util.py")]}, "invalid search regex"),
        ({}, "needs at least one"),
    ],
)
def test_record_rejects(prepared: tuple[Path, Path], kwargs: dict, match: str) -> None:  # type: ignore[type-arg]
    out, repo = prepared
    before = (out / "review-record.json").read_text(encoding="utf-8")
    with pytest.raises(BcpInputError, match=match):
        review.record(out, "alpha/BR-1", "verified", "n", repo=repo, **REVIEWER, **kwargs)
    assert (out / "review-record.json").read_text(encoding="utf-8") == before


def test_record_rejects_long_range_missing_repo_and_correction(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    (repo / "lib" / "long.py").write_text("\n".join("pass" for _ in range(60)), encoding="utf-8")
    with pytest.raises(BcpInputError, match="exceeds 40"):
        review.record(out, "alpha/BR-1", "verified", "n", evidence=["lib/long.py:1-41"], repo=repo, **REVIEWER)
    with pytest.raises(BcpInputError, match="--repo"):
        review.record(out, "alpha/BR-1", "verified", "n", evidence=["lib/long.py:1-2"], **REVIEWER)
    with pytest.raises(BcpInputError, match="correction"):
        review.record(out, "alpha/BR-1", "incorrect", "n", evidence=["lib/long.py:1-2"], repo=repo, **REVIEWER)
    with pytest.raises(BcpInputError, match="note"):
        review.record(out, "alpha/BR-1", "needs-sme", " ", **REVIEWER)


def test_schema_errors(tmp_path: Path) -> None:
    bad = {"schema": "bcp.review/2", "claims": {"alpha/BR-1": {"verdict": "maybe"}}}
    with pytest.raises(ReviewRecordError, match=r'\$\.claims\["alpha/BR-1"\]\.verdict'):
        review.validate_record(bad)
    with pytest.raises(ReviewRecordError, match="needs evidence"):
        review.validate_record({"schema": "bcp.review/2", "claims": {
            "alpha/BR-1": {"verdict": "verified", "reviewer_type": "kiro", "body_sha256": "x", "evidence": []}}})
    with pytest.raises(ReviewRecordError, match="claim id"):
        review.validate_record({"schema": "bcp.review/2", "claims": {"bad id": {"verdict": "unreviewed"}}})
    (tmp_path / "review-record.json").write_text("{", encoding="utf-8")
    with pytest.raises(ReviewRecordError):
        review.load_review_dir(tmp_path)


def test_render_report_and_checklist(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    review.record(out, "alpha/BR-2", "verified", "throws", evidence=["src/main/java/x/AlphaService.java:16-20"],
                  repo=repo, **REVIEWER)
    review.record(out, "alpha/BR-1", "verified", "none", searches=[("lock", "src/main/java/x/*Service.java")],
                  repo=repo, **REVIEWER)
    review.render(out, repo)
    report = (out / "review-report-alpha.md").read_text(encoding="utf-8")
    rel = os.path.relpath(repo / "src/main/java/x/AlphaService.java", out).replace("\\", "/")
    assert f"]({rel}#L16)" in report
    assert "```java" in report
    assert "Files scanned:" in report and "src/main/java/x/BetaService.java" in report
    assert review.KIRO_ONLY_NOTICE in report
    assert "Review span: " in report and "min per 10 claims" in report
    assert "Why not found: `Alpha.status` is a field" in report
    assert "Alpha.java#L7](" in report
    assert "## Claims needing SME attention" in report
    checklist = (out / "signoff-checklist-alpha.md").read_text(encoding="utf-8")
    for key in review.CHECKLIST_KEYS:
        assert f"- [ ] {key}: " in checklist
    assert "## Citations not found" in checklist and "Why not found:" in checklist
    assert "GammaHelper.java#L5" in checklist


def test_report_shows_automated_citation_check(prepared: tuple[Path, Path]) -> None:
    """MVP: found citations show verified-citation, unfound ones needs-sme, per claim and in the header."""
    out, repo = prepared
    sheet = {c["rule_id"]: c for c in load_json(out / "claims-alpha.json")["claims"]}
    assert review.citation_check(sheet["BR-2"]) == "verified-citation"
    assert review.citation_check(sheet["BR-4"]) == "needs-sme"
    assert review.citation_check({"candidates": [], "investigations": []}) == "no-citation"
    review.render(out, repo)
    report = (out / "review-report-alpha.md").read_text(encoding="utf-8")
    assert "- Citation check (automated): verified-citation " in report
    br4 = report.split("### BR-4:")[1].split("### ")[0]
    assert "Citation check: needs-sme" in br4
    br2 = report.split("### BR-2:")[1].split("### ")[0]
    assert "Citation check: verified-citation" in br2


def test_search_count_line_over_20(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    for i in range(25):
        (repo / "lib" / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    review.record(out, "alpha/BR-1", "verified", "n", searches=[("x", "lib/m*.py")], repo=repo, **REVIEWER)
    review.render(out, repo)
    assert "Files scanned: 25 (listed in review-record.json)" in (out / "review-report-alpha.md").read_text(
        encoding="utf-8")


def test_review_span_omitted_without_timestamps() -> None:
    sheet = {"prepared_at": None, "claims": [{"id": "a/BR-1"}]}
    assert review.review_span(sheet, {"a/BR-1": {"verdict": "verified", "reviewed_at": "2026-01-01T00:00:00Z"}}) is None
    sheet["prepared_at"] = "2026-01-01T00:00:00Z"
    span = review.review_span(sheet, {"a/BR-1": {"verdict": "verified", "reviewed_at": "2026-01-01T00:10:00Z"}})
    assert span == ("Review span: 2026-01-01T00:00:00Z → 2026-01-01T00:10:00Z "
                    "(10.0 min for 1 claims, 100.0 min per 10 claims)")


def test_file_uri_fallback(prepared: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    out, repo = prepared
    review.record(out, "alpha/BR-2", "verified", "t", evidence=["src/main/java/x/AlphaService.java:16-17"],
                  repo=repo, **REVIEWER)

    def boom(*_a: object, **_k: object) -> str:
        raise ValueError("different drive")

    monkeypatch.setattr(review.os.path, "relpath", boom)
    review.render(out, repo)
    assert "file:///" in (out / "review-report-alpha.md").read_text(encoding="utf-8")


def test_signoff_round_trip_and_unknown_key(prepared: tuple[Path, Path], tmp_path: Path) -> None:
    out, repo = prepared
    review.render(out, repo)
    text = (out / "signoff-checklist-alpha.md").read_text(encoding="utf-8")
    filled = tmp_path / "filled.md"
    filled.write_text(text.replace("- [ ] all_claims_have_verdict", "- [x] all_claims_have_verdict"), encoding="utf-8")
    entry = review.signoff(out, "alpha", "Kiro", "Demo reviewer", "kiro", "approved-with-exceptions",
                           ["BR-4"], filled)
    assert entry["checklist"]["all_claims_have_verdict"] is True
    assert entry["checklist"]["evidence_checked"] is False
    data = load_json(out / "signoff.json")
    assert data["schema"] == "bcp.signoff/1" and data["domains"]["alpha"]["exceptions"] == ["BR-4"]
    filled.write_text("- [x] made_up_key: hi\n", encoding="utf-8")
    with pytest.raises(ReviewRecordError) as err:
        review.signoff(out, "alpha", "Kiro", "r", "kiro", "approved", None, filled)
    assert err.value.exit_code == 3
    loaded = review.load_review_dir(out)
    assert loaded.present and loaded.domain_signoff("alpha")["decision"] == "approved-with-exceptions"


def test_needs_edit_requires_replacement_and_what_changed(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    ev = ["src/main/java/x/AlphaService.java:16-20"]
    with pytest.raises(BcpInputError, match="full corrected rule"):
        review.record(out, "alpha/BR-2", "needs-edit", "n", evidence=ev, repo=repo, what_changed="x", **REVIEWER)
    with pytest.raises(BcpInputError, match="--what-changed"):
        review.record(out, "alpha/BR-2", "needs-edit", "n", evidence=ev, repo=repo, replacement="body", **REVIEWER)
    with pytest.raises(BcpInputError, match="needs at least one"):
        review.record(out, "alpha/BR-2", "needs-edit", "n", replacement="b", what_changed="w", **REVIEWER)
    with pytest.raises(BcpInputError, match="superseded-pattern"):
        review.record(out, "alpha/BR-2", "needs-edit", "n", evidence=ev, repo=repo, replacement="b",
                      what_changed="w", superseded_patterns=["("], **REVIEWER)
    entry = review.record(out, "alpha/BR-2", "needs-edit", "n", evidence=ev, repo=repo,
                          replacement="## BR-2: An alpha in use cannot be purged\nPurge throws.\n## Inner\nx",
                          what_changed="  the  flag  differs ", superseded_patterns=["in use"], **REVIEWER)
    assert entry["replacement"] == "Purge throws.\n### Inner\nx"
    assert entry["what_changed"] == "the flag differs" and entry["superseded_patterns"] == ["in use"]
    with pytest.raises(ReviewRecordError, match="required for needs-edit"):
        review.validate_record({"schema": "bcp.review/3", "claims": {"alpha/BR-1": {
            "verdict": "needs-edit", "reviewer_type": "kiro", "body_sha256": "x",
            "evidence": [{"kind": "search", "pattern": "p", "scope": "s", "hit_count": 0, "files_scanned": 1}]}}})


def test_history_keeps_prior_entry(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    ev = ["src/main/java/x/AlphaService.java:16-20"]
    review.record(out, "alpha/BR-2", "incorrect", "first", evidence=ev, correction="c1", repo=repo, **REVIEWER)
    review.record(out, "alpha/BR-2", "verified", "second", evidence=ev, repo=repo, **REVIEWER)
    entry = review.record(out, "alpha/BR-2", "needs-sme", "third", **REVIEWER)
    assert [h["note"] for h in entry["history"]] == ["first", "second"]
    assert all("history" not in h for h in entry["history"])
    assert load_json(out / "review-record.json")["schema"] == "bcp.review/3"


V2_RECORD = {"schema": "bcp.review/2", "repo_head": None, "bre_root": "b", "claims": {
    "alpha/BR-1": {"verdict": "incorrect", "body_sha256": "s1", "reviewer": "K", "reviewer_type": "kiro",
                   "reviewed_at": "2026-01-01T00:00:00Z", "note": "n", "correction": "delta only",
                   "suspected_defect": False,
                   "evidence": [{"kind": "range", "path": "a.java", "start": 1, "end": 2, "quote": "q"}]},
    "alpha/BR-2": {"verdict": "verified", "body_sha256": "s2", "reviewer": "K", "reviewer_type": "kiro",
                   "reviewed_at": "2026-01-01T00:00:00Z", "note": "n", "correction": None, "suspected_defect": False,
                   "evidence": [{"kind": "range", "path": "a.java", "start": 1, "end": 2, "quote": "q"}]},
    "alpha/BR-3": {"verdict": "unreviewed", "body_sha256": "s3", "evidence": []},
}}


def test_v2_record_loads_unchanged(tmp_path: Path) -> None:
    import copy

    path = tmp_path / "review-record.json"
    path.write_text(json.dumps(V2_RECORD), encoding="utf-8")
    loaded = review.load_review_record(path)
    assert loaded["claims"] == copy.deepcopy(V2_RECORD)["claims"]  # delta-only incorrect stays incorrect


def test_old_incorrect_with_full_replacement_maps_to_needs_edit() -> None:
    entry = dict(V2_RECORD["claims"]["alpha/BR-1"], replacement="Full corrected rule.")  # type: ignore[arg-type]
    out = review.normalize_entry(entry)
    assert out["verdict"] == "needs-edit" and out["what_changed"] == "delta only"
    assert entry["verdict"] == "incorrect"  # the input is not mutated
    assert review.normalize_entry(dict(entry, replacement="  "))["verdict"] == "incorrect"
    data = {"schema": "bcp.review/2", "claims": {"alpha/BR-1": entry}}
    assert review.validate_record(data)["claims"]["alpha/BR-1"]["verdict"] == "needs-edit"


def test_standin_is_not_sme() -> None:
    entry = dict(V2_RECORD["claims"]["alpha/BR-2"], reviewer_type=review.STANDIN)  # type: ignore[arg-type]
    assert review.STANDIN == "kiro (AI stand-in for SME)" and review.STANDIN in review.REVIEWER_TYPES
    verdict, who = review.effective_status(entry, None, "BR-2", "s2")
    assert verdict == "verified" and who == "AI stand-in for SME (Kiro), not SME-signed (review not signed off)"
    so = {"reviewer": "Bot", "role": "r", "reviewer_type": review.STANDIN, "date": "2026-01-02",
          "decision": "approved", "exceptions": []}
    assert review.effective_status(entry, so, "BR-2", "s2")[1] == "AI stand-in for SME (Kiro), not SME-signed"
    assert "SME-signed by" not in review.effective_status(entry, so, "BR-2", "s2")[1]


def test_standin_report_keeps_kiro_only_notice(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    review.record(out, "alpha/BR-2", "verified", "ok", evidence=["src/main/java/x/AlphaService.java:16-20"],
                  repo=repo, reviewer="Kiro", reviewer_type=review.STANDIN, config=CFG)
    review.render(out, repo)
    report = (out / "review-report-alpha.md").read_text(encoding="utf-8")
    assert review.KIRO_ONLY_NOTICE in report and "needs-edit 0" in report


def test_sme_evidence_alone_needs_sme_reviewer() -> None:
    sme_ev = [{"kind": "sme", "reviewer": "Dana", "date": "2026-02-03", "note": ""}]
    base = {"verdict": "verified", "body_sha256": "x", "evidence": sme_ev}
    review.validate_record({"schema": "bcp.review/3", "claims": {"a/BR-1": dict(base, reviewer_type="sme")}})
    with pytest.raises(ReviewRecordError, match="SME evidence alone"):
        review.validate_record({"schema": "bcp.review/3", "claims": {"a/BR-1": dict(base, reviewer_type="kiro")}})
    with pytest.raises(ReviewRecordError, match="date"):
        review.validate_record({"schema": "bcp.review/3", "claims": {"a/BR-1": dict(
            base, reviewer_type="sme", evidence=[{"kind": "sme", "reviewer": "Dana"}])}})


def test_needs_edit_report_and_checklist(prepared: tuple[Path, Path]) -> None:
    out, repo = prepared
    review.record(out, "alpha/BR-2", "needs-edit", "n", evidence=["src/main/java/x/AlphaService.java:16-20"],
                  repo=repo, replacement="Purge throws when in use.", what_changed="w", **REVIEWER)
    review.render(out, repo)
    report = (out / "review-report-alpha.md").read_text(encoding="utf-8")
    assert "Verdict: NEEDS-EDIT" in report and "What changed: w" in report and "Purge throws when in use." in report
    checklist = (out / "signoff-checklist-alpha.md").read_text(encoding="utf-8")
    assert "needs-edit 1" in checklist
    assert "Every incorrect or needs-edit claim states what the code actually does." in checklist


def test_load_review_dir_empty_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    data = review.load_review_dir(tmp_path)
    assert not data.present and "all claims unreviewed" in caplog.text
    assert json.dumps(data.record)
