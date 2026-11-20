"""Validate a workspace's ``.kiro/`` steering, skills and agents (exit 1 on any ERROR)."""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bcp import frontmatter
from bcp.errors import BcpInputError
from bcp.ignorematch import IgnoreFile, ignored_by
from bcp.render import (
    ANCHORS_HEADER,
    ANCHORS_PREFIX,
    ERRATA_FILE,
    GENERATED_MARKER,
    INDEX_FILE,
    PROVENANCE_DERIVED,
    PROVENANCE_EXTRACTED,
    STEERING_DIR,
    WHAT_CHANGED_PREFIX,
)
from bcp.textio import read_text, safe_join, to_posix
from bcp.trust import TRUST_PREFIX

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - PyYAML is optional at runtime
    yaml = None

log = logging.getLogger(__name__)

# Steering inclusion modes, #[[file:]] / #[[folder:]] references and :N / :A-B line ranges:
# https://kiro.dev/docs/steering/
INCLUSIONS = ("always", "fileMatch", "manual", "auto")
# Skill name rules (lowercase letters, digits, hyphens, max 64, equals folder): https://kiro.dev/docs/skills/
SKILL_NAME_RE = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
FILE_REF_RE = re.compile(r"#\[\[file:([^\]]+)\]\]")
FOLDER_REF_RE = re.compile(r"#\[\[folder:([^\]]+)\]\]")
RANGE_SUFFIX_RE = re.compile(r"^(.*?)(?::(\d+)(?:-(\d+))?)?$")
DERIVED_FILES = ("impact-map.md", "variant-map.md", "glossary.md")
# Configurable bcp heuristic, not a Kiro limit (Kiro publishes no steering size:
# https://kiro.dev/docs/steering/, https://kiro.dev/docs/cli/chat/context/). As bytes >= characters, a byte
# total at or under this never exceeds Kiro's documented 100,000-character file:// cap below.
AGENT_BUDGET = 81920
RULE_HEADING_RE = re.compile(r"^## BR-[\w.-]+:")
RANGE_EVIDENCE_RE = re.compile(r":L\d+-L\d+")
ERRATA_WARN_BYTES = 16000  # configurable bcp heuristic, not a Kiro limit
# Kiro-documented caps on custom-agent file:// resources (CLI 3.1.0+, IDE 1.4.86+):
# https://kiro.dev/docs/custom-agents/configuration-reference/
KIRO_AGENT_MAX_FILES = 32
KIRO_AGENT_MAX_CHARS = 100_000
KIRO_AGENT_MAX_FILE_CHARS = 50_000
KIRO_AGENT_MAX_FILE_BYTES = 1024 * 1024
KIRO_AGENT_DOC = "https://kiro.dev/docs/custom-agents/configuration-reference/"
KIRO_STEERING_DOC = "https://kiro.dev/docs/steering/"
# bcp heuristics, not Kiro limits: an always-included file with more table rows than this, or whose
# #[[file:]] / #[[folder:]] references expand past this many bytes, looks like reference material.
REFERENCE_TABLE_ROWS = 20
REFERENCE_EXPANSION_BYTES = 4000
TRUST_LINE_WINDOW = 3
# Wording that presents the extraction as confirmed truth; generated agent prompts must not use it.
BANNED_PROMPT_PHRASES = ("source of truth", "signed off before it was published", "verified are confirmed")


@dataclass
class Finding:
    """One validation finding."""

    level: str  # ERROR | WARN | INFO
    path: str
    message: str

    def line(self) -> str:
        """Printable line."""
        return f"{self.level} {self.path}: {self.message}"


