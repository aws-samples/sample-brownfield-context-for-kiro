"""Repository file index and citation resolution (resolved / method-missing / unresolved)."""

from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bcp.claims import Claim, Token, classify, is_non_code
from bcp.errors import BcpInputError
from bcp.textio import read_text, safe_join, to_posix

log = logging.getLogger(__name__)

RESOLVED = "resolved"
METHOD_MISSING = "method-missing"
UNRESOLVED = "unresolved"
BRE_DOC = "bre-doc"


@dataclass
class Resolution:
    """Result of resolving one token."""

    status: str
    path: str | None = None
    line: int | None = None
    how: str | None = None
    alternatives: int | None = None


def _preference(paths: Sequence[str]) -> list[str]:
    """Sort: paths containing ``/main/`` first, then shortest, then lexical."""
    return sorted(paths, key=lambda p: ("/main/" not in f"/{p}", len(p), p))


# ``Evidence: <path>#<start>-<end>`` (also ``#Lnn`` / ``#L12-L20`` / comma-joined ranges): the
# line-range anchor is not part of the file name, so strip it before looking the file up on disk.
_LINE_RANGE_SUFFIX_RE = re.compile(r"#L?(\d+)(?:-L?\d+)?(?:,L?\d+(?:-L?\d+)?)*$")


def _split_line_range(tok: str) -> tuple[str, int | None]:
    """Split a trailing line-range anchor off a path token; return (path, start-line or None)."""
    match = _LINE_RANGE_SUFFIX_RE.search(tok)
    if match:
        return tok[: match.start()], int(match.group(1))
    return tok, None


