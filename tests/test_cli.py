"""Tests for bcp.cli (exit codes, --json, help text, python -m bcp)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bcp import cli
from bcp.errors import BcpInputError, OutputWriteError, ReviewRecordError
from conftest import run_python

SRC = Path(__file__).resolve().parents[1] / "src"


def test_top_level_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for cmd in ("generate", "validate", "review", "drift", "check-power", "precheck"):
        assert cmd in out


def test_record_help_has_fnmatch_note(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["review", "record", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "`*` matches across `/` (Python fnmatch), unlike a shell glob" in out


@pytest.mark.parametrize(("exc", "code"), [(BcpInputError, 2), (ReviewRecordError, 3), (OutputWriteError, 4),
                                           (RuntimeError, 70)])
def test_exit_code_mapping(monkeypatch: pytest.MonkeyPatch, exc: type[Exception], code: int) -> None:
    def boom(_args: object) -> int:
        raise exc("boom")

    monkeypatch.setattr(cli, "_run", boom)
    assert cli.main(["validate", "--target", "."]) == code


def test_input_error_exit_2(tmp_path: Path) -> None:
    assert cli.main(["generate", "--bre", str(tmp_path / "none"), "--target", str(tmp_path)]) == 2


def test_json_summary(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bre = workspace / "business-rules-extraction"
    assert cli.main(["--json", "generate", "--bre", str(bre), "--target", str(workspace)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) >= {"files", "written", "always_bytes", "domains", "rules", "dry_run"}
    assert data["domains"] == 3
    assert cli.main(["validate", "--target", str(workspace), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["errors"] == 0


def test_generate_design_flags(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bre = str(workspace / "business-rules-extraction")
    base = ["generate", "--bre", bre, "--target", str(workspace)]
    agents = workspace / ".kiro/agents"
    rules = workspace / ".kiro/context/domains/alpha/reviewed-rules.md"
    assert cli.main(base) == 0
    assert not (agents / "brownfield-fast.json").exists()
    default = (agents / "brownfield.json").read_text(encoding="utf-8")
    assert "Narrow fast path" not in default and "Scope the question" in default  # hybrid by default
    assert cli.main(base + ["--fast-entry-agent"]) == 2  # the fast agent needs the delegate design
    assert cli.main(base + ["--agent-design", "delegate"]) == 0
    assert "Scope the question" not in (agents / "brownfield.json").read_text(encoding="utf-8")
    assert cli.main(base + ["--agent-design", "delegate", "--fast-entry-agent"]) == 0
    assert (agents / "brownfield-fast.json").is_file()
    assert cli.main(base + ["--agent-design", "narrow-capped"]) == 0
    assert not (agents / "brownfield-fast.json").exists()
    assert "Narrow fast path" in (agents / "brownfield.json").read_text(encoding="utf-8")
    assert "cite them without opening the code" in rules.read_text(encoding="utf-8")
    assert cli.main(base + ["--agent-design", "narrow-capped", "--fast-entry-agent"]) == 2
    with pytest.raises(SystemExit):
        cli.main(base + ["--agent-design", "v4"])
    capsys.readouterr()


def test_hook_only_cli_needs_kb(workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bre = str(workspace / "business-rules-extraction")
    base = ["generate", "--bre", bre, "--target", str(workspace)]
    assert cli.main(base + ["--agent-design", "hook-only"]) == 2  # hook-only needs a kb section
    cfg = tmp_path / "bcp.config.json"
    cfg.write_text(json.dumps({"kb": {"kb_id": "KBTEST1234", "region": "us-east-1",
                                      "graph": ".kiro/context/graph.json"}}), encoding="utf-8")
    assert cli.main(base + ["--agent-design", "hook-only", "--config", str(cfg)]) == 0
    agent = (workspace / ".kiro/agents/brownfield.json").read_text(encoding="utf-8")
    assert "subagent/" not in agent  # no sub-agent hop
    capsys.readouterr()


def test_review_flow_via_cli(bre_mini: Path, repo_copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "rev"
    assert cli.main(["review", "prepare", "--bre", str(bre_mini), "--repo", str(repo_copy), "--out", str(out),
                     "--domains", "alpha"]) == 0
    assert cli.main(["review", "record", "--out", str(out), "--claim", "alpha/BR-1", "--verdict", "verified",
                     "--evidence-search", r"\block", "--in", "src/main/java/x/*Service.java", "--note", "none",
                     "--reviewer", "Kiro", "--reviewer-type", "kiro", "--repo", str(repo_copy)]) == 0
    assert cli.main(["review", "record", "--out", str(out), "--claim", "alpha/BR-1", "--verdict", "verified",
                     "--evidence-search", "x", "--note", "n", "--reviewer", "K", "--reviewer-type", "kiro",
                     "--repo", str(repo_copy)]) == 2
    assert cli.main(["review", "render", "--out", str(out), "--repo", str(repo_copy)]) == 0
    assert cli.main(["review", "signoff", "--out", str(out), "--domain", "alpha", "--reviewer", "Kiro",
                     "--role", "Demo", "--reviewer-type", "kiro", "--decision", "approved"]) == 0
    assert cli.main(["drift", "--bre", str(bre_mini), "--repo", str(repo_copy), "--review-dir", str(out),
                     "--out", str(tmp_path / "drift"), "--git-history", "none"]) == 0
    assert (tmp_path / "drift" / "drift-report.json").is_file()


def test_record_needs_edit_via_cli(bre_mini: Path, repo_copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "rev"
    assert cli.main(["review", "prepare", "--bre", str(bre_mini), "--repo", str(repo_copy), "--out", str(out),
                     "--domains", "alpha"]) == 0
    body = tmp_path / "fixed.md"
    body.write_text("Purge throws APIException when the alpha is in use.\n", encoding="utf-8")
    base = ["review", "record", "--out", str(out), "--claim", "alpha/BR-2", "--verdict", "needs-edit",
            "--evidence", "src/main/java/x/AlphaService.java:16-20", "--note", "n", "--reviewer", "Kiro",
            "--reviewer-type", "kiro (AI stand-in for SME)", "--repo", str(repo_copy)]
    assert cli.main(base) == 2  # no replacement
    assert cli.main(base + ["--replacement-file", str(body), "--replacement", "x", "--what-changed", "w"]) == 2
    assert cli.main(base + ["--replacement-file", str(body), "--what-changed", "w",
                            "--superseded-pattern", "in use"]) == 0
    entry = json.loads((out / "review-record.json").read_text(encoding="utf-8"))["claims"]["alpha/BR-2"]
    assert entry["verdict"] == "needs-edit" and entry["reviewer_type"] == "kiro (AI stand-in for SME)"
    assert entry["replacement"] == "Purge throws APIException when the alpha is in use."
    assert entry["superseded_patterns"] == ["in use"]


def test_python_m_bcp_dry_run(workspace: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = run_python(
        ["-m", "bcp", "generate", "--dry-run", "--bre", str(workspace / "business-rules-extraction"),
         "--target", str(workspace)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "planned" in proc.stdout and ".kiro/steering/product.md" in proc.stdout
    assert "WARN bcp.bre: domain gamma" in proc.stderr
    assert not (workspace / ".kiro").exists()
