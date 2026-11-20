"""Tests for bcp.powercheck."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bcp.powercheck import SCHEMA_URL, check_power


def make(root: Path, manifest: dict | None = None, skills: dict[str, str] | None = None) -> Path:  # type: ignore[type-arg]
    data = {"$schema": SCHEMA_URL, "name": "my-power", "version": "1.0.0", "description": "d",
            "author": {"name": "A"}, "keywords": ["k"]}
    if manifest is not None:
        data = manifest
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    for name, text in (skills if skills is not None else {"good-skill": "---\nname: good-skill\ndescription: d\n---\n"}).items():
        (root / "skills" / name).mkdir(parents=True, exist_ok=True)
        (root / "skills" / name / "SKILL.md").write_text(text, encoding="utf-8")
    return root


def base() -> dict:  # type: ignore[type-arg]
    return {"$schema": SCHEMA_URL, "name": "my-power", "version": "1.0.0", "description": "d",
            "author": {"name": "A"}, "keywords": ["k"]}


def test_valid_package(tmp_path: Path) -> None:
    report = check_power(make(tmp_path / "p"))
    assert report.ok and report.skills == ["good-skill"]


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (lambda d: d.update(extra=1), "not allowed"),
        (lambda d: d.update(name="Bad--Name"), "pattern"),
        (lambda d: d.pop("keywords"), "keywords"),
        (lambda d: d.update(author={"name": "A", "phone": "1"}), "author keys"),
        (lambda d: d.update({"$schema": "https://example.com/x.json"}), "$schema"),
    ],
)
def test_manifest_errors(tmp_path: Path, change, needle: str) -> None:  # type: ignore[no-untyped-def]
    data = base()
    change(data)
    report = check_power(make(tmp_path / "p", data))
    assert not report.ok and any(needle in e for e in report.errors), report.errors


@pytest.mark.parametrize("bad", ["-x", "x-", "a--b"])
def test_invalid_skill_names(tmp_path: Path, bad: str) -> None:
    report = check_power(make(tmp_path / "p", None, {bad: f"---\nname: \"{bad}\"\ndescription: d\n---\n"}))
    assert not report.ok


def test_skill_mismatch_and_long_description(tmp_path: Path) -> None:
    report = check_power(make(tmp_path / "p", None, {"a-skill": "---\nname: other\ndescription: d\n---\n"}))
    assert any("folder name" in e for e in report.errors)
    long = "x" * 1025
    report = check_power(make(tmp_path / "q", None, {"b-skill": f"---\nname: b-skill\ndescription: {long}\n---\n"}))
    assert any("1-1024" in e for e in report.errors)


def test_missing_plugin_json(tmp_path: Path) -> None:
    assert not check_power(tmp_path).ok
