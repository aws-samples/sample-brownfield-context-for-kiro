"""Documentation drift: does what the docs say still match the code? (``bcp docdrift``)

Input is a folder (or glob, or file) of Markdown or text documents: repository docs today, pages
exported from a wiki (for example through the team's Confluence MCP server) later. No AI work is done
here. From each document we extract:

- code identifiers: ``Class.method``, backticked ``ClassName``, ``UPPER_SNAKE`` constants, ``/api/...``
  or ``/rest/...`` paths, and configuration keys written as ``key = value``;
- sentences with a behaviour verb or an absolute word that name an identifier ("claims for Kiro to
  check").

Checks:

- HIGH: a named class, method or constant is not found in the repository;
- MEDIUM: a documented configuration value differs from the code default; an API path is not found;
- LOW: the document is older than the code it names (read-only ``git log -1 --format=%cI``).
"""

from __future__ import annotations

import glob as globmod
import logging
import re
from pathlib import Path
from typing import Any

from bcp import drift as drift_mod
from bcp.claims import Token
from bcp.config import BcpConfig
from bcp.precheck import ABSOLUTE_RE
from bcp.programs import _strip_quotes, find_constants, find_defaults
from bcp.sourceindex import METHOD_MISSING, RESOLVED, SourceIndex
from bcp.textio import atomic_write_text, dump_json, md_cell, read_text

log = logging.getLogger(__name__)

SCHEMA_DOCDRIFT = "bcp.docdrift/1"
DOC_SUFFIXES = (".md", ".markdown", ".txt")
QUALIFIED_RE = re.compile(r"\b([A-Z][a-z]\w*)\.([a-z]\w*)\b(?!\.)")
CLASS_RE = re.compile(r"`([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)`")
CONSTANT_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
API_RE = re.compile(r"(?<![\w/])(/(?:[\w.-]+/)*(?:api|rest|ws)/[\w/{}\-.]*[\w}])")
KEY_VALUE_RE = re.compile(
    r"`?(?P<key>[a-z][\w-]*(?:\.[\w-]+)+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`?\s*"
    r"(?:=|:=|defaults? to|is set to)\s*`?(?P<value>\"[^\"]*\"|[^\s`,;)]+)`?"
)
BEHAVIOUR_RE = re.compile(
    r"\b(validates?|rejects?|throws?|creates?|deletes?|voids?|saves?|sends?|calls?|retries|retry|"
    r"rolls? back|commits?|requires?|prevents?|returns?|blocks?|allows?|denies|checks?|fails?)\b", re.I)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*(\[])")
EXTERNAL_TYPES = frozenset(
    "String Integer Long Boolean Double Float Math System Collections Arrays List Map Set Optional Objects "
    "Thread Class Object Date LocalDate LocalDateTime Instant Duration UUID StringUtils Stream Collectors "
    "Files Paths Path File BigDecimal Calendar Locale Pattern Logger LoggerFactory JSON Kiro".split()
)
FILE_SUFFIX_WORDS = frozenset(
    "md java py xml json ts js kt cs go rb sql yml yaml properties txt html css sh ps1 toml cfg ini log csv "
    "jsp gradle class this length".split()
)
MISSING_KINDS = ("missing-method", "missing-class", "missing-constant")
MAX_CLAIMS = 100
MAX_GIT_FILES = 200


def doc_files(spec: str | Path) -> list[Path]:
    """Documents from a folder (recursive), a glob, or a single file."""
    path = Path(spec)
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in DOC_SUFFIXES)
    if path.is_file():
        return [path]
    hits = sorted(Path(p) for p in globmod.glob(str(spec), recursive=True))
    return [p for p in hits if p.is_file() and p.suffix.lower() in DOC_SUFFIXES]


