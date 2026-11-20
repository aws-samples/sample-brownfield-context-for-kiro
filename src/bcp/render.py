"""Render Kiro steering, context files, workspace skills and the custom agent.

Every function returns ``{workspace-relative path: text}``; nothing here touches the disk
except reading templates. Review-policy filtering lives only in :func:`reviewed_rules`.
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

from bcp import frontmatter
from bcp.bre import BreLayout
from bcp.claims import Claim
from bcp.config import DEFAULT_AGENT_DESIGN, BcpConfig
from bcp.manifest import Domain, DomainManifest
from bcp.review import ReviewData, counterexample_found, effective_status, sanitize_replacement
from bcp.sourceindex import SourceIndex, evidence_anchor
from bcp.textio import first_sentence, md_cell, read_text, truncate_words
from bcp.trust import TIER1, TIER2, render_legend, tier_tag, trust_line

log = logging.getLogger(__name__)

GENERATED_MARKER = (
    "<!-- generated-by: brownfield-context-power; do not edit by hand; regenerate with bcp generate -->"
)
PROVENANCE_EXTRACTED = (
    "> Provenance: the overview and integrations below are AWS Transform extraction output, not "
    "reviewed claim by claim. Treat them as unconfirmed; only rules in reviewed-rules.md carry review status."
)
PROVENANCE_DERIVED = (
    "> Provenance: derived from the business-rules extraction manifest and inventories; not SME-reviewed."
)
DRAFT_LINE = (
    "Draft generated from the business-rules extraction. An architect reviews this file before it is relied on."
)
NO_DOMAIN_FILES = "No detailed BRE files for this domain."
NO_RULES = "No business rules extracted for this domain."
CONTEXT_DIR = ".kiro/context"
STEERING_DIR = ".kiro/steering"
FOUNDATION = ("product", "tech", "structure")
INDEX_FILE = "bre-context-index.md"
POINTER_FILE = "bre-use-brownfield-agent.md"
SKILLS: dict[str, str] = {
    "onboarding-guide": (
        "Guide a new team member through the system: domains in reading order, the business flows that "
        "cross them, key business rules by BR id, entry classes and self-check questions. Use for "
        "onboarding, 'how does this system work', 'where does flow X live' or architecture overviews."
    ),
    "change-impact-analysis": (
        "Analyse which domains, components, shared tables and configuration or feature-flag variants a "
        "proposed change affects, with evidence, rules at risk and variants to retest. Use for impact "
        "analysis, regression risk, hidden dependencies or 'what else does this change break'."
    ),
    "requirement-gap-check": (
        "Decide whether a requirement or user story already exists, partly exists or is new, by checking "
        "extracted features (F-n), business rules (BR-n), configuration variants and the code. Use for "
        "'does this already exist', backlog grooming or requirement analysis."
    ),
    "defect-triage": (
        "Trace a defect, error message or exception to its domain, business rule (BR id) and code, rank "
        "root-cause hypotheses, and check that a proposed fix fits the owning domain. Use for bug triage, "
        "defect traceability, root cause analysis or fix review."
    ),
    "scoped-test-generation": (
        "Write exactly the requested number of tests for a change, each tied to a business rule (BR id) "
        "and an acceptance criterion derived from it, and list the excluded candidates. Use for test "
        "generation, acceptance criteria or test planning with a fixed test count."
    ),
    "cross-component-dependencies": (
        "Map how components and domains depend on each other: interfaces, integration points, data flows "
        "through shared tables, and which edges the code confirms versus which the extraction only claims. "
        "Use for 'what depends on X', 'who calls this service', contracts between components or hidden "
        "coupling."
    ),
    "doc-drift-check": (
        "Compare documented behaviour (repository docs, exported wiki pages, the extraction) with the code, "
        "and list what changed since the context was generated. Use for 'are the docs still right', release "
        "readiness, stale documentation or 'what must be re-reviewed after this release'."
    ),
}
# Skills the default (hybrid) entry agent attaches in addition to its specialists' skills.
ENTRY_EXTRA_SKILLS = ("cross-component-dependencies", "doc-drift-check")
EXTRA_DOMAIN_FILES = ("features.md", "workflows.md", "validations.md")
MAX_CODE_READS = 3  # verification cap per answer (narrow-capped design only: agent prompts and skills)
NARROW_CAPPED = "narrow-capped"
DELEGATE = "delegate"
HOOK_ONLY = "hook-only"  # opt-in: entry agent answers from the hook's retrieved context, no sub-agent hop
KB_FIRST = "kb-first"  # opt-in: entry agent answers from verified-fresh facts, no code read for them
FAST_ENTRY = "brownfield-fast"  # opt-in extra entry agent (fast_entry_agent)
ERRATA_FILE = "errata.md"
FRAMEWORK_FILE = "framework-rules.md"
TRUST_FILE = "trust-tiers.md"
FRAMEWORK_STEERING = "bre-framework-rules.md"
DEPS_FILE = "dependency-map"  # .md and .json
PROGRAMS_FILE = "program-config"  # .md and .json, only when program_snapshots is configured

# Shared verification block: every skill gets it as ``$verification_rules``; agents.py builds its
# prompt sentences from the same wording.
VERIFICATION_RULES = "\n".join([
    "## Verification rules",
    f"- The context is a map; the code is the truth. Read `{CONTEXT_DIR}/{ERRATA_FILE}` first and never rely on "
    "an extraction passage it marks superseded.",
    "- Mandatory code check, whatever the review status: before you state anything about authorization or its "
    "absence, transactions, rollback, commit or error end states, concurrency or locking, an absolute claim "
    "(only, every, never, always, unique, none, no X), a count, or anything a diagram or workflow asserts, "
    "open the code and read the whole method, or say the point is unverified.",
    "- For authorization, read the annotations on both the interface and the implementation; an annotation "
    "can span several lines.",
    "- For error end states, trace the exception type to its catch sites and to the transaction boundary "
    f"(see `{CONTEXT_DIR}/{FRAMEWORK_FILE}`).",
    "- Tag each statement: `[code: path#Lx-Ly]`, `[reviewed context: <file> BR-n]` or "
    "`[unreviewed extraction, unverified: <file>]`.",
])


def load_template(rel: str, design: str = DEFAULT_AGENT_DESIGN) -> Template:
    """Load ``templates/<rel>`` from the package (works when the package is vendored). A non-default
    agent design uses ``templates/designs/<design>/<rel>`` when that override exists."""
    base = resources.files("bcp").joinpath("templates")
    path = base.joinpath(*rel.split("/"))
    if design != DEFAULT_AGENT_DESIGN:
        override = base.joinpath("designs", design, *rel.split("/"))
        if override.is_file():
            path = override
    return Template(path.read_text(encoding="utf-8"))


@dataclass
class RenderContext:
    """Inputs shared by every renderer."""

    manifest: DomainManifest
    layout: BreLayout
    config: BcpConfig
    bre_prefix: str
    review: ReviewData = field(default_factory=ReviewData)
    unreviewed_mode: str = "flag"
    foundation_files: dict[str, str] = field(default_factory=dict)

    @property
    def project_name(self) -> str:
        """Configured project name, else ``manifest.project.name``, else a placeholder."""
        return self.config.project_name or str(self.manifest.project.get("name") or "this project")

    def foundation(self, key: str) -> str:
        """Steering file name for a foundation file after the collision rule."""
        return self.foundation_files.get(key, f"{key}.md")

    @property
    def design(self) -> str:
        """The configured agent design (``delegate`` by default)."""
        return self.config.agent_design

    def bre_path(self, rel: str) -> str:
        """Workspace-relative path of a BRE-relative path."""
        return posixpath.join(self.bre_prefix, rel) if self.bre_prefix else rel

    def subst(self) -> dict[str, str]:
        """Common template substitutions."""
        return {
            "project_name": self.project_name,
            "context_dir": CONTEXT_DIR,
            "bre_dir": self.bre_prefix or ".",
            "draft_line": DRAFT_LINE,
            "max_code_reads": str(MAX_CODE_READS),
            "verification_rules": VERIFICATION_RULES,
        }


def steering(front: dict[str, object], body: str) -> str:
    """Front matter, the generated marker, then the body."""
    return frontmatter.emit(front) + GENERATED_MARKER + "\n\n" + body.rstrip("\n") + "\n"


TRUST_DERIVED = "derived from the extraction manifest and inventories; check the code before stating a detail"


def derived_doc(body: str, tier: int = TIER2, note: str = TRUST_DERIVED) -> str:
    """Marker + Trust line + derived provenance notice + body for derived context Markdown."""
    return (GENERATED_MARKER + "\n" + trust_line(tier, note) + "\n" + PROVENANCE_DERIVED + "\n\n"
            + body.rstrip("\n") + "\n")


def marked_doc(body: str, tier: int = TIER2, note: str = "") -> str:
    """Marker + Trust line + body (reviewed rules, review status, errata, legends)."""
    return GENERATED_MARKER + "\n" + trust_line(tier, note) + "\n\n" + body.rstrip("\n") + "\n"


def render_trust_legend() -> str:
    """``.kiro/context/trust-tiers.md``."""
    return marked_doc(render_legend(), TIER1, "legend of the trust tiers")


# --------------------------------------------------------------------------- foundation


def _common_dir(paths: list[str]) -> str:
    parents = [posixpath.dirname(p) for p in paths if p]
    if not parents:
        return ""
    try:
        return posixpath.commonpath(parents)
    except ValueError:
        return ""


def _wave_groups(ctx: RenderContext) -> list[tuple[str | None, list[Domain]]]:
    ordered = ctx.manifest.ordered_domains()
    waves = [d.stats.get("extraction_wave") for d in ordered]
    if ordered and all(isinstance(w, int) and not isinstance(w, bool) for w in waves):
        groups: dict[int, list[Domain]] = {}
        for dom in ordered:
            groups.setdefault(int(dom.stats["extraction_wave"]), []).append(dom)
        return [(f"Wave {w}", groups[w]) for w in sorted(groups)]
    return [(None, ordered)]


def render_foundation(ctx: RenderContext) -> dict[str, str]:
    """``product.md``, ``tech.md``, ``structure.md`` (always) and ``bre-context-index.md`` (auto, pinned
    into every agent). The per-domain directory lives in the index, not in ``product.md``."""
    man = ctx.manifest
    sub = ctx.subst()
    out: dict[str, str] = {}
    # product
    stats = man.project_stats
    facts = [f"{len(man.domains)} domains"]
    if isinstance(man.project.get("total_loc"), int):
        facts.append(f"{man.project['total_loc']:,} lines of code")
    if isinstance(man.project.get("total_source_files"), int):
        facts.append(f"{man.project['total_source_files']:,} source files")
    if isinstance(stats.get("rule_count"), int):
        facts.append(f"{stats['rule_count']} extracted business rules")
    summary_line = f"{ctx.project_name}: " + ", ".join(facts) + "."
    # The per-domain list lives in the auto-included context index, not here: product.md stays small so the
    # always-on overhead is core orientation only (https://kiro.dev/docs/steering/).
    read_more = [f"- Context index: `{STEERING_DIR}/{INDEX_FILE}`"]
    master = ctx.layout.root / "00-master-index.md"
    if master.is_file():
        read_more.insert(0, f"- Extraction master index: `{ctx.bre_path('00-master-index.md')}`")
    body = load_template("steering/product.md.tmpl").substitute(
        sub, summary_line=summary_line, read_more="\n".join(read_more),
    )
    out[f"{STEERING_DIR}/{ctx.foundation('product')}"] = steering({"inclusion": "always"}, body)
    # tech
    tech: list[str] = []
    langs = man.project.get("primary_languages")
    if isinstance(langs, list) and langs:
        tech.append(f"- Languages: {', '.join(str(x) for x in langs)}")
    for key, label in (("build_tool", "Build tool"), ("version", "Version")):
        if stats.get(key):
            tech.append(f"- {label}: {stats[key]}")
    if isinstance(stats.get("modules"), list) and stats["modules"]:
        tech.append(f"- Modules: {', '.join(str(m) for m in stats['modules'])}")
    if not tech:
        log.debug("manifest has no language/build details for tech.md")
        tech.append("- No language or build details in the extraction manifest.")
    concerns = []
    for item in man.cross_cutting_concerns:
        if not item.get("concern"):
            continue
        line = f"- {item['concern']}: {first_sentence(str(item.get('summary') or ''), 140)}"
        if item.get("documented_in"):
            line += f" (`{ctx.bre_path(str(item['documented_in']))}`)"
        concerns.append(line)
    body = load_template("steering/tech.md.tmpl").substitute(
        sub, tech_list="\n".join(tech), concerns="\n".join(concerns) or "- None listed in the manifest."
    )
    out[f"{STEERING_DIR}/{ctx.foundation('tech')}"] = steering({"inclusion": "always"}, body)
    # structure
    modules = stats.get("modules") if isinstance(stats.get("modules"), list) else []
    dirs = []
    for dom in man.ordered_domains():
        common = _common_dir(dom.source_paths)
        if common:
            dirs.append(f"- `{dom.slug}`: `{common}/`")
    artifacts = [
        f"- {key}: `{ctx.bre_path(str(val))}`"
        for key, val in sorted(man.common_artifacts.items())
        if isinstance(val, str)
    ]
    body = load_template("steering/structure.md.tmpl").substitute(
        sub,
        modules="\n".join(f"- {m}" for m in modules) or "- Not listed in the manifest.",
        domain_dirs="\n".join(dirs) or "- No source files mapped.",
        artifacts="\n".join(artifacts) or "- None listed.",
    )
    out[f"{STEERING_DIR}/{ctx.foundation('structure')}"] = steering({"inclusion": "always"}, body)
    # index
    lines = []
    for dom in man.ordered_domains():
        desc = first_sentence(dom.description or dom.responsibility, 160)
        lines.append(f"- {dom.slug} — {dom.name}" + (f" — {desc}" if desc else ""))
    body = load_template("steering/bre-context-index.md.tmpl").substitute(
        sub, domain_lines="\n".join(lines), skills=", ".join(f"`{s}`" for s in SKILLS)
    )
    # The index is a per-domain directory, i.e. detailed reference material: auto inclusion (loads when a
    # request matches its description), not always. Every generated agent still pins it via file:// (see
    # agents.pinned_steering), so the entry agent always has it to scope questions.
    index_desc = truncate_words(
        f"Directory of the domains in {ctx.project_name}: each domain's slug, name and one-line summary. "
        "Use it to find which domain a question, change, defect, requirement or test belongs to before "
        "reading that domain's reviewed rules or routing it to a specialist.", 1000)
    out[f"{STEERING_DIR}/{INDEX_FILE}"] = steering(
        {"inclusion": "auto", "name": "bre-context-index", "description": index_desc}, body)
    return out


# --------------------------------------------------------------------------- domains


def auto_description(dom: Domain) -> str:
    """Auto-inclusion description per the design formula, ≤1000 chars at a word boundary."""
    first = first_sentence(dom.description or dom.responsibility, 300).rstrip(".")
    text = f"{dom.name} domain ({dom.slug})" + (f": {first}." if first else ".")
    parts = []
    ents = [str(e.get("name")) for e in dom.entities if e.get("name")][:8]
    if ents:
        parts.append(", ".join(ents))
    if dom.owned_tables:
        parts.append("tables " + ", ".join(dom.owned_tables[:8]))
    if dom.cross_domain_features:
        parts.append(", ".join(dom.cross_domain_features[:4]))
    if parts:
        joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + ", or " + parts[-1]
        text += f" Use for questions, requirements, defects, tests or changes involving {joined}."
    else:
        text += " Use for questions, requirements, defects, tests or changes in this domain."
    return truncate_words(text, 1000)


def _domain_body(ctx: RenderContext, dom: Domain) -> str:
    folder = ctx.layout.domain_dir(dom.slug)
    refs: list[str] = []
    on_demand: list[str] = []
    if folder is None:
        refs.append(NO_DOMAIN_FILES)
    else:
        base = ctx.layout.rel(folder)
        bre_refs = []
        for name in ("overview.md", "integrations.md"):
            if ctx.layout.domain_file(dom.slug, name) is not None:
                bre_refs.append(f"#[[file:{ctx.bre_path(base + '/' + name)}]]")
        if bre_refs:
            refs += ["", PROVENANCE_EXTRACTED] + bre_refs
        extra = [n for n in EXTRA_DOMAIN_FILES if ctx.layout.domain_file(dom.slug, n) is not None]
        extra_paths = [ctx.bre_path(f"{base}/{n}") for n in extra]
        extra_paths += [ctx.bre_path(ctx.layout.rel(p)) for p in ctx.layout.diagrams(dom.slug)]
        extra_paths = [f"{p} {tier_tag(p)}" for p in extra_paths]
        raw = ctx.bre_path(f"{base}/business-rules.md")
        extra_paths.append(f"{raw} {tier_tag(raw)} (raw extraction; prefer reviewed-rules.md)")
        on_demand = ["", "Read on demand (open with tools when needed; Tier 3 is hypothesis only):", ""] + [
            f"- {p}" for p in extra_paths]
    return load_template("steering/bre-domain.md.tmpl").substitute(
        ctx.subst(),
        domain_name=dom.name,
        slug=dom.slug,
        summary=first_sentence(dom.description or dom.responsibility, 300) or dom.name,
        bre_refs="\n".join(refs),
        on_demand="\n".join(on_demand),
    )


def render_domain_steering(ctx: RenderContext, dom: Domain) -> dict[str, str]:
    """``bre-domain-<slug>.md`` (auto) and ``bre-domain-<slug>-code.md`` (fileMatch)."""
    body = _domain_body(ctx, dom)
    out = {
        f"{STEERING_DIR}/bre-domain-{dom.slug}.md": steering(
            {"inclusion": "auto", "name": f"bre-domain-{dom.slug}", "description": auto_description(dom)}, body
        )
    }
    paths = dom.source_paths
    if paths:
        out[f"{STEERING_DIR}/bre-domain-{dom.slug}-code.md"] = steering(
            {"inclusion": "fileMatch", "fileMatchPattern": list(paths)}, body
        )
    else:
        log.warning("domain %s: no usable source_files; no fileMatch steering written", dom.slug)
    return out


def render_summary(
    ctx: RenderContext, dom: Domain, parsed_count: int | None, counts: dict[str, int] | None
) -> str:
    """``.kiro/context/domains/<slug>/summary.md`` from manifest data."""
    lines = [f"# {dom.name} (`{dom.slug}`): summary", ""]
    for label, text in (("Responsibility", dom.responsibility), ("Description", dom.description),
                        ("Bounded context", dom.bounded_context_summary)):
        if text:
            lines += [f"**{label}:** {' '.join(text.split())}", ""]
    if dom.entities:
        lines += ["## Entities", "", "| Entity | Table | Shared | Source |", "|---|---|---|---|"]
        for ent in dom.entities:
            lines.append(
                f"| {md_cell(ent.get('name'))} | {md_cell(ent.get('table_name') or '-')} | "
                f"{'yes' if ent.get('is_shared') else 'no'} | {md_cell(ent.get('source_file') or '-')} |"
            )
        lines.append("")
    lines += ["## Data and dependencies", "", "| Property | Value |", "|---|---|"]
    for label, values in (
        ("Owned tables", dom.owned_tables),
        ("Shared tables referenced", dom.shared_tables_referenced),
        ("Depends on domains", dom.depends_on),
        ("Depended on by domains", dom.depended_on_by),
        ("Shared entities used", dom.shared_entities_used),
        ("Cross-domain features", dom.cross_domain_features),
    ):
        lines.append(f"| {label} | {md_cell(', '.join(values) or 'none')} |")
    rule_line = f"| Rules parsed from the extraction | {parsed_count if parsed_count is not None else 0}"
    if dom.rule_count is not None:
        rule_line += f" (manifest rule_count {dom.rule_count})"
    lines.append(rule_line + " |")
    if counts is not None:
        lines.append(
            f"| Review status | verified {counts.get('verified', 0)}, needs-edit {counts.get('needs-edit', 0)}, "
            f"incorrect {counts.get('incorrect', 0)}, needs-sme {counts.get('needs-sme', 0)}, "
            f"unreviewed {counts.get('unreviewed', 0)} |"
        )
    lines += ["", "## Source files", ""]
    files = dom.source_files
    for sf in files[:100]:
        extra = ", ".join(x for x in (sf.role, f"{sf.loc} LOC" if sf.loc is not None else None) if x)
        lines.append(f"- `{sf.path}`" + (f" ({extra})" if extra else ""))
    if len(files) > 100:
        lines.append(f"- and {len(files) - 100} more (see `{CONTEXT_DIR}/file-index.json`)")
    if not files:
        lines.append("- None mapped.")
    return derived_doc("\n".join(lines))


# --------------------------------------------------------------------------- reviewed rules


def _evidence_summary(record: dict[str, Any] | None) -> str:
    if not record:
        return "none"
    parts = []
    for ev in record.get("evidence") or []:
        if ev.get("kind") == "range":
            parts.append(f"{ev['path']}:L{ev['start']}-L{ev['end']}")
        elif ev.get("kind") == "search":
            parts.append(f"search /{ev.get('pattern')}/ in {ev.get('scope')}: {ev.get('hit_count')} hits")
    return ", ".join(parts) or "none"


ANCHORS_PREFIX = "> Anchors (cite these; lines as of the review): "
WHAT_CHANGED_PREFIX = "> What changed: "
SECOND_ROUND_PREFIX = "> Second pass: "
RULES_HEADER = (
    "Each rule has a `> Review:` line under its heading. VERIFIED: a reviewer checked the main behaviour "
    "against the code; a side detail can still be wrong. NEEDS EDIT: the main behaviour is right and the text "
    "shown is the reviewer's full corrected rule (the `> What changed:` line says what differs from the "
    "extraction). NEEDS SME and UNREVIEWED: extraction text nobody has confirmed. Claims a reviewer found "
    "incorrect are listed only under \"Known BRE corrections\". The context is a map; the code is the truth."
)
CORRECTIONS_INTRO = (
    "The extraction's text for these rules was wrong in its core and is not shown. Each correction is itself "
    "a reviewed claim: apply the mandatory code check before you rely on it."
)
TRUST_RULES = "each rule carries its own review status; corrected rules are reviewed claims, not facts"
ANCHORS_HEADER = (  # narrow-capped design only; validate keys its anchor checks on this sentence
    "A VERIFIED rule's `> Review:` evidence (path and line range) and `> Anchors` line (file:line, "
    "`Type.member`, quoted line) are the code citation: cite them without opening the code. "
    "Line numbers are as of the review."
)


def anchors_line(record: dict[str, Any] | None, index: SourceIndex | None = None) -> str | None:
    """``> Anchors`` line for the range evidence of a review record: per range the file name and
    line, ``Type.member`` and the quoted declaration (or first significant) line; None when no range
    yields an anchor. The full path and range are already on the ``> Review:`` line."""
    parts = []
    for ev in (record or {}).get("evidence") or []:
        anchor = evidence_anchor(ev, index)
        if anchor is None:
            continue
        name = anchor.path.rsplit("/", 1)[-1]
        moved = " (moved since review)" if anchor.moved else ""
        quote = anchor.quote.replace("`", "'")
        parts.append(f"{name}:L{anchor.line}{moved} `{anchor.member}` `{quote}`")
    return ANCHORS_PREFIX + " · ".join(parts) if parts else None


def status_label(verdict: str, record: dict[str, Any] | None) -> str:
    """Upper-case status label for the ``> Review:`` line."""
    if verdict == "verified":
        label = "VERIFIED"
    elif verdict == "needs-sme":
        note = truncate_words(str((record or {}).get("note") or "no note"), 300)
        label = f"NEEDS SME ({note})"
    elif verdict == "incorrect":
        label = "INCORRECT"
    elif verdict == "needs-edit":
        label = "NEEDS EDIT (corrected text below)"
    else:
        label = "UNREVIEWED — treat as unconfirmed extraction"
    if record and record.get("suspected_defect") and verdict != "unreviewed":
        label += ", SUSPECTED DEFECT"
    return label


def reviewed_rules(
    dom: Domain,
    claims: list[Claim],
    review: ReviewData,
    unreviewed_mode: str = "flag",
    index: SourceIndex | None = None,
    anchors: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Apply the review policy to a domain's rules; return (reviewed-rules.md text, counts).

    With ``anchors`` (the narrow-capped design) VERIFIED rules also get an ``> Anchors`` line (see
    :func:`anchors_line`) so they can be cited without opening the code; ``index`` refines the
    anchors against the current source."""
    counts: dict[str, Any] = {"verified": 0, "needs-edit": 0, "incorrect": 0, "needs-sme": 0, "unreviewed": 0,
                              "suspected": 0, "stale": [], "excluded": 0}
    signoff = review.domain_signoff(dom.slug)
    lines = [f"# Reviewed business rules: {dom.name} (`{dom.slug}`)", ""]
    lines += [RULES_HEADER + (" " + ANCHORS_HEADER if anchors else ""), ""]
    corrections: list[str] = []
    body: list[str] = []
    for claim in sorted(claims, key=lambda c: (c.number, c.rule_id)):
        claim_id = f"{dom.slug}/{claim.rule_id}"
        record = review.claim(claim_id)
        if record and record.get("verdict") not in (None, "unreviewed") and record.get("body_sha256") != claim.body_sha256:
            counts["stale"].append(claim_id)
            log.warning("%s: review verdict is stale (claim text changed); treated as unreviewed", claim_id)
        verdict, who = effective_status(record, signoff, claim.rule_id, claim.body_sha256)
        counts[verdict] += 1
        effective_record = record if verdict != "unreviewed" else None
        if effective_record and effective_record.get("suspected_defect"):
            counts["suspected"] += 1
        if verdict == "incorrect":
            fix = (record or {}).get("correction") or (record or {}).get("note") or "no correction recorded"
            corrections.append(f"- {claim.rule_id}: {claim.title} — reviewer correction: {' '.join(str(fix).split())}")
            continue
        if verdict == "unreviewed" and unreviewed_mode == "exclude":
            counts["excluded"] += 1
            continue
        text_lines = claim.text.split("\n")
        status = f"> Review: {status_label(verdict, effective_record)} · {who} · evidence: {_evidence_summary(effective_record)}"
        head = [text_lines[0], status]
        rest = text_lines[1:]
        if verdict == "needs-edit":
            changed = " ".join(str((effective_record or {}).get("what_changed") or "not recorded").split())
            head.append(WHAT_CHANGED_PREFIX + changed)
            rest = sanitize_replacement(str((effective_record or {}).get("replacement") or "")).split("\n")
        anchor = anchors_line(effective_record, index) if anchors and verdict == "verified" else None
        if anchor:
            head.append(anchor)
        found = counterexample_found(effective_record)
        if found:
            note = truncate_words(" ".join(str(found.get("note") or "no note").split()), 300)
            head.append(f"{SECOND_ROUND_PREFIX}counterexample found ({note})")
        body += head + rest + [""]
    if not claims:
        lines.append(NO_RULES)
    elif not body:
        lines.append("No rules remain after applying the review policy.")
    lines += body
    if counts["excluded"]:
        lines.append(f"{counts['excluded']} unreviewed rule(s) excluded by policy (--unreviewed exclude).")
        lines.append("")
    if corrections:
        lines += ["## Known BRE corrections", "", CORRECTIONS_INTRO, ""] + corrections
    return marked_doc("\n".join(lines), TIER2, TRUST_RULES), counts


