"""End-to-end smoke test of a deployed validated knowledge base, with synthetic data only.

What it does, against the resources named in a state file (``kb_state.json`` from ``bcp kb provision``
or from ``scripts/cfn_stack.py state`` for the CloudFormation route):

0. safety guard: lists ``verified/`` and ``quarantine/`` in the bucket and refuses (exit 2, nothing
   changed) when any key there is not one of this test's own synthetic facts. ``bcp kb sync`` deletes
   facts the export no longer has, so running the test against a populated knowledge base would remove
   its reviewed facts. ``--allow-nonempty-kb`` overrides the guard (test stacks only; it cannot be
   combined with ``--purge-bucket``);

1. builds a throw-away workspace in a temp folder: two synthetic Java files and two reviewed rules
   (``orders/BR-1``, ``orders/BR-2``) with a review record, as the review workflow would leave them;
2. ``bcp kb export``: the trust filter writes the facts (2 rule facts plus derived feature-map and
   glossary facts) and ``export-audit.json``;
3. ``bcp kb sync``: uploads the facts under ``verified/`` and runs an ingestion job;
4. ``bcp kb check-retrieval``: one ``Retrieve`` per fact; both rule facts must come back in the top 3;
5. runs the ``UserPromptSubmit`` hook once (summary <= 2,500 bytes, no fallback) and once against a
   closed local port (graph-only fallback, still a summary, never an exception);
6. ``bcp kb stale``: changes a cited line of ``orders/BR-1``; the fact and its derived facts move to
   ``quarantine/`` and leave the index; ``Retrieve`` no longer returns ``orders-BR-1``;
7. restores the code only: the facts stay held, because the review is still marked stale;
8. restores the review record (stands in for a re-review): the facts are released, ``orders-BR-1`` is
   back in the top 3, and ``bcp kb audit`` reports no violations;
9. optional ``--purge-bucket <name>``: deletes every object version in that bucket and re-ingests, so a
   test stack with ``RetainData=false`` can be deleted cleanly.

It calls bcp in-process (no subprocess, no shell). AWS access uses the standard credential chain.
Exit code 0 when every check passed, 1 otherwise. A JSON report goes to ``--out`` (default
``smoke-report.json`` in the current folder). The report holds resource ids, not credentials.

Usage::

    python scripts/smoke_test.py --state kb_state.json [--out smoke-report.json] [--keep-workspace]
                                 [--purge-bucket <bucket from the state file>] [--allow-nonempty-kb]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bcp import kb, kb_hook, kb_stale, kb_sync  # noqa: E402
from bcp.claims import body_sha256  # noqa: E402
from bcp.config import BcpConfig  # noqa: E402

DOMAIN = "orders"
LIMIT_FILE = "src/main/java/example/orders/OrderLimitService.java"
NOTIFY_FILE = "src/main/java/example/orders/ShipmentNotifier.java"
LIMIT_LINES = [
    "package example.orders;",
    "",
    "public class OrderLimitService {",
    "    static final int MAX_LINES = 50;",
    "",
    "    public void checkLimit(Order order) {",
    "        if (order.lineCount() > MAX_LINES) {",
    "            throw new OrderLimitException(\"an order holds at most \" + MAX_LINES + \" lines\");",
    "        }",
    "    }",
    "}",
]
NOTIFY_LINES = [
    "package example.orders;",
    "",
    "public class ShipmentNotifier {",
    "    public void onShipped(Order order) {",
    "        mailer.send(order.customerEmail(), \"shipment-confirmation\", order.id());",
    "    }",
    "}",
]
HEAD1 = "## BR-1: An order with more than 50 lines is rejected"
BODY1 = ["", "OrderLimitService.checkLimit throws OrderLimitException when an order has more than 50 lines.", "",
         "- Source: `OrderLimitService.checkLimit`"]
HEAD2 = "## BR-2: A shipment confirmation email is sent when an order ships"
BODY2 = ["", "ShipmentNotifier.onShipped sends the shipment-confirmation email to the customer.", "",
         "- Source: `ShipmentNotifier.onShipped`"]
REVIEW_LINE = "> Review: VERIFIED · AI-reviewed, not SME-signed · evidence: synthetic smoke-test rule"
RULE_FACTS = (f"{DOMAIN}-BR-1", f"{DOMAIN}-BR-2")
QUERY1 = "Is an order with more than 50 lines rejected?"
RETRY_SECONDS = 5
RETRIES = 12


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")  # UTF-8, no BOM, LF


def _range(rel: str, lines: list[str], start: int, end: int, text: str) -> dict[str, Any]:
    return {"kind": "range", "path": rel, "start": start, "end": end, "quote": "\n".join(lines[start - 1:end]),
            "file_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _entry(head: str, body: list[str], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {"verdict": "verified", "body_sha256": body_sha256("\n".join([head] + body)),
            "reviewer": "smoke-test", "reviewer_type": "kiro", "reviewed_at": _now(),
            "note": "synthetic rule for the smoke test", "correction": None, "suspected_defect": False,
            "evidence": evidence}


def build_workspace(base: Path) -> dict[str, Path]:
    """Synthetic repository with a reviewed-rules file and a review record (no customer data)."""
    repo = base / "repo"
    limit_text = "\n".join(LIMIT_LINES) + "\n"
    notify_text = "\n".join(NOTIFY_LINES) + "\n"
    _write(repo / LIMIT_FILE, limit_text)
    _write(repo / NOTIFY_FILE, notify_text)
    rules = "\n".join(["# Reviewed business rules: Orders (`orders`)", "", HEAD1, REVIEW_LINE, *BODY1, "",
                       HEAD2, REVIEW_LINE, *BODY2, ""])
    _write(repo / ".kiro" / "context" / "domains" / DOMAIN / "reviewed-rules.md", rules)
    claims = {f"{DOMAIN}/BR-1": _entry(HEAD1, BODY1, [_range(LIMIT_FILE, LIMIT_LINES, 6, 9, limit_text)]),
              f"{DOMAIN}/BR-2": _entry(HEAD2, BODY2, [_range(NOTIFY_FILE, NOTIFY_LINES, 4, 6, notify_text)])}
    record = repo / ".kiro" / "bre-review" / "review-record.json"
    _write(record, json.dumps({"schema": "bcp.review/3", "repo_head": "smoke-test", "claims": claims},
                              indent=2) + "\n")
    return {"repo": repo, "facts": base / "facts", "record": record, "limit": repo / LIMIT_FILE}


class Checks:
    """Collects named pass/fail results."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: Any = None) -> bool:
        self.rows.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail not in (None, "") else ""),
              flush=True)
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.rows) and all(r["ok"] for r in self.rows)


