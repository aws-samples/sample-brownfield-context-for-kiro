"""``bcp precheck``: cheap mechanical checks on extracted rules before review (no LLM).

Per claim it flags absolute words, the forced-check topics (authorization, transactions, concurrency,
counts, diagrams), re-checks "unguarded" claims against the annotations on the cited method in the
implementation and every type it implements (annotations may span lines), resolves cited methods to
file and line ranges, and clusters near-duplicate sentences elsewhere in the extraction tree. The
result feeds the review, the SME workbook and the errata.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bcp.bre import BreLayout
from bcp.claims import Claim, Token, tokenize
from bcp.config import BcpConfig
from bcp.errors import BcpInputError
from bcp.lang import Annotation, for_path
from bcp.manifest import load_manifest
from bcp.review import git_head, now_iso
from bcp.sourceindex import RESOLVED, SourceIndex
from bcp.textio import atomic_write_text, dump_json, md_cell, read_text

log = logging.getLogger(__name__)

SCHEMA_PRECHECK = "bcp.precheck/1"
PRECHECK_DIR = "precheck"
ABSOLUTE_RE = re.compile(
    r"\b(only|every|all|never|always|unique|none|cannot|must not|does not exist|absent|solely|sole|"
    r"exclusively|no\s+[A-Za-z@`][\w@`-]*)\b",
    re.I,
)
TOPIC_RES: dict[str, re.Pattern[str]] = {
    "authorization": re.compile(
        r"\b(authori[sz]\w*|privilege\w*|permission\w*|PreAuthorize|PostAuthorize|Secured|RolesAllowed|"
        r"access control|unguarded|guard(?:ed|s)?)\b", re.I),
    "transaction": re.compile(
        r"\b(transaction\w*|Transactional|rollback\w*|roll(?:s|ed)? back|commit\w*|flush\w*|atomic\w*)\b", re.I),
    "concurrency": re.compile(
        r"\b(concurren\w*|lock(?:s|ed|ing)?|synchroni[sz]\w*|thread\w*|race condition|parallel\w*|mutex|"
        r"optimistic|pessimistic|deadlock\w*)\b", re.I),
    "count": re.compile(
        r"\b(?:exactly|at most|at least)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b|"
        r"\b(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\s+(?:\w+\s+)?(?:rules?|methods?|classes|"
        r"handlers?|steps?|fields?|tables?|types?|states?|values?|checks?|privileges?|calls?|places|ways|"
        r"cases|options|levels|endpoints?|properties|services|statuses|columns|events|tasks)\b", re.I),
    "diagram": re.compile(r"\b(diagram\w*|sequence|flowchart|mermaid|state machine)\b", re.I),
}
UNGUARDED_RE = re.compile(
    r"\bunguarded\b|\bnot\s+(?:guarded|protected|secured)\b|"
    r"\bno\s+(?:@?(?:PreAuthorize|PostAuthorize|Secured|RolesAllowed|Authorized)\b|"
    r"(?:privilege|permission|guard|authori[sz]ation|security)\s*(?:check|annotation|guard)?s?\b)|"
    r"\bwithout\s+(?:any\s+|an?\s+)?(?:@?\w*\s*)?(?:authori[sz]ation|privilege|permission|guard)|"
    r"\bdoes\s+not\s+(?:check|require|enforce)\s+(?:a\s+|any\s+)?(?:privilege|permission|authori[sz]ation)",
    re.I,
)
WORD_RE = re.compile(r"[A-Za-z_][\w$]*(?:[.#][A-Za-z_][\w$]*)*")
STOPWORDS = frozenset(
    "the and for with that this from are was were when then not but its has have been into each any all "
    "only also than they them their there which while where what who whom will would can could should may "
    "might must does did done such other these those after before over under between through about above "
    "below because both either neither more most less least same very just still even upon onto within "
    "without".split()
)
RARE_DF = 50
MIN_SHARED_RARE = 2
MIN_JACCARD = 0.6
MIN_UNIT_TOKENS = 4
MAX_CLUSTER_HITS = 25


# --------------------------------------------------------------------------- text helpers


def _code_mask(lines: list[str]) -> list[bool]:
    """True for lines inside a fenced code block (fence lines included)."""
    out, inside = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            out.append(True)
            inside = not inside
            continue
        out.append(inside)
    return out


def absolutes(text: str, first_line: int = 1) -> list[dict[str, Any]]:
    """Absolute-word hits: ``{word, line, text, in_code_block}`` (line numbers from ``first_line``)."""
    lines = text.split("\n")
    in_code = _code_mask(lines)
    out = []
    for offset, line in enumerate(lines):
        if offset == 0:
            continue  # the heading
        for match in ABSOLUTE_RE.finditer(line):
            out.append({"word": " ".join(match.group(1).lower().split()), "line": first_line + offset,
                        "text": line.strip()[:200], "in_code_block": in_code[offset]})
    return out


def topics(text: str) -> list[str]:
    """Forced-check topic families the rule text touches."""
    return [name for name, regex in TOPIC_RES.items() if regex.search(text)]


def tokens_of(text: str) -> frozenset[str]:
    """Normalised token set: lower-case words of 3+ characters (code identifiers kept whole), no stopwords."""
    out = set()
    for raw in WORD_RE.findall(text.replace("`", " ")):
        word = raw.lower().strip(".#")
        if len(word) >= 3 and word not in STOPWORDS:
            out.add(word)
    return frozenset(out)


BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)]|>|#+)\s*")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*(\[])")


def units(text: str) -> list[tuple[int, str]]:
    """Sentences and bullets of a Markdown text as (1-based line, text); fences and tables skipped."""
    out = []
    lines = text.replace("\r\n", "\n").split("\n")
    in_code = _code_mask(lines)
    for number, line in enumerate(lines, start=1):
        if in_code[number - 1] or line.lstrip().startswith("|"):
            continue
        body = BULLET_PREFIX_RE.sub("", line).strip()
        if not body:
            continue
        for part in SENTENCE_SPLIT_RE.split(body):
            part = part.strip()
            if part:
                out.append((number, part))
    return out


@dataclass
class Unit:
    """One sentence or bullet of the extraction tree."""

    path: str
    line: int
    text: str
    tokens: frozenset[str]


class ClusterIndex:
    """Inverted index over every sentence of the extraction tree (rare tokens only)."""

    def __init__(self, layout: BreLayout) -> None:
        self.units: list[Unit] = []
        for path in layout.markdown_files():
            rel = layout.rel(path)
            try:
                text = read_text(path)
            except OSError:
                continue
            for line, part in units(text):
                toks = tokens_of(part)
                if len(toks) >= MIN_UNIT_TOKENS:
                    self.units.append(Unit(rel, line, part, toks))
        df: dict[str, int] = {}
        for unit in self.units:
            for tok in unit.tokens:
                df[tok] = df.get(tok, 0) + 1
        self.rare = {tok for tok, count in df.items() if count <= RARE_DF}
        self.postings: dict[str, list[int]] = {}
        for i, unit in enumerate(self.units):
            for tok in unit.tokens & self.rare:
                self.postings.setdefault(tok, []).append(i)

    def similar(self, text: str, exclude_path: str | None) -> list[dict[str, Any]]:
        """Near-duplicates of each sentence of ``text`` outside ``exclude_path``."""
        hits: dict[tuple[str, int], dict[str, Any]] = {}
        for _line, part in units(text):
            toks = tokens_of(part)
            if len(toks) < MIN_UNIT_TOKENS:
                continue
            shared: dict[int, int] = {}
            for tok in toks & self.rare:
                for i in self.postings.get(tok, []):
                    shared[i] = shared.get(i, 0) + 1
            for i, count in shared.items():
                if count < MIN_SHARED_RARE:
                    continue
                unit = self.units[i]
                if unit.path == exclude_path:
                    continue
                jac = len(toks & unit.tokens) / len(toks | unit.tokens)
                if jac < MIN_JACCARD:
                    continue
                key = (unit.path, unit.line)
                if key not in hits or hits[key]["jaccard"] < jac:
                    hits[key] = {"path": unit.path, "line": unit.line, "text": unit.text[:200],
                                 "jaccard": round(jac, 2), "matches": part[:200]}
        ordered = sorted(hits.values(), key=lambda h: (-h["jaccard"], h["path"], h["line"]))
        return ordered[:MAX_CLUSTER_HITS]


# --------------------------------------------------------------------------- code checks


def _guard_re(names: list[str]) -> re.Pattern[str]:
    return re.compile(r"^(?:" + "|".join(re.escape(n) for n in names) + r")$")


ITEM_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
CODE_CALL_RE = re.compile(r"(?<![\w.])([a-z_]\w*)\s*\(")
CODE_QUALIFIED_RE = re.compile(r"\b([A-Z]\w*)\.([a-z_]\w*)\s*\(")
BARE_NAME_RE = re.compile(r"^([a-z_]\w*)(?:\(.*\))?$")
NONE_CELL_RE = re.compile(r"\|\s*(?:none|no|unguarded|not guarded|-|—)\s*\|?\s*$", re.I)
EMPHASIS_RE = re.compile(r"\*+|`")
TYPE_NAME_RE = re.compile(r"^[A-Z]\w*[a-z]\w*$")  # a type name, not a CONSTANT
TABLE_RULE_RE = re.compile(r"^\|[\s:|-]+\|?$")
CODE_WORDS = frozenset({"if", "for", "while", "switch", "catch", "return", "new", "throw", "and", "or", "not"})


@dataclass
class Item:
    """One logical piece of a rule: a prose paragraph or list item (continuations joined), a table
    row, or one code-block line, with the method tokens it names."""

    kind: str  # prose | row | code
    text: str
    tokens: list[Token]


def _split_items(text: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    cur: list[str] = []
    in_code = False

    def flush() -> None:
        if cur:
            items.append(("prose", " ".join(cur)))
            cur.clear()

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code:
            if stripped:
                items.append(("code", line))
            continue
        if not stripped:
            flush()
        elif stripped.startswith("|"):
            flush()
            if not TABLE_RULE_RE.match(stripped):  # skip |---|---| separator rows
                items.append(("row", stripped))
        elif ITEM_BULLET_RE.match(line) or stripped.startswith("#"):
            flush()
            cur.append(stripped)
        else:
            cur.append(stripped)
    flush()
    return items


def _anchor(claim: Claim, extensions: list[str]) -> str | None:
    """The class bare method names bind to: the first cited class, else the stem of the first cited
    source file."""
    body: list[Token] = []
    for _kind, part in _split_items(claim.text):
        body += tokenize(part, extensions)
    for tokens in (list(claim.tokens), body):  # the Source lines first, then the rest of the rule
        for tok in tokens:
            if tok.kind in ("fqn", "class") and tok.cls and not tok.bound and TYPE_NAME_RE.match(tok.cls):
                return tok.cls
        for tok in tokens:
            path = tok.path or ""
            if tok.kind == "path" and "/" in path and any(path.endswith(e) for e in extensions):
                return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return None


def claim_items(claim: Claim, extensions: list[str]) -> list[Item]:
    """The rule split into items, each with its ``Class.method`` tokens. Bare method names (backticked
    or called in a code line) bind to :func:`_anchor`."""
    anchor = _anchor(claim, extensions)
    out = []
    for kind, body in _split_items(claim.text):
        toks: list[Token] = []
        if kind == "code":
            code = body.split("--", 1)[0].split("//", 1)[0]
            for cls, method in CODE_QUALIFIED_RE.findall(code):
                toks.append(Token(raw=f"{cls}.{method}", kind="class", cls=cls, method=method))
            qualified = {m for _c, m in CODE_QUALIFIED_RE.findall(code)}
            for name in CODE_CALL_RE.findall(code):
                if anchor and name not in qualified and name not in CODE_WORDS:
                    toks.append(Token(raw=name, kind="class", cls=anchor, method=name, bound=True))
        else:
            for tok in tokenize(body, extensions):
                if tok.kind in ("fqn", "class") and tok.cls and tok.method:
                    toks.append(tok)
                elif tok.kind == "text" and anchor:
                    match = BARE_NAME_RE.match(tok.raw.strip())
                    if match:
                        toks.append(Token(raw=tok.raw, kind="class", cls=anchor, method=match.group(1), bound=True))
        out.append(Item(kind, body, toks))
    return out


def _dedup(tokens: list[Token]) -> list[Token]:
    seen, out = set(), []
    for tok in tokens:
        key = (tok.cls, tok.method)
        if tok.cls and tok.method and key not in seen:
            seen.add(key)
            out.append(tok)
    return out


def _method_tokens(claim: Claim, extensions: list[str]) -> list[Token]:
    """Every ``Class.method`` token in the rule (Source lines, inline backticks, code lines), first seen
    first."""
    tokens = [t for t in claim.tokens if t.kind in ("fqn", "class") and t.method]
    for item in claim_items(claim, extensions):
        tokens += item.tokens
    return _dedup(tokens)


def unguarded_re(guard_names: list[str]) -> re.Pattern[str]:
    """"No guard" wording, including the configured guard annotation names."""
    names = "|".join(re.escape(n) for n in sorted(set(guard_names) | {"PreAuthorize", "Authorized"}))
    return re.compile(
        UNGUARDED_RE.pattern + rf"|\b(?:no|neither|nor|without)\s+(?:an?\s+|any\s+)?@?(?:{names})\b", re.I)


def guarded_methods(claim: Claim, extensions: list[str], guard_names: list[str]) -> tuple[bool, list[Token]]:
    """(the rule claims something is unguarded, the methods that claim is about). The methods come from
    the items that carry the "no guard" wording (a table row whose guard cell is "none" counts); when
    those items name no method, the rule's Source-line methods are used."""
    regex = unguarded_re(guard_names)
    about_guards = bool(TOPIC_RES["authorization"].search(claim.text))
    claimed = False
    tokens: list[Token] = []
    for item in claim_items(claim, extensions):
        clean = EMPHASIS_RE.sub("", item.text)
        if regex.search(clean) or (about_guards and item.kind == "row" and NONE_CELL_RE.search(clean)):
            claimed = True
            tokens += item.tokens
    if claimed and not tokens:
        tokens = [t for t in claim.tokens if t.kind in ("fqn", "class") and t.method]
    return claimed, _dedup(tokens)