def extract(text: str) -> dict[str, list[dict[str, Any]]]:
    """Identifiers, key = value statements and checkable sentences, each with its 1-based line."""
    out: dict[str, list[dict[str, Any]]] = {"methods": [], "classes": [], "constants": [], "apis": [],
                                            "values": [], "claims": []}
    seen: set[tuple[str, str]] = set()

    def add(kind: str, item: dict[str, Any], key: str) -> None:
        if (kind, key) not in seen:
            seen.add((kind, key))
            out[kind].append(item)

    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        for match in KEY_VALUE_RE.finditer(line):
            value, _ = _strip_quotes(match.group("value"))
            add("values", {"key": match.group("key"), "value": value, "line": number}, f"{match.group('key')}={value}")
        for match in QUALIFIED_RE.finditer(line):
            cls, method = match.group(1), match.group(2)
            if cls not in EXTERNAL_TYPES and method not in FILE_SUFFIX_WORDS:
                add("methods", {"class": cls, "method": method, "line": number}, f"{cls}.{method}")
        for match in CLASS_RE.finditer(line):
            if match.group(1) not in EXTERNAL_TYPES:
                add("classes", {"class": match.group(1), "line": number}, match.group(1))
        for match in CONSTANT_RE.finditer(line):
            add("constants", {"name": match.group(1), "line": number}, match.group(1))
        for match in API_RE.finditer(line):
            add("apis", {"path": match.group(1), "line": number}, match.group(1))
        for sentence in SENTENCE_RE.split(line.strip()):
            names_code = QUALIFIED_RE.search(sentence) or CLASS_RE.search(sentence) or CONSTANT_RE.search(sentence)
            if names_code and (BEHAVIOUR_RE.search(sentence) or ABSOLUTE_RE.search(sentence)):
                if len(out["claims"]) < MAX_CLAIMS:
                    out["claims"].append({"text": sentence.strip()[:300], "line": number})
    return out


def _identifier_set(index: SourceIndex, regex: re.Pattern[str]) -> set[str]:
    found: set[str] = set()
    for rel in index.paths:
        for line in index.lines(rel):
            found.update(regex.findall(line))
    return found


def _api_found(index: SourceIndex, api: str) -> bool:
    static = [s for s in api.strip("/").split("/") if s and "{" not in s]
    if not static:
        return True
    tail = static[-1]
    needles = (f'"{tail}"', f"/{tail}\"", f"/{tail}/", f'"/{tail}', f"/{tail}")
    for rel in index.paths:
        for line in index.lines(rel):
            if any(n in line for n in needles):
                return True
    return False


def _git_date(path: Path) -> str | None:
    code, out = drift_mod._git(path.parent, "log", "-1", "--format=%cI", "--", path.name)
    out = out.strip()
    return (out or None) if code == 0 else None


def check_docs(docs: list[Path], repo: Path, config: BcpConfig, index: SourceIndex | None = None,
               use_git: bool = True) -> dict[str, Any]:
    """Run every check over ``docs``; returns the report data."""
    repo = Path(repo)
    index = index or SourceIndex(repo, config.source_extensions, config.exclude_dirs)
    extracted = {str(p): extract(read_text(p)) for p in docs}
    constants_in_code = _identifier_set(index, CONSTANT_RE) if any(e["constants"] for e in extracted.values()) else set()
    keys = {v["key"] for e in extracted.values() for v in e["values"]}
    literal_keys = {k for k in keys if "." in k}
    const_map = find_constants(index, config.variant_constant_patterns, literal_keys, {k for k in keys if "." not in k})
    const_to_literal = {d["constant"]: lit for lit, defs in const_map.items() for d in defs}
    wanted = literal_keys | set(const_to_literal.values())
    defaults = find_defaults(index, config.variant_default_patterns, const_to_literal, wanted)
    findings: list[dict[str, Any]] = []
    code_files: dict[str, set[str]] = {}

    def add(sev: str, kind: str, doc: str, line: int, ident: str, message: str) -> None:
        findings.append({"severity": sev, "kind": kind, "doc": doc, "line": line, "identifier": ident,
                         "message": message})

    for doc, ex in extracted.items():
        named = code_files.setdefault(doc, set())
        for item in ex["methods"]:
            res = index.resolve(Token(raw=f"{item['class']}.{item['method']}", kind="class", cls=item["class"],
                                      method=item["method"]))
            ident = f"{item['class']}.{item['method']}"
            if res.status == RESOLVED and res.path:
                named.add(res.path)
            elif res.status == METHOD_MISSING:
                add("HIGH", "missing-method", doc, item["line"], ident,
                    f"`{item['class']}` exists ({res.path}) but has no method `{item['method']}`")
                if res.path:
                    named.add(res.path)
            else:
                add("HIGH", "missing-class", doc, item["line"], ident, f"class `{item['class']}` not found in the repository")
        for item in ex["classes"]:
            hits = index.by_stem.get(item["class"], [])
            if hits:
                named.update(hits[:1])
            elif not any(m["class"] == item["class"] for m in ex["methods"]):
                add("HIGH", "missing-class", doc, item["line"], item["class"],
                    f"class `{item['class']}` not found in the repository")
        for item in ex["constants"]:
            if item["name"] not in constants_in_code and item["name"] not in keys:
                add("HIGH", "missing-constant", doc, item["line"], item["name"],
                    f"constant `{item['name']}` not found in the code")
        for item in ex["apis"]:
            if not _api_found(index, item["path"]):
                add("MEDIUM", "missing-api-path", doc, item["line"], item["path"],
                    f"no code mentions the path segment of `{item['path']}`")
        for item in ex["values"]:
            literal = item["key"] if "." in item["key"] else const_to_literal.get(item["key"])
            found = sorted(defaults.get(literal or "", []), key=lambda d: (d["pattern"], d["path"], d["line"]))
            if not found:
                continue
            default = found[0]
            if not default["expression"] and default["value"] != item["value"]:
                add("MEDIUM", "value-mismatch", doc, item["line"], item["key"],
                    f"doc says `{item['key']}` = `{item['value']}`; code default is `{default['value']}` "
                    f"({default['path']}:L{default['line']})")
    # An identifier that appears verbatim anywhere in the repository (a message key in a properties file,
    # a string literal) is not missing.
    pending = {f["identifier"] for f in findings if f["kind"] in MISSING_KINDS}
    mentioned: set[str] = set()
    if pending:
        for rel in index.paths:
            blob = "\n".join(index.lines(rel))
            for ident in pending - mentioned:
                if ident in blob:
                    mentioned.add(ident)
            if mentioned == pending:
                break
        findings = [f for f in findings if not (f["kind"] in MISSING_KINDS and f["identifier"] in mentioned)]
    git_dates: dict[str, str | None] = {}
    if use_git:
        for doc, files in code_files.items():
            doc_date = _git_date(Path(doc))
            if not doc_date:
                continue
            newer = []
            for rel in sorted(files):
                if rel not in git_dates and len(git_dates) < MAX_GIT_FILES:
                    git_dates[rel] = _git_date(repo / rel)
                when = git_dates.get(rel)
                if when and when > doc_date:
                    newer.append(f"{rel} ({when[:10]})")
            if newer:
                add("LOW", "doc-older-than-code", doc, 1, ", ".join(newer[:5]),
                    f"doc last changed {doc_date[:10]}; code it names changed later: " + ", ".join(newer[:5])
                    + (f" (+{len(newer) - 5} more)" if len(newer) > 5 else ""))
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["doc"], f["line"], f["identifier"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in order}
    return {
        "schema": SCHEMA_DOCDRIFT,
        "docs": list(extracted),
        "counts": counts,
        "findings": findings,
        "claims_to_check": [dict(c, doc=doc) for doc, ex in extracted.items() for c in ex["claims"]],
    }


