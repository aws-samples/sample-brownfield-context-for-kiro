"""Tests for bcp.kb_sync (kb sync / audit / check-retrieval) with fake AWS clients; no AWS calls."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from bcp import kb_sync
from bcp.cli import main
from bcp.kb_sync import KbSyncError, audit, check_retrieval, query_for, run_ingestion, sync

BUCKET = "amzn-s3-demo-bucket"


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class FakeS3:
    """In-memory bucket: ETag = MD5 of the body, list pages of ``page`` keys."""

    def __init__(self, page: int = 1000) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.page = page
        self.puts: list[str] = []
        self.deletes: list[str] = []

    def seed(self, key: str, body: bytes) -> None:
        self.objects[key] = {"Body": body, "Metadata": {}}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str, Metadata: dict[str, str]) -> dict:
        assert Bucket == BUCKET
        self.objects[Key] = {"Body": Body, "Metadata": Metadata, "ContentType": ContentType}
        self.puts.append(Key)
        return {}

    def list_objects_v2(self, Bucket: str, Prefix: str, ContinuationToken: str | None = None) -> dict:
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        chunk = keys[start:start + self.page]
        more = start + self.page < len(keys)
        def etag(body: bytes) -> str:
            return '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"'

        out: dict[str, Any] = {"Contents": [{"Key": k, "Size": len(self.objects[k]["Body"]),
                                             "ETag": etag(self.objects[k]["Body"]),
                                             "LastModified": "2026-01-01T00:00:00Z"} for k in chunk],
                               "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = str(start + self.page)
        return out

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
            self.deletes.append(obj["Key"])
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:
        return {"Body": io.BytesIO(self.objects[Key]["Body"])}


class FakeAgent:
    def __init__(self, polls: int = 2, conflicts: int = 0, final: str = "COMPLETE") -> None:
        self.polls, self.conflicts, self.final = polls, conflicts, final
        self.started = 0
        self.calls = 0

    def start_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str) -> dict:
        if self.conflicts:
            self.conflicts -= 1
            raise ClientError("ConflictException")
        self.started += 1
        return {"ingestionJob": {"ingestionJobId": "JOB1", "status": "STARTING"}}

    def get_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str, ingestionJobId: str) -> dict:
        self.calls += 1
        if self.calls < self.polls:
            return {"ingestionJob": {"ingestionJobId": ingestionJobId, "status": "IN_PROGRESS"}}
        return {"ingestionJob": {"ingestionJobId": ingestionJobId, "status": self.final,
                                 "statistics": {"numberOfDocumentsScanned": 3, "numberOfDocumentsDeleted": 1,
                                                "numberOfDocumentsFailed": 0},
                                 "failureReasons": [] if self.final == "COMPLETE" else ["boom"]}}


class FakeRuntime:
    """Returns the fact whose title words overlap the query most, then two others."""

    def __init__(self, facts_dir: Path, fail_first: int = 0) -> None:
        self.docs = {}
        for doc in (facts_dir / "verified").rglob("*.md"):
            self.docs[doc.stem] = doc.read_text(encoding="utf-8").splitlines()[0].lower()
        self.fail_first = fail_first

    def retrieve(self, knowledgeBaseId: str, retrievalQuery: dict, retrievalConfiguration: dict) -> dict:
        if self.fail_first:
            self.fail_first -= 1
            raise ClientError("ThrottlingException")
        words = set(retrievalQuery["text"].lower().split())
        ranked = sorted(self.docs, key=lambda d: (-len(words & set(self.docs[d].split())), d))
        n = retrievalConfiguration["vectorSearchConfiguration"]["numberOfResults"]
        return {"retrievalResults": [{"metadata": {"fact_id": d}, "score": 0.9 - i / 10,
                                      "location": {"s3Location": {"uri": f"s3://{BUCKET}/verified/x/{d}.md"}}}
                                     for i, d in enumerate(ranked[:n])]}


def write_fact(root: Path, domain: str, fact_id: str, title: str, status: str = "verified",
               fact_type: str = "rule") -> None:
    folder = root / "verified" / domain
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{fact_id}.md").write_text(f"# {title}\n\n- Fact: {fact_id}\n- Status: {status}\n", encoding="utf-8")
    meta = {"metadataAttributes": {"fact_id": fact_id, "domain": domain, "status": status, "fact_type": fact_type}}
    (folder / f"{fact_id}.md.metadata.json").write_text(json.dumps(meta), encoding="utf-8")


@pytest.fixture
def facts(tmp_path: Path) -> Path:
    root = tmp_path / "facts"
    write_fact(root, "visit", "visit-BR-1", "BR-1: Visit end must follow visit start")
    write_fact(root, "order", "order-BR-2", "BR-2: Order numbers come from a seeded generator", "needs-edit")
    write_fact(root, "order", "order-FEATURE-MAP", "Feature-to-component map: order", "derived", "feature-map")
    audit_doc = {"rules": [{"fact_id": "visit-BR-1", "exported": True, "verdict": "verified"},
                           {"fact_id": "order-BR-2", "exported": True, "verdict": "needs-edit"},
                           {"fact_id": None, "exported": False, "verdict": "incorrect"}],
                 "derived": [{"fact_id": "order-FEATURE-MAP", "from": ["order-BR-2"]}]}
    (root / "export-audit.json").write_text(json.dumps(audit_doc), encoding="utf-8")
    return root


def factory_for(**clients: Any):
    names = {"s3": "s3", "agent": "bedrock-agent", "runtime": "bedrock-agent-runtime", "s3v": "s3vectors"}
    table = {names[k]: v for k, v in clients.items()}
    return lambda name: table[name]


def no_sleep(_: float) -> None:
    return None


# --------------------------------------------------------------------------------------- sync


def test_sync_uploads_deletes_smoke_and_ingests(facts: Path, tmp_path: Path) -> None:
    s3, agent = FakeS3(page=2), FakeAgent()
    for key in ("verified/test/TEST-VISIT-001.md", "verified/test/TEST-VISIT-001.md.metadata.json",
                "verified/visit/old-BR-9.md", "verified/visit/old-BR-9.md.metadata.json"):
        s3.seed(key, b"old")
    s3.seed("graph/latest.json", b"{}")  # outside the prefix: never touched
    report = tmp_path / "sync-report.json"
    res = sync(facts, BUCKET, "KB", "DS", client_factory=factory_for(s3=s3, agent=agent), report=report,
               sleep=no_sleep)
    assert res["counts"] == {"uploaded": 6, "deleted": 4, "unchanged": 0}
    assert sorted(res["deleted"]) == sorted(s3.deletes)
    assert "verified/test/TEST-VISIT-001.md.metadata.json" in res["deleted"]
    assert "graph/latest.json" in s3.objects
    assert sorted(k for k in s3.objects if k.startswith("verified/")) == sorted(
        f"verified/{d}/{f}" for d, f in (("order", "order-BR-2.md"), ("order", "order-BR-2.md.metadata.json"),
                                         ("order", "order-FEATURE-MAP.md"),
                                         ("order", "order-FEATURE-MAP.md.metadata.json"),
                                         ("visit", "visit-BR-1.md"), ("visit", "visit-BR-1.md.metadata.json")))
    put = s3.objects["verified/visit/visit-BR-1.md"]
    assert put["Metadata"]["sha256"] == hashlib.sha256(put["Body"]).hexdigest()
    assert put["ContentType"].startswith("text/markdown")
    assert s3.objects["verified/visit/visit-BR-1.md.metadata.json"]["ContentType"] == "application/json"
    assert res["ingestion"]["status"] == "COMPLETE" and res["ingestion"]["statistics"]["numberOfDocumentsFailed"] == 0
    assert res["local"]["by_status"] == {"derived": 1, "needs-edit": 1, "verified": 1}
    on_disk = json.loads(report.read_text(encoding="utf-8"))
    assert on_disk["counts"]["deleted"] == 4 and not report.read_bytes().startswith(b"\xef\xbb\xbf")


def test_sync_is_idempotent_and_uploads_only_changes(facts: Path) -> None:
    s3 = FakeS3()
    sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    s3.puts.clear()
    again = sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    assert again["counts"] == {"uploaded": 0, "deleted": 0, "unchanged": 6} and s3.puts == []
    doc = facts / "verified" / "visit" / "visit-BR-1.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    third = sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    assert [u["key"] for u in third["uploaded"]] == ["verified/visit/visit-BR-1.md"]
    assert third["uploaded"][0]["reason"] == "changed"


def test_sync_removed_fact_deletes_document_and_metadata(facts: Path) -> None:
    s3 = FakeS3()
    sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    for p in (facts / "verified" / "order").glob("order-BR-2.md*"):
        p.unlink()
    res = sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    assert res["deleted"] == ["verified/order/order-BR-2.md", "verified/order/order-BR-2.md.metadata.json"]


def test_sync_multipart_etag_is_reuploaded(facts: Path) -> None:
    s3 = FakeS3()
    sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    original = s3.list_objects_v2

    def multipart(**kw: Any) -> dict:
        page = original(**kw)
        for obj in page["Contents"]:
            obj["ETag"] = '"abc-2"'
        return page

    s3.list_objects_v2 = multipart  # type: ignore[method-assign]
    assert sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)["counts"]["uploaded"] == 6


def test_sync_dry_run_changes_nothing(facts: Path) -> None:
    s3 = FakeS3()
    s3.seed("verified/test/x.md", b"x")
    res = sync(facts, BUCKET, "KB", "DS", client_factory=factory_for(s3=s3), dry_run=True)
    assert res["counts"]["uploaded"] == 6 and res["deleted"] == ["verified/test/x.md"]
    assert s3.puts == [] and s3.deletes == [] and res["ingestion"] is None


@pytest.mark.parametrize("status", ["needs-sme", "incorrect", "unreviewed", None])
def test_sync_refuses_unreviewed_status(facts: Path, status: str | None) -> None:
    meta = facts / "verified" / "visit" / "visit-BR-1.md.metadata.json"
    meta.write_text(json.dumps({"metadataAttributes": {"fact_id": "visit-BR-1", "status": status}}), encoding="utf-8")
    s3 = FakeS3()
    with pytest.raises(KbSyncError, match="refusing"):
        sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    assert s3.puts == []


def test_sync_refuses_missing_pair(facts: Path) -> None:
    (facts / "verified" / "visit" / "visit-BR-1.md.metadata.json").unlink()
    with pytest.raises(KbSyncError, match="no visit-BR-1.md.metadata.json"):
        sync(facts, BUCKET, None, None, client_factory=factory_for(s3=FakeS3()), ingest=False)
    (facts / "verified" / "order" / "order-BR-2.md").unlink()
    with pytest.raises(KbSyncError, match="metadata without a document"):
        sync(facts, BUCKET, None, None, client_factory=factory_for(s3=FakeS3()), ingest=False)


@pytest.mark.parametrize("name", ["notes.txt", "spec.pdf", "page.html", "table.csv", "doc.docx",
                                  "orphan.metadata.json"])
def test_sync_refuses_stray_files_before_any_upload(facts: Path, name: str) -> None:
    (facts / "verified" / "visit" / name).write_bytes(b"not reviewed")
    s3 = FakeS3()
    with pytest.raises(KbSyncError, match=f"not fact documents.*\n.*visit/{re.escape(name)}"):
        sync(facts, BUCKET, "KB", "DS", client_factory=factory_for(s3=s3, agent=FakeAgent()), sleep=no_sleep)
    assert s3.puts == [] and s3.deletes == []


def test_sync_ignores_atomic_write_temp_files(facts: Path) -> None:
    (facts / "verified" / "visit" / ".visit-BR-1.md.tmp-123").write_bytes(b"partial")
    s3 = FakeS3()
    res = sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    assert res["counts"]["uploaded"] == 6 and not any(".tmp-" in k for k in s3.puts)


def test_sync_refuses_fact_not_exported_by_review_filter(facts: Path) -> None:
    write_fact(facts, "visit", "visit-BR-77", "BR-77: hand-placed and never reviewed")  # status verified
    s3 = FakeS3()
    with pytest.raises(KbSyncError, match="visit-BR-77.md: rule fact not exported by the review filter"):
        sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    assert s3.puts == []


@pytest.mark.parametrize("change", ["verdict", "derived", "alias", "duplicate"])
def test_sync_refuses_export_mismatch(facts: Path, change: str) -> None:
    audit_path = facts / "export-audit.json"
    doc = json.loads(audit_path.read_text(encoding="utf-8"))
    if change == "verdict":
        doc["rules"][0]["verdict"] = "needs-edit"
        audit_path.write_text(json.dumps(doc), encoding="utf-8")
        needle = "differs from metadata status"
    elif change == "derived":
        doc["derived"] = []
        audit_path.write_text(json.dumps(doc), encoding="utf-8")
        needle = "derived fact not listed"
    elif change == "alias":
        folder = facts / "verified" / "visit"
        (folder / "copy.md").write_bytes((folder / "visit-BR-1.md").read_bytes())
        (folder / "copy.md.metadata.json").write_bytes((folder / "visit-BR-1.md.metadata.json").read_bytes())
        needle = "document name does not match"
    else:
        write_fact(facts, "order", "visit-BR-1", "BR-1: same id in another folder")
        needle = "also in"
    s3 = FakeS3()
    with pytest.raises(KbSyncError, match=needle):
        sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    assert s3.puts == []


def test_sync_refuses_without_export_audit(facts: Path) -> None:
    (facts / "export-audit.json").unlink()
    s3 = FakeS3()
    with pytest.raises(KbSyncError, match="export-audit.json not found"):
        sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    (facts / "export-audit.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(KbSyncError, match="unreadable"):
        sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    assert s3.puts == []


def test_sync_input_errors(facts: Path, tmp_path: Path) -> None:
    from bcp.errors import BcpInputError

    with pytest.raises(BcpInputError, match="prefix"):
        sync(facts, BUCKET, None, None, prefix="", client_factory=factory_for(s3=FakeS3()), ingest=False)
    with pytest.raises(BcpInputError, match="prefix"):
        sync(facts, BUCKET, None, None, prefix="verified", client_factory=factory_for(s3=FakeS3()), ingest=False)
    with pytest.raises(BcpInputError, match="kb-id"):
        sync(facts, BUCKET, None, None, client_factory=factory_for(s3=FakeS3()))
    with pytest.raises(BcpInputError, match="kb export"):
        sync(tmp_path / "nowhere", BUCKET, None, None, client_factory=factory_for(s3=FakeS3()), ingest=False)


def test_sync_failed_ingestion_raises_after_report(facts: Path, tmp_path: Path) -> None:
    report = tmp_path / "r.json"
    with pytest.raises(KbSyncError, match="FAILED"):
        sync(facts, BUCKET, "KB", "DS", client_factory=factory_for(s3=FakeS3(), agent=FakeAgent(final="FAILED")),
             report=report, sleep=no_sleep)
    assert json.loads(report.read_text(encoding="utf-8"))["ingestion"]["status"] == "FAILED"


def test_ingestion_retries_conflict_and_times_out() -> None:
    agent = FakeAgent(conflicts=2)
    res = run_ingestion(agent, "KB", "DS", sleep=no_sleep)
    assert agent.started == 1 and res["status"] == "COMPLETE" and res["job_id"] == "JOB1"
    ticks = iter(range(0, 10_000, 100))
    with pytest.raises(KbSyncError, match="still IN_PROGRESS"):
        run_ingestion(FakeAgent(polls=10**6), "KB", "DS", timeout_s=250, sleep=no_sleep, clock=lambda: next(ticks))
    with pytest.raises(ClientError):
        run_ingestion(FakeAgent(conflicts=99), "KB", "DS", sleep=no_sleep)


# --------------------------------------------------------------------------------------- audit


def test_audit_after_sync_all_allowed(facts: Path, tmp_path: Path) -> None:
    s3 = FakeS3()
    sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)

    class FakeS3V:
        def list_vectors(self, **kw: Any) -> dict:
            if kw.get("nextToken"):
                return {"vectors": [{"metadata": {"x-amz-bedrock-kb-source-uri": f"s3://{BUCKET}/verified/visit/visit-BR-1.md"}}]}
            return {"vectors": [{"metadata": {"x-amz-bedrock-kb-source-uri": f"s3://{BUCKET}/verified/order/order-BR-2.md"}}],
                    "nextToken": "t"}

    out = tmp_path / "kb-audit.json"
    res = audit(facts, BUCKET, client_factory=factory_for(s3=s3, s3v=FakeS3V()), out=out,
                vector_bucket="amzn-s3-demo-vector-bucket", index="idx")
    s = res["summary"]
    assert s["all_allowed"] and s["violations"] == [] and s["documents"] == 3 and s["metadata_objects"] == 3
    assert s["by_status"] == {"derived": 1, "needs-edit": 1, "verified": 1} and s["smoke_test_objects"] == []
    docs = {o["fact_id"]: o for o in res["objects"] if o["kind"] == "document"}
    assert "approved replacement" in docs["order-BR-2"]["check"]
    assert "derived from 1" in docs["order-FEATURE-MAP"]["check"]
    assert res["vector_index"]["vectors"] == 2 and res["vector_index"]["smoke_test_sources"] == []
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["objects"] == 6


def test_audit_flags_smoke_unexported_and_unknown(facts: Path) -> None:
    s3 = FakeS3()
    sync(facts, BUCKET, None, None, client_factory=factory_for(s3=s3), ingest=False)
    s3.seed("verified/test/TEST-1.md", b"# t\n")
    s3.seed("verified/test/TEST-1.md.metadata.json",
            json.dumps({"metadataAttributes": {"fact_id": "TEST-1", "status": "verified"}}).encode())
    s3.seed("verified/visit/notes.txt", b"x")
    s3.seed("verified/visit/lonely.md.metadata.json", b"{}")
    s3.seed("verified/visit/nometa.md", b"x")
    res = audit(facts, BUCKET, client_factory=factory_for(s3=s3))
    s = res["summary"]
    assert not s["all_allowed"]
    assert s["smoke_test_objects"] == ["verified/test/TEST-1.md", "verified/test/TEST-1.md.metadata.json"]
    text = "\n".join(s["violations"])
    for needle in ("TEST-1.md: rule fact not exported", "notes.txt: not a fact document",
                   "lonely.md.metadata.json: metadata without its document", "nometa.md: ",
                   "not identical to the current export"):
        assert needle in text


def test_audit_review_check_mismatch_and_no_export(facts: Path) -> None:
    from bcp.kb_sync import _review_check

    export = ({"r": {"exported": True, "verdict": "verified"}, "x": {"exported": False, "verdict": "incorrect"}},
              {"d": ["x"]})
    assert _review_check({"fact_id": "r", "status": "needs-edit"}, export)[0] is False
    assert _review_check({"fact_id": "d", "status": "derived"}, export)[0] is False
    assert _review_check({"fact_id": "zz", "status": "derived"}, export)[0] is False
    assert _review_check({"fact_id": "r", "status": "verified"}, None)[0] is True
    assert _review_check({"fact_id": "n", "status": "needs-edit"}, None)[0] is False


# --------------------------------------------------------------------------------------- retrieval


def test_query_for_strips_rule_id() -> None:
    assert query_for("BR-10: Several methods carry no authorization") == "Several methods carry no authorization"
    assert query_for("Glossary: HttpClient") == "Glossary: HttpClient"
    assert query_for("BR-3a: x") == "x"


def test_check_retrieval_hit_rate_and_retry(facts: Path, tmp_path: Path) -> None:
    out = tmp_path / "retrieval-check.json"
    runtime = FakeRuntime(facts, fail_first=1)
    res = check_retrieval(facts, "KB", client_factory=factory_for(runtime=runtime), out=out, workers=1)
    s = res["summary"]
    assert s["facts"] == 3 and s["hits"] == 3 and s["hit_rate"] == 1.0 and s["errors"] == 0
    assert s["by_fact_type"]["rule"] == {"facts": 2, "hits": 2, "hit_rate": 1.0}
    row = next(r for r in res["facts"] if r["fact_id"] == "visit-BR-1")
    assert row["query"] == "Visit end must follow visit start" and len(row["top"]) == 3 and row["rank"] == 1
    assert isinstance(row["latency_ms"], int) and res["facts"][0]["attempts"] == 2
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["top_k"] == 3


def test_check_retrieval_misses_foreign_ids_and_errors(facts: Path) -> None:
    class Wrong:
        def retrieve(self, **kw: Any) -> dict:
            if "Feature" in kw["retrievalQuery"]["text"]:
                raise ClientError("AccessDeniedException")
            return {"retrievalResults": [{"location": {"s3Location": {"uri": "s3://amzn-s3-demo-bucket/verified/test/TEST-9.md"}}}]}

    res = check_retrieval(facts, "KB", client_factory=factory_for(runtime=Wrong()), top_k=1)
    s = res["summary"]
    assert s["hits"] == 0 and s["errors"] == 1 and s["foreign_ids_in_results"] == ["TEST-9"]
    assert sorted(s["misses"]) == ["order-BR-2", "order-FEATURE-MAP", "visit-BR-1"]
    with pytest.raises(Exception, match="top-k"):
        check_retrieval(facts, "KB", client_factory=factory_for(runtime=Wrong()), top_k=0)


# --------------------------------------------------------------------------------------- boto3 + CLI


def test_default_factory_uses_boto3_with_metadata_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[tuple[str, str]] = []
    module = types.SimpleNamespace(client=lambda name, region_name: made.append((name, region_name)) or object())
    monkeypatch.setitem(sys.modules, "boto3", module)
    monkeypatch.delenv("AWS_EC2_METADATA_DISABLED", raising=False)
    factory = kb_sync.default_client_factory("us-east-1")
    first = factory("s3")
    assert factory("s3") is first and made == [("s3", "us-east-1")]
    import os

    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"


def test_cli_sync_audit_retrieval_with_state(facts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    s3, agent, runtime = FakeS3(), FakeAgent(), FakeRuntime(facts)
    regions: list[str] = []

    def fake_default(region: str):
        regions.append(region)
        return factory_for(s3=s3, agent=agent, runtime=runtime)

    monkeypatch.setattr(kb_sync, "default_client_factory", fake_default)
    monkeypatch.setattr(kb_sync.time, "sleep", no_sleep)
    state = tmp_path / "kb_state.json"
    state.write_text(json.dumps({"region": "eu-west-1", "bucket": BUCKET, "kb_id": "KB", "data_source_id": "DS"}),
                     encoding="utf-8")
    report = tmp_path / "sync.json"
    assert main(["kb", "sync", "--facts", str(facts), "--state", str(state), "--out", str(report)]) == 0
    assert "uploaded 6, deleted 0" in capsys.readouterr().out
    assert main(["kb", "audit", "--facts", str(facts), "--state", str(state), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["all_allowed"] is True
    assert main(["kb", "check-retrieval", "--facts", str(facts), "--state", str(state), "--workers", "1"]) == 0
    assert "3/3 in top 3" in capsys.readouterr().out
    assert regions == ["eu-west-1"] * 3 and report.is_file()
    s3.seed("verified/test/x.md", b"x")
    assert main(["kb", "audit", "--facts", str(facts), "--bucket", BUCKET]) == 1