@dataclass
class Report:
    """All findings plus size measurements."""

    findings: list[Finding] = field(default_factory=list)
    always_bytes: int = 0
    sizes: dict[str, int] = field(default_factory=dict)
    agent_bytes: dict[str, int] = field(default_factory=dict)
    checked: int = 0

    def add(self, level: str, path: str, message: str) -> None:
        """Record a finding."""
        self.findings.append(Finding(level, path, message))

    def count(self, level: str) -> int:
        """Number of findings at ``level``."""
        return sum(1 for f in self.findings if f.level == level)

    @property
    def exit_code(self) -> int:
        """1 when any ERROR was found."""
        return 1 if self.count("ERROR") else 0

    def summary(self) -> dict[str, Any]:
        """JSON-friendly summary."""
        return {
            "checked_files": self.checked,
            "errors": self.count("ERROR"),
            "warnings": self.count("WARN"),
            "info": self.count("INFO"),
            "always_bytes": self.always_bytes,
            "agent_bytes": self.agent_bytes,
            "findings": [f.__dict__ for f in self.findings],
        }


@dataclass
class Reference:
    """One ``#[[file:...]]`` or ``#[[folder:...]]`` reference."""

    kind: str
    path: str
    start: int | None = None
    end: int | None = None


def references(text: str) -> list[Reference]:
    """All file and folder references in ``text``."""
    refs = []
    for raw in FILE_REF_RE.findall(text):
        match = RANGE_SUFFIX_RE.match(raw.strip())
        assert match is not None
        start = int(match.group(2)) if match.group(2) else None
        end = int(match.group(3)) if match.group(3) else start
        refs.append(Reference("file", match.group(1), start, end))
    refs += [Reference("folder", raw.strip()) for raw in FOLDER_REF_RE.findall(text)]
    return refs


def parse_front(text: str) -> dict[str, Any]:
    """Front matter via PyYAML when importable, else the strict parser (raises on bad syntax)."""
    block, _body = frontmatter.split(text)
    if yaml is not None:
        try:
            data = yaml.safe_load("\n".join(block)) or {}
        except yaml.YAMLError as exc:
            raise frontmatter.FrontMatterError(f"YAML error: {exc}") from exc
        if not isinstance(data, dict):
            raise frontmatter.FrontMatterError("front matter is not a mapping")
        return data
    return frontmatter.parse(text)[0]


