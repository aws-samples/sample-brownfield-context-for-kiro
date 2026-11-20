"""Kiro custom agents: the ``brownfield`` entry agent and four task specialists.

Each specialist loads, at startup, the errata, the framework rules and the Tier 1 / Tier 2 files that
fit its task. Tier 3 extraction files (workflows, diagrams, cross-cutting error and integration
documents) are never preloaded: the prompt lists them as on-demand navigation aids. Every agent also
lists the always-included foundation and index steering explicitly (see :func:`pinned_steering`).
Every agent's startup bytes (``file://`` resources, its ``skill://`` files, description and prompt) are
kept within the advisory ``agent_budget_bytes`` (a bcp heuristic, not a Kiro limit) and Kiro's documented
32-file / 100,000-character / 50,000-character-per-file ``file://`` caps
(https://kiro.dev/docs/custom-agents/configuration-reference/): resources are listed most relevant first
and kept while they fit; a file that does not fit is dropped, logged and named in the prompt as
read-on-demand.

Every prompt carries the same context contract: the context is a map and the code is the truth; a
mandatory code check applies to authorization, transactions and error end states, concurrency,
absolute claims, counts and diagram assertions, whatever the review status; answers tag provenance.

Designs (``agent_design``):

- ``hybrid`` (default): ``brownfield`` scopes the question. A narrow (one or two domain) question it
  answers itself after reading those domains' reviewed rules; a broad one it delegates to a specialist.
- ``delegate`` (opt-in): ``brownfield`` routes every question to a specialist.
- ``narrow-capped`` (opt-in): ``brownfield`` answers narrow questions itself, cites ``> Anchors``
  lines, and every agent caps code reads at :data:`MAX_CODE_READS` per answer (the mandatory check
  still applies).
- ``hook-only`` (opt-in, needs a ``kb`` section): ``brownfield`` keeps the ``kb-retrieve`` hook and
  answers directly from the injected retrieved context, the reviewed rules and the code. It has no
  ``subagent/*`` tools, so it physically cannot hop (which also sidesteps the known quirk that
  ``--trust-tools`` does not approve tools inside sub-agents); the four specialists are still generated
  as standalone agents, so genuinely system-wide work is handed to one directly. This removes the agent
  layer the KB PoC measured as the extra cost, not the KB, so it is the recommended default for Q&A.
- ``kb-first`` (opt-in, needs a ``kb`` section): like ``hook-only`` (same plumbing, no ``subagent/*``
  tools, the ``kb-retrieve`` hook, pinned steering), but ``brownfield`` answers from the verified-fresh
  facts and their recorded evidence lines without opening the code for them; it opens the code only for a
  fact tagged stale or quarantined, a point resting on unreviewed extraction, a question no fact covers,
  or a task that changes code. It keeps the honest provenance tags.

``fast_entry_agent`` (opt-in, ``delegate`` only) adds ``brownfield-fast``: the same context,
specialists and code rule as ``delegate``, but it answers narrow questions itself.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from bcp.manifest import Domain
from bcp.render import (
    CONTEXT_DIR,
    ENTRY_EXTRA_SKILLS,
    ERRATA_FILE,
    FAST_ENTRY,
    FRAMEWORK_FILE,
    HOOK_ONLY,
    KB_FIRST,
    INDEX_FILE,
    MAX_CODE_READS,
    NARROW_CAPPED,
    POINTER_FILE,
    STEERING_DIR,
    TRUST_FILE,
    RenderContext,
    always_files,
)
from bcp.trust import tier_for
from bcp.validate import KIRO_AGENT_MAX_CHARS, KIRO_AGENT_MAX_FILE_CHARS, KIRO_AGENT_MAX_FILES

log = logging.getLogger(__name__)

ENTRY = "brownfield"
AGENTS_DIR = ".kiro/agents"
NEVER_LOAD = ("domain-manifest.json",)
SizeOf = Callable[[str], "int | None"]
DELEGATE = "delegate"
HYBRID = "hybrid"
ERRATA_REL = f"{CONTEXT_DIR}/{ERRATA_FILE}"
FRAMEWORK_REL = f"{CONTEXT_DIR}/{FRAMEWORK_FILE}"
KB_LATEST_REL = f"{CONTEXT_DIR}/retrieved/latest.md"
KB_HOOK_NAME = "kb-retrieve"
KB_HOOK_TIMEOUT_S = 30  # Kiro hook timeout is in seconds (default 60): https://kiro.dev/docs/hooks/


@dataclass(frozen=True)
class Source:
    """One resource candidate.

    ``kind`` is ``generated`` (path under ``.kiro/context``), ``bre`` (BRE-relative path),
    ``domain`` (a file name inside each agent domain's BRE folder) or ``rules`` (each agent
    domain's generated ``reviewed-rules.md``). ``tier`` 0 means "from the path" (:func:`tier_for`).
    """

    kind: str
    rel: str = ""
    tier: int = 0


@dataclass(frozen=True)
class Specialist:
    """A task specialist sub-agent. ``on_demand`` lists Tier 3 navigation aids it may open."""

    name: str
    skill: str
    description: str
    task: str
    sources: tuple[Source, ...]
    on_demand: tuple[Source, ...] = ()


CONTRACT_SOURCES = (Source("generated", ERRATA_FILE), Source("generated", FRAMEWORK_FILE))

SPECIALISTS: tuple[Specialist, ...] = (
    Specialist(
        "impact-analysis",
        "change-impact-analysis",
        "Change impact analysis and pull-request or design review: which deployments, configurations "
        "(global properties, feature flags), domains, components and user flows a proposed change affects, "
        "hidden dependencies, regression risk, test gaps and the regression tests to require.",
        "change impact analysis and pull-request or design review",
        (
            *CONTRACT_SOURCES,
            Source("generated", "impact-map.md"),
            Source("generated", "variant-map.md"),
            Source("generated", "program-config.md"),
            Source("generated", "dependency-map.md"),
            Source("bre", "reference-documentation/feature-flag-inventory.md"),
            Source("bre", "domain-decomposition-overview/data-ownership.md"),
        ),
        (
            Source("bre", "reference-documentation/coupling-hotspots.md"),
            Source("bre", "reference-documentation/external-integrations-map.md"),
            Source("bre", "cross-cutting-documentation/integration-patterns.md"),
            Source("bre", "cross-cutting-documentation/event-publishing-patterns.md"),
        ),
    ),
    Specialist(
        "product-onboarding",
        "onboarding-guide",
        "Onboarding and system understanding: explain how the product or a business flow works end to end, "
        "which components and domains take part and in what order, which configuration changes the behaviour, "
        "the architecture, and what a new team member is likely to get wrong.",
        "onboarding and explaining how the system and its business flows work",
        (
            *CONTRACT_SOURCES,
            Source("bre", "domain-decomposition-overview/bounded-contexts.md"),
            Source("bre", "domain-decomposition-overview/decomposition-overview.md"),
            Source("domain", "overview.md"),
        ),
        (
            Source("bre", "diagrams/architecture-diagrams.md"),
            Source("bre", "diagrams/sequence-diagrams.md"),
            Source("domain", "workflows.md"),
        ),
    ),
    Specialist(
        "defect-triage",
        "defect-triage",
        "Defect triage and production incident investigation: trace a defect report, error message, log "
        "excerpt or exception to its domain, business rule and code, rank root-cause hypotheses, and "
        "recommend the safest corrective action, recovery steps and regression tests.",
        "defect triage, root-cause analysis and production incident investigation",
        (
            *CONTRACT_SOURCES,
            Source("rules"),
            Source("domain", "validations.md"),
            Source("domain", "integrations.md"),
        ),
        (
            Source("bre", "cross-cutting-documentation/error-handling-patterns.md"),
            Source("bre", "cross-cutting-documentation/common-error-codes.md"),
        ),
    ),
    Specialist(
        "requirement-gap",
        "requirement-gap-check",
        "Requirement analysis: decide whether a requirement already exists, partly exists or is new, break it "
        "into epics and stories with constraints and clarifying questions, compare documented behaviour with "
        "what the system does, and choose the existing API or idiom new code should use.",
        "requirement gap analysis, documented-versus-actual behaviour checks and choosing existing APIs",
        (
            *CONTRACT_SOURCES,
            Source("domain", "features.md"),
            Source("rules"),
            Source("bre", "reference-documentation/api-endpoint-catalog.md"),
        ),
    ),
)

# --------------------------------------------------------------------------- the context contract

CONTEXT_IS_A_MAP = (
    "The context is a map; the code is the truth. Review statuses in reviewed-rules.md: VERIFIED means a "
    "reviewer checked the main behaviour against the code, but a side detail in the text can still be wrong; "
    "NEEDS EDIT means the main behaviour is right and the text shown is the reviewer's corrected rule; NEEDS SME "
    "and UNREVIEWED are unconfirmed extraction text. Rules under \"Known BRE corrections\" are incorrect: never "
    "use their original extraction text, and treat the correction as a reviewed claim, not a fact."
)

ERRATA_FIRST = (
    f"`{ERRATA_REL}` lists every corrected claim and the other extraction passages that repeat it; read it "
    f"first and never rely on a passage it marks superseded. `{FRAMEWORK_REL}` lists framework semantics "
    "(transactions, rollback, method security, scheduling) that static reading misses; apply it when you "
    "trace an end state."
)

MANDATORY_CHECK = (
    "Mandatory code check, whatever the review status: before you state anything about (a) authorization or "
    "its absence, read the method's annotations on both the interface and the implementation, across line "
    "breaks; (b) transactions, rollback, commit or error end states, trace the exception type to its catch "
    "sites and to the transaction boundary (propagation, rollback-only marking by nested transactional calls, "
    "self-invocation); (c) concurrency or locking; (d) absolute claims (only, every, never, always, unique, "
    "none, no X); (e) counts; (f) anything a diagram or workflow asserts. For these, open the code and read the "
    "whole method, or say plainly that the point is unverified."
)

PROVENANCE = (
    "Tag each statement with its provenance: `[code: path#Lx-Ly]` for what you read in the code, "
    "`[reviewed context: <file> BR-n]` for a reviewed rule you did not re-check, or "
    "`[unreviewed extraction, unverified: <file>]` for extraction text."
)

CODE_RULE = (
    "Open source code for every mandatory-check topic; for rules marked UNREVIEWED, NEEDS SME or stale; for "
    "behaviour the context does not cover; and when asked to verify something against the code. Otherwise, "
    "when the question asks for code citations, cite the evidence ranges already recorded in the `> Review:` "
    "lines of reviewed-rules.md, and open a code file only to confirm a line the context does not give."
)

CONTEXT_FIRST_READ = (
    "When you run as a sub-agent they are not preloaded: if their content is not already in your context, "
    "read all of them first, in one parallel batch, before any other tool call. Answer from them before "
    "opening anything else, except for the mandatory code check."
)

# --------------------------------------------------------------------------- entry agents

ENTRY_DESCRIPTION = (  # hybrid (default)
    "Entry agent for questions about this system: change impact and pull-request review, onboarding and "
    "how flows work, defect and incident triage, requirement gaps. Answers narrow one- or two-domain "
    "questions itself from the reviewed rules and the code, and delegates broad questions to a specialist."
)

DELEGATE_ENTRY_DESCRIPTION = (
    "Entry agent for questions about this system: change impact and pull-request review, onboarding and "
    "how flows work, defect and incident triage, requirement gaps. Routes each question to a specialist "
    "that answers from the reviewed context and the code."
)

# --------------------------------------------------------------------------- narrow-capped design (opt-in)

CAPPED_ENTRY_DESCRIPTION = (
    "Entry agent for questions about this system: change impact and pull-request review, onboarding and "
    "how flows work, defect and incident triage, requirement gaps. Answers narrow single-domain questions "
    "itself from the reviewed context and routes broad or cross-domain questions to a specialist."
)

CAPPED_CODE_RULE = (
    MANDATORY_CHECK + " "
    "Apart from that check, code is for verification only. For a rule marked VERIFIED, cite the evidence the "
    "context already gives (the path and line range on its `> Review:` line, and the file:line, `Type.member` "
    "and quoted line on its `> Anchors` line in reviewed-rules.md): that evidence satisfies a request to cite "
    "file paths and line numbers. Open code only when (a) the rule you need is UNREVIEWED, NEEDS SME, stale, "
    "listed under \"Known BRE corrections\" or missing from the context, (b) the question needs a specific line "
    "or behaviour the context does not give, or (c) the user asks you to verify against the code. Cap "
    f"verification at {MAX_CODE_READS} code reads per answer (each file read or code search counts as one); "
    "when more would be needed, stop and say which points are unconfirmed instead."
)

CAPPED_CONTEXT_FIRST_READ = (
    "When you run as a sub-agent they are not preloaded: read the ones the question needs first, in one "
    "parallel batch, before any other tool call (for a question about one or two domains, only those domains' "
    "files). Answer from them before opening anything else."
)

# --------------------------------------------------------------------------- fast entry agent (opt-in)

FAST_ENTRY_DESCRIPTION = (
    "Fast entry agent for questions about this system: change impact and pull-request review, onboarding and "
    "how flows work, defect and incident triage, requirement gaps. Answers narrow questions about one or two "
    "domains itself from the reviewed context, without a sub-agent, and routes broad or cross-domain "
    "questions to a specialist."
)

NARROW_SCOPE = (
    "   - Narrow: it is about one component, rule, setting, error or proposed code change that sits in one "
    "domain (two at most). Answer it yourself (step 2). Do not start a sub-agent."
)
BROAD_SCOPE = (
    "   - Broad: it spans three or more domains, asks how a flow works end to end, or asks for system-wide "
    "impact. Delegate it (step 3)."
)


def _code_rule(ctx: RenderContext) -> str:
    return CAPPED_CODE_RULE if ctx.design == NARROW_CAPPED else f"{MANDATORY_CHECK} {CODE_RULE}"


@dataclass
class AgentPlan:
    """One agent: its JSON config and the startup-bytes measurement."""

    name: str
    data: dict[str, Any]
    resources: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    startup_bytes: int = 0
    on_demand: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """JSON-friendly summary."""
        return {"startup_bytes": self.startup_bytes, "resources": self.resources, "dropped": self.dropped,
                "on_demand": self.on_demand}


def agent_domains(ctx: RenderContext) -> list[Domain]:
    """``agent_domains`` from the config in that order (unknown slugs warned), else every domain."""
    wanted = ctx.config.agent_domains
    if not wanted:
        return ctx.manifest.ordered_domains()
    out: list[Domain] = []
    for slug in wanted:
        dom = ctx.manifest.domain(slug)
        if dom is None:
            log.warning("agent_domains: unknown domain %s ignored", slug)
        elif dom not in out:
            out.append(dom)
    return out


def _expand(ctx: RenderContext, source: Source, domains: list[Domain]) -> list[str]:
    if source.kind == "generated":
        return [f"{CONTEXT_DIR}/{source.rel}"]
    if source.kind == "bre":
        return [ctx.bre_path(source.rel)] if (ctx.layout.root / source.rel).is_file() else []
    if source.kind == "rules":
        return [f"{CONTEXT_DIR}/domains/{d.slug}/reviewed-rules.md" for d in domains]
    out = []
    for dom in domains:
        path = ctx.layout.domain_file(dom.slug, source.rel, warn=False)
        if path is not None:
            out.append(ctx.bre_path(ctx.layout.rel(path)))
    return out


def _collect(ctx: RenderContext, sources: tuple[Source, ...], domains: list[Domain], size_of: SizeOf,
             name: str) -> list[str]:
    out: list[str] = []
    for source in sources:
        for rel in _expand(ctx, source, domains):
            if rel in out or rel.rsplit("/", 1)[-1] in NEVER_LOAD:
                continue
            if size_of(rel) is None:
                log.info("agent %s: %s not found; not loaded", name, rel)
                continue
            out.append(rel)
    return out


def candidates(ctx: RenderContext, spec: Specialist, domains: list[Domain], size_of: SizeOf) -> list[str]:
    """Existing resource files for ``spec``, most relevant first, never ``domain-manifest.json``."""
    return _collect(ctx, spec.sources, domains, size_of, spec.name)


def on_demand_aids(ctx: RenderContext, spec: Specialist, domains: list[Domain], size_of: SizeOf) -> list[str]:
    """Existing Tier 3 navigation aids for ``spec`` (named in the prompt, never preloaded)."""
    return _collect(ctx, spec.on_demand, domains, size_of, spec.name)


def _skill_rel(skill: str) -> str:
    return f".kiro/skills/{skill}/SKILL.md"


def tiered(paths: list[str]) -> str:
    """```a` (Tier 2), `b` (Tier 1)``."""
    return ", ".join(f"`{p}` (Tier {tier_for(p)})" for p in paths)


def specialist_prompt(ctx: RenderContext, spec: Specialist, loaded: list[str], dropped: list[str],
                      domains: list[Domain], aids: list[str] | None = None) -> str:
    """The specialist's prompt; names its startup files with their tiers, the Tier 3 aids it may open,
    and any files dropped for the budget."""
    slugs = ", ".join(d.slug for d in domains) or "none"
    first_read = CAPPED_CONTEXT_FIRST_READ if ctx.design == NARROW_CAPPED else CONTEXT_FIRST_READ
    lines = [
        f"You are the `{spec.name}` specialist for {ctx.project_name}: {spec.task}.",
        "Your startup resources: " + (tiered(loaded) or "none") + ". " + first_read,
        f"They cover these domains: {slugs}. Tiers are explained in `{CONTEXT_DIR}/{TRUST_FILE}`.",
        CONTEXT_IS_A_MAP,
        ERRATA_FIRST,
        "Extraction documents (overviews, features, integrations, reference documents) were not reviewed claim by "
        "claim; when one conflicts with a reviewed rule, the reviewed rule wins, and the code wins over both.",
        f"For anything not in your startup resources, read context before code: "
        f"`{CONTEXT_DIR}/domains/<slug>/reviewed-rules.md` and `summary.md`, `{CONTEXT_DIR}/impact-map.md`, "
        f"`{CONTEXT_DIR}/variant-map.md`, `{CONTEXT_DIR}/dependency-map.md`, `{CONTEXT_DIR}/program-config.md` "
        f"(when present), `{CONTEXT_DIR}/file-index.json` and `{CONTEXT_DIR}/review-status.md`.",
    ]
    if aids:
        lines.append("On-demand navigation aids (Tier 3, hypothesis only; never state as fact): "
                     + ", ".join(f"`{p}`" for p in aids) + ". Use them to find code to read.")
    if dropped:
        lines.append("Also available on demand (left out of startup to fit the size budget): "
                     + ", ".join(f"`{p}`" for p in dropped) + ".")
    lines += [
        _code_rule(ctx),
        PROVENANCE,
        f"Follow the `{spec.skill}` skill for the procedure and output shape; where a skill step says to open "
        "code, apply the rules above.",
        "Shell is disabled: use read tools only, and do not modify files.",
        "End your answer with a line `Context used:` followed by the context and extraction paths you relied on.",
    ]
    return "\n".join(lines)


def _delegate_lines(step: int) -> list[str]:
    lines = [f"{step}. Delegate. Pick the specialist sub-agent whose task matches the question and give it the "
             "user's full question verbatim:"]
    lines += [f"   - `{s.name}`: {s.description}" for s in SPECIALISTS]
    return lines


def _skills_list(extra: bool = False) -> str:
    names = [f"`{s.skill}` ({s.name})" for s in SPECIALISTS]
    if extra:
        names += [f"`{s}`" for s in ENTRY_EXTRA_SKILLS]
    return ", ".join(names)


KB_RETRIEVED_FIRST = (
    "0. Retrieved context first. The `kb-retrieve` hook has already added a short `## Retrieved context` "
    "summary to this conversation: verified facts from the validated knowledge base and the code-graph "
    "neighbourhood (dependents, dependencies, affected programs) of the components they and the question name. "
    f"Read it before any other step and before any tool call; open `{KB_LATEST_REL}` when you need the full fact "
    "text, the evidence lines or more neighbours. The verified facts are a map, not proof: the code is the truth, "
    "and the mandatory code check (authorization, transactions and error end states, concurrency, absolute "
    "claims) still applies to anything they say on those topics. Cite them as `[verified KB <fact-id>]` and graph "
    "edges as `[code graph <commit>]`, next to the other provenance tags. When the summary reports a graph-only "
    "fallback or no facts, carry on with the steps below."
)


def entry_prompt(ctx: RenderContext) -> str:
    """The ``brownfield`` entry agent prompt for the configured design; with a ``kb`` config section the
    retrieved-context step comes first."""
    if ctx.design == NARROW_CAPPED:
        text = capped_entry_prompt(ctx)
    elif ctx.design == DELEGATE:
        text = delegate_entry_prompt(ctx)
    elif ctx.design == HOOK_ONLY:
        text = hook_only_entry_prompt(ctx)
    elif ctx.design == KB_FIRST:
        text = kb_first_entry_prompt(ctx)
    else:
        text = hybrid_entry_prompt(ctx)
    if not ctx.config.kb:
        return text
    first, rest = text.split("\n", 1)
    return "\n".join([first, KB_RETRIEVED_FIRST, rest])


def kb_hooks(kb: dict[str, Any]) -> list[dict[str, Any]]:
    """The inline ``UserPromptSubmit`` hook for the entry agent (the form proven in Kiro CLI)."""
    from bcp.kb_hook import hook_command, hook_python

    command = hook_command(hook_python(kb.get("python")))  # type: ignore[arg-type]
    return [{"name": KB_HOOK_NAME, "trigger": "UserPromptSubmit",
             "action": {"type": "command", "command": command}, "timeout": KB_HOOK_TIMEOUT_S}]


def render_kb_files(kb: dict[str, Any]) -> dict[str, str]:
    """``.kiro/context/kb-hook.json`` (the hook's settings, read from the event's cwd) and the IDE
    variant ``.kiro/hooks/kb-retrieve.json`` (the same command as a standalone UserPromptSubmit hook)."""
    from bcp.kb_hook import GLOSSARY_REL, RETRIEVED_REL, SETTINGS_REL, SETTINGS_SCHEMA

    settings = {"schema": SETTINGS_SCHEMA, "kb_id": kb["kb_id"], "region": kb["region"], "graph": kb["graph"],
                "glossary": GLOSSARY_REL, "retrieved_dir": RETRIEVED_REL, "top_k": kb["top_k"],
                "timeout_s": kb["timeout_s"], "hops": kb["hops"]}
    base = kb_hooks(kb)[0]
    hook = {"name": base["name"],
            "description": "Before each prompt: retrieve verified facts from the knowledge base and the code-graph "
                           f"neighbourhood; prints a short summary and writes {KB_LATEST_REL}. Always exits 0.",
            "trigger": base["trigger"], "action": base["action"], "timeout": base["timeout"]}
    # Standalone hook files use the v1 schema {"version": "v1", "hooks": [...]}, PascalCase triggers and a
    # timeout in seconds: https://kiro.dev/docs/hooks/, https://kiro.dev/docs/cli/v3/migration-guide/
    hook_file = {"version": "v1", "hooks": [hook]}
    return {SETTINGS_REL: json.dumps(settings, indent=2, sort_keys=True) + "\n",
            ".kiro/hooks/kb-retrieve.json": json.dumps(hook_file, indent=2, ensure_ascii=False) + "\n"}


def hybrid_entry_prompt(ctx: RenderContext) -> str:
    """Default design: scope; answer narrow questions after reading the matched domains' reviewed rules;
    delegate broad ones."""
    lines = [
        f"You are `{ENTRY}`, the entry agent for questions about {ctx.project_name}.",
        f"1. Scope the question first, using the domain list in `{STEERING_DIR}/{INDEX_FILE}` (already loaded):",
        NARROW_SCOPE,
        BROAD_SCOPE,
        f"2. Answer narrow questions yourself. Before every narrow answer, read "
        f"`{CONTEXT_DIR}/domains/<slug>/reviewed-rules.md` for each matched domain, in one parallel batch "
        f"(`{ERRATA_REL}` and `{FRAMEWORK_REL}` are already loaded). Add `{CONTEXT_DIR}/domains/<slug>/summary.md`, "
        f"`{CONTEXT_DIR}/impact-map.md` or `{CONTEXT_DIR}/variant-map.md` to that same batch when the question asks "
        f"about dependents, other domains or configuration (`{CONTEXT_DIR}/dependency-map.md` for dependencies, "
        f"`{CONTEXT_DIR}/program-config.md` for per-program values, when present). Then open the code the mandatory "
        f"check requires. Follow the matching skill for the procedure and output shape ({_skills_list(True)}).",
    ]
    lines += _delegate_lines(3)
    lines += [
        "   A question that spans two tasks goes to the main one first; call a second specialist only if the "
        "first answer leaves part of the question open. Answer the whole question from the specialist results, "
        "keeping their BR ids, citations and provenance tags.",
        f"4. {CONTEXT_IS_A_MAP} {ERRATA_FIRST}",
        f"5. {MANDATORY_CHECK} {CODE_RULE}",
        f"6. {PROVENANCE}",
        "7. Shell is disabled: use read tools and sub-agents only, and do not modify files.",
        "8. End with a line `Context used:` followed by the context and extraction paths the answer relied on.",
    ]
    return "\n".join(lines)


def delegate_entry_prompt(ctx: RenderContext) -> str:
    """Opt-in ``delegate`` design: every question goes to a specialist."""
    lines = [f"You are `{ENTRY}`, the entry agent for questions about {ctx.project_name}."]
    lines += _delegate_lines(1)
    lines += [
        "   A question that spans two tasks goes to the main one first; call a second specialist only if the "
        "first answer leaves part of the question open.",
        f"2. {CONTEXT_IS_A_MAP}",
        f"3. {MANDATORY_CHECK} {CODE_RULE}",
        f"4. Start from `{STEERING_DIR}/{INDEX_FILE}` (already loaded). Shell is disabled: use read tools and "
        "sub-agents only, and do not modify files.",
        "5. Answer the whole question from the specialist results, keeping their BR ids, citations and provenance "
        "tags.",
        "6. End with a line `Context used:` followed by the context and extraction paths the answer relied on.",
    ]
    return "\n".join(lines)


def capped_entry_prompt(ctx: RenderContext) -> str:
    """The narrow-capped ``brownfield`` prompt: scope, narrow fast path with a code-read cap, delegate."""
    lines = [
        f"You are `{ENTRY}`, the entry agent for questions about {ctx.project_name}.",
        f"1. Scope the question first, using the domain list in `{STEERING_DIR}/{INDEX_FILE}` (already loaded):",
        NARROW_SCOPE,
        BROAD_SCOPE,
        f"2. Narrow fast path. In one parallel batch, read `{CONTEXT_DIR}/domains/<slug>/reviewed-rules.md` for "
        f"the domain or two involved; add `{CONTEXT_DIR}/domains/<slug>/summary.md`, `{CONTEXT_DIR}/impact-map.md` "
        f"or `{CONTEXT_DIR}/variant-map.md` to that same batch only when the question asks about dependents, "
        f"other domains or configuration. Use the matching skill for the procedure and output shape "
        f"({_skills_list()}), applying rule 5 wherever a skill step says to open code.",
    ]
    lines += _delegate_lines(3)
    lines += [
        "   A question that spans two tasks goes to the main one first; call a second specialist only if the "
        "first answer leaves part of the question open. Answer from the specialist result, keeping its BR ids "
        "and citations; do not re-open code it already cited.",
        f"4. {CONTEXT_IS_A_MAP} {ERRATA_FIRST}",
        f"5. {CAPPED_CODE_RULE}",
        f"6. {PROVENANCE} Shell is disabled: use read tools and sub-agents only, and do not modify files.",
        "7. End with a line `Context used:` followed by the context and extraction paths the answer relied on.",
    ]
    return "\n".join(lines)


def fast_entry_prompt(ctx: RenderContext) -> str:
    """The ``brownfield-fast`` prompt: the delegate design's rules, but narrow questions are answered by
    the entry agent itself. No code-read cap."""
    lines = [
        f"You are `{FAST_ENTRY}`, the entry agent for questions about {ctx.project_name}.",
        f"1. Scope the question first, using the domain list in `{STEERING_DIR}/{INDEX_FILE}` (already loaded):",
        NARROW_SCOPE,
        BROAD_SCOPE,
        f"2. Answer narrow questions yourself. Read `{CONTEXT_DIR}/domains/<slug>/reviewed-rules.md` for the "
        f"domain or two involved, and `{CONTEXT_DIR}/domains/<slug>/summary.md`, `{CONTEXT_DIR}/impact-map.md` or "
        f"`{CONTEXT_DIR}/variant-map.md` when the question asks about dependents, other domains or configuration. "
        f"Follow the matching skill for the procedure and output shape ({_skills_list()}); where a skill step "
        "says to open code, apply rule 5.",
    ]
    lines += _delegate_lines(3)
    lines += [
        "   A question that spans two tasks goes to the main one first; call a second specialist only if the "
        "first answer leaves part of the question open. Answer the whole question from the specialist results, "
        "keeping their BR ids and citations.",
        f"4. {CONTEXT_IS_A_MAP}",
        f"5. {MANDATORY_CHECK} {CODE_RULE}",
        f"6. {PROVENANCE} Shell is disabled: use read tools and sub-agents only, and do not modify files.",
        "7. End with a line `Context used:` followed by the context and extraction paths the answer relied on.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- hook-only entry agent (opt-in)

HOOK_ONLY_ENTRY_DESCRIPTION = (
    "Entry agent for questions about this system: change impact and pull-request review, onboarding and "
    "how flows work, defect and incident triage, requirement gaps. Answers directly from the retrieved "
    "verified facts, the reviewed rules and the code, with no sub-agent hop; hand genuinely system-wide "
    "work to a specialist agent."
)


def hook_only_entry_prompt(ctx: RenderContext) -> str:
    """The ``hook-only`` ``brownfield`` prompt: answer from the hook's retrieved context, the reviewed
    rules and the code, without a sub-agent. It names the specialists so genuinely system-wide work is
    handed to one directly (it has no ``subagent/*`` tools, so it cannot delegate)."""
    lines = [
        f"You are `{ENTRY}`, the entry agent for questions about {ctx.project_name}. You answer directly, "
        "without a sub-agent.",
        f"1. Scope the question first, using the `## Retrieved context` summary from step 0 and the domain "
        f"list in `{STEERING_DIR}/{INDEX_FILE}` (already loaded).",
        f"2. Answer the question yourself. The retrieved summary and `{KB_LATEST_REL}` already name the "
        f"verified facts and the code-graph neighbourhood; read `{CONTEXT_DIR}/domains/<slug>/reviewed-rules.md` "
        f"for each domain the question touches, in one parallel batch, and add "
        f"`{CONTEXT_DIR}/domains/<slug>/summary.md`, `{CONTEXT_DIR}/impact-map.md` or "
        f"`{CONTEXT_DIR}/variant-map.md` when it asks about dependents, other domains or configuration "
        f"(`{CONTEXT_DIR}/dependency-map.md` for dependencies, `{CONTEXT_DIR}/program-config.md` for "
        f"per-program values, when present). Then open the code the mandatory check requires. Follow the "
        f"matching skill for the procedure and output shape ({_skills_list(True)}). Do not start a sub-agent.",
        "3. For genuinely system-wide work (a change across three or more domains, an end-to-end flow "
        "walkthrough or a broad impact sweep), hand it to the specialist agent built for it and invoke that "
        "agent directly:",
    ]
    lines += [f"   - `{s.name}`: {s.description}" for s in SPECIALISTS]
    lines += [
        f"4. {CONTEXT_IS_A_MAP} {ERRATA_FIRST}",
        f"5. {MANDATORY_CHECK} {CODE_RULE}",
        f"6. {PROVENANCE}",
        "7. Shell is disabled: use read tools only, and do not modify files.",
        "8. End with a line `Context used:` followed by the context and extraction paths the answer relied on.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- kb-first entry agent (opt-in)

KB_FIRST_ENTRY_DESCRIPTION = (
    "Entry agent for questions about this system: change impact and pull-request review, onboarding and "
    "how flows work, defect and incident triage, requirement gaps. Answers from the verified-fresh facts "
    "the kb-retrieve hook returns, citing their fact IDs and recorded evidence lines without opening the "
    "code; opens the code only for stale or unverified facts, questions no fact covers, or a task that "
    "changes code. Hands genuinely system-wide work to a specialist agent."
)


def kb_first_entry_prompt(ctx: RenderContext) -> str:
    """The ``kb-first`` ``brownfield`` prompt: answer from the verified-fresh facts and their recorded
    evidence lines without opening the code for them; open the code only for a stale or unverified fact,
    a question no fact covers, or a task that changes code. No sub-agent hop (it names the specialists so
    system-wide work is handed to one directly)."""
    lines = [
        f"You are `{ENTRY}`, the entry agent for questions about {ctx.project_name}. You answer directly "
        "from the verified knowledge base, without a sub-agent.",
        f"1. Scope the question using the `## Retrieved context` summary from step 0 and the domain list in "
        f"`{STEERING_DIR}/{INDEX_FILE}` (already loaded).",
        "2. Answer from the verified-fresh facts first. For each fact the retrieved summary tags "
        "`[verified-fresh]`, answer from its claim and the evidence lines already recorded for it (its fact "
        f"id and `path#Lx-Ly`); do NOT open the code for it. Its recorded evidence satisfies the mandatory "
        f"check for the topic it covers. Open `{KB_LATEST_REL}` for the full fact text, evidence lines or more "
        f"neighbours when you need them. Follow the matching skill for the procedure and output shape "
        f"({_skills_list(True)}). Do not start a sub-agent.",
        "3. Open the code only when the answer needs it: a fact the summary tags `[verified-stale]` or that "
        "is quarantined, a point that rests on unreviewed or unverified extraction, a question no fact "
        "covers, or a task that changes code or asks you to verify against the code. Then apply the mandatory "
        "code check (rule 6).",
        "4. For genuinely system-wide work (a change across three or more domains, an end-to-end flow "
        "walkthrough or a broad impact sweep), hand it to the specialist agent built for it and invoke that "
        "agent directly:",
    ]
    lines += [f"   - `{s.name}`: {s.description}" for s in SPECIALISTS]
    lines += [
        f"5. {CONTEXT_IS_A_MAP} {ERRATA_FIRST}",
        f"6. {MANDATORY_CHECK}",
        f"7. {PROVENANCE}",
        "8. Shell is disabled: use read tools only, and do not modify files.",
        "9. End with a line `Context used:` followed by the context and extraction paths the answer relied on.",
    ]
    return "\n".join(lines)


def _rules(*extra: dict[str, Any]) -> dict[str, Any]:
    return {"rules": [{"capability": "fs_read", "effect": "allow"}, *extra,
                      {"capability": "shell", "effect": "deny"}]}


def _measure(files: list[str], size_of: SizeOf, prompt: str, description: str, skill: str | None) -> int:
    total = sum(size_of(p) or 0 for p in files) + len(prompt.encode("utf-8")) + len(description.encode("utf-8"))
    if skill:
        total += size_of(_skill_rel(skill)) or 0
    return total


def pinned_steering(ctx: RenderContext, size_of: SizeOf) -> list[str]:
    """The always-included steering every generated agent lists explicitly: product, tech, structure and
    the context index (not the default-agent pointer). The Kiro steering docs say custom agents do not get
    steering automatically (https://kiro.dev/docs/steering/); the agent configuration reference says they
    inherit it (https://kiro.dev/docs/custom-agents/configuration-reference/). Listing it works under both.
    The context index is ``auto`` inclusion (not always), so it is pinned here directly rather than through
    :func:`always_files`, which keeps every agent listing it even though it no longer loads in every
    session."""
    pointer = f"{STEERING_DIR}/{POINTER_FILE}"
    paths = [p for p in always_files(ctx) if p != pointer] + [f"{STEERING_DIR}/{INDEX_FILE}"]
    return [p for p in paths if size_of(p) is not None]


def _cap_problem(files: list[str], rel: str, size_of: SizeOf) -> str | None:
    """The Kiro file:// cap that adding ``rel`` to ``files`` would break (bytes >= characters, so byte
    counts are a safe stand-in), else None."""
    size = size_of(rel) or 0
    if size > KIRO_AGENT_MAX_FILE_CHARS:
        return f"Kiro truncates file:// files over {KIRO_AGENT_MAX_FILE_CHARS} characters"
    if len(files) >= KIRO_AGENT_MAX_FILES:
        return f"Kiro loads at most {KIRO_AGENT_MAX_FILES} file:// files"
    if sum(size_of(p) or 0 for p in files) + size > KIRO_AGENT_MAX_CHARS:
        return f"Kiro loads at most {KIRO_AGENT_MAX_CHARS} file:// characters"
    return None


def plan_specialist(ctx: RenderContext, spec: Specialist, size_of: SizeOf) -> AgentPlan:
    """Resources and prompt for one specialist within the advisory budget (a bcp heuristic) and Kiro's
    documented 32-file / 100,000-character / 50,000-character-per-file caps.

    The pinned steering (:func:`pinned_steering`) is listed first and never dropped. Other files are
    taken in relevance order and kept while they fit; a file that does not fit after the more relevant
    ones is dropped (logged, and named in the prompt as read-on-demand), and smaller less relevant files
    may still fill the remaining space.
    """
    domains = agent_domains(ctx)
    pinned = pinned_steering(ctx, size_of)
    ordered = [p for p in candidates(ctx, spec, domains, size_of) if p not in pinned]
    aids = on_demand_aids(ctx, spec, domains, size_of)
    budget = ctx.config.agent_budget_bytes
    loaded: list[str] = []
    for rel in ordered:
        cap = _cap_problem(pinned + loaded, rel, size_of)
        if cap is not None:
            log.warning("agent %s: dropped %s (%d bytes): %s (https://kiro.dev/docs/custom-agents/"
                        "configuration-reference/)", spec.name, rel, size_of(rel) or 0, cap)
            continue
        trial = loaded + [rel]
        others = [p for p in ordered if p not in trial]
        prompt = specialist_prompt(ctx, spec, trial, others, domains, aids)
        if _measure(pinned + trial, size_of, prompt, spec.description, spec.skill) <= budget:
            loaded = trial
        else:
            log.warning("agent %s: dropped %s (%d bytes) to fit the advisory agent_budget_bytes %d (bcp heuristic)",
                        spec.name, rel, size_of(rel) or 0, budget)
    dropped = [p for p in ordered if p not in loaded]
    prompt = specialist_prompt(ctx, spec, loaded, dropped, domains, aids)
    total = _measure(pinned + loaded, size_of, prompt, spec.description, spec.skill)
    if total > budget:
        log.warning("agent %s: %d startup bytes still over the advisory agent_budget_bytes %d (bcp heuristic)",
                    spec.name, total, budget)
    data = {
        "name": spec.name,
        "description": spec.description,
        "prompt": prompt,
        "tools": ["read"],
        "resources": [f"file://{p}" for p in pinned + loaded] + [f"skill://{_skill_rel(spec.skill)}"],
        "permissions": _rules(),
    }
    return AgentPlan(spec.name, data, loaded, dropped, total, aids)


def _plan_router(ctx: RenderContext, size_of: SizeOf, name: str, description: str, prompt: str,
                 with_skills: bool, extra_files: tuple[str, ...] = (),
                 extra_skills: tuple[str, ...] = (), with_subagents: bool = True) -> AgentPlan:
    """An entry agent: read (plus ``subagent/*`` tools unless ``with_subagents`` is false), the pinned
    steering (foundation and index), optional extra context files and (optionally) the specialists' task
    skills plus ``extra_skills``, so it can follow them when it answers itself. The hook-only design sets
    ``with_subagents=False`` so the agent cannot hop to a sub-agent."""
    names = [s.name for s in SPECIALISTS]
    pinned = pinned_steering(ctx, size_of)
    loaded = pinned + [p for p in extra_files if p not in pinned and size_of(p) is not None]
    wanted = [s.skill for s in SPECIALISTS] + list(extra_skills) if with_skills else []
    skills = [s for s in wanted if size_of(_skill_rel(s)) is not None]
    total = _measure(loaded, size_of, prompt, description, None) + sum(size_of(_skill_rel(s)) or 0 for s in skills)
    if total > ctx.config.agent_budget_bytes:
        log.warning("agent %s: %d startup bytes over the advisory agent_budget_bytes %d (bcp heuristic)", name,
                    total, ctx.config.agent_budget_bytes)
    file_bytes = sum(size_of(p) or 0 for p in loaded)
    if (len(loaded) > KIRO_AGENT_MAX_FILES or file_bytes > KIRO_AGENT_MAX_CHARS
            or any((size_of(p) or 0) > KIRO_AGENT_MAX_FILE_CHARS for p in loaded)):
        log.warning("agent %s: %d file:// files, %d bytes break Kiro's documented file:// caps (%d files, %d "
                    "characters, %d per file; https://kiro.dev/docs/custom-agents/configuration-reference/)",
                    name, len(loaded), file_bytes, KIRO_AGENT_MAX_FILES, KIRO_AGENT_MAX_CHARS,
                    KIRO_AGENT_MAX_FILE_CHARS)
    tools = ["read"] + ([f"subagent/{n}" for n in names] if with_subagents else [])
    extra_rules = ({"capability": "subagent", "match": names, "effect": "allow"},) if with_subagents else ()
    data = {
        "name": name,
        "description": description,
        "prompt": prompt,
        "tools": tools,
        "resources": [f"file://{p}" for p in loaded] + [f"skill://{_skill_rel(s)}" for s in skills],
        "permissions": _rules(*extra_rules),
    }
    return AgentPlan(name, data, loaded, [], total)


def plan_entry(ctx: RenderContext, size_of: SizeOf) -> AgentPlan:
    """The ``brownfield`` entry agent. The hybrid, narrow-capped and hook-only designs attach the task
    skills; the hybrid design also preloads the errata and the framework rules; the hook-only design has
    no ``subagent/*`` tools."""
    if ctx.design == DELEGATE:
        plan = _plan_router(ctx, size_of, ENTRY, DELEGATE_ENTRY_DESCRIPTION, entry_prompt(ctx), with_skills=False)
    elif ctx.design == NARROW_CAPPED:
        plan = _plan_router(ctx, size_of, ENTRY, CAPPED_ENTRY_DESCRIPTION, entry_prompt(ctx), with_skills=True)
    elif ctx.design == HOOK_ONLY:
        plan = _plan_router(ctx, size_of, ENTRY, HOOK_ONLY_ENTRY_DESCRIPTION, entry_prompt(ctx),
                            with_skills=True, with_subagents=False, extra_skills=ENTRY_EXTRA_SKILLS)
    elif ctx.design == KB_FIRST:
        plan = _plan_router(ctx, size_of, ENTRY, KB_FIRST_ENTRY_DESCRIPTION, entry_prompt(ctx),
                            with_skills=True, with_subagents=False, extra_skills=ENTRY_EXTRA_SKILLS)
    else:
        plan = _plan_router(ctx, size_of, ENTRY, ENTRY_DESCRIPTION, entry_prompt(ctx), with_skills=True,
                            extra_files=(ERRATA_REL, FRAMEWORK_REL), extra_skills=ENTRY_EXTRA_SKILLS)
    if ctx.config.kb:
        plan.data["hooks"] = kb_hooks(ctx.config.kb)
    return plan


def plan_fast_entry(ctx: RenderContext, size_of: SizeOf) -> AgentPlan:
    """The opt-in ``brownfield-fast`` entry agent (task skills attached, delegate code rule)."""
    return _plan_router(ctx, size_of, FAST_ENTRY, FAST_ENTRY_DESCRIPTION, fast_entry_prompt(ctx), with_skills=True)


def plan_agents(ctx: RenderContext, size_of: SizeOf) -> list[AgentPlan]:
    """The entry agent, the fast entry agent when enabled, then every specialist."""
    plans = [plan_entry(ctx, size_of)]
    if ctx.config.fast_entry_agent:
        plans.append(plan_fast_entry(ctx, size_of))
    return plans + [plan_specialist(ctx, s, size_of) for s in SPECIALISTS]


def render_agents(plans: list[AgentPlan]) -> dict[str, str]:
    """``.kiro/agents/<name>.json`` for every plan."""
    return {f"{AGENTS_DIR}/{p.name}.json": json.dumps(p.data, indent=2, ensure_ascii=False) + "\n" for p in plans}
