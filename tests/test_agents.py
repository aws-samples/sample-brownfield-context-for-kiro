"""Tests for bcp.agents: the brownfield entry agent, its specialists and the startup-bytes budget."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from bcp.agents import ENTRY, FAST_ENTRY, MAX_CODE_READS, SPECIALISTS, Source, Specialist, plan_specialist
from bcp.trust import tier_for
from bcp.bre import BreLayout
from bcp.config import BcpConfig, from_dict
from bcp.manifest import load_manifest
from bcp.render import RenderContext
from bcp.errors import BcpInputError
from bcp.generate import generate

NAMES = [s.name for s in SPECIALISTS]


def run(ws: Path, cfg: BcpConfig | None = None):  # type: ignore[no-untyped-def]
    return generate(ws / "business-rules-extraction", ws, None, cfg or BcpConfig())


def agents(ws: Path) -> dict[str, dict]:  # type: ignore[type-arg]
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((ws / ".kiro/agents").glob("*.json"))}


PINNED = [".kiro/steering/product.md", ".kiro/steering/tech.md", ".kiro/steering/structure.md",
          ".kiro/steering/bre-context-index.md"]
PINNED_RES = [f"file://{p}" for p in PINNED]


def file_resources(data: dict) -> list[str]:  # type: ignore[type-arg]
    return [r[len("file://"):] for r in data["resources"] if r.startswith("file://")]


def context_files(data: dict) -> list[str]:  # type: ignore[type-arg]
    """file:// resources after the pinned steering (which every agent lists first)."""
    files = file_resources(data)
    assert files[:len(PINNED)] == PINNED
    return files[len(PINNED):]


def test_emits_entry_and_four_specialists(workspace: Path) -> None:
    run(workspace)
    found = agents(workspace)
    assert sorted(found) == sorted([ENTRY, *NAMES]) == sorted(
        ["brownfield", "impact-analysis", "product-onboarding", "defect-triage", "requirement-gap"])
    assert not (workspace / ".kiro/agents/brownfield-context.json").exists()
    for name, data in found.items():
        assert data["name"] == name
        assert data["description"].strip() and data["prompt"].strip()
        assert "*" not in data["tools"] and "shell" not in data["tools"] and "@builtin" not in data["tools"]
        rules = data["permissions"]["rules"]
        assert {"capability": "shell", "effect": "deny"} in rules
        assert {"capability": "fs_read", "effect": "allow"} in rules
        assert "Context used:" in data["prompt"]
        prompt = data["prompt"].lower()
        assert "the code is the truth" in prompt
        for banned in ("source of truth", "signed off before it was published", "verified are confirmed"):
            assert banned not in prompt, (name, banned)
        assert "mandatory code check" in prompt
        assert "[code: path#lx-ly]" in prompt and "[unreviewed extraction, unverified:" in prompt


CAPPED = BcpConfig(agent_design="narrow-capped")
DELEGATE = BcpConfig(agent_design="delegate")
FAST = BcpConfig(agent_design="delegate", fast_entry_agent=True)
HOOK_ONLY = from_dict({"agent_design": "hook-only",
                       "kb": {"kb_id": "KBTEST1234", "region": "us-east-1",
                              "graph": ".kiro/context/graph.json"}})
KB_FIRST = from_dict({"agent_design": "kb-first",
                      "kb": {"kb_id": "KBTEST1234", "region": "us-east-1",
                             "graph": ".kiro/context/graph.json"}})
ENTRY_EXTRA = ["skill://.kiro/skills/cross-component-dependencies/SKILL.md",
               "skill://.kiro/skills/doc-drift-check/SKILL.md"]
TASK_SKILLS = ("defect-triage", "change-impact-analysis", "requirement-gap-check")
MANDATORY_TOPICS = ("authorization or its absence", "interface and the implementation", "across line breaks",
                    "rollback-only marking", "self-invocation", "concurrency or locking", "absolute claims",
                    "counts", "anything a diagram or workflow asserts", "whatever the review status")


