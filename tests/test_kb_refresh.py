"""Tests for bcp.kb_refresh (``bcp kb refresh``) with fake S3 / bedrock-agent clients; no AWS calls.

``test_refresh_reanchors_quarantines_and_lists_gap`` is the fail-on-revert check for the re-anchor and
quarantine logic: revert the re-anchor in bcp.kb_reanchor (so a moved block is treated as changed) and
BR-1 is quarantined instead of re-anchored, which fails the assertions on the re-anchored fact.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from bcp import cli, kb, kb_refresh
from bcp.claims import body_sha256
from bcp.config import BcpConfig

BUCKET = "amzn-s3-demo-bucket"
WIDGET = [f"    line {n} of widget" for n in range(1, 21)]
GADGET = [f"    line {n} of gadget" for n in range(1, 11)]
W, G = "src/app/WidgetService.java", "src/app/Gadget.java"
HEAD1, BODY1 = "## BR-1: Widgets are checked before the save", ["", "The check runs first.", "", "- Source: `x`"]
HEAD2, BODY2 = "## BR-2: Gadgets are copied", ["", "Gadgets are copied.", "", "- Source: `y`"]
RULES_MD = "\n".join(["# Reviewed business rules: Alpha (`alpha`)", "", HEAD1,
                      "> Review: VERIFIED · AI-reviewed, not SME-signed · evidence: x", *BODY1, "", HEAD2,
                      "> Review: VERIFIED · AI-reviewed, not SME-signed · evidence: x", *BODY2, ""])


def _range(path: str, lines: list[str], start: int, end: int) -> dict[str, Any]:
    return {"kind": "range", "path": path, "start": start, "end": end, "quote": "\n".join(lines[start - 1:end]),
            "file_sha256": "0" * 64}


def _entry(head: str, body: list[str], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {"verdict": "verified", "body_sha256": body_sha256("\n".join([head] + body)), "reviewer": "r",
            "reviewer_type": "kiro", "reviewed_at": "2026-01-02T03:04:05Z", "note": "n", "correction": None,
            "suspected_defect": False, "evidence": evidence}


class FakeS3:
    """In-memory bucket: list, put, copy and batch delete (ETag = MD5)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    def list_objects_v2(self, Bucket: str, Prefix: str, ContinuationToken: str | None = None) -> dict:
        return {"Contents": [{"Key": k, "Size": len(v),
                              "ETag": '"' + hashlib.md5(v, usedforsecurity=False).hexdigest() + '"'}
                             for k, v in sorted(self.objects.items()) if k.startswith(Prefix)], "IsTruncated": False}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "", Metadata: dict | None = None) -> dict:
        self.objects[Key] = Body
        self.puts.append(Key)
        return {}

    def copy_object(self, Bucket: str, Key: str, CopySource: dict, MetadataDirective: str) -> dict:
        self.objects[Key] = self.objects[CopySource["Key"]]
        return {}

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
        return {}


class FakeAgent:
    def __init__(self) -> None:
        self.started = 0

    def start_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str) -> dict:
        self.started += 1
        return {"ingestionJob": {"ingestionJobId": f"JOB{self.started}", "status": "STARTING"}}

    def get_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str, ingestionJobId: str) -> dict:
        return {"ingestionJob": {"ingestionJobId": ingestionJobId, "status": "COMPLETE",
                                 "statistics": {"numberOfDocumentsScanned": 10}, "failureReasons": []}}


