"""Cross-domain dependency / impact map and the file → domain index."""

from __future__ import annotations

import logging
from typing import Any

from bcp.bre import BreLayout
from bcp.manifest import DomainManifest
from bcp.textio import md_cell

log = logging.getLogger(__name__)

RELATED_DOCS = (
    ("Coupling hotspots", "coupling-hotspots.md"),
    ("Data ownership", "data-ownership.md"),
    ("Shared kernel", "shared-kernel.md"),
)


def mermaid_id(slug: str) -> str:
    """Mermaid node id for a slug (hyphens become underscores)."""
    return "d_" + slug.replace("-", "_")


def _join(prefix: str, rel: str) -> str:
    return f"{prefix.rstrip('/')}/{rel}" if prefix else rel


def related_docs(layout: BreLayout | None, bre_prefix: str) -> dict[str, str | None]:
    """Workspace-relative paths of the hotspot, data-ownership and shared-kernel docs (or None)."""
    found: dict[str, str | None] = {}
    for _label, name in RELATED_DOCS:
        path = layout.find_doc(name) if layout is not None else None
        found[name] = _join(bre_prefix, layout.rel(path)) if path is not None and layout is not None else None
    return found


def build_impact(
    manifest: DomainManifest, layout: BreLayout | None = None, bre_prefix: str = ""
) -> dict[str, Any]:
    """Impact map data: per-domain edges and tables, derived shared tables, shared entities."""
    known = set(manifest.slugs)
    domains: dict[str, Any] = {}
    shared_tables: dict[str, set[str]] = {}
    for dom in manifest.domains:
        def edges(slugs: list[str], kind: str) -> list[dict[str, Any]]:
            out = []
            for slug in slugs:
                edge: dict[str, Any] = {"slug": slug}
                if slug not in known:
                    edge["unknown"] = True
                    log.warning("domain %s: %s names unknown domain %r", dom.slug, kind, slug)
                out.append(edge)
            return out

        domains[dom.slug] = {
            "name": dom.name,
            "depends_on": edges(dom.depends_on, "depends_on_domains"),
            "depended_on_by": edges(dom.depended_on_by, "depended_on_by_domains"),
            "owned_tables": sorted(dom.owned_tables),
            "shared_tables_referenced": sorted(dom.shared_tables_referenced),
            "shared_entities_used": sorted(dom.shared_entities_used),
            "cross_domain_features": list(dom.cross_domain_features),
        }
        for table in dom.owned_tables:
            shared_tables.setdefault(table, set()).add(dom.slug)
        for table in dom.shared_tables_referenced:
            shared_tables.setdefault(table, set()).add(dom.slug)
    docs = related_docs(layout, bre_prefix)
    return {
        "domains": {slug: domains[slug] for slug in sorted(domains)},
        "shared_tables": {t: sorted(s) for t, s in sorted(shared_tables.items()) if len(s) > 1},
        "shared_entities": manifest.shared_entities,
        "hotspots_doc": docs.get("coupling-hotspots.md"),
        "related_docs": docs,
    }


def build_file_index(manifest: DomainManifest) -> dict[str, list[str]]:
    """``{repo-relative path: [slugs]}`` from every domain's ``source_files``."""
    index: dict[str, set[str]] = {}
    for dom in manifest.domains:
        for path in dom.source_paths:
            index.setdefault(path, set()).add(dom.slug)
    return {path: sorted(slugs) for path, slugs in sorted(index.items())}


def blast_radius(impact: dict[str, Any]) -> list[tuple[str, int]]:
    """Domains sorted by how many domains depend on them (desc), then slug."""
    counts = [(slug, len(d["depended_on_by"])) for slug, d in impact["domains"].items()]
    return sorted(counts, key=lambda item: (-item[1], item[0]))


def render_impact_md(impact: dict[str, Any], manifest: DomainManifest, file_index_path: str) -> str:
    """Markdown body (no marker/provenance header) for ``impact-map.md``."""
    lines = ["# Cross-domain impact map", ""]
    lines += [
        "Use this map to answer \"which domains, tables and variants does this change affect?\".",
        f"Map a file to its domains with `{file_index_path}`.",
        "",
        "## Dependency graph",
        "",
        "Arrows point from a domain to the domains it depends on.",
        "",
        "```mermaid",
        "graph LR",
    ]
    for dom in manifest.domains:
        lines.append(f'  {mermaid_id(dom.slug)}["{dom.slug}"]')
    for slug, data in impact["domains"].items():
        for edge in data["depends_on"]:
            if not edge.get("unknown"):
                lines.append(f"  {mermaid_id(slug)} --> {mermaid_id(edge['slug'])}")
    lines += ["```", "", "## Blast radius", "", "Domains most other domains depend on (change these with most care):", ""]
    for slug, count in blast_radius(impact):
        lines.append(f"- `{slug}`: {count} dependent domain(s)")
    lines += ["", "## Per-domain dependencies", ""]
    for slug, data in impact["domains"].items():
        up = ", ".join(e["slug"] + (" (unknown)" if e.get("unknown") else "") for e in data["depends_on"]) or "none"
        down = ", ".join(e["slug"] for e in data["depended_on_by"]) or "none"
        shared = [t for t in data["owned_tables"] + data["shared_tables_referenced"] if t in impact["shared_tables"]]
        lines += [
            f"### {data['name']} (`{slug}`)",
            "",
            "| Property | Value |",
            "|---|---|",
            f"| Depends on (upstream) | {md_cell(up)} |",
            f"| Depended on by (downstream) | {md_cell(down)} |",
            f"| Owned tables | {md_cell(', '.join(data['owned_tables']) or 'none')} |",
            f"| Shared tables touched | {md_cell(', '.join(sorted(set(shared))) or 'none')} |",
            f"| Shared entities used | {md_cell(', '.join(data['shared_entities_used']) or 'none')} |",
            f"| Cross-domain features | {md_cell('; '.join(data['cross_domain_features']) or 'none')} |",
            "",
        ]
    lines += ["## Shared tables", ""]
    if impact["shared_tables"]:
        lines += ["| Table | Domains (owner and referrers) |", "|---|---|"]
        lines += [f"| `{t}` | {md_cell(', '.join(s))} |" for t, s in impact["shared_tables"].items()]
    else:
        lines.append("No table is used by more than one domain.")
    docs = [(label, impact["related_docs"].get(name)) for label, name in RELATED_DOCS]
    docs = [(label, path) for label, path in docs if path]
    if docs:
        lines += ["", "## Related extraction documents", ""]
        lines += [f"- {label}: {path}" for label, path in docs]
    return "\n".join(lines) + "\n"