def test_entry_agent_routes_to_specialists(workspace: Path) -> None:
    """Opt-in delegate design: the entry agent loads only the index and delegates every question."""
    run(workspace, DELEGATE)
    entry = agents(workspace)[ENTRY]
    assert entry["tools"] == ["read"] + [f"subagent/{n}" for n in NAMES]
    assert {"capability": "subagent", "match": NAMES, "effect": "allow"} in entry["permissions"]["rules"]
    assert entry["resources"] == PINNED_RES
    assert entry["description"].endswith("Routes each question to a specialist that answers from the reviewed "
                                         "context and the code.")
    prompt = entry["prompt"]
    assert prompt.split("\n")[1].startswith("1. Delegate.")
    for name in NAMES:
        assert f"`{name}`" in prompt
    for absent in ("Scope the question", "Narrow fast path", "Cap verification", "> Anchors", "code reads"):
        assert absent not in prompt, absent
    assert "cite the evidence ranges already recorded in the `> Review:` lines" in prompt
    pointer = (workspace / ".kiro/steering/bre-use-brownfield-agent.md").read_text(encoding="utf-8")
    assert "routes the question to a specialist" in pointer


def test_hybrid_entry_scopes_and_reads_rules(workspace: Path) -> None:
    """Default (hybrid) design: scope first; narrow questions answered after reading the matched domains'
    reviewed rules; broad ones delegated. Errata, framework rules and task skills are attached."""
    result = run(workspace)
    entry = agents(workspace)[ENTRY]
    assert BcpConfig().agent_design == "hybrid"
    assert entry["tools"] == ["read"] + [f"subagent/{n}" for n in NAMES]
    assert entry["resources"] == PINNED_RES + [
        "file://.kiro/context/errata.md",
        "file://.kiro/context/framework-rules.md"] + [f"skill://.kiro/skills/{s.skill}/SKILL.md" for s in SPECIALISTS] \
        + ["skill://.kiro/skills/cross-component-dependencies/SKILL.md", "skill://.kiro/skills/doc-drift-check/SKILL.md"]
    prompt = entry["prompt"]
    assert "`cross-component-dependencies`" in prompt and "`doc-drift-check`" in prompt
    assert prompt.index("Scope the question") < prompt.index("reviewed-rules.md") < prompt.index("Delegate.")
    assert "Before every narrow answer, read `.kiro/context/domains/<slug>/reviewed-rules.md` for each matched domain" \
        in prompt
    assert "Do not start a sub-agent" in prompt
    for name in NAMES:
        assert f"`{name}`" in prompt
    for topic in MANDATORY_TOPICS:
        assert topic in prompt, topic
    for absent in ("Cap verification", "> Anchors", "Narrow fast path"):
        assert absent not in prompt
    assert result.agents[ENTRY]["startup_bytes"] <= BcpConfig().agent_budget_bytes
    pointer = (workspace / ".kiro/steering/bre-use-brownfield-agent.md").read_text(encoding="utf-8")
    assert "answers narrow questions itself" in pointer and "delegates broad ones" in pointer


def test_default_specialists_read_all_startup_files_without_cap(workspace: Path) -> None:
    run(workspace)
    for name in NAMES:
        specialist = agents(workspace)[name]["prompt"]
        assert "read all of them first, in one parallel batch" in specialist
        assert "Open source code for every mandatory-check topic; for rules marked UNREVIEWED, NEEDS SME or " \
               "stale" in specialist
        for topic in MANDATORY_TOPICS:
            assert topic in specialist, (name, topic)
        assert "Cap verification" not in specialist and "> Anchors" not in specialist