def _related_types(index: SourceIndex, path: str, depth: int = 2) -> list[str]:
    """Files of the types ``path`` implements or extends (``depth`` levels), plus ``<Name>Impl``."""
    out: list[str] = []
    frontier = [path]
    for _ in range(depth):
        nxt = []
        for rel in frontier:
            lang = for_path(rel)
            for name in lang.implemented_types(index.lines(rel)):
                cand, _how = index.find_class(name)
                if cand and cand not in out and cand != path:
                    out.append(cand)
                    nxt.append(cand)
        frontier = nxt
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    impl, _how = index.find_class(f"{stem}Impl")
    if impl and impl not in out and impl != path:
        out.append(impl)
    return out


def _ann_hits(rel: str, anns: list[Annotation], guard: re.Pattern[str], method: str) -> list[dict[str, Any]]:
    return [{"path": rel, "start": a.start, "end": a.end, "annotation": a.name, "text": a.text[:300],
             "method": method} for a in anns if guard.match(a.name)]


def method_citations(index: SourceIndex, token: Token, domain_paths: list[str],
                     manifest_paths: list[str]) -> tuple[str | None, list[tuple[int, int]]]:
    """(path, [(start, end)]) for a ``Class.method`` token, one range per declaration found."""
    res = index.resolve(token, domain_paths, manifest_paths)
    if res.status != RESOLVED or not res.path or not token.method:
        return None, []
    lang = for_path(res.path)
    lines = index.lines(res.path)
    decls = lang.method_decls(lines, token.method) or ([res.line] if res.line else [])
    return res.path, [lang.method_range(lines, d) for d in decls]


