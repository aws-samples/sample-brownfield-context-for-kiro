"""Sync exported facts to the knowledge-base bucket, ingest them, audit the bucket and check retrieval.

``bcp kb sync`` mirrors ``<facts>/verified/`` (written by ``bcp kb export``) to ``s3://<bucket>/verified/``:

* only ``*.md`` and ``*.md.metadata.json`` are synced; any other file under ``verified/`` stops the sync
  before any upload;
* every ``.md`` needs its ``.md.metadata.json`` and a status of ``verified``, ``needs-edit`` (exported only
  with an approved replacement) or ``derived`` (built from those), and must be confirmed against
  ``export-audit.json`` (a rule exported by the review filter with the same verdict, or a derived fact
  built from exported rules; a missing audit file refuses too); anything else stops the sync before any
  upload;
* change detection compares the local MD5 with the S3 ETag (single-part ``PutObject`` with SSE-S3 or no
  encryption: the ETag is the MD5 of the body). Unchanged objects are not uploaded, so a second run
  uploads nothing. Each upload also carries ``x-amz-meta-sha256``;
* every object under the prefix that the export no longer contains is deleted (documents and their
  ``.metadata.json`` alike, and anything else that is not part of the export);
* then ``StartIngestionJob`` on the data source, poll until it ends, report the statistics. With
  deletion policy ``DELETE`` the ingestion also drops the vectors of deleted documents.

``bcp kb audit`` lists every object under the prefix with the status read back from its
``.metadata.json`` in S3 and cross-checks rule facts against ``export-audit.json``.

``bcp kb check-retrieval`` sends one Retrieve query per fact (built from its title) and reports whether
the fact comes back in the top *k* (hit rate, rank, latency).

All AWS access goes through a ``client_factory(service_name)`` so tests run with fakes and no AWS.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bcp.errors import BcpError, BcpInputError
from bcp.kb import AUDIT_FILE, METADATA_SUFFIX, VERIFIED_DIR
from bcp.textio import atomic_write_text, dump_json, read_text, to_posix

log = logging.getLogger(__name__)

SCHEMA_SYNC = "bcp.kb-sync/1"
SCHEMA_AUDIT = "bcp.kb-audit/1"
SCHEMA_RETRIEVAL = "bcp.kb-retrieval-check/1"
DEFAULT_PREFIX = f"{VERIFIED_DIR}/"
ALLOWED_STATUSES = ("verified", "needs-edit", "derived")
TERMINAL_JOB_STATES = {"COMPLETE", "FAILED", "STOPPED"}
DELETE_BATCH = 1000
POLL_SECONDS = 5.0
DEFAULT_TIMEOUT_S = 900.0
START_ATTEMPTS = 6
TITLE_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+[a-z]?:\s*")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")

ClientFactory = Callable[[str], Any]


class KbSyncError(BcpError):
    """Sync, ingestion, audit or retrieval check failed in a way the user can act on."""


# --------------------------------------------------------------------------------------- clients


def default_client_factory(region: str) -> ClientFactory:
    """boto3 clients for ``region`` (instance metadata lookup disabled; one client per service)."""
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BcpInputError("boto3 is required for kb sync/audit/check-retrieval (pip install boto3)") from exc
    cache: dict[str, Any] = {}

    def factory(name: str) -> Any:
        if name not in cache:
            cache[name] = boto3.client(name, region_name=region)
        return cache[name]

    return factory


def load_state(path: Path) -> dict[str, Any]:
    """Read the ``kb_state.json`` written by provisioning (bucket, kb_id, data_source_id, region...)."""
    try:
        data = json.loads(read_text(Path(path)))
    except FileNotFoundError as exc:
        raise BcpInputError(f"state file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BcpInputError(f"state file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BcpInputError(f"state file {path} must hold a JSON object")
    return data


def check_prefix(prefix: str) -> str:
    """A non-empty relative key prefix ending in ``/`` (deletes never reach outside it)."""
    if not prefix or prefix.startswith("/") or not prefix.endswith("/") or ".." in prefix:
        raise BcpInputError(f"prefix must be a relative key prefix ending in '/', e.g. 'verified/': {prefix!r}")
    return prefix


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path | None, data: Mapping[str, Any]) -> None:
    if path is not None:
        atomic_write_text(Path(path), dump_json(data))


# --------------------------------------------------------------------------------------- local facts


@dataclass(frozen=True)
class LocalObject:
    """One file of the export, mapped to its object key."""

    key: str
    path: Path
    md5: str
    sha256: str
    size: int


@dataclass(frozen=True)
class LocalFact:
    """One fact document with its parsed metadata attributes."""

    fact_id: str
    domain: str
    title: str
    key: str
    attributes: dict[str, Any]


def _verified_root(facts_dir: Path) -> Path:
    root = Path(facts_dir) / VERIFIED_DIR
    if not root.is_dir():
        raise BcpInputError(f"no {VERIFIED_DIR}/ folder under {facts_dir}; run `bcp kb export` first")
    return root


def _is_fact_file(name: str) -> bool:
    return name.endswith(".md") or name.endswith(".md" + METADATA_SUFFIX)


def local_objects(facts_dir: Path, prefix: str = DEFAULT_PREFIX, strict: bool = True) -> dict[str, LocalObject]:
    """Every fact file under ``<facts>/verified/`` keyed by ``<prefix><domain>/<file>`` (temp files skipped).

    Only ``*.md`` and ``*.md.metadata.json`` are fact files. With ``strict`` (the default, used by sync)
    any other file under ``verified/`` stops the whole run before anything is uploaded; without it (audit)
    such files are left out.

    Raises:
        KbSyncError: ``strict`` and a file that is not a fact document or its metadata is present.
    """
    root = _verified_root(facts_dir)
    out: dict[str, LocalObject] = {}
    foreign: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if not _is_fact_file(path.name):
            if not path.name.startswith("."):  # dot files (atomic-write temp files) are never uploaded
                foreign.append(to_posix(path.relative_to(root)))
            continue
        data = path.read_bytes()
        key = prefix + to_posix(path.relative_to(root))
        # MD5 only to compare with the S3 ETag (change detection); integrity uses the SHA-256 next to it
        etag = hashlib.md5(data, usedforsecurity=False).hexdigest()
        out[key] = LocalObject(key, path, etag, hashlib.sha256(data).hexdigest(), len(data))
    if foreign and strict:
        raise KbSyncError(f"refusing to sync: {len(foreign)} file(s) under {VERIFIED_DIR}/ are not fact documents "
                          "(*.md) or their metadata (*.md.metadata.json); only `bcp kb export` output may be "
                          "synced:\n  " + "\n  ".join(foreign[:20]))
    return out


def check_export(facts_dir: Path, facts: Sequence[LocalFact]) -> None:
    """Every local fact must be one the review filter exported, as recorded in ``export-audit.json``.

    A rule fact must be listed as exported with the same verdict as its metadata status, a derived fact
    must be listed as derived from exported facts, each fact_id must occur once, and the document must be
    named ``<fact_id>.md`` (no aliases of an exported fact).

    Raises:
        KbSyncError: ``export-audit.json`` is missing or unreadable, or any fact fails the check.
    """
    try:
        export = _export_index(facts_dir)
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        raise KbSyncError(f"refusing to sync: {Path(facts_dir) / AUDIT_FILE} is unreadable ({exc})") from exc
    if export is None:
        raise KbSyncError(f"refusing to sync: {Path(facts_dir) / AUDIT_FILE} not found; the export cannot be "
                          "confirmed against the review filter (run `bcp kb export`)")
    problems: list[str] = []
    seen: dict[str, str] = {}
    for fact in facts:
        if fact.fact_id in seen:
            problems.append(f"{fact.key}: fact {fact.fact_id} also in {seen[fact.fact_id]}")
            continue
        seen[fact.fact_id] = fact.key
        if fact.key.rsplit("/", 1)[-1] != f"{fact.fact_id}.md":
            problems.append(f"{fact.key}: document name does not match fact_id {fact.fact_id!r}")
            continue
        ok, why = _review_check({**fact.attributes, "fact_id": fact.fact_id}, export)
        if not ok:
            problems.append(f"{fact.key}: {why}")
    if problems:
        raise KbSyncError("refusing to sync facts the review filter did not export:\n  " + "\n  ".join(problems))


def _title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def local_facts(facts_dir: Path, prefix: str = DEFAULT_PREFIX) -> list[LocalFact]:
    """Parse every fact document and its metadata; fail on a missing pair, bad JSON or a disallowed status.

    Raises:
        KbSyncError: a document or metadata file is missing, unreadable, or not a reviewed status.
    """
    root = _verified_root(facts_dir)
    facts: list[LocalFact] = []
    problems: list[str] = []
    docs = sorted(p for p in root.rglob("*.md") if p.is_file())
    for meta in sorted(root.rglob(f"*{METADATA_SUFFIX}")):
        if not meta.with_name(meta.name[: -len(METADATA_SUFFIX)]).is_file():
            problems.append(f"{to_posix(meta.relative_to(root))}: metadata without a document")
    for doc in docs:
        rel = to_posix(doc.relative_to(root))
        meta = doc.with_name(doc.name + METADATA_SUFFIX)
        if not meta.is_file():
            problems.append(f"{rel}: no {doc.name}{METADATA_SUFFIX}")
            continue
        try:
            attrs = json.loads(read_text(meta)).get("metadataAttributes") or {}
        except (json.JSONDecodeError, AttributeError) as exc:
            problems.append(f"{rel}{METADATA_SUFFIX}: invalid metadata JSON ({exc})")
            continue
        status = attrs.get("status")
        if status not in ALLOWED_STATUSES:
            problems.append(f"{rel}: status {status!r} is not one of {', '.join(ALLOWED_STATUSES)}")
            continue
        fact_id = str(attrs.get("fact_id") or doc.stem)
        facts.append(LocalFact(fact_id, str(attrs.get("domain") or doc.parent.name), _title(read_text(doc)),
                               prefix + rel, attrs))
    if problems:
        raise KbSyncError("refusing to sync unreviewed or incomplete facts:\n  " + "\n  ".join(problems))
    if not facts:
        raise KbSyncError(f"no fact documents under {root}")
    return facts


# --------------------------------------------------------------------------------------- S3 helpers


def list_remote(s3: Any, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
    """Every current object under ``prefix``: key -> {etag, size, last_modified}."""
    out: dict[str, dict[str, Any]] = {}
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []) or []:
            last = obj.get("LastModified")
            out[obj["Key"]] = {"etag": str(obj.get("ETag", "")).strip('"').lower(), "size": int(obj.get("Size", 0)),
                               "last_modified": last.isoformat() if hasattr(last, "isoformat") else last}
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")
        if not token:
            return out


def _content_type(key: str) -> str:
    return "application/json" if key.endswith(".json") else "text/markdown; charset=utf-8"


def _delete_keys(s3: Any, bucket: str, keys: Sequence[str]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for i in range(0, len(keys), DELETE_BATCH):
        batch = keys[i:i + DELETE_BATCH]
        resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
        errors += [{"key": e.get("Key"), "code": e.get("Code"), "message": e.get("Message")}
                   for e in resp.get("Errors", []) or []]
    return errors


# --------------------------------------------------------------------------------------- ingestion


def _error_code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def run_ingestion(agent: Any, kb_id: str, data_source_id: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                  poll_s: float = POLL_SECONDS, sleep: Callable[[float], None] | None = None,
                  clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Start an ingestion job (retrying while another job runs), wait for it to end, return its summary."""
    sleep = sleep or time.sleep
    t0 = clock()
    job: dict[str, Any] | None = None
    for attempt in range(1, START_ATTEMPTS + 1):
        try:
            job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=data_source_id)["ingestionJob"]
            break
        except Exception as exc:  # botocore ClientError; the class is not imported so tests need no botocore
            if _error_code(exc) not in {"ConflictException", "ThrottlingException"} or attempt == START_ATTEMPTS:
                raise
            log.info("ingestion start: %s, retry %d in %.0f s", _error_code(exc), attempt, poll_s * 2)
            sleep(poll_s * 2)
    assert job is not None
    job_id = job["ingestionJobId"]
    log.info("ingestion job %s started", job_id)
    while job.get("status") not in TERMINAL_JOB_STATES:
        if clock() - t0 > timeout_s:
            raise KbSyncError(f"ingestion job {job_id} still {job.get('status')} after {timeout_s:.0f} s")
        sleep(poll_s)
        job = agent.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=data_source_id,
                                      ingestionJobId=job_id)["ingestionJob"]
    return {"job_id": job_id, "status": job.get("status"), "statistics": job.get("statistics") or {},
            "failure_reasons": job.get("failureReasons") or [], "seconds": round(clock() - t0, 1)}