class SourceIndex:
    """One ``os.walk`` over the repository, keyed by file stem and file name."""

    def __init__(self, repo: Path, extensions: Sequence[str], exclude_dirs: Sequence[str]) -> None:
        self.repo = Path(repo)
        self.extensions = tuple(extensions)
        excluded = set(exclude_dirs)
        self.paths: list[str] = []
        self.by_stem: dict[str, list[str]] = {}
        self.by_name: dict[str, list[str]] = {}
        self._lines: dict[str, list[str]] = {}
        if self.repo.is_dir():
            for dirpath, dirnames, filenames in os.walk(self.repo):
                dirnames[:] = sorted(d for d in dirnames if d not in excluded)
                for name in filenames:
                    if not name.endswith(self.extensions):
                        continue
                    rel = to_posix(os.path.relpath(os.path.join(dirpath, name), self.repo))
                    self.paths.append(rel)
        self.paths.sort()
        self._path_set = set(self.paths)
        for rel in self.paths:
            name = rel.rsplit("/", 1)[-1]
            self.by_stem.setdefault(Path(name).stem, []).append(rel)
            self.by_name.setdefault(name, []).append(rel)

    def has_source(self) -> bool:
        """True when at least one indexed source file exists."""
        return bool(self.paths)

    def contains(self, rel: str) -> bool:
        """True if ``rel`` is an indexed path."""
        return rel in self._path_set

    def lines(self, rel: str) -> list[str]:
        """Cached file lines (no line endings)."""
        if rel not in self._lines:
            try:
                text = read_text(self.repo / rel)
            except OSError:
                text = ""
            self._lines[rel] = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return self._lines[rel]

    def glob_scope(self, pattern: str) -> list[str]:
        """Indexed paths matching ``pattern`` with ``fnmatch.fnmatchcase`` (``*`` crosses ``/``)."""
        pattern = to_posix(pattern)
        if pattern in self._path_set:
            return [pattern]
        return [p for p in self.paths if fnmatch.fnmatchcase(p, pattern)]

    def find_method_line(self, rel: str, method: str) -> int | None:
        """First line (1-based) matching ``(?<![.\\w])method\\s*\\(``."""
        regex = re.compile(rf"(?<![.\w]){re.escape(method)}\s*\(")
        for number, line in enumerate(self.lines(rel), start=1):
            if regex.search(line):
                return number
        return None

    def find_class(
        self,
        cls: str,
        domain_paths: Sequence[str] = (),
        manifest_paths: Sequence[str] = (),
        package_path: str | None = None,
    ) -> tuple[str | None, str]:
        """Locate a class file; returns (path, how)."""
        candidates = self.by_stem.get(cls, [])
        if not candidates:
            return None, ""
        if package_path:
            fqn_hits = [
                p for p in candidates if f"/{p.rsplit('/', 1)[0]}".endswith(f"/{package_path}")
            ]
            if fqn_hits:
                return _preference(fqn_hits)[0], "fqn-path"
        domain_set = set(domain_paths)
        for pool in (
            [p for p in candidates if p in domain_set],
            [p for p in candidates if p in set(manifest_paths)],
            candidates,
        ):
            if pool:
                return _preference(pool)[0], "class-file"
        return None, ""

    def resolve(
        self,
        token: Token,
        domain_paths: Sequence[str] = (),
        manifest_paths: Sequence[str] = (),
        bre_root: Path | None = None,
        bre_file_dir: str | None = None,
    ) -> Resolution:
        """Resolve a classified token to one of the three states (or ``bre-doc``)."""
        if token.kind == "path":
            return self._resolve_path(token.path or token.raw, bre_root, bre_file_dir)
        if token.kind not in ("fqn", "class") or not token.cls:
            return Resolution(UNRESOLVED)
        path, how = self.find_class(token.cls, domain_paths, manifest_paths, token.package_path)
        if path is None:
            return Resolution(UNRESOLVED)
        if not token.method:
            return Resolution(RESOLVED, path, 1, how if how == "fqn-path" else "class-file")
        line = self.find_method_line(path, token.method)
        if line is None:
            return Resolution(METHOD_MISSING, path, 1, "class-file")
        return Resolution(RESOLVED, path, line, "method-line")

    def _resolve_path(self, tok: str, bre_root: Path | None, bre_file_dir: str | None) -> Resolution:
        tok = to_posix(tok.strip())
        tok, line_anchor = _split_line_range(tok)
        line = line_anchor or 1
        try:
            rel = to_posix(os.path.relpath(safe_join(self.repo, tok), self.repo))
            if rel in self._path_set:
                return Resolution(RESOLVED, rel, line, "path")
        except (BcpInputError, ValueError):
            pass
        if "/" not in tok:
            hits = self.by_name.get(tok, [])
            if hits:
                ordered = _preference(hits)
                return Resolution(
                    RESOLVED, ordered[0], line, "filename-search", len(hits) if len(hits) > 1 else None
                )
        if bre_root is not None:
            tries = [tok]
            if bre_file_dir:
                joined = posixpath.normpath(posixpath.join(bre_file_dir, tok))
                if not joined.startswith(".."):
                    tries.append(joined)
            for rel in tries:
                try:
                    if safe_join(bre_root, rel).is_file():
                        return Resolution(BRE_DOC, rel, None, "bre-doc")
                except BcpInputError:
                    continue
        return Resolution(UNRESOLVED)


# --------------------------------------------------------------------------- evidence anchors
#
# An anchor lets an agent cite a reviewed range without opening the file: the enclosing type and
# member, the declaration (or first significant) line number, and that line quoted. The patterns are
# language-neutral heuristics (Java/C#/Kotlin/TypeScript/Python/Go/Perl style declarations); line
# numbers are hints as of the review.

_COMMENT_RE = re.compile(r"^\s*(?://|/\*|\*|#|--|\*/)")
_BARE_ANNOTATION_RE = re.compile(r"^\s*@\w+\s*$")
_MODIFIERS = (r"public|protected|private|internal|static|final|abstract|synchronized|native|default|"
              r"override|async|virtual|open|suspend|export")
