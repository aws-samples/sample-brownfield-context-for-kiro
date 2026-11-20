"""Step 2 tooling: claim worksheets, verdict records with captured evidence, reports, sign-off."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bcp.bre import BreLayout
from bcp.config import BcpConfig
from bcp.errors import BcpInputError, ReviewRecordError
from bcp.investigate import investigate, why_line
from bcp.manifest import load_manifest
from bcp.sourceindex import METHOD_MISSING, SourceIndex, resolve_claim
from bcp.textio import (
    atomic_write_text,
    dump_json,
    read_text,
    safe_join,
    sha256_file,
    to_posix,
)

log = logging.getLogger(__name__)

SCHEMA_CLAIMS = "bcp.claims/1"
SCHEMA_REVIEW = "bcp.review/3"
SCHEMAS_REVIEW_ACCEPTED = ("bcp.review/2", SCHEMA_REVIEW)
SCHEMA_SIGNOFF = "bcp.signoff/1"
# verified: main behaviour checked against the code. needs-edit: main behaviour right, a full corrected
# rule text is supplied. incorrect: the core is wrong; the body is removed. needs-sme: an SME must decide.
VERDICTS = ("verified", "needs-edit", "incorrect", "needs-sme", "unreviewed")
RECORD_VERDICTS = ("verified", "needs-edit", "incorrect", "needs-sme")
STANDIN = "kiro (AI stand-in for SME)"
STANDIN_LABEL = "AI stand-in for SME (Kiro)"
REVIEWER_TYPES = ("kiro", "sme", STANDIN)
EVIDENCE_KINDS = ("range", "search", "sme")
PASS_OUTCOMES = ("upheld", "counterexample-found")
DECISIONS = ("approved", "approved-with-exceptions", "rejected")
CLAIM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}/[A-Za-z0-9_.-]+$")
MAX_RANGE_LINES = 40
MAX_SCOPE_FILES = 200
MAX_HITS = 20
CHECKLIST_ITEMS: tuple[tuple[str, str], ...] = (
    ("all_claims_have_verdict", "Every BR claim in this domain has a verdict other than unreviewed."),
    ("evidence_checked", "I opened the evidence for each verified and incorrect claim and agree with it."),
    ("incorrect_claims_have_corrections", "Every incorrect or needs-edit claim states what the code actually does."),
    ("needs_sme_items_listed", "Every needs-sme claim has a question an SME can answer."),
    (
        "suspected_defects_raised_with_owner",
        "Each suspected defect is logged with an owner outside this review.",
    ),
    (
        "requirements_source_consulted",
        "Where intent was unclear, I checked the requirements or asked the product owner.",
    ),
)
CHECKLIST_KEYS = tuple(key for key, _ in CHECKLIST_ITEMS)
CHECKLIST_LINE_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*([A-Za-z0-9_]+)\s*:")
KIRO_ONLY_NOTICE = "> This review was performed by Kiro. It is not SME approval."


def now_iso() -> str:
    """Current UTC time, ISO 8601 with ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp (``Z`` allowed); None when absent or invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def git_head(repo: Path) -> str | None:
    """Read-only ``git rev-parse HEAD``; None when git is missing or the folder is not a repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = proc.stdout.strip()
    return head if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,64}", head) else None


def _load_json(path: Path, error: type[Exception]) -> Any:
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise error(f"{path.name}: invalid JSON: {exc}") from exc


def worksheet_path(out: Path, slug: str) -> Path:
    """``<out>/claims-<slug>.json``."""
    return Path(out) / f"claims-{slug}.json"


def load_worksheets(out: Path) -> dict[str, dict[str, Any]]:
    """All worksheets in ``out`` keyed by domain slug (invalid ones raise BcpInputError)."""
    sheets: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(out).glob("claims-*.json")):
        data = _load_json(path, BcpInputError)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_CLAIMS:
            raise BcpInputError(f"{path.name}: not a {SCHEMA_CLAIMS} worksheet")
        sheets[str(data.get("domain"))] = data
    return sheets


# --------------------------------------------------------------------------- prepare


def prepare(
    bre: Path,
    repo: Path,
    out: Path,
    domains: list[str] | None,
    config: BcpConfig,
    refresh_baseline: bool = False,
) -> list[Path]:
    """Write ``claims-<slug>.json`` worksheets and create/extend ``review-record.json``."""
    bre, repo, out = Path(bre), Path(repo), Path(out)
    manifest = load_manifest(bre)
    layout = BreLayout(bre)
    selected = domains or manifest.slugs
    for slug in selected:
        if manifest.domain(slug) is None:
            raise BcpInputError(f"unknown domain slug: {slug}")
    index = SourceIndex(repo, config.source_extensions, config.exclude_dirs)
    manifest_paths = [p for d in manifest.domains for p in d.source_paths]
    head = git_head(repo)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    record_path = out / "review-record.json"
    record = (
        load_review_record(record_path)
        if record_path.is_file()
        else {"schema": SCHEMA_REVIEW, "repo_head": head, "bre_root": bre.name, "claims": {}}
    )
    for slug in selected:
        domain = manifest.domain(slug)
        assert domain is not None
        parsed, bre_file = layout.load_claims(slug, config)
        if domain.rule_count is not None and domain.rule_count != len(parsed):
            log.info("domain %s: parsed %d rules, manifest rule_count %d", slug, len(parsed), domain.rule_count)
        sheet_path = worksheet_path(out, slug)
        old: dict[str, Any] = {}
        old_sheet: dict[str, Any] | None = None
        if sheet_path.is_file() and not refresh_baseline:
            old_sheet = _load_json(sheet_path, BcpInputError)
            if isinstance(old_sheet, dict) and old_sheet.get("schema") == SCHEMA_CLAIMS:
                old = {c["id"]: c for c in old_sheet.get("claims", []) if isinstance(c, dict) and "id" in c}
            else:
                raise BcpInputError(f"{sheet_path.name}: not a {SCHEMA_CLAIMS} worksheet")
        bre_dir = bre_file.rsplit("/", 1)[0] if bre_file else None
        entries: list[dict[str, Any]] = []
        stale: list[str] = []
        for claim in parsed:
            claim_id = f"{slug}/{claim.rule_id}"
            prior = old.get(claim_id)
            if prior is not None and prior.get("body_sha256") == claim.body_sha256:
                entries.append(prior)
                continue
            if prior is not None:
                stale.append(claim_id)
            res = resolve_claim(index, claim, domain.source_paths, manifest_paths, bre, bre_dir)
            for cand in res.candidates:
                if cand["status"] == METHOD_MISSING and not cand["bound"]:
                    log.info("%s: cited %s is not a method in %s", claim_id, cand["token"], cand["path"])
            investigations = [investigate(index, tok, r) for tok, r in res.missing]
            entry = {
                "id": claim_id,
                "rule_id": claim.rule_id,
                "title": claim.title,
                "body_sha256": claim.body_sha256,
                "citations": claim.citations,
                "candidates": res.candidates,
                "unresolved": res.unresolved,
                "bre_refs": res.bre_refs,
                "investigations": investigations,
            }
            if needs_sme_fallback(entry):
                entry["fallback_verdict"] = "needs-sme"
            entries.append(entry)
        keep_time = old_sheet is not None and not stale and len(old) == len(entries)
        sheet = {
            "schema": SCHEMA_CLAIMS,
            "domain": slug,
            "domain_name": domain.name,
            "bre_file": bre_file,
            "prepared_at": old_sheet.get("prepared_at") if keep_time and old_sheet else now_iso(),
            "repo_head": old_sheet.get("repo_head") if keep_time and old_sheet else head,
            "claims": entries,
            "stale": stale,
        }
        if stale:
            log.warning("domain %s: %d claim(s) changed since the last prepare: %s", slug, len(stale), ", ".join(stale))
        atomic_write_text(sheet_path, dump_json(sheet))
        written.append(sheet_path)
        for entry in entries:
            existing = record["claims"].get(entry["id"])
            replace = existing is None or existing.get("verdict") == "unreviewed" or (
                existing.get("auto") and existing.get("body_sha256") != entry["body_sha256"]
            )
            if not replace:
                continue
            if entry.get("fallback_verdict") == "needs-sme":
                record["claims"][entry["id"]] = _fallback_entry(entry)
            else:
                record["claims"][entry["id"]] = _unreviewed_entry(entry["body_sha256"])
    record["schema"] = SCHEMA_REVIEW
    atomic_write_text(record_path, dump_json(record))
    written.append(record_path)
    return written


AUTO_REVIEWER = "bcp citation check"


def needs_sme_fallback(entry: dict[str, Any]) -> bool:
    """True when a cited method/class/file could not be found (non-code tokens excluded)."""
    return any(inv.get("outcome") != "non-code" for inv in entry.get("investigations", []))


def _fallback_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Automatic needs-SME verdict: the tool never verifies or fails a claim on its own."""
    whys = [why_line(inv) for inv in entry.get("investigations", []) if inv.get("outcome") != "non-code"]
    return {
        "verdict": "needs-sme",
        "body_sha256": entry["body_sha256"],
        "reviewer": AUTO_REVIEWER,
        "reviewer_type": "kiro",
        "reviewed_at": now_iso(),
        "note": "A cited code location was not found; an SME must confirm or correct the citation. "
        + " | ".join(whys),
        "correction": None,
        "suspected_defect": False,
        "evidence": [],
        "auto": True,
    }


