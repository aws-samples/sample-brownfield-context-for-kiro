"""Cross-component dependency and contracts map: ``dependency-map.md`` / ``.json`` and ``bcp deps``.

Code-derived edges are mechanical (Tier 1): a mapped file imports, or (Java) names a type from its own
package that lives in, a file mapped to another domain. Edges are aggregated domain → domain through
the file index. Extraction-claimed edges (the manifest's ``depends_on`` / ``depended_on_by``, which
also feed the impact map) are Tier 3 until the code confirms them, so every edge is classified:

- ``confirmed-by-code``: claimed by the extraction and found in the code;
- ``bre-only (verify)``: claimed by the extraction, not found in the code (could be runtime wiring,
  reflection, configuration, or wrong);
- ``code-only (missing from BRE)``: found in the code, not claimed by the extraction.

The map also lists interfaces per domain (``*Service``, ``*Api``, ``*Client``, ``*Gateway``),
integration points (``integration_patterns``; location mechanical, meaning Tier 2) and data flows
through shared tables.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from bcp.config import BcpConfig
from bcp.lang import for_path
from bcp.lang.java import mask
from bcp.manifest import DomainManifest
from bcp.sourceindex import SourceIndex, type_name
from bcp.textio import atomic_write_text, dump_json, md_cell

log = logging.getLogger(__name__)

SCHEMA_DEPS = "bcp.deps/1"
CONFIRMED = "confirmed-by-code"
BRE_ONLY = "bre-only (verify)"
CODE_ONLY = "code-only (missing from BRE)"
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;")
TYPE_TOKEN_RE = re.compile(r"\b[A-Z]\w*\b")
INTERFACE_RE = re.compile(r"\binterface\s+([A-Z]\w*)")
INTERFACE_SUFFIXES = ("Service", "Api", "API", "Client", "Gateway")
MAX_SAMPLES = 3
MAX_HITS = 20
MD_HITS = 3


def _package(lines: list[str]) -> str:
    for line in lines[:80]:
        match = PACKAGE_RE.match(line)
        if match:
            return match.group(1)
    return ""


class TypeTable:
    """Java type lookup: fully qualified name → path, package → {simple name: path}."""

    def __init__(self, index: SourceIndex) -> None:
        self.by_fqn: dict[str, str] = {}
        self.by_package: dict[str, dict[str, str]] = {}
        self.package_of: dict[str, str] = {}
        for rel in index.paths:
            if not rel.lower().endswith(".java"):
                continue
            pkg = _package(index.lines(rel))
            stem = rel.rsplit("/", 1)[-1][:-5]
            self.package_of[rel] = pkg
            fqn = f"{pkg}.{stem}" if pkg else stem
            current = self.by_fqn.get(fqn)
            if current is not None and "/main/" in f"/{current}" and "/main/" not in f"/{rel}":
                continue  # a main-source type wins over a test type of the same name
            self.by_fqn[fqn] = rel
            self.by_package.setdefault(pkg, {})[stem] = rel

    def resolve_import(self, name: str) -> list[str]:
        """Paths an import names: a type, a static member's type, or every type of a ``.*`` package."""
        if name.endswith(".*"):
            base = name[:-2]
            if base in self.by_fqn:  # static import of every member of a type
                return [self.by_fqn[base]]
            return list(self.by_package.get(base, {}).values())
        if name in self.by_fqn:
            return [self.by_fqn[name]]
        owner = name.rsplit(".", 1)[0]
        return [self.by_fqn[owner]] if owner in self.by_fqn else []


def file_references(index: SourceIndex, types: TypeTable, rel: str) -> set[str]:
    """Repository files ``rel`` depends on: resolved imports plus (Java) same-package type names."""
    lines = index.lines(rel)
    plugin = for_path(rel)
    out: set[str] = set()
    for name in plugin.imports(lines):
        out.update(types.resolve_import(name))
    if rel in types.package_of:
        siblings = types.by_package.get(types.package_of[rel], {})
        if siblings:
            used = set(TYPE_TOKEN_RE.findall("\n".join(mask(lines))))
            out.update(path for stem, path in siblings.items() if stem in used)
    out.discard(rel)
    return out