def render_docdrift_md(data: dict[str, Any]) -> str:
    """Markdown documentation drift report."""
    counts = data["counts"]
    lines = [
        "# Documentation drift report",
        "",
        f"- Documents checked: {len(data['docs'])}",
        f"- Findings: HIGH {counts['HIGH']}, MEDIUM {counts['MEDIUM']}, LOW {counts['LOW']}",
        "",
        "HIGH: a named class, method or constant is not in the code. MEDIUM: a documented value differs from the "
        "code default, or an API path is not found. LOW: the document is older than the code it names. These "
        "checks are mechanical; they do not judge behaviour.",
        "",
        "## Findings",
        "",
        "| Severity | Kind | Document | Line | Identifier | Detail |",
        "|---|---|---|---|---|---|",
    ]
    for f in data["findings"]:
        lines.append(f"| {f['severity']} | {f['kind']} | {md_cell(Path(f['doc']).name)} | {f['line']} | "
                     f"`{md_cell(f['identifier'])}` | {md_cell(f['message'])} |")
    if not data["findings"]:
        lines.append("| - | - | - | - | - | no findings |")
    lines += ["", "## Claims for Kiro to check", "",
              "Sentences that state behaviour about named code. Open the code and confirm or correct each one.", ""]
    for c in data["claims_to_check"]:
        lines.append(f"- {md_cell(Path(c['doc']).name)}:L{c['line']}: {md_cell(c['text'])}")
    if not data["claims_to_check"]:
        lines.append("None found.")
    return "\n".join(lines).rstrip("\n") + "\n"


def run_docdrift(docs_spec: list[str], repo: Path, out: Path, config: BcpConfig) -> dict[str, Any]:
    """``bcp docdrift``: check the documents and write ``docdrift-report.md`` / ``.json``."""
    docs: list[Path] = []
    for spec in docs_spec:
        docs += [p for p in doc_files(spec) if p not in docs]
    if not docs:
        from bcp.errors import BcpInputError

        raise BcpInputError(f"no Markdown or text documents found in {', '.join(docs_spec)}")
    data = check_docs(docs, Path(repo), config)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "docdrift-report.md", out / "docdrift-report.json"
    atomic_write_text(md, render_docdrift_md(data))
    atomic_write_text(js, dump_json(data))
    data["written"] = [str(md), str(js)]
    return data
