"""Tests for bcp.impact."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bcp.bre import BreLayout
from bcp.impact import build_file_index, build_impact, mermaid_id, render_impact_md
from bcp.manifest import load_manifest, parse_manifest


def test_shared_tables_and_edges(bre_mini: Path) -> None:
    man = load_manifest(bre_mini)
    impact = build_impact(man, BreLayout(bre_mini), "business-rules-extraction")
    assert impact["shared_tables"] == {"alpha": ["alpha", "beta"], "beta": ["alpha", "beta"]}
    assert impact["domains"]["alpha"]["depends_on"] == [{"slug": "beta"}]
    assert impact["domains"]["beta"]["depended_on_by"] == [{"slug": "alpha"}]
    assert impact["hotspots_doc"] == "business-rules-extraction/reference-documentation/coupling-hotspots.md"
    assert impact["shared_entities"][0]["name"] == "Alpha"


def test_unknown_dependency(caplog: pytest.LogCaptureFixture) -> None:
    raw = {"domains": [{"slug": "order-set", "name": "O", "depends_on_domains": ["ghost"]}]}
    man = parse_manifest(raw, Path("m.json"))
    with caplog.at_level(logging.WARNING):
        impact = build_impact(man)
    assert impact["domains"]["order-set"]["depends_on"] == [{"slug": "ghost", "unknown": True}]
    assert "ghost" in caplog.text
    md = render_impact_md(impact, man, "file-index.json")
    assert "d_order_set" in md and "--> d_ghost" not in md
    assert mermaid_id("order-set") == "d_order_set"


def test_file_index_and_markdown(bre_mini: Path) -> None:
    man = load_manifest(bre_mini)
    index = build_file_index(man)
    assert index["src/main/java/x/AlphaService.java"] == ["alpha"]
    assert index["lib/util.py"] == ["gamma"]
    md = render_impact_md(build_impact(man, BreLayout(bre_mini), "bre"), man, ".kiro/context/file-index.json")
    assert "graph LR" in md
    assert "  d_alpha --> d_beta" in md and "  d_beta --> d_gamma" in md
    assert md.index("`beta`: 1") < md.index("`gamma`: 1") or "`alpha`: 0" in md
    assert "- Coupling hotspots: bre/reference-documentation/coupling-hotspots.md" in md
    assert "- Data ownership: bre/domain-decomposition-overview/data-ownership.md" in md
