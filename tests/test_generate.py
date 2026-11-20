"""Tests for bcp.generate (orchestration, ownership, idempotency, AC 6, N3, sticky baseline)."""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

import pytest

from bcp import review, textio
from bcp.config import BcpConfig
from bcp.drift import check_drift
from bcp.errors import BcpInputError, OutputWriteError
from bcp.generate import BASELINE_REL, RECORD_REL, generate
from bcp.render import GENERATED_MARKER
from conftest import BRE_MINI, MANIFEST_REL, copy_tree, load_json, write_json

CFG = BcpConfig()


def bre_of(ws: Path) -> Path:
    return ws / "business-rules-extraction"


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted((root / ".kiro").rglob("*")) if p.is_file()}


@pytest.mark.skipif(sys.platform != "win32", reason="open files block os.replace only on Windows")
def test_regenerate_while_a_generated_file_is_open(workspace: Path) -> None:
    """Real-data fix: re-running generate while Kiro read reviewed-rules.md failed with WinError 5."""
    generate(bre_of(workspace), workspace, None, CFG)
    held = workspace / ".kiro" / "context" / "domains" / "alpha" / "reviewed-rules.md"
    handle = open(held, "rb")  # noqa: SIM115 - held open on purpose, like a Kiro reader
    waits: list[float] = []

    def reader_closes_during_wait(seconds: float) -> None:
        waits.append(seconds)
        handle.close()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(textio.time, "sleep", reader_closes_during_wait)
        try:
            result = generate(bre_of(workspace), workspace, None, CFG)
        finally:
            handle.close()
    assert waits, "the open file should have forced at least one retry"
    assert ".kiro/context/domains/alpha/reviewed-rules.md" in result.written
    assert not list((workspace / ".kiro").glob(".bcp-staging-*"))


def test_full_run_tree(workspace: Path, tmp_path: Path) -> None:
    rev = tmp_path / "review"
    review.prepare(bre_of(workspace), workspace, rev, ["alpha"], CFG)
    review.record(rev, "alpha/BR-2", "verified", "throws", "Kiro", "kiro", CFG,
                  evidence=["src/main/java/x/AlphaService.java:16-20"], repo=workspace)
    result = generate(bre_of(workspace), workspace, rev, CFG)
    files = set(result.files)
    for rel in [
        ".kiro/steering/product.md", ".kiro/steering/tech.md", ".kiro/steering/structure.md",
        ".kiro/steering/bre-context-index.md", ".kiro/steering/bre-domain-alpha.md",
        ".kiro/steering/bre-domain-alpha-code.md", ".kiro/steering/bre-impact-analysis.md",
        ".kiro/steering/bre-variants.md", ".kiro/steering/bre-glossary.md",
        ".kiro/context/domains/alpha/summary.md", ".kiro/context/domains/alpha/reviewed-rules.md",
        ".kiro/context/impact-map.md", ".kiro/context/impact-map.json", ".kiro/context/file-index.json",
        ".kiro/context/variant-map.md", ".kiro/context/variant-map.json", ".kiro/context/glossary.md",
        ".kiro/context/review-status.md", BASELINE_REL,
        ".kiro/skills/onboarding-guide/SKILL.md", ".kiro/skills/scoped-test-generation/SKILL.md",
        ".kiro/steering/bre-use-brownfield-agent.md",
        ".kiro/agents/brownfield.json", ".kiro/agents/impact-analysis.json",
        ".kiro/agents/product-onboarding.json", ".kiro/agents/defect-triage.json",
        ".kiro/agents/requirement-gap.json",
    ]:
        assert rel in files, rel
        assert (workspace / rel).is_file()
    assert (workspace / RECORD_REL).is_file()
    rules = (workspace / ".kiro/context/domains/alpha/reviewed-rules.md").read_text(encoding="utf-8")
    assert "## BR-2: An alpha in use cannot be purged\n> Review: VERIFIED" in rules
    assert "> Anchors" not in rules  # default (delegate) design: no anchor lines
    assert "> Review: NEEDS SME (A cited code location was not found" in rules  # D2 fallback flows through
    assert not list((workspace / ".kiro").glob(".bcp-staging-*"))
    # the narrow-capped design adds a citable anchor resolved against the target's source
    generate(bre_of(workspace), workspace, rev, BcpConfig(agent_design="narrow-capped"))
    rules = (workspace / ".kiro/context/domains/alpha/reviewed-rules.md").read_text(encoding="utf-8")
    assert ("> Anchors (cite these; lines as of the review): AlphaService.java:L16 `AlphaService.purgeAlpha` "
            "`public void purgeAlpha(Alpha alpha) {`") in rules


