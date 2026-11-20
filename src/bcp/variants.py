"""Configuration / feature-flag variant map from BRE inventory tables plus a code scan."""

from __future__ import annotations

import logging
import re
from typing import Any

from bcp.bre import BreLayout
from bcp.config import BcpConfig
from bcp.sourceindex import SourceIndex
from bcp.textio import md_cell, read_text

log = logging.getLogger(__name__)

KEY_HEADER_RE = re.compile(r"(?i)property|flag|key|constant|setting|toggle")
DOMAIN_HEADER_RE = re.compile(r"(?i)^domain$")
IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")
SEPARATOR_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
MAX_HITS = 20
NO_INVENTORY = (
    "No configuration inventory found in the BRE; add `variant_key_patterns` to bcp.config.json"
)


def clean_cell(cell: str) -> str:
    """Strip whitespace, then surrounding ``**``/``__``, then surrounding backticks."""
    text = cell.strip()
    for wrap in ("**", "__"):
        if len(text) > 2 * len(wrap) and text.startswith(wrap) and text.endswith(wrap):
            text = text[len(wrap):-len(wrap)].strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        text = text[1:-1].strip()
    return text


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c for c in re.split(r"(?<!\\)\|", body)]


def parse_inventory(text: str, slugs: set[str], source: str) -> list[dict[str, Any]]:
    """One entry per row of every table whose first header cell names a key/flag/property."""
    entries: list[dict[str, Any]] = []
    lines = text.replace("\r\n", "\n").split("\n")
    category = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("## "):
            category = line[3:].strip()
        is_table = (
            line.lstrip().startswith("|")
            and index + 1 < len(lines)
            and SEPARATOR_RE.match(lines[index + 1].strip())
        )
        if not is_table:
            index += 1
            continue
        headers = [clean_cell(c) for c in _split_row(line)]
        index += 2
        rows: list[list[str]] = []
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            rows.append([clean_cell(c) for c in _split_row(lines[index])])
            index += 1
        if not headers or not KEY_HEADER_RE.search(headers[0]):
            continue
        domain_col = next((i for i, h in enumerate(headers) if DOMAIN_HEADER_RE.match(h)), None)
        for row in rows:
            if not row or not row[0]:
                continue
            domain = row[domain_col] if domain_col is not None and domain_col < len(row) else None
            if domain is not None and domain not in slugs:
                log.debug("variant %s: domain cell %r is not a manifest slug", row[0], domain)
                domain = None
            effect = "; ".join(
                c for i, c in enumerate(row) if i not in (0, domain_col) and c
            )
            entries.append(
                {"key": row[0], "category": category or "Uncategorized", "domain": domain,
                 "effect": effect, "source": source}
            )
    return entries


def scan_code(
    index: SourceIndex, keys: list[str], file_index: dict[str, list[str]]
) -> dict[str, dict[str, Any]]:
    """Whole-word occurrences (≤20 per key) of identifier keys, mapped to domains."""
    wanted = sorted({k for k in keys if IDENT_RE.match(k)})
    regexes = {k: re.compile(rf"\b{re.escape(k)}\b") for k in wanted}
    found: dict[str, dict[str, Any]] = {k: {"hits": [], "read_by": set(), "hit_count": 0} for k in wanted}
    for rel in index.paths:
        lines = index.lines(rel)
        blob = "\n".join(lines)
        for key in wanted:
            if key not in blob:
                continue
            data = found[key]
            for number, line in enumerate(lines, start=1):
                if regexes[key].search(line):
                    data["hit_count"] += 1
                    if len(data["hits"]) < MAX_HITS:
                        data["hits"].append(f"{rel}:{number}")
                    data["read_by"].update(file_index.get(rel, []))
    return {
        k: {"hits": v["hits"], "hit_count": v["hit_count"], "read_by": sorted(v["read_by"])}
        for k, v in found.items()
    }


