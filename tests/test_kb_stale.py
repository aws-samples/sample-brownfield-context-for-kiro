"""Tests for bcp.kb_stale (``bcp kb stale``) with fake S3 / bedrock-agent clients; no AWS calls."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from bcp import cli, kb, kb_stale
from bcp.claims import body_sha256
from bcp.config import BcpConfig
from bcp.errors import BcpInputError
from bcp.kb_sync import KbSyncError
from bcp.review import load_review_record

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
    """In-memory bucket with copy, list and batch delete (ETag = MD5)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.copies: list[tuple[str, str]] = []

    def list_objects_v2(self, Bucket: str, Prefix: str, ContinuationToken: str | None = None) -> dict:
        assert Bucket == BUCKET
        return {"Contents": [{"Key": k, "Size": len(v), "ETag": '"' + hashlib.md5(v, usedforsecurity=False).hexdigest() + '"'}
                             for k, v in sorted(self.objects.items()) if k.startswith(Prefix)], "IsTruncated": False}

    def copy_object(self, Bucket: str, Key: str, CopySource: dict, MetadataDirective: str) -> dict:
        assert Bucket == BUCKET and CopySource["Bucket"] == BUCKET and MetadataDirective == "COPY"
        self.objects[Key] = self.objects[CopySource["Key"]]
        self.copies.append((CopySource["Key"], Key))
        return {}

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"])
        return {}


class FakeAgent:
    def __init__(self, final: str = "COMPLETE") -> None:
        self.started = 0
        self.final = final

    def start_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str) -> dict:
        self.started += 1
        return {"ingestionJob": {"ingestionJobId": f"JOB{self.started}", "status": "STARTING"}}

    def get_ingestion_job(self, knowledgeBaseId: str, dataSourceId: str, ingestionJobId: str) -> dict:
        return {"ingestionJob": {"ingestionJobId": ingestionJobId, "status": self.final,
                                 "statistics": {"numberOfDocumentsDeleted": 3}, "failureReasons": []}}


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
    scratch = tmp_path / "scratch" / "WidgetService.java"
    scratch.parent.mkdir()
    shutil.copyfile(repo / W, scratch)
    return {"repo": repo, "facts": facts, "record": record, "original": record.read_bytes(), "s3": s3,
            "agent": agent, "scratch": scratch, "factory": {"s3": s3, "bedrock-agent": agent}.__getitem__}


def _run(ws: dict[str, Any], **kw: Any) -> dict[str, Any]:
    kw.setdefault("path_map", {W: ws["scratch"]})
    return kb_stale.stale(ws["facts"], BUCKET, "KB", "DS", client_factory=ws["factory"], sleep=lambda _s: None, **kw)


def _edit_scratch(ws: dict[str, Any]) -> None:
    lines = list(WIDGET)
    lines[3] = "    line 4 of widget CHANGED"
    ws["scratch"].write_text("\n".join(lines) + "\n", encoding="utf-8")


STALE_SET = ["GLOSSARY-WidgetService", "alpha-BR-1", "alpha-FEATURE-MAP"]


def test_clean_run_moves_nothing_and_calls_no_aws(ws: dict[str, Any]) -> None:
    res = kb_stale.stale(ws["facts"], BUCKET, "KB", "DS", path_map={W: ws["scratch"]},
                         client_factory=lambda name: pytest.fail(f"unexpected client {name}"))
    assert res["clean"] and res["counts"]["checked"] == 5 and res["counts"]["clean"] == 5
    assert res["ingestion"] is None and res["facts"] == []
    assert ws["record"].read_bytes() == ws["original"]