def guard_check(index: SourceIndex, claim: Claim, tokens: list[Token], domain_paths: list[str],
                manifest_paths: list[str], guard_names: list[str]) -> dict[str, Any]:
    """Re-check an "unguarded" claim against method and class annotations of the cited method in its
    file and in every related type (interfaces, super types, ``<Name>Impl``)."""
    guard = _guard_re(guard_names)
    hits: list[dict[str, Any]] = []
    checked: list[str] = []
    for tok in tokens:
        path, _ranges = method_citations(index, tok, domain_paths, manifest_paths)
        if path is None:
            continue
        for rel in [path] + _related_types(index, path):
            lang = for_path(rel)
            lines = index.lines(rel)
            decls = lang.method_decls(lines, str(tok.method))
            if not decls and rel != path:
                continue
            checked.append(f"{rel}#{tok.method}")
            for decl in decls:
                hits += _ann_hits(rel, lang.annotations_before(lines, decl), guard, str(tok.method))
            if decls:
                hits += _ann_hits(rel, lang.class_annotations(lines), guard, f"{tok.method} (class-level)")
    if not checked:
        return {"status": "not-checked", "checked": [], "hits": []}
    unique = {(h["path"], h["start"], h["end"], h["annotation"]): h for h in hits}
    hits = sorted(unique.values(), key=lambda h: (h["path"], h["start"]))
    return {"status": "contradicted" if hits else "consistent", "checked": checked, "hits": hits}