def _unreviewed_entry(body_sha: str) -> dict[str, Any]:
    return {
        "verdict": "unreviewed",
        "body_sha256": body_sha,
        "reviewer": None,
        "reviewer_type": None,
        "reviewed_at": None,
        "note": None,
        "correction": None,
        "suspected_defect": False,
        "evidence": [],
    }


# --------------------------------------------------------------------------- record

RANGE_RE = re.compile(r"^(?P<path>.+?):L?(?P<start>\d+)(?:-L?(?P<end>\d+))?$")


def capture_range(repo: Path, spec: str) -> dict[str, Any]:
    """Read ``path:start-end`` from disk and return range evidence (quote captured by the tool)."""
    match = RANGE_RE.match(spec.strip())
    if not match:
        raise BcpInputError(f"evidence must be path:start-end, got {spec!r}")
    rel = to_posix(match.group("path"))
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    full = safe_join(repo, rel)
    if not full.is_file():
        raise BcpInputError(f"evidence file not found in repo: {rel}")
    lines = read_text(full).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if start < 1 or end < start or end > len(lines):
        raise BcpInputError(f"evidence range {start}-{end} is outside {rel} ({len(lines)} lines)")
    if end - start + 1 > MAX_RANGE_LINES:
        raise BcpInputError(f"evidence range {rel}:{start}-{end} exceeds {MAX_RANGE_LINES} lines")
    return {
        "kind": "range",
        "path": rel,
        "start": start,
        "end": end,
        "quote": "\n".join(lines[start - 1:end]),
        "file_sha256": sha256_file(full),
    }


