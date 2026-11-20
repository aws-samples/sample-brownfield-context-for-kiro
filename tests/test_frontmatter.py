"""Tests for bcp.frontmatter (emit, strict parse, agreement with PyYAML)."""

from __future__ import annotations

import pytest
import yaml

from bcp import frontmatter
from bcp.frontmatter import FrontMatterError

CASES = [
    {"inclusion": "always"},
    {"inclusion": "auto", "name": "bre-domain-alpha", "description": 'Alpha: "quoted" \\ back, colon: yes'},
    {"inclusion": "fileMatch", "fileMatchPattern": ["src/a b.java", 'q"uote.java']},
    {"inclusion": "auto", "name": "true", "description": "123"},
    {"inclusion": "auto", "name": "x", "description": "multi\nline"},
]


@pytest.mark.parametrize("data", CASES)
def test_round_trip_and_yaml_agree(data: dict[str, object]) -> None:
    text = frontmatter.emit(data) + "body\n"
    parsed, body = frontmatter.parse(text)
    expected = {k: (v.replace("\n", " ") if isinstance(v, str) else v) for k, v in data.items()}
    assert parsed == expected
    assert body == "body\n"
    block = text.split("---")[1]
    assert yaml.safe_load(block) == expected


def test_key_order() -> None:
    text = frontmatter.emit({"description": "d1", "name": "nm", "inclusion": "auto"})
    assert text.splitlines()[1:4] == ["inclusion: auto", "name: nm", "description: d1"]
    # YAML 1.1 booleans stay strings
    assert 'name: "n"' in frontmatter.emit({"name": "n"})


@pytest.mark.parametrize(
    "text",
    ["inclusion: always\n---\n", "---\ninclusion: always\n", "---\nbad line\n---\n", "---\nx: [a]\n---\n"],
)
def test_strict_parse_rejects(text: str) -> None:
    with pytest.raises(FrontMatterError):
        frontmatter.parse(text)