def test_default_skills_open_code_without_cap(workspace: Path) -> None:
    run(workspace)
    for skill in TASK_SKILLS:
        text = (workspace / f".kiro/skills/{skill}/SKILL.md").read_text(encoding="utf-8")
        assert "code reads" not in text and "> Anchors" not in text, skill
    triage = (workspace / ".kiro/skills/defect-triage/SKILL.md").read_text(encoding="utf-8")
    assert "5. Open the code at the rule's evidence lines and read the method." in triage
    assert triage.index("errata.md") < triage.index("Extract the symptoms")
    assert "framework-rules.md" in triage and "rollback-only" in triage
    for skill in (*TASK_SKILLS, "onboarding-guide", "scoped-test-generation", "cross-component-dependencies",
                  "doc-drift-check"):
        text = (workspace / f".kiro/skills/{skill}/SKILL.md").read_text(encoding="utf-8")
        assert "## Verification rules" in text and "The context is a map; the code is the truth." in text, skill
    assert "NEEDS EDIT" in triage
    assert "NEEDS EDIT" in (workspace / ".kiro/skills/scoped-test-generation/SKILL.md").read_text(encoding="utf-8")
    pointer = (workspace / ".kiro/steering/bre-use-brownfield-agent.md").read_text(encoding="utf-8")
    assert "If you already are `brownfield` or one of those specialists" in pointer


def test_narrow_capped_entry_fast_path_and_code_rule(workspace: Path) -> None:
    result = run(workspace, CAPPED)
    entry = agents(workspace)[ENTRY]
    # the task skills are attached so the entry agent can answer narrow questions without a sub-agent hop
    assert [r for r in entry["resources"] if r.startswith("skill://")] == [
        f"skill://.kiro/skills/{s.skill}/SKILL.md" for s in SPECIALISTS]
    prompt = entry["prompt"]
    assert "Narrow fast path" in prompt and "Do not start a sub-agent" in prompt
    assert ".kiro/context/domains/<slug>/reviewed-rules.md" in prompt
    assert prompt.index("Scope the question") < prompt.index("Delegate.")  # scope before delegating
    assert "do not re-open code it already cited" in prompt
    assert f"Cap verification at {MAX_CODE_READS} code reads per answer" in prompt
    assert "`> Anchors` line" in prompt
    assert prompt.index("Mandatory code check") < prompt.index("Cap verification")  # the check comes first
    assert result.agents[ENTRY]["startup_bytes"] <= BcpConfig().agent_budget_bytes
    for name in NAMES:
        specialist = agents(workspace)[name]["prompt"]
        assert f"Cap verification at {MAX_CODE_READS} code reads per answer" in specialist
        assert "Mandatory code check" in specialist
        assert "only those domains' files" in specialist
        assert "read all of them first" not in specialist


def test_narrow_capped_skills_cap_code_reads(workspace: Path) -> None:
    run(workspace, CAPPED)
    for skill in TASK_SKILLS:
        text = (workspace / f".kiro/skills/{skill}/SKILL.md").read_text(encoding="utf-8")
        assert f"at most {MAX_CODE_READS} code reads" in text, skill
        assert "> Anchors" in text, skill
    pointer = (workspace / ".kiro/steering/bre-use-brownfield-agent.md").read_text(encoding="utf-8")
    assert "answers narrow questions from the reviewed context itself" in pointer


def test_hook_only_entry_no_subagents_has_hook(workspace: Path) -> None:
    """Hook-only design: the brownfield agent keeps the kb-retrieve hook, carries the task skills, pins
    the four steering files, but has no subagent/* tools, so it cannot hop to a sub-agent."""
    from bcp.validate import validate

    result = run(workspace, HOOK_ONLY)
    entry = agents(workspace)[ENTRY]
    assert entry["tools"] == ["read"]  # no subagent/* hop
    assert not any(t.startswith("subagent/") for t in entry["tools"])
    assert {"capability": "subagent", "match": NAMES, "effect": "allow"} not in entry["permissions"]["rules"]
    assert {"capability": "shell", "effect": "deny"} in entry["permissions"]["rules"]
    assert entry["resources"] == PINNED_RES + [
        f"skill://.kiro/skills/{s.skill}/SKILL.md" for s in SPECIALISTS] + ENTRY_EXTRA
    assert entry["hooks"][0]["name"] == "kb-retrieve" and entry["hooks"][0]["trigger"] == "UserPromptSubmit"
    prompt = entry["prompt"]
    assert prompt.split("\n")[1].startswith("0. Retrieved context first.")
    assert "without a sub-agent" in prompt and "Do not start a sub-agent" in prompt
    assert "the code is the truth" in prompt.lower() and "Context used:" in prompt
    assert "source of truth" not in prompt.lower()
    for name in NAMES:
        assert f"`{name}`" in prompt  # specialists named for direct, system-wide work
    assert result.agents[ENTRY]["startup_bytes"] <= BcpConfig().agent_budget_bytes
    report = validate(workspace)
    assert report.count("ERROR") == 0, [f.line() for f in report.findings if f.level == "ERROR"]