def capture_search(index: SourceIndex, pattern: str, scope: str) -> dict[str, Any]:
    """Run a regex over the files a scope matches and return search evidence."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise BcpInputError(f"invalid search regex {pattern!r}: {exc}") from exc
    scope = to_posix(scope)
    if scope.startswith("/") or ".." in scope.split("/"):
        raise BcpInputError(f"scope must be repo-relative: {scope!r}")
    files = index.glob_scope(scope)
    if not files:
        raise BcpInputError(f"scope matched no files: {scope}")
    if len(files) > MAX_SCOPE_FILES:
        raise BcpInputError(f"scope matched {len(files)} files; narrow the scope (max {MAX_SCOPE_FILES})")
    hits: list[dict[str, Any]] = []
    shas: dict[str, str] = {}
    for rel in sorted(files):
        shas[rel] = sha256_file(index.repo / rel)
        for number, line in enumerate(index.lines(rel), start=1):
            if regex.search(line):
                hits.append({"path": rel, "line": number, "text": line.strip()[:200]})
    hits.sort(key=lambda h: (h["path"], h["line"]))
    return {
        "kind": "search",
        "pattern": pattern,
        "scope": scope,
        "files_scanned": len(files),
        "files_sha256": shas,
        "hits": hits[:MAX_HITS],
        "hit_count": len(hits),
    }


def record(
    out: Path,
    claim_id: str,
    verdict: str,
    note: str,
    reviewer: str,
    reviewer_type: str,
    config: BcpConfig,
    evidence: list[str] | None = None,
    searches: list[tuple[str, str]] | None = None,
    correction: str | None = None,
    suspected_defect: bool = False,
    repo: Path | None = None,
    replacement: str | None = None,
    what_changed: str | None = None,
    superseded_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """Record one verdict with tool-captured evidence; the record file is replaced atomically.

    A replaced entry (anything but unreviewed) is kept in the new entry's ``history``, so the original
    review lives only in the record."""
    evidence = evidence or []
    searches = searches or []
    patterns = [p for p in (superseded_patterns or []) if p.strip()]
    if verdict not in RECORD_VERDICTS:
        raise BcpInputError(f"verdict must be one of {', '.join(RECORD_VERDICTS)}")
    if not note or not note.strip():
        raise BcpInputError("--note is required")
    if not reviewer or not reviewer.strip():
        raise BcpInputError("--reviewer is required")
    if reviewer_type not in REVIEWER_TYPES:
        raise BcpInputError(f"--reviewer-type must be one of: {', '.join(REVIEWER_TYPES)}")
    if (evidence or searches) and repo is None:
        raise BcpInputError("--repo is required when evidence is given")
    if verdict in ("verified", "needs-edit", "incorrect") and not (evidence or searches):
        raise BcpInputError(f"verdict {verdict} needs at least one --evidence or --evidence-search")
    if verdict == "incorrect" and not (correction and correction.strip()):
        raise BcpInputError("verdict incorrect needs --correction")
    if verdict == "needs-edit":
        if not (replacement and replacement.strip()):
            raise BcpInputError("verdict needs-edit needs the full corrected rule (--replacement-file or --replacement)")
        if not (what_changed and what_changed.strip()):
            raise BcpInputError("verdict needs-edit needs --what-changed (one line)")
    for pat in patterns:
        try:
            re.compile(pat)
        except re.error as exc:
            raise BcpInputError(f"invalid --superseded-pattern {pat!r}: {exc}") from exc
    if not CLAIM_ID_RE.match(claim_id):
        raise BcpInputError(f"claim id must be <slug>/<rule id>, got {claim_id!r}")
    out = Path(out)
    record_path = out / "review-record.json"
    if not record_path.is_file():
        raise BcpInputError(f"{record_path} not found; run review prepare first")
    slug = claim_id.split("/", 1)[0]
    sheet_path = worksheet_path(out, slug)
    if not sheet_path.is_file():
        raise BcpInputError(f"{sheet_path.name} not found; run review prepare for {slug}")
    sheet = _load_json(sheet_path, BcpInputError)
    claim = next((c for c in sheet.get("claims", []) if c.get("id") == claim_id), None)
    if claim is None:
        raise BcpInputError(f"claim {claim_id} is not in {sheet_path.name}")
    captured: list[dict[str, Any]] = [capture_range(Path(repo), spec) for spec in evidence] if repo else []
    if searches:
        index = SourceIndex(Path(repo), config.source_extensions, config.exclude_dirs)  # type: ignore[arg-type]
        captured += [capture_search(index, pat, scope) for pat, scope in searches]
    data = load_review_record(record_path)
    entry: dict[str, Any] = {
        "verdict": verdict,
        "body_sha256": claim["body_sha256"],
        "reviewer": reviewer.strip(),
        "reviewer_type": reviewer_type,
        "reviewed_at": now_iso(),
        "note": note.strip(),
        "correction": correction.strip() if correction else None,
        "suspected_defect": bool(suspected_defect),
        "evidence": captured,
    }
    if verdict == "needs-edit":
        entry["replacement"] = sanitize_replacement(str(replacement))
        entry["what_changed"] = " ".join(str(what_changed).split())
    if patterns:
        entry["superseded_patterns"] = patterns
    put_entry(data, claim_id, entry)
    atomic_write_text(record_path, dump_json(data))
    return entry


def put_entry(data: dict[str, Any], claim_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Store ``entry`` for ``claim_id``; a reviewed previous entry moves to ``entry['history']``."""
    prev = data["claims"].get(claim_id)
    history = list((prev or {}).get("history") or [])
    if prev and prev.get("verdict") not in (None, "unreviewed"):
        history.append({k: v for k, v in prev.items() if k != "history"})
    if history:
        entry["history"] = history
    data["claims"][claim_id] = entry
    data["schema"] = SCHEMA_REVIEW
    return entry