# --------------------------------------------------------------------------------------- sync


def sync(facts_dir: Path, bucket: str, kb_id: str | None, data_source_id: str | None,
         prefix: str = DEFAULT_PREFIX, client_factory: ClientFactory | None = None, region: str = "us-east-1",
         ingest: bool = True, dry_run: bool = False, timeout_s: float = DEFAULT_TIMEOUT_S,
         report: Path | None = None, sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
    """``bcp kb sync``: upload changed facts, delete objects no longer exported, ingest, report.

    Raises:
        KbSyncError: a fact is unreviewed or incomplete, a delete failed, or ingestion did not finish.
    """
    prefix = check_prefix(prefix)
    if ingest and not dry_run and not (kb_id and data_source_id):
        raise BcpInputError("--kb-id and --data-source-id are required unless --no-ingest")
    local = local_objects(facts_dir, prefix)  # refuses non-fact files before any AWS call
    facts = local_facts(facts_dir, prefix)
    check_export(facts_dir, facts)
    factory = client_factory or default_client_factory(region)
    s3 = factory("s3")
    t0 = time.monotonic()
    remote = list_remote(s3, bucket, prefix)
    upload = [o for k, o in sorted(local.items())
              if not (k in remote and MD5_RE.match(remote[k]["etag"]) and remote[k]["etag"] == o.md5)]
    delete = sorted(k for k in remote if k not in local)
    log.info("kb sync: %d local objects, %d remote; upload %d, delete %d", len(local), len(remote),
             len(upload), len(delete))
    uploaded: list[dict[str, Any]] = []
    if not dry_run:
        for obj in upload:
            s3.put_object(Bucket=bucket, Key=obj.key, Body=obj.path.read_bytes(), ContentType=_content_type(obj.key),
                          Metadata={"sha256": obj.sha256})
            uploaded.append({"key": obj.key, "bytes": obj.size, "md5": obj.md5,
                             "reason": "changed" if obj.key in remote else "new"})
        errors = _delete_keys(s3, bucket, delete) if delete else []
        if errors:
            raise KbSyncError(f"delete failed for {len(errors)} object(s): {errors[:5]}")
    else:
        uploaded = [{"key": o.key, "bytes": o.size, "md5": o.md5, "reason": "changed" if o.key in remote else "new"}
                    for o in upload]
    s3_seconds = round(time.monotonic() - t0, 1)
    ingestion: dict[str, Any] | None = None
    if ingest and not dry_run:
        ingestion = run_ingestion(factory("bedrock-agent"), str(kb_id), str(data_source_id), timeout_s, sleep=sleep)
    by_status: dict[str, int] = {}
    for fact in facts:
        by_status[str(fact.attributes.get("status"))] = by_status.get(str(fact.attributes.get("status")), 0) + 1
    result = {
        "schema": SCHEMA_SYNC, "generated_at": _now(), "dry_run": dry_run, "bucket": bucket, "prefix": prefix,
        "kb_id": kb_id, "data_source_id": data_source_id, "facts_dir": str(facts_dir),
        "change_detection": "local MD5 vs S3 ETag (single-part PutObject, SSE-S3); sha256 in x-amz-meta-sha256",
        "local": {"facts": len(facts), "objects": len(local), "by_status": dict(sorted(by_status.items()))},
        "remote_before": len(remote),
        "uploaded": uploaded, "deleted": delete, "unchanged": len(local) - len(upload),
        "counts": {"uploaded": len(uploaded), "deleted": len(delete), "unchanged": len(local) - len(upload)},
        "s3_seconds": s3_seconds, "ingestion": ingestion,
    }
    _write_json(report, result)
    if ingestion and ingestion["status"] != "COMPLETE":
        raise KbSyncError(f"ingestion job {ingestion['job_id']} ended {ingestion['status']}: "
                          f"{ingestion['failure_reasons'][:3]}")
    return result


# --------------------------------------------------------------------------------------- audit


def _export_index(facts_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]] | None:
    path = Path(facts_dir) / AUDIT_FILE
    if not path.is_file():
        return None
    data = json.loads(read_text(path))
    rules = {r["fact_id"]: r for r in data.get("rules", []) if r.get("fact_id")}
    derived = {d["fact_id"]: list(d.get("from") or []) for d in data.get("derived", []) if d.get("fact_id")}
    return rules, derived


