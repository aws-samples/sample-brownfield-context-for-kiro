"""Tests for bcp.validate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bcp.config import BcpConfig
from bcp.generate import generate
from bcp.render import GENERATED_MARKER, PROVENANCE_DERIVED, PROVENANCE_EXTRACTED
from bcp.validate import validate


@pytest.fixture
def gen(workspace: Path) -> Path:
    generate(workspace / "business-rules-extraction", workspace, None, BcpConfig())
    return workspace


def levels(report, needle: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [f.level for f in report.findings if needle in f.message or needle in f.path]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def test_generated_workspace_is_clean(gen: Path) -> None:
    report = validate(gen)
    assert report.exit_code == 0, [f.line() for f in report.findings]
    assert report.count("ERROR") == 0 and report.count("WARN") == 0
    assert 0 < report.always_bytes <= 16000


@pytest.mark.parametrize(
    ("name", "content", "needle"),
    [
        ("bom.md", "\ufeff---\ninclusion: always\n---\n", "BOM"),
        ("blank.md", "\n---\ninclusion: always\n---\n", "line 1"),
        ("bad.md", "---\ninclusion: sometimes\n---\n", "inclusion"),
        ("auto.md", "---\ninclusion: auto\nname: x\n---\n", "description"),
        ("fm.md", "---\ninclusion: fileMatch\n---\n", "fileMatchPattern"),
        ("ref.md", "---\ninclusion: manual\n---\n#[[file:nope/missing.md]]\n", "does not resolve"),
        ("out.md", "---\ninclusion: manual\n---\n#[[file:../outside.md]]\n", "leaves the workspace"),
        ("range.md", "---\ninclusion: manual\n---\n#[[file:lib/util.py:5-99]]\n", "line range"),
        ("folder.md", "---\ninclusion: manual\n---\n#[[folder:nope]]\n", "directory"),
    ],
)
def test_steering_errors(gen: Path, name: str, content: str, needle: str) -> None:
    write(gen / ".kiro/steering" / name, content.replace("---\n", "---\n", 1) + GENERATED_MARKER + "\n")
    report = validate(gen)
    assert "ERROR" in levels(report, needle), [f.line() for f in report.findings]
    assert report.exit_code == 1


def test_unmarked_file_errors_are_warnings(gen: Path) -> None:
    write(gen / ".kiro/steering/user.md", "---\ninclusion: sometimes\n---\n")
    report = validate(gen)
    assert levels(report, "user.md") == ["WARN"]
    assert report.exit_code == 0


def test_valid_line_range_and_folder(gen: Path) -> None:
    write(gen / ".kiro/steering/ok.md", "---\ninclusion: manual\n---\n#[[file:lib/util.py:1-3]]\n#[[folder:lib]]\n")
    assert validate(gen).exit_code == 0


@pytest.mark.parametrize("bad", ["-x", "x-", "a--b", "a" * 65, "Upper"])
def test_skill_names(gen: Path, bad: str) -> None:
    write(gen / ".kiro/skills" / bad / "SKILL.md",
          f"---\nname: {bad}\ndescription: d\n---\n{GENERATED_MARKER}\n")
    report = validate(gen)
    assert "ERROR" in levels(report, "valid Agent Skills name")


def test_skill_mismatch_and_description(gen: Path) -> None:
    write(gen / ".kiro/skills/good-name/SKILL.md",
          f"---\nname: other\ndescription: \"{'x' * 1025}\"\n---\n{GENERATED_MARKER}\n")
    report = validate(gen)
    assert "ERROR" in levels(report, "folder name") and "ERROR" in levels(report, "1-1024")


def test_always_budget_overflow(gen: Path) -> None:
    report = validate(gen, budget=2000)
    assert levels(report, "always-included steering expands") == ["WARN"]
    assert report.exit_code == 0
    assert any("not a Kiro limit" in f.message for f in report.findings if "always-included" in f.message)


def test_always_reference_material_warn(gen: Path) -> None:
    rows = "".join(f"| r{i} | v{i} |\n" for i in range(25))
    write(gen / ".kiro/steering/inventory.md", f"---\ninclusion: always\n---\n{GENERATED_MARKER}\n{rows}")
    write(gen / ".kiro/steering/small.md", f"---\ninclusion: always\n---\n{GENERATED_MARKER}\n| a | b |\n")
    report = validate(gen)
    assert levels(report, "reference material") == ["WARN"]
    assert [f.path for f in report.findings if "reference material" in f.message] == [".kiro/steering/inventory.md"]


def test_always_reference_expansion_warn(gen: Path) -> None:
    write(gen / "lib/big.txt", "x" * 5000)
    write(gen / ".kiro/steering/expands.md", f"---\ninclusion: always\n---\n{GENERATED_MARKER}\n#[[file:lib/big.txt]]\n")
    assert levels(validate(gen), "reference material") == ["WARN"]


def test_agents_md_counted(gen: Path) -> None:
    before = validate(gen).always_bytes
    write(gen / "AGENTS.md", "y" * 1234)
    report = validate(gen)
    assert report.always_bytes == before + 1234 and report.sizes["AGENTS.md"] == 1234


def _agent(gen: Path, resources: list[str], tools: bool = True) -> None:
    data = {"name": "a", "resources": resources}
    if tools:
        data["tools"] = ["*"]
    write(gen / ".kiro/agents/a.json", json.dumps(data))


def test_agent_glob_over_steering_is_error(gen: Path) -> None:
    record = gen / ".kiro/context/generation-record.json"
    rec = json.loads(record.read_text(encoding="utf-8"))
    rec["files"][".kiro/agents/a.json"] = "x"
    record.write_text(json.dumps(rec), encoding="utf-8")
    _agent(gen, ["file://.kiro/steering/**/*.md"])
    report = validate(gen)
    assert "ERROR" in levels(report, "list only always-included files")


def test_agent_unowned_is_warn_and_tools_missing(gen: Path) -> None:
    _agent(gen, ["file://.kiro/steering/**/*.md"], tools=False)
    report = validate(gen)
    assert set(levels(report, "a.json")) == {"WARN"}
    assert "WARN" in levels(report, "no tools list")


def test_agent_over_budget(gen: Path) -> None:
    rec = gen / ".kiro/context/generation-record.json"
    data = json.loads(rec.read_text(encoding="utf-8"))
    data["files"][".kiro/agents/a.json"] = "x"
    rec.write_text(json.dumps(data), encoding="utf-8")
    _agent(gen, ["file://.kiro/steering/product.md"])
    assert not levels(validate(gen, budget=2000), "startup bytes")  # the always budget no longer caps agents
    found = [f for f in validate(gen, agent_budget=500).findings if f.path == ".kiro/agents/a.json"
             and "advisory agent_budget_bytes" in f.message]
    assert [f.level for f in found] == ["WARN"] and "not a Kiro limit" in found[0].message


def _owned_agent(gen: Path, resources: list[str]) -> None:
    rec = gen / ".kiro/context/generation-record.json"
    data = json.loads(rec.read_text(encoding="utf-8"))
    data["files"][".kiro/agents/a.json"] = "x"
    rec.write_text(json.dumps(data), encoding="utf-8")
    write(gen / ".kiro/agents/a.json", json.dumps({"name": "a", "tools": ["read"], "resources": resources,
                                                   "permissions": {"rules": [{"capability": "shell",
                                                                              "effect": "deny"}]}}))


def test_agent_file_count_cap(gen: Path) -> None:
    for i in range(33):
        write(gen / f"ctx/f{i:02}.md", "x")
    _owned_agent(gen, ["file://ctx/*.md"])
    assert levels(validate(gen), "at most 32 file://") == ["ERROR"]
    (gen / "ctx/f32.md").unlink()
    assert not levels(validate(gen), "at most 32 file://")


def test_agent_char_cap(gen: Path) -> None:
    for i in range(3):
        write(gen / f"ctx/f{i}.md", "x" * 40000)
    _owned_agent(gen, ["file://ctx/*.md"])
    report = validate(gen, agent_budget=1000000)
    assert levels(report, "at most 100000 characters") == ["ERROR"]
    assert not levels(report, "truncates")


def test_agent_file_truncation(gen: Path) -> None:
    write(gen / "ctx/big.md", "x" * 50001)
    _owned_agent(gen, ["file://ctx/big.md"])
    assert levels(validate(gen, agent_budget=1000000), "over 50000; Kiro truncates") == ["ERROR"]
    write(gen / "ctx/big.md", "x" * 50000)
    assert not levels(validate(gen, agent_budget=1000000), "truncates")


def test_agent_file_over_1mib_is_skipped(gen: Path) -> None:
    write(gen / "ctx/huge.md", "x" * (1048576 + 1))
    _owned_agent(gen, ["file://ctx/huge.md"])
    report = validate(gen, agent_budget=10000000)
    assert levels(report, "over 1 MiB; Kiro skips it") == ["ERROR"]
    assert not levels(report, "truncates")  # skipped files are not also reported as truncated
    write(gen / "ctx/huge.md", "x" * 1048576)
    report = validate(gen, agent_budget=10000000)
    assert not levels(report, "over 1 MiB")
    assert levels(report, "Kiro truncates") == ["ERROR"]


def test_agent_missing_resources_are_errors(gen: Path) -> None:
    rec = gen / ".kiro/context/generation-record.json"
    data = json.loads(rec.read_text(encoding="utf-8"))
    data["files"][".kiro/agents/a.json"] = "x"
    rec.write_text(json.dumps(data), encoding="utf-8")
    _agent(gen, ["file://nope/missing.md", "skill://.kiro/skills/nope/SKILL.md"])
    report = validate(gen)
    assert "ERROR" in levels(report, "file:// resource file://nope/missing.md matches no file")
    assert "ERROR" in levels(report, "skill:// resource")


def test_generated_agents_pass(gen: Path) -> None:
    report = validate(gen)
    assert not levels(report, ".kiro/agents/"), [f.line() for f in report.findings]
    assert set(report.agent_bytes) == {f".kiro/agents/{n}.json" for n in (
        "brownfield", "impact-analysis", "product-onboarding", "defect-triage", "requirement-gap")}


def test_agent_shell_must_stay_denied(gen: Path) -> None:
    path = gen / ".kiro/agents/brownfield.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["permissions"]["rules"] = [r for r in data["permissions"]["rules"] if r.get("capability") != "shell"]
    data["tools"].append("shell")
    path.write_text(json.dumps(data), encoding="utf-8")
    report = validate(gen)
    assert "ERROR" in levels(report, "no {capability: shell, effect: deny} rule")
    assert "ERROR" in levels(report, "grant shell access")
    assert report.exit_code == 1


def test_agent_unknown_subagent_is_error(gen: Path) -> None:
    (gen / ".kiro/agents/defect-triage.json").unlink()
    report = validate(gen)
    assert "ERROR" in levels(report, "tool subagent/defect-triage names no agent")


def _verified_without_anchor(text: str) -> tuple[str, int, int]:
    first = text.index("> Review: ")
    end = text.index("\n", first)
    return text[:first] + "> Review: VERIFIED · AI-reviewed · evidence: src/A.java:L1-L2" + text[end:], first, end


def test_rules_format_checks(gen: Path) -> None:
    """Default (delegate) design: no anchors expected; a missing review line is still an error."""
    path = gen / ".kiro/context/domains/alpha/reviewed-rules.md"
    text = path.read_text(encoding="utf-8")
    assert validate(gen).exit_code == 0
    stale, first, end = _verified_without_anchor(text)
    path.write_text(stale, encoding="utf-8")
    assert levels(validate(gen), "no '> Anchors' line") == []
    assert levels(validate(gen, domain_budget=100), "narrow fast path") == []
    # a rule heading without its review line is an error
    path.write_text(text[:first] + text[end + 1:], encoding="utf-8")
    report = validate(gen)
    assert "ERROR" in levels(report, "without a '> Review:' line") and report.exit_code == 1


def test_needs_edit_output_validates_and_needs_what_changed(workspace: Path, tmp_path: Path) -> None:
    from bcp import review

    bre = workspace / "business-rules-extraction"
    rev = tmp_path / "rev"
    review.prepare(bre, workspace, rev, ["alpha"], BcpConfig())
    review.record(rev, "alpha/BR-2", "needs-edit", "n", "Kiro", "kiro", BcpConfig(),
                  evidence=["src/main/java/x/AlphaService.java:16-20"], repo=workspace,
                  replacement="Purge throws.\n## BR-9: an inner heading\nmore", what_changed="w")
    generate(bre, workspace, rev, BcpConfig())
    report = validate(workspace)
    assert report.exit_code == 0, [f.line() for f in report.findings]
    summary = (workspace / ".kiro/context/domains/alpha/summary.md").read_text(encoding="utf-8")
    assert "verified 0, needs-edit 1, incorrect 0" in summary
    status = (workspace / ".kiro/context/review-status.md").read_text(encoding="utf-8")
    assert "| Needs edit |" in status and "| `alpha` | 0 | 1 | 0 |" in status
    path = workspace / ".kiro/context/domains/alpha/reviewed-rules.md"
    text = path.read_text(encoding="utf-8")
    assert "### BR-9: an inner heading" in text  # demoted, so it does not pose as a rule heading
    path.write_text(text.replace("> What changed: w\n", ""), encoding="utf-8")
    assert "ERROR" in levels(validate(workspace), "without a '> What changed:' line")


def test_banned_prompt_phrases_are_errors(gen: Path) -> None:
    path = gen / ".kiro/agents/defect-triage.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "source of truth" not in data["prompt"].lower()
    data["prompt"] += "\nThe reviewed context is the source of truth."
    path.write_text(json.dumps(data), encoding="utf-8")
    report = validate(gen)
    assert "ERROR" in levels(report, "confirmed truth") and report.exit_code == 1


def test_rules_format_checks_narrow_capped(workspace: Path) -> None:
    generate(workspace / "business-rules-extraction", workspace, None, BcpConfig(agent_design="narrow-capped"))
    path = workspace / ".kiro/context/domains/alpha/reviewed-rules.md"
    text = path.read_text(encoding="utf-8")
    report = validate(workspace)
    assert report.exit_code == 0 and report.count("WARN") == 0, [f.line() for f in report.findings]
    # a VERIFIED rule with range evidence but no anchors line (an older bcp) is a warning
    stale, _first, _end = _verified_without_anchor(text)
    path.write_text(stale, encoding="utf-8")
    assert levels(validate(workspace), "no '> Anchors' line") == ["WARN"]
    path.write_text(text, encoding="utf-8")
    assert "WARN" in levels(validate(workspace, domain_budget=100), "narrow fast path")


def test_fast_entry_agent_validates(workspace: Path) -> None:
    generate(workspace / "business-rules-extraction", workspace, None,
             BcpConfig(agent_design="delegate", fast_entry_agent=True))
    report = validate(workspace)
    assert report.exit_code == 0 and report.count("WARN") == 0, [f.line() for f in report.findings]
    assert ".kiro/agents/brownfield-fast.json" in report.agent_bytes


def test_domain_budget_and_provenance(gen: Path) -> None:
    report = validate(gen, domain_budget=100)
    assert "WARN" in levels(report, "domain_budget_bytes")
    path = gen / ".kiro/steering/bre-domain-alpha.md"
    path.write_text(path.read_text(encoding="utf-8").replace(PROVENANCE_EXTRACTED, ""), encoding="utf-8")
    glossary = gen / ".kiro/context/glossary.md"
    glossary.write_text(glossary.read_text(encoding="utf-8").replace(PROVENANCE_DERIVED, ""), encoding="utf-8")
    report = validate(gen)
    assert levels(report, "provenance notice missing") == ["WARN", "WARN"]
    assert report.exit_code == 0


def test_kiroignore_and_gitignore(gen: Path) -> None:
    write(gen / ".kiroignore", "*.sql\nbusiness-rules-extraction/\n!business-rules-extraction/detailed-domain-bre/\n")
    write(gen / ".gitignore", ".kiro/context/glossary.md\n")
    report = validate(gen)
    hits = [f for f in report.findings if "ignored by .kiroignore" in f.message]
    assert hits and all(f.level == "WARN" for f in hits)
    assert any("business-rules-extraction/" in f.message for f in hits)  # re-include under excluded dir still warns
    git_hits = [f for f in report.findings if "ignored by .gitignore" in f.message]
    assert git_hits and all(f.level == "INFO" for f in git_hits)
    assert report.exit_code == 0


def test_missing_inclusion_is_info(gen: Path) -> None:
    write(gen / ".kiro/steering/noinc.md", f"---\nname: x\n---\n{GENERATED_MARKER}\n")
    report = validate(gen)
    assert levels(report, "no inclusion key") == ["INFO"]
    msg = next(f.message for f in report.findings if "no inclusion key" in f.message)
    assert "Kiro treats it as always, the documented default" in msg
    assert "https://kiro.dev/docs/steering/" in msg