class Validator:
    """Runs every check against one target workspace."""

    # budget, domain_budget and agent_budget are configurable bcp heuristics (WARN only), not Kiro limits.
    def __init__(self, target: Path, budget: int = 16000, domain_budget: int = 50000,
                 agent_budget: int = AGENT_BUDGET) -> None:
        self.target = Path(target)
        self.budget = budget
        self.domain_budget = domain_budget
        self.agent_budget = agent_budget
        self.report = Report()
        self.generated = self._generated_paths()
        self._ignore_cache: dict[str, list[IgnoreFile]] = {}

    def _generated_paths(self) -> set[str]:
        rec = self.target / ".kiro/context/generation-record.json"
        try:
            data = json.loads(read_text(rec)) if rec.is_file() else {}
        except json.JSONDecodeError:
            return set()
        return set((data.get("files") or {}).keys()) if isinstance(data, dict) else set()

    def rel(self, path: Path) -> str:
        """Target-relative forward-slash path."""
        return to_posix(os.path.relpath(path, self.target))

    def err(self, rel: str, message: str, owned: bool) -> None:
        """ERROR for generated files, WARN for files the generator does not own."""
        self.report.add("ERROR" if owned else "WARN", rel, message + ("" if owned else " (not generated by bcp)"))

    # ---------------------------------------------------------------- references

    def resolve_ref(self, src_rel: str, ref: Reference, owned: bool) -> int:
        """Check one reference; return its byte size (0 when unresolved)."""
        try:
            path = safe_join(self.target, ref.path)
        except BcpInputError:
            self.err(src_rel, f"reference leaves the workspace root: {ref.path}", owned)
            return 0
        if ref.kind == "folder":
            if not path.is_dir():
                self.err(src_rel, f"#[[folder:{ref.path}]] does not resolve to a directory", owned)
            return 0
        if not path.is_file():
            self.err(src_rel, f"#[[file:{ref.path}]] does not resolve to a file", owned)
            return 0
        if ref.start is not None:
            count = len(read_text(path).splitlines())
            if ref.start < 1 or (ref.end or ref.start) < ref.start or (ref.end or ref.start) > count:
                self.err(src_rel, f"line range {ref.start}-{ref.end} outside {ref.path} ({count} lines)", owned)
        self.check_ignored(src_rel, ref)
        return path.stat().st_size

    def _ignore_files(self, directory: str, name: str) -> list[IgnoreFile]:
        key = f"{name}:{directory}"
        if key in self._ignore_cache:
            return self._ignore_cache[key]
        files: list[IgnoreFile] = []
        parts = [p for p in directory.split("/") if p]
        for depth in range(0, len(parts) + 1):
            base = "/".join(parts[:depth])
            candidate = self.target / base / name if base else self.target / name
            if candidate.is_file():
                files.append(IgnoreFile.load(candidate, base, f"{base + '/' if base else ''}{name}"))
        self._ignore_cache[key] = files
        return files

    def _global_ignores(self) -> list[IgnoreFile]:
        if "global" in self._ignore_cache:
            return self._ignore_cache["global"]
        files = []
        global_file = Path.home() / ".kiro" / "settings" / "kiroignore"
        if global_file.is_file():
            files.append(IgnoreFile.load(global_file, "", "~/.kiro/settings/kiroignore"))
        excludes = git_excludes_file(self.target)
        if excludes is not None and excludes.is_file():
            files.append(IgnoreFile.load(excludes, "", f"core.excludesfile ({excludes.name})"))
        self._ignore_cache["global"] = files
        return files

    def check_ignored(self, src_rel: str, ref: Reference) -> None:
        """WARN for .kiroignore hits; INFO for .gitignore, the global kiroignore and core.excludesfile."""
        rel = to_posix(ref.path).strip("/")
        directory = rel.rsplit("/", 1)[0] if "/" in rel else ""
        is_dir = ref.kind == "folder"
        rule = ignored_by(self._ignore_files(directory, ".kiroignore"), rel, is_dir)
        if rule is not None:
            self.report.add("WARN", src_rel, f"-> {rel} ignored by {rule.source}:{rule.lineno} \"{rule.raw}\"")
        rule = ignored_by(self._ignore_files(directory, ".gitignore"), rel, is_dir)
        if rule is not None:
            self.report.add("INFO", src_rel, f"-> {rel} ignored by {rule.source}:{rule.lineno} \"{rule.raw}\" "
                                             "(applies only if kiroAgent.agentIgnoreFiles lists .gitignore)")
        rule = ignored_by(self._global_ignores(), rel, is_dir)
        if rule is not None:
            self.report.add("INFO", src_rel, f"-> {rel} ignored by {rule.source}:{rule.lineno} \"{rule.raw}\"")

    # ---------------------------------------------------------------- steering

    def steering_inclusion(self, path: Path) -> str | None:
        """Inclusion mode of a steering file (None when unparseable)."""
        try:
            data = parse_front(read_text(path))
        except frontmatter.FrontMatterError:
            return None
        return str(data.get("inclusion") or "always")

    def check_steering(self) -> None:
        """Every ``.kiro/steering/*.md`` file."""
        folder = self.target / ".kiro" / "steering"
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            self.report.checked += 1
            rel = self.rel(path)
            raw = path.read_bytes()
            text = read_text(path)
            owned = GENERATED_MARKER in text
            if raw.startswith(b"\xef\xbb\xbf"):
                self.err(rel, "file starts with a BOM; front matter must be the first content", owned)
            first = raw.lstrip(b"\xef\xbb\xbf").split(b"\n", 1)[0].rstrip(b"\r")
            if first != b"---":
                self.err(rel, "line 1 is not '---' (front matter must be the first content)", owned)
                continue
            try:
                data = parse_front(text)
            except frontmatter.FrontMatterError as exc:
                self.err(rel, f"front matter does not parse: {exc}", owned)
                continue
            inclusion = data.get("inclusion")
            if inclusion is None:
                self.report.add("INFO", rel, "no inclusion key; Kiro treats it as always, the documented default "
                                             "(https://kiro.dev/docs/steering/)")
                inclusion = "always"
            if inclusion not in INCLUSIONS:
                self.err(rel, f"inclusion {inclusion!r} is not one of {', '.join(INCLUSIONS)}", owned)
                continue
            if inclusion == "auto":
                for key in ("name", "description"):
                    if not isinstance(data.get(key), str) or not data[key].strip():
                        self.err(rel, f"auto inclusion needs a non-empty string {key}", owned)
            if inclusion == "fileMatch":
                pat = data.get("fileMatchPattern")
                ok = (isinstance(pat, str) and pat.strip()) or (
                    isinstance(pat, list) and pat and all(isinstance(p, str) and p.strip() for p in pat)
                )
                if not ok:
                    self.err(rel, "fileMatch needs fileMatchPattern (string or list of strings)", owned)
            expansion = sum(self.resolve_ref(rel, r, owned) for r in references(text))
            size = len(raw) + expansion
            self.report.sizes[rel] = size
            if inclusion == "always":
                self.report.always_bytes += size
                self.check_reference_material(rel, text, expansion)
            elif inclusion in ("auto", "fileMatch") and size > self.domain_budget:
                self.report.add("WARN", rel, f"expanded size {size} bytes is over domain_budget_bytes "
                                             f"{self.domain_budget} (bcp heuristic, not a Kiro limit)")
            if owned and path.name.startswith("bre-domain-") and "/detailed-domain-bre/" in text:
                if PROVENANCE_EXTRACTED not in text:
                    self.report.add("WARN", rel, "provenance notice missing before the extraction references")
        # AGENTS.md is always included and has no inclusion modes (https://kiro.dev/docs/steering/).
        agents_md = self.target / "AGENTS.md"
        if agents_md.is_file():
            self.report.sizes["AGENTS.md"] = agents_md.stat().st_size
            self.report.always_bytes += agents_md.stat().st_size
        if self.report.always_bytes > self.budget:
            self.report.add("WARN", ".kiro/steering",
                            f"always-included steering expands to {self.report.always_bytes} bytes, over the "
                            f"advisory always_budget_bytes {self.budget} (a bcp heuristic, not a Kiro limit); move "
                            f"reference material to fileMatch, auto or manual steering or a skill ({KIRO_STEERING_DOC})")

    def check_reference_material(self, rel: str, text: str, expansion: int) -> None:
        """WARN when an always-included file looks like reference material (bcp heuristic thresholds)."""
        rows = sum(1 for line in text.splitlines() if line.lstrip().startswith("|"))
        if rows <= REFERENCE_TABLE_ROWS and expansion <= REFERENCE_EXPANSION_BYTES:
            return
        why = (f"{rows} table rows" if rows > REFERENCE_TABLE_ROWS
               else f"references expanding to {expansion} bytes")
        self.report.add("WARN", rel, f"always-included file looks like reference material ({why}; bcp heuristic); "
                                     "Kiro recommends auto inclusion for detailed reference material, fileMatch for "
                                     f"file-specific guidance, manual for occasional use, or a skill ({KIRO_STEERING_DOC})")

    def check_context(self) -> None:
        """Provenance notices on generated derived context files."""
        ctx = self.target / ".kiro" / "context"
        if not ctx.is_dir():
            return
        candidates = [ctx / name for name in DERIVED_FILES] + sorted(ctx.glob("domains/*/summary.md"))
        for path in candidates:
            if path.is_file():
                text = read_text(path)
                if GENERATED_MARKER in text and PROVENANCE_DERIVED not in text:
                    self.report.add("WARN", self.rel(path), "provenance notice missing")
        for path in sorted(ctx.rglob("*.md")):
            rel = self.rel(path)
            if rel.startswith(".kiro/context/bre/"):
                continue  # copied extraction files are not generated context
            head = read_text(path).split("\n", TRUST_LINE_WINDOW + 1)[:TRUST_LINE_WINDOW]
            if not any(GENERATED_MARKER in line for line in head):
                continue
            if not any(line.startswith(TRUST_PREFIX) for line in head):
                self.report.add("ERROR", rel, f"generated context file has no '{TRUST_PREFIX.strip()}' line in "
                                              f"its first {TRUST_LINE_WINDOW} lines")
        errata = ctx / ERRATA_FILE
        if errata.is_file() and errata.stat().st_size > ERRATA_WARN_BYTES:
            self.report.add("WARN", self.rel(errata), f"{errata.stat().st_size} bytes is over {ERRATA_WARN_BYTES} "
                                                      "(bcp heuristic, not a Kiro limit); every agent loads it first")

    # ---------------------------------------------------------------- skills and agents

    def check_skills(self) -> None:
        """``.kiro/skills/*/SKILL.md`` name and description rules."""
        for path in sorted((self.target / ".kiro" / "skills").glob("*/SKILL.md")):
            self.report.checked += 1
            rel = self.rel(path)
            text = read_text(path)
            owned = GENERATED_MARKER in text
            for level, message in check_skill_text(text, path.parent.name):
                if level == "ERROR":
                    self.err(rel, message, owned)
                else:
                    self.report.add(level, rel, message)

    def check_agents(self) -> None:
        """``.kiro/agents/*.json``: parse, name, tools, resources resolve, steering resources are always
        files, Kiro's documented file:// caps hold (32 files, 100,000 characters in total, 50,000 per file,
        no file over 1 MiB; https://kiro.dev/docs/custom-agents/configuration-reference/), and startup bytes
        (file:// + skill:// + prompt) are compared with the advisory agent budget. That budget is a bcp
        heuristic: it counts the full SKILL.md, although Kiro loads only skill metadata at startup."""
        for path in sorted((self.target / ".kiro" / "agents").glob("*.json")):
            self.report.checked += 1
            rel = self.rel(path)
            owned = rel in self.generated
            try:
                data = json.loads(read_text(path))
            except json.JSONDecodeError as exc:
                self.err(rel, f"invalid JSON: {exc}", owned)
                continue
            if not isinstance(data, dict) or not data.get("name"):
                self.err(rel, "agent needs a name", owned)
                continue
            tools = data.get("tools")
            if not isinstance(tools, list) or not tools:
                self.report.add("WARN", rel, "no tools list; the docs do not define what an omitted tools field means")
            self.check_agent_access(rel, data, owned)
            prompt_lower = str(data.get("prompt") or "").lower()
            banned = [p for p in BANNED_PROMPT_PHRASES if p in prompt_lower]
            if banned:
                self.err(rel, "prompt presents the context as confirmed truth ("
                         + ", ".join(repr(b) for b in banned) + "); the context is a map, the code is the truth",
                         owned)
            total = len(str(data.get("prompt") or "").encode("utf-8"))
            file_chars: dict[str, int] = {}  # unique file:// matches in load order -> characters
            for res in data.get("resources") or []:
                if not isinstance(res, str):
                    continue
                if res.startswith("skill://"):
                    pattern = res[len("skill://"):]
                    if pattern.startswith("~") or os.path.isabs(pattern):
                        continue
                    found = [Path(m) for m in glob.glob(str(self.target / pattern), recursive=True)]
                    if not any(m.is_file() for m in found):
                        self.err(rel, f"skill:// resource {res} matches no file", owned)
                    total += sum(m.stat().st_size for m in found if m.is_file())
                    continue
                if not res.startswith("file://"):
                    continue
                pattern = res[len("file://"):]
                matches = sorted(glob.glob(str(self.target / pattern), recursive=True))
                if not any(Path(m).is_file() for m in matches):
                    self.err(rel, f"file:// resource {res} matches no file", owned)
                for match in matches:
                    mpath = Path(match)
                    if not mpath.is_file():
                        continue
                    mrel = self.rel(mpath)
                    text = read_text(mpath)
                    file_chars.setdefault(mrel, len(text))
                    total += mpath.stat().st_size
                    total += sum(
                        safe_size(self.target, r.path) for r in references(text) if r.kind == "file"
                    )
                    if mrel.startswith(".kiro/steering/") and mrel.endswith(".md"):
                        inclusion = self.steering_inclusion(mpath)
                        # The generated context index is auto inclusion but deliberately pinned into every
                        # agent via file:// (bcp agents.pinned_steering): force-loading it is the intent, so
                        # it is exempt from the "list only always-included files" rule.
                        pinned_index = inclusion == "auto" and mrel == f"{STEERING_DIR}/{INDEX_FILE}"
                        if inclusion != "always" and not pinned_index:
                            self.err(rel, f"file:// resource {res} loads {mrel} (inclusion {inclusion}) at "
                                          "startup, so it loses its inclusion mode (Kiro loads every file:// "
                                          f"match at startup: {KIRO_AGENT_DOC}); list only always-included "
                                          "files", owned)
            self.check_file_caps(rel, file_chars, owned)
            self.report.agent_bytes[rel] = total
            if total > self.agent_budget:
                self.report.add("WARN", rel, f"file:// resources, skills and prompt expand to {total} startup bytes, "
                                             f"over the advisory agent_budget_bytes {self.agent_budget} (bcp "
                                             "heuristic, not a Kiro limit; counts the prompt and the full SKILL.md, "
                                             "which Kiro loads only as metadata at startup)")

    def check_file_caps(self, rel: str, file_chars: dict[str, int], owned: bool) -> None:
        """Kiro's documented file:// caps (https://kiro.dev/docs/custom-agents/configuration-reference/)."""
        if len(file_chars) > KIRO_AGENT_MAX_FILES:
            self.err(rel, f"{len(file_chars)} file:// files; Kiro loads at most {KIRO_AGENT_MAX_FILES} file:// "
                          f"files and later files are not loaded ({KIRO_AGENT_DOC})", owned)
        chars = sum(file_chars.values())
        if chars > KIRO_AGENT_MAX_CHARS:
            self.err(rel, f"file:// files total {chars} characters; Kiro loads at most {KIRO_AGENT_MAX_CHARS} "
                          f"characters and later files are not loaded ({KIRO_AGENT_DOC})", owned)
        for mrel, count in file_chars.items():
            if (self.target / mrel).stat().st_size > KIRO_AGENT_MAX_FILE_BYTES:
                self.err(rel, f"file:// file {mrel} is over 1 MiB; Kiro skips it ({KIRO_AGENT_DOC})", owned)
            elif count > KIRO_AGENT_MAX_FILE_CHARS:
                self.err(rel, f"file:// file {mrel} has {count} characters, over {KIRO_AGENT_MAX_FILE_CHARS}; "
                              f"Kiro truncates it ({KIRO_AGENT_DOC})", owned)

    def check_agent_access(self, rel: str, data: dict[str, Any], owned: bool) -> None:
        """Shell must stay denied (no shell or wildcard tool, an explicit shell deny rule), and every
        ``subagent/<name>`` tool must name an agent defined in ``.kiro/agents``."""
        tools = [t for t in data.get("tools") or [] if isinstance(t, str)]
        risky = [t for t in tools if t in ("*", "shell", "@builtin") or t.startswith("shell")]
        if risky:
            self.err(rel, f"tools {risky} grant shell access; shell must stay disabled", owned)
        rules = ((data.get("permissions") or {}).get("rules") or []) if isinstance(data.get("permissions"), dict) else []
        if {"capability": "shell", "effect": "deny"} not in rules:
            self.err(rel, "permissions.rules has no {capability: shell, effect: deny} rule", owned)
        defined = {p.stem for p in (self.target / ".kiro" / "agents").glob("*.json")}
        for tool in tools:
            if tool.startswith("subagent/") and tool[len("subagent/"):] not in defined:
                self.err(rel, f"tool {tool} names no agent in .kiro/agents", owned)

    def check_rules(self) -> None:
        """Format and size of generated ``reviewed-rules.md``: every rule heading carries a ``> Review:``
        line. Files generated with the narrow-capped design (their header announces anchors) are also
        checked for an ``> Anchors`` line on every VERIFIED rule with range evidence (so it can be cited
        without opening code) and for a size over ``domain_budget`` (too big for one fast-path read)."""
        for path in sorted((self.target / ".kiro" / "context" / "domains").glob("*/reviewed-rules.md")):
            text = read_text(path)
            if GENERATED_MARKER not in text:
                continue
            self.report.checked += 1
            rel = self.rel(path)
            anchored_design = ANCHORS_HEADER in text
            lines = text.split("\n")
            missing_review, missing_anchor, missing_change = [], [], []
            for i, line in enumerate(lines):
                if not RULE_HEADING_RE.match(line):
                    continue
                status = lines[i + 1] if i + 1 < len(lines) else ""
                if not status.startswith("> Review: "):
                    missing_review.append(line[3:].split(":", 1)[0])
                    continue
                if status.startswith("> Review: NEEDS EDIT") and not (
                        i + 2 < len(lines) and lines[i + 2].startswith(WHAT_CHANGED_PREFIX)):
                    missing_change.append(line[3:].split(":", 1)[0])
                verified = status.startswith("> Review: VERIFIED")
                ranged = RANGE_EVIDENCE_RE.search(status) is not None
                anchored = i + 2 < len(lines) and lines[i + 2].startswith(ANCHORS_PREFIX)
                if verified and ranged and not anchored:
                    missing_anchor.append(line[3:].split(":", 1)[0])
            if missing_review:
                self.report.add("ERROR", rel, f"rule heading without a '> Review:' line: {', '.join(missing_review)}")
            if missing_change:
                self.report.add("ERROR", rel, "NEEDS EDIT rule without a '> What changed:' line after its review "
                                              f"line: {', '.join(missing_change)}")
            if not anchored_design:
                continue
            if missing_anchor:
                self.report.add("WARN", rel, "VERIFIED rule with range evidence but no '> Anchors' line (regenerate "
                                             f"with this bcp version): {', '.join(missing_anchor)}")
            size = path.stat().st_size
            self.report.sizes[rel] = size
            if size > self.domain_budget:
                self.report.add("WARN", rel, f"{size} bytes is over domain_budget_bytes {self.domain_budget}; "
                                             "too big to read in one call on the narrow fast path")

    def run(self) -> Report:
        """Run all checks."""
        if not (self.target / ".kiro").is_dir():
            raise BcpInputError(f"no .kiro folder under {self.target}")
        self.check_steering()
        self.check_context()
        self.check_rules()
        self.check_skills()
        self.check_agents()
        return self.report