def _review_check(attrs: Mapping[str, Any], export: tuple[dict[str, Any], dict[str, list[str]]] | None) -> tuple[bool, str]:
    status, fact_id = attrs.get("status"), attrs.get("fact_id")
    if status not in ALLOWED_STATUSES:
        return False, f"status {status!r} is not reviewed"
    if export is None:
        return status == "verified", "no export-audit.json to confirm the review decision"
    rules, derived = export
    if status == "derived":
        sources = derived.get(str(fact_id))
        if sources is None:
            return False, "derived fact not listed in export-audit.json"
        bad = [s for s in sources if not (rules.get(s, {}).get("exported")
                                          and rules[s].get("verdict") in ("verified", "needs-edit"))]
        return (not bad), (f"derived from {len(sources)} exported verified/approved facts" if not bad
                           else f"derived from facts that were not exported: {bad}")
    rule = rules.get(str(fact_id))
    if not rule or not rule.get("exported"):
        return False, "rule fact not exported by the review filter"
    if rule.get("verdict") != status:
        return False, f"review verdict {rule.get('verdict')!r} differs from metadata status {status!r}"
    return True, ("verified by review" if status == "verified"
                  else "needs-edit with an approved replacement (exported by the review filter)")


def _get_json(s3: Any, bucket: str, key: str) -> dict[str, Any]:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body.decode("utf-8-sig"))


