"""Knowledge-base layer: export reviewed facts for a vector knowledge base (``bcp kb export``).

Only reviewer-confirmed content is exported, one fact per document:

* ``verified`` rules, with the reviewed rule text exactly as it stands in ``reviewed-rules.md``;
* ``needs-edit`` rules that carry a full replacement text (the reviewer's corrected rule).

``incorrect``, ``needs-sme``, ``unreviewed``, automatic verdicts, ``needs-edit`` without a replacement,
claims whose latest second pass found a counterexample, verdicts that no longer match the reviewed
context (stale text or a sign-off exception) and facts whose cited code lines changed since the review
are never exported. Every rule seen is listed in ``export-audit.json`` with the reason.

Each fact is written as ``<out>/verified/<domain>/<FACT-ID>.md`` plus ``<FACT-ID>.md.metadata.json``
(``{"metadataAttributes": {...}}``, at most :data:`METADATA_MAX_BYTES` bytes). Two derived fact kinds
are built from the exported facts only: a per-domain feature-to-component map and one component
glossary entry per class cited as evidence.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from bcp import claims as claims_mod
from bcp.config import BcpConfig
from bcp.errors import BcpInputError, OutputWriteError
from bcp.render import ANCHORS_PREFIX, SECOND_ROUND_PREFIX, WHAT_CHANGED_PREFIX
from bcp.review import (
    counterexample_found,
    effective_status,
    git_head,
    load_review_dir,
    now_iso,
    sanitize_replacement,
)
from bcp.textio import atomic_write_text, dump_json, read_text, safe_join, sha256_text, to_posix

log = logging.getLogger(__name__)

SCHEMA_AUDIT = "bcp.kb-export/1"
EXPORT_VERDICTS = ("verified", "needs-edit")
METADATA_MAX_BYTES = 1024
METADATA_SUFFIX = ".metadata.json"
VERIFIED_DIR = "verified"
QUARANTINE_DIR = "quarantine"
AUDIT_FILE = "export-audit.json"
REVIEW_PREFIX = "> Review: "
STATUS_LINE_PREFIXES = (REVIEW_PREFIX, WHAT_CHANGED_PREFIX, ANCHORS_PREFIX, SECOND_ROUND_PREFIX)
CONTEXT_LABELS = (("VERIFIED", "verified"), ("NEEDS EDIT", "needs-edit"), ("NEEDS SME", "needs-sme"),
                  ("UNREVIEWED", "unreviewed"), ("INCORRECT", "incorrect"))
DERIVED = "derived"
DERIVED_REVIEWER = "derived-from-verified"
HASH_DERIVED = "SHA-256 over the source hashes of the facts it is derived from, one per line"
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Skip reasons (stable keys used in the audit and the summary counts).
R_UNREVIEWED = "unreviewed"
R_NEEDS_SME = "needs-sme"
R_INCORRECT = "incorrect"
R_AUTO = "automatic-verdict"
R_NO_REPLACEMENT = "needs-edit-without-replacement"
R_COUNTEREXAMPLE = "counterexample-found"
R_CONTEXT = "context-mismatch"
R_STALE = "stale-text"
R_CODE_CHANGED = "cited-code-changed"
R_NO_EVIDENCE = "no-line-evidence"
R_UNKNOWN = "unknown-verdict"


# --------------------------------------------------------------------------- filter


def record_filter(entry: dict[str, Any] | None, raw_verdict: str | None = None) -> tuple[bool, str, str]:
    """Decide from the review-record entry alone whether a rule may be exported.

    Returns ``(exportable, reason_key, detail)``. Only ``verified`` and ``needs-edit`` with a non-empty
    replacement pass; everything else is refused with a reason. ``raw_verdict`` is the verdict as stored
    on disk: loading upgrades a legacy ``incorrect`` entry that carries a replacement to ``needs-edit``,
    and the export still refuses it."""
    if entry is None:
        return False, R_UNREVIEWED, "no review-record entry"
    if raw_verdict == "incorrect":
        return False, R_INCORRECT, "recorded incorrect (a legacy replacement is not exported)"
    verdict = entry.get("verdict")
    if verdict in (None, "unreviewed"):
        return False, R_UNREVIEWED, "not reviewed"
    if verdict == "incorrect":
        return False, R_INCORRECT, "core claim wrong; only a reviewer correction exists"
    if verdict == "needs-sme":
        return False, R_NEEDS_SME, "waiting for an SME decision"
    if verdict not in EXPORT_VERDICTS:
        return False, R_UNKNOWN, f"verdict {verdict!r} is not exportable"
    if entry.get("auto"):
        return False, R_AUTO, "automatic tool verdict, not a review"
    if verdict == "needs-edit" and not str(entry.get("replacement") or "").strip():
        return False, R_NO_REPLACEMENT, "needs-edit without an approved full replacement text"
    found = counterexample_found(entry)
    if found:
        return False, R_COUNTEREXAMPLE, "latest second pass found a counterexample"
    return True, "exported", verdict


# --------------------------------------------------------------------------- reviewed context


@dataclass
class ContextRule:
    """One rule section of ``reviewed-rules.md`` with its status lines separated from the text."""

    rule_id: str
    title: str
    status: str
    body: str
    body_sha256: str


def context_status(review_line: str) -> str:
    """Map a ``> Review:`` line to a verdict key (``unreviewed`` when unknown)."""
    label = review_line[len(REVIEW_PREFIX):] if review_line.startswith(REVIEW_PREFIX) else review_line
    for prefix, key in CONTEXT_LABELS:
        if label.startswith(prefix):
            return key
    return "unreviewed"


def parse_reviewed_rules(text: str, heading_re: re.Pattern[str] | None = None) -> dict[str, ContextRule]:
    """Parse rule sections; the ``> Review:``-style lines under a heading are status, not rule text.

    ``body_sha256`` is recomputed over heading plus rule text, so it equals the review record's hash when
    the context shows exactly the text the reviewer checked."""
    out: dict[str, ContextRule] = {}
    for claim in claims_mod.parse(text, heading_re):
        lines = claim.text.split("\n")
        heading, rest = lines[0], lines[1:]
        status = "unreviewed"
        while rest and rest[0].startswith(STATUS_LINE_PREFIXES):
            line = rest.pop(0)
            if line.startswith(REVIEW_PREFIX):
                status = context_status(line)
        out[claim.rule_id] = ContextRule(
            rule_id=claim.rule_id,
            title=claim.title,
            status=status,
            body="\n".join(rest).strip("\n"),
            body_sha256=claims_mod.body_sha256("\n".join([heading] + rest)),
        )
    return out


# --------------------------------------------------------------------------- evidence, hash, metadata


def _file_lines(repo: Path, rel: str, cache: dict[str, list[str] | None]) -> list[str] | None:
    if rel not in cache:
        path = safe_join(repo, rel)
        if not path.is_file():
            cache[rel] = None
        else:
            lines = read_text(path).replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            cache[rel] = lines
    return cache[rel]


def range_evidence(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Line-range evidence items in record order."""
    return [ev for ev in entry.get("evidence") or [] if ev.get("kind") == "range"]


