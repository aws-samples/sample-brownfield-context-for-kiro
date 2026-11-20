"""``bcp.config.json`` loading and validation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

from bcp.errors import BcpInputError
from bcp.textio import is_safe_relpath, read_text

log = logging.getLogger(__name__)

DEFAULT_EXTENSIONS = [
    ".java", ".kt", ".cs", ".py", ".ts", ".js", ".go", ".rb", ".scala", ".groovy",
    ".xml", ".sql", ".properties", ".yml", ".yaml",
]
DEFAULT_EXCLUDE_DIRS = [
    ".git", ".kiro", "node_modules", "target", "build", "dist", "bin", "obj", ".venv",
    "business-rules-extraction",
]
DEFAULT_INVENTORY_GLOBS = ["*feature-flag*", "*config*inventory*", "*global-propert*"]
DEFAULT_RULE_HEADING = r"^##\s+(BR-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)\s*[:.—-]?\s*(.*)$"
DEFAULT_SOURCE_LINE = r"^\s*[-*]\s*\**(?:Source|Evidence)\**\s*:?\**\s*(.+)$"
# Agent and context designs. ``hybrid`` (default): the entry agent answers narrow (one or two domain)
# questions itself after reading those domains' reviewed rules, and delegates broad ones to a
# specialist. ``delegate`` (opt-in): the entry agent routes every question to a specialist.
# ``narrow-capped`` (opt-in, experimental): VERIFIED rules carry ``> Anchors`` lines, the entry agent
# answers narrow questions itself, and code reads are capped per answer. ``hook-only`` (opt-in, requires a
# ``kb`` section): the entry agent keeps the ``kb-retrieve`` hook and answers directly from the injected
# retrieved context, the reviewed rules and the code, with no sub-agent hop; specialists stay callable.
AGENT_DESIGNS = ("hybrid", "delegate", "narrow-capped", "hook-only", "kb-first")
DEFAULT_AGENT_DESIGN = "hybrid"
FAST_ENTRY_DESIGN = "delegate"  # the opt-in brownfield-fast agent exists only next to the delegate design
HOOK_ONLY_DESIGN = "hook-only"  # recommended default for Q&A personas; needs a kb section
KB_FIRST_DESIGN = "kb-first"  # answers from verified-fresh facts without opening code; needs a kb section
KB_SECTION_DESIGNS = (HOOK_ONLY_DESIGN, KB_FIRST_DESIGN)  # designs that require a kb section
DEFAULT_GUARD_ANNOTATIONS = ["PreAuthorize", "PostAuthorize", "PreFilter", "PostFilter", "Secured",
                             "RolesAllowed", "Authorized"]
FRAMEWORK_RULE_KEYWORDS = ("auto", "java-spring", "blank")
# Per-program configuration map (``bcp variants``). A constant definition: group 1 the name, group 2 the
# literal key. A default definition: named groups ``key`` (a constant, a qualified constant or a quoted
# literal) and ``default``; earlier patterns win (a declared seed default before a getter fallback).
# Constant names must be UPPER_SNAKE so markup attributes (``value="..."``) are not taken for constants.
DEFAULT_VARIANT_CONSTANT_PATTERNS = [r'\b([A-Z][A-Z0-9_]*)\s*=\s*"([A-Za-z][\w.\-]+)"']
DEFAULT_VARIANT_DEFAULT_PATTERNS = [
    r'new\s+\w*Propert\w*\(\s*(?P<key>[\w.]+|"[^"]+")\s*,\s*(?P<default>"[^"]*"|[\w.]+(?:\(\))?)',
    r'get\w*Propert\w*\(\s*(?P<key>[\w.]+|"[^"]+")\s*,\s*(?P<default>"[^"]*"|[\w.]+)\s*\)',
]
DEFAULT_VARIANT_MASK_PATTERNS = ["(?i)password|passwd|secret|token|credential|private|apikey|api_key"]
# Integration points for the dependency map: label → regex (matched on code with comments and string
# contents blanked).
DEFAULT_INTEGRATION_PATTERNS = {
    "http": r"HttpURLConnection|RestTemplate|WebClient|HttpClient|\bURL\(",
    "messaging": r"JmsTemplate|KafkaTemplate|RabbitTemplate|@\w*Listener\b",
    "scheduling": r"@Scheduled\b|TimerTask|ScheduledExecutorService",
    "events": r"ApplicationEventPublisher|publishEvent\(",
    "file": r"FileOutputStream|Files\.write",
}


@dataclass
class BcpConfig:
    """Effective configuration; every key is optional in the JSON file."""

    project_name: str | None = None
    source_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    exclude_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS))
    variant_inventory_globs: list[str] = field(default_factory=lambda: list(DEFAULT_INVENTORY_GLOBS))
    variant_key_patterns: list[str] = field(default_factory=list)
    glossary_extra: Path | None = None
    # Advisory bcp heuristics (WARN only), not Kiro limits; see references/config-reference.md.
    always_budget_bytes: int = 16000
    domain_budget_bytes: int = 50000
    agent_budget_bytes: int = 81920
    agent_domains: list[str] | None = None
    agent_design: str = DEFAULT_AGENT_DESIGN
    fast_entry_agent: bool = False
    main_source_dirs: list[str] | None = None
    rule_heading_pattern: str = DEFAULT_RULE_HEADING
    source_line_pattern: str = DEFAULT_SOURCE_LINE
    guard_annotations: list[str] = field(default_factory=lambda: list(DEFAULT_GUARD_ANNOTATIONS))
    framework_rules: str = "auto"
    variant_constant_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_VARIANT_CONSTANT_PATTERNS))
    variant_default_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_VARIANT_DEFAULT_PATTERNS))
    variant_mask_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_VARIANT_MASK_PATTERNS))
    program_snapshots: list[str] = field(default_factory=list)
    integration_patterns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_INTEGRATION_PATTERNS))
    # Optional verified knowledge base (``bcp kb hook``): kb_id, region, graph (+ python, top_k, timeout_s,
    # hops). When set, ``generate`` adds the retrieval hook to the ``brownfield`` entry agent.
    kb: dict[str, object] | None = None

    @property
    def rule_heading_re(self) -> re.Pattern[str]:
        """Compiled rule heading regex."""
        return re.compile(self.rule_heading_pattern)

    @property
    def source_line_re(self) -> re.Pattern[str]:
        """Compiled citation-line regex (matches ``Source:`` or ``Evidence:`` bullets by default)."""
        return re.compile(self.source_line_pattern)


def _str_list(key: str, value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BcpInputError(f"config key {key!r} must be a list of strings")
    return list(value)


def _int_range(key: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BcpInputError(f"config key {key!r} must be an integer in {low}..{high}")
    return value


def _regex(key: str, value: object, groups: int | None = None) -> str:
    if not isinstance(value, str):
        raise BcpInputError(f"config key {key!r} must be a regex string")
    try:
        compiled = re.compile(value)
    except re.error as exc:
        raise BcpInputError(f"config key {key!r} does not compile: {exc}") from exc
    if groups is not None and compiled.groups < groups:
        raise BcpInputError(f"config key {key!r} needs at least {groups} groups")
    return value


def check_agent_options(cfg: BcpConfig) -> None:
    """The fast entry agent is defined on the ``delegate`` design only (``hybrid`` already answers
    narrow questions in the entry agent). The ``hook-only`` and ``kb-first`` designs need a ``kb``
    section: their whole point is to answer from the hook's retrieved context without a sub-agent hop."""
    if cfg.agent_design not in AGENT_DESIGNS:
        raise BcpInputError(f"agent design must be one of {', '.join(AGENT_DESIGNS)}")
    if cfg.fast_entry_agent and cfg.agent_design != FAST_ENTRY_DESIGN:
        raise BcpInputError(f"fast_entry_agent works only with agent_design {FAST_ENTRY_DESIGN!r}")
    if cfg.agent_design in KB_SECTION_DESIGNS and not cfg.kb:
        raise BcpInputError(f"agent_design {cfg.agent_design!r} requires a kb section")