def test_changed_lines_quarantine_mark_and_ingest(ws: dict[str, Any], tmp_path: Path) -> None:
    repo_before = (ws["repo"] / W).read_bytes()
    _edit_scratch(ws)
    res = _run(ws, report=tmp_path / "stale.json")
    assert res["quarantined"] == STALE_SET and res["counts"]["clean"] == 2
    facts, s3 = ws["facts"], ws["s3"]
    for fid, dom in (("alpha-BR-1", "alpha"), ("alpha-FEATURE-MAP", "alpha")):
        for suffix in (".md", ".md.metadata.json"):
            assert not (facts / "verified" / dom / f"{fid}{suffix}").exists()
            assert (facts / "quarantine" / dom / f"{fid}{suffix}").is_file()
            assert f"verified/{dom}/{fid}{suffix}" not in s3.objects
            assert f"quarantine/{dom}/{fid}{suffix}" in s3.objects
    assert "verified/alpha/alpha-BR-2.md" in s3.objects and (facts / "verified" / "alpha" / "alpha-BR-2.md").is_file()
    assert len(s3.copies) == 6 and ws["agent"].started == 1
    assert res["ingestion"]["statistics"]["numberOfDocumentsDeleted"] == 3
    row = next(r for r in res["facts"] if r["fact_id"] == "alpha-BR-1")
    assert row["changed_ranges"] == [f"{W}#L3-L5"] and not row["code_ok"]
    # review record: bcp.review/3 entry now unreviewed with the stale mark; the reviewed entry is in history
    rec = load_review_record(ws["record"])
    entry = rec["claims"]["alpha/BR-1"]
    assert rec["schema"] == "bcp.review/3" and entry["verdict"] == "unreviewed"
    assert entry["stale"]["status"] == "needs-re-review" and entry["stale"]["fact_id"] == "alpha-BR-1"
    assert entry["stale"]["previous_verdict"] == "verified"
    assert entry["history"][-1]["verdict"] == "verified" and entry["history"][-1]["evidence"]
    assert rec["claims"]["alpha/BR-2"]["verdict"] == "verified"
    assert res["marked"] == [{"claim": "alpha/BR-1", "fact_id": "alpha-BR-1", "previous_verdict": "verified"}]
    assert json.loads((tmp_path / "stale.json").read_text(encoding="utf-8"))["schema"] == kb_stale.SCHEMA_STALE
    assert (ws["repo"] / W).read_bytes() == repo_before
    # a fresh export now refuses the marked rule: it needs re-review
    again = kb.export_facts(ws["repo"], tmp_path / "facts2", BcpConfig(), domains=["alpha"])
    assert "alpha-BR-1" not in {f.fact_id for f in again.facts}


def test_second_run_while_stale_does_nothing_new(ws: dict[str, Any]) -> None:
    _edit_scratch(ws)
    _run(ws)
    after_mark = ws["record"].read_bytes()
    res = _run(ws)
    assert res["quarantined"] == [] and res["held"] == STALE_SET and res["ingestion"] is None
    assert ws["record"].read_bytes() == after_mark and ws["agent"].started == 1


def test_code_restored_alone_holds_until_review_restored(ws: dict[str, Any]) -> None:
    _edit_scratch(ws)
    _run(ws)
    shutil.copyfile(ws["repo"] / W, ws["scratch"])
    held = _run(ws)
    assert held["released"] == [] and held["held"] == STALE_SET and ws["agent"].started == 1
    assert all(r["code_ok"] and not r["review_ok"] for r in held["facts"])
    ws["record"].write_bytes(ws["original"])
    res = _run(ws)
    assert res["released"] == STALE_SET and res["clean"] and ws["agent"].started == 2
    assert not any((ws["facts"] / "quarantine").rglob("*.md"))
    assert sorted(k for k in ws["s3"].objects if k.startswith("quarantine/")) == []
    assert len([k for k in ws["s3"].objects if k.startswith("verified/")]) == 10
    assert ws["record"].read_bytes() == ws["original"]
    assert _run(ws)["clean"] and ws["agent"].started == 2


def test_rereview_releases(ws: dict[str, Any]) -> None:
    _edit_scratch(ws)
    _run(ws)
    shutil.copyfile(ws["repo"] / W, ws["scratch"])
    data = json.loads(ws["record"].read_text(encoding="utf-8"))
    data["claims"]["alpha/BR-1"] = _entry(HEAD1, BODY1, [_range(W, WIDGET, 3, 5)])  # a new review, no stale mark
    ws["record"].write_text(json.dumps(data), encoding="utf-8")
    assert _run(ws)["released"] == STALE_SET


def test_real_change_rereview_reexport_is_clean(ws: dict[str, Any]) -> None:
    """Code really changes -> quarantine; SME re-reviews the new lines; re-export retires the quarantined copies."""
    _edit_scratch(ws)
    assert _run(ws)["quarantined"] == STALE_SET
    new_lines = ws["scratch"].read_text(encoding="utf-8").splitlines()
    shutil.copyfile(ws["scratch"], ws["repo"] / W)  # the change lands in the repository (tmp copy)
    data = json.loads(ws["record"].read_text(encoding="utf-8"))
    data["claims"]["alpha/BR-1"] = _entry(HEAD1, BODY1, [_range(W, new_lines, 3, 5)])  # re-review, new evidence
    ws["record"].write_text(json.dumps(data), encoding="utf-8")
    res = kb.export_facts(ws["repo"], ws["facts"], BcpConfig(), domains=["alpha"])
    assert "alpha-BR-1" in {f.fact_id for f in res.facts}
    assert not any((ws["facts"] / "quarantine").rglob("*.md*"))
    assert {p.name for p in res.removed} >= {"alpha-BR-1.md", "alpha-BR-1.md.metadata.json"}
    after = _run(ws, path_map={})
    assert after["clean"] and after["quarantined"] == [] and after["held"] == [] and after["released"] == []
    assert after["counts"]["checked"] == 5