def audit(facts_dir: Path, bucket: str, prefix: str = DEFAULT_PREFIX, client_factory: ClientFactory | None = None,
          region: str = "us-east-1", out: Path | None = None, workers: int = 8,
          vector_bucket: str | None = None, index: str | None = None) -> dict[str, Any]:
    """``bcp kb audit``: every object under ``prefix`` with the status read back from S3.

    Optionally (``vector_bucket`` + ``index``) also lists the vector index and counts vectors per source
    document, to show what the knowledge base itself holds.
    """
    prefix = check_prefix(prefix)
    factory = client_factory or default_client_factory(region)
    s3 = factory("s3")
    remote = list_remote(s3, bucket, prefix)
    local = local_objects(facts_dir, prefix, strict=False) if (Path(facts_dir) / VERIFIED_DIR).is_dir() else {}
    export = _export_index(facts_dir)
    meta_keys = [k for k in remote if k.endswith(METADATA_SUFFIX)]

    def fetch(key: str) -> tuple[str, dict[str, Any] | str]:
        try:
            return key, (_get_json(s3, bucket, key).get("metadataAttributes") or {})
        except Exception as exc:  # recorded per object; the audit continues
            return key, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        metas = dict(pool.map(fetch, sorted(meta_keys)))
    objects: list[dict[str, Any]] = []
    violations: list[str] = []
    by_status: dict[str, int] = {}
    for key in sorted(remote):
        info = remote[key]
        entry: dict[str, Any] = {"key": key, "bytes": info["size"], "etag": info["etag"],
                                 "last_modified": info["last_modified"],
                                 "matches_export": key in local and local[key].md5 == info["etag"]}
        if key.endswith(METADATA_SUFFIX):
            entry["kind"] = "metadata"
            entry["document_present"] = key[: -len(METADATA_SUFFIX)] in remote
        elif key.endswith(".md"):
            entry["kind"] = "document"
            attrs = metas.get(key + METADATA_SUFFIX)
            if isinstance(attrs, dict):
                ok, why = _review_check(attrs, export)
                entry.update({"fact_id": attrs.get("fact_id"), "status": attrs.get("status"),
                              "fact_type": attrs.get("fact_type"), "domain": attrs.get("domain"),
                              "reviewer_type": attrs.get("reviewer_type"), "commit": attrs.get("commit"),
                              "allowed": ok, "check": why})
                by_status[str(attrs.get("status"))] = by_status.get(str(attrs.get("status")), 0) + 1
            else:
                entry.update({"allowed": False, "check": attrs or "no metadata object in S3"})
        else:
            entry.update({"kind": "other", "allowed": False, "check": "not a fact document or its metadata"})
        if entry.get("kind") == "metadata" and not entry["document_present"]:
            violations.append(f"{key}: metadata without its document")
        if entry.get("kind") != "metadata" and not entry.get("allowed"):
            violations.append(f"{key}: {entry.get('check')}")
        if not entry["matches_export"]:
            violations.append(f"{key}: not identical to the current export")
        objects.append(entry)
    missing = sorted(k for k in local if k not in remote)
    violations += [f"{k}: in the export but not in S3" for k in missing]
    docs = [o for o in objects if o["kind"] == "document"]
    summary: dict[str, Any] = {
        "objects": len(objects), "documents": len(docs),
        "metadata_objects": sum(1 for o in objects if o["kind"] == "metadata"),
        "by_status": dict(sorted(by_status.items())),
        "by_fact_type": _count(o.get("fact_type") for o in docs),
        "all_allowed": not violations, "violations": violations,
        "keys_outside_export": sorted(k for k in remote if k not in local),
        "smoke_test_objects": sorted(k for k in remote if k.startswith(f"{prefix}test/")),
        "export_audit_used": export is not None,
    }
    result: dict[str, Any] = {"schema": SCHEMA_AUDIT, "generated_at": _now(), "bucket": bucket, "prefix": prefix,
                              "allowed_statuses": list(ALLOWED_STATUSES), "summary": summary, "objects": objects}
    if vector_bucket and index:
        result["vector_index"] = vector_index_summary(factory("s3vectors"), vector_bucket, index, bucket)
    _write_json(out, result)
    return result


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))