def from_dict(raw: dict[str, object], base_dir: Path | None = None) -> BcpConfig:
    """Validate a config mapping; ``base_dir`` resolves ``glossary_extra``."""
    cfg = BcpConfig()
    known = {f.name for f in fields(BcpConfig)}
    for key in sorted(raw):
        if key not in known:
            log.warning("unknown config key ignored: %s", key)
    if "project_name" in raw:
        value = raw["project_name"]
        if not isinstance(value, str) or not value.strip() or len(value) > 80:
            raise BcpInputError("config key 'project_name' must be a non-empty string of at most 80 chars")
        cfg.project_name = value.strip()
    if "source_extensions" in raw:
        exts = _str_list("source_extensions", raw["source_extensions"])
        if not all(e.startswith(".") and len(e) > 1 for e in exts):
            raise BcpInputError("config key 'source_extensions': each entry must start with '.'")
        cfg.source_extensions = exts
    if "exclude_dirs" in raw:
        dirs = _str_list("exclude_dirs", raw["exclude_dirs"])
        if not all(d and "/" not in d and "\\" not in d for d in dirs):
            raise BcpInputError("config key 'exclude_dirs': names only, no '/'")
        cfg.exclude_dirs = dirs
    if "variant_inventory_globs" in raw:
        cfg.variant_inventory_globs = _str_list("variant_inventory_globs", raw["variant_inventory_globs"])
    if "variant_key_patterns" in raw:
        pats = _str_list("variant_key_patterns", raw["variant_key_patterns"])
        for pat in pats:
            _regex("variant_key_patterns", pat)
        cfg.variant_key_patterns = pats
    if "glossary_extra" in raw and raw["glossary_extra"] is not None:
        value = raw["glossary_extra"]
        if not isinstance(value, str) or not value.strip():
            raise BcpInputError("config key 'glossary_extra' must be a path string")
        path = Path(value)
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        if not path.is_file():
            log.warning("glossary_extra file not found: %s", path)
            cfg.glossary_extra = None
        else:
            cfg.glossary_extra = path
    if "always_budget_bytes" in raw:
        cfg.always_budget_bytes = _int_range("always_budget_bytes", raw["always_budget_bytes"], 2000, 100000)
    if "domain_budget_bytes" in raw:
        cfg.domain_budget_bytes = _int_range("domain_budget_bytes", raw["domain_budget_bytes"], 5000, 500000)
    if "agent_budget_bytes" in raw:
        cfg.agent_budget_bytes = _int_range("agent_budget_bytes", raw["agent_budget_bytes"], 10000, 1000000)
    if "agent_domains" in raw and raw["agent_domains"] is not None:
        slugs = _str_list("agent_domains", raw["agent_domains"])
        if not slugs or not all(s.strip() for s in slugs):
            raise BcpInputError("config key 'agent_domains' must be a non-empty list of domain slugs")
        cfg.agent_domains = [s.strip() for s in slugs]
    if "agent_design" in raw:
        value = raw["agent_design"]
        if value not in AGENT_DESIGNS:
            raise BcpInputError(f"config key 'agent_design' must be one of {', '.join(AGENT_DESIGNS)}")
        cfg.agent_design = str(value)
    if "fast_entry_agent" in raw:
        if not isinstance(raw["fast_entry_agent"], bool):
            raise BcpInputError("config key 'fast_entry_agent' must be true or false")
        cfg.fast_entry_agent = raw["fast_entry_agent"]
    if "main_source_dirs" in raw and raw["main_source_dirs"] is not None:
        dirs = _str_list("main_source_dirs", raw["main_source_dirs"])
        for d in dirs:
            if "\\" in d or not is_safe_relpath(d):
                raise BcpInputError(
                    "config key 'main_source_dirs': repo-relative, forward slashes, no '..', not absolute"
                )
        cfg.main_source_dirs = [d.rstrip("/") for d in dirs]
    if "rule_heading_pattern" in raw:
        cfg.rule_heading_pattern = _regex("rule_heading_pattern", raw["rule_heading_pattern"], 2)
    if "source_line_pattern" in raw:
        cfg.source_line_pattern = _regex("source_line_pattern", raw["source_line_pattern"], 1)
    if "guard_annotations" in raw:
        names = _str_list("guard_annotations", raw["guard_annotations"])
        if not names or not all(re.fullmatch(r"[A-Za-z_][\w.]*", n.lstrip("@")) for n in names):
            raise BcpInputError("config key 'guard_annotations' must be a non-empty list of annotation names")
        cfg.guard_annotations = [n.lstrip("@") for n in names]
    if "framework_rules" in raw:
        value = raw["framework_rules"]
        if not isinstance(value, str) or not value.strip():
            raise BcpInputError("config key 'framework_rules' must be auto, java-spring, blank or a file path")
        if value in FRAMEWORK_RULE_KEYWORDS:
            cfg.framework_rules = value
        else:
            path = Path(value)
            if not path.is_absolute() and base_dir is not None:
                path = base_dir / path
            if not path.is_file():
                raise BcpInputError(f"config key 'framework_rules': file not found: {path}")
            cfg.framework_rules = str(path)
    if "variant_constant_patterns" in raw:
        pats = _str_list("variant_constant_patterns", raw["variant_constant_patterns"])
        for pat in pats:
            _regex("variant_constant_patterns", pat, 2)
        cfg.variant_constant_patterns = pats
    if "variant_default_patterns" in raw:
        pats = _str_list("variant_default_patterns", raw["variant_default_patterns"])
        for pat in pats:
            _regex("variant_default_patterns", pat)
            if not {"key", "default"} <= set(re.compile(pat).groupindex):
                raise BcpInputError("config key 'variant_default_patterns': each regex needs named groups "
                                    "'key' and 'default'")
        cfg.variant_default_patterns = pats
    if "variant_mask_patterns" in raw:
        pats = _str_list("variant_mask_patterns", raw["variant_mask_patterns"])
        for pat in pats:
            _regex("variant_mask_patterns", pat)
        cfg.variant_mask_patterns = pats
    if "program_snapshots" in raw:
        paths = []
        for value in _str_list("program_snapshots", raw["program_snapshots"]):
            path = Path(value)
            if not path.is_absolute() and base_dir is not None:
                path = base_dir / path
            if not path.exists():
                raise BcpInputError(f"config key 'program_snapshots': not found: {path}")
            paths.append(str(path))
        cfg.program_snapshots = paths
    if "integration_patterns" in raw:
        value = raw["integration_patterns"]
        if not isinstance(value, dict) or not all(isinstance(k, str) and k for k in value):
            raise BcpInputError("config key 'integration_patterns' must map labels to regex strings")
        cfg.integration_patterns = {k: _regex("integration_patterns", v) for k, v in value.items()}
    if "kb" in raw and raw["kb"] is not None:
        cfg.kb = _kb_section(raw["kb"])
    check_agent_options(cfg)  # after kb parsing: the hook-only design requires a kb section
    return cfg


