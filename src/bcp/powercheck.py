"""Structure check of a Kiro Power package in the Agent Plugins format.

Rules come from https://kiro.dev/docs/powers/create/ (required plugin.json fields, skills/<name>/SKILL.md)
and https://agent-plugins.org/schemas/1.0.0/plugin.schema.json (allowed keys, name pattern).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bcp.textio import read_text
from bcp.validate import check_skill_text

SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
ALLOWED_KEYS = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license",
                "keywords", "extensions"}
REQUIRED_KEYS = ("$schema", "name", "version", "description", "author", "keywords")
AUTHOR_KEYS = {"name", "email", "url"}
NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


@dataclass
class PowerReport:
    """Errors and the checked structure."""

    errors: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no errors were found."""
        return not self.errors

    def summary(self) -> dict[str, Any]:
        """JSON-friendly summary."""
        return {"ok": self.ok, "errors": self.errors, "skills": self.skills}


def check_power(package: Path) -> PowerReport:
    """Check ``plugin.json`` and every ``skills/<name>/SKILL.md`` in ``package``."""
    package = Path(package)
    report = PowerReport()
    manifest = package / "plugin.json"
    if not manifest.is_file():
        report.errors.append("plugin.json not found at the package root")
        return report
    try:
        data = json.loads(read_text(manifest))
    except json.JSONDecodeError as exc:
        report.errors.append(f"plugin.json is not valid JSON: {exc}")
        return report
    if not isinstance(data, dict):
        report.errors.append("plugin.json must be an object")
        return report
    for key in sorted(set(data) - ALLOWED_KEYS):
        report.errors.append(f"plugin.json: key {key!r} is not allowed by the Agent Plugins schema")
    for key in REQUIRED_KEYS:
        if key not in data or data[key] in (None, "", []):
            report.errors.append(f"plugin.json: required key {key!r} missing")
    if data.get("$schema") not in (None, SCHEMA_URL):
        report.errors.append(f"plugin.json: $schema must be {SCHEMA_URL}")
    name = data.get("name")
    if isinstance(name, str) and (len(name) > 64 or not NAME_RE.match(name)):
        report.errors.append(f"plugin.json: name {name!r} does not match the schema pattern (max 64)")
    elif name is not None and not isinstance(name, str):
        report.errors.append("plugin.json: name must be a string")
    author = data.get("author")
    if author is not None:
        if not isinstance(author, dict) or not isinstance(author.get("name"), str) or not author["name"].strip():
            report.errors.append("plugin.json: author must be an object with a name")
        elif set(author) - AUTHOR_KEYS:
            report.errors.append(f"plugin.json: author keys must be among {sorted(AUTHOR_KEYS)}")
    keywords = data.get("keywords")
    if keywords is not None and (not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords)):
        report.errors.append("plugin.json: keywords must be a list of strings")
    skills_dir = package / "skills"
    folders = sorted(p for p in skills_dir.iterdir() if p.is_dir()) if skills_dir.is_dir() else []
    if not folders:
        report.errors.append("no skills/<name>/ folders found")
    for folder in folders:
        skill = folder / "SKILL.md"
        if not skill.is_file():
            report.errors.append(f"skills/{folder.name}: SKILL.md missing")
            continue
        report.skills.append(folder.name)
        for _level, message in check_skill_text(read_text(skill), folder.name):
            report.errors.append(f"skills/{folder.name}/SKILL.md: {message}")
    return report
