"""Drift check: does the BRE (and its review evidence) still match the current code?"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bcp.bre import BreLayout
from bcp.claims import Token, classify, is_non_code
from bcp.config import BcpConfig
from bcp.errors import BcpInputError
from bcp.investigate import investigate, why_line
from bcp.manifest import DomainManifest, load_manifest
from bcp.review import SCHEMA_CLAIMS, load_review_record, now_iso
from bcp.sourceindex import METHOD_MISSING, RESOLVED, UNRESOLVED, SourceIndex, resolve_claim, token_from_entry
from bcp.textio import atomic_write_text, dump_json, md_cell, read_text, safe_join, sha256_file

log = logging.getLogger(__name__)

SCHEMA_DRIFT = "bcp.drift/1"
SCHEMA_BASELINE = "bcp.citations/1"
SEVERITIES = ("HIGH", "MEDIUM", "LOW", "INFO")
MAX_UNMAPPED = 200
ACTIONS = {
    "missing-file": "Re-run the BRE for this component and update the domain's source files.",
    "loc-changed": "Re-review this domain's claims that cite the file.",
    "unresolved-citation": "Re-review the claim; re-run the BRE for the component if the code moved.",
    "pre-existing-unresolved": "Ask an SME to confirm or correct the citation (it never pointed at a method).",
    "citation-now-resolves": "No action; the citation now resolves.",
    "no-baseline-unresolved": "Run review prepare or generate to record a baseline, then re-check.",
    "evidence-moved": "Re-review the claim's range evidence.",
    "evidence-changed": "Re-review the claim; the recorded search result changed.",
    "unmapped-file": "Map the file to a domain (re-run the BRE for its component) or accept it as unmapped.",
    "changed-since-review": "Re-review claims that cite this file.",
}


@dataclass
class DriftReport:
    """Findings plus metadata."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    baseline_source: str = "none"
    repo_head: str | None = None
    unmapped_total: int = 0
    git_history: str = "high"  # high | all | none: which findings run the read-only git log check

    def add(self, severity: str, kind: str, domain: str | None, message: str, **extra: Any) -> None:
        """Record a finding."""
        entry = {"severity": severity, "kind": kind, "domain": domain, "message": message,
                 "action": ACTIONS.get(kind, "")}
        entry.update({k: v for k, v in extra.items() if v is not None})
        self.findings.append(entry)

    def counts(self) -> dict[str, int]:
        """Findings per severity."""
        return {s: sum(1 for f in self.findings if f["severity"] == s) for s in SEVERITIES}

    def high_citation(self) -> int:
        """HIGH citation findings (unresolved-citation)."""
        return sum(1 for f in self.findings if f["severity"] == "HIGH" and f["kind"] == "unresolved-citation")

    def to_json(self) -> dict[str, Any]:
        """JSON report."""
        return {
            "schema": SCHEMA_DRIFT,
            "generated_at": now_iso(),
            "repo_head": self.repo_head,
            "baseline_source": self.baseline_source,
            "counts": self.counts(),
            "unmapped_total": self.unmapped_total,
            "findings": self.findings,
        }


def derive_main_source_dirs(manifest: DomainManifest) -> list[str]:
    """Longest common directory prefix per top-level module of all mapped source files."""
    groups: dict[str, list[str]] = {}
    for dom in manifest.domains:
        for path in dom.source_paths:
            parent = posixpath.dirname(path)
            if parent:
                groups.setdefault(parent.split("/", 1)[0], []).append(parent)
    dirs = []
    for module, parents in sorted(groups.items()):
        common = posixpath.commonpath(parents) if parents else module
        dirs.append(common or module)
    return sorted(set(dirs))


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return proc.returncode, proc.stdout


def _load(path: Path, schema: str) -> dict[str, Any]:
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise BcpInputError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise BcpInputError(f"{path}: not a {schema} file")
    return data


def _baseline_tokens(entry: dict[str, Any], extensions: list[str], source: str) -> list[tuple[Token, str]]:
    """(token, baseline state) pairs from a worksheet claim or a citation-baseline claim."""
    pairs: list[tuple[Token, str]] = []
    if source == "worksheet":
        for cand in entry.get("candidates", []):
            pairs.append((token_from_entry(cand, extensions), str(cand.get("status"))))
        for raw in entry.get("unresolved", []):
            tok = classify(str(raw), extensions, None)
            if tok.kind != "text" and not is_non_code(tok):
                pairs.append((tok, UNRESOLVED))
    else:
        for tok_entry in entry.get("tokens", []):
            pairs.append((token_from_entry(tok_entry, extensions), str(tok_entry.get("status"))))
    return pairs


