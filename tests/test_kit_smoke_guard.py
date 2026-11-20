"""The kit smoke test refuses to sync over a knowledge base that holds other facts (no AWS calls)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test.py"


@pytest.fixture()
def smoke(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("kit_smoke_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeS3:
    """Just enough of ``list_objects_v2`` for ``kb_sync.list_remote``."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys

    def list_objects_v2(self, Bucket: str, Prefix: str, **_: Any) -> dict[str, Any]:
        return {"Contents": [{"Key": k, "ETag": '"x"', "Size": 1} for k in self.keys if k.startswith(Prefix)],
                "IsTruncated": False}


def _setup(smoke: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keys: list[str]) -> dict[str, int]:
    state = tmp_path / "kb_state.json"
    state.write_text(json.dumps({"region": "us-east-1", "bucket": "amzn-s3-demo-bucket", "kb_id": "KBEXAMPLE1",
                                 "data_source_id": "DSEXAMPLE1"}), encoding="utf-8")
    s3 = FakeS3(keys)
    calls = {"sync": 0}

    def factory(name: str) -> Any:
        return s3 if name == "s3" else object()

    def fake_sync(*_: Any, **__: Any) -> Any:
        calls["sync"] += 1
        raise RuntimeError("stop after the guard")

    monkeypatch.setattr(smoke.kb_sync, "default_client_factory", lambda region: factory)
    monkeypatch.setattr(smoke.kb_sync, "sync", fake_sync)
    return calls


def test_refuses_when_kb_holds_foreign_facts(smoke, monkeypatch, tmp_path):
    calls = _setup(smoke, monkeypatch, tmp_path, ["verified/billing/billing-BR-7.md"])
    out = tmp_path / "report.json"
    assert smoke.run(tmp_path / "kb_state.json", out, False, None) == 2
    assert calls["sync"] == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["refused"] is True and report["steps"]["guard"]["foreign_keys"] == 1


def test_refuses_foreign_quarantined_facts(smoke, monkeypatch, tmp_path):
    calls = _setup(smoke, monkeypatch, tmp_path, ["quarantine/billing/billing-BR-7.md"])
    assert smoke.run(tmp_path / "kb_state.json", tmp_path / "r.json", False, None) == 2
    assert calls["sync"] == 0


def test_own_smoke_facts_do_not_block_a_rerun(smoke, monkeypatch, tmp_path):
    keys = ["verified/orders/orders-BR-1.md", "verified/orders/orders-BR-1.md.metadata.json",
            "quarantine/orders/orders-BR-2.md"]
    calls = _setup(smoke, monkeypatch, tmp_path, keys)
    assert smoke.run(tmp_path / "kb_state.json", tmp_path / "r.json", False, None) == 1  # stopped by fake sync
    assert calls["sync"] == 1


def test_override_flag_lets_the_run_continue(smoke, monkeypatch, tmp_path):
    calls = _setup(smoke, monkeypatch, tmp_path, ["verified/billing/billing-BR-7.md"])
    assert smoke.run(tmp_path / "kb_state.json", tmp_path / "r.json", False, None, allow_nonempty=True) == 1
    assert calls["sync"] == 1


def test_override_cannot_combine_with_purge(smoke, monkeypatch, tmp_path):
    calls = _setup(smoke, monkeypatch, tmp_path, [])
    assert smoke.run(tmp_path / "kb_state.json", tmp_path / "r.json", False, "amzn-s3-demo-bucket",
                     allow_nonempty=True) == 2
    assert calls["sync"] == 0