def safe_size(root: Path, rel: str) -> int:
    """Byte size of ``rel`` under root, 0 when unsafe or missing."""
    match = RANGE_SUFFIX_RE.match(rel.strip())
    try:
        path = safe_join(root, match.group(1) if match else rel)
    except BcpInputError:
        return 0
    return path.stat().st_size if path.is_file() else 0


def check_skill_text(text: str, folder: str) -> list[tuple[str, str]]:
    """Agent Skills rules: name equals folder and matches the spec regex; description 1–1024 chars."""
    problems: list[tuple[str, str]] = []
    try:
        data = parse_front(text)
    except frontmatter.FrontMatterError as exc:
        return [("ERROR", f"SKILL.md front matter does not parse: {exc}")]
    name = data.get("name")
    if not isinstance(name, str) or name != folder:
        problems.append(("ERROR", f"skill name {name!r} must equal the folder name {folder!r}"))
    if not isinstance(name, str) or not SKILL_NAME_RE.match(name):
        problems.append(("ERROR", f"skill name {name!r} is not a valid Agent Skills name"))
    desc = data.get("description")
    if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 1024:
        problems.append(("ERROR", "skill description must be 1-1024 characters"))
    return problems


def git_excludes_file(target: Path) -> Path | None:
    """Read-only ``git config --get core.excludesfile``; None if git is missing or not a repo."""
    try:
        inside = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        proc = subprocess.run(
            ["git", "-C", str(target), "config", "--get", "core.excludesfile"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        return None
    return Path(os.path.expanduser(value))


def validate(target: Path, budget: int | None = None, domain_budget: int = 50000,
             agent_budget: int | None = None) -> Report:
    """Validate ``<target>/.kiro``."""
    return Validator(Path(target), budget or 16000, domain_budget, agent_budget or AGENT_BUDGET).run()