def discover_by_patterns(index: SourceIndex, patterns: list[str]) -> list[str]:
    """Keys matched by ``variant_key_patterns`` (group 1 when present) in indexed files."""
    keys: set[str] = set()
    compiled = [re.compile(p) for p in patterns]
    for rel in index.paths:
        for line in index.lines(rel):
            for regex in compiled:
                for match in regex.finditer(line):
                    keys.add(match.group(1) if regex.groups else match.group(0))
    return sorted(k for k in keys if k)


def build_variants(
    layout: BreLayout,
    config: BcpConfig,
    slugs: list[str],
    index: SourceIndex | None,
    file_index: dict[str, list[str]],
    bre_prefix: str = "",
) -> dict[str, Any]:
    """Variant map data; warns (once) when there is no inventory and no key pattern."""
    docs = layout.inventory_docs(config.variant_inventory_globs)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for doc in docs:
        rel = layout.rel(doc)
        source = f"{bre_prefix.rstrip('/')}/{rel}" if bre_prefix else rel
        for entry in parse_inventory(read_text(doc), set(slugs), source):
            if entry["key"] in seen:
                continue
            seen.add(entry["key"])
            entries.append(entry)
    if index is not None and config.variant_key_patterns:
        for key in discover_by_patterns(index, config.variant_key_patterns):
            if key not in seen:
                seen.add(key)
                entries.append({"key": key, "category": "Discovered by variant_key_patterns",
                                "domain": None, "effect": "", "source": "code scan"})
    message = None
    if not docs and not config.variant_key_patterns:
        message = NO_INVENTORY
        log.warning("no configuration inventory found in the BRE and no variant_key_patterns configured")
    scanned = index is not None
    if scanned:
        scan = scan_code(index, [e["key"] for e in entries], file_index)  # type: ignore[arg-type]
        for entry in entries:
            hit = scan.get(entry["key"])
            entry["read_by"] = hit["read_by"] if hit else []
            entry["hits"] = hit["hits"] if hit else []
            entry["hit_count"] = hit["hit_count"] if hit else 0
    else:
        for entry in entries:
            entry["read_by"], entry["hits"], entry["hit_count"] = [], [], 0
    entries.sort(key=lambda e: (e["category"], e["key"]))
    return {
        "inventory_docs": [
            f"{bre_prefix.rstrip('/')}/{layout.rel(d)}" if bre_prefix else layout.rel(d) for d in docs
        ],
        "code_scanned": scanned,
        "message": message,
        "entries": entries,
    }


def render_variants_md(data: dict[str, Any]) -> str:
    """Markdown body (no marker/provenance header) for ``variant-map.md``."""
    lines = [
        "# Configuration and feature-flag variant map",
        "",
        "Behaviour that differs by configuration lives here. When a change touches a domain,",
        "retest every variant whose readers include that domain.",
        "",
    ]
    if data.get("message"):
        lines += [data["message"], ""]
    if data["inventory_docs"]:
        lines += ["Sources: " + ", ".join(data["inventory_docs"]), ""]
    if not data["code_scanned"]:
        lines += ["Code scan skipped (no source files under the target); hit locations are not listed.", ""]
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for entry in data["entries"]:
        by_cat.setdefault(entry["category"], []).append(entry)
    for category in sorted(by_cat):
        lines += [f"## {category}", "", "| Key | Owning domain | Read by (code) | Effect | Hit locations |",
                  "|---|---|---|---|---|"]
        for e in by_cat[category]:
            hits = ", ".join(e["hits"][:5]) + (f" (+{e['hit_count'] - 5} more)" if e["hit_count"] > 5 else "")
            lines.append(
                f"| `{md_cell(e['key'])}` | {md_cell(e['domain'] or '-')} | "
                f"{md_cell(', '.join(e['read_by']) or '-')} | {md_cell(e['effect'] or '-')} | {md_cell(hits or '-')} |"
            )
        lines.append("")
    if not data["entries"] and not data.get("message"):
        lines.append("The inventory documents contain no key tables.")
    return "\n".join(lines).rstrip("\n") + "\n"