def risk_score(entry: dict[str, Any]) -> float:
    """Absolutes (prose 1.0, code 0.5, cap 4) + 1 per topic + 3 if guard-contradicted + 1 if clustered."""
    absolute = sum(0.5 if a["in_code_block"] else 1.0 for a in entry.get("absolutes", []))
    score = min(absolute, 4.0) + len(entry.get("topics", []))
    if (entry.get("guard_check") or {}).get("status") == "contradicted":
        score += 3
    if entry.get("cluster"):
        score += 1
    return round(score, 2)


def flags_line(entry: dict[str, Any] | None) -> str:
    """Short human-readable flags (workbook, prompts)."""
    if not entry:
        return "no precheck"
    parts = []
    guard = (entry.get("guard_check") or {}).get("status")
    if guard == "contradicted":
        where = ", ".join(f"{h['path']}:L{h['start']}-L{h['end']} @{h['annotation']}"
                          for h in entry["guard_check"]["hits"][:3])
        parts.append(f"GUARD CONTRADICTED ({where})")
    words = sorted({a["word"] for a in entry.get("absolutes", []) if not a["in_code_block"]})
    if words:
        parts.append("absolutes: " + ", ".join(words[:8]))
    if entry.get("topics"):
        parts.append("topics: " + ", ".join(entry["topics"]))
    if entry.get("cluster"):
        parts.append(f"repeated in {len({h['path'] for h in entry['cluster']})} other file(s)")
    parts.append(f"risk {entry.get('risk_score', 0)}")
    return "; ".join(parts)