def retrieve_ids(runtime: Any, kb_id: str, query: str, top_k: int = 3) -> list[str]:
    """Fact ids of the top ``top_k`` Retrieve results for ``query``."""
    resp = runtime.retrieve(knowledgeBaseId=kb_id, retrievalQuery={"text": query},
                            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k}})
    return [kb_sync._result_id(r) for r in resp.get("retrievalResults", [])[:top_k]]


def wait_for(predicate: Callable[[], tuple[bool, Any]], retries: int = RETRIES,
             delay: float = RETRY_SECONDS) -> tuple[bool, Any]:
    """Poll ``predicate`` until it is true (Retrieve can lag the ingestion job by a few seconds)."""
    ok, detail = predicate()
    for _ in range(retries):
        if ok:
            break
        time.sleep(delay)
        ok, detail = predicate()
    return ok, detail


def purge_bucket(s3: Any, bucket: str) -> int:
    """Delete every object version and delete marker in ``bucket``; returns the count."""
    deleted = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        items = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                 for v in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])]
        for i in range(0, len(items), 1000):
            resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": items[i:i + 1000], "Quiet": True})
            if resp.get("Errors"):
                raise RuntimeError(f"delete failed: {resp['Errors'][:3]}")
            deleted += len(items[i:i + 1000])
    return deleted


GUARDED_PREFIXES = ("verified/", "quarantine/")


def smoke_keys(facts_dir: Path) -> set[str]:
    """Every S3 key this test's export can own, under ``verified/`` and ``quarantine/``."""
    keys = set(kb_sync.local_objects(facts_dir))
    return keys | {"quarantine/" + k[len("verified/"):] for k in keys if k.startswith("verified/")}


def foreign_keys(s3: Any, bucket: str, allowed: set[str]) -> list[str]:
    """Keys under the guarded prefixes that are not this test's own synthetic facts (sorted)."""
    found: set[str] = set()
    for prefix in GUARDED_PREFIXES:
        found |= set(kb_sync.list_remote(s3, bucket, prefix))
    return sorted(found - allowed)