def vector_index_summary(s3v: Any, vector_bucket: str, index: str, bucket: str) -> dict[str, Any]:
    """Count vectors in the index and the source documents they come from (best effort, never raises)."""
    try:
        sources: dict[str, int] = {}
        total = 0
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"vectorBucketName": vector_bucket, "indexName": index, "maxResults": 500,
                                      "returnMetadata": True}
            if token:
                kwargs["nextToken"] = token
            page = s3v.list_vectors(**kwargs)
            for vec in page.get("vectors", []) or []:
                total += 1
                meta = vec.get("metadata") or {}
                uri = str(meta.get("x-amz-bedrock-kb-source-uri") or meta.get("fact_id") or "?")
                sources[uri] = sources.get(uri, 0) + 1
            token = page.get("nextToken")
            if not token:
                break
        base = f"s3://{bucket}/"
        keys = sorted(s[len(base):] if s.startswith(base) else s for s in sources)
        return {"vectors": total, "source_documents": len(sources),
                "smoke_test_sources": [k for k in keys if "/test/" in k or k.startswith("TEST-")],
                "sources": keys}
    except Exception as exc:  # the audit of the bucket stands on its own
        return {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------------------- retrieval check


def query_for(title: str) -> str:
    """Query text from a fact title: the title without a leading rule id such as ``BR-10:``."""
    return TITLE_ID_RE.sub("", title).strip() or title


def _result_id(res: Mapping[str, Any]) -> str:
    meta = res.get("metadata") or {}
    if meta.get("fact_id"):
        return str(meta["fact_id"])
    uri = str(((res.get("location") or {}).get("s3Location") or {}).get("uri") or "")
    name = uri.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name or "?"


def _pct(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))])


