"""Load and validate ``domain-manifest.json`` from a business-rules extraction."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bcp.errors import BcpInputError
from bcp.textio import is_safe_relpath, read_text

log = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/@+-]+$")
MANIFEST_RELPATH = "domain-decomposition-overview/domain-manifest.json"


@dataclass(frozen=True)
class SourceFile:
    """One source file mapped to a domain."""

    path: str
    loc: int | None = None
    role: str | None = None


@dataclass
class Domain:
    """One domain entry from the manifest."""

    slug: str
    name: str
    description: str = ""
    responsibility: str = ""
    bounded_context_summary: str = ""
    source_files: list[SourceFile] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    owned_tables: list[str] = field(default_factory=list)
    shared_tables_referenced: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    depended_on_by: list[str] = field(default_factory=list)
    shared_entities_used: list[str] = field(default_factory=list)
    cross_domain_features: list[str] = field(default_factory=list)
    rule_count: int | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def source_paths(self) -> list[str]:
        """Repo-relative source paths of this domain."""
        return [sf.path for sf in self.source_files]


@dataclass
class DomainManifest:
    """Validated manifest; ``raw`` keeps the full JSON for derived builders."""

    path: Path
    project: dict[str, Any]
    domains: list[Domain]
    execution_order: list[str]
    shared_entities: list[dict[str, Any]]
    shared_kernel_components: list[dict[str, Any]]
    cross_cutting_concerns: list[dict[str, Any]]
    common_artifacts: dict[str, Any]
    raw: dict[str, Any]

    @property
    def slugs(self) -> list[str]:
        """Domain slugs in manifest order."""
        return [d.slug for d in self.domains]

    def domain(self, slug: str) -> Domain | None:
        """Return the domain with ``slug`` or None."""
        for dom in self.domains:
            if dom.slug == slug:
                return dom
        return None

    @property
    def project_stats(self) -> dict[str, Any]:
        """``project.stats`` or an empty dict."""
        stats = self.project.get("stats")
        return stats if isinstance(stats, dict) else {}

    def ordered_domains(self) -> list[Domain]:
        """Domains in ``execution_order``, then any missing ones in manifest order."""
        by_slug = {d.slug: d for d in self.domains}
        ordered = [by_slug[s] for s in self.execution_order if s in by_slug]
        seen = {d.slug for d in ordered}
        ordered += [d for d in self.domains if d.slug not in seen]
        return ordered


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _strs(value: Any) -> list[str]:
    return [str(v) for v in _list(value) if isinstance(v, (str, int, float))]


def _source_files(slug: str, raw: Any) -> list[SourceFile]:
    files: list[SourceFile] = []
    for entry in _list(raw):
        if isinstance(entry, str):
            path, loc, role = entry, None, None
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
            path = entry["path"]
            loc_raw = entry.get("loc")
            loc = loc_raw if isinstance(loc_raw, int) and not isinstance(loc_raw, bool) else None
            role = entry.get("role") if isinstance(entry.get("role"), str) else None
        else:
            log.warning("domain %s: source_files entry without a path skipped", slug)
            continue
        norm = path.replace("\\", "/")
        if not is_safe_relpath(norm) or not SAFE_PATH_RE.match(norm):
            log.warning("domain %s: unsafe source_files path skipped: %r", slug, path)
            continue
        files.append(SourceFile(norm, loc, role))
    return files


def parse_manifest(raw: Any, path: Path) -> DomainManifest:
    """Validate a parsed manifest object."""
    if not isinstance(raw, dict):
        raise BcpInputError(f"manifest must be a JSON object: {path}")
    domains_raw = raw.get("domains")
    if not isinstance(domains_raw, list):
        raise BcpInputError(f"manifest 'domains' is missing or not a list: {path}")
    domains: list[Domain] = []
    seen: set[str] = set()
    for index, entry in enumerate(domains_raw):
        if not isinstance(entry, dict):
            raise BcpInputError(f"manifest domains[{index}] is not an object")
        slug, name = entry.get("slug"), entry.get("name")
        if not isinstance(slug, str) or not slug or not isinstance(name, str) or not name.strip():
            raise BcpInputError(f"manifest domains[{index}] needs 'slug' and 'name'")
        if not SLUG_RE.match(slug):
            raise BcpInputError(f"manifest domains[{index}] has an invalid slug: {slug!r}")
        if slug in seen:
            raise BcpInputError(f"manifest has a duplicate slug: {slug!r}")
        seen.add(slug)
        rule_count = entry.get("rule_count")
        domains.append(
            Domain(
                slug=slug,
                name=name.strip(),
                description=str(entry.get("description") or ""),
                responsibility=str(entry.get("responsibility") or ""),
                bounded_context_summary=str(entry.get("bounded_context_summary") or ""),
                source_files=_source_files(slug, entry.get("source_files")),
                entities=[e for e in _list(entry.get("entities")) if isinstance(e, dict)],
                owned_tables=_strs(entry.get("owned_tables")),
                shared_tables_referenced=_strs(entry.get("shared_tables_referenced")),
                depends_on=_strs(entry.get("depends_on_domains")),
                depended_on_by=_strs(entry.get("depended_on_by_domains")),
                shared_entities_used=_strs(entry.get("shared_entities_used")),
                cross_domain_features=_strs(entry.get("cross_domain_features")),
                rule_count=rule_count if isinstance(rule_count, int) else None,
                stats=entry.get("stats") if isinstance(entry.get("stats"), dict) else {},
            )
        )

    project = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    if not isinstance(project.get("stats"), dict):
        log.warning("manifest has no project.stats; tech and product details reduced")

    def optional(key: str, kind: type, default: Any) -> Any:
        value = raw.get(key)
        if not isinstance(value, kind):
            log.warning("manifest has no %s; using default", key)
            return default
        return value

    order = [str(s) for s in optional("execution_order", list, [])]
    if order and (set(order) != seen or len(order) != len(set(order))):
        log.warning("manifest execution_order does not match domains; manifest order used for missing entries")
    elif not order:
        order = [d.slug for d in domains]
    return DomainManifest(
        path=path,
        project=project,
        domains=domains,
        execution_order=order,
        shared_entities=[e for e in optional("shared_entities", list, []) if isinstance(e, dict)],
        shared_kernel_components=[
            e for e in _list(raw.get("shared_kernel_components")) if isinstance(e, dict)
        ],
        cross_cutting_concerns=[
            e for e in _list(raw.get("cross_cutting_concerns")) if isinstance(e, dict)
        ],
        common_artifacts=optional("common_artifacts", dict, {}),
        raw=raw,
    )


def load_manifest(bre_root: Path) -> DomainManifest:
    """Load ``<bre_root>/domain-decomposition-overview/domain-manifest.json``."""
    path = Path(bre_root) / MANIFEST_RELPATH
    if not path.is_file():
        raise BcpInputError(f"domain manifest not found: {path}")
    try:
        raw = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise BcpInputError(f"domain manifest is not valid JSON: {path}: {exc}") from exc
    return parse_manifest(raw, path)
