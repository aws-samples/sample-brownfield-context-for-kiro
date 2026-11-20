"""Investigation of citations that do not resolve (``method-missing`` / ``unresolved``).

Each investigation tries the six checks below in order and reports what it found, so a
reviewer (Kiro or an SME) can confirm or correct the citation quickly:

1. The class file exists but the method does not: similar names in that class
   (same prefix, or edit distance <= 2).
2. The same method name is declared in another class (a possible move).
3. The token is a field, parameter or constant, not a method (declaration regex).
4. The class (or file) is missing: same simple name elsewhere, or a similar name (rename).
5. The token is not code (malformed BRE text); it is reclassified as text.
6. Drift only: read-only ``git log -S <name>`` reports when the name disappeared.

The module only reads the source index and, for step 6, runs read-only git commands.
Resolution itself stays in :mod:`bcp.sourceindex`, so a fact index can reuse both.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from bcp.claims import Token, is_non_code
from bcp.sourceindex import METHOD_MISSING, Resolution, SourceIndex

log = logging.getLogger(__name__)

try:  # rapidfuzz is optional; results are identical to the stdlib fallback.
    from rapidfuzz.distance import Levenshtein as _rf_lev  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only without rapidfuzz
    _rf_lev = None

OUTCOMES = ("moved", "renamed", "field-not-method", "deleted", "non-code")
MAX_CANDIDATES = 10
MIN_PREFIX = 4
_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "new", "synchronized", "super", "this",
    "throw", "else", "try", "do", "assert", "case", "elif", "def", "class", "print", "lambda",
}
_DECL_CALL_RE = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\s*\(")
_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (rapidfuzz when installed, else a small DP)."""
    if _rf_lev is not None:
        return int(_rf_lev.distance(a, b))
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similar(name: str, other: str) -> bool:
    if name == other:
        return False
    prefix = min(len(name), MIN_PREFIX)
    if len(name) >= MIN_PREFIX and other.lower().startswith(name[:prefix].lower()):
        return True
    return edit_distance(name.lower(), other.lower()) <= 2


def _method_decl_re(name: str) -> re.Pattern[str]:
    """A declaration (return type or ``def`` before the name), not a call."""
    esc = re.escape(name)
    return re.compile(rf"(?:\bdef\s+{esc}\s*\(|[\w>\]?]\s+{esc}\s*\()")


def _field_decl_re(name: str) -> re.Pattern[str]:
    """Field, parameter or constant declaration of ``name`` (Java/C#/Kotlin/Python shapes)."""
    esc = re.escape(name)
    return re.compile(
        rf"(?:[\w>\]?]\s+{esc}\s*[=;,)]|^\s*(?:self\.)?{esc}\s*(?::\s*[^=]+)?=(?!=)|\b(?:val|var)\s+{esc}\b)"
    )


def _check(step: int, name: str, result: str) -> dict[str, Any]:
    return {"step": step, "check": name, "result": result}


def _names_in(index: SourceIndex, rel: str) -> list[str]:
    names: set[str] = set()
    for line in index.lines(rel):
        for match in _DECL_CALL_RE.finditer(line):
            if match.group(1) not in _KEYWORDS:
                names.add(match.group(1))
    return sorted(names)


def _first_line(index: SourceIndex, rel: str, regex: re.Pattern[str]) -> int | None:
    for number, line in enumerate(index.lines(rel), start=1):
        if regex.search(line):
            return number
    return None