HEADING_LINE_RE = re.compile(r"^##\s+\S")


def sanitize_replacement(text: str) -> str:
    """Corrected rule body: no leading ``## BR-n`` heading; inner ``## `` lines demoted to ``### ``."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and HEADING_LINE_RE.match(lines[0]):
        lines.pop(0)
    out = ["#" + ln if ln.startswith("## ") else ln for ln in lines]
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(ln.rstrip() for ln in out).rstrip("\n")


def add_pass(
    out: Path,
    claim_id: str,
    outcome: str,
    note: str,
    reviewer: str,
    reviewer_type: str,
    evidence: list[str] | None = None,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Append a counterexample-pass result to a reviewed claim's ``passes`` list."""
    if outcome not in PASS_OUTCOMES:
        raise BcpInputError(f"--outcome must be one of {', '.join(PASS_OUTCOMES)}")
    if not note or not note.strip():
        raise BcpInputError("--note is required")
    if not reviewer or not reviewer.strip():
        raise BcpInputError("--reviewer is required")
    if reviewer_type not in REVIEWER_TYPES:
        raise BcpInputError(f"--reviewer-type must be one of: {', '.join(REVIEWER_TYPES)}")
    if evidence and repo is None:
        raise BcpInputError("--repo is required when evidence is given")
    record_path = Path(out) / "review-record.json"
    if not record_path.is_file():
        raise BcpInputError(f"{record_path} not found; run review prepare first")
    data = load_review_record(record_path)
    entry = data["claims"].get(claim_id)
    if not entry or entry.get("verdict") in (None, "unreviewed"):
        raise BcpInputError(f"claim {claim_id} has no review verdict; record one before a second pass")
    captured = [capture_range(Path(repo), spec) for spec in evidence or []] if repo else []
    result = {
        "pass": "counterexample",  # nosec B105 - review-pass schema field, not a credential
        "outcome": outcome,
        "note": note.strip(),
        "evidence": captured,
        "reviewer": reviewer.strip(),
        "reviewer_type": reviewer_type,
        "at": now_iso(),
    }
    entry.setdefault("passes", []).append(result)
    data["schema"] = SCHEMA_REVIEW
    atomic_write_text(record_path, dump_json(data))
    return result