def render_review_status(ctx: RenderContext, per_domain: dict[str, dict[str, Any]]) -> str:
    """``review-status.md``: counts per domain and verdict plus sign-off state."""
    lines = ["# Review status", ""]
    if not ctx.review.present:
        lines += ["No review record was supplied; every rule is UNREVIEWED.", ""]
    lines += ["| Domain | Verified | Needs edit | Incorrect | Needs SME | Unreviewed | Suspected defects | Sign-off |",
              "|---|---|---|---|---|---|---|---|"]
    for dom in ctx.manifest.ordered_domains():
        c = per_domain.get(dom.slug, {})
        so = ctx.review.domain_signoff(dom.slug)
        state = (f"{so.get('decision')} by {so.get('reviewer')} ({so.get('reviewer_type')}, {so.get('date')})"
                 if so else "not signed off")
        lines.append(
            f"| `{dom.slug}` | {c.get('verified', 0)} | {c.get('needs-edit', 0)} | {c.get('incorrect', 0)} | "
            f"{c.get('needs-sme', 0)} | {c.get('unreviewed', 0)} | {c.get('suspected', 0)} | {md_cell(state)} |"
        )
    return marked_doc("\n".join(lines), TIER1, "counts from the review record")


# --------------------------------------------------------------------------- framework rules