def check_drift(
    bre: Path,
    repo: Path,
    config: BcpConfig,
    review_dir: Path | None = None,
    baseline: Path | None = None,
    loc_threshold: float = 0.2,
    use_git: bool = True,
    git_history: str = "high",
) -> DriftReport:
    """Compute every drift finding.

    ``git_history`` selects which citation findings run the read-only ``git log -S`` check:
    ``high`` (default; HIGH findings only, because it is slow on long histories), ``all`` or ``none``.
    """
    bre, repo = Path(bre), Path(repo)
    if git_history not in ("high", "all", "none"):
        raise BcpInputError("--git-history must be high, all or none")
    manifest = load_manifest(bre)
    layout = BreLayout(bre)
    exclude = list(config.exclude_dirs)
    try:
        exclude.append(Path(bre).resolve().relative_to(repo.resolve()).parts[0])
    except (ValueError, IndexError):
        pass
    index = SourceIndex(repo, config.source_extensions, exclude)
    report = DriftReport(git_history=git_history)
    manifest_paths = [p for d in manifest.domains for p in d.source_paths]
    git_repo = repo if use_git else None

    # files: missing-file and loc-changed
    for dom in manifest.domains:
        for sf in dom.source_files:
            full = repo / sf.path
            if not full.is_file():
                report.add("HIGH", "missing-file", dom.slug, f"{sf.path} no longer exists", path=sf.path)
                continue
            if sf.loc:
                current = len(read_text(full).splitlines())
                if abs(current - sf.loc) / sf.loc > loc_threshold:
                    report.add("MEDIUM", "loc-changed", dom.slug,
                               f"{sf.path}: {sf.loc} → {current} lines", path=sf.path, loc=sf.loc, current=current)

    # citation baselines
    worksheets: dict[str, dict[str, Any]] = {}
    if review_dir is not None and Path(review_dir).is_dir():
        for path in sorted(Path(review_dir).glob("claims-*.json")):
            sheet = _load(path, SCHEMA_CLAIMS)
            worksheets[str(sheet.get("domain"))] = sheet
    if baseline is None and (repo / ".kiro/context/citation-baseline.json").is_file():
        baseline = repo / ".kiro/context/citation-baseline.json"
    base_claims: dict[str, Any] = {}
    if baseline is not None:
        if not Path(baseline).is_file():
            raise BcpInputError(f"baseline file not found: {baseline}")
        base_data = _load(Path(baseline), SCHEMA_BASELINE)
        base_claims = base_data.get("claims", {})
        report.repo_head = base_data.get("repo_head")
    sources = []
    if worksheets:
        sources.append("worksheets")
    if base_claims:
        sources.append("citation-baseline.json")
    report.baseline_source = " + ".join(sources) or "none"
    if not sources:
        log.info("no review worksheet and no citation baseline; citation findings are no-baseline only")

    for dom in manifest.domains:
        claims, _ = layout.load_claims(dom.slug, config)
        bre_file = layout.domain_file(dom.slug, "business-rules.md", warn=False)
        bre_dir = layout.rel(bre_file.parent) if bre_file else None
        sheet_claims = {c["id"]: c for c in worksheets.get(dom.slug, {}).get("claims", [])}
        for claim in claims:
            claim_id = f"{dom.slug}/{claim.rule_id}"
            if claim_id in sheet_claims:
                pairs = _baseline_tokens(sheet_claims[claim_id], config.source_extensions, "worksheet")
            elif claim_id in base_claims:
                pairs = _baseline_tokens(base_claims[claim_id], config.source_extensions, "baseline")
            else:
                res = resolve_claim(index, claim, dom.source_paths, manifest_paths, bre, bre_dir)
                for tok, r in res.missing:
                    if is_non_code(tok):
                        continue
                    inv = investigate(index, tok, r, git_repo if git_history == "all" else None)
                    report.add("INFO", "no-baseline-unresolved", dom.slug,
                               f"{claim_id}: `{tok.raw}` is {r.status} and has no baseline; {why_line(inv)}",
                               claim=claim_id, token=tok.raw, investigation=inv)
                continue
            for tok, base_state in pairs:
                cur = index.resolve(tok, dom.source_paths, manifest_paths, bre, bre_dir)
                _citation_finding(report, index, dom.slug, claim_id, tok, base_state, cur, git_repo)

    # review evidence
    if review_dir is not None and (Path(review_dir) / "review-record.json").is_file():
        record = load_review_record(Path(review_dir) / "review-record.json")
        report.repo_head = record.get("repo_head") or report.repo_head
        for claim_id, entry in sorted(record.get("claims", {}).items()):
            slug = claim_id.split("/", 1)[0]
            for ev in entry.get("evidence", []):
                if ev.get("kind") == "range":
                    _range_finding(report, repo, slug, claim_id, entry, ev)
                elif ev.get("kind") == "search":
                    _search_finding(report, index, slug, claim_id, entry, ev)

    # unmapped files
    prefixes = config.main_source_dirs or derive_main_source_dirs(manifest)
    mapped = set(manifest_paths)
    unmapped = [p for p in index.paths if p not in mapped and any(p.startswith(d + "/") for d in prefixes)]
    report.unmapped_total = len(unmapped)
    for path in unmapped[:MAX_UNMAPPED]:
        report.add("LOW", "unmapped-file", None, f"{path} is under a main source directory but in no domain",
                   path=path)

    # changed since review (optional git)
    if report.repo_head and use_git:
        code, out = _git(repo, "diff", "--name-only", report.repo_head, "HEAD")
        if code != 0:
            log.info("git diff against %s unavailable; changed-since-review skipped", report.repo_head)
        else:
            file_domains: dict[str, list[str]] = {}
            for dom in manifest.domains:
                for p in dom.source_paths:
                    file_domains.setdefault(p, []).append(dom.slug)
            for path in sorted(line.strip() for line in out.splitlines() if line.strip()):
                if path in file_domains:
                    report.add("INFO", "changed-since-review", ",".join(file_domains[path]),
                               f"{path} changed since {report.repo_head[:12]}", path=path)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    report.findings.sort(key=lambda f: (f.get("domain") or "~", order[f["severity"]], f["kind"], f["message"]))
    return report


