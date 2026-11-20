"""Tests for bcp.kb_hook (the UserPromptSubmit retrieval hook) and its generator wiring. No AWS calls:
the Bedrock client is a fake, and the default factory is exercised against a fake boto3 module."""

from __future__ import annotations

import io
import json
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from conftest import run_python
from bcp import kb_hook
from bcp.agents import ENTRY, KB_HOOK_NAME
from bcp.cli import main as cli_main
from bcp.config import BcpConfig, from_dict
from bcp.errors import BcpInputError
from bcp.generate import generate
from bcp.validate import validate

COMMIT = "abc123def4567890abc123def4567890abc12345"


def _edge(a: str, b: str, kind: str, ev: str = "") -> dict[str, Any]:
    return {"from": a, "to": b, "type": kind, "evidence": [ev] if ev else []}


def graph_data() -> dict[str, Any]:
    cls = lambda fqn, dom, demo=True: {  # noqa: E731
        "id": f"class:{fqn}", "type": "class", "kind": "class", "fqn": fqn, "name": fqn.rsplit(".", 1)[-1],
        "domains": [dom] if dom else [], "demo": demo, "path": f"api/src/main/java/{fqn.replace('.', '/')}.java"}
    nodes = [
        cls("org.x.Visit", "visit"),
        cls("org.x.validator.VisitValidator", "visit"),
        cls("org.x.api.impl.VisitServiceImpl", "visit"),
        cls("org.x.api.impl.EncounterServiceImpl", "encounter"),
        cls("org.x.web.VisitController", "", False),
        {"id": "table:visit", "type": "table", "name": "visit", "owner_domains": ["visit"]},
        {"id": "domain:visit", "type": "domain", "name": "visit", "demo": True},
        {"id": "domain:encounter", "type": "domain", "name": "encounter", "demo": True},
        {"id": "config:visits.assignmentHandler", "type": "config", "key": "visits.assignmentHandler",
         "name": "visits.assignmentHandler", "constants": ["GP_VISIT_ASSIGNMENT_HANDLER"], "differs": True,
         "programs": {"coastal": "A", "harbor": "B", "metro": "B"},
         "groups": [{"effective": "A", "programs": ["coastal"]}, {"effective": "B", "programs": ["harbor", "metro"]}]},
    ]
    edges = [
        _edge("class:org.x.validator.VisitValidator", "class:org.x.Visit", "imports", "VisitValidator.java#L20"),
        _edge("class:org.x.api.impl.VisitServiceImpl", "class:org.x.validator.VisitValidator", "uses",
              "VisitServiceImpl.java#L88"),
        _edge("class:org.x.web.VisitController", "class:org.x.api.impl.VisitServiceImpl", "uses",
              "VisitController.java#L12"),
        _edge("class:org.x.api.impl.VisitServiceImpl", "class:org.x.Visit", "imports"),
        _edge("class:org.x.api.impl.EncounterServiceImpl", "class:org.x.Visit", "imports"),
        _edge("class:org.x.Visit", "table:visit", "maps-to-table"),
        _edge("class:org.x.api.impl.EncounterServiceImpl", "config:visits.assignmentHandler", "reads-config",
              "EncounterServiceImpl.java#L300"),
        _edge("class:org.x.Visit", "domain:visit", "in-domain"),
        _edge("class:org.x.validator.VisitValidator", "domain:visit", "in-domain"),
        _edge("class:org.x.api.impl.VisitServiceImpl", "domain:visit", "in-domain"),
        _edge("class:org.x.api.impl.EncounterServiceImpl", "domain:encounter", "in-domain"),
    ]
    return {"schema": "bcp.graph/1", "commit": COMMIT, "nodes": nodes, "edges": edges}


GLOSSARY = """<!-- generated-by: brownfield-context-power -->
# Glossary
- **Visit**: entity, table `visit`, owning domain `visit`, shared: yes
- **Encounter**: domain `encounter`: Owns encounters.
- **authorization**: cross-cutting concern: method security.
"""


