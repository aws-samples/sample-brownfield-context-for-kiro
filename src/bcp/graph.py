"""Persistent class-level component graph: ``bcp graph build`` (queries live in :mod:`bcp.graphq`).

Every node and edge is mechanical (read from the code or from generated context files) and carries
evidence. Nodes:

* ``class:<fqn>``: a main-source Java type (component). The demo domains' classes plus every class
  within ``hops`` dependency hops of them (either direction), so a query of depth <= ``hops`` started at
  a demo class is complete;
* ``table:<name>``: a table a class maps to (``@Table`` / ``@JoinTable`` / ``@CollectionTable``, Hibernate
  ``.hbm.xml``), with owning and referencing domains from ``impact-map.json``;
* ``config:<key>``: a configuration key from ``program-config.json`` read by a class in scope, joined with
  the per-program effective values;
* ``endpoint:service:<Name>``: a public service interface of a demo domain (``*Service|*Api|*Client|*Gateway``);
  ``endpoint:message:<TYPE>``: an inbound message route (``put("ORU_R01", handler)``);
  ``endpoint:integration:<label>``: an integration point kind (``integration_patterns``);
* ``domain:<slug>``: a domain from the file index (structural ``in-domain`` edges only).

Class-to-class edges reuse :func:`bcp.deps.file_references` (imports plus same-package type names), so
they are the file-level edges behind ``dependency-map.json``, typed ``extends`` / ``implements`` /
``imports`` / ``uses`` with the line that shows them. An interface named like a service, DAO or handler
also gets an ``implemented-by`` edge to each implementation (callers reach it through the interface).
Other edges: ``maps-to-table`` / ``joins-table``, ``reads-config`` / ``declares-config``, ``exposes``,
``routes-to``, ``integrates-via`` and the structural ``in-domain``. Every edge carries ``evidence``
(``path#Lnn``) and the ``commit`` it was read at.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Sequence

from bcp.config import BcpConfig
from bcp.deps import INTERFACE_SUFFIXES, TypeTable, file_references
from bcp.errors import BcpInputError, OutputWriteError
from bcp.graphq import SCHEMA
from bcp.lang.java import IMPORT_RE, mask
from bcp.review import git_head, now_iso
from bcp.sourceindex import SourceIndex, type_name
from bcp.textio import atomic_write_text, dump_json, read_text, sha256_text, to_posix

log = logging.getLogger(__name__)

DEFAULT_HOPS = 3
MAX_EVIDENCE = 5
DEFAULT_PREFIX = "graph/"
KIND_RE = re.compile(r"\b(@interface|interface|enum|record|class)\b")
TABLE_ANN_RE = re.compile(r"@(Table|JoinTable|CollectionTable|SecondaryTable)\s*\(")
NAME_ATTR_RE = re.compile(r"""\bname\s*=\s*"([^"]+)\"""")
HBM_PACKAGE_RE = re.compile(r"<hibernate-mapping\b[^>]*\bpackage\s*=\s*\"([\w.]+)\"", re.S)
HBM_TAG_RE = re.compile(r"<(class|joined-subclass|union-subclass|subclass|set|bag|list|map|idbag|join)\b([^>]*)>", re.S)
HBM_ATTR_RE = re.compile(r"\b(name|table|entity-name)\s*=\s*\"([^\"]+)\"")
HBM_CLASS_TAGS = ("class", "joined-subclass", "union-subclass", "subclass")
ROUTE_RE = re.compile(r"""\.put\(\s*"(?P<name>[A-Z]{3}_[A-Z0-9]{2,4})"\s*,\s*(?P<var>\w+)\s*\)""")
CLASS_EDGE_TYPES = ("extends", "implements", "imports", "uses")
# Interfaces whose implementations are reached at run time through the interface (injected services,
# DAOs, handlers chosen by configuration): an ``implemented-by`` edge points from the interface to each
# implementing class so a dependents query of an implementation reaches the interface's callers.
DISPATCH_SUFFIXES = tuple(INTERFACE_SUFFIXES) + ("DAO", "Dao", "Handler")


def is_main_java(rel: str, main_dirs: Sequence[str] | None = None) -> bool:
    """A production Java file: under ``main_dirs`` when configured, else under a ``src/main/`` folder."""
    if not rel.endswith(".java"):
        return False
    if main_dirs:
        return any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in main_dirs)
    return "/src/main/" in f"/{rel}"


def _spec(rel: str, line: int) -> str:
    return f"{rel}#L{line}"


def _evidence(items: list[str]) -> dict[str, Any]:
    uniq = list(dict.fromkeys(items))
    out: dict[str, Any] = {"evidence": uniq[:MAX_EVIDENCE]}
    if len(uniq) > MAX_EVIDENCE:
        out["evidence_total"] = len(uniq)
    return out


def _decl(masked: list[str]) -> tuple[int | None, str]:
    """0-based declaration line of the first type and its kind."""
    for idx, line in enumerate(masked):
        if type_name(line):
            match = KIND_RE.search(line)
            return idx, (match.group(1).lstrip("@") if match else "class")
    return None, "class"


def _heritage(masked: list[str], idx: int | None) -> dict[str, set[str]]:
    """Simple names after ``extends`` / ``implements`` of the first type (generics removed)."""
    out: dict[str, set[str]] = {"extends": set(), "implements": set()}
    if idx is None:
        return out
    parts = []
    for line in masked[idx:idx + 15]:
        parts.append(line.split("{", 1)[0])
        if "{" in line:
            break
    head = " ".join(" ".join(parts).split())
    while re.search(r"<[^<>]*>", head):
        head = re.sub(r"<[^<>]*>", "", head)
    for keyword in ("extends", "implements"):
        match = re.search(rf"\b{keyword}\s+(.+?)(?=\bimplements\b|\bpermits\b|$)", head)
        if match:
            for item in match.group(1).split(","):
                item = item.strip()
                if item:
                    out[keyword].add(item.split()[0].rsplit(".", 1)[-1])
    return out


def class_edges(index: SourceIndex, types: TypeTable, rel: str, allowed: set[str]) -> list[dict[str, Any]]:
    """Typed edges ``rel -> target`` for every target of :func:`bcp.deps.file_references` in ``allowed``."""
    lines = index.lines(rel)
    masked = mask(lines)
    decl_idx, _kind = _decl(masked)
    heritage = _heritage(masked, decl_idx)
    import_line: dict[str, int] = {}
    for number, line in enumerate(lines, start=1):
        match = IMPORT_RE.match(line)
        if match:
            for path in types.resolve_import(match.group(2)):
                import_line.setdefault(path, number)
    body_start = max(import_line.values(), default=0)
    edges = []
    for target in sorted(file_references(index, types, rel) & allowed):
        stem = target.rsplit("/", 1)[-1][:-5]
        kind = next((k for k in ("extends", "implements") if stem in heritage[k]), None)
        if kind is not None and decl_idx is not None:
            line = decl_idx + 1
        elif target in import_line:
            kind, line = "imports", import_line[target]
        else:
            kind, line = "uses", 0
            token = re.compile(rf"\b{re.escape(stem)}\b")
            for number, text in enumerate(masked, start=1):
                if number > body_start and token.search(text):
                    line = number
                    break
        edges.append({"to": target, "type": kind, "line": line})
    return edges


def _load_json(path: Path) -> Any:
    import json

    try:
        return json.loads(read_text(path))
    except (OSError, ValueError) as exc:
        raise BcpInputError(f"cannot read {path}: {exc}") from exc


def _optional_json(path: Path) -> tuple[Any, str | None]:
    if not path.is_file():
        return None, None
    text = read_text(path)
    return _load_json(path), sha256_text(text)


class _Builder:
    def __init__(self, commit: str) -> None:
        self.commit = commit
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def node(self, node_id: str, node_type: str, **attrs: Any) -> dict[str, Any]:
        node = self.nodes.setdefault(node_id, {"id": node_id, "type": node_type})
        for key, value in attrs.items():
            if value is not None:
                node[key] = value
        return node

    def edge(self, src: str, dst: str, edge_type: str, evidence: list[str], source: str) -> None:
        key = (src, dst, edge_type)
        edge = self.edges.get(key)
        if edge is None:
            self.edges[key] = {"from": src, "to": dst, "type": edge_type, "evidence": list(evidence),
                               "source": source, "commit": self.commit}
        else:
            edge["evidence"] = list(dict.fromkeys(edge["evidence"] + evidence))

    def finish(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        edges = []
        for key in sorted(self.edges):
            edge = self.edges[key]
            edge.update(_evidence(edge["evidence"]))
            edges.append(edge)
        return [self.nodes[k] for k in sorted(self.nodes)], edges


def _scope(demo: set[str], adjacency: dict[str, set[str]], hops: int | None) -> dict[str, int]:
    """``{path: hop distance}`` for the demo files and everything within ``hops`` (None: all connected)."""
    dist = {p: 0 for p in demo}
    frontier = sorted(demo)
    level = 0
    while frontier and (hops is None or level < hops):
        level += 1
        nxt = []
        for path in frontier:
            for other in sorted(adjacency.get(path, ())):
                if other not in dist:
                    dist[other] = level
                    nxt.append(other)
        frontier = nxt
    return dist


def _hbm_tables(index: SourceIndex) -> list[tuple[str, str, str, str]]:
    """``(class fqn, table, edge type, evidence)`` from Hibernate mapping files."""
    out = []
    for rel in index.paths:
        if not rel.endswith(".hbm.xml"):
            continue
        text = "\n".join(index.lines(rel))
        text_nc = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
        pkg_match = HBM_PACKAGE_RE.search(text_nc)
        pkg = pkg_match.group(1) if pkg_match else ""
        current = ""
        for match in HBM_TAG_RE.finditer(text_nc):
            tag, attrs = match.group(1), dict(HBM_ATTR_RE.findall(match.group(2)))
            line = text_nc.count("\n", 0, match.start()) + 1
            if tag in HBM_CLASS_TAGS:
                name = attrs.get("name") or attrs.get("entity-name") or ""
                current = name if "." in name or not pkg else f"{pkg}.{name}"
                if attrs.get("table") and current:
                    out.append((current, attrs["table"].lower(), "maps-to-table", _spec(rel, line)))
            elif attrs.get("table") and current:
                out.append((current, attrs["table"].lower(), "joins-table", _spec(rel, line)))
    return out


def _annotation_tables(lines: list[str], masked: list[str]) -> list[tuple[str, str, int]]:
    """``(table, edge type, line)`` from JPA annotations (multi-line arguments read up to 4 lines)."""
    out = []
    for idx, text in enumerate(masked):
        match = TABLE_ANN_RE.search(text)
        if not match:
            continue
        raw = " ".join(lines[idx:idx + 4])
        start = raw.find("@" + match.group(1))
        name = NAME_ATTR_RE.search(raw[start:start + 400]) if start >= 0 else None
        if name:
            out.append((name.group(1).lower(), "maps-to-table" if match.group(1) in ("Table", "SecondaryTable")
                        else "joins-table", idx + 1))
    return out


def build_graph(repo: Path, config: BcpConfig, domains: Sequence[str], context: Path | None = None,
                hops: int | None = DEFAULT_HOPS, commit: str | None = None) -> dict[str, Any]:
    """Build the graph (JSON-friendly) from the repository and its generated ``.kiro/context`` files."""
    repo = Path(repo)
    context = Path(context) if context else repo / ".kiro" / "context"
    file_index_path = context / "file-index.json"
    if not file_index_path.is_file():
        raise BcpInputError(f"file index not found: {file_index_path} (run bcp generate first)")
    raw_index = _load_json(file_index_path)
    file_index = {to_posix(k): list(v) for k, v in raw_index.items()} if isinstance(raw_index, dict) else {}
    known = {d for slugs in file_index.values() for d in slugs}
    wanted = [d.strip() for d in domains if d.strip()]
    if not wanted:
        raise BcpInputError("give at least one domain")
    missing = [d for d in wanted if d not in known]
    if missing:
        raise BcpInputError(f"domains not in the file index: {', '.join(missing)}")
    impact, impact_sha = _optional_json(context / "impact-map.json")
    programs, programs_sha = _optional_json(context / "program-config.json")
    commit = commit or git_head(repo) or "unknown"

    index = SourceIndex(repo, config.source_extensions, config.exclude_dirs)
    if not index.has_source():
        raise BcpInputError(f"no source files under {repo}")
    types = TypeTable(index)
    main = {p for p in index.paths if is_main_java(p, config.main_source_dirs)}
    demo = {p for p in main if set(file_index.get(p, [])) & set(wanted)}
    if not demo:
        raise BcpInputError(f"no main-source Java file is mapped to {', '.join(wanted)}")

    raw_edges: dict[str, list[dict[str, Any]]] = {}
    adjacency: dict[str, set[str]] = {}
    for rel in sorted(main):
        raw_edges[rel] = class_edges(index, types, rel, main)
        for edge in raw_edges[rel]:
            adjacency.setdefault(rel, set()).add(edge["to"])
            adjacency.setdefault(edge["to"], set()).add(rel)
    scope = _scope(demo, adjacency, hops)

    b = _Builder(commit)
    fqn_of: dict[str, str] = {}
    class_id: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for rel in sorted(scope):
        pkg = types.package_of.get(rel, "")
        stem = rel.rsplit("/", 1)[-1][:-5]
        fqn = f"{pkg}.{stem}" if pkg else stem
        masked = mask(index.lines(rel))
        decl_idx, kind = _decl(masked)
        fqn_of[rel], class_id[rel], kinds[rel] = fqn, f"class:{fqn}", kind
        doms = file_index.get(rel, [])
        b.node(class_id[rel], "class", name=stem, fqn=fqn, package=pkg, path=rel, kind=kind,
               line=(decl_idx + 1) if decl_idx is not None else None, domains=doms,
               demo=rel in demo, hop=scope[rel])
        for dom in doms:
            b.node(f"domain:{dom}", "domain", name=dom, demo=dom in wanted)
            b.edge(class_id[rel], f"domain:{dom}", "in-domain", [f"{to_posix(file_index_path.name)}:{rel}"],
                   "file-index")
    for rel in sorted(scope):
        for edge in raw_edges[rel]:
            if edge["to"] not in scope:
                continue
            evidence = [_spec(rel, edge["line"])] if edge["line"] else [rel]
            b.edge(class_id[rel], class_id[edge["to"]], edge["type"], evidence, "code")
            target = edge["to"]
            if edge["type"] == "implements" and kinds[target] == "interface" \
                    and target.rsplit("/", 1)[-1][:-5].endswith(DISPATCH_SUFFIXES):
                # Callers hold the interface; the container or a factory hands them this implementation.
                b.edge(class_id[target], class_id[rel], "implemented-by", evidence, "code-derived")

    # Tables: JPA annotations in scope, then Hibernate mapping files for classes in scope.
    by_fqn = {fqn: rel for rel, fqn in fqn_of.items()}
    for rel in sorted(scope):
        lines = index.lines(rel)
        for table, edge_type, line in _annotation_tables(lines, mask(lines)):
            b.node(f"table:{table}", "table", name=table)
            b.edge(class_id[rel], f"table:{table}", edge_type, [_spec(rel, line)], "code")
    for fqn, table, edge_type, spec in _hbm_tables(index):
        if fqn in by_fqn:
            b.node(f"table:{table}", "table", name=table)
            b.edge(class_id[by_fqn[fqn]], f"table:{table}", edge_type, [spec], "code")
    domain_tables = (impact or {}).get("domains", {}) if isinstance(impact, dict) else {}
    for node in [n for n in b.nodes.values() if n["type"] == "table"]:
        owners = sorted(s for s, d in domain_tables.items() if node["name"] in (d.get("owned_tables") or []))
        refs = sorted(s for s, d in domain_tables.items() if node["name"] in (d.get("shared_tables_referenced") or []))
        node.update(owner_domains=owners, referenced_by_domains=refs, shared=len(set(owners) | set(refs)) > 1)

    # Configuration keys (joined with per-program values).
    if isinstance(programs, dict):
        _config_nodes(b, programs, class_id)

    # Endpoints of the demo domains.
    rel_by_stem: dict[str, list[str]] = {}
    for rel in scope:
        rel_by_stem.setdefault(rel.rsplit("/", 1)[-1][:-5], []).append(rel)
    compiled = {label: re.compile(rx) for label, rx in config.integration_patterns.items()}
    for rel in sorted(demo):
        lines = index.lines(rel)
        masked = mask(lines)
        node = b.nodes[class_id[rel]]
        name = node["name"]
        if kinds[rel] == "interface" and name.endswith(INTERFACE_SUFFIXES):
            ep = f"endpoint:service:{name}"
            b.node(ep, "endpoint", name=name, kind="service-api", domains=file_index.get(rel, []))
            b.edge(ep, class_id[rel], "exposes", [_spec(rel, node.get("line") or 1)], "code")
        hits: dict[str, list[str]] = {}
        for number, text in enumerate(masked, start=1):
            for label, regex in compiled.items():
                if regex.search(text):
                    hits.setdefault(label, []).append(_spec(rel, number))
        for label, specs in sorted(hits.items()):
            ep = f"endpoint:integration:{label}"
            b.node(ep, "endpoint", name=label, kind="integration")
            b.edge(class_id[rel], ep, "integrates-via", specs, "code")
    for rel in sorted(scope):
        lines = index.lines(rel)
        masked = mask(lines)
        for idx, text in enumerate(lines):
            match = ROUTE_RE.search(text)
            if not match or ".put(" not in masked[idx]:
                continue
            var = match.group("var")
            declared = re.compile(rf"\b([A-Z]\w*)\s+{re.escape(var)}\b")
            handler = None
            for back in range(idx, max(-1, idx - 30), -1):
                found = declared.search(masked[back])
                if found:
                    handler = found.group(1)
                    break
            targets = [t for t in rel_by_stem.get(handler or "", []) if t in file_references(index, types, rel)
                       or types.package_of.get(t) == types.package_of.get(rel)]
            if len(targets) != 1:
                log.debug("route %s in %s: handler %r not resolved", match.group("name"), rel, handler)
                continue
            ep = f"endpoint:message:{match.group('name')}"
            b.node(ep, "endpoint", name=match.group("name"), kind="message-route", declared_in=class_id[rel])
            b.edge(ep, class_id[targets[0]], "routes-to", [_spec(rel, idx + 1)], "code")

    nodes, edges = b.finish()
    node_counts: dict[str, int] = {}
    for node in nodes:
        node_counts[node["type"]] = node_counts.get(node["type"], 0) + 1
    edge_counts: dict[str, int] = {}
    for edge in edges:
        edge_counts[edge["type"]] = edge_counts.get(edge["type"], 0) + 1
    return {
        "schema": SCHEMA,
        "generated_at": now_iso(),
        "commit": commit,
        "repo": repo.name,
        "demo_domains": wanted,
        "scope": {"class_hops": hops, "main_java_files": len(main), "demo_classes": len(demo),
                  "classes": len(scope),
                  "note": f"demo-domain classes plus classes within {hops} dependency hops (either direction); "
                          "queries of that depth started at a demo class are complete" if hops is not None
                  else "every main-source class connected to a demo-domain class"},
        "edge_direction": "from depends on to",
        "inputs": {"file-index.json": sha256_text(read_text(file_index_path)),
                   "impact-map.json": impact_sha, "program-config.json": programs_sha},
        "node_counts": dict(sorted(node_counts.items())),
        "edge_counts": dict(sorted(edge_counts.items())),
        "nodes": nodes,
        "edges": edges,
    }


def _config_nodes(b: _Builder, programs: dict[str, Any], class_id: dict[str, str]) -> None:
    catalogue = programs.get("catalogue") or {}
    comparison = programs.get("comparison") or {}
    affects = programs.get("affects") or {}
    differing = set(programs.get("differing_keys") or [])
    synthetic = any(p.get("synthetic") for p in programs.get("programs") or [])
    for key in sorted(catalogue):
        entry = catalogue[key] or {}
        # A site in the file that defines the key's constant is the definition or its seeded default
        # (``new GlobalProperty(GP_X, "1")``), not a run-time read.
        decl_files = {to_posix(c.get("path", "")) for c in entry.get("constants") or []}
        reads: dict[str, list[str]] = {}
        declares: dict[str, list[str]] = {}
        for site in (affects.get(key) or {}).get("read_sites") or entry.get("read_sites") or []:
            rel, line = to_posix(site.get("path", "")), site.get("line")
            if rel not in class_id:
                continue
            target = declares if rel in decl_files else reads
            target.setdefault(rel, []).append(_spec(rel, line))
        for const in entry.get("constants") or []:
            rel = to_posix(const.get("path", ""))
            if rel in class_id:
                declares.setdefault(rel, []).append(_spec(rel, const.get("line")))
        if not reads and not declares:
            continue
        per_program = {pid: v.get("effective") for pid, v in sorted((comparison.get(key) or {}).items())}
        groups = (affects.get(key) or {}).get("groups")
        if groups is None:
            grouped: dict[str, list[str]] = {}
            for pid, value in per_program.items():
                grouped.setdefault(str(value), []).append(pid)
            groups = [{"effective": v, "programs": p} for v, p in sorted(grouped.items())]
        default = entry.get("default") or {}
        node_id = f"config:{key}"
        b.node(node_id, "config", name=key, key=key,
               constants=[c.get("constant") for c in entry.get("constants") or [] if c.get("constant")],
               default=({"value": default.get("value"), "at": _spec(to_posix(default["path"]), default["line"])}
                        if default.get("path") else None),
               read_by=list(entry.get("read_by") or []), read_site_total=entry.get("read_site_total"),
               programs=per_program, groups=groups, differs=key in differing, synthetic_snapshots=synthetic)
        for rel, specs in sorted(reads.items()):
            b.edge(class_id[rel], node_id, "reads-config", specs, "program-config")
        for rel, specs in sorted(declares.items()):
            b.edge(class_id[rel], node_id, "declares-config", specs, "program-config")


# --------------------------------------------------------------------------- write and upload


def write_graph(data: dict[str, Any], paths: Sequence[Path]) -> tuple[str, list[Path]]:
    """Write the graph JSON (deterministic, UTF-8 without BOM) to every path; return text and paths."""
    text = dump_json(data)
    written = []
    for path in paths:
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, text)
        except OSError as exc:
            raise OutputWriteError(f"cannot write {path}: {exc}") from exc
        written.append(path)
    return text, written


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/prefix/`` → ``(bucket, "prefix/")`` (the prefix defaults to ``graph/``)."""
    match = re.fullmatch(r"s3://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])(?:/(.*))?", uri.strip())
    if not match:
        raise BcpInputError(f"not an s3://bucket[/prefix] URI: {uri!r}")
    prefix = match.group(2) or DEFAULT_PREFIX
    return match.group(1), prefix if prefix.endswith("/") else prefix + "/"


