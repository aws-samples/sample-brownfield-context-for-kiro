"""Tests for generated steering, context files, skills and agent."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from bcp import frontmatter
from bcp.bre import BreLayout
from bcp.config import BcpConfig
from bcp.manifest import load_manifest
from bcp.render import (
    GENERATED_MARKER,
    PROVENANCE_DERIVED,
    PROVENANCE_EXTRACTED,
    RenderContext,
    auto_description,
    render_agent_pointer,
    render_cross_steering,
    render_domain_steering,
    render_foundation,
    render_framework_rules,
    render_skills,
    render_summary,
)
from bcp.validate import SKILL_NAME_RE


@pytest.fixture
def ctx(bre_mini: Path) -> RenderContext:
    return RenderContext(load_manifest(bre_mini), BreLayout(bre_mini), BcpConfig(), "business-rules-extraction")


def _all(ctx: RenderContext) -> dict[str, str]:
    out = render_foundation(ctx)
    for dom in ctx.manifest.domains:
        out.update(render_domain_steering(ctx, dom))
    out.update(render_cross_steering(ctx))
    out.update(render_skills(ctx))
    return out


def test_front_matter_line1_and_parsers_agree(ctx: RenderContext) -> None:
    for rel, text in _all(ctx).items():
        assert text.startswith("---\n"), rel
        strict, _ = frontmatter.parse(text)
        block = text.split("---\n", 2)[1]
        assert yaml.safe_load(block) == strict, rel
        assert GENERATED_MARKER in text


def test_inclusion_modes(ctx: RenderContext) -> None:
    files = _all(ctx)
    modes = {rel.rsplit("/", 1)[-1]: frontmatter.parse(t)[0].get("inclusion") for rel, t in files.items()
             if "/steering/" in rel}
    assert modes["product.md"] == modes["tech.md"] == modes["structure.md"] == "always"
    assert modes["bre-context-index.md"] == "auto"  # the per-domain directory is reference material, not always
    assert modes["bre-domain-alpha.md"] == "auto" and modes["bre-domain-alpha-code.md"] == "fileMatch"
    assert modes["bre-impact-analysis.md"] == modes["bre-variants.md"] == "auto"
    assert modes["bre-glossary.md"] == "manual"
    for rel in ("product.md", "tech.md", "structure.md", "bre-context-index.md"):
        assert "#[[file:" not in files[f".kiro/steering/{rel}"]


def test_auto_description_and_filematch(ctx: RenderContext) -> None:
    alpha = ctx.manifest.domain("alpha")
    desc = auto_description(alpha)  # type: ignore[arg-type]
    assert desc.startswith("Alpha Management domain (alpha): Manages alpha records and their lifecycle. Use for")
    assert "tables alpha, alpha_attribute" in desc and len(desc) <= 1000
    alpha.description = "word " * 400  # type: ignore[union-attr]
    assert len(auto_description(alpha)) <= 1000  # type: ignore[arg-type]
    code = render_domain_steering(ctx, ctx.manifest.domain("beta"))[".kiro/steering/bre-domain-beta-code.md"]  # type: ignore[arg-type]
    assert 'fileMatchPattern: ["src/main/java/x/BetaService.java", "app/services/orders.py"]' in code


def test_domain_refs_and_provenance(ctx: RenderContext) -> None:
    text = render_domain_steering(ctx, ctx.manifest.domain("alpha"))[".kiro/steering/bre-domain-alpha.md"]  # type: ignore[arg-type]
    lines = text.split("\n")
    first_bre = next(i for i, ln in enumerate(lines) if "detailed-domain-bre" in ln and ln.startswith("#[[file:"))
    assert lines[first_bre - 1] == PROVENANCE_EXTRACTED
    assert "#[[file:business-rules-extraction/detailed-domain-bre/domain-alpha/integrations.md]]" in text
    assert "- business-rules-extraction/detailed-domain-bre/domain-alpha/features.md" in text
    beta = render_domain_steering(ctx, ctx.manifest.domain("beta"))[".kiro/steering/bre-domain-beta.md"]  # type: ignore[arg-type]
    assert "integrations.md]]" not in beta
    gamma = render_domain_steering(ctx, ctx.manifest.domain("gamma"))[".kiro/steering/bre-domain-gamma.md"]  # type: ignore[arg-type]
    assert "No detailed BRE files for this domain." in gamma and PROVENANCE_EXTRACTED not in gamma


def test_unsafe_paths_skipped_and_empty_domain(bre_copy: Path, caplog: pytest.LogCaptureFixture) -> None:
    import json as _json

    path = bre_copy / "domain-decomposition-overview/domain-manifest.json"
    data = _json.loads(path.read_text(encoding="utf-8"))
    data["domains"][2]["source_files"] = ["../escape.py", "/abs.py"]
    path.write_text(_json.dumps(data), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        man = load_manifest(bre_copy)
        out = render_domain_steering(RenderContext(man, BreLayout(bre_copy), BcpConfig(), ""), man.domain("gamma"))  # type: ignore[arg-type]
    assert list(out) == [".kiro/steering/bre-domain-gamma.md"]
    assert "no usable source_files" in caplog.text


def test_summary_provenance_and_cap(ctx: RenderContext) -> None:
    dom = ctx.manifest.domain("alpha")
    text = render_summary(ctx, dom, 8, None)  # type: ignore[arg-type]
    head = text.split("\n")
    assert head[0] == GENERATED_MARKER and head[1].startswith("> Trust: Tier 2") and head[2] == PROVENANCE_DERIVED
    from bcp.manifest import SourceFile

    dom.source_files = [SourceFile(f"src/f{i}.java") for i in range(130)]  # type: ignore[union-attr]
    assert "- and 30 more (see `.kiro/context/file-index.json`)" in render_summary(ctx, dom, 0, None)  # type: ignore[arg-type]


def test_skills(ctx: RenderContext) -> None:
    skills = render_skills(ctx)
    descs = set()
    assert len(skills) == 7
    for rel, text in skills.items():
        assert "Mandatory code check, whatever the review status" in text, rel
        from bcp.validate import check_skill_text

        assert not [p for p in check_skill_text(text, rel.split("/")[2]) if p[0] == "ERROR"], rel
        data, body = frontmatter.parse(text)
        folder = rel.split("/")[2]
        assert data["name"] == folder and SKILL_NAME_RE.match(folder)
        assert 1 <= len(str(data["description"])) <= 1024
        descs.add(data["description"])
        assert "$" not in body.replace("$$", "")
    assert len(descs) == 7
    assert "capped at 12" in skills[".kiro/skills/scoped-test-generation/SKILL.md"]
    impact = skills[".kiro/skills/change-impact-analysis/SKILL.md"]
    assert "What this analysis could not see" in impact
    assert "program-config.md" in impact and "Test gaps" in impact and "dependency-map.md" in impact
    onboarding = skills[".kiro/skills/onboarding-guide/SKILL.md"]
    assert "## Reading order and trust" in onboarding and "Tier 3, hypothesis only" in onboarding
    deps = skills[".kiro/skills/cross-component-dependencies/SKILL.md"]
    assert "`bre-only (verify)`" in deps and "Never state it as fact" in deps
    drift = skills[".kiro/skills/doc-drift-check/SKILL.md"]
    assert "docdrift-report.md" in drift and "release-drift.md" in drift and "If neither report exists" in drift


def test_agent_pointer(ctx: RenderContext) -> None:
    out = render_agent_pointer(ctx, ["impact-analysis", "defect-triage"])
    text = out[".kiro/steering/bre-use-brownfield-agent.md"]
    assert frontmatter.parse(text)[0] == {"inclusion": "always"}
    assert "`brownfield`" in text and "`impact-analysis`, `defect-triage`" in text
    assert "#[[file:" not in text and len(text.encode("utf-8")) < 1000


def test_framework_rules_auto_selects_by_language(ctx: RenderContext) -> None:
    out = render_framework_rules(ctx)  # the fixture manifest lists Java
    text = out[".kiro/context/framework-rules.md"]
    assert text.split("\n")[0] == GENERATED_MARKER and text.split("\n")[1].startswith("> Trust: Tier 1: mechanical")
    for needle in ("Self-invocation bypasses the advice", "rollback-only", "UnexpectedRollbackException",
                   "REQUIRES_NEW", "Checked exceptions do not roll back", "on the interface", "multi-line",
                   "@Scheduled", "How to check in code"):
        assert needle.lower() in text.lower(), needle
    ctx.manifest.project["primary_languages"] = ["COBOL"]
    blank = render_framework_rules(ctx)[".kiro/context/framework-rules.md"]
    assert "Ask the team to fill this in for COBOL" in blank and "UnexpectedRollbackException" not in blank
    ctx.config.framework_rules = "java-spring"
    assert "REQUIRES_NEW" in render_framework_rules(ctx)[".kiro/context/framework-rules.md"]


def test_framework_rules_custom_file_and_steering(ctx: RenderContext, tmp_path: Path) -> None:
    from bcp.config import from_dict
    from bcp.errors import BcpInputError

    custom = tmp_path / "rules.md"
    custom.write_text("# Our rules\n- Batch jobs commit every 500 rows.\n", encoding="utf-8")
    cfg = from_dict({"framework_rules": "rules.md"}, base_dir=tmp_path)
    assert cfg.framework_rules == str(custom)
    with pytest.raises(BcpInputError, match="framework_rules"):
        from_dict({"framework_rules": "missing.md"}, base_dir=tmp_path)
    ctx.config.framework_rules = cfg.framework_rules
    out = render_framework_rules(ctx)
    text = out[".kiro/context/framework-rules.md"]
    assert text.split("\n")[1].startswith("> Trust: Tier 1: mechanical; team-maintained")
    assert "# Our rules\n- Batch jobs commit every 500 rows." in text
    steer = out[".kiro/steering/bre-framework-rules.md"]
    data = frontmatter.parse(steer)[0]
    assert data["inclusion"] == "auto" and data["name"] == "bre-framework-rules"
    assert "rollback" in str(data["description"]) and "#[[file:.kiro/context/framework-rules.md]]" in steer


def test_framework_steering_not_in_always_budget(workspace: Path) -> None:
    from bcp.generate import generate
    from bcp.render import always_files

    result = generate(workspace / "business-rules-extraction", workspace, None, BcpConfig())
    assert ".kiro/steering/bre-framework-rules.md" in result.files
    ctx = RenderContext(load_manifest(workspace / "business-rules-extraction"),
                        BreLayout(workspace / "business-rules-extraction"), BcpConfig(), "business-rules-extraction")
    assert ".kiro/steering/bre-framework-rules.md" not in always_files(ctx)
    assert result.always_bytes == sum(result.files[p] for p in always_files(ctx))


def test_foundation_content(ctx: RenderContext) -> None:
    out = render_foundation(ctx)
    product = out[".kiro/steering/product.md"]
    assert "Draft generated from the business-rules extraction." in product
    # the per-domain list moved to the auto-included index, so product.md carries no domain list or waves
    assert "## Domains" not in product and "### Wave" not in product
    assert "- Context index: `.kiro/steering/bre-context-index.md`" in product
    index = out[".kiro/steering/bre-context-index.md"]
    front = frontmatter.parse(index)[0]
    assert front["inclusion"] == "auto" and front["name"] == "bre-context-index"
    assert isinstance(front["description"], str) and 0 < len(front["description"].strip()) <= 1024
    assert "- alpha — Alpha Management — Manages alpha records and their lifecycle." in index
    assert "including today's defects" in index
    assert "- auditing: Every save is audited." in out[".kiro/steering/tech.md"]
    assert "- `alpha`: `src/main/java/x/`" in out[".kiro/steering/structure.md"]
