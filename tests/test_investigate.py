"""Tests for bcp.investigate: one case per D2 outcome plus the rendered line."""

from __future__ import annotations

from pathlib import Path

import pytest

from bcp.claims import Token, classify
from bcp.config import DEFAULT_EXCLUDE_DIRS, DEFAULT_EXTENSIONS
from bcp.investigate import edit_distance, investigate, why_line
from bcp.sourceindex import SourceIndex

EXT = list(DEFAULT_EXTENSIONS)
ALPHA = ["src/main/java/x/AlphaService.java", "src/main/java/x/Alpha.java"]


@pytest.fixture
def index(repo_mini: Path) -> SourceIndex:
    return SourceIndex(repo_mini, EXT, DEFAULT_EXCLUDE_DIRS)


def run(index: SourceIndex, raw: str, git_repo: Path | None = None) -> dict:  # type: ignore[type-arg]
    token = classify(raw, EXT, None)
    return investigate(index, token, index.resolve(token, ALPHA, ALPHA), git_repo)


def test_six_checks_always_reported(index: SourceIndex) -> None:
    inv = run(index, "ZetaEngine.ignite")
    assert [c["step"] for c in inv["checks"]] == [1, 2, 3, 4, 5, 6]


def test_moved(index: SourceIndex) -> None:
    inv = run(index, "AlphaService.relocatedMethod")
    assert inv["outcome"] == "moved"
    assert inv["status"] == "method-missing"
    assert inv["candidates"][0] == {
        "path": "src/main/java/y/GammaHelper.java",
        "line": 5,
        "reason": "relocatedMethod declared here",
    }


def test_renamed_class(index: SourceIndex) -> None:
    inv = run(index, "AlphaServices")
    assert inv["outcome"] == "renamed"
    assert any(c["path"] == "src/main/java/x/AlphaService.java" for c in inv["candidates"])


def test_renamed_method_similar_name(index: SourceIndex) -> None:
    inv = run(index, "AlphaService.saveAlph")
    assert inv["outcome"] == "renamed"
    assert inv["candidates"][0]["path"] == "src/main/java/x/AlphaService.java"
    assert inv["candidates"][0]["line"] == 7


def test_field_not_method(index: SourceIndex) -> None:
    inv = run(index, "Alpha.status")
    assert inv["outcome"] == "field-not-method"
    assert inv["candidates"][0]["path"] == "src/main/java/x/Alpha.java"
    assert inv["candidates"][0]["line"] == 7


def test_constant_not_class(index: SourceIndex) -> None:
    inv = run(index, "GP_ALPHA_MODE")
    assert inv["outcome"] == "field-not-method"
    assert inv["candidates"][0]["path"] == "src/main/java/x/Constants.java"


def test_deleted(index: SourceIndex) -> None:
    inv = run(index, "ZetaEngine.ignite")
    assert inv["outcome"] == "deleted"
    assert inv["candidates"] == []


def test_non_code(index: SourceIndex) -> None:
    inv = run(index, "IF a / b THEN c")
    assert inv["outcome"] == "non-code"
    assert "treated as text" in inv["why"]


def test_git_step_without_git_repo(index: SourceIndex, tmp_path: Path) -> None:
    inv = run(index, "ZetaEngine.ignite", git_repo=tmp_path)
    assert "git history not available" in inv["checks"][5]["result"]


def test_why_line_links(index: SourceIndex) -> None:
    inv = run(index, "Alpha.status")
    plain = why_line(inv)
    assert plain.startswith("Why not found: ")
    assert "src/main/java/x/Alpha.java#L7" in plain
    linked = why_line(inv, lambda p, n: f"../{p}#L{n}")
    assert "[src/main/java/x/Alpha.java#L7](../src/main/java/x/Alpha.java#L7)" in linked


def test_edit_distance() -> None:
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance("abc", "abc") == 0
    assert isinstance(Token(raw="x", kind="text"), Token)
