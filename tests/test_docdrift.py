"""Tests for bcp.docdrift and the release drift list (``drift --since-generation``).

Git is never run: ``bcp.drift._git`` is monkeypatched, so no repository is created.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bcp import drift
from bcp.cli import main
from bcp.config import BcpConfig
from bcp.docdrift import check_docs, extract, render_docdrift_md
from bcp.errors import BcpInputError

DEFAULTS_JAVA = """package x;

public class Defaults {
    void seed(java.util.List<Object> props) {
        props.add(new GlobalProperty(Constants.GP_ALPHA_MODE, "basic", "the alpha mode"));
    }
}
"""
DOC = """# Alpha guide

`AlphaService.saveAlpha` validates the alpha and always calls `BetaService.saveBeta`.
The purge uses `AlphaService.removeAlpha`.
`MissingWidget` handles retries.
The constant GP_ALPHA_MODE selects the mode; GP_NOT_THERE is unused.
`alpha.mode` = strict by default.
Calls go to /api/alpha/save. See Setup.md for details.
"""


def _fake_git(calls: list[tuple[str, ...]], answers: dict[str, tuple[int, str]]) -> Any:
    def fake(repo: Path, *args: str) -> tuple[int, str]:
        calls.append(args)
        if args[0] == "log":
            name = args[-1]
            return answers.get(name, (0, "2024-05-01T00:00:00+00:00\n"))
        return answers.get(args[0], (0, ""))
    return fake


@pytest.fixture
def repo(repo_copy: Path) -> Path:
    (repo_copy / "src/main/java/x/Defaults.java").write_text(DEFAULTS_JAVA, encoding="utf-8")
    return repo_copy


def test_extract_kinds() -> None:
    ex = extract(DOC)
    assert {(m["class"], m["method"]) for m in ex["methods"]} == {
        ("AlphaService", "saveAlpha"), ("BetaService", "saveBeta"), ("AlphaService", "removeAlpha")}
    assert [c["class"] for c in ex["classes"]] == ["MissingWidget"]
    assert [c["name"] for c in ex["constants"]] == ["GP_ALPHA_MODE", "GP_NOT_THERE"]
    assert ex["values"] == [{"key": "alpha.mode", "value": "strict", "line": 7}]
    assert [a["path"] for a in ex["apis"]] == ["/api/alpha/save"]
    assert [c["line"] for c in ex["claims"]] == [3, 5]


def test_findings_by_severity(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = tmp_path / "docs" / "guide.md"
    doc.parent.mkdir()
    doc.write_text(DOC, encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(drift, "_git", _fake_git(calls, {"guide.md": (0, "2020-01-01T00:00:00+00:00\n")}))
    data = check_docs([doc], repo, BcpConfig())
    kinds = {(f["severity"], f["kind"], f["identifier"]) for f in data["findings"]}
    assert ("HIGH", "missing-method", "AlphaService.removeAlpha") in kinds
    assert ("HIGH", "missing-class", "MissingWidget") in kinds
    assert ("HIGH", "missing-constant", "GP_NOT_THERE") in kinds
    assert ("MEDIUM", "value-mismatch", "alpha.mode") in kinds
    assert ("MEDIUM", "missing-api-path", "/api/alpha/save") in kinds
    assert not any(f["identifier"] in ("AlphaService.saveAlpha", "GP_ALPHA_MODE", "Setup.md") for f in data["findings"])
    low = [f for f in data["findings"] if f["severity"] == "LOW"]
    assert len(low) == 1 and "src/main/java/x/AlphaService.java (2024-05-01)" in low[0]["message"]
    assert all(c[0] == "log" for c in calls)  # read-only git only
    md = render_docdrift_md(data)
    assert "## Claims for Kiro to check" in md and "guide.md:L3" in md
    mismatch = next(f for f in data["findings"] if f["kind"] == "value-mismatch")
    assert "code default is `basic` (src/main/java/x/Defaults.java:L5)" in mismatch["message"]


def test_message_keys_and_class_literals_are_not_missing(repo: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: ``Alpha.purgeBlocked`` is a message key (in a properties file), not a method, and
    ``Alpha.class`` is a class literal; neither is drift."""
    (repo / "src/main/resources/messages.properties").write_text("Alpha.purgeBlocked=The alpha is in use\n",
                                                                encoding="utf-8")
    doc = tmp_path / "keys.md"
    doc.write_text("It rejects with `Alpha.purgeBlocked`. The validator supports `Alpha.class`.\n"
                   "It also uses `Alpha.vanished`.\n", encoding="utf-8")
    monkeypatch.setattr(drift, "_git", _fake_git([], {}))
    data = check_docs([doc], repo, BcpConfig(), use_git=False)
    assert [f["identifier"] for f in data["findings"]] == ["Alpha.vanished"]