def test_hook_only_generates_and_validates(workspace: Path) -> None:
    import json

    from bcp.config import from_dict
    from bcp.validate import validate

    cfg = from_dict({"agent_design": "hook-only",
                     "kb": {"kb_id": "KBTEST1234", "region": "us-east-1",
                            "graph": ".kiro/context/graph.json"}})
    generate(bre_of(workspace), workspace, None, cfg)
    report = validate(workspace)
    assert report.count("ERROR") == 0, [f.line() for f in report.findings if f.level == "ERROR"]
    product = (workspace / ".kiro/steering/product.md").read_text(encoding="utf-8")
    assert "## Domains" not in product  # the per-domain list moved to the auto context index
    entry = json.loads((workspace / ".kiro/agents/brownfield.json").read_text(encoding="utf-8"))
    assert entry["tools"] == ["read"] and "hooks" in entry
    assert not any(t.startswith("subagent/") for t in entry["tools"])


def test_rerun_byte_identical(workspace: Path) -> None:
    generate(bre_of(workspace), workspace, None, CFG)
    first = snapshot(workspace)
    generate(bre_of(workspace), workspace, None, CFG)
    second = snapshot(workspace)
    first.pop(RECORD_REL)
    second.pop(RECORD_REL)
    assert first == second


def test_stale_file_removal(workspace: Path) -> None:
    generate(bre_of(workspace), workspace, None, CFG)
    assert (workspace / ".kiro/steering/bre-domain-gamma.md").exists()
    path = bre_of(workspace) / MANIFEST_REL
    data = load_json(path)
    data["domains"] = data["domains"][:2]
    data["domains"][1]["depends_on_domains"] = []
    data["execution_order"] = ["beta", "alpha"]
    write_json(path, data)
    result = generate(bre_of(workspace), workspace, None, CFG)
    assert ".kiro/steering/bre-domain-gamma.md" in result.deleted
    assert not (workspace / ".kiro/steering/bre-domain-gamma.md").exists()


def test_unmarked_foundation_file(workspace: Path, caplog: pytest.LogCaptureFixture) -> None:
    mine = workspace / ".kiro/steering/product.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("---\ninclusion: always\n---\nTeam product notes\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        generate(bre_of(workspace), workspace, None, CFG)
    assert mine.read_text(encoding="utf-8").endswith("Team product notes\n")
    assert GENERATED_MARKER in (workspace / ".kiro/steering/bre-product.md").read_text(encoding="utf-8")
    assert "bre-product.md instead" in caplog.text


@pytest.mark.parametrize("rel", [".kiro/steering/bre-glossary.md", ".kiro/context/impact-map.json"])
def test_unmarked_other_file_is_fatal(workspace: Path, rel: str) -> None:
    dest = workspace / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("user content\n", encoding="utf-8")
    with pytest.raises(OutputWriteError) as err:
        generate(bre_of(workspace), workspace, None, CFG)
    assert err.value.exit_code == 4
    assert dest.read_text(encoding="utf-8") == "user content\n"


def test_hand_edited_kept_unless_force(workspace: Path) -> None:
    generate(bre_of(workspace), workspace, None, CFG)
    path = workspace / ".kiro/steering/bre-glossary.md"
    edited = path.read_text(encoding="utf-8") + "my edit\n"
    path.write_text(edited, encoding="utf-8", newline="\n")
    result = generate(bre_of(workspace), workspace, None, CFG)
    assert ".kiro/steering/bre-glossary.md" in result.kept
    assert path.read_text(encoding="utf-8") == edited
    generate(bre_of(workspace), workspace, None, CFG)
    assert path.read_text(encoding="utf-8") == edited  # still kept on the next run
    generate(bre_of(workspace), workspace, None, CFG, force=True)
    assert "my edit" not in path.read_text(encoding="utf-8")


