"""In-memory queries over the persistent component graph (``graph.json``, schema ``bcp.graph/1``).

Standard library only, so a Kiro hook can import it cheaply::

    from bcp.graphq import Graph
    g = Graph.load(".kiro/context/graph.json")
    g.dependents("EncounterServiceImpl", depth=2)

Edges point from the dependent to what it depends on (``A --imports--> B``: A depends on B). Queries:

* :meth:`Graph.dependencies` follows edges forward (what ``x`` needs);
* :meth:`Graph.dependents` follows edges backward (what breaks when ``x`` changes), depth at most 3;
* :meth:`Graph.path` finds a shortest dependency chain between two nodes;
* :meth:`Graph.affected_programs` joins a configuration key with the per-program values captured from
  ``program-config.json`` and the components that read the key.

Structural edges (``in-domain``) are not followed unless asked for; they would connect every class of
a domain to every other one.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "bcp.graph/1"
STRUCTURAL_EDGES = frozenset({"in-domain"})
MAX_DEPENDENT_DEPTH = 3
MAX_DEPTH = 6
MAX_PATH = 12


class GraphError(ValueError):
    """Bad graph file or a query that cannot be answered (unknown or ambiguous node, bad depth)."""


class AmbiguousNode(GraphError):
    """A name matches more than one node; ``candidates`` lists their ids."""

    def __init__(self, name: str, candidates: list[str]) -> None:
        super().__init__(f"{name!r} matches {len(candidates)} nodes: {', '.join(candidates[:10])}"
                         + (" ..." if len(candidates) > 10 else ""))
        self.candidates = candidates


def _brief(edge: dict[str, Any]) -> dict[str, Any]:
    return {"from": edge["from"], "to": edge["to"], "type": edge["type"], "evidence": edge.get("evidence", [])}


class Graph:
    """The graph held in memory with forward and backward adjacency lists."""

    def __init__(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise GraphError(f"not a {SCHEMA} graph")
        self.meta = {k: v for k, v in data.items() if k not in ("nodes", "edges")}
        self.nodes: dict[str, dict[str, Any]] = {n["id"]: n for n in data.get("nodes", [])}
        self.edges: list[dict[str, Any]] = list(data.get("edges", []))
        self.out: dict[str, list[dict[str, Any]]] = {}
        self.inc: dict[str, list[dict[str, Any]]] = {}
        for edge in self.edges:
            if edge["from"] not in self.nodes or edge["to"] not in self.nodes:
                raise GraphError(f"edge {edge['from']} -> {edge['to']} names an unknown node")
            self.out.setdefault(edge["from"], []).append(edge)
            self.inc.setdefault(edge["to"], []).append(edge)
        self._aliases: dict[str, list[str]] | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Graph":
        """Read ``graph.json`` (UTF-8, BOM tolerated)."""
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError) as exc:
            raise GraphError(f"cannot read graph {path}: {exc}") from exc

    @property
    def commit(self) -> str | None:
        """Commit the graph was built from."""
        return self.meta.get("commit")

    # ------------------------------------------------------------------ name resolution

    def _alias_index(self) -> dict[str, list[str]]:
        if self._aliases is None:
            index: dict[str, list[str]] = {}
            for node_id, node in self.nodes.items():
                names = {node_id, node.get("name"), node.get("fqn"), node.get("path"), node.get("key")}
                names.update(node.get("constants") or [])
                for name in names:
                    if name:
                        bucket = index.setdefault(str(name).lower(), [])
                        if node_id not in bucket:
                            bucket.append(node_id)
            self._aliases = index
        return self._aliases

    def resolve(self, name: str) -> str:
        """Node id for an id, class simple or qualified name, path, table, key, constant or endpoint name.

        A name shared by several nodes resolves to the single class node among them when there is one
        (a class and its table often share a name); otherwise :class:`AmbiguousNode` is raised."""
        if name in self.nodes:
            return name
        found = sorted(self._alias_index().get(name.strip().lower(), []))
        if not found:
            raise GraphError(f"no node named {name!r}")
        if len(found) > 1:
            classes = [n for n in found if self.nodes[n]["type"] == "class"]
            if len(classes) == 1:
                return classes[0]
            raise AmbiguousNode(name, found)
        return found[0]

    def node(self, name: str) -> dict[str, Any]:
        """The node record for ``name`` (see :meth:`resolve`)."""
        return self.nodes[self.resolve(name)]

    # ------------------------------------------------------------------ traversal

    def _allowed(self, edge_types: Iterable[str] | None) -> set[str] | None:
        return set(edge_types) if edge_types else None

    def _walk(self, start: str, depth: int, forward: bool, edge_types: Iterable[str] | None,
              include_structural: bool) -> list[dict[str, Any]]:
        allowed = self._allowed(edge_types)
        adjacency = self.out if forward else self.inc
        seen = {start}
        results: list[dict[str, Any]] = []
        frontier = [start]
        for level in range(1, depth + 1):
            nxt: list[str] = []
            for current in frontier:
                for edge in sorted(adjacency.get(current, []), key=lambda e: (e["type"], e["to"], e["from"])):
                    if allowed is not None and edge["type"] not in allowed:
                        continue
                    if allowed is None and not include_structural and edge["type"] in STRUCTURAL_EDGES:
                        continue
                    other = edge["to"] if forward else edge["from"]
                    if other in seen:
                        continue
                    seen.add(other)
                    nxt.append(other)
                    results.append(self._row(other, level, edge))
            frontier = nxt
            if not frontier:
                break
        return sorted(results, key=lambda r: (r["depth"], r["type"], r["id"]))

    def _row(self, node_id: str, level: int, edge: dict[str, Any]) -> dict[str, Any]:
        node = self.nodes[node_id]
        row = {"id": node_id, "type": node["type"], "name": node.get("name"), "depth": level, "via": _brief(edge)}
        for key in ("domains", "path"):
            if key in node:
                row[key] = node[key]
        return row

    @staticmethod
    def _check_depth(depth: int, limit: int) -> int:
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= limit:
            raise GraphError(f"depth must be an integer in 1..{limit}")
        return depth

    def _summary(self, query: str, start: str, depth: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        domains: dict[str, int] = {}
        for row in rows:
            by_type[row["type"]] = by_type.get(row["type"], 0) + 1
            for dom in row.get("domains") or []:
                domains[dom] = domains.get(dom, 0) + 1
        return {"query": query, "node": start, "node_type": self.nodes[start]["type"], "depth": depth,
                "commit": self.commit, "count": len(rows), "by_type": dict(sorted(by_type.items())),
                "by_domain": dict(sorted(domains.items())), "results": rows}

    def dependencies(self, name: str, depth: int = 1, edge_types: Iterable[str] | None = None,
                     include_structural: bool = False) -> dict[str, Any]:
        """What ``name`` depends on, up to ``depth`` hops (1..6)."""
        start = self.resolve(name)
        depth = self._check_depth(depth, MAX_DEPTH)
        return self._summary("dependencies", start, depth,
                             self._walk(start, depth, True, edge_types, include_structural))

    def dependents(self, name: str, depth: int = 1, edge_types: Iterable[str] | None = None,
                   include_structural: bool = False) -> dict[str, Any]:
        """What depends on ``name`` (directly or transitively), up to ``depth`` hops (1..3)."""
        start = self.resolve(name)
        depth = self._check_depth(depth, MAX_DEPENDENT_DEPTH)
        return self._summary("dependents", start, depth,
                             self._walk(start, depth, False, edge_types, include_structural))

    def _shortest(self, start: str, goal: str, allowed: set[str] | None, undirected: bool,
                  max_len: int) -> list[tuple[dict[str, Any], str]] | None:
        parent: dict[str, tuple[str, dict[str, Any], str] | None] = {start: None}
        queue = deque([(start, 0)])
        while queue:
            current, dist = queue.popleft()
            if current == goal:
                hops: list[tuple[dict[str, Any], str]] = []
                while parent[current] is not None:
                    prev, edge, direction = parent[current]  # type: ignore[misc]
                    hops.append((edge, direction))
                    current = prev
                return hops[::-1]
            if dist >= max_len:
                continue
            steps = [(e, "forward", e["to"]) for e in self.out.get(current, [])]
            if undirected:
                steps += [(e, "reverse", e["from"]) for e in self.inc.get(current, [])]
            for edge, direction, other in sorted(steps, key=lambda s: (s[2], s[0]["type"], s[1])):
                if allowed is not None and edge["type"] not in allowed:
                    continue
                if allowed is None and edge["type"] in STRUCTURAL_EDGES:
                    continue
                if other not in parent:
                    parent[other] = (current, edge, direction)
                    queue.append((other, dist + 1))
        return None

    def path(self, a: str, b: str, edge_types: Iterable[str] | None = None, undirected: bool = False,
             max_len: int = MAX_PATH) -> dict[str, Any]:
        """Shortest dependency chain ``a -> ... -> b``; when there is none, ``b -> ... -> a`` (reported as
        ``direction: b->a``). ``undirected`` also lets hops run against the edge direction (each hop is
        then marked ``forward`` or ``reverse``)."""
        src, dst = self.resolve(a), self.resolve(b)
        allowed = self._allowed(edge_types)
        result: dict[str, Any] = {"query": "path", "from": src, "to": dst, "commit": self.commit}
        attempts = [("a->b", src, dst, False), ("b->a", dst, src, False)]
        if undirected:
            attempts.append(("mixed", src, dst, True))
        for label, s, d, und in attempts:
            hops = self._shortest(s, d, allowed, und, max_len)
            if hops is not None:
                nodes = [s] + [(e["to"] if direction == "forward" else e["from"]) for e, direction in hops]
                result.update(found=True, direction=label, length=len(hops), nodes=nodes,
                              hops=[dict(_brief(e), direction=direction) for e, direction in hops])
                return result
        result.update(found=False, direction=None, length=None, nodes=[], hops=[])
        return result

    # ------------------------------------------------------------------ configuration join

    def affected_programs(self, key: str, depth: int = 1) -> dict[str, Any]:
        """Which programs a change to configuration ``key`` affects and which components read it.

        ``programs`` are the per-program effective values from ``program-config.json``; ``groups`` puts
        programs with the same effective value together, so a change to the key's handling affects each
        group differently when ``differs`` is true. ``impacted`` lists the dependents of the reading
        components up to ``depth`` hops (1..3)."""
        node_id = self.resolve(key)
        node = self.nodes[node_id]
        if node["type"] != "config":
            raise GraphError(f"{key!r} resolves to a {node['type']} node ({node_id}), not a configuration key")
        depth = self._check_depth(depth, MAX_DEPENDENT_DEPTH)
        readers = [self._row(e["from"], 1, e) for e in sorted(self.inc.get(node_id, []), key=lambda e: e["from"])
                   if e["type"] == "reads-config"]
        declared = [self._row(e["from"], 1, e) for e in sorted(self.inc.get(node_id, []), key=lambda e: e["from"])
                    if e["type"] == "declares-config"]
        reader_ids = {r["id"] for r in readers} | {node_id}
        impacted: dict[str, dict[str, Any]] = {}
        for reader in sorted(reader_ids - {node_id}):
            for row in self._walk(reader, depth, False, None, False):
                if row["id"] not in reader_ids and (row["id"] not in impacted
                                                    or row["depth"] < impacted[row["id"]]["depth"]):
                    impacted[row["id"]] = dict(row, through=reader)
        domains = sorted({d for r in readers for d in r.get("domains") or []} | set(node.get("read_by") or []))
        return {
            "query": "affected_programs", "node": node_id, "key": node.get("key"), "commit": self.commit,
            "differs": bool(node.get("differs")), "programs": node.get("programs") or {},
            "groups": node.get("groups") or [], "default": node.get("default"),
            "domains": domains, "readers": readers, "declared_by": declared,
            "impacted": sorted(impacted.values(), key=lambda r: (r["depth"], r["type"], r["id"])),
            "note": "program values come from read-only configuration snapshots"
                    + (" (synthetic)" if node.get("synthetic_snapshots") else ""),
        }


def load(path: str | Path) -> Graph:
    """Shorthand for :meth:`Graph.load`."""
    return Graph.load(path)
