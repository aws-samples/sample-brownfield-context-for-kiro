"""Tests for bcp.config."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from bcp.config import BcpConfig, from_dict, load_config
from bcp.errors import BcpInputError


def test_defaults() -> None:
    cfg = load_config(None)
    assert isinstance(cfg, BcpConfig)
    assert cfg.always_budget_bytes == 16000
    assert ".java" in cfg.source_extensions
    assert "business-rules-extraction" in cfg.exclude_dirs


def test_unknown_key_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        from_dict({"nope": 1})
    assert "nope" in caplog.text


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("project_name", ""),
        ("source_extensions", ["java"]),
        ("exclude_dirs", ["a/b"]),
        ("variant_key_patterns", ["("]),
        ("always_budget_bytes", 10),
        ("domain_budget_bytes", True),
        ("main_source_dirs", ["../x"]),
        ("rule_heading_pattern", "^(BR)$"),
        ("source_line_pattern", "["),
        ("guard_annotations", []),
        ("guard_annotations", ["Pre Authorize"]),
        ("framework_rules", 3),
        ("agent_design", "v4"),
    ],
)
def test_bad_values_name_the_key(key: str, value: object) -> None:
    with pytest.raises(BcpInputError, match=key):
        from_dict({key: value})


def test_glossary_extra_relative_to_config(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "terms.md").write_text("- term", encoding="utf-8")
    cfg_path = tmp_path / "bcp.config.json"
    cfg_path.write_text(json.dumps({"glossary_extra": "terms.md", "project_name": "P"}), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.glossary_extra == tmp_path / "terms.md"
    cfg_path.write_text(json.dumps({"glossary_extra": "missing.md"}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_config(cfg_path).glossary_extra is None
    assert "missing.md" in caplog.text


def test_new_keys_defaults() -> None:
    cfg = BcpConfig()
    assert cfg.agent_design == "hybrid" and cfg.framework_rules == "auto"
    assert "PreAuthorize" in cfg.guard_annotations and "Authorized" in cfg.guard_annotations
    assert from_dict({"framework_rules": "blank"}).framework_rules == "blank"
    assert from_dict({"agent_design": "delegate"}).agent_design == "delegate"


def test_hook_only_requires_kb_section() -> None:
    kb = {"kb_id": "KB1", "region": "us-east-1", "graph": ".kiro/context/graph.json"}
    cfg = from_dict({"agent_design": "hook-only", "kb": kb})
    assert cfg.agent_design == "hook-only" and cfg.kb is not None
    with pytest.raises(BcpInputError, match="hook-only"):
        from_dict({"agent_design": "hook-only"})


def test_kb_first_requires_kb_section() -> None:
    kb = {"kb_id": "KB1", "region": "us-east-1", "graph": ".kiro/context/graph.json"}
    cfg = from_dict({"agent_design": "kb-first", "kb": kb})
    assert cfg.agent_design == "kb-first" and cfg.kb is not None
    with pytest.raises(BcpInputError, match="kb-first"):
        from_dict({"agent_design": "kb-first"})


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(BcpInputError):
        load_config(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(BcpInputError):
        load_config(bad)
