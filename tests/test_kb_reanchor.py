"""Tests for bcp.kb_reanchor: the deterministic move-but-unchanged re-anchor (no AI, offline).

Includes a fail-on-revert check: if the re-anchor logic is reverted so a moved block is treated as
changed, ``test_moved_method_reanchors`` fails because no new range is returned.
"""
from __future__ import annotations

from bcp import kb_reanchor

METHOD = ["public void save(Widget w) {", "    check(w);", "    repo.save(w);", "}"]


def _rstrip(lines: list[str]) -> list[str]:
    return [ln.rstrip() for ln in lines]


def _ev(path: str, start: int, end: int, quote: str) -> dict:
    return {"kind": "range", "path": path, "start": start, "end": end, "quote": quote}


def test_moved_method_reanchors() -> None:
    # the method was at lines 1-4; two import lines were inserted above, so it now sits at lines 3-6
    original = _ev("A.java", 1, 4, "\n".join(METHOD))
    now = _rstrip(["import x;", "import y;", *METHOD, "", "// tail"])
    out = kb_reanchor.reanchor_evidence([original], {"A.java": now})
    assert out is not None and len(out) == 1
    assert (out[0]["start"], out[0]["end"]) == (3, 6)
    assert out[0]["quote"] == "\n".join(METHOD)


def test_changed_method_does_not_reanchor() -> None:
    original = _ev("A.java", 1, 4, "\n".join(METHOD))
    changed = _rstrip(["public void save(Widget w) {", "    audit(w);", "    repo.save(w);", "}"])  # body changed
    assert kb_reanchor.reanchor_evidence([original], {"A.java": changed}) is None


def test_ambiguous_block_does_not_reanchor() -> None:
    original = _ev("A.java", 1, 2, "a();\nb();")
    now = _rstrip(["a();", "b();", "c();", "a();", "b();"])  # the block appears twice
    assert kb_reanchor.reanchor_evidence([original], {"A.java": now}) is None


def test_missing_file_does_not_reanchor() -> None:
    original = _ev("A.java", 1, 4, "\n".join(METHOD))
    assert kb_reanchor.reanchor_evidence([original], {"A.java": None}) is None


def test_no_move_returns_none() -> None:
    # found at the same place and unchanged: nothing to re-anchor
    original = _ev("A.java", 1, 4, "\n".join(METHOD))
    assert kb_reanchor.reanchor_evidence([original], {"A.java": _rstrip(METHOD)}) is None


def test_trailing_whitespace_is_ignored_in_matching() -> None:
    original = _ev("A.java", 1, 2, "foo\nbar")
    now = _rstrip(["pad", "foo   ", "bar\t"])  # same code, moved down, trailing whitespace only
    out = kb_reanchor.reanchor_evidence([original], {"A.java": now})
    assert out is not None and (out[0]["start"], out[0]["end"]) == (2, 3)