def _citation_finding(
    report: DriftReport,
    index: SourceIndex,
    slug: str,
    claim_id: str,
    tok: Token,
    base: str,
    cur: Any,
    git_repo: Path | None,
) -> None:
    """Apply the baseline-vs-current table (including N2)."""
    now = cur.status
    if now == "bre-doc":
        return
    if base == RESOLVED and now == RESOLVED:
        return
    if base in (METHOD_MISSING, UNRESOLVED) and now == RESOLVED:
        report.add("INFO", "citation-now-resolves", slug, f"{claim_id}: `{tok.raw}` now resolves to {cur.path}",
                   claim=claim_id, token=tok.raw)
        return
    high = base == RESOLVED or (base == METHOD_MISSING and now == UNRESOLVED)
    use_git = git_repo if (high or report.git_history == "all") and report.git_history != "none" else None
    inv = investigate(index, tok, cur, use_git)
    why = why_line(inv)
    if high:
        detail = "was resolved at baseline" if base == RESOLVED else "class file existed at baseline"
        report.add("HIGH", "unresolved-citation", slug, f"{claim_id}: `{tok.raw}` {detail}, now {now}; {why}",
                   claim=claim_id, token=tok.raw, baseline=base, current=now, investigation=inv)
        return
    note = "class now exists, method still missing" if (base == UNRESOLVED and now == METHOD_MISSING) else now
    report.add("INFO", "pre-existing-unresolved", slug, f"{claim_id}: `{tok.raw}` already {base} at baseline ({note}); {why}",
               claim=claim_id, token=tok.raw, baseline=base, current=now, investigation=inv)


def _range_finding(report: DriftReport, repo: Path, slug: str, claim_id: str, entry: dict[str, Any],
                   ev: dict[str, Any]) -> None:
    try:
        full = safe_join(repo, ev["path"])
    except BcpInputError:
        return
    verdict = entry.get("verdict")
    if not full.is_file():
        report.add("HIGH", "evidence-moved", slug, f"{claim_id} ({verdict}): evidence file {ev['path']} is gone",
                   claim=claim_id, path=ev["path"])
        return
    lines = read_text(full).replace("\r\n", "\n").split("\n")
    quote = str(ev.get("quote", "")).split("\n")
    start = int(ev["start"])
    if lines[start - 1:start - 1 + len(quote)] == quote:
        return
    for i in range(len(lines) - len(quote) + 1):
        if lines[i:i + len(quote)] == quote:
            report.add("MEDIUM", "evidence-moved", slug,
                       f"{claim_id} ({verdict}): evidence {ev['path']}:L{start} moved to L{i + 1}",
                       claim=claim_id, path=ev["path"], line=i + 1)
            return
    report.add("HIGH", "evidence-moved", slug, f"{claim_id} ({verdict}): evidence quote at {ev['path']}:L{start} not found",
               claim=claim_id, path=ev["path"])


