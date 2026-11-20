"""Tests for bcp.errata and the errata.md wiring in generate."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from bcp import review
from bcp.bre import BreLayout
from bcp.config import BcpConfig
from bcp.errata import EMPTY, build_errata, render_errata_md
from bcp.generate import generate
from bcp.precheck import run_precheck
from bcp.review import ReviewData
from bcp.validate import validate

CFG = BcpConfig()


def _claims(bre: Path) -> dict[str, Any]:
    layout = BreLayout(bre)
    return {slug: layout.load_claims(slug, CFG)[0] for slug in ("alpha", "beta")}


def _entry(sha: str, verdict: str, **extra: Any) -> dict[str, Any]:
    data = {"verdict": verdict, "body_sha256": sha, "reviewer": "Kiro", "reviewer_type": "kiro",
            "reviewed_at": "2026-01-01T00:00:00Z", "note": "n", "correction": None, "suspected_defect": False,
            "evidence": [{"kind": "range", "path": "a.java", "start": 1, "end": 2, "quote": "q"}]}
    data.update(extra)
    return data


def _review(bre: Path) -> ReviewData:
    claims = _claims(bre)
    sha = {f"{s}/{c.rule_id}": c.body_sha256 for s, cs in claims.items() for c in cs}
    return ReviewData(record={"claims": {
        "beta/BR-6": _entry(sha["beta/BR-6"], "needs-edit", replacement="The save is guarded on the interface.",
                            what_changed="The interface carries @PreAuthorize; the claim of no guard is wrong."),
        "alpha/BR-2": _entry(sha["alpha/BR-2"], "incorrect", correction="Purge checks a different flag.",
                             superseded_patterns=[r"Validate, then place"]),
        "alpha/BR-1": _entry(sha["alpha/BR-1"], "verified"),
    }}, present=True)


def test_needs_edit_and_incorrect_listed_with_superseded_hits(tmp_path: Path, bre_mini: Path,
                                                               repo_mini: Path) -> None:
    pre = run_precheck(bre_mini, repo_mini, tmp_path / "rev", ["alpha", "beta"], CFG)
    data = build_errata(_review(bre_mini), _claims(bre_mini), BreLayout(bre_mini), pre, "bre", ["alpha", "beta"])
    ids = [e["claim_id"] for e in data["entries"]]
    assert ids == ["alpha/BR-2", "beta/BR-6"]  # verified alpha/BR-1 is not an erratum
    beta = data["entries"][1]
    assert beta["status"] == "needs-edit" and beta["change"].startswith("The interface carries")
    assert {"path": "bre/detailed-domain-bre/domain-beta/workflows.md", "line": 4} in beta["occurrences"]  # cluster
    alpha = data["entries"][0]
    assert {"path": "bre/detailed-domain-bre/domain-beta/workflows.md", "line": 3} in alpha["occurrences"]  # pattern
    text = render_errata_md(data)
    assert "## beta/BR-6 (NEEDS EDIT): Guarded records are saved without an authorization check" in text
    assert "- What changed: The interface carries" in text
    assert "## alpha/BR-2 (INCORRECT): An alpha in use cannot be purged" in text
    assert "`bre/detailed-domain-bre/domain-beta/workflows.md:L4`" in text
    assert "Superseded elsewhere" in text


def test_empty_errata() -> None:
    text = render_errata_md({"entries": []})
    assert EMPTY in text and text.split("\n")[1].startswith("> Trust: Tier 2")


def test_size_cap_truncates_occurrences_never_entries(caplog: pytest.LogCaptureFixture) -> None:
    entries = [{"claim_id": f"a/BR-{i}", "status": "incorrect", "title": f"t{i}", "change": "c " * 20,
                "reviewer": "AI-reviewed", "pointer": "p",
                "occurrences": [{"path": f"bre/some/long/path/file{j}.md", "line": j} for j in range(10)]}
               for i in range(8)]
    full = render_errata_md({"entries": entries}, cap=10**6)
    assert "(+" not in full and "file9.md:L9" in full
    capped = render_errata_md({"entries": entries}, cap=len(full.encode()) - 200)
    assert len(capped.encode()) <= len(full.encode()) - 200
    assert all(f"## a/BR-{i} (" in capped for i in range(8))
    assert "(+" in capped
    with caplog.at_level(logging.WARNING):
        tiny = render_errata_md({"entries": entries}, cap=100)
    assert all(f"## a/BR-{i} (" in tiny for i in range(8)) and "over the 100-byte cap" in caplog.text


def test_generate_writes_errata_from_review_and_precheck(workspace: Path, tmp_path: Path) -> None:
    bre = workspace / "business-rules-extraction"
    rev = tmp_path / "rev"
    review.prepare(bre, workspace, rev, ["beta"], CFG)
    run_precheck(bre, workspace, rev, ["beta"], CFG)
    review.record(rev, "beta/BR-6", "needs-edit", "guarded on the interface", "Kiro", "kiro", CFG,
                  evidence=["src/main/java/x/GuardedApi.java:13-15"], repo=workspace,
                  replacement="## BR-6: Guarded records\n- Source: `GuardedApi.saveGuarded`.\n"
                              "- `@PreAuthorize` on the interface guards the save.",
                  what_changed="A guard exists on the interface.")
    generate(bre, workspace, rev, CFG)
    errata = (workspace / ".kiro/context/errata.md").read_text(encoding="utf-8")
    assert "## beta/BR-6 (" in errata and "domain-beta/workflows.md:L4" in errata
    assert validate(workspace).exit_code == 0
    generate(bre, workspace, None, CFG)
    assert EMPTY in (workspace / ".kiro/context/errata.md").read_text(encoding="utf-8")


def test_validate_warns_on_oversized_errata(workspace: Path) -> None:
    generate(workspace / "business-rules-extraction", workspace, None, CFG)
    path = workspace / ".kiro/context/errata.md"
    path.write_text(path.read_text(encoding="utf-8") + "x" * 17000, encoding="utf-8")
    assert any(f.level == "WARN" and "errata.md" in f.path for f in validate(workspace).findings)