def upload_graph(text: str, commit: str, bucket: str, prefix: str = DEFAULT_PREFIX, region: str = "us-east-1",
                 client_factory: Callable[[str], Any] | None = None) -> list[dict[str, Any]]:
    """Put ``<prefix>graph-<commit>.json`` (versioned) and ``<prefix>latest.json``; verify each by HEAD."""
    if not re.fullmatch(r"[0-9A-Za-z._-]+", commit):
        raise BcpInputError(f"commit {commit!r} is not usable in an object key")
    if client_factory is None:
        os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BcpInputError("boto3 is required for --upload (pip install boto3)") from exc

        def client_factory(name: str) -> Any:
            return boto3.client(name, region_name=region)

    s3 = client_factory("s3")
    body = text.encode("utf-8")
    digest = sha256_text(text)
    out = []
    for key in (f"{prefix}graph-{commit}.json", f"{prefix}latest.json"):
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json",
                      Metadata={"commit": commit, "schema": SCHEMA, "sha256": digest})
        head = s3.head_object(Bucket=bucket, Key=key)
        size = int(head.get("ContentLength", -1))
        if size != len(body):
            raise OutputWriteError(f"s3://{bucket}/{key}: uploaded {len(body)} bytes, HEAD reports {size}")
        out.append({"key": key, "bytes": size, "sha256": digest, "etag": str(head.get("ETag", "")).strip('"')})
    return out