def make_ws(tmp_path: Path, **settings: Any) -> Path:
    ws = tmp_path / "ws"
    ctx = ws / ".kiro" / "context"
    ctx.mkdir(parents=True)
    (ctx / "graph.json").write_text(json.dumps(graph_data()), encoding="utf-8")
    (ctx / "glossary.md").write_text(GLOSSARY, encoding="utf-8")
    base = {"schema": kb_hook.SETTINGS_SCHEMA, "kb_id": "KBTEST1234", "region": "us-east-1",
            "graph": ".kiro/context/graph.json", "top_k": 5, "timeout_s": 5, "hops": 2}
    base.update(settings)
    (ctx / "kb-hook.json").write_text(json.dumps({k: v for k, v in base.items() if v is not None}),
                                      encoding="utf-8")
    return ws


def result(fact_id: str, status: str = "verified", components: Any = ("VisitValidator",), score: float = 0.74,
           text: str | None = None) -> dict[str, Any]:
    body = text or (f"# BR-9: Visit date-ordering rule\n- Fact: {fact_id}\n- Type: rule\n- Domain: visit\n"
                    f"- Status: {status}\n- Components: VisitValidator\n```\nIF visit.stopDatetime IS BEFORE "
                    "visit.startDatetime\nTHEN reject \"stopDatetime\"\n```\n- Source: `VisitValidator`.")
    return {"content": {"text": body}, "score": score,
            "location": {"s3Location": {"uri": f"s3://amzn-s3-demo-bucket/verified/visit/{fact_id}.md"}},
            "metadata": {"fact_id": fact_id, "status": status,
                         "components": components if isinstance(components, str) else list(components),
                         "evidence": ["api/src/main/java/org/x/validator/VisitValidator.java#L83-L91"],
                         "commit": COMMIT, "domain": "visit", "fact_type": "rule"}}


class FakeClient:
    def __init__(self, results: list[dict[str, Any]], delay: float = 0.0, error: Exception | None = None) -> None:
        self.results, self.delay, self.error = results, delay, error
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return {"retrievalResults": self.results}


def factory_for(client: FakeClient, seen: list[Any] | None = None):  # type: ignore[no-untyped-def]
    def factory(region: str, options: dict[str, Any]) -> FakeClient:
        if seen is not None:
            seen.append((region, options))
        return client
    return factory


def event(ws: Path, prompt: str = "How are visits closed and what does VisitValidator check?") -> str:
    return json.dumps({"session_id": "s-1", "hook_event_name": "UserPromptSubmit", "cwd": str(ws),
                       "prompt": prompt})


def timing(ws: Path) -> list[dict[str, Any]]:
    path = ws / kb_hook.RETRIEVED_REL / kb_hook.TIMING_FILE
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- hook behaviour


