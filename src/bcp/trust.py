"""Trust tiers for generated context and extraction files.

Tier 1 is mechanical (derived by tools, or maintained by the team). Tier 2 is AI narrative, useful
for orientation. Tier 3 is hypothesis only: it may point to the right code, but it is never stated as
fact without reading that code. Markdown context files carry a ``> Trust:`` line straight after the
generated marker; JSON files cannot, so their tier is named in agent prompts and in
``.kiro/context/trust-tiers.md``.
"""

from __future__ import annotations

import fnmatch

TIER1 = 1
TIER2 = 2
TIER3 = 3
LABELS = {
    TIER1: "mechanical",
    TIER2: "AI narrative, for orientation",
    TIER3: "hypothesis only",
}
TRUST_PREFIX = "> Trust: "

# Path patterns, matched against "/" + the forward-slash path (``*`` crosses ``/``). Tier 1 and
# Tier 3 are explicit; anything else (including an unknown extraction path) is Tier 2.
TIER1_PATTERNS = (
    "*/file-index.json",
    "*/citation-baseline.json",
    "*/generation-record.json",
    "*/review-status.md",
    "*/trust-tiers.md",
    "*/framework-rules.md",
    "*/dependency-map.md",
    "*/dependency-map.json",
    "*/database-documentation/*",
    "*/domain-decomposition-overview/data-ownership.md",
)
TIER3_PATTERNS = (
    "*/workflows.md",
    "*/diagrams/*",
    "*/cross-cutting-documentation/*",
    "*/external-integrations-map.md",
    "*/coupling-hotspots.md",
    "*/dead-code*",
    "*/cron-daemon*",
    "*/security-model.md",
)
TIER2_EXAMPLES = (
    "reviewed-rules.md (each rule carries its own review status)",
    "errata.md",
    "summary.md",
    "impact-map.md / impact-map.json",
    "variant-map.md / variant-map.json",
    "program-config.md / program-config.json (values imported from configuration exports; behaviour notes are "
    "Tier 2)",
    "glossary.md",
    "extraction overview.md, features.md, validations.md, integrations.md",
    "bounded-contexts.md, decomposition-overview.md, feature-flag-inventory.md, api-endpoint-catalog.md",
)
TIER1_EXAMPLES = (
    "file-index.json, citation-baseline.json (tool-derived)",
    "review-status.md (counts from the review record)",
    "framework-rules.md (team-maintained framework semantics)",
    "dependency-map.md / dependency-map.json (code-derived edges; its bre-only edges are Tier 3 claims to verify)",
    "extraction database-documentation/* and data-ownership.md (schema-derived)",
)
TIER3_EXAMPLES = (
    "extraction workflows.md",
    "every diagram (diagrams/* and per-domain diagrams)",
    "cross-cutting-documentation/* (error handling, error codes, integration and event patterns)",
    "external-integrations-map.md, coupling-hotspots.md, dead-code and cron/daemon inventories, security-model.md",
)


def _match(path: str, patterns: tuple[str, ...]) -> bool:
    probe = "/" + path.replace("\\", "/").lstrip("/").lower()
    return any(fnmatch.fnmatchcase(probe, pat) for pat in patterns)


def tier_for(rel: str) -> int:
    """Trust tier of a workspace-, context- or extraction-relative path."""
    if _match(rel, TIER1_PATTERNS):
        return TIER1
    if _match(rel, TIER3_PATTERNS):
        return TIER3
    return TIER2


def trust_line(tier: int, note: str = "") -> str:
    """``> Trust: Tier N: <label>; <note>``."""
    line = f"{TRUST_PREFIX}Tier {tier}: {LABELS[tier]}"
    return f"{line}; {note}" if note else line


def tier_tag(rel: str) -> str:
    """``(Tier N)`` for a path."""
    return f"(Tier {tier_for(rel)})"


def render_legend() -> str:
    """Body of ``.kiro/context/trust-tiers.md`` (without the generated marker)."""
    lines = [
        "# Trust tiers",
        "",
        "Every generated context file names its tier on a `> Trust:` line. The context is a map; the code is "
        "the truth. Use the tiers to decide how much to check before you rely on a file.",
        "",
        f"## Tier 1: {LABELS[TIER1]}",
        "",
        "Derived by tools from the code, the schema or the review record, or maintained by the team. Reliable "
        "for what it records; it records no behaviour.",
        "",
    ]
    lines += [f"- {x}" for x in TIER1_EXAMPLES]
    lines += [
        "",
        f"## Tier 2: {LABELS[TIER2]}",
        "",
        "Written by AI from static reading. The main behaviour is usually right; side details are often wrong. "
        "A reviewed rule says what a reviewer checked. Check the code before you state a detail as fact.",
        "",
    ]
    lines += [f"- {x}" for x in TIER2_EXAMPLES]
    lines += [
        "",
        f"## Tier 3: {LABELS[TIER3]}",
        "",
        "Not reviewed, and often wrong about end states, error paths and ordering. Use it to find code to "
        "read. Never state it as fact. These files are read on demand, never preloaded.",
        "",
    ]
    lines += [f"- {x}" for x in TIER3_EXAMPLES]
    return "\n".join(lines)