@pytest.fixture
def ws(tmp_path: Path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    (repo / "src" / "app").mkdir(parents=True)
    (repo / W).write_text("\n".join(WIDGET) + "\n", encoding="utf-8")
    (repo / G).write_text("\n".join(GADGET) + "\n", encoding="utf-8")
    ctx = repo / ".kiro" / "context" / "domains" / "alpha"
    ctx.mkdir(parents=True)
    (ctx / "reviewed-rules.md").write_text(RULES_MD, encoding="utf-8")
    review = repo / ".kiro" / "bre-review"
    review.mkdir(parents=True)
    claims = {"alpha/BR-1": _entry(HEAD1, BODY1, [_range(W, WIDGET, 3, 5)]),
              "alpha/BR-2": _entry(HEAD2, BODY2, [_range(G, GADGET, 1, 2)])}
    record = review / "review-record.json"
    record.write_text(json.dumps({"schema": "bcp.review/3", "repo_head": "abc", "claims": claims}, indent=2) + "\n",
                      encoding="utf-8")
    facts = tmp_path / "facts"
    kb.export_facts(repo, facts, BcpConfig(), domains=["alpha"])
    s3 = FakeS3()
    for path in (facts / "verified").rglob("*"):
        if path.is_file():
            s3.objects["verified/" + path.relative_to(facts / "verified").as_posix()] = path.read_bytes()
    agent = FakeAgent()
    return {"repo": repo, "facts": facts, "record": record, "s3": s3, "agent": agent,
            "factory": {"s3": s3, "bedrock-agent": agent}.__getitem__}


def _changed_copy(ws: dict[str, Any], tmp_path: Path) -> Path:
    """A source-root copy where BR-1's method moved down (re-anchor) and BR-2's lines changed (quarantine),
    plus a brand-new uncited source file (coverage gap)."""
    root = tmp_path / "copy"
    shutil.copytree(ws["repo"] / "src", root / "src")
    (root / W).write_text("\n".join(["import a;", "import b;", *WIDGET]) + "\n", encoding="utf-8")  # moved +2
    (root / G).write_text("\n".join(["    line 1 of gadget CHANGED", *GADGET[1:]]) + "\n", encoding="utf-8")
    (root / "src" / "app" / "NewFeature.java").write_text("class NewFeature {}\n", encoding="utf-8")
    return root


def _refresh(ws: dict[str, Any], root: Path, **kw: Any) -> dict[str, Any]:
    return kb_refresh.refresh(ws["facts"], BcpConfig(), BUCKET, "KB", "DS", repo=ws["repo"], source_root=root,
                              client_factory=ws["factory"], sleep=lambda _s: None, **kw)


def test_refresh_reanchors_quarantines_and_lists_gap(ws: dict[str, Any], tmp_path: Path) -> None:
    root = _changed_copy(ws, tmp_path)
    before = next(f for f in kb.export_facts(ws["repo"], tmp_path / "e", BcpConfig(), domains=["alpha"]).facts
                  if f.fact_id == "alpha-BR-1")
    res = _refresh(ws, root, report=tmp_path / "r.json")

    # BR-1 moved but unchanged -> re-anchored (ranges 3-5 -> 5-7), still verified, not quarantined
    assert res["counts"]["reanchored"] == 1
    ra = res["reanchored"][0]
    assert ra["fact_id"] == "alpha-BR-1" and ra["old_ranges"] == [f"{W}#L3-L5"] and ra["new_ranges"] == [f"{W}#L5-L7"]
    assert "alpha-BR-1" not in res["quarantined"]
    meta = json.loads((ws["facts"] / "verified" / "alpha" / "alpha-BR-1.md.metadata.json").read_text("utf-8"))
    assert meta["metadataAttributes"]["evidence"] == [f"{W}#L5-L7"]
    assert meta["metadataAttributes"]["content_hash"] == before.content_hash  # content-hash survives the move

    # BR-2's cited lines changed -> quarantined and queued for re-review
    assert "alpha-BR-2" in res["quarantined"]
    assert not (ws["facts"] / "verified" / "alpha" / "alpha-BR-2.md").exists()
    assert (ws["facts"] / "quarantine" / "alpha" / "alpha-BR-2.md").is_file()
    assert "verified/alpha/alpha-BR-2.md" not in ws["s3"].objects

    # the new uncited source file is a coverage gap
    assert "src/app/NewFeature.java" in res["coverage_gap"]

    # re-ingested exactly once; the re-anchored fact was re-uploaded
    assert ws["agent"].started == 1 and res["ingestion"]["status"] == "COMPLETE"
    assert "verified/alpha/alpha-BR-1.md" in ws["s3"].puts

    # the glossary fact that cites only the re-anchored BR-1 stays verified; the facts derived from the
    # changed BR-2 are quarantined with it
    assert (ws["facts"] / "verified" / "alpha" / "GLOSSARY-WidgetService.md").is_file()
    assert res["quarantined"] == ["GLOSSARY-Gadget", "alpha-BR-2", "alpha-FEATURE-MAP"]

    # queue + gap files written
    queue = json.loads((ws["facts"] / kb_refresh.QUEUE_JSON).read_text("utf-8"))
    assert [q["fact_id"] for q in queue["quarantined"]] == ["GLOSSARY-Gadget", "alpha-BR-2", "alpha-FEATURE-MAP"]
    assert len(queue["reanchored"]) == 1
    gap = json.loads((ws["facts"] / kb_refresh.GAP_JSON).read_text("utf-8"))
    assert "src/app/NewFeature.java" in gap["coverage_gap"]
    assert json.loads((tmp_path / "r.json").read_text("utf-8"))["schema"] == kb_refresh.SCHEMA_REFRESH

    # the real repository source was not touched
    assert (ws["repo"] / W).read_text("utf-8") == "\n".join(WIDGET) + "\n"


def test_reanchored_fact_is_clean_on_a_second_refresh(ws: dict[str, Any], tmp_path: Path) -> None:
    root = _changed_copy(ws, tmp_path)
    _refresh(ws, root)
    # BR-1 re-anchored, BR-2 quarantined; a second refresh re-anchors nothing and keeps BR-2 held
    again = _refresh(ws, root)
    assert again["counts"]["reanchored"] == 0 and "alpha-BR-1" not in again["quarantined"]


def test_coverage_gap_limited_to_changed_files(ws: dict[str, Any], tmp_path: Path) -> None:
    root = _changed_copy(ws, tmp_path)
    res = _refresh(ws, root, changed_files=["src/app/NewFeature.java", W])
    # only the listed files are considered; W is cited by BR-1 so only NewFeature.java is a gap
    assert res["coverage_gap"] == ["src/app/NewFeature.java"]


def test_dry_run_changes_nothing(ws: dict[str, Any], tmp_path: Path) -> None:
    root = _changed_copy(ws, tmp_path)
    original = ws["record"].read_bytes()
    res = _refresh(ws, root, dry_run=True)
    assert res["counts"]["reanchored"] == 1 and "alpha-BR-2" in res["quarantined"]
    assert ws["record"].read_bytes() == original  # record untouched
    assert ws["agent"].started == 0 and not ws["s3"].puts  # no AWS writes
    assert (ws["facts"] / "verified" / "alpha" / "alpha-BR-2.md").is_file()  # nothing quarantined on disk


def test_graph_rebuild_is_invoked(ws: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _changed_copy(ws, tmp_path)
    calls: list[Any] = []

    def fake_build(repo: Any, config: Any, domains: Any, **kw: Any) -> dict[str, Any]:
        calls.append((repo, list(domains)))
        return {"commit": "deadbeef", "nodes": 1}

    monkeypatch.setattr("bcp.graph.run_build", fake_build)
    res = _refresh(ws, root, graph_domains=["alpha"])
    assert calls and calls[0][1] == ["alpha"] and res["graph"]["rebuilt"] and res["graph"]["commit"] == "deadbeef"


def test_cli_refresh(ws: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                     capsys: pytest.CaptureFixture[str]) -> None:
    root = _changed_copy(ws, tmp_path)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"region": "us-east-1", "bucket": BUCKET, "kb_id": "KB", "data_source_id": "DS"}),
                     encoding="utf-8")
    monkeypatch.setattr("bcp.kb_sync.default_client_factory", lambda region: ws["factory"])
    monkeypatch.setattr("bcp.kb_stale.default_client_factory", lambda region: ws["factory"])
    monkeypatch.setattr("time.sleep", lambda _s: None)
    rc = cli.main(["kb", "refresh", "--facts", str(ws["facts"]), "--state", str(state),
                   "--repo", str(ws["repo"]), "--source-root", str(root), "--out", str(tmp_path / "out.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "re-anchored 1" in out and "quarantined 3" in out
    assert json.loads((tmp_path / "out.json").read_text("utf-8"))["counts"]["reanchored"] == 1
