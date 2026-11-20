"""Tests for bcp.drift (file, citation, evidence and unmapped findings; D2 investigations)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bcp import review
from bcp.config import BcpConfig
from bcp.drift import check_drift, derive_main_source_dirs, render_drift_md, write_reports
from bcp.errors import BcpInputError
from bcp.manifest import load_manifest

CFG = BcpConfig()


@pytest.fixture
def prepared(tmp_path: Path, bre_mini: Path, repo_copy: Path) -> tuple[Path, Path]:
    out = tmp_path / "review"
    review.prepare(bre_mini, repo_copy, out, ["alpha", "beta"], CFG)
    return out, repo_copy


def run(bre: Path, repo: Path, rev: Path | None, **kw):  # type: ignore[no-untyped-def]
    return check_drift(bre, repo, kw.pop("config", CFG), rev, use_git=False, **kw)


def find(report, kind: str, claim: str | None = None, token: str | None = None) -> list[dict]:  # type: ignore[no-untyped-def,type-arg]
    return [f for f in report.findings if f["kind"] == kind and (claim is None or f.get("claim") == claim)
            and (token is None or f.get("token") == token)]


def edit(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_unchanged_repo_has_no_high_citation(bre_mini: Path, prepared: tuple[Path, Path]) -> None:
    rev, repo = prepared
    report = run(bre_mini, repo, rev)
    assert report.high_citation() == 0
    pre = find(report, "pre-existing-unresolved", "alpha/BR-4")
    assert pre and pre[0]["severity"] == "INFO"
    assert pre[0]["investigation"]["outcome"] == "field-not-method"


def test_resolved_to_missing_is_high_with_investigation(bre_mini: Path, prepared: tuple[Path, Path]) -> None:
    rev, repo = prepared
    edit(repo / "src/main/java/x/AlphaService.java", "saveAlpha(", "saveAlphaRecord(")
    report = run(bre_mini, repo, rev)
    high = find(report, "unresolved-citation", "alpha/BR-1")
    assert high and high[0]["severity"] == "HIGH"
    inv = high[0]["investigation"]
    assert inv["outcome"] == "renamed" and inv["candidates"][0]["path"] == "src/main/java/x/AlphaService.java"
    assert "Why not found:" in high[0]["message"]
    assert inv["checks"][5]["step"] == 6


def test_method_missing_then_class_deleted_is_high(bre_mini: Path, prepared: tuple[Path, Path]) -> None:
    rev, repo = prepared
    (repo / "src/main/java/x/Alpha.java").unlink()
    report = run(bre_mini, repo, rev)
    high = find(report, "unresolved-citation", "alpha/BR-4")
    assert high and high[0]["baseline"] == "method-missing" and high[0]["current"] == "unresolved"
    assert find(report, "missing-file")[0]["severity"] == "HIGH"


def test_n2_unresolved_then_method_missing_is_info(bre_mini: Path, prepared: tuple[Path, Path]) -> None:
    rev, repo = prepared
    (repo / "src/main/java/x/ZetaEngine.java").write_text("class ZetaEngine {\n}\n", encoding="utf-8")
    report = run(bre_mini, repo, rev)
    pre = find(report, "pre-existing-unresolved", "beta/BR-4")
    assert pre and pre[0]["severity"] == "INFO" and "class now exists, method still missing" in pre[0]["message"]
    assert not find(report, "unresolved-citation", "beta/BR-4")


def test_now_resolves(bre_mini: Path, prepared: tuple[Path, Path]) -> None:
    rev, repo = prepared
    (repo / "src/main/java/x/ZetaEngine.java").write_text("class ZetaEngine {\n  void ignite() {}\n}\n",
                                                          encoding="utf-8")
    report = run(bre_mini, repo, rev)
    assert find(report, "citation-now-resolves", "beta/BR-4")[0]["severity"] == "INFO"


def test_no_baseline(bre_mini: Path, repo_copy: Path) -> None:
    report = run(bre_mini, repo_copy, None)
    nob = find(report, "no-baseline-unresolved")
    assert nob and all(f["severity"] == "INFO" for f in nob)
    assert {f["investigation"]["outcome"] for f in nob} >= {"deleted", "renamed", "moved", "field-not-method"}
    assert report.baseline_source == "none"


def test_worksheet_precedence_over_baseline(bre_mini: Path, prepared: tuple[Path, Path], tmp_path: Path) -> None:
    rev, repo = prepared
    fake = {"schema": "bcp.citations/1", "repo_head": None, "claims": {"alpha/BR-4": {
        "body_sha256": "x", "recorded_at": "t",
        "tokens": [{"token": "Alpha.status", "kind": "class", "class": "Alpha", "method": "status",  # nosec B105 - citation token, not a credential
                    "bound": False, "status": "resolved", "path": "src/main/java/x/Alpha.java", "line": 7}]}}}
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps(fake), encoding="utf-8")
    with_sheet = run(bre_mini, repo, rev, baseline=base)
    assert not find(with_sheet, "unresolved-citation", "alpha/BR-4")
    only_base = run(bre_mini, repo, None, baseline=base)
    assert find(only_base, "unresolved-citation", "alpha/BR-4")


def test_invalid_baseline(bre_mini: Path, repo_copy: Path, tmp_path: Path) -> None:
    bad = tmp_path / "b.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(BcpInputError) as err:
        run(bre_mini, repo_copy, None, baseline=bad)
    assert err.value.exit_code == 2
    bad.write_text(json.dumps({"schema": "other"}), encoding="utf-8")
    with pytest.raises(BcpInputError):
        run(bre_mini, repo_copy, None, baseline=bad)


def test_file_and_evidence_findings(bre_mini: Path, prepared: tuple[Path, Path]) -> None:
    rev, repo = prepared
    review.record(rev, "alpha/BR-2", "verified", "throws", "Kiro", "kiro", CFG,
                  evidence=["src/main/java/x/AlphaService.java:16-20"], repo=repo)
    review.record(rev, "alpha/BR-1", "verified", "no lock", "Kiro", "kiro", CFG,
                  searches=[(r"\block\w*\s*\(", "src/main/java/x/*Service.java")], repo=repo)
    review.record(rev, "beta/BR-1", "verified", "limit", "Kiro", "kiro", CFG,
                  searches=[("parseInt", "src/main/java/x/BetaService.java")], repo=repo)
    review.record(rev, "beta/BR-2", "verified", "orders", "Kiro", "kiro", CFG,
                  evidence=["app/services/orders.py:4-7"], repo=repo)
    svc = repo / "src/main/java/x/AlphaService.java"
    svc.write_text("// header\n// header\n" + svc.read_text(encoding="utf-8")
                   + "\nclass Extra { void lockAll() {} }\n" + "\n".join("//" for _ in range(20)), encoding="utf-8")
    beta = repo / "src/main/java/x/BetaService.java"
    beta.write_text(beta.read_text(encoding="utf-8") + "\n// comment\n", encoding="utf-8")
    edit(repo / "app/services/orders.py", 'raise ValueError("order required")', "return None")
    (repo / "lib/util.py").unlink()
    report = run(bre_mini, repo, rev)
    moved = find(report, "evidence-moved", "alpha/BR-2")
    assert moved[0]["severity"] == "MEDIUM" and moved[0]["line"] == 18
    assert find(report, "evidence-moved", "beta/BR-2")[0]["severity"] == "HIGH"
    assert find(report, "evidence-changed", "alpha/BR-1")[0]["severity"] == "HIGH"
    assert find(report, "evidence-changed", "beta/BR-1")[0]["severity"] == "INFO"
    assert find(report, "missing-file")[0]["path"] == "lib/util.py"
    assert find(report, "loc-changed")[0]["path"] == "src/main/java/x/AlphaService.java"
    out = write_reports(report, rev / "drift")
    assert all(p.is_file() for p in out)
    assert "## alpha" in render_drift_md(report)


def test_main_source_dirs_derived_and_override(bre_mini: Path, repo_copy: Path) -> None:
    assert derive_main_source_dirs(load_manifest(bre_mini)) == ["app/services", "lib", "src/main/java"]
    report = run(bre_mini, repo_copy, None)
    paths = sorted(f["path"] for f in find(report, "unmapped-file"))
    # the GuardedApi fixtures (precheck guard tests) are deliberately not in the manifest
    assert paths == ["app/services/sub/x_handler.py", "lib/unmapped.py", "src/main/java/x/GuardedApi.java",
                     "src/main/java/x/GuardedApiImpl.java"]
    assert all(f["severity"] == "LOW" for f in find(report, "unmapped-file"))
    report = run(bre_mini, repo_copy, None, config=BcpConfig(main_source_dirs=["lib"]))
    assert [f["path"] for f in find(report, "unmapped-file")] == ["lib/unmapped.py"]


def test_git_step_reports_unavailable(bre_mini: Path, prepared: tuple[Path, Path]) -> None:
    rev, repo = prepared
    edit(repo / "src/main/java/x/AlphaService.java", "saveAlpha(", "keepAlpha(")
    report = check_drift(bre_mini, repo, CFG, rev, use_git=True)
    inv = find(report, "unresolved-citation", "alpha/BR-1")[0]["investigation"]
    assert "git history not available" in inv["checks"][5]["result"]
