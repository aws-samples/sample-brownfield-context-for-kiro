"""Tests for bcp.variants."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bcp.bre import BreLayout
from bcp.config import BcpConfig
from bcp.impact import build_file_index
from bcp.manifest import load_manifest
from bcp.sourceindex import SourceIndex
from bcp.variants import NO_INVENTORY, build_variants, clean_cell, parse_inventory, render_variants_md

SLUGS = {"alpha", "beta", "gamma"}


@pytest.mark.parametrize(
    ("cell", "expected"),
    [(" `encounter` ", "encounter"), ("**key**", "key"), ("__x__", "x"), ("**`both`**", "both"), ("plain", "plain")],
)
def test_clean_cell(cell: str, expected: str) -> None:
    assert clean_cell(cell) == expected


def test_parse_inventory_with_and_without_domain(bre_mini: Path) -> None:
    text = (bre_mini / "reference-documentation/feature-flag-inventory.md").read_text(encoding="utf-8")
    entries = {e["key"]: e for e in parse_inventory(text, SLUGS, "inv.md")}
    assert set(entries) == {"GP_ALPHA_MODE", "GP_BETA_LIMIT", "GP_UNKNOWN_FLAG", "lib.flag"}
    assert entries["GP_ALPHA_MODE"]["domain"] == "alpha"
    assert entries["GP_ALPHA_MODE"]["category"] == "Class A — implementation selectors"
    assert entries["GP_ALPHA_MODE"]["effect"] == "selects the alpha **mode**"
    assert entries["GP_BETA_LIMIT"]["domain"] == "beta"
    assert entries["GP_UNKNOWN_FLAG"]["domain"] is None
    assert entries["lib.flag"]["domain"] is None and entries["lib.flag"]["effect"] == "toggles the lib"


def _build(bre: Path, repo: Path | None, cfg: BcpConfig | None = None) -> dict:  # type: ignore[type-arg]
    cfg = cfg or BcpConfig()
    man = load_manifest(bre)
    index = SourceIndex(repo, cfg.source_extensions, cfg.exclude_dirs) if repo else None
    return build_variants(BreLayout(bre), cfg, man.slugs, index, build_file_index(man), "bre")


def test_code_scan_maps_hits_to_domains(bre_mini: Path, repo_mini: Path) -> None:
    data = _build(bre_mini, repo_mini)
    entries = {e["key"]: e for e in data["entries"]}
    alpha = entries["GP_ALPHA_MODE"]
    assert alpha["read_by"] == ["alpha"]
    assert "src/main/java/x/AlphaService.java:11" in alpha["hits"]
    assert "src/main/java/x/Constants.java:4" in alpha["hits"]
    assert entries["GP_BETA_LIMIT"]["read_by"] == ["alpha", "beta"]
    assert entries["lib.flag"]["hits"] == []  # not an identifier → not scanned
    assert data["inventory_docs"] == ["bre/reference-documentation/feature-flag-inventory.md"]
    md = render_variants_md(data)
    assert "## Class A — implementation selectors" in md and "`GP_ALPHA_MODE`" in md


def test_no_source_index_leaves_hits_empty(bre_mini: Path) -> None:
    data = _build(bre_mini, None)
    assert all(e["hits"] == [] for e in data["entries"])
    assert "Code scan skipped" in render_variants_md(data)


def test_variant_key_patterns(bre_noinv: Path, repo_mini: Path) -> None:
    cfg = BcpConfig(variant_key_patterns=[r"\b(GP_[A-Z_]+)\b"])
    data = _build(bre_noinv, repo_mini, cfg)
    keys = [e["key"] for e in data["entries"]]
    assert keys == ["GP_ALPHA_MODE", "GP_BETA_LIMIT"]
    assert data["message"] is None


def test_empty_inventory_message(bre_noinv: Path, repo_mini: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        data = _build(bre_noinv, repo_mini)
    assert data["message"] == NO_INVENTORY and data["entries"] == []
    assert NO_INVENTORY in render_variants_md(data)
    assert caplog.text.count("no configuration inventory") == 1
