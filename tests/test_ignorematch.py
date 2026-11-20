"""Tests for bcp.ignorematch (gitignore subset)."""

from __future__ import annotations

import pytest

from bcp.ignorematch import IgnoreFile, ignored_by, parse_rules


def files(text: str, base: str = "") -> list[IgnoreFile]:
    return [IgnoreFile("test", base, parse_rules(text, "test"))]


@pytest.mark.parametrize(
    ("patterns", "path", "is_dir", "ignored"),
    [
        ("*.sql", "a/b/schema.sql", False, True),
        ("*.sql", "a/b/schema.md", False, False),
        ("/top.md", "top.md", False, True),
        ("/top.md", "sub/top.md", False, False),
        ("docs/*.md", "docs/a.md", False, True),
        ("docs/*.md", "docs/x/a.md", False, False),  # * does not cross /
        ("docs/**/a.md", "docs/x/y/a.md", False, True),
        ("**/temp", "x/y/temp", True, True),
        ("build/", "build", False, False),  # dir-only pattern does not match a file
        ("build/", "build/out.txt", False, True),  # file under an ignored dir
        ("*.md\n!keep.md", "keep.md", False, False),  # last match wins
        ("*.md\n!keep.md\n*.md", "keep.md", False, True),
        ("ex/\n!ex/keep.md", "ex/keep.md", False, True),  # cannot re-include under excluded dir
        ("# comment\n\nfile.txt", "a/file.txt", False, True),
        ("a?c.txt", "abc.txt", False, True),
    ],
)
def test_rules(patterns: str, path: str, is_dir: bool, ignored: bool) -> None:
    assert (ignored_by(files(patterns), path, is_dir) is not None) is ignored


def test_base_dir_anchoring() -> None:
    sub = files("/x.md", base="docs")
    assert ignored_by(sub, "docs/x.md") is not None
    assert ignored_by(sub, "x.md") is None
    assert ignored_by(sub, "docs/deep/x.md") is None


def test_deeper_file_wins() -> None:
    combined = files("*.md") + files("!a.md", base="docs")
    assert ignored_by(combined, "docs/a.md") is None
    assert ignored_by(combined, "docs/b.md") is not None
