"""Tests for bcp.workbook: SME workbook export/import, effort estimate and the counterexample pass."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from bcp import cli, review, workbook
from bcp.config import BcpConfig
from bcp.errors import BcpInputError
from bcp.precheck import run_precheck
from bcp.render import reviewed_rules
from bcp.bre import BreLayout
from bcp.manifest import load_manifest
from conftest import load_json

CFG = BcpConfig()
KIRO = {"reviewer": "Kiro", "reviewer_type": "kiro", "config": CFG}


@pytest.fixture
def rev(tmp_path: Path, bre_mini: Path, repo_mini: Path) -> Path:
    out = tmp_path / "rev"
    review.prepare(bre_mini, repo_mini, out, ["alpha", "beta"], CFG)
    run_precheck(bre_mini, repo_mini, out, ["alpha", "beta"], CFG)
    for rule in ("BR-1", "BR-3", "BR-6"):
        review.record(out, f"alpha/{rule}", "verified", "ok", evidence=["src/main/java/x/AlphaService.java:7-14"],
                      repo=repo_mini, **KIRO)
    review.record(out, "alpha/BR-2", "incorrect", "wrong", evidence=["src/main/java/x/AlphaService.java:16-20"],
                  correction="purge checks another flag", repo=repo_mini, **KIRO)
    review.record(out, "beta/BR-1", "verified", "ok", evidence=["src/main/java/x/BetaService.java:5-10"],
                  repo=repo_mini, **KIRO)
    return out


def rows_of(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(workbook.COLUMNS), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8-sig")


def test_export_ranks_counterexample_and_guard_first(rev: Path, repo_mini: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbook, "_openpyxl", lambda: None)
    review.add_pass(rev, "alpha/BR-1", "counterexample-found", "the guard is skipped on self-call", "Kiro", "kiro",
                    ["src/main/java/x/AlphaService.java:22-24"], repo_mini)
    book = workbook.export_workbook(rev, None, "auto")
    assert [p.name for p in book.written] == ["sme-workbook.md", "sme-workbook.csv"]
    ranked = [r["claim_id"] for r in book.rows]
    # incorrect (5) and the counterexample (1 + 3) and the guard-contradicted beta/BR-6 (2 + >=3) lead
    assert set(ranked[:3]) == {"alpha/BR-2", "alpha/BR-1", "beta/BR-6"}
    rows = rows_of(rev / "sme-workbook.csv")
    assert list(rows[0]) == list(workbook.COLUMNS)
    assert rows[0]["rank"] == "1"
    br1 = next(r for r in rows if r["claim_id"] == "alpha/BR-1")
    assert br1["ai_verdict"] == "verified (counterexample found)"
    br6 = next(r for r in rows if r["claim_id"] == "beta/BR-6")
    assert "GUARD CONTRADICTED" in br6["precheck_flags"]
    md = (rev / "sme-workbook.md").read_text(encoding="utf-8")
    assert "## Effort estimate" in md and "calibrate on the first pilot domain" in md


def test_spot_check_sample_and_effort(rev: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbook, "_openpyxl", lambda: None)
    book = workbook.build_workbook(rev, ["alpha"])
    spots = [r for r in book.rows if r["_spot"]]
    assert len(spots) == 1  # 3 verified alpha claims → ceil(3/4) = 1
    assert all(r["ai_verdict"] == "verified" for r in spots)
    flagged = [r for r in book.rows if not r["_spot"]]
    assert {r["_verdict"] for r in flagged} <= {"incorrect", "needs-sme", "unreviewed", "needs-edit"}
    effort = book.effort[0]
    assert effort["domain"] == "alpha" and effort["to_confirm"] == len(book.rows) == effort["flagged"] + 1
    assert effort["minutes"] == sum(workbook._row_minutes(r) for r in book.rows)
    assert effort["minutes"] >= 6 + 15  # one spot check + the incorrect claim at least
    assert effort["ai_review_span"] and "min per 10 claims" in effort["ai_review_span"]


def test_csv_round_trip_imports_needs_edit(rev: Path, tmp_path: Path, bre_mini: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbook, "_openpyxl", lambda: None)
    workbook.export_workbook(rev, ["alpha"], "csv")
    rows = rows_of(rev / "sme-workbook.csv")
    row = next(r for r in rows if r["claim_id"] == "alpha/BR-2")
    row.update(sme_verdict="needs-edit", sme_replacement_text="## BR-2: x\nPurge throws when in use.\n## Detail\nmore",
               sme_what_changed="The message code is right; the flag name was wrong.", sme_notes="checked",
               sme_reviewer="Dana (SME)", sme_date="2026-02-03")
    edited = tmp_path / "edited.csv"
    write_rows(edited, rows)
    result = workbook.import_workbook(rev, edited)
    assert result.imported == ["alpha/BR-2"] and result.skipped == []
    entry = load_json(rev / "review-record.json")["claims"]["alpha/BR-2"]
    assert entry["verdict"] == "needs-edit" and entry["reviewer_type"] == "sme"
    assert entry["replacement"] == "Purge throws when in use.\n### Detail\nmore"
    assert entry["evidence"][0]["kind"] == "range" and entry["evidence"][-1] == {
        "kind": "sme", "reviewer": "Dana (SME)", "date": "2026-02-03", "note": "checked"}
    assert entry["history"][0]["verdict"] == "incorrect" and entry["history"][0]["reviewer"] == "Kiro"
    assert entry["reviewed_at"] == "2026-02-03T00:00:00Z"
    # the rendered rule shows the SME's text, not the original body
    manifest = load_manifest(bre_mini)
    claims = BreLayout(bre_mini).load_claims("alpha", CFG)[0]
    text, counts = reviewed_rules(manifest.domain("alpha"), claims, review.load_review_dir(rev))  # type: ignore[arg-type]
    assert "Purge throws when in use." in text and "Message code `Alpha.purge.inUse` is raised" not in text
    assert counts["needs-edit"] == 1


def test_stale_and_invalid_rows_are_skipped(rev: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbook, "_openpyxl", lambda: None)
    workbook.export_workbook(rev, ["alpha"], "csv")
    rows = rows_of(rev / "sme-workbook.csv")
    by_id = {r["claim_id"]: r for r in rows}
    by_id["alpha/BR-2"].update(sme_verdict="verified", sme_reviewer="Dana", sme_date="2026-02-03",
                               body_sha256="0" * 64)
    by_id["alpha/BR-4"].update(sme_verdict="verified", sme_date="2026-02-03")  # no reviewer
    by_id["alpha/BR-5"].update(sme_verdict="needs-sme", sme_reviewer="Dana", sme_date="yesterday")
    by_id["alpha/BR-7"].update(sme_verdict="incorrect", sme_reviewer="Dana", sme_date="2026-02-03")  # no notes
    edited = tmp_path / "edited.csv"
    write_rows(edited, list(by_id.values()))
    before = (rev / "review-record.json").read_text(encoding="utf-8")
    result = workbook.import_workbook(rev, edited)
    assert result.imported == []
    reasons = dict(result.skipped)
    assert any("stale" in r for r in reasons.values())
    assert any("sme_reviewer is required" in r for r in reasons.values())
    assert any("ISO date" in r for r in reasons.values())
    assert any("incorrect needs sme_notes" in r for r in reasons.values())
    assert (rev / "review-record.json").read_text(encoding="utf-8") == before


def test_dry_run_writes_nothing(rev: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbook, "_openpyxl", lambda: None)
    workbook.export_workbook(rev, ["alpha"], "csv")
    rows = rows_of(rev / "sme-workbook.csv")
    rows[0].update(sme_verdict="needs-sme", sme_reviewer="Dana", sme_date="2026-02-03", sme_notes="ask the owner")
    edited = tmp_path / "edited.csv"
    write_rows(edited, rows)
    before = (rev / "review-record.json").read_text(encoding="utf-8")
    result = workbook.import_workbook(rev, edited, dry_run=True)
    assert result.imported and result.dry_run
    assert (rev / "review-record.json").read_text(encoding="utf-8") == before


def test_xlsx_round_trip(rev: Path, tmp_path: Path) -> None:
    pytest.importorskip("openpyxl")
    book = workbook.export_workbook(rev, ["alpha"], "xlsx")
    assert book.written[1].name == "sme-workbook.xlsx"
    import openpyxl  # type: ignore[import-untyped]

    wb = openpyxl.load_workbook(book.written[1])
    ws = wb["claims"]
    header = [c.value for c in ws[1]]
    col = {name: i + 1 for i, name in enumerate(header)}
    ws.cell(row=2, column=col["sme_verdict"], value="needs-sme")
    ws.cell(row=2, column=col["sme_reviewer"], value="Dana")
    ws.cell(row=2, column=col["sme_date"], value="2026-02-03")
    edited = tmp_path / "edited.xlsx"
    wb.save(edited)
    assert len(workbook.import_workbook(rev, edited).imported) == 1


def test_export_neutralises_spreadsheet_formulas(rev: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbook, "_openpyxl", lambda: None)
    real = workbook.build_workbook

    def patched(out: Path, domains: list[str] | None = None) -> workbook.Workbook:
        book = real(out, domains)
        book.rows[0]["title"] = '=HYPERLINK("https://example.com/x","open")'
        book.rows[0]["ai_note"] = "@SUM(A1)"
        return book

    monkeypatch.setattr(workbook, "build_workbook", patched)
    workbook.export_workbook(rev, ["alpha"], "csv")
    rows = rows_of(rev / "sme-workbook.csv")
    assert rows[0]["title"] == '\'=HYPERLINK("https://example.com/x","open")' and rows[0]["ai_note"] == "'@SUM(A1)"
    assert not any(v.startswith(workbook.FORMULA_PREFIXES) for r in rows for v in r.values())
    assert workbook.spreadsheet_safe("plain text") == "plain text"
    assert workbook.spreadsheet_safe("-1") == "'-1" and workbook.spreadsheet_safe("\tx") == "'\tx"


def test_xlsx_without_openpyxl_is_an_input_error(rev: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbook, "_openpyxl", lambda: None)
    with pytest.raises(BcpInputError, match="openpyxl"):
        workbook.export_workbook(rev, None, "xlsx")


def test_counterexample_prepare_and_record(rev: Path, bre_mini: Path, repo_mini: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    written = workbook.prepare_counterexamples(bre_mini, rev, CFG, ["alpha", "beta"])
    assert [p.name for p in written] == ["alpha-BR-2.md"]
    text = written[0].read_text(encoding="utf-8")
    assert "## Correction (review)" in text and "purge checks another flag" in text
    assert "Message code `Alpha.purge.inUse` is raised." in text and "--outcome upheld|counterexample-found" in text
    tier3 = workbook.prepare_counterexamples(bre_mini, rev, CFG, ["alpha", "beta"], include_tier3=True)
    assert "beta-BR-6.md" in [p.name for p in tier3]  # authorization topic
    assert cli.main(["review", "counterexample", "record", "--out", str(rev), "--repo", str(repo_mini),
                     "--claim", "alpha/BR-2", "--outcome", "upheld", "--note", "correction holds",
                     "--evidence", "src/main/java/x/AlphaService.java:16-20", "--reviewer", "Kiro",
                     "--reviewer-type", "kiro"]) == 0
    entry = load_json(rev / "review-record.json")["claims"]["alpha/BR-2"]
    assert entry["passes"][0]["pass"] == "counterexample" and entry["passes"][0]["outcome"] == "upheld"
    assert entry["passes"][0]["evidence"][0]["start"] == 16
    assert cli.main(["review", "counterexample", "record", "--out", str(rev), "--claim", "alpha/BR-5",
                     "--outcome", "upheld", "--note", "n", "--reviewer", "K", "--reviewer-type", "kiro"]) == 2
    capsys.readouterr()


def test_second_pass_line_rendered(rev: Path, bre_mini: Path, repo_mini: Path) -> None:
    review.add_pass(rev, "alpha/BR-1", "counterexample-found", "self-call skips the check", "Kiro", "kiro")
    manifest = load_manifest(bre_mini)
    claims = BreLayout(bre_mini).load_claims("alpha", CFG)[0]
    text, _ = reviewed_rules(manifest.domain("alpha"), claims, review.load_review_dir(rev))  # type: ignore[arg-type]
    head = text.split("## BR-1: Saving an alpha requires the save privilege\n", 1)[1].split("\n")
    assert head[0].startswith("> Review: VERIFIED")
    assert head[1] == "> Second pass: counterexample found (self-call skips the check)"


def test_workbook_cli(rev: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbook, "_openpyxl", lambda: None)
    assert cli.main(["--json", "review", "workbook", "export", "--out", str(rev), "--domains", "alpha"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["rows"] > 0 and data["effort"][0]["domain"] == "alpha"
    assert cli.main(["review", "workbook", "import", "--out", str(rev), "--file",
                     str(rev / "sme-workbook.csv"), "--dry-run"]) == 0
    assert "would import 0 row(s)" in capsys.readouterr().out