_DECL_RES = (
    re.compile(rf"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:{_MODIFIERS})\s+)+[\w<>\[\],.?\s]*?\b(\w+)\s*\("),
    re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("),
    re.compile(r"^\s*(?:\w+\s+)*fun\s+(?:<[^>]*>\s*)?(?:\w+\.)?(\w+)\s*\("),
    re.compile(r"^\s*sub\s+(\w+)\b"),
)
# no-modifier style (package-private or interface members): ``Type name(...`` or ``Type name(...);``
_PLAIN_DECL_RE = re.compile(r"^\s*([\w<>\[\],.?]+)\s+(\w+)\s*\((.*)$")
_ABSTRACT_END_RE = re.compile(r"\)\s*(?:throws\s+[\w.,\s]+)?;\s*$")
_NOT_TYPES = {"return", "new", "throw", "else", "case", "yield", "await", "assert", "print", "echo", "not",
              "and", "or", "in", "is", "goto", "delete", "typeof", "sizeof", "raise", "import", "package"}
_TYPE_RE = re.compile(
    rf"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:{_MODIFIERS}|data|sealed|partial|abstract)\s+)*"
    r"(?:class|interface|enum|record|struct|trait|object|@interface)\s+([A-Za-z_]\w*)"
    r"|^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b"
)
_NOT_NAMES = {"if", "for", "while", "switch", "catch", "return", "new", "else", "throw", "synchronized",
              "try", "do", "case", "assert", "super", "this", "elif", "with", "print"}
ANCHOR_QUOTE_CHARS = 120
ENCLOSING_LOOKBACK = 400


def significant(line: str) -> bool:
    """True for a line worth quoting: not blank, a comment, a lone brace or a bare annotation."""
    text = line.strip()
    return bool(text) and text not in ("{", "}", "};", ")", ");") and not _COMMENT_RE.match(line) \
        and not _BARE_ANNOTATION_RE.match(line)


def declaration_name(line: str) -> str | None:
    """Name of the function or method declared on ``line``, else None."""
    if _COMMENT_RE.match(line):
        return None
    for regex in _DECL_RES:
        match = regex.match(line)
        if match and match.group(1) not in _NOT_NAMES:
            return match.group(1)
    match = _PLAIN_DECL_RE.match(line)
    if match and match.group(1) not in _NOT_TYPES and match.group(2) not in _NOT_NAMES:
        rest = match.group(3)
        if ";" not in rest or _ABSTRACT_END_RE.search(line):
            return match.group(2)
    return None


def _indent(line: str) -> int:
    return len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())


def type_name(line: str) -> str | None:
    """Name of the class/interface/enum/record/struct declared on ``line``, else None."""
    if _COMMENT_RE.match(line):
        return None
    match = _TYPE_RE.match(line)
    return (match.group(1) or match.group(2)) if match else None


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= ANCHOR_QUOTE_CHARS else text[: ANCHOR_QUOTE_CHARS - 1].rstrip() + "…"


@dataclass
class Anchor:
    """Citable anchor of one reviewed evidence range."""

    path: str
    line: int
    member: str
    quote: str
    moved: bool = False


def evidence_anchor(ev: dict[str, Any], index: "SourceIndex | None" = None) -> Anchor | None:
    """Anchor of a ``range`` evidence item from its stored quote, refined with the current source.

    The member is the first declaration inside the range, else the nearest declaration above the
    range in the current file (when ``index`` has it). The type is the nearest type declaration
    above, else the file stem. When the quoted line is no longer at its recorded line but occurs
    exactly once in the current file, the anchor uses the current line and is marked ``moved``.
    """
    if ev.get("kind") != "range" or not ev.get("path"):
        return None
    path = str(ev["path"])
    try:
        start = int(ev.get("start") or 1)
    except (TypeError, ValueError):
        return None
    quote = str(ev.get("quote") or "").replace("\r\n", "\n").split("\n")
    lines = index.lines(path) if index is not None and index.contains(path) else []
    pick, member = None, None
    for offset, text in enumerate(quote):
        name = declaration_name(text)
        if name:
            pick, member = offset, name
            break
    if pick is None:
        pick = next((i for i, text in enumerate(quote) if significant(text) and not text.lstrip().startswith("@")),
                    None)
    if pick is None:
        pick = next((i for i, text in enumerate(quote) if significant(text)), None)
    if pick is None:
        return None
    line_no, text = start + pick, quote[pick]
    moved = False
    if lines and (line_no > len(lines) or lines[line_no - 1].strip() != text.strip()):
        hits = [n for n, cur in enumerate(lines, start=1) if cur.strip() == text.strip()]
        if len(hits) == 1:
            line_no, moved = hits[0], True
    if member is None and lines:
        depth = _indent(text)
        for n in range(min(line_no, len(lines)) - 2, max(-1, line_no - 1 - ENCLOSING_LOOKBACK), -1):
            if _indent(lines[n]) >= depth or not lines[n].strip():
                continue
            name = declaration_name(lines[n])
            if name:
                member = name
                break
            if type_name(lines[n]):
                break
    # owner: the nearest type declared above with less indentation (a sibling nested type is skipped)
    owner = None
    source = lines[: min(line_no, len(lines))] if lines else quote[: pick + 1]
    depth = _indent(text)
    for cur in reversed(source[:-1] if source and source[-1].strip() == text.strip() else source):
        name = type_name(cur)
        if name and _indent(cur) < depth:
            owner = name
            break
    if owner is None and type_name(text):
        owner = type_name(text)
    owner = owner or Path(path).stem
    label = f"{owner}.{member}" if member and member != owner else owner
    return Anchor(path, line_no, label, _clip(text), moved)


