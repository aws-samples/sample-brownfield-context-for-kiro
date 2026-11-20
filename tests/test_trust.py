"""Tests for bcp.trust and the Trust line on generated context files."""

from __future__ import annotations

from pathlib import Path

import pytest

from bcp.config import BcpConfig
from bcp.generate import generate
from bcp.render import GENERATED_MARKER, PROVENANCE_DERIVED, derived_doc, marked_doc
from bcp.trust import TIER1, TIER2, TIER3, TRUST_PREFIX, tier_for, trust_line
from bcp.validate import validate


@pytest.mark.parametrize(
    ("rel", "tier"),
    [
        (".kiro/context/file-index.json", TIER1),
        (".kiro/context/citation-baseline.json", TIER1),
        (".kiro/context/review-status.md", TIER1),
        (".kiro/context/framework-rules.md", TIER1),
        ("bre/database-documentation/00-global-schema.sql", TIER1),
        ("bre/domain-decomposition-overview/data-ownership.md", TIER1),
        (".kiro/context/domains/a/reviewed-rules.md", TIER2),
        (".kiro/context/errata.md", TIER2),
        (".kiro/context/impact-map.json", TIER2),
        ("bre/detailed-domain-bre/domain-a/overview.md", TIER2),
        ("bre/reference-documentation/feature-flag-inventory.md", TIER2),
        ("bre/detailed-domain-bre/domain-a/workflows.md", TIER3),
        ("bre/detailed-domain-bre/domain-a/diagrams/flow.md", TIER3),
        ("bre/diagrams/sequence-diagrams.md", TIER3),
        ("bre/cross-cutting-documentation/common-error-codes.md", TIER3),
        ("bre/reference-documentation/external-integrations-map.md", TIER3),
        ("bre/reference-documentation/coupling-hotspots.md", TIER3),
        ("bre/something/unknown.md", TIER2),
    ],
)
def test_tier_table(rel: str, tier: int) -> None:
    assert tier_for(rel) == tier


def test_trust_line_format() -> None:
    assert trust_line(TIER3) == "> Trust: Tier 3: hypothesis only"
    assert trust_line(TIER1, "x") == "> Trust: Tier 1: mechanical; x"


def test_marker_stays_line_one() -> None:
    for text in (derived_doc("body"), marked_doc("body")):
        lines = text.split("\n")
        assert lines[0] == GENERATED_MARKER and lines[1].startswith(TRUST_PREFIX)
    assert derived_doc("b").split("\n")[2] == PROVENANCE_DERIVED


def test_every_generated_context_markdown_has_a_trust_line(workspace: Path) -> None:
    generate(workspace / "business-rules-extraction", workspace, None, BcpConfig())
    files = sorted((workspace / ".kiro/context").rglob("*.md"))
    names = {p.name for p in files}
    assert {"errata.md", "trust-tiers.md", "framework-rules.md", "review-status.md", "reviewed-rules.md",
            "summary.md", "impact-map.md", "variant-map.md", "glossary.md"} <= names
    for path in files:
        head = path.read_text(encoding="utf-8").split("\n")[:3]
        assert head[0] == GENERATED_MARKER, path
        assert any(line.startswith(TRUST_PREFIX) for line in head), path
    legend = (workspace / ".kiro/context/trust-tiers.md").read_text(encoding="utf-8")
    assert "## Tier 3: hypothesis only" in legend and "Never state it as fact" in legend


def test_validate_errors_on_missing_trust_line(workspace: Path) -> None:
    generate(workspace / "business-rules-extraction", workspace, None, BcpConfig())
    assert validate(workspace).exit_code == 0
    path = workspace / ".kiro/context/glossary.md"
    lines = path.read_text(encoding="utf-8").split("\n")
    path.write_text("\n".join(line for line in lines if not line.startswith(TRUST_PREFIX)), encoding="utf-8")
    report = validate(workspace)
    assert any(f.level == "ERROR" and f.path == ".kiro/context/glossary.md" and "Trust" in f.message
               for f in report.findings)
    # a copied extraction file or a hand-written file without the marker is not checked
    other = workspace / ".kiro/context/notes.md"
    other.write_text("# my notes\n", encoding="utf-8")
    assert not [f for f in validate(workspace).findings if f.path == ".kiro/context/notes.md"]


def test_domain_steering_names_tiers_on_demand(workspace: Path) -> None:
    generate(workspace / "business-rules-extraction", workspace, None, BcpConfig())
    text = (workspace / ".kiro/steering/bre-domain-alpha.md").read_text(encoding="utf-8")
    assert "domain-alpha/workflows.md (Tier 3)" in text
    assert "domain-alpha/features.md (Tier 2)" in text
    assert "diagrams/component-diagram.md (Tier 3)" in text