def run(state_path: Path, out: Path, keep: bool, purge: str | None, allow_nonempty: bool = False) -> int:
    state = kb_sync.load_state(state_path)
    region, bucket = str(state["region"]), str(state["bucket"])
    kb_id, ds_id = str(state["kb_id"]), str(state["data_source_id"])
    if purge is not None and purge != bucket:
        print(f"--purge-bucket must name the state file's bucket exactly ({bucket}); nothing was done.",
              file=sys.stderr)
        return 2
    if purge is not None and allow_nonempty:
        print("--allow-nonempty-kb cannot be combined with --purge-bucket; nothing was done.", file=sys.stderr)
        return 2
    factory = kb_sync.default_client_factory(region)
    runtime = factory("bedrock-agent-runtime")
    checks = Checks()
    report: dict[str, Any] = {"schema": "kit.smoke-test/1", "started_at": _now(), "region": region,
                              "bucket": bucket, "kb_id": kb_id, "data_source_id": ds_id, "steps": {}}
    base = Path(tempfile.mkdtemp(prefix="kb-smoke-"))
    try:
        ws = build_workspace(base)
        original_code = ws["limit"].read_bytes()
        original_record = ws["record"].read_bytes()

        # 2. export (trust filter)
        exported = kb.export_facts(ws["repo"], ws["facts"], BcpConfig(), domains=[DOMAIN])
        ids = sorted(f.fact_id for f in exported.facts)
        report["steps"]["export"] = {"facts": ids, "summary": exported.audit["summary"]}
        checks.add("export: both reviewed rules exported", all(r in ids for r in RULE_FACTS), ids)

        # 0. guard: never sync over a knowledge base that holds facts other than this test's own
        foreign = foreign_keys(factory("s3"), bucket, smoke_keys(ws["facts"]))
        report["steps"]["guard"] = {"foreign_keys": len(foreign), "allow_nonempty_kb": allow_nonempty}
        if foreign and not allow_nonempty:
            report["refused"] = True
            print(f"refusing to run: {len(foreign)} object(s) under verified/ or quarantine/ in {bucket} are not "
                  "smoke-test facts, and the sync step would delete them. Run the smoke test right after "
                  "provisioning, against an empty knowledge base, or pass --allow-nonempty-kb on a test stack. "
                  "Nothing was changed. First keys:\n  " + "\n  ".join(foreign[:10]), file=sys.stderr)
            return 2
        checks.add("guard: knowledge base holds no facts other than the smoke test's",
                   not foreign or allow_nonempty, {"foreign_keys": len(foreign)})

        # 3. sync + ingestion
        synced = kb_sync.sync(ws["facts"], bucket, kb_id, ds_id, client_factory=factory, region=region)
        ing = synced["ingestion"] or {}
        report["steps"]["sync"] = {"counts": synced["counts"], "ingestion": ing}
        checks.add("sync: ingestion COMPLETE, 0 failed",
                   ing.get("status") == "COMPLETE"
                   and not (ing.get("statistics") or {}).get("numberOfDocumentsFailed"),
                   {"status": ing.get("status"), "statistics": ing.get("statistics")})

        # 4. retrieval check
        def both_hit() -> tuple[bool, Any]:
            res = kb_sync.check_retrieval(ws["facts"], kb_id, client_factory=factory, region=region, top_k=3)
            hits = {r["fact_id"]: r["rank"] for r in res["facts"]} if "facts" in res else {}
            return all(hits.get(f) for f in RULE_FACTS), res["summary"]
        ok, summary = wait_for(both_hit)
        report["steps"]["check_retrieval"] = summary
        checks.add("retrieve: both rule facts in the top 3", ok,
                   {"hits": summary.get("hits"), "facts": summary.get("facts"), "misses": summary.get("misses")})

        # 5. hook: normal run and a fallback drill
        event = json.dumps({"session_id": "smoke", "hook_event_name": "userPromptSubmit",
                            "cwd": str(ws["repo"]), "prompt": QUERY1})
        summary_text, record, _ = kb_hook.run_hook(event, {"kb_id": kb_id, "region": region,
                                                           "cwd": str(ws["repo"])})
        size = len(summary_text.encode("utf-8"))
        report["steps"]["hook"] = {"summary_bytes": size, "fallback": record.get("fallback"),
                                   "fact_ids": record.get("fact_ids"), "kb_ms": record.get("kb_ms"),
                                   "total_ms": record.get("total_ms")}
        checks.add("hook: verified fact injected, summary <= 2,500 bytes, no fallback",
                   size <= kb_hook.SUMMARY_LIMIT and not record.get("fallback")
                   and RULE_FACTS[0] in (record.get("fact_ids") or []),
                   {"bytes": size, "kb_ms": record.get("kb_ms"), "fact_ids": record.get("fact_ids")})
        drill_text, drill, _ = kb_hook.run_hook(event, {"kb_id": kb_id, "region": region, "cwd": str(ws["repo"]),
                                                        "endpoint_url": "https://127.0.0.1:9", "timeout_s": 4})
        report["steps"]["hook_fallback_drill"] = {"fallback": drill.get("fallback"),
                                                  "reason": drill.get("fallback_reason"),
                                                  "total_ms": drill.get("total_ms")}
        checks.add("hook: unreachable endpoint falls back, still returns a summary",
                   bool(drill.get("fallback")) and drill_text.startswith("## Retrieved context"),
                   drill.get("fallback_reason"))

        # 6. stale: change a cited line of BR-1
        changed = list(LIMIT_LINES)
        changed[6] = "        if (order.lineCount() >= MAX_LINES) {"
        ws["limit"].write_text("\n".join(changed) + "\n", encoding="utf-8", newline="\n")
        res = kb_stale.stale(ws["facts"], bucket, kb_id, ds_id, repo=ws["repo"], client_factory=factory,
                             region=region)
        report["steps"]["stale"] = {"quarantined": res["quarantined"], "ingestion": res["ingestion"]}
        checks.add("stale: BR-1 and its derived facts quarantined", RULE_FACTS[0] in res["quarantined"]
                   and RULE_FACTS[1] not in res["quarantined"], res["quarantined"])
        s3 = factory("s3")
        keys = set(kb_sync.list_remote(s3, bucket, "quarantine/")) | set(kb_sync.list_remote(s3, bucket, "verified/"))
        checks.add("stale: S3 object moved verified/ -> quarantine/",
                   f"quarantine/{DOMAIN}/{RULE_FACTS[0]}.md" in keys
                   and f"verified/{DOMAIN}/{RULE_FACTS[0]}.md" not in keys)
        ok, top = wait_for(lambda: ((t := retrieve_ids(runtime, kb_id, QUERY1)) and RULE_FACTS[0] not in t, t))
        report["steps"]["retrieve_after_stale"] = top
        checks.add("stale: Retrieve no longer returns BR-1", ok, top)

        # 7. code restored, review still marked stale: held
        ws["limit"].write_bytes(original_code)
        held = kb_stale.stale(ws["facts"], bucket, kb_id, ds_id, repo=ws["repo"], client_factory=factory,
                              region=region)
        report["steps"]["code_restored"] = {"held": held["held"], "released": held["released"]}
        checks.add("restore code only: facts held until re-review (no silent re-trust)",
                   RULE_FACTS[0] in held["held"] and not held["released"], held["held"])

        # 8. review restored (stands in for a re-review): released
        ws["record"].write_bytes(original_record)
        rel = kb_stale.stale(ws["facts"], bucket, kb_id, ds_id, repo=ws["repo"], client_factory=factory,
                             region=region)
        report["steps"]["review_restored"] = {"released": rel["released"], "clean": rel["clean"],
                                              "ingestion": rel["ingestion"]}
        checks.add("restore review: facts released", RULE_FACTS[0] in rel["released"], rel["released"])
        ok, top = wait_for(lambda: (RULE_FACTS[0] in (t := retrieve_ids(runtime, kb_id, QUERY1)), t))
        report["steps"]["retrieve_after_restore"] = top
        checks.add("restore: Retrieve returns BR-1 in the top 3 again", ok, top)
        audit = kb_sync.audit(ws["facts"], bucket, client_factory=factory, region=region)
        report["steps"]["audit"] = {k: audit["summary"][k] for k in ("objects", "documents", "by_status",
                                                                     "all_allowed", "violations")}
        checks.add("audit: every object allowed and identical to the export", audit["summary"]["all_allowed"],
                   audit["summary"]["violations"][:5])

        # 9. optional purge
        if purge:
            n = purge_bucket(s3, bucket)
            ing = kb_sync.run_ingestion(factory("bedrock-agent"), kb_id, ds_id)
            left = s3.list_object_versions(Bucket=bucket, MaxKeys=1)
            empty = not (left.get("Versions") or left.get("DeleteMarkers"))
            report["steps"]["purge"] = {"deleted_versions": n, "ingestion": ing, "bucket_empty": empty}
            checks.add("purge: bucket empty (all versions) and index re-synced",
                       empty and ing["status"] == "COMPLETE", {"deleted_versions": n})
    except Exception as exc:  # report the failure, then exit 1
        checks.add("smoke test ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        report["finished_at"] = _now()
        report["checks"] = checks.rows
        report["ok"] = checks.ok
        report["workspace"] = str(base) if keep else None
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
    passed = sum(1 for r in checks.rows if r["ok"])
    print(f"smoke test: {'PASS' if checks.ok else 'FAIL'} ({passed}/{len(checks.rows)} checks); report {out}")
    return 0 if checks.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state", type=Path, required=True, help="kb_state.json")
    parser.add_argument("--out", type=Path, default=Path("smoke-report.json"))
    parser.add_argument("--keep-workspace", action="store_true", help="keep the temp workspace for inspection")
    parser.add_argument("--purge-bucket", metavar="BUCKET",
                        help="after the test, delete every object version in this bucket (must match the state)")
    parser.add_argument("--allow-nonempty-kb", action="store_true",
                        help="run even if verified/ or quarantine/ holds other facts; the sync step deletes them "
                             "(test stacks only)")
    args = parser.parse_args(argv)
    return run(args.state, args.out, args.keep_workspace, args.purge_bucket, args.allow_nonempty_kb)


if __name__ == "__main__":
    sys.exit(main())