def test_kb_first_entry_answers_from_facts_without_code(workspace: Path) -> None:
    """kb-first design: brownfield keeps the hook, has no sub-agent hop, pins the four steering files, and
    its prompt says to answer from verified-fresh facts and not open the code for them."""
    from bcp.validate import validate

    result = run(workspace, KB_FIRST)
    entry = agents(workspace)[ENTRY]
    assert entry["tools"] == ["read"]  # no sub-agent hop
    assert {"capability": "subagent", "match": NAMES, "effect": "allow"} not in entry["permissions"]["rules"]
    assert entry["resources"] == PINNED_RES + [
        f"skill://.kiro/skills/{s.skill}/SKILL.md" for s in SPECIALISTS] + ENTRY_EXTRA
    assert entry["hooks"][0]["name"] == "kb-retrieve" and entry["hooks"][0]["trigger"] == "UserPromptSubmit"
    prompt = entry["prompt"]
    assert prompt.split("\n")[1].startswith("0. Retrieved context first.")
    assert "verified-fresh" in prompt and "do NOT open the code for it" in prompt
    assert "[verified-stale]" in prompt and "a task that changes code" in prompt
    assert "the code is the truth" in prompt.lower() and "mandatory code check" in prompt.lower()
    assert "[code: path#Lx-Ly]" in prompt and "[unreviewed extraction, unverified:" in prompt
    assert "source of truth" not in prompt.lower()
    for name in NAMES:
        assert f"`{name}`" in prompt  # specialists named for direct, system-wide work
    assert result.agents[ENTRY]["startup_bytes"] <= BcpConfig().agent_budget_bytes
    report = validate(workspace)
    assert report.count("ERROR") == 0, [f.line() for f in report.findings if f.level == "ERROR"]


def test_fast_entry_agent_only_with_flag(workspace: Path) -> None:
    run(workspace, DELEGATE)
    assert not (workspace / f".kiro/agents/{FAST_ENTRY}.json").exists()
    run(workspace, FAST)
    found = agents(workspace)
    assert sorted(found) == sorted([ENTRY, FAST_ENTRY, *NAMES])
    run(workspace, DELEGATE)  # switching the flag off again removes the stale fast agent
    assert not (workspace / f".kiro/agents/{FAST_ENTRY}.json").exists()


def test_fast_entry_agent_answers_narrow_itself_without_cap(workspace: Path) -> None:
    result = run(workspace, FAST)
    fast = agents(workspace)[FAST_ENTRY]
    assert fast["name"] == FAST_ENTRY
    assert fast["tools"] == ["read"] + [f"subagent/{n}" for n in NAMES]
    assert {"capability": "subagent", "match": NAMES, "effect": "allow"} in fast["permissions"]["rules"]
    assert {"capability": "shell", "effect": "deny"} in fast["permissions"]["rules"]
    assert fast["resources"] == PINNED_RES + [
        f"skill://.kiro/skills/{s.skill}/SKILL.md" for s in SPECIALISTS]
    prompt = fast["prompt"]
    assert prompt.index("Scope the question") < prompt.index("Answer narrow questions yourself") < prompt.index(
        "Delegate.")
    assert "Do not start a sub-agent" in prompt
    for name in NAMES:
        assert f"`{name}`" in prompt
    # the delegate design's code rule, with no cap and no anchors
    assert "Open source code for every mandatory-check topic; for rules marked UNREVIEWED, NEEDS SME or stale" \
        in prompt
    for absent in ("Cap verification", "code reads", "> Anchors", "Narrow fast path"):
        assert absent not in prompt, absent
    assert "Context used:" in prompt and "the code is the truth" in prompt and "source of truth" not in prompt
    assert result.agents[FAST_ENTRY]["startup_bytes"] <= BcpConfig().agent_budget_bytes
    pointer = (workspace / ".kiro/steering/bre-use-brownfield-agent.md").read_text(encoding="utf-8")
    assert "If you already are `brownfield`, `brownfield-fast` or one of those specialists" in pointer


