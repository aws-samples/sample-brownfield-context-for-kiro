"""Deterministic re-anchor (no AI): when a fact's cited code moved but its content is unchanged, find
the new line ranges so the fact can be kept instead of quarantined.

``bcp.kb.source_hash`` bakes the ``path#Lx-Ly`` spec into the hash, so a pure line move changes it and
``bcp kb stale`` would quarantine the fact even though the code is identical. Re-anchoring recognises
that case: for each cited range it searches the current file for the one contiguous block whose lines
(compared with trailing whitespace stripped) equal the lines the review captured. When every range is
found exactly once and at least one moved, the fact is re-anchored to the new ranges; a range that is
missing or ambiguous (zero or more than one match) is not re-anchored and the caller quarantines the
fact. The functions here are pure and offline so they are unit-testable without AWS.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def _rstrip_lines(text: str) -> list[str]:
    """Lines of ``text`` with trailing whitespace stripped and line endings normalised."""
    return [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def _needle(quote: str) -> list[str]:
    """The quoted lines to match, trailing blank line dropped (quotes are stored without a final LF)."""
    lines = _rstrip_lines(quote)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def find_blocks(haystack: Sequence[str], needle: Sequence[str]) -> list[int]:
    """Zero-based start indices of every contiguous run in ``haystack`` equal to ``needle``."""
    n = len(needle)
    if n == 0 or n > len(haystack):
        return []
    needle = list(needle)
    return [i for i in range(len(haystack) - n + 1) if list(haystack[i:i + n]) == needle]


def reanchor_range(ev: Mapping[str, Any], file_lines: Sequence[str]) -> dict[str, Any] | None:
    """A new range dict for one cited range when its quoted lines occur exactly once in ``file_lines``.

    ``file_lines`` are the current file's lines with trailing whitespace already stripped. Returns the
    evidence item with updated ``start``/``end`` (one-based, inclusive) and ``quote``, or None when the
    quote is missing, absent or ambiguous."""
    quote = ev.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return None
    needle = _needle(quote)
    hits = find_blocks(file_lines, needle)
    if len(hits) != 1:
        return None
    start = hits[0] + 1
    new = dict(ev)
    new["start"], new["end"] = start, start + len(needle) - 1
    new["quote"] = "\n".join(file_lines[hits[0]:hits[0] + len(needle)])
    return new


def reanchor_evidence(evidence: Sequence[Mapping[str, Any]],
                      lines_by_path: Mapping[str, Sequence[str] | None]) -> list[dict[str, Any]] | None:
    """Re-anchored range evidence (same length, new ranges) when every cited range is a unique unchanged
    block and at least one moved; else None (the caller quarantines).

    ``lines_by_path`` maps each cited path (forward slashes) to the current file's rstripped lines, or
    None when the file is gone."""
    ranges = [ev for ev in evidence if ev.get("kind") == "range"]
    if not ranges:
        return None
    out: list[dict[str, Any]] = []
    moved = False
    for ev in ranges:
        rel = str(ev.get("path", "")).replace("\\", "/")
        lines = lines_by_path.get(rel)
        if lines is None:
            return None
        new = reanchor_range(ev, lines)
        if new is None:
            return None
        if (int(new["start"]), int(new["end"])) != (int(ev["start"]), int(ev["end"])):
            moved = True
        out.append(new)
    return out if moved else None
