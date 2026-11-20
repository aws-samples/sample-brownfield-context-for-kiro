"""Generation orchestration: BRE + review record → ``<target>/.kiro/`` (staged, then committed)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bcp.agents import SPECIALISTS, plan_agents, render_agents, render_kb_files
from bcp.bre import BreLayout
from bcp.config import BcpConfig, check_agent_options, load_config
from bcp.errata import ERRATA_CAP_BYTES, build_errata, render_errata_md
from bcp.errors import BcpInputError, OutputWriteError
from bcp.glossary import render_glossary_md
from bcp.precheck import load_precheck
from bcp.impact import build_file_index, build_impact, render_impact_md
from bcp.manifest import DomainManifest, load_manifest
from bcp.render import (
    CONTEXT_DIR,
    ERRATA_FILE,
    FOUNDATION,
    GENERATED_MARKER,
    NARROW_CAPPED,
    STEERING_DIR,
    TRUST_FILE,
    RenderContext,
    always_files,
    derived_doc,
    render_agent_pointer,
    render_cross_steering,
    render_domain_steering,
    render_foundation,
    render_framework_rules,
    render_review_status,
    render_skills,
    render_summary,
    render_trust_legend,
    reviewed_rules,
)
from bcp.review import git_head, load_review_dir, now_iso
from bcp.sourceindex import SourceIndex, resolve_claim
from bcp.textio import dump_json, read_text, replace_with_retry, sha256_bytes, sha256_file, to_posix
from bcp.variants import build_variants, render_variants_md
from bcp.deps import build_deps, render_deps_md
from bcp.programs import build_program_config, load_snapshots, render_program_config_md, snapshot_files
from bcp.render import DEPS_FILE, PROGRAMS_FILE, marked_doc
from bcp.trust import TIER1, TIER2

log = logging.getLogger(__name__)

TRUST_DEPS = ("code-derived edges and locations are mechanical; rows marked bre-only are extraction claims "
              "(Tier 3) to verify in the code")
TRUST_PROGRAMS = ("program values imported read-only from configuration exports; behaviour notes come from code "
                  "scans and extraction rules; check the code")

RECORD_REL = f"{CONTEXT_DIR}/generation-record.json"
BASELINE_REL = f"{CONTEXT_DIR}/citation-baseline.json"
BRE_COPY_REL = f"{CONTEXT_DIR}/bre"
SCHEMA_BASELINE = "bcp.citations/1"
COPY_SUFFIXES = (".md", ".json", ".sql")


@dataclass
class GenerateResult:
    """Summary of one generate run."""

    files: dict[str, int] = field(default_factory=dict)
    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    always_bytes: int = 0
    domains: int = 0
    rules: int = 0
    dry_run: bool = False
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """JSON-friendly summary."""
        return {
            "agents": self.agents,
            "files": len(self.files),
            "written": len(self.written),
            "kept_hand_edited": self.kept,
            "deleted": self.deleted,
            "always_bytes": self.always_bytes,
            "domains": self.domains,
            "rules": self.rules,
            "dry_run": self.dry_run,
        }


def _bre_prefix(bre: Path, target: Path) -> str | None:
    """Workspace-relative BRE path when the BRE is inside the target, else None."""
    try:
        rel = Path(os.path.abspath(bre)).relative_to(Path(os.path.abspath(target)))
    except ValueError:
        return None
    return to_posix(rel) if str(rel) != "." else ""


def _has_marker(path: Path) -> bool:
    try:
        return GENERATED_MARKER in read_text(path)
    except OSError:
        return False


def _foundation_files(target: Path) -> dict[str, str]:
    names = {}
    for key in FOUNDATION:
        dest = target / STEERING_DIR / f"{key}.md"
        if dest.exists() and not _has_marker(dest):
            log.warning("%s/%s.md exists and was not generated; writing bre-%s.md instead", STEERING_DIR, key, key)
            names[key] = f"bre-{key}.md"
        else:
            names[key] = f"{key}.md"
    return names


def _load_json_quiet(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError:
        log.warning("%s is not valid JSON; ignored", path.name)
        return None
    return data if isinstance(data, dict) else None


def build_baseline(
    manifest: DomainManifest,
    layout: BreLayout,
    config: BcpConfig,
    index: SourceIndex,
    claims_by_domain: dict[str, list[Any]],
    old: dict[str, Any] | None,
    repo_head: str | None,
) -> dict[str, Any]:
    """Citation baseline for every domain; unchanged claims keep their old entry (sticky)."""
    old_claims = (old or {}).get("claims", {}) if (old or {}).get("schema") == SCHEMA_BASELINE else {}
    manifest_paths = [p for d in manifest.domains for p in d.source_paths]
    claims_out: dict[str, Any] = {}
    stamp = now_iso()
    for dom in manifest.domains:
        bre_file = layout.domain_file(dom.slug, "business-rules.md", warn=False)
        bre_dir = layout.rel(bre_file.parent) if bre_file else None
        for claim in claims_by_domain.get(dom.slug, []):
            claim_id = f"{dom.slug}/{claim.rule_id}"
            prior = old_claims.get(claim_id)
            if prior and prior.get("body_sha256") == claim.body_sha256:
                claims_out[claim_id] = prior
                continue
            res = resolve_claim(index, claim, dom.source_paths, manifest_paths, layout.root, bre_dir)
            tokens = []
            for cand in res.candidates:
                tokens.append({k: cand[k] for k in ("token", "kind", "bound", "status", "path", "line",
                                                    "class", "method", "package_path") if k in cand})
            for tok, r in res.missing:
                if tok.raw in res.unresolved:
                    entry = {"token": tok.raw, "kind": tok.kind, "bound": tok.bound, "status": r.status,
                             "path": None, "line": None}
                    for key, val in (("class", tok.cls), ("method", tok.method), ("package_path", tok.package_path)):
                        if val:
                            entry[key] = val
                    tokens.append(entry)
            claims_out[claim_id] = {"body_sha256": claim.body_sha256, "recorded_at": stamp, "tokens": tokens}
    head = (old or {}).get("repo_head") if old_claims and all(
        claims_out[c] is old_claims.get(c) for c in claims_out
    ) else repo_head
    return {"schema": SCHEMA_BASELINE, "repo_head": head, "claims": dict(sorted(claims_out.items()))}


def _stage(staging: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = staging / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _encode(text: str) -> bytes:
    return text.replace("\r\n", "\n").encode("utf-8")


def generate(
    bre: Path,
    target: Path,
    review_dir: Path | None = None,
    config: BcpConfig | Path | None = None,
    unreviewed: str = "flag",
    no_copy_bre: bool = False,
    force: bool = False,
    refresh_baseline: bool = False,
    dry_run: bool = False,
) -> GenerateResult:
    """Run the 9-step generation flow and return a summary."""
    bre, target = Path(bre), Path(target)
    if unreviewed not in ("flag", "exclude"):
        raise BcpInputError("--unreviewed must be flag or exclude")
    if not target.is_dir():
        raise BcpInputError(f"target folder not found: {target}")
    cfg = config if isinstance(config, BcpConfig) else load_config(config)
    check_agent_options(cfg)
    # 1-2. manifest and layout
    manifest = load_manifest(bre)
    layout = BreLayout(bre)
    # 3. review
    review = load_review_dir(review_dir)
    # BRE location
    prefix = _bre_prefix(bre, target)
    copy_bre = prefix is None
    if copy_bre and no_copy_bre:
        raise BcpInputError("the BRE folder is outside --target and --no-copy-bre was given")
    bre_prefix = BRE_COPY_REL if copy_bre else prefix or ""
    ctx = RenderContext(manifest, layout, cfg, bre_prefix, review, unreviewed, _foundation_files(target))
    # 4. claims
    claims_by_domain: dict[str, list[Any]] = {}
    known_ids: set[str] = set()
    for dom in manifest.domains:
        parsed, _ = layout.load_claims(dom.slug, cfg)
        claims_by_domain[dom.slug] = parsed
        known_ids.update(f"{dom.slug}/{c.rule_id}" for c in parsed)
        if dom.rule_count is not None and dom.rule_count != len(parsed):
            log.info("domain %s: parsed %d rules, manifest rule_count %d", dom.slug, len(parsed), dom.rule_count)
    unknown = sorted(set(review.record.get("claims", {})) - known_ids)
    if unknown:
        log.warning("review record has %d claim id(s) not in the BRE (ignored): %s", len(unknown), ", ".join(unknown[:10]))
    # 5. source index (N3)
    exclude = list(cfg.exclude_dirs)
    if prefix:
        exclude.append(Path(prefix).parts[0])
    index: SourceIndex | None = SourceIndex(target, cfg.source_extensions, exclude)
    if not index.has_source():
        log.warning("no source files under target; citation baseline and variant code scan skipped")
        index = None
    file_index = build_file_index(manifest)
    impact = build_impact(manifest, layout, bre_prefix)
    variants = build_variants(layout, cfg, manifest.slugs, index, file_index, bre_prefix)
    # 6. render
    texts: dict[str, str] = {}
    texts.update(render_foundation(ctx))
    per_domain: dict[str, dict[str, Any]] = {}
    for dom in manifest.ordered_domains():
        claims = claims_by_domain[dom.slug]
        rules_text, counts = reviewed_rules(dom, claims, review, unreviewed, index,
                                            anchors=ctx.design == NARROW_CAPPED)
        per_domain[dom.slug] = counts
        texts[f"{CONTEXT_DIR}/domains/{dom.slug}/reviewed-rules.md"] = rules_text
        texts[f"{CONTEXT_DIR}/domains/{dom.slug}/summary.md"] = render_summary(ctx, dom, len(claims), counts)
        texts.update(render_domain_steering(ctx, dom))
    texts[f"{CONTEXT_DIR}/impact-map.md"] = derived_doc(
        render_impact_md(impact, manifest, f"{CONTEXT_DIR}/file-index.json")
    )
    texts[f"{CONTEXT_DIR}/impact-map.json"] = dump_json(impact)
    texts[f"{CONTEXT_DIR}/file-index.json"] = dump_json(file_index)
    texts[f"{CONTEXT_DIR}/variant-map.md"] = derived_doc(render_variants_md(variants))
    texts[f"{CONTEXT_DIR}/variant-map.json"] = dump_json(variants)
    deps = build_deps(manifest, cfg, index, file_index, impact)
    focus = [s for s in cfg.agent_domains or [] if manifest.domain(s) is not None] or None
    texts[f"{CONTEXT_DIR}/{DEPS_FILE}.md"] = marked_doc(render_deps_md(deps, focus), TIER1, TRUST_DEPS)
    texts[f"{CONTEXT_DIR}/{DEPS_FILE}.json"] = dump_json(deps)
    if cfg.program_snapshots:
        snaps = [Path(p) for p in cfg.program_snapshots]
        programs = load_snapshots(snaps, cfg.variant_mask_patterns)
        prog_data = build_program_config(layout, manifest, cfg, index, file_index, programs, None, variants,
                                         [p.name for p in snapshot_files(snaps)])
        texts[f"{CONTEXT_DIR}/{PROGRAMS_FILE}.md"] = marked_doc(render_program_config_md(prog_data), TIER2,
                                                                TRUST_PROGRAMS)
        texts[f"{CONTEXT_DIR}/{PROGRAMS_FILE}.json"] = dump_json(prog_data)
    texts[f"{CONTEXT_DIR}/glossary.md"] = derived_doc(render_glossary_md(manifest, cfg.glossary_extra))
    texts[f"{CONTEXT_DIR}/review-status.md"] = render_review_status(ctx, per_domain)
    precheck = load_precheck(review_dir)
    agent_first = [s for s in cfg.agent_domains or [] if manifest.domain(s) is not None]
    errata = build_errata(review, claims_by_domain, layout, precheck, bre_prefix,
                          agent_first + [d.slug for d in manifest.ordered_domains() if d.slug not in agent_first])
    texts[f"{CONTEXT_DIR}/{ERRATA_FILE}"] = render_errata_md(errata, ERRATA_CAP_BYTES)
    texts[f"{CONTEXT_DIR}/{TRUST_FILE}"] = render_trust_legend()
    texts.update(render_framework_rules(ctx))
    texts.update(render_cross_steering(ctx))
    texts.update(render_skills(ctx))
    texts.update(render_agent_pointer(ctx, [s.name for s in SPECIALISTS]))
    if index is not None:
        old = None if refresh_baseline else _load_json_quiet(target / BASELINE_REL)
        baseline = build_baseline(manifest, layout, cfg, index, claims_by_domain, old, git_head(target))
        texts[BASELINE_REL] = dump_json(baseline)
    files: dict[str, bytes] = {rel: _encode(text) for rel, text in texts.items()}
    if copy_bre:
        for path in sorted(bre.rglob("*")):
            if path.is_file() and path.suffix.lower() in COPY_SUFFIXES:
                files[f"{BRE_COPY_REL}/{to_posix(path.relative_to(bre))}"] = path.read_bytes()

    def size_of(rel: str) -> int | None:
        if rel in files:
            return len(files[rel])
        path = target / rel
        return path.stat().st_size if path.is_file() else None

    plans = plan_agents(ctx, size_of)
    files.update({rel: _encode(text) for rel, text in render_agents(plans).items()})
    if cfg.kb:
        graph_rel = str(cfg.kb["graph"])
        if not (Path(graph_rel) if Path(graph_rel).is_absolute() else target / graph_rel).is_file():
            log.warning("kb.graph %s not found under the target; the hook will run without the code graph "
                        "until bcp graph build writes it", graph_rel)
        files.update({rel: _encode(text) for rel, text in render_kb_files(cfg.kb).items()})
    files = dict(sorted(files.items()))
    result = GenerateResult(files={rel: len(data) for rel, data in files.items()},
                            domains=len(manifest.domains),
                            rules=sum(len(v) for v in claims_by_domain.values()),
                            dry_run=dry_run,
                            agents={p.name: p.summary() for p in plans})
    # 7. always budget
    always = always_files(ctx)
    result.always_bytes = sum(len(files[p]) for p in always)
    if result.always_bytes > cfg.always_budget_bytes:
        sizes = ", ".join(f"{p.rsplit('/', 1)[-1]}={len(files[p])}" for p in always)
        # Advisory only: Kiro publishes no steering size (https://kiro.dev/docs/steering/).
        log.warning("always-included steering is %d bytes, over the advisory always_budget_bytes %d (a bcp "
                    "heuristic, not a Kiro limit; %s); move reference material to fileMatch, auto or manual "
                    "steering or a skill (https://kiro.dev/docs/steering/)",
                    result.always_bytes, cfg.always_budget_bytes, sizes)
    if dry_run:
        return result
    # 6 (staging) + 8. commit
    staging = target / ".kiro" / f".bcp-staging-{os.getpid()}"
    try:
        if staging.exists():
            shutil.rmtree(staging)
        _stage(staging, files)
        _commit(target, staging, files, force, result)
    except OSError as exc:
        raise OutputWriteError(f"write failed: {exc}; staging kept at {staging}") from exc
    shutil.rmtree(staging, ignore_errors=True)
    return result


def _commit(target: Path, staging: Path, files: dict[str, bytes], force: bool, result: GenerateResult) -> None:
    """Single write path: ownership checks first, then atomic replaces, stale removal, record last."""
    old_record = _load_json_quiet(target / RECORD_REL) or {}
    old_files: dict[str, str] = old_record.get("files", {}) if isinstance(old_record.get("files"), dict) else {}
    plan: list[str] = []
    for rel in files:
        dest = target / rel
        if not dest.exists():
            plan.append(rel)
            continue
        in_record = rel in old_files
        owned = in_record if rel.endswith(".json") else (in_record or _has_marker(dest))
        if not owned:
            raise OutputWriteError(f"refusing to overwrite a file bcp did not generate: {rel}")
        if in_record and sha256_file(dest) != old_files[rel] and not force:
            if sha256_file(dest) == sha256_bytes(files[rel]):
                plan.append(rel)
                continue
            log.warning("%s was edited by hand; kept (use --force to overwrite)", rel)
            result.kept.append(rel)
            continue
        plan.append(rel)
    for rel in plan:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.bcp-tmp")
        shutil.copyfile(staging / rel, tmp)
        replace_with_retry(tmp, dest)
        result.written.append(rel)
    for rel in sorted(set(old_files) - set(files)):
        dest = target / rel
        if not dest.exists():
            continue
        if force or sha256_file(dest) == old_files[rel] or (rel.endswith(".md") and _has_marker(dest)):
            dest.unlink()
            result.deleted.append(rel)
        else:
            log.warning("stale generated file %s was edited by hand; kept", rel)
            result.kept.append(rel)
    record_files = {
        rel: (old_files[rel] if rel in result.kept else sha256_file(target / rel))
        for rel in files
        if (target / rel).exists()
    }
    for rel in result.kept:  # kept stale files stay owned so a later run can still remove them
        if rel not in record_files and rel in old_files:
            record_files[rel] = old_files[rel]
    record = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": dict(sorted(record_files.items())),
    }
    dest = target / RECORD_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.bcp-tmp")
    tmp.write_bytes(dump_json(record).encode("utf-8"))
    replace_with_retry(tmp, dest)