def _search_finding(report: DriftReport, index: SourceIndex, slug: str, claim_id: str, entry: dict[str, Any],
                    ev: dict[str, Any]) -> None:
    verdict = entry.get("verdict")
    files = index.glob_scope(str(ev.get("scope")))
    if not files:
        report.add("HIGH", "evidence-changed", slug, f"{claim_id} ({verdict}): search scope {ev.get('scope')} matches no files",
                   claim=claim_id)
        return
    regex = re.compile(str(ev.get("pattern")))
    count = sum(1 for rel in files for line in index.lines(rel) if regex.search(line))
    if count != ev.get("hit_count"):
        report.add("HIGH", "evidence-changed", slug,
                   f"{claim_id} ({verdict}): search /{ev.get('pattern')}/ in {ev.get('scope')} had "
                   f"{ev.get('hit_count')} hits, now {count}", claim=claim_id)
        return
    old = ev.get("files_sha256") or {}
    current = {rel: sha256_file(index.repo / rel) for rel in files}
    if current != old:
        report.add("INFO", "evidence-changed", slug,
                   f"{claim_id} ({verdict}): searched files changed but the hit count ({count}) did not",
                   claim=claim_id)


def render_drift_md(report: DriftReport) -> str:
    """Markdown report grouped by domain with recommended actions."""
    counts = report.counts()
    lines = [
        "# Drift report",
        "",
        f"- Baseline: {report.baseline_source}",
        f"- Repo HEAD at baseline: {report.repo_head or 'unknown'}",
        "- Findings: " + ", ".join(f"{s} {counts[s]}" for s in SEVERITIES),
        f"- Unmapped files: {report.unmapped_total}" + (f" (first {MAX_UNMAPPED} listed)" if report.unmapped_total > MAX_UNMAPPED else ""),
        "",
        "HIGH `unresolved-citation` is real drift (a cited method, class or file that resolved before is gone). "
        "INFO `pre-existing-unresolved` is an extraction citation that never pointed at a method.",
        "",
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in report.findings:
        groups.setdefault(f.get("domain") or "(no domain)", []).append(f)
    for domain in sorted(groups):
        lines += [f"## {domain}", "", "| Severity | Finding | Detail | Action |", "|---|---|---|---|"]
        for f in groups[domain]:
            lines.append(f"| {f['severity']} | {f['kind']} | {md_cell(f['message'])} | {md_cell(f['action'])} |")
        lines.append("")
    if not report.findings:
        lines.append("No drift found.")
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- release drift list

SCHEMA_RELEASE = "bcp.release-drift/1"
FILE_INDEX_REL = ".kiro/context/file-index.json"
BASELINE_REL = ".kiro/context/citation-baseline.json"


def _porcelain_paths(out: str) -> list[str]:
    paths = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


def release_drift(bre: Path, repo: Path, config: BcpConfig, base: str | None = None,
                  review_dir: Path | None = None) -> dict[str, Any]:
    """Files changed since the context was generated (read-only git) → domains → claims to re-review.

    The base is ``citation-baseline.json``'s ``repo_head`` unless ``base`` is given. Changed files are
    ``git diff --name-only <base>..HEAD`` plus uncommitted files from ``git status --porcelain``.
    """
    bre, repo = Path(bre), Path(repo)
    index_path = repo / FILE_INDEX_REL
    if not index_path.is_file():
        raise BcpInputError(f"{FILE_INDEX_REL} not found under {repo}; run bcp generate first")
    file_index: dict[str, list[str]] = json.loads(read_text(index_path))
    baseline_claims: dict[str, Any] = {}
    head = None
    if (repo / BASELINE_REL).is_file():
        data = _load(repo / BASELINE_REL, SCHEMA_BASELINE)
        baseline_claims = data.get("claims", {})
        head = data.get("repo_head")
    base = base or head
    if not base:
        raise BcpInputError("no base revision: citation-baseline.json has no repo_head; pass --base <rev>")
    code, out = _git(repo, "diff", "--name-only", f"{base}..HEAD")
    if code != 0:
        raise BcpInputError(f"git diff {base}..HEAD failed in {repo} (not a git repository, or unknown revision)")
    committed = sorted({p.strip() for p in out.splitlines() if p.strip()})
    code, status_out = _git(repo, "status", "--porcelain")
    uncommitted = sorted(set(_porcelain_paths(status_out))) if code == 0 else []
    changed = [(p, "committed") for p in committed] + [(p, "uncommitted") for p in uncommitted if p not in committed]
    changed = [(p, how) for p, how in changed if not p.startswith(".kiro/")]
    verdicts: dict[str, str] = {}
    review_path = Path(review_dir) / "review-record.json" if review_dir else repo / ".kiro/bre-review/review-record.json"
    if review_path.is_file():
        record = load_review_record(review_path)
        verdicts = {cid: str(e.get("verdict")) for cid, e in record.get("claims", {}).items()}
    by_path: dict[str, set[str]] = {}
    for claim_id, entry in baseline_claims.items():
        for tok in entry.get("tokens", []):
            if tok.get("path"):
                by_path.setdefault(str(tok["path"]), set()).add(claim_id)
    layout = BreLayout(bre)
    rows = []
    domains_hit: dict[str, list[str]] = {}
    for path, how in changed:
        domains = file_index.get(path, [])
        claims = [{"claim": c, "status": verdicts.get(c, "unreviewed")} for c in sorted(by_path.get(path, set()))]
        rows.append({"path": path, "change": how, "domains": domains, "claims": claims})
        for slug in domains:
            domains_hit.setdefault(slug, [])
            for c in claims:
                if c["claim"] not in domains_hit[slug]:
                    domains_hit[slug].append(c["claim"])
    docs = {}
    for slug in sorted(domains_hit):
        folder = layout.domain_dir(slug)
        docs[slug] = sorted(layout.rel(p) for p in folder.glob("*.md")) if folder is not None else []
    return {
        "schema": SCHEMA_RELEASE,
        "generated_at": now_iso(),
        "base": base,
        "base_source": "--base" if base != head else "citation-baseline.json repo_head",
        "changed_files": rows,
        "unmapped_changed": [r["path"] for r in rows if not r["domains"]],
        "domains": {slug: {"claims": domains_hit[slug], "bre_docs": docs[slug]} for slug in sorted(domains_hit)},
    }


def render_release_md(data: dict[str, Any]) -> str:
    """Markdown release drift list."""
    rows = data["changed_files"]
    lines = [
        "# Release drift list",
        "",
        f"- Base: `{data['base']}` ({data['base_source']})",
        f"- Changed files: {len(rows)} ({sum(1 for r in rows if r['change'] == 'uncommitted')} uncommitted), "
        f"{len(data['unmapped_changed'])} in no domain",
        f"- Domains touched: {', '.join(data['domains']) or 'none'}",
        "",
        "Re-review the listed claims against the new code, then re-check the domain's extraction documents. "
        "Git was used read-only.",
        "",
        "## Changed files",
        "",
        "| File | Change | Domains | Claims citing it (status) |",
        "|---|---|---|---|",
    ]
    for row in rows:
        claims = ", ".join(f"{c['claim']} ({c['status']})" for c in row["claims"]) or "-"
        lines.append(f"| `{md_cell(row['path'])}` | {row['change']} | {md_cell(', '.join(row['domains']) or '-')} | "
                     f"{md_cell(claims)} |")
    if not rows:
        lines.append("| - | - | - | no changes since the base |")
    lines += ["", "## Per domain", ""]
    for slug, item in data["domains"].items():
        lines += [f"### `{slug}`", "", "- Claims to re-review: " + (", ".join(item["claims"]) or "none cite the files"),
                  "- Extraction documents to re-check: " + (", ".join(f"`{d}`" for d in item["bre_docs"]) or "none"), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write_release(data: dict[str, Any], out: Path) -> list[Path]:
    """Write ``release-drift.md`` and ``release-drift.json``."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "release-drift.md", out / "release-drift.json"
    atomic_write_text(md, render_release_md(data))
    atomic_write_text(js, dump_json(data))
    return [md, js]


def write_reports(report: DriftReport, out: Path) -> list[Path]:
    """Write ``drift-report.md`` and ``drift-report.json``."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "drift-report.md", out / "drift-report.json"
    atomic_write_text(md, render_drift_md(report))
    atomic_write_text(js, dump_json(report.to_json()))
    return [md, js]