KB_KEYS = ("kb_id", "region", "graph", "python", "top_k", "timeout_s", "hops")


def _kb_section(value: object) -> dict[str, object]:
    """The ``kb`` section: ``kb_id``, ``region`` and ``graph`` (workspace-relative or absolute path to
    graph.json) are required; ``python`` (interpreter with boto3), ``top_k`` (1..10, default 5),
    ``timeout_s`` (1..25, default 12) and ``hops`` (1..3, default 2) are optional."""
    if not isinstance(value, dict):
        raise BcpInputError("config key 'kb' must be an object with kb_id, region and graph")
    for key in sorted(value):
        if key not in KB_KEYS:
            log.warning("unknown config key ignored: kb.%s", key)
    out: dict[str, object] = {}
    kb_id = value.get("kb_id")
    if not isinstance(kb_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,32}", kb_id):
        raise BcpInputError("config key 'kb.kb_id' must be a knowledge base id (letters and digits)")
    region = value.get("region")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d+", region):
        raise BcpInputError("config key 'kb.region' must be an AWS Region such as us-east-1")
    graph = value.get("graph")
    if not isinstance(graph, str) or not graph.strip():
        raise BcpInputError("config key 'kb.graph' must be a path to graph.json (relative to the workspace)")
    out.update(kb_id=kb_id, region=region, graph=graph.strip().replace("\\", "/") if not Path(graph).is_absolute()
               else graph.strip())
    if value.get("python") is not None:
        python = value["python"]
        if not isinstance(python, str) or not python.strip():
            raise BcpInputError("config key 'kb.python' must be a path to a python interpreter")
        out["python"] = python.strip()
    out["top_k"] = _int_range("kb.top_k", value.get("top_k", 5), 1, 10)
    out["hops"] = _int_range("kb.hops", value.get("hops", 2), 1, 3)
    timeout = value.get("timeout_s", 12)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 25:
        raise BcpInputError("config key 'kb.timeout_s' must be a number in 1..25")
    out["timeout_s"] = float(timeout)
    return out


def load_config(path: Path | None) -> BcpConfig:
    """Load ``path`` (or return defaults when None); bad files or values raise BcpInputError."""
    if path is None:
        return BcpConfig()
    path = Path(path)
    if not path.is_file():
        raise BcpInputError(f"config file not found: {path}")
    try:
        raw = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise BcpInputError(f"config file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BcpInputError(f"config file must hold a JSON object: {path}")
    return from_dict(raw, base_dir=path.parent)
