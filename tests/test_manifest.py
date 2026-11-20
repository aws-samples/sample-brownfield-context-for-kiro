"""Tests for bcp.manifest."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from bcp.errors import BcpInputError
from bcp.manifest import load_manifest
from conftest import MANIFEST_REL, load_json, write_json


def test_valid_load(bre_mini: Path) -> None:
    man = load_manifest(bre_mini)
    assert man.slugs == ["alpha", "beta", "gamma"]
    alpha = man.domain("alpha")
    assert alpha is not None
    assert alpha.source_files[0].path == "src/main/java/x/AlphaService.java"
    assert alpha.source_files[0].loc == 30
    assert man.domain("beta").depended_on_by == ["alpha"]  # type: ignore[union-attr]
    assert man.domain("gamma").source_paths[0] == "lib/util.py"  # type: ignore[union-attr]
    assert [d.slug for d in man.ordered_domains()] == ["gamma", "beta", "alpha"]
    assert alpha.stats["extraction_wave"] == 2


def _mutate(bre_copy: Path, change) -> Path:  # type: ignore[no-untyped-def]
    path = bre_copy / MANIFEST_REL
    data = load_json(path)
    change(data)
    write_json(path, data)
    return bre_copy


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BcpInputError, match="not found"):
        load_manifest(tmp_path)


def test_invalid_json(bre_copy: Path) -> None:
    (bre_copy / MANIFEST_REL).write_text("{nope", encoding="utf-8")
    with pytest.raises(BcpInputError, match="valid JSON"):
        load_manifest(bre_copy)


def test_missing_domains(bre_copy: Path) -> None:
    _mutate(bre_copy, lambda d: d.pop("domains"))
    with pytest.raises(BcpInputError, match="domains"):
        load_manifest(bre_copy)


@pytest.mark.parametrize("slug", ["../x", "A B", ""])
def test_bad_slug(bre_copy: Path, slug: str) -> None:
    _mutate(bre_copy, lambda d: d["domains"][0].update(slug=slug))
    with pytest.raises(BcpInputError):
        load_manifest(bre_copy)


def test_duplicate_slug(bre_copy: Path) -> None:
    _mutate(bre_copy, lambda d: d["domains"][1].update(slug="alpha"))
    with pytest.raises(BcpInputError, match="duplicate"):
        load_manifest(bre_copy)


def test_missing_name(bre_copy: Path) -> None:
    _mutate(bre_copy, lambda d: d["domains"][1].pop("name"))
    with pytest.raises(BcpInputError, match="name"):
        load_manifest(bre_copy)


def test_optional_keys_default_with_warnings(bre_copy: Path, caplog: pytest.LogCaptureFixture) -> None:
    def strip(d: dict) -> None:  # type: ignore[type-arg]
        for key in ("execution_order", "shared_entities", "common_artifacts"):
            d.pop(key)
        d["project"].pop("stats")

    _mutate(bre_copy, strip)
    with caplog.at_level(logging.WARNING):
        man = load_manifest(bre_copy)
    assert man.execution_order == ["alpha", "beta", "gamma"]
    assert man.shared_entities == [] and man.common_artifacts == {}
    for key in ("execution_order", "shared_entities", "common_artifacts", "project.stats"):
        assert key in caplog.text


def test_unsafe_source_paths_skipped(bre_copy: Path, caplog: pytest.LogCaptureFixture) -> None:
    def add(d: dict) -> None:  # type: ignore[type-arg]
        d["domains"][0]["source_files"] += ["/abs/x.java", "a/../b.java", "bad path.java"]

    _mutate(bre_copy, add)
    with caplog.at_level(logging.WARNING):
        man = load_manifest(bre_copy)
    assert len(man.domain("alpha").source_files) == 3  # type: ignore[union-attr]
    assert caplog.text.count("unsafe source_files path") == 3


def test_order_mismatch_falls_back(bre_copy: Path, caplog: pytest.LogCaptureFixture) -> None:
    _mutate(bre_copy, lambda d: d.update(execution_order=["beta", "nosuch"]))
    with caplog.at_level(logging.WARNING):
        man = load_manifest(bre_copy)
    assert [d.slug for d in man.ordered_domains()] == ["beta", "alpha", "gamma"]
    assert "execution_order" in caplog.text
    assert json.dumps(man.raw)  # raw kept
