"""Parse ``## BR-n`` rule sections and their ``Source``/``Evidence`` citations from business-rules.md."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from bcp.config import DEFAULT_EXTENSIONS, DEFAULT_RULE_HEADING, DEFAULT_SOURCE_LINE
from bcp.textio import sha256_text

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"`([^`]+)`")
FQN_RE = re.compile(r"^((?:[a-z_][\w]*\.)+)([A-Z][\w$]*)(?:[.#]([a-zA-Z_]\w*))?(?:\(.*\))?$")
CLASS_RE = re.compile(r"^([A-Z][\w$]*)(?:[.#]([a-zA-Z_]\w*))?$")
BARE_RE = re.compile(r"^([a-z_]\w*)(?:\(.*\))?$")
ARGS_RE = re.compile(r"\(.*\)$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


@dataclass
class Token:
    """One backticked token from a citation, classified by the ordered rules."""

    raw: str
    kind: str  # path | fqn | class | text
    path: str | None = None
    cls: str | None = None
    method: str | None = None
    package_path: str | None = None
    bound: bool = False

    @property
    def symbol(self) -> str:
        """Human-readable symbol: path, ``Class`` or ``Class.method``."""
        if self.kind == "path":
            return self.path or self.raw
        if self.cls and self.method:
            return f"{self.cls}.{self.method}"
        return self.cls or self.raw


@dataclass
class Claim:
    """One rule section."""

    rule_id: str
    title: str
    text: str
    body_sha256: str
    citations: list[str] = field(default_factory=list)
    tokens: list[Token] = field(default_factory=list)

    @property
    def number(self) -> int:
        """Numeric part of the rule id (for sorting)."""
        digits = re.sub(r"\D", "", self.rule_id)
        return int(digits) if digits else 0


def normalize_section(text: str) -> str:
    """Universal newlines, rstrip lines, drop trailing blank lines, join with LF."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def body_sha256(section_text: str) -> str:
    """Hash that ties a verdict to the exact claim text."""
    return sha256_text(normalize_section(section_text))


def classify(raw: str, extensions: list[str], bound_cls: tuple[str, str | None] | None) -> Token:
    """Classify one token by rules 1–5; ``bound_cls`` is (class, package_path) for bare methods."""
    tok = raw.strip()
    if "/" in tok or any(tok.endswith(ext) for ext in extensions):
        return Token(raw=raw, kind="path", path=tok)
    match = FQN_RE.match(tok)
    if match:
        package = match.group(1).rstrip(".").replace(".", "/")
        return Token(raw=raw, kind="fqn", cls=match.group(2), method=match.group(3), package_path=package)
    stripped = ARGS_RE.sub("", tok)
    match = CLASS_RE.match(stripped)
    if match:
        return Token(raw=raw, kind="class", cls=match.group(1), method=match.group(2))
    match = BARE_RE.match(tok)
    if match and bound_cls is not None:
        return Token(
            raw=raw,
            kind="class",
            cls=bound_cls[0],
            method=match.group(1),
            package_path=bound_cls[1],
            bound=True,
        )
    return Token(raw=raw, kind="text")


# A path, optionally followed by a line-range anchor the BRE's rule 5 mandates
# (``Evidence: <path>#<start>-<end>``), also ``#Lnn``/``#L12-L20`` and comma-joined ranges.
_LINE_RANGE_SUFFIX = r"(?:#L?\d+(?:-L?\d+)?(?:,L?\d+(?:-L?\d+)?)*)?"
_PATH_OK_RE = re.compile(r"^[\w./@+$-]+" + _LINE_RANGE_SUFFIX + r"$")
_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")


def is_non_code(token: Token) -> bool:
    """True when a path/class token cannot be a code location (spaces, operators, prose)."""
    raw = token.raw.strip()
    if token.kind == "path":
        return not _PATH_OK_RE.match(raw)
    if token.kind in ("fqn", "class"):
        return not _IDENT_RE.match(token.cls or "") or (
            token.method is not None and not _IDENT_RE.match(token.method)
        )
    return True


def tokenize(citation: str, extensions: list[str]) -> list[Token]:
    """Extract and classify backticked tokens of one citation in order."""
    tokens: list[Token] = []
    bound: tuple[str, str | None] | None = None
    for raw in TOKEN_RE.findall(citation):
        token = classify(raw, extensions, bound)
        if token.kind in ("fqn", "class") and not token.bound and token.cls:
            bound = (token.cls, token.package_path)
        tokens.append(token)
    return tokens


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def extract_citations(lines: list[str], source_re: re.Pattern[str]) -> list[str]:
    """Return the citation texts of a section, joining continuation lines."""
    citations: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            index += 1
            continue
        match = None if in_fence else source_re.match(line)
        if not match:
            index += 1
            continue
        marker_indent = _indent(line)
        parts = [match.group(1).strip()]
        index += 1
        while index < len(lines):
            nxt = lines[index]
            stripped = nxt.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("```"):
                break
            if _indent(nxt) <= marker_indent:
                break
            if BULLET_RE.match(nxt) and _indent(nxt) <= marker_indent:
                break
            parts.append(stripped)
            index += 1
        citations.append(" ".join(parts))
    return citations


def parse(
    text: str,
    heading_re: re.Pattern[str] | None = None,
    source_re: re.Pattern[str] | None = None,
    extensions: list[str] | None = None,
) -> list[Claim]:
    """Parse rule sections from Markdown text; duplicate ids keep the first (WARN)."""
    heading_re = heading_re or re.compile(DEFAULT_RULE_HEADING)
    source_re = source_re or re.compile(DEFAULT_SOURCE_LINE)
    extensions = extensions if extensions is not None else list(DEFAULT_EXTENSIONS)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    claims: list[Claim] = []
    seen: set[str] = set()
    index = 0
    while index < len(lines):
        match = heading_re.match(lines[index])
        if not match:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        section = lines[index:end]
        rule_id, title = match.group(1), (match.group(2) or "").strip()
        index = end
        if rule_id in seen:
            log.warning("duplicate rule id %s ignored (first occurrence kept)", rule_id)
            continue
        seen.add(rule_id)
        section_text = "\n".join(section)
        citations = extract_citations(section[1:], source_re)
        tokens: list[Token] = []
        for citation in citations:
            tokens.extend(tokenize(citation, extensions))
        claims.append(
            Claim(
                rule_id=rule_id,
                title=title,
                text=normalize_section(section_text),
                body_sha256=body_sha256(section_text),
                citations=citations,
                tokens=tokens,
            )
        )
    return claims