@dataclass
class ClaimResolution:
    """Candidates and leftovers for one claim."""

    candidates: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    bre_refs: list[str] = field(default_factory=list)
    text_tokens: list[str] = field(default_factory=list)
    # Tokens that need an investigation: explicit method-missing candidates, unresolved
    # tokens, and non-code tokens reclassified as text. Not serialized directly.
    missing: list[tuple[Token, Resolution]] = field(default_factory=list)


def candidate_dict(token: Token, res: Resolution) -> dict[str, Any]:
    """Worksheet candidate entry."""
    entry: dict[str, Any] = {
        "token": token.raw,
        "kind": token.kind,
        "bound": token.bound,
        "path": res.path,
        "line": res.line,
        "symbol": token.symbol,
        "how": res.how,
        "status": res.status,
    }
    if token.cls:
        entry["class"] = token.cls
    if token.method:
        entry["method"] = token.method
    if token.package_path:
        entry["package_path"] = token.package_path
    if res.alternatives:
        entry["alternatives"] = res.alternatives
    return entry


def token_from_entry(entry: dict[str, Any], extensions: Sequence[str]) -> Token:
    """Rebuild a token from a stored candidate/baseline entry."""
    kind = entry.get("kind")
    raw = str(entry.get("token", ""))
    if kind == "path":
        return Token(raw=raw, kind="path", path=raw.strip())
    if kind in ("fqn", "class") and entry.get("class"):
        return Token(
            raw=raw,
            kind=kind,
            cls=entry.get("class"),
            method=entry.get("method"),
            package_path=entry.get("package_path"),
            bound=bool(entry.get("bound")),
        )
    return classify(raw, list(extensions), None)


def resolve_claim(
    index: SourceIndex,
    claim: Claim,
    domain_paths: Sequence[str],
    manifest_paths: Sequence[str],
    bre_root: Path | None = None,
    bre_file_dir: str | None = None,
) -> ClaimResolution:
    """Resolve every location token of a claim, applying the reclassification rules."""
    out = ClaimResolution()
    for token in claim.tokens:
        if token.kind == "text":
            out.text_tokens.append(token.raw)
            continue
        res = index.resolve(token, domain_paths, manifest_paths, bre_root, bre_file_dir)
        if res.status == BRE_DOC:
            if res.path and res.path not in out.bre_refs:
                out.bre_refs.append(res.path)
            continue
        if token.bound and res.status != RESOLVED:
            out.text_tokens.append(token.raw)
            continue
        if res.status == UNRESOLVED:
            if is_non_code(token):
                out.text_tokens.append(token.raw)
            else:
                out.unresolved.append(token.raw)
            out.missing.append((token, res))
            continue
        if res.status == METHOD_MISSING:
            out.missing.append((token, res))
        out.candidates.append(candidate_dict(token, res))
    return out


def resolution_ratio(candidates: Sequence[dict[str, Any]], unresolved: Sequence[str]) -> float:
    """``resolved / (resolved + method-missing + unresolved)``; 1.0 when nothing to resolve."""
    resolved = sum(1 for c in candidates if c.get("status") == RESOLVED)
    missing = sum(1 for c in candidates if c.get("status") == METHOD_MISSING)
    total = resolved + missing + len(unresolved)
    return 1.0 if total == 0 else resolved / total