# --------------------------------------------------------------------------- run


def _rule_first_line(file_lines: list[str], heading: str) -> int:
    for number, line in enumerate(file_lines, start=1):
        if line.rstrip() == heading.rstrip():
            return number
    return 1


def run_precheck(bre: Path, repo: Path, out: Path, domains: list[str] | None, config: BcpConfig) -> dict[str, Any]:
    """Run every check for the selected domains; write ``<out>/precheck/precheck.json`` and ``.md``."""
    bre, repo, out = Path(bre), Path(repo), Path(out)
    manifest = load_manifest(bre)
    layout = BreLayout(bre)
    selected = domains or manifest.slugs
    for slug in selected:
        if manifest.domain(slug) is None:
            raise BcpInputError(f"unknown domain slug: {slug}")
    exclude = list(config.exclude_dirs)
    try:
        exclude.append(bre.resolve().relative_to(repo.resolve()).parts[0])
    except (ValueError, IndexError):
        pass
    index = SourceIndex(repo, config.source_extensions, exclude)
    clusters = ClusterIndex(layout)
    manifest_paths = [p for d in manifest.domains for p in d.source_paths]
    claims_out: dict[str, Any] = {}
    for slug in selected:
        domain = manifest.domain(slug)
        assert domain is not None
        parsed, bre_file = layout.load_claims(slug, config)
        file_lines = read_text(bre / bre_file).replace("\r\n", "\n").split("\n") if bre_file else []
        for claim in parsed:
            first = _rule_first_line(file_lines, claim.text.split("\n", 1)[0])
            toks = _method_tokens(claim, config.source_extensions)
            citations = []
            for tok in toks:
                path, ranges = method_citations(index, tok, domain.source_paths, manifest_paths)
                for start, end in ranges:
                    citations.append({"token": tok.raw, "path": path, "start": start, "end": end})
            entry: dict[str, Any] = {
                "domain": slug,
                "rule_id": claim.rule_id,
                "title": claim.title,
                "body_sha256": claim.body_sha256,
                "bre_file": bre_file,
                "line": first,
                "absolutes": absolutes(claim.text, first),
                "topics": topics(claim.text),
                "citations": citations,
                "cluster": clusters.similar("\n".join(claim.text.split("\n")[1:]), bre_file),
            }
            claimed, guarded = guarded_methods(claim, config.source_extensions, config.guard_annotations)
            if claimed:
                entry["guard_check"] = guard_check(index, claim, guarded, domain.source_paths, manifest_paths,
                                                   config.guard_annotations)
            entry["risk_score"] = risk_score(entry)
            claims_out[f"{slug}/{claim.rule_id}"] = entry
    data = {
        "schema": SCHEMA_PRECHECK,
        "generated_at": now_iso(),
        "repo_head": git_head(repo),
        "bre_root": bre.name,
        "domains": list(selected),
        "guard_annotations": list(config.guard_annotations),
        "claims": claims_out,
        "summary": summarize(claims_out),
    }
    folder = out / PRECHECK_DIR
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_text(folder / "precheck.json", dump_json(data))
    atomic_write_text(folder / "precheck.md", render_precheck_md(data))
    data["written"] = [str(folder / "precheck.json"), str(folder / "precheck.md")]
    return data