def test_fast_entry_agent_leaves_default_output_unchanged(workspace: Path, tmp_path: Path) -> None:
    run(workspace, DELEGATE)
    default = {p.relative_to(workspace).as_posix(): p.read_bytes()
               for p in sorted((workspace / ".kiro").rglob("*")) if p.is_file()}
    run(workspace, FAST)
    fast = {p.relative_to(workspace).as_posix(): p.read_bytes()
            for p in sorted((workspace / ".kiro").rglob("*")) if p.is_file()}
    changed = sorted(k for k in default if fast.get(k) != default[k])
    assert set(fast) - set(default) == {f".kiro/agents/{FAST_ENTRY}.json"}
    # only the fast agent, the pointer's "if you already are" line and the record differ
    assert changed == [".kiro/context/generation-record.json", ".kiro/steering/bre-use-brownfield-agent.md"]


def test_fast_entry_agent_rejects_narrow_capped(workspace: Path) -> None:
    for design in ("narrow-capped", "hybrid"):
        with pytest.raises(BcpInputError, match="fast_entry_agent"):
            run(workspace, BcpConfig(agent_design=design, fast_entry_agent=True))
        with pytest.raises(BcpInputError, match="fast_entry_agent"):
            from_dict({"agent_design": design, "fast_entry_agent": True})
    with pytest.raises(BcpInputError, match="fast_entry_agent"):
        from_dict({"fast_entry_agent": True})  # the default design is hybrid


def test_specialists_tools_skills_and_resources(workspace: Path) -> None:
    run(workspace)
    found = agents(workspace)
    skills = {s.name: s.skill for s in SPECIALISTS}
    for name in NAMES:
        data = found[name]
        assert data["tools"] == ["read"]
        assert [r for r in data["resources"] if r.startswith("skill://")] == [
            f"skill://.kiro/skills/{skills[name]}/SKILL.md"]
        assert {"capability": "subagent", "match": NAMES, "effect": "allow"} not in data["permissions"]["rules"]
    contract = [".kiro/context/errata.md", ".kiro/context/framework-rules.md"]
    for name in NAMES:
        files = context_files(found[name])
        assert files[:2] == contract, name  # errata first, framework rules second
        assert not any(tier_for(p) == 3 for p in files), (name, files)  # Tier 3 is never preloaded
        assert "(Tier 2)" in found[name]["prompt"] and "trust-tiers.md" in found[name]["prompt"]
    impact = context_files(found["impact-analysis"])
    assert impact[2:4] == [".kiro/context/impact-map.md", ".kiro/context/variant-map.md"]
    assert "business-rules-extraction/reference-documentation/coupling-hotspots.md" not in impact
    assert "business-rules-extraction/domain-decomposition-overview/data-ownership.md" in impact
    prompt = found["impact-analysis"]["prompt"]
    assert ("On-demand navigation aids (Tier 3, hypothesis only; never state as fact): "
            "`business-rules-extraction/reference-documentation/coupling-hotspots.md`") in prompt
    assert "`.kiro/context/impact-map.md` (Tier 2)" in prompt and "`.kiro/context/framework-rules.md` (Tier 1)" in prompt
    # missing extraction files are skipped, never listed
    assert not any("external-integrations-map" in p for p in impact) and "external-integrations-map" not in prompt
    triage = file_resources(found["defect-triage"])
    assert ".kiro/context/domains/alpha/reviewed-rules.md" in triage
    assert "business-rules-extraction/detailed-domain-bre/domain-alpha/validations.md" in triage
    gap = context_files(found["requirement-gap"])
    assert gap[2].endswith("/features.md")  # features first after the contract files, in manifest reading order
    assert "business-rules-extraction/detailed-domain-bre/domain-alpha/features.md" in gap
    onboarding = file_resources(found["product-onboarding"])
    assert "business-rules-extraction/detailed-domain-bre/domain-alpha/overview.md" in onboarding
    assert not any(p.endswith("workflows.md") for p in onboarding)
    assert "domain-alpha/workflows.md" in found["product-onboarding"]["prompt"]  # named as an on-demand aid