def run_build(repo: Path, config: BcpConfig, domains: Sequence[str], out: Path | None = None,
              copies: Sequence[Path] = (), context: Path | None = None, hops: int | None = DEFAULT_HOPS,
              upload: str | None = None, region: str = "us-east-1",
              client_factory: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """``bcp graph build``: build, write ``out`` (default ``<repo>/.kiro/context/graph.json``) plus copies,
    optionally upload. Returns a summary."""
    data = build_graph(repo, config, domains, context, hops)
    target = Path(out) if out else Path(repo) / ".kiro" / "context" / "graph.json"
    paths = [target] + [Path(c) / "graph.json" if Path(c).suffix.lower() != ".json" else Path(c) for c in copies]
    text, written = write_graph(data, paths)
    summary: dict[str, Any] = {"commit": data["commit"], "node_counts": data["node_counts"],
                               "edge_counts": data["edge_counts"], "nodes": len(data["nodes"]),
                               "edges": len(data["edges"]), "scope": data["scope"], "bytes": len(text.encode("utf-8")),
                               "written": [str(p) for p in written], "uploaded": []}
    if upload:
        bucket, prefix = parse_s3_uri(upload)
        summary["uploaded"] = [dict(u, uri=f"s3://{bucket}/{u['key']}")
                               for u in upload_graph(text, data["commit"], bucket, prefix, region, client_factory)]
    return summary