def test_bre_outside_target_copied(repo_copy: Path) -> None:
    generate(BRE_MINI, repo_copy, None, CFG)
    copy = repo_copy / ".kiro/context/bre"
    assert (copy / MANIFEST_REL).is_file()
    assert (copy / "database-documentation/00-global-schema.sql").is_file()
    assert not (copy / "analysis-dashboard.html").exists()
    text = (repo_copy / ".kiro/steering/bre-domain-alpha.md").read_text(encoding="utf-8")
    assert "#[[file:.kiro/context/bre/detailed-domain-bre/domain-alpha/overview.md]]" in text


def test_no_copy_bre_fatal(repo_copy: Path) -> None:
    with pytest.raises(BcpInputError) as err:
        generate(BRE_MINI, repo_copy, None, CFG, no_copy_bre=True)
    assert err.value.exit_code == 2


def test_ac6_missing_optional_inputs(bre_noinv: Path, repo_copy: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="bcp"):
        result = generate(bre_noinv, repo_copy, None, CFG)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert sum("domain gamma: no detailed BRE folder" in w for w in warnings) == 1
    assert sum("integrations.md not found" in w for w in warnings) == 1
    assert sum("no configuration inventory" in w for w in warnings) == 1
    assert len(warnings) == 3, warnings
    assert result.files
    assert "No configuration inventory found" in (repo_copy / ".kiro/context/variant-map.md").read_text(encoding="utf-8")
    assert "UNREVIEWED" in (repo_copy / ".kiro/context/domains/alpha/reviewed-rules.md").read_text(encoding="utf-8")


def test_budget_exceeded(workspace: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = generate(bre_of(workspace), workspace, None, BcpConfig(always_budget_bytes=2000))
    assert result.always_bytes > 2000
    assert any("always_budget_bytes" in r.getMessage() and "not a Kiro limit" in r.getMessage()
               for r in caplog.records)
    assert (workspace / ".kiro/steering/product.md").is_file()


def test_dry_run_writes_nothing(workspace: Path) -> None:
    result = generate(bre_of(workspace), workspace, None, CFG, dry_run=True)
    assert result.files and result.dry_run
    assert not (workspace / ".kiro").exists()


def test_n3_target_with_only_bre(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    target = tmp_path / "only-bre"
    copy_tree(BRE_MINI, target / "business-rules-extraction")
    with caplog.at_level(logging.WARNING, logger="bcp"):
        generate(target / "business-rules-extraction", target, None, CFG)
    assert caplog.text.count("no source files under target") == 1
    assert not (target / BASELINE_REL).exists()
    variants = load_json(target / ".kiro/context/variant-map.json")
    assert variants["code_scanned"] is False and all(e["hits"] == [] for e in variants["entries"])


def test_sticky_baseline(workspace: Path) -> None:
    generate(bre_of(workspace), workspace, None, CFG)
    base = load_json(workspace / BASELINE_REL)
    entry = base["claims"]["beta/BR-1"]
    assert entry["tokens"][0]["status"] == "resolved"
    svc = workspace / "src/main/java/x/BetaService.java"
    svc.write_text(svc.read_text(encoding="utf-8").replace("saveBeta(", "storeBeta("), encoding="utf-8")
    generate(bre_of(workspace), workspace, None, CFG)
    again = load_json(workspace / BASELINE_REL)["claims"]["beta/BR-1"]
    assert again == entry  # sticky: still resolved, same recorded_at
    report = check_drift(bre_of(workspace), workspace, CFG, use_git=False)
    assert any(f["kind"] == "unresolved-citation" and f.get("claim") == "beta/BR-1" for f in report.findings)
    generate(bre_of(workspace), workspace, None, CFG, refresh_baseline=True)
    fresh = load_json(workspace / BASELINE_REL)["claims"]["beta/BR-1"]
    assert fresh["tokens"][0]["status"] == "method-missing"
    shutil.rmtree(workspace / ".kiro")