def test_json_valid_and_every_resource_exists(workspace: Path) -> None:
    run(workspace)
    for path in sorted((workspace / ".kiro/agents").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))  # raises on invalid JSON
        assert isinstance(data, dict)
        for res in data["resources"]:
            kind, rel = res.split("://", 1)
            assert kind in ("file", "skill"), res
            assert (workspace / rel).is_file(), f"{path.name}: {res}"
            assert not rel.endswith("domain-manifest.json")


def test_agent_domains_scope_and_order(workspace: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="bcp"):
        run(workspace, BcpConfig(agent_domains=["beta", "nope", "alpha"]))
    assert "agent_domains: unknown domain nope ignored" in caplog.text
    triage = file_resources(agents(workspace)["defect-triage"])
    rules = [p for p in triage if p.endswith("reviewed-rules.md")]
    assert rules == [".kiro/context/domains/beta/reviewed-rules.md", ".kiro/context/domains/alpha/reviewed-rules.md"]
    run(workspace, BcpConfig(agent_domains=["alpha"]))
    triage = file_resources(agents(workspace)["defect-triage"])
    assert not any("/beta/" in p or "domain-beta" in p for p in triage)


def test_startup_bytes_measured(workspace: Path) -> None:
    result = run(workspace)
    found = agents(workspace)
    for name, info in result.agents.items():
        data = found[name]
        expected = len(data["prompt"].encode("utf-8")) + len(data["description"].encode("utf-8"))
        for res in data["resources"]:
            expected += (workspace / res.split("://", 1)[1]).stat().st_size
        assert info["startup_bytes"] == expected, name
        assert info["startup_bytes"] <= BcpConfig().agent_budget_bytes
        assert info["dropped"] == []


def test_budget_drops_lowest_relevance_and_logs(workspace: Path, caplog: pytest.LogCaptureFixture) -> None:
    full = run(workspace).agents["impact-analysis"]
    assert len(full["resources"]) >= 3
    budget = full["startup_bytes"] - 1
    with caplog.at_level(logging.WARNING, logger="bcp"):
        tight = run(workspace, BcpConfig(agent_budget_bytes=budget)).agents["impact-analysis"]
    assert tight["startup_bytes"] <= budget
    assert tight["dropped"] and sorted(tight["resources"] + tight["dropped"]) == sorted(full["resources"])
    assert tight["resources"] == [p for p in full["resources"] if p in tight["resources"]]  # order kept
    assert f"agent impact-analysis: dropped {tight['dropped'][-1]}" in caplog.text
    prompt = agents(workspace)["impact-analysis"]["prompt"]
    assert "Also available on demand" in prompt and tight["dropped"][0] in prompt
    assert f"file://{tight['dropped'][0]}" not in agents(workspace)["impact-analysis"]["resources"]


def test_budget_keeps_relevance_order_and_fills_with_smaller_files(bre_mini: Path) -> None:
    ctx = RenderContext(load_manifest(bre_mini), BreLayout(bre_mini), BcpConfig(agent_budget_bytes=12000),
                        "business-rules-extraction")
    spec = Specialist("x", "defect-triage", "d", "t",
                      (Source("generated", "a.md"), Source("generated", "big.md"), Source("generated", "c.md"),
                       Source("generated", "d.md")))
    sizes = {".kiro/context/a.md": 3000, ".kiro/context/big.md": 50000, ".kiro/context/c.md": 3000,
             ".kiro/context/d.md": 4000, ".kiro/skills/defect-triage/SKILL.md": 500}
    plan = plan_specialist(ctx, spec, sizes.get)
    assert plan.resources == [".kiro/context/a.md", ".kiro/context/c.md"]  # big dropped, c still fits
    assert plan.dropped == [".kiro/context/big.md", ".kiro/context/d.md"]
    assert plan.startup_bytes <= 12000
    assert "`.kiro/context/big.md`" in plan.data["prompt"]


