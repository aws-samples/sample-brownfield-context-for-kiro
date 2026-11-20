"""Tests for bcp.glossary."""

from __future__ import annotations

from pathlib import Path

from bcp.glossary import glossary_entries, render_glossary_md
from bcp.manifest import load_manifest


def test_entries_sorted_and_complete(bre_mini: Path) -> None:
    entries = glossary_entries(load_manifest(bre_mini))
    terms = [t for t, _ in entries]
    assert terms == sorted(terms, key=str.lower)
    defs = dict((t, d) for t, d in entries if t != "Alpha")
    assert defs["Alpha Management"] == "domain `alpha`: Manages alpha records and their lifecycle."
    assert defs["Constants"] == "shared-kernel constants, source `src/main/java/x/Constants.java`"
    assert defs["auditing"].startswith("cross-cutting concern: Every save is audited.")
    alpha_defs = [d for t, d in entries if t == "Alpha"]
    assert "entity, table `alpha`, owning domain `alpha`, shared: yes" in alpha_defs
    assert "shared entity owned by `alpha`, used by alpha, beta" in alpha_defs


def test_team_terms(bre_mini: Path, tmp_path: Path) -> None:
    extra = tmp_path / "terms.md"
    extra.write_text("- **Fare product**: what a rider buys\n", encoding="utf-8")
    md = render_glossary_md(load_manifest(bre_mini), extra)
    assert md.endswith("## Team terms\n\n- **Fare product**: what a rider buys\n")
