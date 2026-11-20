"""Stale loop for the knowledge base (``bcp kb stale``).

Every exported fact carries ``source_hash``: for a rule fact the SHA-256 of the code lines its review
cited (:func:`bcp.kb.source_hash`), for a derived fact (feature map, glossary entry) the SHA-256 over the
source hashes of the rule facts it is built from. ``bcp kb stale`` recomputes those hashes from the code
as it is now and acts on the difference:

* a fact in ``verified/`` whose hash changed (or whose cited range is gone, or whose review entry is no
  longer exportable) is **quarantined**: both files (``.md`` and ``.md.metadata.json``) move from
  ``verified/`` to ``quarantine/`` in S3 and in the local export folder; a derived fact follows the rule
  facts it is built from;
* the review-record entry of a stale rule fact is **marked**: in ``bcp.review/3`` terms it becomes an
  ``unreviewed`` entry (the reviewed entry moves to ``history``, as for any re-review) with a ``stale``
  object that says what changed. ``bcp kb export`` and the review queue then treat it as needing review;
* a fact in ``quarantine/`` is **released** back to ``verified/`` only when its hash matches the code
  again *and* its review entry is exportable and carries no stale mark (that is, someone re-reviewed it
  with ``bcp review record``, or the record was restored). Code that merely changed back does not
  release a fact whose review was marked stale;
* when anything moved, ``StartIngestionJob`` resyncs the knowledge base (the data source indexes
  ``verified/`` only, so quarantined documents are deleted from the vector index).

``--source-root`` and ``--path-map REL=FILE`` read the code from somewhere else than the repository
(for example a scratch copy of one file), so the loop can be exercised without touching the source.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from bcp.errors import BcpInputError
from bcp.kb import (
    AUDIT_FILE,
    METADATA_SUFFIX,
    QUARANTINE_DIR,
    VERIFIED_DIR,
    cited_lines,
    range_evidence,
    record_filter,
    source_hash,
)
from bcp.kb_sync import (
    DEFAULT_PREFIX,
    DEFAULT_TIMEOUT_S,
    ClientFactory,
    KbSyncError,
    _now,
    _write_json,
    check_prefix,
    default_client_factory,
    list_remote,
    run_ingestion,
)
from bcp.review import normalize_entry, put_entry, validate_record
from bcp.textio import atomic_write_text, dump_json, read_text, sha256_text, to_posix

log = logging.getLogger(__name__)

SCHEMA_STALE = "bcp.kb-stale/1"
DEFAULT_QUARANTINE_PREFIX = f"{QUARANTINE_DIR}/"
MARK_KEY = "stale"
MARKED_BY = "bcp kb stale"
MARK_NOTE = "Stale: the code lines this review cited changed; the fact is quarantined and needs re-review."


# --------------------------------------------------------------------------------------- inputs


def parse_path_map(items: Sequence[str] | None) -> dict[str, Path]:
    """``REL=FILE`` pairs: read repository file ``REL`` from ``FILE`` instead."""
    out: dict[str, Path] = {}
    for item in items or []:
        rel, sep, target = item.partition("=")
        if not sep or not rel.strip() or not target.strip():
            raise BcpInputError(f"--path-map must be REL=FILE, got {item!r}")
        path = Path(target.strip())
        if not path.is_file():
            raise BcpInputError(f"--path-map target not found: {path}")
        out[to_posix(rel.strip()).removeprefix("./")] = path
    return out


def _lines(path: Path) -> list[str]:
    lines = read_text(path).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


@dataclass
class FactFile:
    """One fact as found in ``verified/`` or ``quarantine/`` of the export folder."""

    fact_id: str
    fact_type: str
    domain: str
    location: str
    doc: Path
    rel: str
    source_hash: str
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def meta(self) -> Path:
        return self.doc.with_name(self.doc.name + METADATA_SUFFIX)


def scan_facts(facts_dir: Path) -> dict[str, FactFile]:
    """Every fact document under ``verified/`` and ``quarantine/`` (a fact in both is an error)."""
    out: dict[str, FactFile] = {}
    for location in (VERIFIED_DIR, QUARANTINE_DIR):
        root = Path(facts_dir) / location
        if not root.is_dir():
            continue
        for doc in sorted(p for p in root.rglob("*.md") if p.is_file()):
            meta = doc.with_name(doc.name + METADATA_SUFFIX)
            if not meta.is_file():
                raise KbSyncError(f"{doc}: no {doc.name}{METADATA_SUFFIX}")
            attrs = json.loads(read_text(meta)).get("metadataAttributes") or {}
            fact_id = str(attrs.get("fact_id") or doc.stem)
            if fact_id in out:
                raise KbSyncError(f"fact {fact_id} is in both {out[fact_id].location}/ and {location}/")
            out[fact_id] = FactFile(fact_id, str(attrs.get("fact_type") or "rule"), str(attrs.get("domain") or ""),
                                    location, doc, to_posix(doc.relative_to(root)),
                                    str(attrs.get("source_hash") or ""), attrs)
    if not out:
        raise BcpInputError(f"no facts under {facts_dir}/{VERIFIED_DIR} or {QUARANTINE_DIR}; run `bcp kb export`")
    return out


def _load_export(facts_dir: Path) -> dict[str, Any]:
    path = Path(facts_dir) / AUDIT_FILE
    if not path.is_file():
        raise BcpInputError(f"{path} not found; run `bcp kb export` first")
    return json.loads(read_text(path))


# --------------------------------------------------------------------------------------- review record


def is_marked(entry: Mapping[str, Any] | None) -> bool:
    """True when ``bcp kb stale`` marked this review-record entry and nobody re-reviewed it since."""
    return isinstance(entry, Mapping) and isinstance(entry.get(MARK_KEY), Mapping)


def reviewed_basis(entry: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The entry whose evidence the fact was exported from (the last history item when marked)."""
    if not is_marked(entry):
        return entry
    history = list((entry or {}).get("history") or [])
    return history[-1] if history else None