def code_edges(index: SourceIndex, file_index: dict[str, list[str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(from, to): {count, samples}}`` aggregated from file references between mapped files."""
    types = TypeTable(index)
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for rel, src_domains in file_index.items():
        if not index.contains(rel):
            continue
        for target in sorted(file_references(index, types, rel)):
            for src in src_domains:
                for dst in file_index.get(target, []):
                    if dst == src or dst in src_domains:
                        continue
                    edge = edges.setdefault((src, dst), {"count": 0, "samples": []})
                    edge["count"] += 1
                    if len(edge["samples"]) < MAX_SAMPLES:
                        edge["samples"].append(f"{rel} → {target}")
    return edges


def interfaces(index: SourceIndex, manifest: DomainManifest) -> dict[str, list[dict[str, str]]]:
    """Per domain, mapped files that declare an interface named ``*Service|*Api|*Client|*Gateway``."""
    out: dict[str, list[dict[str, str]]] = {}
    for dom in manifest.ordered_domains():
        found = []
        for rel in dom.source_paths:
            if not index.contains(rel):
                continue
            for line in mask(index.lines(rel))[:200]:
                if type_name(line):
                    match = INTERFACE_RE.search(line)
                    if match and match.group(1).endswith(INTERFACE_SUFFIXES):
                        found.append({"name": match.group(1), "path": rel})
                    break
        out[dom.slug] = found
    return out


def integration_points(index: SourceIndex, manifest: DomainManifest,
                       patterns: dict[str, str]) -> dict[str, dict[str, Any]]:
    """``{slug: {label: {count, hits: [path:Lnn]}}}`` from ``integration_patterns`` (comments and string
    contents blanked)."""
    compiled = {label: re.compile(rx) for label, rx in patterns.items()}
    out: dict[str, dict[str, Any]] = {}
    for dom in manifest.ordered_domains():
        per: dict[str, Any] = {}
        for rel in dom.source_paths:
            if not index.contains(rel):
                continue
            masked = mask(index.lines(rel)) if rel.lower().endswith(".java") else index.lines(rel)
            for number, line in enumerate(masked, start=1):
                for label, regex in compiled.items():
                    if regex.search(line):
                        item = per.setdefault(label, {"count": 0, "hits": []})
                        item["count"] += 1
                        if len(item["hits"]) < MAX_HITS:
                            item["hits"].append(f"{rel}:L{number}")
        out[dom.slug] = dict(sorted(per.items()))
    return out


def bre_edges(manifest: DomainManifest) -> set[tuple[str, str]]:
    """Extraction-claimed ``(from, to)`` edges: ``from`` depends on ``to``."""
    known = set(manifest.slugs)
    out: set[tuple[str, str]] = set()
    for dom in manifest.domains:
        out.update((dom.slug, d) for d in dom.depends_on if d in known and d != dom.slug)
        out.update((d, dom.slug) for d in dom.depended_on_by if d in known and d != dom.slug)
    return out


def build_deps(manifest: DomainManifest, config: BcpConfig, index: SourceIndex | None,
               file_index: dict[str, list[str]], impact: dict[str, Any]) -> dict[str, Any]:
    """The dependency map (JSON-friendly)."""
    code = code_edges(index, file_index) if index is not None else {}
    claimed = bre_edges(manifest)
    edges = []
    for pair in sorted(set(code) | claimed):
        status = CONFIRMED if pair in code and pair in claimed else (BRE_ONLY if pair in claimed else CODE_ONLY)
        info = code.get(pair, {"count": 0, "samples": []})
        edges.append({"from": pair[0], "to": pair[1], "status": status, "code_references": info["count"],
                      "samples": info["samples"]})
    counts = {s: sum(1 for e in edges if e["status"] == s) for s in (CONFIRMED, BRE_ONLY, CODE_ONLY)}
    return {
        "schema": SCHEMA_DEPS,
        "code_scanned": index is not None,
        "edge_counts": counts,
        "edges": edges,
        "interfaces": interfaces(index, manifest) if index is not None else {},
        "integration_points": integration_points(index, manifest, config.integration_patterns)
        if index is not None else {},
        "shared_tables": impact.get("shared_tables", {}),
    }


def _short(sample: str) -> str:
    """``a/b/X.java → c/Y.java`` as ``X.java → Y.java``."""
    return " → ".join(part.strip().rsplit("/", 1)[-1] for part in sample.split("→"))


def render_deps_md(data: dict[str, Any], focus: list[str] | None = None) -> str:
    """Markdown body (no marker or Trust line) for ``dependency-map.md``. With ``focus`` (domain slugs)
    only edges, interfaces and integration points that touch those domains are listed; the JSON keeps
    everything."""
    if focus:
        wanted = set(focus)
        data = dict(data,
                    edges=[e for e in data["edges"] if e["from"] in wanted or e["to"] in wanted],
                    interfaces={k: v for k, v in data["interfaces"].items() if k in wanted},
                    integration_points={k: v for k, v in data["integration_points"].items() if k in wanted},
                    shared_tables={t: d for t, d in data["shared_tables"].items() if wanted & set(d)})
    counts = data["edge_counts"]
    lines = [
        "# Cross-component dependency and contracts map",
        "",
        "Code-derived edges are mechanical: a file imports, or names a same-package type from, a file mapped to "
        "another domain. Extraction-claimed edges are hypotheses (Tier 3) until the code confirms them. Verify "
        "every `bre-only` edge in the code before you rely on it; it may be runtime wiring, reflection, "
        "configuration, or wrong. A `code-only` edge is a dependency the extraction missed.",
        "",
        f"Edges: {counts[CONFIRMED]} {CONFIRMED}, {counts[BRE_ONLY]} {BRE_ONLY}, {counts[CODE_ONLY]} {CODE_ONLY}.",
        "",
    ]
    if focus:
        lines += [f"Scoped to the agent domains ({', '.join(focus)}): {len(data['edges'])} edges touch them. "
                  "`dependency-map.json` beside this file has every edge with full paths.", ""]
    if not data["code_scanned"]:
        lines += ["Code scan skipped (no source files); every extraction edge is unverified.", ""]
    lines += ["## Domain dependencies", "", "Read `from → to` as \"from depends on to\". Samples show file names; "
              "the JSON has full paths.", "",
              "| From | To | Status | Code refs | Sample |", "|---|---|---|---|---|"]
    for edge in data["edges"]:
        sample = _short(edge["samples"][0]) if edge["samples"] else "-"
        lines.append(f"| `{edge['from']}` | `{edge['to']}` | {edge['status']} | {edge['code_references']} | "
                     f"{md_cell(sample)} |")
    if not data["edges"]:
        lines.append("| - | - | no edges | 0 | - |")
    lines += ["", "## Interfaces (contracts) per domain", ""]
    shown = False
    for slug, items in data["interfaces"].items():
        if items:
            shown = True
            lines.append(f"- `{slug}`: " + ", ".join(f"`{i['name']}` ({i['path'].rsplit('/', 1)[-1]})"
                                                      for i in items[:12])
                         + (f" (+{len(items) - 12} more)" if len(items) > 12 else ""))
    if not shown:
        lines.append("None found.")
    lines += ["", "## Integration points", "",
              "Locations are mechanical; what each call does is not checked. Open the code before you state it.", ""]
    rows = [(slug, label, item) for slug, per in data["integration_points"].items() for label, item in per.items()]
    if rows:
        lines += ["| Domain | Kind | Hits | First locations |", "|---|---|---|---|"]
        for slug, label, item in rows:
            hits = ", ".join(h.rsplit("/", 1)[-1] for h in item["hits"][:MD_HITS])
            lines.append(f"| `{slug}` | {label} | {item['count']} | {md_cell(hits)} |")
    else:
        lines.append("None found.")
    lines += ["", "## Data flows: shared tables", ""]
    if data["shared_tables"]:
        lines += ["| Table | Domains |", "|---|---|"]
        lines += [f"| `{t}` | {md_cell(', '.join(d))} |" for t, d in data["shared_tables"].items()]
    else:
        lines.append("No table is used by more than one domain.")
    return "\n".join(lines).rstrip("\n") + "\n"


def run_deps(bre: Path, repo: Path, out: Path, config: BcpConfig) -> dict[str, Any]:
    """``bcp deps``: build the map and write ``dependency-map.md`` / ``.json`` into ``out``."""
    from bcp.bre import BreLayout
    from bcp.impact import build_file_index, build_impact
    from bcp.manifest import load_manifest
    from bcp.programs import repo_index

    manifest = load_manifest(Path(bre))
    index = repo_index(Path(repo), Path(bre), config)
    file_index = build_file_index(manifest)
    data = build_deps(manifest, config, index, file_index, build_impact(manifest, BreLayout(Path(bre))))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "dependency-map.md", out / "dependency-map.json"
    atomic_write_text(md, render_deps_md(data))
    atomic_write_text(js, dump_json(data))
    data["written"] = [str(md), str(js)]
    return data
