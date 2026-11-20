"""Glossary built from the manifest (domains, entities, shared kernel, concerns)."""

from __future__ import annotations

from pathlib import Path

from bcp.manifest import DomainManifest
from bcp.textio import first_sentence, read_text


def glossary_entries(manifest: DomainManifest) -> list[tuple[str, str]]:
    """``(term, definition)`` pairs, sorted case-insensitively by term then definition."""
    entries: list[tuple[str, str]] = []
    for dom in manifest.domains:
        desc = first_sentence(dom.description or dom.responsibility)
        entries.append((dom.name, f"domain `{dom.slug}`" + (f": {desc}" if desc else "")))
        for ent in dom.entities:
            name = ent.get("name")
            if not name:
                continue
            parts = ["entity"]
            if ent.get("table_name"):
                parts.append(f"table `{ent['table_name']}`")
            parts.append(f"owning domain `{dom.slug}`")
            parts.append(f"shared: {'yes' if ent.get('is_shared') else 'no'}")
            entries.append((str(name), ", ".join(parts)))
    for ent in manifest.shared_entities:
        name = ent.get("name")
        if not name:
            continue
        users = ", ".join(str(u) for u in ent.get("used_by_domains") or []) or "none listed"
        entries.append(
            (str(name), f"shared entity owned by `{ent.get('owning_domain') or 'unknown'}`, used by {users}")
        )
    for comp in manifest.shared_kernel_components:
        name = comp.get("component_name") or comp.get("name")
        if not name:
            continue
        kind = comp.get("component_type") or comp.get("type") or "component"
        src = comp.get("source_file")
        entries.append((str(name), f"shared-kernel {kind}" + (f", source `{src}`" if src else "")))
    for concern in manifest.cross_cutting_concerns:
        name = concern.get("concern")
        if not name:
            continue
        summary = first_sentence(str(concern.get("summary") or ""))
        doc = concern.get("documented_in")
        text = "cross-cutting concern" + (f": {summary}" if summary else "")
        if doc:
            text += f" (documented in {doc})"
        entries.append((str(name), text))
    return sorted(set(entries), key=lambda e: (e[0].lower(), e[0], e[1]))


def render_glossary_md(manifest: DomainManifest, extra: Path | None = None) -> str:
    """Markdown body (no marker/provenance header) for ``glossary.md``."""
    lines = ["# Glossary", ""]
    for term, definition in glossary_entries(manifest):
        lines.append(f"- **{term}**: {definition}")
    if extra is not None:
        lines += ["", "## Team terms", "", read_text(extra).replace("\r\n", "\n").rstrip("\n")]
    return "\n".join(lines) + "\n"