def stale_mark(entry: Mapping[str, Any], fact: FactFile, current: str | None, changed: Sequence[str],
               at: str) -> dict[str, Any]:
    """The ``bcp.review/3`` entry that replaces a reviewed entry whose cited code changed."""
    return {
        "verdict": "unreviewed",
        "body_sha256": entry.get("body_sha256"),
        "reviewer": None,
        "reviewer_type": None,
        "reviewed_at": None,
        "note": MARK_NOTE,
        "correction": None,
        "suspected_defect": False,
        "evidence": [],
        MARK_KEY: {
            "status": "needs-re-review",
            "marked_at": at,
            "marked_by": MARKED_BY,
            "fact_id": fact.fact_id,
            "previous_verdict": entry.get("verdict"),
            "reason": "cited-code-changed",
            "expected_source_hash": fact.source_hash,
            "current_source_hash": current,
            "changed_ranges": list(changed),
        },
    }


# --------------------------------------------------------------------------------------- check


@dataclass
class Check:
    """Hash and review state of one fact now."""

    fact: FactFile
    claim: str | None = None
    current_hash: str | None = None
    code_ok: bool = False
    review_ok: bool = False
    marked: bool = False
    changed: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def check_facts(facts: Mapping[str, FactFile], export: Mapping[str, Any], record: Mapping[str, Any],
                source_root: Path, path_map: Mapping[str, Path] | None = None) -> dict[str, Check]:
    """Recompute every fact's source hash from the code under ``source_root`` (``path_map`` wins)."""
    rules = {r["fact_id"]: r for r in export.get("rules", []) if r.get("fact_id")}
    derived = {d["fact_id"]: list(d.get("from") or []) for d in export.get("derived", []) if d.get("fact_id")}
    claims = record.get("claims") or {}
    cache: dict[str, list[str] | None] = {rel: _lines(p) for rel, p in (path_map or {}).items()}
    out: dict[str, Check] = {}
    for fact_id, fact in facts.items():
        if fact.fact_type != "rule":
            continue
        chk = Check(fact)
        row = rules.get(fact_id)
        if row is None:
            chk.reasons.append("rule fact not listed in export-audit.json")
            out[fact_id] = chk
            continue
        chk.claim = str(row.get("claim"))
        entry = claims.get(chk.claim)
        chk.marked = is_marked(entry)
        basis = reviewed_basis(entry)
        evidence = range_evidence(dict(basis)) if isinstance(basis, Mapping) else []
        if not evidence:
            chk.reasons.append("no line-range evidence in the review record")
        else:
            items, chk.changed = cited_lines(Path(source_root), evidence, cache)
            if not chk.changed:
                chk.current_hash = source_hash(items)
            chk.code_ok = not chk.changed and chk.current_hash == fact.source_hash
            if chk.changed:
                chk.reasons.append("cited lines changed or missing: " + ", ".join(chk.changed[:5]))
            elif not chk.code_ok:
                chk.reasons.append("source hash differs from the exported fact")
        if chk.marked:
            chk.reasons.append("review entry marked stale; needs re-review")
        else:
            ok, why, _ = record_filter(normalize_entry(copy.deepcopy(entry)) if entry else None,
                                       (entry or {}).get("verdict"))
            chk.review_ok = ok
            if not ok:
                chk.reasons.append(f"review entry not exportable ({why})")
        out[fact_id] = chk
    for fact_id, fact in facts.items():
        if fact.fact_type == "rule":
            continue
        chk = Check(fact, sources=derived.get(fact_id, []))
        if fact_id not in derived:
            chk.reasons.append("derived fact not listed in export-audit.json")
            out[fact_id] = chk
            continue
        parts = [out.get(s) for s in chk.sources]
        if all(p is not None and p.current_hash for p in parts):
            chk.current_hash = sha256_text("".join(p.current_hash + "\n" for p in parts))  # type: ignore[union-attr]
        bad_code = [s for s, p in zip(chk.sources, parts) if p is None or not p.code_ok]
        bad_review = [s for s, p in zip(chk.sources, parts) if p is None or not p.review_ok]
        chk.code_ok = not bad_code and chk.current_hash == fact.source_hash
        chk.review_ok = not bad_review
        if bad_code:
            chk.reasons.append("derived from stale facts: " + ", ".join(bad_code))
        elif not chk.code_ok:
            chk.reasons.append("source hash differs from the exported fact")
        if bad_review:
            chk.reasons.append("derived from facts that need review: " + ", ".join(bad_review))
        out[fact_id] = chk
    return out