FRAMEWORK_KEYWORDS = ("auto", "java-spring", "blank")
TRUST_FRAMEWORK = "team-maintained framework semantics; ask the team to keep it current for its stack"


def framework_choice(ctx: RenderContext) -> str:
    """``java-spring``, ``blank`` or a customer file path for the ``framework_rules`` config key."""
    choice = ctx.config.framework_rules
    if choice != "auto":
        return choice
    langs = ctx.manifest.project.get("primary_languages")
    names = [str(x).lower() for x in langs] if isinstance(langs, list) else []
    return "java-spring" if any(n == "java" or n.startswith("java ") for n in names) else "blank"


def render_framework_rules(ctx: RenderContext) -> dict[str, str]:
    """``.kiro/context/framework-rules.md`` (Tier 1) and its auto-included steering file."""
    choice = framework_choice(ctx)
    langs = ctx.manifest.project.get("primary_languages")
    stack = ", ".join(str(x) for x in langs) if isinstance(langs, list) and langs else "your stack"
    if choice in ("java-spring", "blank"):
        body = load_template(f"framework-rules/{choice}.md.tmpl").substitute(ctx.subst(), stack=stack)
    else:
        body = read_text(Path(choice)).replace("\r\n", "\n").replace("\r", "\n")
    rel = f"{CONTEXT_DIR}/{FRAMEWORK_FILE}"
    pointer = (
        f"# Framework rules: {ctx.project_name}\n"
        "Framework semantics that static reading of the code misses. Apply them whenever an answer depends on "
        "transactions, rollback, commit, error end states, method security, proxies, scheduling or async work.\n"
        f"#[[file:{rel}]]"
    )
    return {
        rel: marked_doc(body, TIER1, TRUST_FRAMEWORK),
        f"{STEERING_DIR}/{FRAMEWORK_STEERING}": steering(
            {
                "inclusion": "auto",
                "name": "bre-framework-rules",
                "description": (
                    f"Framework rules for {ctx.project_name}: transaction boundaries, rollback and rollback-only "
                    "marking, commit and error end states, proxy self-invocation, method-security annotations "
                    "and authorization, scheduled and async work. Use when a question or change involves "
                    "transactions, rollback, errors, authorization, privileges, scheduling or threads."
                ),
            },
            pointer,
        ),
    }