def cited_lines(repo: Path, evidence: Sequence[dict[str, Any]],
                cache: dict[str, list[str] | None] | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    """Read each cited range from the repo now; return ``([(path#Lx-Ly, text)], [changed specs])``.

    A range is *changed* when the file is gone, the range no longer fits, or the lines differ from the
    quote the review captured."""
    cache = {} if cache is None else cache
    items: list[tuple[str, str]] = []
    changed: list[str] = []
    for ev in evidence:
        rel, start, end = to_posix(ev["path"]), int(ev["start"]), int(ev["end"])
        spec = f"{rel}#L{start}-L{end}"
        lines = _file_lines(repo, rel, cache)
        if lines is None or start < 1 or end < start or end > len(lines):
            changed.append(spec)
            continue
        text = "\n".join(lines[start - 1:end])
        if "quote" in ev and ev["quote"] != text:
            changed.append(spec)
        items.append((spec, text))
    return items, changed


def source_hash(items: Sequence[tuple[str, str]]) -> str:
    """SHA-256 over the cited code lines: ``path#Lx-Ly`` LF lines LF, per range, in record order.

    The line spec is part of the input, so a pure line move changes this hash; :func:`content_hash` is
    the position-independent companion used to tell a move apart from a real content change."""
    return sha256_text("".join(f"{spec}\n{text}\n" for spec, text in items))


def normalise_code(text: str) -> str:
    """Whitespace-normalised cited text: trailing whitespace stripped per line, runs of blank lines
    collapsed to one, leading and trailing blank lines dropped. Position-independent (no line numbers)."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def content_hash(items: Sequence[tuple[str, str]]) -> str:
    """SHA-256 over only the cited code text, whitespace-normalised and position-independent: the
    line specs are excluded, so a fact whose code merely moved keeps the same ``content_hash`` even
    though its :func:`source_hash` changes. Ranges are joined in record order with a unit separator."""
    return sha256_text("\n\x1e\n".join(normalise_code(text) for _spec, text in items))


def evidence_specs(evidence: Sequence[dict[str, Any]]) -> list[str]:
    """``path#Lx-Ly,La-Lb`` per file, files in first-cited order, ranges in record order (deduplicated)."""
    grouped: dict[str, list[str]] = {}
    for ev in evidence:
        rng = f"L{int(ev['start'])}-L{int(ev['end'])}"
        bucket = grouped.setdefault(to_posix(ev["path"]), [])
        if rng not in bucket:
            bucket.append(rng)
    return [f"{path}#{','.join(rngs)}" for path, rngs in grouped.items()]


def component_name(rel: str) -> str:
    """Class (component) name of a source file: its stem."""
    name = to_posix(rel).rsplit("/", 1)[-1]
    return name.split(".", 1)[0] if "." in name else name


def components(evidence: Sequence[dict[str, Any]]) -> list[str]:
    """Distinct component names of the cited files, in first-cited order."""
    out: list[str] = []
    for ev in evidence:
        name = component_name(ev["path"])
        if name not in out:
            out.append(name)
    return out


def metadata_text(attrs: dict[str, Any]) -> str:
    """The exact metadata file text (deterministic JSON, one line plus LF)."""
    return json.dumps({"metadataAttributes": attrs}, sort_keys=True, ensure_ascii=False) + "\n"


def fit_metadata(attrs: dict[str, Any], limit: int = METADATA_MAX_BYTES) -> tuple[dict[str, Any], bool]:
    """Trim metadata from the tail until the metadata file is <= ``limit`` bytes.

    Order (deterministic, list order kept): drop ``evidence`` items from the end down to one; then drop
    line ranges from the end of that last item down to one range; then drop ``components`` from the end.
    When anything is dropped the totals are recorded as ``evidence_total`` / ``components_total`` and
    ``truncated`` is true; the fact document still lists everything."""
    attrs = dict(attrs)
    attrs["evidence"] = list(attrs.get("evidence") or [])
    attrs["components"] = list(attrs.get("components") or [])
    if len(metadata_text(attrs).encode("utf-8")) <= limit:
        return attrs, False
    attrs["truncated"] = True
    attrs["evidence_total"] = len(attrs["evidence"])
    attrs["components_total"] = len(attrs["components"])
    while len(metadata_text(attrs).encode("utf-8")) > limit:
        last = attrs["evidence"][-1] if attrs["evidence"] else ""
        if len(attrs["evidence"]) > 1:
            attrs["evidence"].pop()
        elif "," in last.split("#", 1)[-1]:
            attrs["evidence"][-1] = last.rsplit(",", 1)[0]
        elif len(attrs["components"]) > 1:
            attrs["components"].pop()
        else:
            raise OutputWriteError(f"metadata for {attrs.get('fact_id')} cannot fit in {limit} bytes")
    return attrs, True


def safe_id(text: str) -> str:
    """File-name-safe fact id fragment."""
    return SAFE_ID_RE.sub("-", text).strip("-") or "x"


# --------------------------------------------------------------------------- export


@dataclass
class Fact:
    """One exported fact document."""

    fact_id: str
    fact_type: str
    domain: str
    title: str
    status: str
    reviewer_type: str
    components: list[str]
    evidence: list[str]
    source_hash: str
    body: str
    header: list[str] = field(default_factory=list)
    suspected_defect: bool = False
    sources: list[str] = field(default_factory=list)
    content_hash: str = ""

    def attributes(self, commit: str) -> dict[str, Any]:
        """Bedrock metadata attributes before size fitting."""
        attrs = {
            "fact_id": self.fact_id,
            "fact_type": self.fact_type,
            "domain": self.domain,
            "components": self.components,
            "status": self.status,
            "reviewer_type": self.reviewer_type,
            "evidence": self.evidence,
            "commit": commit,
            "source_hash": self.source_hash,
            "suspected_defect": self.suspected_defect,
        }
        if self.content_hash:  # omitted on facts exported before 1.3.0; present on every new export
            attrs["content_hash"] = self.content_hash
        return attrs

    def document(self, commit: str) -> str:
        """Markdown fact document (self-contained: one document is one retrieval chunk)."""
        lines = [f"# {self.title}", "", f"- Fact: {self.fact_id}", f"- Type: {self.fact_type}",
                 f"- Domain: {self.domain}", f"- Status: {self.status}"]
        lines += self.header
        lines += [f"- Components: {', '.join(self.components) or 'none'}",
                  f"- Evidence: {'; '.join(self.evidence) or 'none'}",
                  f"- Commit: {commit}",
                  f"- Source hash ({'SHA-256 of the cited code lines' if self.fact_type == 'rule' else HASH_DERIVED}): "
                  f"{self.source_hash}"]
        if self.content_hash:
            lines.append(f"- Content hash (position-independent SHA-256 of the cited code text): {self.content_hash}")
        lines += ["", self.body.strip("\n"), ""]
        return "\n".join(lines)


@dataclass
class ExportResult:
    """What ``bcp kb export`` wrote and why it skipped the rest."""

    facts: list[Fact]
    audit: dict[str, Any]
    written: list[Path]
    removed: list[Path]

    def counts(self) -> dict[str, dict[str, int]]:
        """Exported facts by domain and status."""
        out: dict[str, dict[str, int]] = {}
        for fact in self.facts:
            per = out.setdefault(fact.domain, {})
            per[fact.status] = per.get(fact.status, 0) + 1
        return dict(sorted(out.items()))

    def summary(self) -> dict[str, Any]:
        """Machine-readable summary (``--json``)."""
        return {"counts": self.counts(), "skipped": self.audit["summary"]["skipped_by_reason"],
                "rules_seen": self.audit["summary"]["rules_seen"], "facts": len(self.facts),
                "metadata_truncated": self.audit["summary"]["metadata_truncated"],
                "written": len(self.written), "removed": len(self.removed)}


def _rule_sort_key(rule_id: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", rule_id)
    return (int(digits) if digits else 0, rule_id)


def _discover_domains(context: Path) -> list[str]:
    base = context / "domains"
    if not base.is_dir():
        raise BcpInputError(f"no domains folder under {context}")
    return sorted(p.name for p in base.iterdir() if (p / "reviewed-rules.md").is_file())


def _load_file_index(context: Path) -> dict[str, list[str]]:
    path = context / "file-index.json"
    if not path.is_file():
        return {}
    data = json.loads(read_text(path))
    return {to_posix(k): list(v) for k, v in data.items()} if isinstance(data, dict) else {}


def export_facts(
    repo: Path,
    out: Path,
    config: BcpConfig,
    context: Path | None = None,
    review_dir: Path | None = None,
    domains: Sequence[str] | None = None,
) -> ExportResult:
    """Export verified and approved needs-edit rules plus derived facts; write the audit list."""
    repo = Path(repo)
    context = Path(context) if context else repo / ".kiro" / "context"
    review_dir = Path(review_dir) if review_dir else repo / ".kiro" / "bre-review"
    if not (review_dir / "review-record.json").is_file():
        raise BcpInputError(f"review record not found: {review_dir / 'review-record.json'}")
    review = load_review_dir(review_dir)
    raw_claims = json.loads(read_text(review_dir / "review-record.json")).get("claims") or {}
    slugs = list(domains) if domains else _discover_domains(context)
    commit = git_head(repo) or str(review.record.get("repo_head") or "unknown")
    file_index = _load_file_index(context)
    heading_re = config.rule_heading_re
    cache: dict[str, list[str] | None] = {}

    rules_audit: list[dict[str, Any]] = []
    facts: list[Fact] = []
    for slug in slugs:
        rules_path = context / "domains" / slug / "reviewed-rules.md"
        if not rules_path.is_file():
            raise BcpInputError(f"reviewed rules not found for domain {slug}: {rules_path}")
        ctx_rules = parse_reviewed_rules(read_text(rules_path), heading_re)
        record_ids = {cid.split("/", 1)[1] for cid in review.record.get("claims", {}) if cid.startswith(slug + "/")}
        signoff = review.domain_signoff(slug)
        for rule_id in sorted(record_ids | set(ctx_rules), key=_rule_sort_key):
            claim_id = f"{slug}/{rule_id}"
            entry = review.claim(claim_id)
            ctx = ctx_rules.get(rule_id)
            ok, reason, detail = record_filter(entry, (raw_claims.get(claim_id) or {}).get("verdict"))
            row: dict[str, Any] = {
                "claim": claim_id, "domain": slug, "rule_id": rule_id,
                "title": ctx.title if ctx else None,
                "verdict": (entry or {}).get("verdict") or "unreviewed",
                "reviewer_type": (entry or {}).get("reviewer_type"),
                "context_status": ctx.status if ctx else None,
            }
            fact: Fact | None = None
            if ok:
                fact, reason, detail = _rule_fact(slug, rule_id, entry or {}, ctx, signoff, repo, cache)
            row.update({"exported": fact is not None, "reason": reason, "detail": detail,
                        "fact_id": fact.fact_id if fact else None})
            rules_audit.append(row)
            if fact:
                facts.append(fact)

    rule_facts = list(facts)
    facts += feature_maps(rule_facts)
    facts += glossary_facts(rule_facts, file_index)

    written, truncated = _write_facts(Path(out), facts, commit)
    removed = _remove_stale(Path(out) / VERIFIED_DIR, {p.resolve() for p in written})
    removed += _retire_quarantined(Path(out) / QUARANTINE_DIR, {f.fact_id for f in facts})
    skipped: dict[str, int] = {}
    for row in rules_audit:
        if not row["exported"]:
            skipped[row["reason"]] = skipped.get(row["reason"], 0) + 1
    by_domain: dict[str, dict[str, int]] = {}
    for fact in facts:
        per = by_domain.setdefault(fact.domain, {})
        per[fact.status] = per.get(fact.status, 0) + 1
    audit = {
        "schema": SCHEMA_AUDIT,
        "generated_at": now_iso(),
        "repo": to_posix(repo),
        "commit": commit,
        "record_repo_head": review.record.get("repo_head"),
        "domains": slugs,
        "metadata_max_bytes": METADATA_MAX_BYTES,
        "source_hash": "SHA-256 over '<path>#L<start>-L<end>' LF '<cited lines>' LF per range, record order",
        "rules": rules_audit,
        "derived": [{"fact_id": f.fact_id, "fact_type": f.fact_type, "domain": f.domain, "from": f.sources}
                    for f in facts if f.fact_type != "rule"],
        "summary": {
            "rules_seen": len(rules_audit),
            "rules_exported": len(rule_facts),
            "facts": len(facts),
            "by_domain_status": dict(sorted(by_domain.items())),
            "skipped_by_reason": dict(sorted(skipped.items())),
            "metadata_truncated": truncated,
        },
    }
    audit_path = Path(out) / AUDIT_FILE
    atomic_write_text(audit_path, dump_json(audit))
    written.append(audit_path)
    return ExportResult(facts=facts, audit=audit, written=written, removed=removed)


def _rule_fact(
    slug: str,
    rule_id: str,
    entry: dict[str, Any],
    ctx: ContextRule | None,
    signoff: dict[str, Any] | None,
    repo: Path,
    cache: dict[str, list[str] | None],
) -> tuple[Fact | None, str, str]:
    """Context, staleness and code checks for a rule that passed :func:`record_filter`."""
    verdict = str(entry["verdict"])
    if ctx is None:
        return None, R_CONTEXT, "rule not present in reviewed-rules.md"
    effective, who = effective_status(entry, signoff, rule_id)
    if ctx.status != verdict or effective != verdict:
        return None, R_CONTEXT, (f"record says {verdict}, reviewed context shows {ctx.status}, effective "
                                 f"status {effective} (stale verdict or sign-off exception)")
    if entry.get("body_sha256") != ctx.body_sha256 and verdict == "verified":
        return None, R_STALE, "reviewed-rules.md text differs from the text the reviewer checked"
    if verdict == "needs-edit":
        body = sanitize_replacement(str(entry.get("replacement") or ""))
        if body.strip() != ctx.body.strip():
            return None, R_CONTEXT, "reviewed-rules.md does not show the approved replacement text"
    else:
        body = ctx.body
    evidence = range_evidence(entry)
    if not evidence:
        return None, R_NO_EVIDENCE, "no line-range evidence to cite or hash"
    items, changed = cited_lines(repo, evidence, cache)
    if changed:
        return None, R_CODE_CHANGED, "cited lines differ from the review: " + ", ".join(changed[:5])
    header = [f"- Review: {who}; reviewer {entry.get('reviewer') or 'unknown'} "
              f"({entry.get('reviewer_type') or 'unknown'}), {str(entry.get('reviewed_at') or '')[:10]}"]
    if entry.get("suspected_defect"):
        header.append("- Suspected defect: yes (the reviewer flagged the code behaviour as a possible defect)")
    if verdict == "needs-edit":
        header.append(f"- What changed from the extraction: {' '.join(str(entry.get('what_changed') or '').split())}")
    searches = [ev for ev in entry.get("evidence") or [] if ev.get("kind") == "search"]
    for ev in searches:
        header.append(f"- Search evidence: /{ev.get('pattern')}/ in {ev.get('scope')}: {ev.get('hit_count')} hits")
    fact = Fact(
        fact_id=f"{safe_id(slug)}-{safe_id(rule_id)}",
        fact_type="rule",
        domain=slug,
        title=f"{rule_id}: {ctx.title}" if ctx.title else rule_id,
        status=verdict,
        reviewer_type=str(entry.get("reviewer_type") or "unknown"),
        components=components(evidence),
        evidence=evidence_specs(evidence),
        source_hash=source_hash(items),
        content_hash=content_hash(items),
        body=body,
        header=header,
        suspected_defect=bool(entry.get("suspected_defect")),
        sources=[f"{slug}/{rule_id}"],
    )
    return fact, "exported", verdict


def _derived_hash(facts: Sequence[Fact]) -> str:
    return sha256_text("".join(f.source_hash + "\n" for f in facts))


def _derived_content_hash(facts: Sequence[Fact]) -> str:
    """Position-independent companion of :func:`_derived_hash`: SHA-256 over the content hashes of the
    facts a derived fact is built from (empty when any source predates 1.3.0 and has no content hash)."""
    if not all(f.content_hash for f in facts):
        return ""
    return sha256_text("".join(f.content_hash + "\n" for f in facts))


def _merge_specs(specs: Sequence[str]) -> list[str]:
    """Merge ``path#ranges`` specs per path (first-seen order, ranges deduplicated)."""
    grouped: dict[str, list[str]] = {}
    for spec in specs:
        path, rngs = spec.split("#", 1)
        bucket = grouped.setdefault(path, [])
        bucket += [r for r in rngs.split(",") if r not in bucket]
    return [f"{p}#{','.join(r)}" for p, r in grouped.items()]


def _merge_evidence(facts: Sequence[Fact]) -> list[str]:
    return _merge_specs([spec for fact in facts for spec in fact.evidence])


def feature_maps(rule_facts: Sequence[Fact]) -> list[Fact]:
    """One feature-to-component map per domain, built only from that domain's exported rule facts."""
    out: list[Fact] = []
    by_domain: dict[str, list[Fact]] = {}
    for fact in rule_facts:
        by_domain.setdefault(fact.domain, []).append(fact)
    for slug, facts in sorted(by_domain.items()):
        comps: list[str] = []
        for fact in facts:
            comps += [c for c in fact.components if c not in comps]
        lines = [f"Feature-to-component map for domain `{slug}`, derived only from its exported reviewed facts "
                 "(each line: the reviewed rule, then the components its evidence cites).", ""]
        lines += [f"- {f.title} ({f.fact_id}, {f.status}) -> {', '.join(f.components)}" for f in facts]
        lines += ["", "Component -> features:", ""]
        for comp in comps:
            ids = [f.fact_id for f in facts if comp in f.components]
            lines.append(f"- {comp}: {', '.join(ids)}")
        out.append(Fact(
            fact_id=f"{safe_id(slug)}-FEATURE-MAP", fact_type="feature-map", domain=slug,
            title=f"Feature-to-component map: {slug}", status=DERIVED, reviewer_type=DERIVED_REVIEWER,
            components=comps, evidence=_merge_evidence(facts), source_hash=_derived_hash(facts),
            content_hash=_derived_content_hash(facts),
            body="\n".join(lines), header=[f"- Derived from: {', '.join(f.fact_id for f in facts)}"],
            sources=[f.fact_id for f in facts],
        ))
    return out


def glossary_facts(rule_facts: Sequence[Fact], file_index: dict[str, list[str]]) -> list[Fact]:
    """One glossary entry per component cited by an exported rule fact.

    The entry names the component's file(s) and owning domain(s) from the mechanical file index and the
    reviewed facts that cite it; it adds no unreviewed narrative."""
    cited: dict[str, list[Fact]] = {}
    paths: dict[str, list[str]] = {}
    for fact in rule_facts:
        for spec in fact.evidence:
            path = spec.split("#", 1)[0]
            comp = component_name(path)
            if fact not in cited.setdefault(comp, []):
                cited[comp].append(fact)
            if path not in paths.setdefault(comp, []):
                paths[comp].append(path)
    out: list[Fact] = []
    for comp in sorted(cited):
        facts = cited[comp]
        owners: list[str] = []
        for path in paths[comp]:
            owners += [d for d in file_index.get(path, []) if d not in owners]
        domain = owners[0] if owners else facts[0].domain
        lines = [f"`{comp}` is a code component (class) cited as evidence by reviewed facts.", "",
                 f"- Source file(s): {', '.join(paths[comp])}",
                 f"- Owning domain(s) per the file index: {', '.join(owners) or 'not mapped'}",
                 "- Behaviour it takes part in (reviewed facts):"]
        lines += [f"  - {f.title} ({f.fact_id}, domain {f.domain}, {f.status})" for f in facts]
        evidence = _merge_specs([spec for f in facts for spec in f.evidence if spec.split("#", 1)[0] in paths[comp]])
        out.append(Fact(
            fact_id=f"GLOSSARY-{safe_id(comp)}", fact_type="glossary", domain=domain,
            title=f"Glossary: {comp}", status=DERIVED, reviewer_type=DERIVED_REVIEWER,
            components=[comp], evidence=evidence, source_hash=_derived_hash(facts),
            content_hash=_derived_content_hash(facts),
            body="\n".join(lines), header=[f"- Derived from: {', '.join(f.fact_id for f in facts)}"],
            sources=[f.fact_id for f in facts],
        ))
    return out


def _write_facts(out: Path, facts: Sequence[Fact], commit: str) -> tuple[list[Path], list[dict[str, Any]]]:
    written: list[Path] = []
    truncated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fact in facts:
        if fact.fact_id in seen:
            raise OutputWriteError(f"duplicate fact id {fact.fact_id}")
        seen.add(fact.fact_id)
        folder = out / VERIFIED_DIR / safe_id(fact.domain)
        doc = folder / f"{fact.fact_id}.md"
        meta_path = folder / f"{fact.fact_id}.md{METADATA_SUFFIX}"
        attrs, cut = fit_metadata(fact.attributes(commit))
        if cut:
            truncated.append({"fact_id": fact.fact_id, "bytes": len(metadata_text(attrs).encode("utf-8")),
                              "evidence_kept": len(attrs["evidence"]), "evidence_total": attrs["evidence_total"],
                              "components_kept": len(attrs["components"]),
                              "components_total": attrs["components_total"]})
            log.debug("%s: metadata trimmed to %d bytes (evidence %d/%d, components %d/%d kept)", fact.fact_id,
                     len(metadata_text(attrs).encode("utf-8")), len(attrs["evidence"]), attrs["evidence_total"],
                     len(attrs["components"]), attrs["components_total"])
        atomic_write_text(doc, fact.document(commit))
        atomic_write_text(meta_path, metadata_text(attrs))
        written += [doc, meta_path]
    return written, truncated


def _remove_stale(verified: Path, keep: set[Path]) -> list[Path]:
    """Delete fact files from an earlier export that this export no longer produces."""
    removed: list[Path] = []
    if not verified.is_dir():
        return removed
    for path in sorted(verified.rglob("*.md")) + sorted(verified.rglob(f"*.md{METADATA_SUFFIX}")):
        if path.is_file() and path.resolve() not in keep:
            path.unlink()
            removed.append(path)
    return removed


def _retire_quarantined(quarantine: Path, exported: set[str]) -> list[Path]:
    """Delete the local ``quarantine/`` copy of every fact this export wrote to ``verified/`` again.

    After a real code change the SME re-reviews against the new code and re-exports; the new fact
    supersedes the quarantined one (``bcp kb stale`` would otherwise find it in both folders). The
    quarantined S3 objects sit outside the indexed ``verified/`` prefix and are overwritten if the fact is
    quarantined again.
    """
    removed: list[Path] = []
    if not quarantine.is_dir():
        return removed
    for doc in sorted(quarantine.rglob("*.md")):
        meta = doc.with_name(doc.name + METADATA_SUFFIX)
        fact_id = doc.stem
        if meta.is_file():
            try:
                fact_id = str((json.loads(read_text(meta)).get("metadataAttributes") or {}).get("fact_id") or doc.stem)
            except (json.JSONDecodeError, AttributeError):
                pass
        if fact_id not in exported:
            continue
        for path in (doc, meta):
            if path.is_file():
                path.unlink()
                removed.append(path)
        if doc.parent != quarantine and doc.parent.is_dir() and not any(doc.parent.iterdir()):
            doc.parent.rmdir()
    return removed