# --------------------------------------------------------------------------------------- moves


def _keys(prefix: str, rel: str) -> list[str]:
    return [prefix + rel, prefix + rel + METADATA_SUFFIX]


def _move_s3(s3: Any, bucket: str, src_keys: Sequence[str], dst_keys: Sequence[str],
             remote: Mapping[str, Any]) -> list[str]:
    """Copy each source object to its destination, then delete the sources; returns keys not in S3."""
    missing = [k for k in src_keys if k not in remote]
    present = [(s, d) for s, d in zip(src_keys, dst_keys) if s in remote]
    for src, dst in present:
        s3.copy_object(Bucket=bucket, Key=dst, CopySource={"Bucket": bucket, "Key": src}, MetadataDirective="COPY")
    if present:
        resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": s} for s, _ in present], "Quiet": True})
        errors = resp.get("Errors") or []
        if errors:
            raise KbSyncError(f"delete after copy failed: {errors[:3]}")
    return missing


def _move_local(fact: FactFile, facts_dir: Path, to: str) -> Path:
    dest = Path(facts_dir) / to / fact.rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = fact.meta
    os.replace(fact.doc, dest)
    os.replace(meta, dest.with_name(dest.name + METADATA_SUFFIX))
    old_dir = fact.doc.parent
    if old_dir.is_dir() and not any(old_dir.iterdir()):
        old_dir.rmdir()
    return dest


# --------------------------------------------------------------------------------------- run