# --------------------------------------------------------------------------- cross-domain steering


def render_cross_steering(ctx: RenderContext) -> dict[str, str]:
    """``bre-impact-analysis.md`` (auto), ``bre-variants.md`` (auto), ``bre-glossary.md`` (manual)."""
    sub = ctx.subst()
    name = ctx.project_name
    return {
        f"{STEERING_DIR}/bre-impact-analysis.md": steering(
            {
                "inclusion": "auto",
                "name": "bre-impact-analysis",
                "description": (
                    f"Cross-domain impact map for {name}. Use for change impact analysis: which domains, "
                    "components, shared tables or configuration variants a change affects, hidden "
                    "dependencies between components, and regression risk in other domains."
                ),
            },
            load_template("steering/bre-impact-analysis.md.tmpl").substitute(sub),
        ),
        f"{STEERING_DIR}/bre-variants.md": steering(
            {
                "inclusion": "auto",
                "name": "bre-variants",
                "description": (
                    f"Configuration, feature-flag and global-property variant map for {name}. Use when "
                    "behaviour depends on configuration, a feature flag, a program or deployment variant, "
                    "or when something behaves differently in another configuration."
                ),
            },
            load_template("steering/bre-variants.md.tmpl").substitute(sub),
        ),
        f"{STEERING_DIR}/bre-glossary.md": steering(
            {"inclusion": "manual"}, load_template("steering/bre-glossary.md.tmpl").substitute(sub)
        ),
    }