def test_docdrift_cli(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(DOC, encoding="utf-8")
    monkeypatch.setattr(drift, "_git", _fake_git([], {}))
    out = tmp_path / "out"
    assert main(["docdrift", "--docs", str(docs), "--repo", str(repo), "--out", str(out)]) == 0
    data = json.loads((out / "docdrift-report.json").read_text(encoding="utf-8"))
    assert data["schema"] == "bcp.docdrift/1" and data["counts"]["HIGH"] == 3
    assert main(["docdrift", "--docs", str(tmp_path / "nothing"), "--repo", str(repo), "--out", str(out)]) == 2


def _generated(workspace: Path) -> Path:
    from bcp.generate import generate

    bre = workspace / "business-rules-extraction"
    generate(bre, workspace, None, BcpConfig())
    return bre


def test_release_drift_maps_files_to_domains_and_claims(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bre = _generated(workspace)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(drift, "_git", _fake_git(calls, {
        "diff": (0, "src/main/java/x/AlphaService.java\nREADME.md\n.kiro/context/errata.md\n"),
        "status": (0, " M src/main/java/x/BetaService.java\n?? notes.txt\n"),
    }))
    data = drift.release_drift(bre, workspace, BcpConfig(), base="abc1234")
    assert calls[0] == ("diff", "--name-only", "abc1234..HEAD") and calls[1] == ("status", "--porcelain")
    rows = {r["path"]: r for r in data["changed_files"]}
    assert set(rows) == {"src/main/java/x/AlphaService.java", "README.md", "src/main/java/x/BetaService.java",
                         "notes.txt"}
    alpha = rows["src/main/java/x/AlphaService.java"]
    assert alpha["change"] == "committed" and alpha["domains"] == ["alpha"]
    assert {"claim": "alpha/BR-1", "status": "unreviewed"} in alpha["claims"]
    assert rows["src/main/java/x/BetaService.java"]["change"] == "uncommitted"
    assert set(data["unmapped_changed"]) == {"README.md", "notes.txt"}
    assert "detailed-domain-bre/domain-alpha/business-rules.md" in data["domains"]["alpha"]["bre_docs"]
    md = drift.render_release_md(data)
    assert "| `src/main/java/x/AlphaService.java` | committed | alpha |" in md and "Git was used read-only" in md


def test_release_drift_needs_a_base(workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bre = _generated(workspace)
    monkeypatch.setattr(drift, "_git", _fake_git([], {}))
    with pytest.raises(BcpInputError, match="no base revision"):
        drift.release_drift(bre, workspace, BcpConfig())  # the fixture is not a git repo: no repo_head
    out = tmp_path / "rel"
    assert main(["drift", "--since-generation", "--base", "abc1234", "--bre", str(bre), "--repo", str(workspace),
                 "--out", str(out)]) == 0
    assert (out / "release-drift.md").is_file() and (out / "release-drift.json").is_file()
    monkeypatch.setattr(drift, "_git", _fake_git([], {"diff": (128, "")}))
    with pytest.raises(BcpInputError, match="git diff abc1234..HEAD failed"):
        drift.release_drift(bre, workspace, BcpConfig(), base="abc1234")