def check_retrieval(facts_dir: Path, kb_id: str, client_factory: ClientFactory | None = None,
                    region: str = "us-east-1", top_k: int = 3, out: Path | None = None, workers: int = 4,
                    retries: int = 1) -> dict[str, Any]:
    """``bcp kb check-retrieval``: one Retrieve per fact (query from its title); hit = fact in top ``top_k``."""
    if top_k < 1:
        raise BcpInputError("--top-k must be >= 1")
    facts = local_facts(facts_dir)
    factory = client_factory or default_client_factory(region)
    runtime = factory("bedrock-agent-runtime")
    known = {f.fact_id for f in facts}

    def one(fact: LocalFact) -> dict[str, Any]:
        query = query_for(fact.title)
        error = None
        for attempt in range(retries + 1):
            t0 = time.perf_counter()
            try:
                resp = runtime.retrieve(knowledgeBaseId=kb_id, retrievalQuery={"text": query},
                                        retrievalConfiguration={"vectorSearchConfiguration": {
                                            "numberOfResults": top_k}})
                ms = round((time.perf_counter() - t0) * 1000)
                results = resp.get("retrievalResults", [])[:top_k]
                ids = [_result_id(r) for r in results]
                rank = ids.index(fact.fact_id) + 1 if fact.fact_id in ids else None
                return {"fact_id": fact.fact_id, "fact_type": fact.attributes.get("fact_type"),
                        "domain": fact.domain, "status": fact.attributes.get("status"), "query": query,
                        "top": ids, "scores": [round(float(r.get("score", 0.0)), 4) for r in results],
                        "hit": rank is not None, "rank": rank, "latency_ms": ms, "attempts": attempt + 1}
            except Exception as exc:  # recorded; one retry
                error = f"{type(exc).__name__}: {exc}"
        return {"fact_id": fact.fact_id, "fact_type": fact.attributes.get("fact_type"), "domain": fact.domain,
                "status": fact.attributes.get("status"), "query": query, "top": [], "scores": [], "hit": False,
                "rank": None, "latency_ms": None, "attempts": retries + 1, "error": error}

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = list(pool.map(one, facts))
    wall = round(time.perf_counter() - t0, 1)
    hits = sum(1 for r in rows if r["hit"])
    lat = [float(r["latency_ms"]) for r in rows if r["latency_ms"] is not None]
    by_type: dict[str, dict[str, Any]] = {}
    for r in rows:
        t = by_type.setdefault(str(r["fact_type"]), {"facts": 0, "hits": 0})
        t["facts"] += 1
        t["hits"] += int(r["hit"])
    for t in by_type.values():
        t["hit_rate"] = round(t["hits"] / t["facts"], 4)
    foreign = sorted({i for r in rows for i in r["top"] if i not in known})
    summary = {
        "facts": len(rows), "top_k": top_k, "hits": hits, "hit_rate": round(hits / len(rows), 4) if rows else 0.0,
        "hit_at_1": sum(1 for r in rows if r["rank"] == 1), "errors": sum(1 for r in rows if r.get("error")),
        "by_fact_type": dict(sorted(by_type.items())),
        "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "max": max(lat) if lat else 0.0,
                       "mean": round(statistics.fmean(lat), 1) if lat else 0.0},
        "foreign_ids_in_results": foreign, "workers": workers, "wall_seconds": wall,
        "misses": [r["fact_id"] for r in rows if not r["hit"]],
    }
    result = {"schema": SCHEMA_RETRIEVAL, "generated_at": _now(), "kb_id": kb_id,
              "query_rule": "fact title without a leading rule id (e.g. 'BR-10: ')", "summary": summary,
              "facts": rows}
    _write_json(out, result)
    return result