def test_dry_run_and_no_mark(ws: dict[str, Any]) -> None:
    _edit_scratch(ws)
    dry = _run(ws, dry_run=True)
    assert dry["quarantined"] == STALE_SET and dry["counts"]["marked"] == 1 and dry["moved"] == []
    assert ws["record"].read_bytes() == ws["original"] and not ws["s3"].copies
    res = _run(ws, mark=False)
    assert res["quarantined"] == STALE_SET and res["marked"] == []
    assert ws["record"].read_bytes() == ws["original"]
    shutil.copyfile(ws["repo"] / W, ws["scratch"])
    assert _run(ws, release=False)["held"] == STALE_SET
    assert _run(ws)["released"] == STALE_SET


def test_source_root_override(ws: dict[str, Any], tmp_path: Path) -> None:
    root = tmp_path / "copy"
    shutil.copytree(ws["repo"] / "src", root / "src")
    (root / G).write_text("\n".join(["changed"] + GADGET[1:]) + "\n", encoding="utf-8")
    res = _run(ws, path_map={}, source_root=root)
    assert res["quarantined"] == ["GLOSSARY-Gadget", "alpha-BR-2", "alpha-FEATURE-MAP"]


def test_review_no_longer_exportable_quarantines_without_mark(ws: dict[str, Any]) -> None:
    data = json.loads(ws["record"].read_text(encoding="utf-8"))
    data["claims"]["alpha/BR-2"]["verdict"] = "needs-sme"
    ws["record"].write_text(json.dumps(data), encoding="utf-8")
    before = ws["record"].read_bytes()
    res = _run(ws)
    assert res["quarantined"] == ["GLOSSARY-Gadget", "alpha-BR-2", "alpha-FEATURE-MAP"] and res["marked"] == []
    assert ws["record"].read_bytes() == before


def test_missing_cited_file_is_stale(ws: dict[str, Any], tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    res = _run(ws, path_map={}, source_root=root, mark=False)
    assert res["counts"]["quarantined"] == 5


def test_failed_ingestion_raises(ws: dict[str, Any]) -> None:
    ws["agent"].final = "FAILED"
    _edit_scratch(ws)
    with pytest.raises(KbSyncError, match="FAILED"):
        _run(ws)


def test_input_errors(ws: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(BcpInputError, match="REL=FILE"):
        kb_stale.parse_path_map(["nofile"])
    with pytest.raises(BcpInputError, match="not found"):
        kb_stale.parse_path_map([f"{W}={tmp_path / 'missing.java'}"])
    assert kb_stale.parse_path_map([f"./{W}={ws['scratch']}"]) == {W: ws["scratch"]}
    with pytest.raises(BcpInputError, match="differ"):
        _run(ws, quarantine_prefix="verified/")
    with pytest.raises(BcpInputError, match="export"):
        kb_stale.stale(tmp_path / "nothing", BUCKET, "KB", "DS")
    with pytest.raises(BcpInputError, match="kb-id"):
        kb_stale.stale(ws["facts"], BUCKET, None, None)
    with pytest.raises(BcpInputError, match="review record"):
        kb_stale.stale(ws["facts"], BUCKET, "KB", "DS", review_dir=tmp_path)


def test_cli_stale_with_state(ws: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                              capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "kb_state.json"
    state.write_text(json.dumps({"region": "us-east-1", "bucket": BUCKET, "kb_id": "KB", "data_source_id": "DS"}),
                     encoding="utf-8")
    monkeypatch.setattr(kb_stale, "default_client_factory", lambda region: ws["factory"])
    monkeypatch.setattr("time.sleep", lambda _s: None)
    _edit_scratch(ws)
    rc = cli.main(["kb", "stale", "--facts", str(ws["facts"]), "--state", str(state),
                   "--path-map", f"{W}={ws['scratch']}", "--out", str(tmp_path / "r.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "quarantined 3" in out and "ingestion JOB1: COMPLETE" in out
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["quarantined"] == STALE_SET
