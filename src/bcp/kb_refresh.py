"""``bcp kb refresh``: the deterministic sync pipeline (no AI).

Given the repository at a new commit versus the commit each fact was reviewed at, for every exported
fact ``refresh``:

* re-anchors a fact whose cited lines **moved but whose content is unchanged** (:mod:`bcp.kb_reanchor`):
  the review record's line ranges and the fact files are rewritten and the hashes recomputed, so the
  fact stays verified instead of being quarantined;
* quarantines a fact whose cited lines **changed or whose file is gone** and adds it to the re-review
  queue -- delegated to :func:`bcp.kb_stale.stale`, which moves the objects to the ``quarantine/`` prefix
  (out of the indexed ``verified/`` prefix), marks the review record and releases re-reviewed facts;
* lists a changed or new **source file that no fact cites** as a coverage gap.

Optionally it rebuilds the code graph first (``bcp graph build``, static, no AI). After the moves it
re-ingests ``verified/`` once (via :func:`bcp.kb_sync.sync`, which also uploads the re-anchored facts).
``refresh`` writes ``refresh-queue.md``/``.json`` (the re-review queue and the re-anchors) and
``coverage-gap.md``/``.json`` under the facts folder.

``bcp kb stale`` stays the lower-level primitive; ``refresh`` orchestrates re-anchor + stale + coverage
gap + a single re-ingest.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

from bcp import kb_stale, kb_sync
from bcp.config import BcpConfig
from bcp.errors import BcpInputError
from bcp.kb import (
    VERIFIED_DIR,
    cited_lines,
    content_hash,
    evidence_specs,
    fit_metadata,
    metadata_text,
    source_hash,
)
from bcp.kb_reanchor import reanchor_evidence
from bcp.kb_stale import FactFile, check_facts, reviewed_basis, scan_facts
from bcp.kb_stale import _lines as _raw_lines
from bcp.review import validate_record
from bcp.sourceindex import SourceIndex
from bcp.textio import atomic_write_text, dump_json, read_text, safe_join, sha256_text, to_posix

log = logging.getLogger(__name__)

SCHEMA_REFRESH = "bcp.kb-refresh/1"
QUEUE_JSON = "refresh-queue.json"
QUEUE_MD = "refresh-queue.md"
GAP_JSON = "coverage-gap.json"
GAP_MD = "coverage-gap.md"


# --------------------------------------------------------------------------------------- code reading


def _rstrip_lines(path: Path) -> list[str] | None:
    if not path.is_file():
        return None
    lines = [ln.rstrip() for ln in read_text(path).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _lines_for(rel: str, source_root: Path, path_map: Mapping[str, Path] | None,
               cache: dict[str, list[str] | None]) -> list[str] | None:
    """Current lines of a cited file (trailing whitespace stripped); ``path_map`` wins over source root."""
    if rel in cache:
        return cache[rel]
    target = (path_map or {}).get(rel)
    path = Path(target) if target else safe_join(source_root, rel)
    cache[rel] = _rstrip_lines(path)
    return cache[rel]


# --------------------------------------------------------------------------------------- re-anchor


def _rewrite_md(text: str, specs: Sequence[str], src_hash: str, cont_hash: str) -> str:
    """Rewrite the ``- Evidence:``, ``- Source hash`` and ``- Content hash`` lines of a fact document."""
    out: list[str] = []
    saw_content = False
    for line in text.split("\n"):
        if line.startswith("- Evidence:"):
            out.append(f"- Evidence: {'; '.join(specs) or 'none'}")
        elif line.startswith("- Source hash ("):
            prefix = line.split("):", 1)[0]
            out.append(f"{prefix}): {src_hash}")
            if cont_hash and not saw_content and "- Content hash (" not in text:
                out.append("- Content hash (position-independent SHA-256 of the cited code text): " + cont_hash)
                saw_content = True
        elif line.startswith("- Content hash ("):
            prefix = line.split("):", 1)[0]
            out.append(f"{prefix}): {cont_hash}")
            saw_content = True
        else:
            out.append(line)
    return "\n".join(out)


def _rewrite_fact_files(fact: FactFile, specs: Sequence[str], src_hash: str, cont_hash: str) -> None:
    attrs = dict(fact.attributes)
    attrs["evidence"] = list(specs)
    attrs["source_hash"] = src_hash
    if cont_hash:
        attrs["content_hash"] = cont_hash
    for key in ("truncated", "evidence_total", "components_total"):
        attrs.pop(key, None)
    fitted, _ = fit_metadata(attrs)
    atomic_write_text(fact.meta, metadata_text(fitted))
    atomic_write_text(fact.doc, _rewrite_md(read_text(fact.doc), specs, src_hash, cont_hash))


def apply_reanchors(record: dict[str, Any], facts: Mapping[str, FactFile], checks: Mapping[str, kb_stale.Check],
                    export: Mapping[str, Any], source_root: Path, path_map: Mapping[str, Path] | None,
                    dry_run: bool) -> list[dict[str, Any]]:
    """Re-anchor every verified rule fact whose cited code moved but is unchanged. Rewrites the review
    record and the fact files (rule facts, then the derived facts built from them) unless ``dry_run``."""
    claims = record.get("claims") or {}
    rstrip_cache: dict[str, list[str] | None] = {}
    raw_cache: dict[str, list[str] | None] = {rel: _raw_lines(p) for rel, p in (path_map or {}).items()}
    reanchored: list[dict[str, Any]] = []
    new_hashes: dict[str, tuple[str, str]] = {}  # fact_id -> (source_hash, content_hash)
    for fact_id in sorted(facts):
        fact, chk = facts[fact_id], checks.get(fact_id)
        if fact.fact_type != "rule" or fact.location != VERIFIED_DIR or chk is None:
            continue
        if chk.code_ok or chk.marked or not chk.claim:
            continue
        basis = reviewed_basis(claims.get(chk.claim))
        if not isinstance(basis, Mapping):
            continue
        evidence = list(basis.get("evidence") or [])
        ranges = [ev for ev in evidence if ev.get("kind") == "range"]
        lines_by_path = {to_posix(ev["path"]): _lines_for(to_posix(ev["path"]), source_root, path_map, rstrip_cache)
                         for ev in ranges}
        new_ranges = reanchor_evidence(ranges, lines_by_path)
        if new_ranges is None:
            continue
        # recompute the hashes from the raw cited lines (exactly what bcp kb stale reads back)
        probe = [{k: v for k, v in ev.items() if k != "quote"} for ev in new_ranges]
        items, changed = cited_lines(source_root, probe, dict(raw_cache))
        if changed:
            continue
        for ev, (_spec, text) in zip(new_ranges, items):
            ev["quote"] = text
        new_src, new_cont = source_hash(items), content_hash(items)
        specs = evidence_specs(new_ranges)
        reanchored.append({"fact_id": fact_id, "claim": chk.claim, "domain": fact.domain,
                           "old_ranges": [f"{to_posix(e['path'])}#L{e['start']}-L{e['end']}" for e in ranges],
                           "new_ranges": list(specs)})
        new_hashes[fact_id] = (new_src, new_cont)
        if dry_run:
            continue
        non_range = [ev for ev in evidence if ev.get("kind") != "range"]
        basis["evidence"] = list(new_ranges) + non_range  # type: ignore[index]
        _rewrite_fact_files(fact, specs, new_src, new_cont)
    if reanchored and not dry_run:
        _reanchor_derived(facts, export, new_hashes)
    return reanchored


def _reanchor_derived(facts: Mapping[str, FactFile], export: Mapping[str, Any],
                      new_hashes: Mapping[str, tuple[str, str]]) -> None:
    """Keep each derived fact (feature map, glossary) consistent with its re-anchored rule sources by
    recomputing its source and content hashes; its evidence line ranges are refreshed by the next
    ``bcp kb export`` (they are informational and not part of the staleness check)."""
    src_of = {fid: new_hashes.get(fid, (f.source_hash, str(f.attributes.get("content_hash") or "")))
              for fid, f in facts.items() if f.fact_type == "rule"}
    for row in export.get("derived", []):
        fid = row.get("fact_id")
        sources = list(row.get("from") or [])
        if fid not in facts or facts[fid].location != VERIFIED_DIR:
            continue
        if not any(s in new_hashes for s in sources) or not all(s in src_of for s in sources):
            continue
        new_src = sha256_text("".join(src_of[s][0] + "\n" for s in sources))
        conts = [src_of[s][1] for s in sources]
        new_cont = sha256_text("".join(c + "\n" for c in conts)) if all(conts) else ""
        fact = facts[fid]
        specs = list(fact.attributes.get("evidence") or [])
        _rewrite_fact_files(fact, specs, new_src, new_cont)


# --------------------------------------------------------------------------------------- coverage gap


def _excluded(rel: str, exclude_dirs: Sequence[str]) -> bool:
    parts = set(to_posix(rel).split("/"))
    return any(d in parts for d in exclude_dirs)


def coverage_gap(facts: Mapping[str, FactFile], source_root: Path, config: BcpConfig,
                 changed_files: Sequence[str] | None = None) -> list[str]:
    """Source files no exported fact cites. With ``changed_files`` (e.g. a CI diff) only those files are
    considered; otherwise every source file under ``source_root`` is scanned."""
    cited = {to_posix(spec).split("#", 1)[0] for f in facts.values()
             for spec in (f.attributes.get("evidence") or [])}
    exts = tuple(config.source_extensions)
    if changed_files is not None:
        candidates = {to_posix(c) for c in changed_files
                      if to_posix(c).endswith(exts) and not _excluded(c, config.exclude_dirs)}
    else:
        candidates = set(SourceIndex(source_root, config.source_extensions, config.exclude_dirs).paths)
    return sorted(candidates - cited)


# --------------------------------------------------------------------------------------- queue files


def _write_queue(facts_dir: Path, reanchored: Sequence[Mapping[str, Any]], stale_result: Mapping[str, Any],
                 gaps: Sequence[str]) -> None:
    quarantined = [r for r in stale_result.get("facts", []) if r.get("action") == "quarantine"]
    data = {
        "schema": SCHEMA_REFRESH,
        "generated_at": stale_result.get("generated_at"),
        "reanchored": list(reanchored),
        "quarantined": [{"fact_id": r["fact_id"], "domain": r.get("domain"), "claim": r.get("claim"),
                         "changed_ranges": r.get("changed_ranges"), "reasons": r.get("reasons")}
                        for r in quarantined],
        "coverage_gap": list(gaps),
    }
    atomic_write_text(facts_dir / QUEUE_JSON, dump_json(data))
    md = ["# KB refresh re-review queue", "",
          f"- Re-anchored (line ranges moved, content unchanged, kept verified): {len(reanchored)}",
          f"- Quarantined (cited code changed; re-review needed): {len(data['quarantined'])}",
          f"- Coverage gap (changed/new source files no fact cites): {len(gaps)}", ""]
    if data["quarantined"]:
        md += ["## Quarantined facts to re-review", ""]
        for r in data["quarantined"]:
            md.append(f"- `{r['fact_id']}` ({r['domain']}, claim {r['claim']}): "
                      + ("; ".join(r["reasons"] or []) or "cited code changed")
                      + (f" [ranges: {', '.join(r['changed_ranges'])}]" if r.get("changed_ranges") else ""))
        md.append("")
    if reanchored:
        md += ["## Re-anchored facts (automatic, no re-review needed)", ""]
        md += [f"- `{r['fact_id']}` ({r['domain']}): {', '.join(r['old_ranges'])} -> {', '.join(r['new_ranges'])}"
               for r in reanchored]
        md.append("")
    atomic_write_text(facts_dir / QUEUE_MD, "\n".join(md))
    gap_json = {"schema": SCHEMA_REFRESH, "generated_at": stale_result.get("generated_at"), "coverage_gap": list(gaps)}
    atomic_write_text(facts_dir / GAP_JSON, dump_json(gap_json))
    gap_md = ["# KB coverage gap", "",
              "Changed or new source files that no exported fact cites. Extract and review rules for these, "
              "or confirm they need no business-rule coverage.", ""]
    gap_md += [f"- `{g}`" for g in gaps] or ["(none)"]
    gap_md.append("")
    atomic_write_text(facts_dir / GAP_MD, "\n".join(gap_md))


# --------------------------------------------------------------------------------------- run


def refresh(facts_dir: Path, config: BcpConfig, bucket: str | None, kb_id: str | None,
            data_source_id: str | None, repo: Path | None = None, source_root: Path | None = None,
            path_map: Mapping[str, Path] | None = None, review_dir: Path | None = None,
            prefix: str = kb_sync.DEFAULT_PREFIX, quarantine_prefix: str = kb_stale.DEFAULT_QUARANTINE_PREFIX,
            graph_domains: Sequence[str] | None = None, context: Path | None = None,
            changed_files: Sequence[str] | None = None, client_factory: kb_sync.ClientFactory | None = None,
            region: str = "us-east-1", release: bool = True, ingest: bool = True, dry_run: bool = False,
            timeout_s: float = kb_sync.DEFAULT_TIMEOUT_S, report: Path | None = None,
            sleep: Any = None) -> dict[str, Any]:
    """Re-anchor, quarantine (via ``bcp kb stale``), list coverage gaps and re-ingest once.

    Raises:
        BcpInputError: missing export, review record, bucket or KB ids.
        KbSyncError: an S3 move, an upload or the ingestion job failed.
    """
    facts_dir = Path(facts_dir)
    export = kb_stale._load_export(facts_dir)
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
    if not dry_run and not bucket:
        raise BcpInputError("--bucket (or --state) is required")
    if ingest and not dry_run and not (kb_id and data_source_id):
        raise BcpInputError("--kb-id and --data-source-id are required unless --no-ingest")

    graph_summary: dict[str, Any] | None = None
    if graph_domains and not dry_run:
        from bcp.graph import run_build

        graph_summary = run_build(source_root, config, list(graph_domains), context=context)
        log.info("kb refresh: rebuilt graph %s (%d nodes)", str(graph_summary["commit"])[:12], graph_summary["nodes"])

    raw = json.loads(read_text(record_path))
    validate_record(copy.deepcopy(raw), record_path.name)
    facts = scan_facts(facts_dir)
    checks = check_facts(facts, export, raw, source_root, path_map)
    reanchored = apply_reanchors(raw, facts, checks, export, source_root, path_map, dry_run)
    if reanchored and not dry_run:
        validate_record(copy.deepcopy(raw), record_path.name)
        atomic_write_text(record_path, dump_json(raw))

    # quarantine / mark / release the facts whose code truly changed (re-anchored ones are now clean).
    stale_result = kb_stale.stale(
        facts_dir, bucket, kb_id, data_source_id, repo=repo, source_root=source_root, path_map=path_map,
        review_dir=review_dir, prefix=prefix, quarantine_prefix=quarantine_prefix,
        client_factory=client_factory, region=region, release=release, ingest=False, dry_run=dry_run,
        timeout_s=timeout_s)

    # one re-ingest at the end: upload the re-anchored (and released) facts, delete removed, ingest once.
    sync_result: dict[str, Any] | None = None
    if not dry_run:
        sync_result = kb_sync.sync(facts_dir, str(bucket), kb_id, data_source_id, prefix,
                                   client_factory=client_factory, region=region, ingest=ingest, dry_run=False,
                                   timeout_s=timeout_s, sleep=sleep)

    gaps = coverage_gap(facts, source_root, config, changed_files)
    _write_queue(facts_dir, reanchored, stale_result, gaps)

    result = {
        "schema": SCHEMA_REFRESH, "generated_at": stale_result["generated_at"], "dry_run": dry_run,
        "facts_dir": str(facts_dir), "repo": to_posix(repo), "source_root": to_posix(source_root),
        "bucket": bucket, "prefix": kb_sync.check_prefix(prefix), "kb_id": kb_id,
        "data_source_id": data_source_id,
        "graph": {"rebuilt": graph_summary is not None, "commit": (graph_summary or {}).get("commit")},
        "counts": {
            "checked": stale_result["counts"]["checked"] + len(reanchored),
            "reanchored": len(reanchored),
            "quarantined": stale_result["counts"]["quarantined"],
            "released": stale_result["counts"]["released"],
            "held": stale_result["counts"]["held"],
            "marked": stale_result["counts"]["marked"],
            "coverage_gap": len(gaps),
        },
        "reanchored": reanchored,
        "quarantined": stale_result["quarantined"],
        "released": stale_result["released"],
        "held": stale_result["held"],
        "coverage_gap": gaps,
        "stale": stale_result,
        "ingestion": (sync_result or {}).get("ingestion"),
        "queue_files": [str(facts_dir / QUEUE_MD), str(facts_dir / GAP_MD)],
    }
    if report:
        atomic_write_text(Path(report), dump_json(result))
    return result
