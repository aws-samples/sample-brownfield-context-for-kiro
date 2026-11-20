"""Tests for bcp.graph (graph build, write, upload) and bcp.graphq (in-memory queries)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from bcp import graph as graph_mod
from bcp.cli import main
from bcp.config import BcpConfig
from bcp.errors import BcpInputError, OutputWriteError
from bcp.graph import build_graph, parse_s3_uri, run_build, upload_graph
from bcp.graphq import SCHEMA, AmbiguousNode, Graph, GraphError

COMMIT = "abc1234"
J = "src/main/java/org/demo"
FILES = {
    f"{J}/BaseThing.java": "package org.demo;\n\npublic abstract class BaseThing {\n}\n",
    f"{J}/Part.java": "package org.demo;\n\npublic class Part extends BaseThing {\n}\n",
    f"{J}/Widget.java": (
        "package org.demo;\n"
        "\n"
        "import javax.persistence.Entity;\n"
        "import javax.persistence.Table;\n"
        "\n"
        "@Entity\n"
        "@Table(name = \"widget\")\n"
        "public class Widget extends BaseThing {\n"
        "    // a Part is attached later\n"
        "    private Part part;\n"
        "}\n"
    ),
    f"{J}/api/WidgetService.java": (
        "package org.demo.api;\n"
        "\n"
        "import org.demo.Widget;\n"
        "\n"
        "public interface WidgetService {\n"
        "    Widget save(Widget widget);\n"
        "}\n"
    ),
    f"{J}/api/impl/WidgetServiceImpl.java": (
        "package org.demo.api.impl;\n"
        "\n"
        "import java.io.FileOutputStream;\n"
        "import org.demo.Widget;\n"
        "import org.demo.api.WidgetService;\n"
        "import org.demo.util.Constants;\n"
        "\n"
        "public class WidgetServiceImpl implements WidgetService {\n"
        "    public Widget save(Widget widget) {\n"
        "        String mode = Settings.get(Constants.GP_MODE);\n"
        "        new FileOutputStream(\"x\");\n"
        "        return widget;\n"
        "    }\n"
        "}\n"
    ),
    f"{J}/util/Constants.java": (
        "package org.demo.util;\n"
        "\n"
        "public final class Constants {\n"
        "    public static final String GP_MODE = \"widget.mode\";\n"
        "    public static final String[] SEEDS = { GP_MODE };\n"
        "}\n"
    ),
    f"{J}/handler/FooHandler.java": "package org.demo.handler;\n\npublic class FooHandler {\n}\n",
    f"{J}/AppConfig.java": (
        "package org.demo;\n"
        "\n"
        "import java.util.HashMap;\n"
        "import java.util.Map;\n"
        "import org.demo.handler.FooHandler;\n"
        "\n"
        "public class AppConfig {\n"
        "    public Map<String, Object> routes(FooHandler fooHandler) {\n"
        "        Map<String, Object> map = new HashMap<>();\n"
        "        // map.put(\"ADT_A01\", fooHandler); is not a route\n"
        "        map.put(\"ORU_R01\", fooHandler);\n"
        "        return map;\n"
        "    }\n"
        "}\n"
    ),
    f"{J}/web/WidgetController.java": (
        "package org.demo.web;\n"
        "\n"
        "import org.demo.api.WidgetService;\n"
        "\n"
        "public class WidgetController {\n"
        "    private WidgetService service;\n"
        "}\n"
    ),
    "src/main/java/org/other/Far.java": (
        "package org.other;\n\nimport org.demo.web.WidgetController;\n\npublic class Far {\n"
        "    WidgetController c;\n}\n"
    ),
    "src/test/java/org/demo/WidgetTest.java": (
        "package org.demo;\n\nimport org.demo.api.WidgetService;\n\npublic class WidgetTest {\n"
        "    WidgetService s;\n}\n"
    ),
    "src/main/resources/org/demo/Part.hbm.xml": (
        "<?xml version=\"1.0\"?>\n"
        "<hibernate-mapping package=\"org.demo\">\n"
        "  <!-- <class name=\"Ghost\" table=\"ghost\"> -->\n"
        "  <class name=\"Part\"\n"
        "         table=\"part\">\n"
        "    <set name=\"tags\" table=\"part_tag\">\n"
        "    </set>\n"
        "  </class>\n"
        "</hibernate-mapping>\n"
    ),
}
WIDGET = ["BaseThing", "Part", "Widget", "api/WidgetService", "api/impl/WidgetServiceImpl", "handler/FooHandler",
          "AppConfig"]


def _line(rel: str, needle: str) -> int:
    return next(i for i, text in enumerate(FILES[rel].split("\n"), start=1) if needle in text)


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    ctx = repo / ".kiro" / "context"
    ctx.mkdir(parents=True)
    file_index = {f"{J}/{name}.java": ["widget"] for name in WIDGET}
    file_index[f"{J}/web/WidgetController.java"] = ["web"]
    file_index["src/test/java/org/demo/WidgetTest.java"] = ["widget"]
    (ctx / "file-index.json").write_text(json.dumps(file_index), encoding="utf-8")
    impact = {"domains": {"widget": {"owned_tables": ["widget", "part"], "shared_tables_referenced": []},
                          "web": {"owned_tables": [], "shared_tables_referenced": ["widget"]}}}
    (ctx / "impact-map.json").write_text(json.dumps(impact), encoding="utf-8")
    consts = f"{J}/util/Constants.java"
    impl = f"{J}/api/impl/WidgetServiceImpl.java"
    programs = {
        "schema": "bcp.programs/1",
        "catalogue": {
            "widget.mode": {
                "constants": [{"constant": "GP_MODE", "line": _line(consts, "GP_MODE"), "path": consts}],
                "default": {"value": "fast", "line": _line(impl, "GP_MODE"), "path": impl},
                "read_by": ["widget"], "read_site_total": 2,
                "read_sites": [{"domains": ["widget"], "line": _line(impl, "GP_MODE"), "path": impl},
                               {"domains": [], "line": _line(consts, "SEEDS"), "path": consts}],
            },
            "unread.key": {"constants": [], "read_by": [], "read_sites": [{"line": 1, "path": "x/Nowhere.java"}]},
        },
        "comparison": {"widget.mode": {"a": {"effective": "fast"}, "b": {"effective": "slow"},
                                       "c": {"effective": "fast"}}},
        "differing_keys": ["widget.mode"],
        "programs": [{"program_id": p, "synthetic": True} for p in "abc"],
        "affects": {},
    }
    (ctx / "program-config.json").write_text(json.dumps(programs), encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def fixed_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_mod, "git_head", lambda repo: COMMIT)


def _build(repo: Path, hops: int | None = 3) -> dict[str, Any]:
    return build_graph(repo, BcpConfig(), ["widget"], hops=hops)


def _edges(data: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {(e["from"], e["to"], e["type"]): e for e in data["edges"]}


C = "class:org.demo."


# --------------------------------------------------------------------------- build


def test_build_nodes_and_typed_edges_with_evidence(demo_repo: Path) -> None:
    data = _build(demo_repo)
    assert data["schema"] == SCHEMA and data["commit"] == COMMIT and data["demo_domains"] == ["widget"]
    ids = {n["id"] for n in data["nodes"]}
    assert "class:org.demo.WidgetTest" not in ids  # test sources are not components
    assert {"table:widget", "table:part", "table:part_tag", "config:widget.mode", "endpoint:service:WidgetService",
            "endpoint:message:ORU_R01", "endpoint:integration:file", "domain:widget", "domain:web"} <= ids
    assert "table:ghost" not in ids and "config:unread.key" not in ids and "endpoint:message:ADT_A01" not in ids
    e = _edges(data)
    w, impl, svc = f"{J}/Widget.java", f"{J}/api/impl/WidgetServiceImpl.java", f"{J}/api/WidgetService.java"
    assert e[(C + "Widget", C + "BaseThing", "extends")]["evidence"] == [f"{w}#L{_line(w, 'extends BaseThing')}"]
    assert e[(C + "Widget", C + "Part", "uses")]["evidence"] == [f"{w}#L{_line(w, 'private Part')}"]
    assert e[(C + "api.impl.WidgetServiceImpl", C + "api.WidgetService", "implements")]
    assert e[(C + "api.WidgetService", C + "api.impl.WidgetServiceImpl", "implemented-by")]["source"] == "code-derived"
    assert e[(C + "api.impl.WidgetServiceImpl", C + "Widget", "imports")]["evidence"] == [
        f"{impl}#L{_line(impl, 'import org.demo.Widget')}"]
    assert e[(C + "Widget", "table:widget", "maps-to-table")]["evidence"] == [f"{w}#L{_line(w, '@Table')}"]
    hbm = "src/main/resources/org/demo/Part.hbm.xml"
    assert e[(C + "Part", "table:part", "maps-to-table")]["evidence"] == [f"{hbm}#L4"]
    assert e[(C + "Part", "table:part_tag", "joins-table")]["evidence"] == [f"{hbm}#L6"]
    assert e[(C + "api.impl.WidgetServiceImpl", "config:widget.mode", "reads-config")]["source"] == "program-config"
    consts = f"{J}/util/Constants.java"
    assert e[(C + "util.Constants", "config:widget.mode", "declares-config")]["evidence"] == [
        f"{consts}#L{_line(consts, 'SEEDS')}", f"{consts}#L{_line(consts, 'GP_MODE =')}"]
    assert (C + "util.Constants", "config:widget.mode", "reads-config") not in e  # definition and seed, not reads
    assert e[("endpoint:service:WidgetService", C + "api.WidgetService", "exposes")]["evidence"] == [
        f"{svc}#L{_line(svc, 'interface WidgetService')}"]
    route = e[("endpoint:message:ORU_R01", C + "handler.FooHandler", "routes-to")]
    assert route["evidence"] == [f"{J}/AppConfig.java#L{_line(f'{J}/AppConfig.java', 'ORU_R01')}"]
    assert (C + "api.impl.WidgetServiceImpl", "endpoint:integration:file", "integrates-via") in e
    assert (C + "web.WidgetController", "domain:web", "in-domain") in e
    for edge in data["edges"]:
        assert edge["commit"] == COMMIT and edge["evidence"], edge
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["table:widget"]["owner_domains"] == ["widget"]
    assert nodes["table:widget"]["referenced_by_domains"] == ["web"] and nodes["table:widget"]["shared"] is True
    cfg = nodes["config:widget.mode"]
    assert cfg["programs"] == {"a": "fast", "b": "slow", "c": "fast"} and cfg["differs"] is True
    assert cfg["groups"] == [{"effective": "fast", "programs": ["a", "c"]}, {"effective": "slow", "programs": ["b"]}]
    assert nodes[C + "Widget"]["kind"] == "class" and nodes[C + "api.WidgetService"]["kind"] == "interface"
    assert nodes[C + "Widget"]["demo"] is True and nodes[C + "web.WidgetController"]["demo"] is False
    counts = data["edge_counts"]
    assert sum(counts.values()) == len(data["edges"]) and sum(data["node_counts"].values()) == len(data["nodes"])


def test_hops_bound_the_class_scope(demo_repo: Path) -> None:
    near = {n["id"]: n for n in _build(demo_repo, hops=1)["nodes"]}
    assert "class:org.other.Far" not in near and near[C + "web.WidgetController"]["hop"] == 1
    far = {n["id"]: n for n in _build(demo_repo, hops=2)["nodes"]}
    assert far["class:org.other.Far"]["hop"] == 2
    assert C + "util.Constants" in near  # one hop from the service implementation


def test_build_is_deterministic_and_validates_input(demo_repo: Path) -> None:
    one, two = _build(demo_repo), _build(demo_repo)
    assert one["nodes"] == two["nodes"] and one["edges"] == two["edges"]
    with pytest.raises(BcpInputError, match="not in the file index"):
        build_graph(demo_repo, BcpConfig(), ["nope"])
    with pytest.raises(BcpInputError, match="file index not found"):
        build_graph(demo_repo, BcpConfig(), ["widget"], context=demo_repo / "missing")


# --------------------------------------------------------------------------- queries


@pytest.fixture
def g(demo_repo: Path) -> Graph:
    return Graph(_build(demo_repo))


def test_dependents_and_dependencies(g: Graph) -> None:
    res = g.dependents("Widget", depth=1)
    found = {r["id"]: r for r in res["results"]}
    assert {C + "api.WidgetService", C + "api.impl.WidgetServiceImpl"} <= set(found)
    assert found[C + "api.WidgetService"]["via"]["type"] == "imports" and res["node"] == C + "Widget"
    # An implementation reaches the interface's callers through implemented-by.
    deep = {r["id"]: r["depth"] for r in g.dependents("WidgetServiceImpl", depth=2)["results"]}
    assert deep[C + "api.WidgetService"] == 1 and deep[C + "web.WidgetController"] == 2
    assert "class:org.other.Far" not in deep
    assert "class:org.other.Far" in {r["id"] for r in g.dependents("WidgetServiceImpl", depth=3)["results"]}
    deps = g.dependencies("org.demo.web.WidgetController", depth=3)
    by_id = {r["id"]: r["depth"] for r in deps["results"]}
    assert by_id[C + "api.WidgetService"] == 1 and by_id[C + "Widget"] == 2 and by_id["table:widget"] == 3
    assert deps["by_type"]["class"] >= 3
    tables = g.dependencies("Part", 1, edge_types=["maps-to-table", "joins-table"])
    assert [r["id"] for r in tables["results"]] == ["table:part", "table:part_tag"]
    assert all(r["type"] != "domain" for r in deps["results"])
    with_dom = g.dependencies("Widget", 1, include_structural=True)
    assert "domain:widget" in {r["id"] for r in with_dom["results"]}
    with pytest.raises(GraphError, match="1..3"):
        g.dependents("Widget", depth=4)
    with pytest.raises(GraphError, match="1..6"):
        g.dependencies("Widget", depth=0)


def test_path_queries(g: Graph) -> None:
    res = g.path("WidgetController", "table:widget")
    assert res["found"] and res["direction"] == "a->b" and res["length"] == 3
    assert res["nodes"] == [C + "web.WidgetController", C + "api.WidgetService", C + "Widget", "table:widget"]
    assert res["hops"][-1]["type"] == "maps-to-table" and res["hops"][0]["evidence"]
    back = g.path("table:widget", "WidgetController")
    assert back["found"] and back["direction"] == "b->a"
    none = g.path("FooHandler", "table:part_tag")
    assert none["found"] is False
    assert g.path("Part", "table:widget")["found"] is False
    mixed = g.path("Part", "table:widget", undirected=True)
    assert mixed["found"] and mixed["direction"] == "mixed" and mixed["nodes"] == [
        C + "Part", C + "Widget", "table:widget"]
    assert [h["direction"] for h in mixed["hops"]] == ["reverse", "forward"]


def test_resolution(g: Graph) -> None:
    assert g.resolve("widget") == C + "Widget"  # class and table share the name: the class wins
    assert g.resolve("table:widget") == "table:widget"
    assert g.resolve(f"{J}/Part.java") == C + "Part"
    assert g.resolve("GP_MODE") == "config:widget.mode"
    assert g.resolve("ORU_R01") == "endpoint:message:ORU_R01"
    with pytest.raises(GraphError, match="no node named"):
        g.resolve("Missing")
    data = {"schema": SCHEMA, "nodes": [{"id": "table:x", "type": "table", "name": "x"},
                                        {"id": "config:x", "type": "config", "name": "x"}], "edges": []}
    with pytest.raises(AmbiguousNode) as info:
        Graph(data).resolve("x")
    assert info.value.candidates == ["config:x", "table:x"]


def test_affected_programs(g: Graph) -> None:
    res = g.affected_programs("widget.mode")
    assert res["differs"] is True and res["programs"] == {"a": "fast", "b": "slow", "c": "fast"}
    assert [r["id"] for r in res["readers"]] == [C + "api.impl.WidgetServiceImpl"]
    assert res["readers"][0]["via"]["evidence"]
    assert [r["id"] for r in res["declared_by"]] == [C + "util.Constants"]
    assert [r["id"] for r in res["impacted"]] == [C + "api.WidgetService"]
    assert res["domains"] == ["widget"] and "synthetic" in res["note"]
    deeper = g.affected_programs("GP_MODE", depth=2)
    assert C + "web.WidgetController" in {r["id"] for r in deeper["impacted"]}
    with pytest.raises(GraphError, match="not a configuration key"):
        g.affected_programs("Widget")


def test_graph_file_validation(tmp_path: Path) -> None:
    with pytest.raises(GraphError, match="not a bcp.graph/1"):
        Graph({"schema": "other"})
    with pytest.raises(GraphError, match="unknown node"):
        Graph({"schema": SCHEMA, "nodes": [], "edges": [{"from": "a", "to": "b", "type": "uses"}]})
    with pytest.raises(GraphError, match="cannot read"):
        Graph.load(tmp_path / "none.json")


def test_query_module_is_stdlib_only() -> None:
    import ast

    tree = ast.parse(Path(sys.modules["bcp.graphq"].__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"__future__", "json", "collections", "pathlib", "typing"}


# --------------------------------------------------------------------------- write and upload


class FakeS3:
    def __init__(self, short: bool = False) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.short = short

    def put_object(self, **kw: Any) -> None:
        self.objects[kw["Key"]] = kw

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3 names
        size = len(self.objects[Key]["Body"]) - (1 if self.short else 0)
        return {"ContentLength": size, "ETag": '"e"'}


def test_run_build_writes_copies_and_uploads(demo_repo: Path, tmp_path: Path) -> None:
    fake = FakeS3()
    made: list[str] = []

    def factory(name: str) -> FakeS3:
        made.append(name)
        return fake

    copies = tmp_path / "copies"
    res = run_build(demo_repo, BcpConfig(), ["widget"], copies=[copies], upload="s3://amzn-s3-demo-bucket",
                    client_factory=factory)
    local = demo_repo / ".kiro" / "context" / "graph.json"
    raw = local.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and (copies / "graph.json").read_bytes() == raw
    assert made == ["s3"] and sorted(fake.objects) == [f"graph/graph-{COMMIT}.json", "graph/latest.json"]
    for obj in fake.objects.values():
        assert obj["Bucket"] == "amzn-s3-demo-bucket" and obj["Body"] == raw and obj["ContentType"] == "application/json"
        assert obj["Metadata"]["commit"] == COMMIT
    assert [u["uri"] for u in res["uploaded"]] == [f"s3://amzn-s3-demo-bucket/graph/graph-{COMMIT}.json",
                                                   "s3://amzn-s3-demo-bucket/graph/latest.json"]
    assert res["edges"] == len(json.loads(raw)["edges"]) and res["commit"] == COMMIT
    with pytest.raises(OutputWriteError, match="HEAD reports"):
        upload_graph("{}", COMMIT, "amzn-s3-demo-bucket", client_factory=lambda name: FakeS3(short=True))
    with pytest.raises(BcpInputError, match="object key"):
        upload_graph("{}", "bad/commit", "amzn-s3-demo-bucket", client_factory=lambda name: fake)


def test_upload_uses_boto3_with_metadata_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3()
    calls: list[tuple[str, str]] = []
    module = types.ModuleType("boto3")
    module.client = lambda name, region_name=None: calls.append((name, region_name)) or fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", module)
    monkeypatch.delenv("AWS_EC2_METADATA_DISABLED", raising=False)
    out = upload_graph("{}\n", COMMIT, "amzn-s3-demo-bucket", "kb/graph/", region="us-east-1")
    assert calls == [("s3", "us-east-1")] and [o["key"] for o in out] == [f"kb/graph/graph-{COMMIT}.json",
                                                                          "kb/graph/latest.json"]
    import os

    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"


def test_parse_s3_uri() -> None:
    assert parse_s3_uri("s3://amzn-s3-demo-bucket1") == ("amzn-s3-demo-bucket1", "graph/")
    assert parse_s3_uri("s3://amzn-s3-demo-bucket/kb/graph") == ("amzn-s3-demo-bucket", "kb/graph/")
    with pytest.raises(BcpInputError):
        parse_s3_uri("https://amzn-s3-demo-bucket/graph/")


# --------------------------------------------------------------------------- CLI


def test_cli_build_and_query(demo_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "g" / "graph.json"
    assert main(["graph", "build", "--repo", str(demo_repo), "--domains", "widget", "--out", str(out), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["commit"] == COMMIT and summary["node_counts"]["class"] >= 9 and summary["uploaded"] == []
    assert main(["graph", "query", "--graph", str(out), "dependents", "Widget", "--depth", "2"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["query"] == "dependents" and C + "web.WidgetController" in {r["id"] for r in res["results"]}
    assert main(["graph", "query", "--graph", str(out), "path", "WidgetController", "table:widget"]) == 0
    assert json.loads(capsys.readouterr().out)["length"] == 3
    assert main(["graph", "query", "--graph", str(out), "affected-programs", "widget.mode"]) == 0
    assert json.loads(capsys.readouterr().out)["differs"] is True
    assert main(["graph", "query", "--graph", str(out), "dependencies", "Part", "--edge-types", "joins-table"]) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)["results"]] == ["table:part_tag"]
    assert main(["graph", "query", "--graph", str(out), "node", "ORU_R01"]) == 0
    assert json.loads(capsys.readouterr().out)["out"][0]["type"] == "routes-to"
    assert main(["graph", "query", "--graph", str(out), "dependents", "Widget", "--depth", "4"]) == 2
    assert main(["graph", "query", "--graph", str(out), "dependents", "Nope"]) == 2
    assert main(["graph", "build", "--repo", str(demo_repo), "--domains", "widget", "--hops", "-1"]) == 2