def test_budget_too_small_keeps_agent_and_warns(workspace: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="bcp"):
        result = run(workspace, BcpConfig(agent_budget_bytes=100))
    info = result.agents["defect-triage"]
    assert info["resources"] == [] and info["dropped"]
    assert "still over the advisory agent_budget_bytes" in caplog.text
    # the pinned steering is never dropped, even when the advisory budget is far too small
    assert agents(workspace)["defect-triage"]["resources"] == PINNED_RES + [
        "skill://.kiro/skills/defect-triage/SKILL.md"]


def test_every_agent_lists_steering_explicitly(workspace: Path) -> None:
    for cfg in (BcpConfig(), DELEGATE, CAPPED, FAST, HOOK_ONLY, KB_FIRST):
        run(workspace, cfg)
        for name, data in agents(workspace).items():
            files = file_resources(data)
            assert files[:len(PINNED)] == PINNED, (cfg.agent_design, name)
            assert ".kiro/steering/bre-use-brownfield-agent.md" not in files


def test_specialist_respects_kiro_file_caps(bre_mini: Path, caplog: pytest.LogCaptureFixture) -> None:
    ctx = RenderContext(load_manifest(bre_mini), BreLayout(bre_mini), BcpConfig(agent_budget_bytes=1000000),
                        "business-rules-extraction")
    many = tuple(Source("generated", f"f{i:02}.md") for i in range(40))
    spec = Specialist("x", "defect-triage", "d", "t", (Source("generated", "huge.md"), *many))
    sizes = {f".kiro/context/f{i:02}.md": 100 for i in range(40)}
    sizes.update({".kiro/context/huge.md": 50001, ".kiro/skills/defect-triage/SKILL.md": 500})
    with caplog.at_level(logging.WARNING, logger="bcp"):
        plan = plan_specialist(ctx, spec, sizes.get)
    files = file_resources(plan.data)
    assert len(files) == 32 and ".kiro/context/huge.md" not in files
    assert "Kiro truncates file:// files over 50000 characters" in caplog.text
    assert "Kiro loads at most 32 file:// files" in caplog.text
    sizes = {f".kiro/context/f{i:02}.md": 30000 for i in range(5)}
    plan = plan_specialist(ctx, Specialist("y", "defect-triage", "d", "t", many[:5]), sizes.get)
    assert len(file_resources(plan.data)) == 3  # a fourth 30,000-byte file would pass 100,000 characters


def test_pointer_steering_is_always_and_tiny(workspace: Path) -> None:
    result = run(workspace)
    path = workspace / ".kiro/steering/bre-use-brownfield-agent.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\ninclusion: always\n---\n")
    assert path.stat().st_size < 1000
    assert result.always_bytes >= path.stat().st_size


def test_config_keys() -> None:
    cfg = from_dict({"agent_budget_bytes": 40000, "agent_domains": ["a", " b "]})
    assert cfg.agent_budget_bytes == 40000 and cfg.agent_domains == ["a", "b"]
    assert BcpConfig().agent_budget_bytes == 81920 and BcpConfig().agent_domains is None
    assert BcpConfig().agent_design == "hybrid" and BcpConfig().fast_entry_agent is False
    assert from_dict({"agent_design": "delegate", "fast_entry_agent": True}).fast_entry_agent is True
    cfg = from_dict({"agent_design": "narrow-capped"})
    assert cfg.agent_design == "narrow-capped" and not cfg.fast_entry_agent
    for bad in ({"agent_budget_bytes": 5}, {"agent_domains": []}, {"agent_domains": "a"},
                {"agent_design": "v4"}, {"fast_entry_agent": "yes"}):
        with pytest.raises(BcpInputError):
            from_dict(bad)