def stale(facts_dir: Path, bucket: str | None, kb_id: str | None, data_source_id: str | None,
          repo: Path | None = None, source_root: Path | None = None, path_map: Mapping[str, Path] | None = None,
          review_dir: Path | None = None, prefix: str = DEFAULT_PREFIX,
          quarantine_prefix: str = DEFAULT_QUARANTINE_PREFIX, client_factory: ClientFactory | None = None,
          region: str = "us-east-1", mark: bool = True, release: bool = True, ingest: bool = True,
          dry_run: bool = False, timeout_s: float = DEFAULT_TIMEOUT_S, report: Path | None = None,
          sleep: Any = None) -> dict[str, Any]:
    """``bcp kb stale``: quarantine facts whose cited code changed, release re-reviewed ones, resync.

    Raises:
        BcpInputError: missing export, review record, bucket or KB ids.
        KbSyncError: an S3 move or the ingestion job failed.
    """
    facts_dir = Path(facts_dir)
    prefix, quarantine_prefix = check_prefix(prefix), check_prefix(quarantine_prefix)
    if prefix == quarantine_prefix:
        raise BcpInputError("--quarantine-prefix must differ from --prefix")
    export = _load_export(facts_dir)
    repo = Path(repo) if repo else Path(str(export.get("repo") or ""))
    if not str(repo) or not repo.is_dir():
        raise BcpInputError(f"repository not found: {repo} (give --repo)")
    source_root = Path(source_root) if source_root else repo
    if not source_root.is_dir():
        raise BcpInputError(f"--source-root not found: {source_root}")
    review_dir = Path(review_dir) if review_dir else repo / ".kiro" / "bre-review"
    record_path = review_dir / "review-record.json"
    if not record_path.is_file():
        raise BcpInputError(f"review record not found: {record_path}")
    raw = json.loads(read_text(record_path))
    validate_record(copy.deepcopy(raw), record_path.name)
    if not dry_run and not bucket:
        raise BcpInputError("--bucket (or --state) is required")
    if ingest and not dry_run and not (kb_id and data_source_id):
        raise BcpInputError("--kb-id and --data-source-id are required unless --no-ingest")

    facts = scan_facts(facts_dir)
    checks = check_facts(facts, export, raw, source_root, path_map)
    to_quarantine = sorted(f for f, c in checks.items()
                           if c.fact.location == VERIFIED_DIR and not (c.code_ok and c.review_ok))
    to_release = sorted(f for f, c in checks.items()
                        if c.fact.location == QUARANTINE_DIR and c.code_ok and c.review_ok) if release else []
    held = sorted(f for f, c in checks.items() if c.fact.location == QUARANTINE_DIR and f not in to_release)
    to_mark = [f for f in to_quarantine
               if checks[f].fact.fact_type == "rule" and not checks[f].code_ok and not checks[f].marked
               and checks[f].claim and isinstance((raw.get("claims") or {}).get(checks[f].claim), dict)] if mark else []
    log.info("kb stale: %d facts checked; quarantine %d, release %d, held %d, mark %d", len(checks),
             len(to_quarantine), len(to_release), len(held), len(to_mark))

    marked: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    missing_in_s3: list[str] = []
    ingestion: dict[str, Any] | None = None
    if not dry_run:
        at = _now()
        if to_mark:
            for fact_id in to_mark:
                chk = checks[fact_id]
                entry = raw["claims"][chk.claim]
                put_entry(raw, str(chk.claim), stale_mark(entry, chk.fact, chk.current_hash, chk.changed, at))
                marked.append({"claim": chk.claim, "fact_id": fact_id, "previous_verdict": entry.get("verdict")})
            validate_record(copy.deepcopy(raw), record_path.name)
            atomic_write_text(record_path, dump_json(raw))
        if to_quarantine or to_release:
            factory = client_factory or default_client_factory(region)
            s3 = factory("s3")
            remote = {**list_remote(s3, str(bucket), prefix), **list_remote(s3, str(bucket), quarantine_prefix)}
            for action, ids, src_p, dst_p, dst_dir in (
                    ("quarantined", to_quarantine, prefix, quarantine_prefix, QUARANTINE_DIR),
                    ("released", to_release, quarantine_prefix, prefix, VERIFIED_DIR)):
                for fact_id in ids:
                    fact = checks[fact_id].fact
                    src, dst = _keys(src_p, fact.rel), _keys(dst_p, fact.rel)
                    missing_in_s3 += _move_s3(s3, str(bucket), src, dst, remote)
                    _move_local(fact, facts_dir, dst_dir)
                    moved.append({"fact_id": fact_id, "action": action, "from": src, "to": dst})
            if ingest:
                ingestion = run_ingestion(factory("bedrock-agent"), str(kb_id), str(data_source_id), timeout_s,
                                          sleep=sleep)

    rows = []
    for fact_id, chk in sorted(checks.items()):
        action = ("quarantine" if fact_id in to_quarantine else "release" if fact_id in to_release
                  else "held" if fact_id in held else "clean")
        rows.append({"fact_id": fact_id, "fact_type": chk.fact.fact_type, "domain": chk.fact.domain,
                     "claim": chk.claim, "location_before": chk.fact.location, "action": action,
                     "code_ok": chk.code_ok, "review_ok": chk.review_ok, "marked_stale": chk.marked,
                     "expected_source_hash": chk.fact.source_hash, "current_source_hash": chk.current_hash,
                     "changed_ranges": chk.changed, "reasons": chk.reasons, "sources": chk.sources})
    result = {
        "schema": SCHEMA_STALE, "generated_at": _now(), "dry_run": dry_run, "facts_dir": str(facts_dir),
        "repo": to_posix(repo), "source_root": to_posix(source_root),
        "path_map": {k: to_posix(v) for k, v in (path_map or {}).items()},
        "review_record": to_posix(record_path), "bucket": bucket, "prefix": prefix,
        "quarantine_prefix": quarantine_prefix, "kb_id": kb_id, "data_source_id": data_source_id,
        "counts": {"checked": len(rows), "clean": sum(1 for r in rows if r["action"] == "clean"),
                   "quarantined": len(to_quarantine), "released": len(to_release), "held": len(held),
                   "marked": len(marked) if not dry_run else len(to_mark)},
        "clean": not to_quarantine and not held,
        "quarantined": to_quarantine, "released": to_release, "held": held,
        "marked": marked if not dry_run else [{"claim": checks[f].claim, "fact_id": f} for f in to_mark],
        "moved": moved, "missing_in_s3": missing_in_s3, "ingestion": ingestion,
        "facts": [r for r in rows if r["action"] != "clean"],
    }
    _write_json(report, result)
    if ingestion and ingestion["status"] != "COMPLETE":
        raise KbSyncError(f"ingestion job {ingestion['job_id']} ended {ingestion['status']}: "
                          f"{ingestion['failure_reasons'][:3]}")
    return result