# --------------------------------------------------------------------------- skills and agent


def render_skills(ctx: RenderContext) -> dict[str, str]:
    """The seven workspace skills (instruction-only)."""
    out = {}
    for name, description in SKILLS.items():
        body = load_template(f"skills/{name}/SKILL.md.tmpl", ctx.design).substitute(ctx.subst())
        text = frontmatter.emit({"name": name, "description": description}) + GENERATED_MARKER + "\n\n" + body
        out[f".kiro/skills/{name}/SKILL.md"] = text.rstrip("\n") + "\n"
    return out


def always_files(ctx: RenderContext) -> list[str]:
    """Workspace-relative paths of the always-included steering files (foundation and the agent pointer).
    The context index is ``auto`` inclusion, not always; every agent pins it via :func:`pinned_steering`."""
    return ([f"{STEERING_DIR}/{ctx.foundation(k)}" for k in FOUNDATION]
            + [f"{STEERING_DIR}/{POINTER_FILE}"])


def render_agent_pointer(ctx: RenderContext, specialists: list[str]) -> dict[str, str]:
    """The tiny always-included note that sends default-agent users to the ``brownfield`` agent."""
    entries = ["brownfield"] + ([FAST_ENTRY] if ctx.config.fast_entry_agent else [])
    body = load_template(f"steering/{POINTER_FILE}.tmpl", ctx.design).substitute(
        ctx.subst(), specialists=", ".join(f"`{s}`" for s in specialists),
        entry_agents=", ".join(f"`{e}`" for e in entries),
    )
    return {f"{STEERING_DIR}/{POINTER_FILE}": steering({"inclusion": "always"}, body)}