def counterexample_found(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """The latest counterexample pass when its outcome is ``counterexample-found``, else None."""
    passes = [p for p in (entry or {}).get("passes") or [] if p.get("pass") == "counterexample"]
    if passes and passes[-1].get("outcome") == "counterexample-found":
        return passes[-1]
    return None


# --------------------------------------------------------------------------- validation / loading


def _require(cond: bool, where: str, message: str) -> None:
    if not cond:
        raise ReviewRecordError(f"{where}: {message}")


def normalize_entry(entry: Any) -> Any:
    """Backward compatibility for older records, in one place.

    An old ``incorrect`` entry that carries a non-empty full ``replacement`` becomes ``needs-edit``
    (its ``what_changed`` falls back to the correction or note). Every other entry is returned as is,
    so delta-only ``incorrect`` entries keep rendering as Known BRE corrections."""
    if not isinstance(entry, dict) or entry.get("verdict") != "incorrect":
        return entry
    replacement = entry.get("replacement")
    if not isinstance(replacement, str) or not replacement.strip():
        return entry
    out = dict(entry)
    out["verdict"] = "needs-edit"
    if not str(out.get("what_changed") or "").strip():
        out["what_changed"] = " ".join(str(out.get("correction") or out.get("note") or "corrected text supplied").split())
    return out


def _check_evidence(where: str, ev: Any, i: int) -> None:
    ev_where = f"{where}.evidence[{i}]"
    _require(isinstance(ev, dict), ev_where, "must be an object")
    kind = ev.get("kind")
    if kind == "range":
        for key in ("path", "start", "end", "quote"):
            _require(key in ev, f"{ev_where}.{key}", "required")
        _require(not os.path.isabs(str(ev["path"])), f"{ev_where}.path", "must be repo-relative")
    elif kind == "search":
        for key in ("pattern", "scope", "hit_count", "files_scanned"):
            _require(key in ev, f"{ev_where}.{key}", "required")
    elif kind == "sme":
        for key in ("reviewer", "date"):
            _require(bool(str(ev.get(key) or "").strip()), f"{ev_where}.{key}", "required")
    else:
        raise ReviewRecordError(f"{ev_where}.kind: unknown evidence kind {kind!r}")


def validate_record(data: Any, name: str = "review-record.json") -> dict[str, Any]:
    """Schema-check a review record (``bcp.review/2`` or ``/3``); errors name the JSON path.

    Entries are passed through :func:`normalize_entry` first."""
    _require(isinstance(data, dict), f"{name} $", "must be an object")
    _require(data.get("schema") in SCHEMAS_REVIEW_ACCEPTED, f"{name} $.schema",
             f"must be one of {', '.join(SCHEMAS_REVIEW_ACCEPTED)}")
    claims = data.get("claims")
    _require(isinstance(claims, dict), f"{name} $.claims", "must be an object")
    for claim_id in list(claims):
        claims[claim_id] = normalize_entry(claims[claim_id])
        entry = claims[claim_id]
        where = f'{name} $.claims["{claim_id}"]'
        _require(bool(CLAIM_ID_RE.match(claim_id)), where, "claim id must be <slug>/<rule id>")
        _require(isinstance(entry, dict), where, "must be an object")
        verdict = entry.get("verdict")
        _require(verdict in VERDICTS, f"{where}.verdict", f"unknown verdict {verdict!r}")
        evidence = entry.get("evidence", [])
        _require(isinstance(evidence, list), f"{where}.evidence", "must be a list")
        if verdict in ("verified", "needs-edit", "incorrect"):
            _require(bool(evidence), f"{where}.evidence", f"{verdict} needs evidence")
            if all(isinstance(ev, dict) and ev.get("kind") == "sme" for ev in evidence):
                _require(entry.get("reviewer_type") == "sme", f"{where}.evidence",
                         "SME evidence alone is accepted only from reviewer_type sme")
        if verdict == "needs-edit":
            for key in ("replacement", "what_changed"):
                _require(isinstance(entry.get(key), str) and bool(entry[key].strip()), f"{where}.{key}",
                         "required for needs-edit")
        if verdict != "unreviewed":
            _require(
                entry.get("reviewer_type") in REVIEWER_TYPES,
                f"{where}.reviewer_type",
                f"must be one of: {', '.join(REVIEWER_TYPES)}",
            )
            _require(isinstance(entry.get("body_sha256"), str), f"{where}.body_sha256", "required")
        pats = entry.get("superseded_patterns", [])
        _require(isinstance(pats, list) and all(isinstance(p, str) for p in pats),
                 f"{where}.superseded_patterns", "must be a list of strings")
        for key in ("passes", "history"):
            _require(isinstance(entry.get(key, []), list), f"{where}.{key}", "must be a list")
        for i, ev in enumerate(evidence):
            _check_evidence(where, ev, i)
    return data


def validate_signoff(data: Any, name: str = "signoff.json") -> dict[str, Any]:
    """Schema-check a sign-off record."""
    _require(isinstance(data, dict), f"{name} $", "must be an object")
    _require(data.get("schema") == SCHEMA_SIGNOFF, f"{name} $.schema", f"must be {SCHEMA_SIGNOFF}")
    domains = data.get("domains")
    _require(isinstance(domains, dict), f"{name} $.domains", "must be an object")
    for slug, entry in domains.items():
        where = f'{name} $.domains["{slug}"]'
        _require(isinstance(entry, dict), where, "must be an object")
        _require(entry.get("decision") in DECISIONS, f"{where}.decision", "unknown decision")
        _require(entry.get("reviewer_type") in REVIEWER_TYPES, f"{where}.reviewer_type",
                 f"must be one of: {', '.join(REVIEWER_TYPES)}")
        _require(isinstance(entry.get("exceptions", []), list), f"{where}.exceptions", "must be a list")
    return data


def load_review_record(path: Path) -> dict[str, Any]:
    """Load and validate ``review-record.json``."""
    return validate_record(_load_json(Path(path), ReviewRecordError))


@dataclass
class ReviewData:
    """Everything ``generate`` needs from a review folder."""

    record: dict[str, Any] = field(default_factory=lambda: {"claims": {}})
    signoff: dict[str, Any] = field(default_factory=lambda: {"domains": {}})
    present: bool = False

    def claim(self, claim_id: str) -> dict[str, Any] | None:
        """Claim record or None."""
        return self.record.get("claims", {}).get(claim_id)

    def domain_signoff(self, slug: str) -> dict[str, Any] | None:
        """Sign-off entry for a domain or None."""
        return self.signoff.get("domains", {}).get(slug)


def load_review_dir(review_dir: Path | None) -> ReviewData:
    """Load ``review-record.json`` and ``signoff.json`` (both optional; invalid → ReviewRecordError)."""
    data = ReviewData()
    if review_dir is None:
        return data
    review_dir = Path(review_dir)
    rec_path = review_dir / "review-record.json"
    so_path = review_dir / "signoff.json"
    if not rec_path.is_file() and not so_path.is_file():
        log.warning("review dir %s has no review-record.json or signoff.json; all claims unreviewed", review_dir)
        return data
    data.present = True
    if rec_path.is_file():
        data.record = load_review_record(rec_path)
    if so_path.is_file():
        data.signoff = validate_signoff(_load_json(so_path, ReviewRecordError))
    return data


# --------------------------------------------------------------------------- effective status


def _date(value: str | None) -> str:
    return (value or "")[:10] or "undated"


def ai_label(reviewer_type: str | None) -> str:
    """``AI-reviewed``, or the stand-in label; only reviewer type ``sme`` ever counts as SME."""
    return STANDIN_LABEL if reviewer_type == STANDIN else "AI-reviewed"


def effective_status(
    claim_record: dict[str, Any] | None,
    domain_signoff: dict[str, Any] | None,
    rule_id: str | None = None,
    body_sha256: str | None = None,
) -> tuple[str, str]:
    """Return (effective verdict, reviewer label) per the 9-row sign-off decision table."""
    if (
        claim_record is None
        or claim_record.get("verdict") in (None, "unreviewed")
        or (body_sha256 is not None and claim_record.get("body_sha256") != body_sha256)
    ):
        return "unreviewed", "not reviewed"
    recorded = str(claim_record["verdict"])
    if domain_signoff is not None:
        so_type = domain_signoff.get("reviewer_type")
        decision = domain_signoff.get("decision")
        name = domain_signoff.get("reviewer") or "unknown"
        role = domain_signoff.get("role") or "role not given"
        date = _date(domain_signoff.get("date"))
        if so_type == "sme":
            if decision == "rejected":
                return "unreviewed", f"SME rejected this domain's review ({name}, {date})"
            if decision == "approved-with-exceptions" and rule_id in (domain_signoff.get("exceptions") or []):
                verdict = recorded if recorded in ("incorrect", "needs-edit") else "needs-sme"
                return verdict, f"SME exception: not approved by {name} ({date})"
            return recorded, f"SME-signed by {name} ({role}, {date})"
        if decision == "rejected":
            return "unreviewed", "AI review rejected, not SME-signed"
        if claim_record.get("reviewer_type") == "sme":
            who = claim_record.get("reviewer") or "unknown"
            return recorded, (
                f"SME-reviewed claim ({who}, {_date(claim_record.get('reviewed_at'))}), domain not SME-signed"
            )
        return recorded, f"{ai_label(claim_record.get('reviewer_type'))}, not SME-signed"
    if claim_record.get("reviewer_type") == "sme":
        who = claim_record.get("reviewer") or "unknown"
        return recorded, (
            f"SME-reviewed claim ({who}, {_date(claim_record.get('reviewed_at'))}), review not signed off"
        )
    return recorded, f"{ai_label(claim_record.get('reviewer_type'))}, not SME-signed (review not signed off)"


# --------------------------------------------------------------------------- render


def _link(repo: Path, rel: str, report_dir: Path, line: int | None) -> str:
    anchor = f"#L{line}" if line else ""
    target = Path(repo) / rel
    try:
        link = to_posix(os.path.relpath(target, report_dir))
    except ValueError:
        log.debug("relpath failed for %s; using file URI", rel)
        link = target.resolve().as_uri()
    return link + anchor


def _fence_lang(path: str) -> str:
    return "java" if path.endswith(".java") else ""


def _counts(sheet: dict[str, Any], record_claims: dict[str, Any]) -> dict[str, int]:
    counts = {v: 0 for v in VERDICTS}
    counts["suspected"] = 0
    for claim in sheet.get("claims", []):
        entry = record_claims.get(claim["id"])
        verdict = entry.get("verdict", "unreviewed") if entry else "unreviewed"
        if entry and entry.get("body_sha256") != claim.get("body_sha256"):
            verdict = "unreviewed"
        counts[verdict] = counts.get(verdict, 0) + 1
        if entry and entry.get("suspected_defect") and verdict != "unreviewed":
            counts["suspected"] += 1
    return counts


def citation_check(claim: dict[str, Any]) -> str:
    """Automated citation status for one worksheet claim, reusing the BRE's own citations.

    ``verified-citation``: every cited file/class/method was found on disk.
    ``needs-sme``: at least one citation was not found (see the ``Why not found:`` lines).
    ``no-citation``: the rule cites no code location.
    """
    if claim.get("fallback_verdict") == "needs-sme" or needs_sme_fallback(claim):
        return "needs-sme"
    return "verified-citation" if claim.get("candidates") else "no-citation"


def citation_counts(out: Path, domains: list[str] | None = None) -> dict[str, int]:
    """Totals of the automatic citation check across the worksheets in ``out``.

    Reads ``claims-<slug>.json`` and tallies each claim as ``verified-citation``, ``needs-sme`` or
    ``no-citation``; this is what ``review prepare`` prints so the reader sees the citation triage."""
    counts = {"verified-citation": 0, "needs-sme": 0, "no-citation": 0}
    for slug, sheet in load_worksheets(out).items():
        if domains and slug not in domains:
            continue
        for claim in sheet.get("claims", []):
            counts[citation_check(claim)] += 1
    return counts


def review_span(sheet: dict[str, Any], record_claims: dict[str, Any]) -> str | None:
    """``Review span:`` line from worksheet ``prepared_at`` and the latest ``reviewed_at``."""
    start = parse_iso(sheet.get("prepared_at"))
    times = []
    for claim in sheet.get("claims", []):
        entry = record_claims.get(claim["id"])
        if entry and entry.get("verdict") != "unreviewed" and not entry.get("auto"):
            stamp = parse_iso(entry.get("reviewed_at"))
            if stamp:
                times.append((stamp, entry.get("reviewed_at")))
    if start is None or not times:
        return None
    latest_dt, latest = max(times)
    minutes = max((latest_dt - start).total_seconds() / 60.0, 0.0)
    count = len(times)
    per10 = minutes / count * 10
    return (
        f"Review span: {sheet.get('prepared_at')} → {latest} "
        f"({minutes:.1f} min for {count} claims, {per10:.1f} min per 10 claims)"
    )


def render_report(
    sheet: dict[str, Any],
    record_data: dict[str, Any],
    signoff_entry: dict[str, Any] | None,
    repo: Path,
    report_dir: Path,
) -> str:
    """Markdown review report for one domain."""
    claims = record_data.get("claims", {})
    slug = sheet.get("domain")
    name = sheet.get("domain_name") or slug
    counts = _counts(sheet, claims)
    reviewers = sorted(
        {
            f"{e.get('reviewer')} ({e.get('reviewer_type')})"
            for c in sheet.get("claims", [])
            if (e := claims.get(c["id"])) and e.get("verdict") != "unreviewed"
        }
    )
    types = {
        e.get("reviewer_type")
        for c in sheet.get("claims", [])
        if (e := claims.get(c["id"])) and e.get("verdict") != "unreviewed"
    }
    if signoff_entry:
        signoff_line = (
            f"{signoff_entry.get('decision')} by {signoff_entry.get('reviewer')} "
            f"({signoff_entry.get('reviewer_type')}, {signoff_entry.get('date')})"
        )
    else:
        signoff_line = "Not signed off"
    lines = [
        f"# Review report: {name} (`{slug}`)",
        "",
        f"- BRE file: `{sheet.get('bre_file') or 'none'}`",
        f"- Repo HEAD: {record_data.get('repo_head') or sheet.get('repo_head') or 'unknown'}",
        f"- Reviewers: {', '.join(reviewers) if reviewers else 'none yet'}",
        (
            f"- Verdicts: verified {counts['verified']} / needs-edit {counts['needs-edit']} / "
            f"incorrect {counts['incorrect']} / needs-sme {counts['needs-sme']} / unreviewed {counts['unreviewed']}; "
            f"suspected defects: {counts['suspected']}"
        ),
        f"- Sign-off: {signoff_line}",
    ]
    cites = [citation_check(c) for c in sheet.get("claims", [])]
    lines.append(
        f"- Citation check (automated): verified-citation {cites.count('verified-citation')} / "
        f"needs-sme {cites.count('needs-sme')} / no-citation {cites.count('no-citation')}"
    )
    span = review_span(sheet, claims)
    if span:
        lines.append(f"- {span}")
    so_is_sme = bool(signoff_entry and signoff_entry.get("reviewer_type") == "sme")
    if types and "sme" not in types and not so_is_sme:
        lines += ["", KIRO_ONLY_NOTICE]
    lines += ["", "## Claims", ""]
    attention: list[str] = []
    ordered = sorted(sheet.get("claims", []), key=lambda c: (_rule_num(c.get("rule_id", "")), c.get("rule_id", "")))
    for claim in ordered:
        entry = claims.get(claim["id"]) or {}
        verdict = entry.get("verdict", "unreviewed")
        if entry and entry.get("body_sha256") != claim.get("body_sha256"):
            verdict = "unreviewed"
        lines.append(f"### {claim['rule_id']}: {claim.get('title', '')}")
        lines.append("")
        lines.append(f"Verdict: {verdict.upper()}")
        lines.append(f"Citation check: {citation_check(claim)}")
        if entry.get("suspected_defect"):
            lines.append("Suspected defect: yes")
        if verdict != "unreviewed":
            lines.append(
                f"Reviewer: {entry.get('reviewer')} ({entry.get('reviewer_type')}, {_date(entry.get('reviewed_at'))})"
            )
            lines.append(f"Note: {entry.get('note')}")
            if verdict == "incorrect":
                lines.append(f"Correction: {entry.get('correction')}")
            if verdict == "needs-edit":
                lines.append(f"What changed: {entry.get('what_changed')}")
                lines += ["Corrected rule:", "", "```markdown"]
                lines += str(entry.get("replacement") or "").split("\n")
                lines += ["```", ""]
            found = counterexample_found(entry)
            if found:
                lines.append(f"Second pass: counterexample found ({found.get('note')})")
            lines.append("Evidence:")
            lines.append("")
            for ev in entry.get("evidence", []):
                lines += _render_evidence(ev, repo, report_dir)
        lines += _why_lines(claim, repo, report_dir)
        lines.append("")
        if verdict == "unreviewed" and claim.get("fallback_verdict") == "needs-sme":
            attention.append(f"- {claim['rule_id']}: {claim.get('title', '')} (citation not found)")
        elif verdict == "needs-sme" or (entry.get("suspected_defect") and verdict != "unreviewed"):
            tag = "needs-sme" if verdict == "needs-sme" else "suspected defect"
            attention.append(f"- {claim['rule_id']}: {claim.get('title', '')} ({tag}) — {entry.get('note')}")
    lines += ["## Claims needing SME attention", ""]
    lines += attention or ["None."]
    return "\n".join(lines).rstrip("\n") + "\n"


def _why_lines(claim: dict[str, Any], repo: Path, report_dir: Path) -> list[str]:
    """``Why not found:`` bullets with ``file#Lnn`` candidate links for one worksheet claim."""
    out: list[str] = []
    for inv in claim.get("investigations", []):
        out.append("- " + why_line(inv, lambda p, n: _link(repo, p, report_dir, n)))
    return out


def _rule_num(rule_id: str) -> int:
    digits = re.sub(r"\D", "", rule_id)
    return int(digits) if digits else 0


def _render_evidence(ev: dict[str, Any], repo: Path, report_dir: Path) -> list[str]:
    if ev.get("kind") == "range":
        link = _link(repo, ev["path"], report_dir, ev["start"])
        out = [f"- [{ev['path']}:L{ev['start']}-L{ev['end']}]({link})", ""]
        out.append(f"```{_fence_lang(ev['path'])}")
        out += str(ev.get("quote", "")).split("\n")
        out += ["```", ""]
        return out
    files = sorted((ev.get("files_sha256") or {}).keys())
    out = [
        f"- Search `{ev.get('pattern')}` in `{ev.get('scope')}`: "
        f"{ev.get('files_scanned')} files, {ev.get('hit_count')} hits"
    ]
    if int(ev.get("files_scanned") or 0) <= MAX_HITS and files:
        out.append("  - Files scanned:")
        out += [f"    - [{p}]({_link(repo, p, report_dir, None)})" for p in files]
    else:
        out.append(f"  - Files scanned: {ev.get('files_scanned')} (listed in review-record.json)")
    for hit in ev.get("hits", []):
        out.append(f"  - [{hit['path']}:L{hit['line']}]({_link(repo, hit['path'], report_dir, hit['line'])})")
    out.append("")
    return out


def render_checklist(
    sheet: dict[str, Any],
    record_data: dict[str, Any],
    repo: Path | None = None,
    report_dir: Path | None = None,
) -> str:
    """Sign-off checklist Markdown with the exact six checkbox lines.

    Claims whose citations were not found are listed with their ``Why not found:`` line.
    """
    counts = _counts(sheet, record_data.get("claims", {}))
    total = len(sheet.get("claims", []))
    name = sheet.get("domain_name") or sheet.get("domain")
    lines = [
        f"# Sign-off checklist: {name}",
        "",
        (
            f"Claims: {total} total; verified {counts['verified']}, needs-edit {counts['needs-edit']}, "
            f"incorrect {counts['incorrect']}, needs-sme {counts['needs-sme']}, unreviewed {counts['unreviewed']}; "
            f"suspected defects {counts['suspected']}."
        ),
        "",
    ]
    lines += [f"- [ ] {key}: {text}" for key, text in CHECKLIST_ITEMS]
    lines += [
        "",
        "Reviewer:",
        "Role:",
        f"Reviewer type ({'|'.join(REVIEWER_TYPES)}):",
        "Date:",
        "Decision (approved|approved-with-exceptions|rejected):",
        "Exceptions (BR ids):",
    ]
    unfound = [c for c in sheet.get("claims", []) if c.get("investigations")]
    if unfound:
        lines += ["", "## Citations not found (confirm or correct each)", ""]
        for claim in sorted(unfound, key=lambda c: _rule_num(c.get("rule_id", ""))):
            lines.append(f"### {claim['rule_id']}: {claim.get('title', '')}")
            if repo is not None and report_dir is not None:
                lines += _why_lines(claim, repo, report_dir)
            else:
                lines += ["- " + why_line(inv) for inv in claim.get("investigations", [])]
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render(out: Path, repo: Path) -> list[Path]:
    """Write ``review-report-<slug>.md`` and ``signoff-checklist-<slug>.md`` for every worksheet."""
    out = Path(out)
    record_path = out / "review-record.json"
    record_data = load_review_record(record_path) if record_path.is_file() else {"claims": {}}
    signoff_path = out / "signoff.json"
    signoff_data = (
        validate_signoff(_load_json(signoff_path, ReviewRecordError))
        if signoff_path.is_file()
        else {"domains": {}}
    )
    written: list[Path] = []
    for slug, sheet in sorted(load_worksheets(out).items()):
        report = out / f"review-report-{slug}.md"
        atomic_write_text(
            report,
            render_report(sheet, record_data, signoff_data["domains"].get(slug), Path(repo), out),
        )
        checklist = out / f"signoff-checklist-{slug}.md"
        atomic_write_text(checklist, render_checklist(sheet, record_data, Path(repo), out))
        written += [report, checklist]
    return written


# --------------------------------------------------------------------------- signoff


def parse_checklist(text: str) -> dict[str, bool]:
    """Read ``- [x] key:`` lines; unknown keys raise ReviewRecordError."""
    result = {key: False for key in CHECKLIST_KEYS}
    for line in text.replace("\r\n", "\n").split("\n"):
        match = CHECKLIST_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(2)
        if key not in result:
            raise ReviewRecordError(f"checklist: unknown item {key!r}")
        result[key] = match.group(1).lower() == "x"
    return result


def signoff(
    out: Path,
    domain: str,
    reviewer: str,
    role: str,
    reviewer_type: str,
    decision: str,
    exceptions: list[str] | None = None,
    checklist_file: Path | None = None,
) -> dict[str, Any]:
    """Upsert the domain entry in ``signoff.json``."""
    if reviewer_type not in REVIEWER_TYPES:
        raise BcpInputError(f"--reviewer-type must be one of: {', '.join(REVIEWER_TYPES)}")
    if decision not in DECISIONS:
        raise BcpInputError(f"--decision must be one of {', '.join(DECISIONS)}")
    if not reviewer or not reviewer.strip() or not role or not role.strip():
        raise BcpInputError("--reviewer and --role are required")
    out = Path(out)
    checklist = {key: False for key in CHECKLIST_KEYS}
    if checklist_file is not None:
        path = Path(checklist_file)
        if not path.is_file():
            raise BcpInputError(f"checklist file not found: {path}")
        checklist = parse_checklist(read_text(path))
    path = out / "signoff.json"
    data = (
        validate_signoff(_load_json(path, ReviewRecordError))
        if path.is_file()
        else {"schema": SCHEMA_SIGNOFF, "domains": {}}
    )
    entry = {
        "reviewer": reviewer.strip(),
        "role": role.strip(),
        "reviewer_type": reviewer_type,
        "date": now_iso()[:10],
        "decision": decision,
        "exceptions": [e.strip() for e in (exceptions or []) if e.strip()],
        "checklist": checklist,
    }
    data["domains"][domain] = entry
    out.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, dump_json(data))
    return entry