def _search_repo(
    index: SourceIndex, name: str, regex: re.Pattern[str], skip: str | None
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for rel in index.paths:
        if rel == skip:
            continue
        lines = index.lines(rel)
        if not any(name in line for line in lines):
            continue
        for number, line in enumerate(lines, start=1):
            if regex.search(line):
                hits.append({"path": rel, "line": number})
                break
        if len(hits) >= MAX_CANDIDATES:
            break
    return hits


def git_disappearance(repo: Path, name: str) -> str:
    """Read-only ``git log -S``: most recent commits that changed occurrences of ``name``."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "log", "-S", name, "-n", "3", "--date=short", "--format=%h %ad %s"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "git history not available (git not found or timed out)"
    if proc.returncode != 0:
        return "git history not available (not a git repository)"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return f"git log -S {name}: no commit added or removed it"
    return f"git log -S {name}: last change in {lines[0]}"


def investigate(
    index: SourceIndex,
    token: Token,
    resolution: Resolution,
    git_repo: Path | None = None,
) -> dict[str, Any]:
    """Run the six checks for one unfound token and return the investigation record.

    ``git_repo`` enables step 6 (drift only); otherwise step 6 is reported as skipped.
    """
    checks: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    outcome: str | None = None
    why = ""
    name = token.method or token.cls or (token.path or token.raw).rsplit("/", 1)[-1]
    class_file = resolution.path if resolution.status == METHOD_MISSING else None

    # Step 1: similar names in the class file.
    similar: list[dict[str, Any]] = []
    if class_file and token.method:
        for other in _names_in(index, class_file):
            if _similar(token.method, other):
                line = _first_line(index, class_file, re.compile(rf"(?<![.\w]){re.escape(other)}\s*\("))
                similar.append({"path": class_file, "line": line or 1, "reason": f"similar name {other}"})
        checks.append(
            _check(1, "similar-names-in-class", ", ".join(c["reason"][13:] for c in similar) or "none")
        )
    else:
        checks.append(_check(1, "similar-names-in-class", "skipped (no class file or no method)"))

    # Step 2: same method declared in another class.
    moved: list[dict[str, Any]] = []
    if token.method and _IDENT_RE.match(token.method):
        moved = [
            {**hit, "reason": f"{token.method} declared here"}
            for hit in _search_repo(index, token.method, _method_decl_re(token.method), class_file)
        ]
        checks.append(_check(2, "method-elsewhere", f"{len(moved)} file(s)" if moved else "none"))
    else:
        checks.append(_check(2, "method-elsewhere", "skipped (no method name)"))

    # Step 3: field, parameter or constant.
    field_hit: dict[str, Any] | None = None
    if _IDENT_RE.match(name or ""):
        regex = _field_decl_re(name)
        if class_file:
            line = _first_line(index, class_file, regex)
            if line:
                field_hit = {"path": class_file, "line": line, "reason": f"{name} declared as a field/parameter"}
        if field_hit is None and resolution.status != METHOD_MISSING:
            hits = _search_repo(index, name, regex, None)
            if hits:
                field_hit = {**hits[0], "reason": f"{name} declared as a field/constant"}
        checks.append(
            _check(3, "field-parameter-constant", f"{field_hit['path']}#L{field_hit['line']}" if field_hit else "none")
        )
    else:
        checks.append(_check(3, "field-parameter-constant", "skipped (not an identifier)"))

    # Step 4: class or file missing → rename / same name elsewhere.
    renamed: list[dict[str, Any]] = []
    if resolution.status != METHOD_MISSING:
        if token.kind == "path":
            base = (token.path or token.raw).rsplit("/", 1)[-1]
            for path in index.by_name.get(base, []):
                moved.append({"path": path, "line": 1, "reason": f"same file name {base}"})
            stem = Path(base).stem
            for other_stem, paths in sorted(index.by_stem.items()):
                if other_stem != stem and len(stem) >= 3 and edit_distance(stem.lower(), other_stem.lower()) <= 2:
                    renamed += [{"path": p, "line": 1, "reason": f"similar file {other_stem}"} for p in paths]
        elif token.cls:
            for other_stem, paths in sorted(index.by_stem.items()):
                if other_stem != token.cls and _similar_class(token.cls, other_stem):
                    renamed += [{"path": p, "line": 1, "reason": f"similar class {other_stem}"} for p in paths]
        renamed = renamed[:MAX_CANDIDATES]
        checks.append(
            _check(4, "class-renamed-or-moved", ", ".join(c["reason"] for c in renamed) or "none")
        )
    else:
        checks.append(_check(4, "class-renamed-or-moved", "skipped (class file found)"))

    # Step 5: not code at all.
    non_code = is_non_code(token)
    checks.append(_check(5, "non-code-token", "yes, reclassified as text" if non_code else "no"))

    # Step 6: git history (drift only).
    if git_repo is not None and _IDENT_RE.match(name or ""):
        checks.append(_check(6, "git-history", git_disappearance(git_repo, name)))
    else:
        checks.append(
            _check(6, "git-history", "skipped (drift runs it for HIGH findings, or for all with --git-history all)")
        )

    label = token.symbol
    if non_code:
        outcome, why = "non-code", f"`{token.raw}` is not a code location (malformed BRE text); treated as text"
    elif field_hit is not None:
        outcome = "field-not-method"
        why = f"`{label}` is a field, parameter or constant, not a method"
        candidates = [field_hit]
    elif similar:
        outcome = "renamed"
        why = f"`{label}` was not found; the class has similar names (possible rename)"
        candidates = similar + moved
    elif moved:
        outcome = "moved"
        why = f"`{label}` is not where the BRE says; the same name exists elsewhere (possible move)"
        candidates = moved
    elif renamed:
        outcome = "renamed"
        why = f"`{label}` was not found; similar names exist (possible rename)"
        candidates = similar + renamed
    else:
        outcome = "deleted"
        why = f"`{label}` was not found anywhere in the indexed source (deleted or never existed)"
    if class_file and outcome == "deleted":
        why = f"`{label}`: class file {class_file} has no such method and no similar name"
    return {
        "token": token.raw,
        "status": resolution.status,
        "outcome": outcome,
        "why": why,
        "candidates": candidates[:MAX_CANDIDATES],
        "checks": checks,
    }


def _similar_class(name: str, other: str) -> bool:
    if name.lower() == other.lower():
        return True
    if len(name) >= 5 and (name in other or other in name) and abs(len(name) - len(other)) <= 8:
        return True
    return len(name) >= 4 and edit_distance(name.lower(), other.lower()) <= 2


def why_line(inv: dict[str, Any], link: Any = None) -> str:
    """``Why not found: ...`` with ``file#Lnn`` candidates (``link(path, line)`` builds hrefs)."""
    parts = [f"Why not found: {inv.get('why')}"]
    cands = inv.get("candidates") or []
    if cands:
        rendered = []
        for cand in cands:
            text = f"{cand['path']}#L{cand.get('line') or 1}"
            rendered.append(f"[{text}]({link(cand['path'], cand.get('line') or 1)})" if link else text)
        parts.append("candidates: " + ", ".join(rendered))
    return "; ".join(parts)