def test_facts_graph_neighbourhood_summary_and_files(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    client = FakeClient([result("visit-BR-9"), result("visit-BR-77", status="incorrect"),
                         result("visit-BR-2", status="needs-edit", components="VisitServiceImpl")])
    seen: list[Any] = []
    summary, rec, _ = kb_hook.run_hook(event(ws), factory=factory_for(client, seen))
    call = client.calls[0]
    assert call["knowledgeBaseId"] == "KBTEST1234"
    assert call["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 5
    assert call["retrievalQuery"]["text"].startswith("How are visits closed")
    assert seen[0][0] == "us-east-1" and seen[0][1]["budget_s"] == 5
    assert len(summary.encode("utf-8")) <= kb_hook.SUMMARY_LIMIT
    assert "[verified KB visit-BR-9] [verified-fresh] BR-9: Visit date-ordering rule: IF visit.stopDatetime" in summary
    assert "[verified KB visit-BR-2] [verified-fresh] (needs-edit)" in summary
    assert "visit-BR-77" not in summary  # a status outside the KB contract is dropped
    assert f"[code graph {COMMIT[:12]}] class VisitValidator (visit): 2 dependents" in summary
    assert "VisitServiceImpl" in summary and "depends on 2: Visit, visit" in summary
    assert "the code is the truth" in summary and "latest.md" in summary
    latest = (ws / ".kiro/context/retrieved/latest.md").read_text(encoding="utf-8")
    assert "### 1. [verified KB visit-BR-9] score 0.74, status verified, verified-fresh" in latest
    assert "THEN reject \"stopDatetime\"" in latest
    assert "via uses class:org.x.api.impl.VisitServiceImpl -> class:org.x.validator.VisitValidator" in latest
    assert "[VisitServiceImpl.java#L88]" in latest
    assert "Matched: cited by visit-BR-9; name `VisitValidator` in the prompt" in latest
    assert "mandatory code check" in latest
    rows = timing(ws)
    assert len(rows) == 1
    row = rows[0]
    assert row["fallback"] is False and row["facts"] == 2 and row["facts_skipped_status"] == 1
    assert row["fact_ids"] == ["visit-BR-9", "visit-BR-2"]
    assert row["summary_bytes"] == len(summary.encode("utf-8")) and row["graph_commit"] == COMMIT
    assert "class:org.x.validator.VisitValidator" in row["components"]
    assert {"total_ms", "kb_ms", "graph_ms", "client_ms", "call_ms", "session_id"} <= set(row)
    assert rec == row
    assert not (ws / ".kiro/context/retrieved/latest.md").read_bytes().startswith(b"\xef\xbb\xbf")


def test_prompt_names_glossary_and_config_without_kb(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, kb_id=None)
    prompt = "What happens to visits if I change GP_VISIT_ASSIGNMENT_HANDLER? Also encounters."
    summary, rec, retriever = kb_hook.run_hook(event(ws, prompt))
    assert retriever is None
    assert rec["fallback"] is True and rec["fallback_reason"] == "no kb_id configured"
    assert "graph-only fallback" in summary
    assert "config visits.assignmentHandler" in summary and "programs differ: coastal=A; harbor/metro=B" in summary
    assert "read by EncounterServiceImpl" in summary
    assert "class Visit (visit)" in summary  # glossary term "visits" -> class Visit
    assert "domain encounter: 1 classes; most depended-on: EncounterServiceImpl" in summary
    latest = (ws / ".kiro/context/retrieved/latest.md").read_text(encoding="utf-8")
    assert "glossary term `visit`" in latest and "name `GP_VISIT_ASSIGNMENT_HANDLER` in the prompt" in latest
    assert "Affected programs (differs: True" in latest


def test_hops_limit_dependents(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, kb_id=None, hops=1)
    summary, _, _ = kb_hook.run_hook(event(ws, "VisitValidator"))
    assert "class VisitValidator (visit): 1 dependents" in summary  # VisitController is 2 hops away
    ws2 = make_ws(tmp_path / "b", kb_id=None, hops=3)
    summary2, _, _ = kb_hook.run_hook(event(ws2, "VisitValidator"))
    assert "class VisitValidator (visit): 2 dependents" in summary2


def test_timeout_falls_back_to_graph_within_budget(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, timeout_s=0.3)
    client = FakeClient([result("visit-BR-9")], delay=3.0)
    start = time.perf_counter()
    summary, rec, retriever = kb_hook.run_hook(event(ws), factory=factory_for(client))
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    assert retriever is not None and retriever.is_alive()
    assert rec["fallback"] is True and "timed out (0.3 s)" in rec["fallback_reason"]
    assert "graph-only fallback" in summary and "[code graph" in summary and "[verified KB visit" not in summary
    assert timing(ws)[0]["fallback"] is True


def test_client_error_falls_back(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    client = FakeClient([], error=ConnectionError("Could not connect to the endpoint URL"))
    summary, rec, _ = kb_hook.run_hook(event(ws), factory=factory_for(client))
    assert rec["fallback"] is True and rec["fallback_reason"].startswith("KB retrieve failed: ConnectionError")
    assert "class VisitValidator" in summary


def test_factory_error_falls_back(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)

    def broken(region: str, options: dict[str, Any]) -> Any:
        raise RuntimeError("no credentials")

    _, rec, _ = kb_hook.run_hook(event(ws), factory=broken)
    assert rec["fallback"] is True and "RuntimeError: no credentials" in rec["fallback_reason"]


def test_summary_never_exceeds_limit(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, top_k=10)
    long_text = "# BR-1: " + "very long title " * 40 + "\n" + "statement word " * 400
    client = FakeClient([result(f"visit-BR-{i}", text=long_text, components=["VisitValidator", "Visit",
                                                                              "VisitServiceImpl"])
                         for i in range(10)])
    prompt = "VisitValidator Visit VisitServiceImpl EncounterServiceImpl VisitController visits.assignmentHandler"
    summary, rec, _ = kb_hook.run_hook(event(ws, prompt), factory=factory_for(client))
    assert len(summary.encode("utf-8")) <= kb_hook.SUMMARY_LIMIT
    assert summary.rstrip().endswith("Cite [verified KB <fact-id>] and [code graph <commit>].")
    assert "latest.md" in summary
    assert rec["summary_bytes"] <= kb_hook.SUMMARY_LIMIT
    assert len(rec["components"]) <= kb_hook.MAX_COMPONENTS


def test_fit_summary_drops_lines_and_keeps_footer() -> None:
    body = ["- " + "x" * 390 for _ in range(20)]
    text = kb_hook.fit_summary("## head", body, ["- foot 1", "- foot 2"])
    assert len(text.encode("utf-8")) <= kb_hook.SUMMARY_LIMIT
    assert text.endswith("- foot 2") and "more lines in the full file" in text
    clipped = kb_hook.fit_summary("## head", ["- " + "é" * 600], ["- foot"])
    assert "..." in clipped and len(clipped.encode("utf-8")) <= kb_hook.SUMMARY_LIMIT


def test_bad_input_and_missing_graph_never_raise(tmp_path: Path) -> None:
    ws = tmp_path / "empty"
    ws.mkdir()
    summary, rec, _ = kb_hook.run_hook("not json", {"cwd": str(ws)})
    assert "Retrieved context" in summary and rec["fallback"] is True
    summary2, rec2, _ = kb_hook.run_hook(json.dumps({"cwd": str(ws), "prompt": "VisitValidator"}),
                                         {"kb_id": "K1"}, factory=factory_for(FakeClient([result("visit-BR-9")])))
    assert rec2["graph_error"] and "code graph unavailable" in summary2 and "[verified KB visit-BR-9]" in summary2
    assert (ws / ".kiro/context/retrieved/timing.jsonl").is_file()


def test_internal_error_still_returns_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = make_ws(tmp_path, kb_id=None)

    def boom(*a: Any, **k: Any) -> str:
        raise ValueError("render broke")

    monkeypatch.setattr(kb_hook, "render_full", boom)
    summary, rec, _ = kb_hook.run_hook(event(ws))
    assert "unavailable (ValueError)" in summary
    assert rec["fallback"] is True and "render broke" in rec["fallback_reason"]


def test_settings_found_from_subfolder_and_overrides(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, kb_id=None)
    sub = ws / "api" / "src"
    sub.mkdir(parents=True)
    _, rec, _ = kb_hook.run_hook(json.dumps({"cwd": str(sub), "prompt": "VisitValidator"}))
    assert rec["graph_commit"] == COMMIT
    assert (ws / ".kiro/context/retrieved/latest.md").is_file()
    client = FakeClient([result("visit-BR-9")])
    _, rec2, _ = kb_hook.run_hook(event(ws), {"kb_id": "OTHERKB", "top_k": 2}, factory=factory_for(client))
    assert client.calls[0]["knowledgeBaseId"] == "OTHERKB"
    assert client.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 2


def test_default_factory_uses_mocked_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Any] = []

    class Config:
        def __init__(self, **kw: Any) -> None:
            self.kw = kw

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service, **kw: made.append((service, kw)) or "client"  # type: ignore[attr-defined]
    fake_config = types.ModuleType("botocore.config")
    fake_config.Config = Config  # type: ignore[attr-defined]
    fake_botocore = types.ModuleType("botocore")
    fake_botocore.__path__ = []  # type: ignore[attr-defined]
    fake_botocore.config = fake_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", fake_config)
    monkeypatch.delenv("AWS_EC2_METADATA_DISABLED", raising=False)
    assert kb_hook.default_client_factory("us-east-1", {"budget_s": 12, "endpoint_url": "https://127.0.0.1:9"}) \
        == "client"
    service, kw = made[0]
    assert service == "bedrock-agent-runtime" and kw["region_name"] == "us-east-1"
    assert kw["endpoint_url"] == "https://127.0.0.1:9"
    assert kw["config"].kw["read_timeout"] == 12 and kw["config"].kw["connect_timeout"] == 5
    assert kw["config"].kw["retries"]["total_max_attempts"] == 2
    import os

    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"


def test_signed_retrieve_request_builds_sigv4() -> None:
    """The warm default signs a Retrieve request with botocore only, no network and no boto3 client."""
    from botocore.credentials import Credentials

    creds = Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    req = kb_hook.signed_retrieve_request("us-west-2", "KB123ABC", "how are visits closed?", 5, creds)
    assert req.url == "https://bedrock-agent-runtime.us-west-2.amazonaws.com/knowledgebases/KB123ABC/retrieve"
    assert req.method == "POST"
    auth = req.headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 ") and "/us-west-2/bedrock/aws4_request" in auth
    assert "X-Amz-Date" in req.headers
    body = json.loads(req.data.decode("utf-8"))
    assert body["retrievalQuery"]["text"] == "how are visits closed?"
    assert body["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 5


def test_default_factory_falls_back_to_boto3_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the signed client cannot resolve credentials, the default factory degrades to boto3."""
    import botocore.session

    monkeypatch.setattr(botocore.session.Session, "get_credentials", lambda self: None)
    made: list[Any] = []
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service, **kw: made.append((service, kw)) or "boto3-client"  # type: ignore[attr-defined]
    fake_config = types.ModuleType("botocore.config")

    class Config:
        def __init__(self, **kw: Any) -> None:
            self.kw = kw

    fake_config.Config = Config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore.config", fake_config)
    client = kb_hook.default_client_factory("us-east-1", {"budget_s": 12})
    assert client == "boto3-client" and made[0][0] == "bedrock-agent-runtime"


def test_script_mode_subprocess_exit_0(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, kb_id=None)
    script = Path(kb_hook.__file__).resolve()
    proc = run_python([str(script)], input=event(ws).encode("utf-8"), capture_output=True, timeout=60,
                      cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.decode("utf-8")
    assert len(proc.stdout) <= kb_hook.SUMMARY_LIMIT + 2
    assert "## Retrieved context" in out and "class VisitValidator" in out
    assert (ws / ".kiro/context/retrieved/latest.md").is_file() and timing(ws)


def test_script_mode_garbage_stdin_exit_0(tmp_path: Path) -> None:
    script = Path(kb_hook.__file__).resolve()
    proc = run_python([str(script), "--top-k", "notanint"], input=b"\xff\xfe garbage", capture_output=True,
                      timeout=60, cwd=str(tmp_path))
    assert proc.returncode == 0 and b"Retrieved context" in proc.stdout


def test_cli_kb_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = make_ws(tmp_path, kb_id=None)

    class Stdin(io.StringIO):
        buffer = io.BytesIO(event(ws, "VisitValidator").encode("utf-8"))

        def isatty(self) -> bool:
            return False

    printed: list[str] = []
    monkeypatch.setattr(sys, "stdin", Stdin())
    monkeypatch.setattr(kb_hook, "emit", printed.append)
    assert cli_main(["kb", "hook", "--hops", "1"]) == 0
    assert "class VisitValidator (visit): 1 dependents" in printed[0]
    monkeypatch.setattr(kb_hook, "run_hook", lambda *a, **k: 1 / 0)
    assert cli_main(["kb", "hook"]) == 0 and "unavailable (ZeroDivisionError)" in printed[1]


def test_parse_results_metadata_shapes() -> None:
    raw = [result("a", components="X, Y"), result("b", components=[]), {"metadata": {"status": "needs-sme"}},
           result("c", components=['"EncounterServiceImpl"', '"NoVisitAssignmentHandler"'])]
    raw[1]["metadata"]["components"] = '["P", "Q"]'
    raw[3]["metadata"]["evidence"] = ['"api/A.java#L1-L2"']
    facts, skipped = kb_hook.parse_results(raw)
    assert [f["components"] for f in facts] == [["X", "Y"], ["P", "Q"],
                                                ["EncounterServiceImpl", "NoVisitAssignmentHandler"]]
    assert facts[2]["evidence"] == ["api/A.java#L1-L2"] and skipped == 1


def test_fact_brief_skips_header_and_prefers_correction() -> None:
    text = ("# BR-1: Visit save privilege\n- Fact: visit-BR-1\n- Status: needs-edit\n- Review: AI\n"
            "- What changed from the extraction: saveVisit carries @PreAuthorize ADD_VISITS.\n"
            "- Components: VisitService\n- Source hash (SHA-256 of the cited code lines): abc\n```\nIF x\n```\n")
    assert kb_hook.fact_brief(text) == ("BR-1: Visit save privilege",
                                        "corrected: saveVisit carries @PreAuthorize ADD_VISITS.")
    title, statement = kb_hook.fact_brief(text.replace("- What changed from the extraction: saveVisit carries "
                                                       "@PreAuthorize ADD_VISITS.\n", ""))
    assert statement == "IF x" and "abc" not in statement


# --------------------------------------------------------------------------- config and generator


KB = {"kb_id": "KBEXAMPLE1", "region": "us-east-1", "graph": ".kiro/context/graph.json"}


def test_config_kb_section_defaults_and_errors() -> None:
    cfg = from_dict({"kb": dict(KB)})
    assert cfg.kb == {**KB, "top_k": 5, "hops": 2, "timeout_s": 12.0}
    assert from_dict({}).kb is None
    for bad in ({"kb": "x"}, {"kb": {**KB, "kb_id": "bad id"}}, {"kb": {**KB, "region": "useast"}},
                {"kb": {k: v for k, v in KB.items() if k != "graph"}}, {"kb": {**KB, "hops": 4}},
                {"kb": {**KB, "timeout_s": 60}}, {"kb": {**KB, "top_k": 0}}, {"kb": {**KB, "python": ""}}):
        with pytest.raises(BcpInputError):
            from_dict(bad)


def agents_of(ws: Path) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (ws / ".kiro/agents").glob("*.json")}


def test_generate_with_kb_adds_hook_prompt_and_files(workspace: Path) -> None:
    (workspace / ".kiro/context").mkdir(parents=True, exist_ok=True)
    (workspace / ".kiro/context/graph.json").write_text(json.dumps(graph_data()), encoding="utf-8")
    cfg = from_dict({"kb": {**KB, "python": r"C:\Py\python.exe"}})
    generate(workspace / "business-rules-extraction", workspace, None, cfg)
    found = agents_of(workspace)
    entry = found[ENTRY]
    hooks = entry["hooks"]
    script = str(Path(kb_hook.__file__).resolve())
    assert hooks == [{"name": KB_HOOK_NAME, "trigger": "UserPromptSubmit",
                      "action": {"type": "command", "command": f'"C:\\Py\\python.exe" "{script}"'}, "timeout": 30}]
    prompt = entry["prompt"]
    lines = prompt.split("\n")
    assert lines[1].startswith("0. Retrieved context first.")
    for phrase in (".kiro/context/retrieved/latest.md", "verified facts are a map", "the code is the truth",
                   "mandatory code check (authorization, transactions and error end states, concurrency, absolute "
                   "claims) still applies", "[verified KB <fact-id>]", "[code graph <commit>]",
                   "graph-only fallback"):
        assert phrase in prompt, phrase
    assert "1. Scope the question first" in prompt  # the rest of the hybrid prompt is unchanged
    for name, data in found.items():
        if name != ENTRY:
            assert "hooks" not in data and "Retrieved context first" not in data["prompt"]
    ide = json.loads((workspace / ".kiro/hooks/kb-retrieve.json").read_text(encoding="utf-8"))
    # the documented v1 standalone hook schema (https://kiro.dev/docs/hooks/)
    assert set(ide) == {"version", "hooks"} and ide["version"] == "v1" and len(ide["hooks"]) == 1
    ide_hook = ide["hooks"][0]
    assert ide_hook["name"] == KB_HOOK_NAME and "generated_by" not in ide_hook
    assert (ide_hook["trigger"] == "UserPromptSubmit" and ide_hook["action"] == hooks[0]["action"]
            and ide_hook["timeout"] == 30)
    settings = json.loads((workspace / kb_hook.SETTINGS_REL).read_text(encoding="utf-8"))
    assert settings == {"schema": kb_hook.SETTINGS_SCHEMA, "kb_id": "KBEXAMPLE1", "region": "us-east-1",
                        "graph": ".kiro/context/graph.json", "glossary": kb_hook.GLOSSARY_REL,
                        "retrieved_dir": kb_hook.RETRIEVED_REL, "top_k": 5, "timeout_s": 12.0, "hops": 2}
    for rel in (".kiro/hooks/kb-retrieve.json", kb_hook.SETTINGS_REL, ".kiro/agents/brownfield.json"):
        assert not (workspace / rel).read_bytes().startswith(b"\xef\xbb\xbf")
    record = json.loads((workspace / ".kiro/context/generation-record.json").read_text(encoding="utf-8"))
    assert ".kiro/hooks/kb-retrieve.json" in record["files"] and kb_hook.SETTINGS_REL in record["files"]
    report = validate(workspace)
    assert report.count("ERROR") == 0, [f.line() for f in report.findings if f.level == "ERROR"]
    # the generated hook runs against this workspace (no kb call: the fake settings carry an id, so override)
    summary, rec, _ = kb_hook.run_hook(json.dumps({"cwd": str(workspace), "prompt": "VisitValidator"}),
                                       factory=factory_for(FakeClient([result("visit-BR-9")])))
    assert rec["fallback"] is False and "[verified KB visit-BR-9]" in summary


def test_generate_without_kb_has_no_hook(workspace: Path) -> None:
    generate(workspace / "business-rules-extraction", workspace, None, BcpConfig())
    entry = agents_of(workspace)[ENTRY]
    assert "hooks" not in entry and "Retrieved context first" not in entry["prompt"]
    assert not (workspace / ".kiro/hooks").exists() and not (workspace / kb_hook.SETTINGS_REL).exists()


def test_generate_kb_then_without_removes_kb_files(workspace: Path, caplog: pytest.LogCaptureFixture) -> None:
    generate(workspace / "business-rules-extraction", workspace, None, from_dict({"kb": dict(KB)}))
    assert "kb.graph .kiro/context/graph.json not found" in caplog.text
    assert (workspace / ".kiro/hooks/kb-retrieve.json").is_file()
    generate(workspace / "business-rules-extraction", workspace, None, BcpConfig())
    assert not (workspace / ".kiro/hooks/kb-retrieve.json").exists()
    assert not (workspace / kb_hook.SETTINGS_REL).exists()


@pytest.mark.parametrize("design", ["delegate", "narrow-capped", "hook-only", "kb-first"])
def test_kb_prompt_step_in_other_designs(workspace: Path, design: str) -> None:
    generate(workspace / "business-rules-extraction", workspace, None,
             from_dict({"kb": dict(KB), "agent_design": design}))
    entry = agents_of(workspace)[ENTRY]
    assert entry["prompt"].split("\n")[1].startswith("0. Retrieved context first.") and entry["hooks"]
    if design in ("hook-only", "kb-first"):
        assert entry["tools"] == ["read"]  # no sub-agent hop


def test_hook_python_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert kb_hook.hook_python(r"D:\x\python.exe") == r"D:\x\python.exe"
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert kb_hook.hook_python() == sys.executable
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    fallback = tmp_path / "python.exe"
    fallback.write_text("")
    monkeypatch.setattr(kb_hook, "FALLBACK_PYTHON", str(fallback))
    assert kb_hook.hook_python() == str(fallback)
    monkeypatch.setattr(kb_hook, "FALLBACK_PYTHON", str(tmp_path / "missing.exe"))
    assert kb_hook.hook_python() == sys.executable


# --------------------------------------------------------------------------- per-fact status tags (A4)


def test_fact_freshness_tag() -> None:
    fresh = {"commit": COMMIT}
    assert kb_hook.fact_freshness(fresh, COMMIT) == "verified-fresh"
    assert kb_hook.fact_freshness({"commit": "other1234"}, COMMIT) == "verified-stale"
    assert kb_hook.fact_freshness({"commit": None}, COMMIT) == "verified-fresh"  # unknown commit: fresh
    assert kb_hook.fact_freshness({"commit": "other"}, None) == "verified-fresh"  # no graph commit to compare


def test_summary_tags_stale_fact(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    stale = result("visit-BR-9")
    stale["metadata"]["commit"] = "0000stalecommit0000"  # differs from the graph commit
    summary, _rec, _ = kb_hook.run_hook(event(ws), factory=factory_for(FakeClient([stale]), []))
    assert len(summary.encode("utf-8")) <= kb_hook.SUMMARY_LIMIT
    assert "[verified KB visit-BR-9] [verified-stale]" in summary
    assert "(1 stale)" in summary


def test_summary_labels_no_facts_unverified(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    summary, _rec, _ = kb_hook.run_hook(event(ws), factory=factory_for(FakeClient([]), []))
    assert "unverified: no verified fact covered this prompt" in summary


def test_summary_labels_fallback_unverified(tmp_path: Path) -> None:
    # no kb_id configured -> graph-only fallback -> the facts section is reported unverified
    ws = make_ws(tmp_path, kb_id="")
    summary, rec, _ = kb_hook.run_hook(json.dumps({"cwd": str(ws), "prompt": "VisitValidator"}))
    assert rec["fallback"] is True
    assert "unverified (graph-only fallback)" in summary


@pytest.mark.parametrize("url", ["http://bedrock.example/retrieve", "file:///etc/hosts", "ftp://x/y", "https:///nohost"])
def test_https_post_refuses_non_https(url: str) -> None:
    """The signed Retrieve transport opens HTTPS URLs only (no file: or custom schemes)."""
    with pytest.raises(ValueError):
        kb_hook.https_post(url, b"{}", {}, 1.0)


def test_https_post_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    calls: list[Any] = []

    class Resp:
        status = 403

        def read(self) -> bytes:
            return b'{"message":"denied"}'

    class Conn:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            calls.append((host, port, timeout))

        def request(self, method: str, path: str, body: Any = None, headers: Any = None) -> None:
            calls.append((method, path))

        def getresponse(self) -> Resp:
            return Resp()

        def close(self) -> None:
            calls.append("closed")

    monkeypatch.setattr(http.client, "HTTPSConnection", Conn)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        kb_hook.https_post("https://bedrock-agent-runtime.us-east-1.amazonaws.com/knowledgebases/K/retrieve?x=1",
                           b"{}", {"a": "b"}, 7.0)
    assert calls[0] == ("bedrock-agent-runtime.us-east-1.amazonaws.com", 443, 7.0)
    assert calls[1] == ("POST", "/knowledgebases/K/retrieve?x=1") and calls[-1] == "closed"