def summarize(claims: dict[str, Any]) -> dict[str, int]:
    """Counts for the CLI line."""
    return {
        "claims": len(claims),
        "with_absolutes": sum(1 for e in claims.values() if any(not a["in_code_block"] for a in e["absolutes"])),
        "guard_contradictions": sum(1 for e in claims.values()
                                    if (e.get("guard_check") or {}).get("status") == "contradicted"),
        "guard_checked": sum(1 for e in claims.values() if "guard_check" in e),
        "clustered": sum(1 for e in claims.values() if e["cluster"]),
    }


def render_precheck_md(data: dict[str, Any]) -> str:
    """Per-domain tables sorted by risk, guard contradictions first."""
    claims = data["claims"]
    summary = data["summary"]
    lines = [
        "# Precheck report",
        "",
        "Mechanical checks, no AI. A flag is a reason to look, not a verdict.",
        "",
        f"- Generated: {data['generated_at']}; repo HEAD: {data.get('repo_head') or 'unknown'}",
        f"- Claims: {summary['claims']}; with absolute words: {summary['with_absolutes']}; "
        f"\"unguarded\" claims checked: {summary['guard_checked']}; guard contradictions: "
        f"{summary['guard_contradictions']}; repeated elsewhere: {summary['clustered']}",
        f"- Guard annotations: {', '.join('@' + n for n in data.get('guard_annotations', []))}",
        "",
        "## Guard contradictions",
        "",
        "The rule says the method is unguarded, but a guard annotation was found on the method or its class, in "
        "the implementation or a type it implements.",
        "",
    ]
    contra = [(cid, e) for cid, e in claims.items() if (e.get("guard_check") or {}).get("status") == "contradicted"]
    if not contra:
        lines.append("None.")
    for cid, e in sorted(contra):
        where = "; ".join(f"`{h['path']}:L{h['start']}-L{h['end']}` {h['text']}" for h in e["guard_check"]["hits"][:5])
        lines.append(f"- {cid}: {e['title']} — {where}")
    for slug in data.get("domains", []):
        rows = sorted(((cid, e) for cid, e in claims.items() if e["domain"] == slug),
                      key=lambda x: (-x[1]["risk_score"], x[1]["rule_id"]))
        lines += ["", f"## Domain `{slug}`", "", "| Claim | Risk | Guard | Absolutes | Topics | Repeated in | Citations |",
                  "|---|---|---|---|---|---|---|"]
        for cid, e in rows:
            words = ", ".join(sorted({a["word"] for a in e["absolutes"] if not a["in_code_block"]})) or "-"
            guard = (e.get("guard_check") or {}).get("status", "-")
            files = len({h["path"] for h in e["cluster"]})
            cites = ", ".join(f"{c['path'].rsplit('/', 1)[-1]}:L{c['start']}-L{c['end']}" for c in e["citations"][:3])
            lines.append(f"| {cid} | {e['risk_score']} | {guard} | {md_cell(words)} | "
                         f"{md_cell(', '.join(e['topics']) or '-')} | {files or '-'} | {md_cell(cites or '-')} |")
    return "\n".join(lines) + "\n"


def load_precheck(review_dir: Path | None) -> dict[str, Any] | None:
    """``<review_dir>/precheck/precheck.json`` when present and valid, else None."""
    if review_dir is None:
        return None
    path = Path(review_dir) / PRECHECK_DIR / "precheck.json"
    if not path.is_file():
        return None
    import json

    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError:
        log.warning("%s is not valid JSON; ignored", path)
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_PRECHECK:
        log.warning("%s is not a %s file; ignored", path, SCHEMA_PRECHECK)
        return None
    return data


def spot_check_count(n: int) -> int:
    """ceil(n/4), at least 1 (0 when there is nothing to sample)."""
    return 0 if n <= 0 else max(1, math.ceil(n / 4))
