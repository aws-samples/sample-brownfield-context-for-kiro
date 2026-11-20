"""A small gitignore-subset matcher, used only to warn when a reference is ignored.

Supported (per https://kiro.dev/docs/kiroignore/ "standard gitignore syntax"): blank and ``#``
lines skipped; ``!`` negates; a trailing ``/`` matches directories only; a leading or inner
``/`` anchors the pattern to the ignore file's directory, otherwise it matches the basename
or any path suffix; ``*`` and ``?`` do not cross ``/``; ``**`` matches zero or more segments;
the last matching pattern wins, except that a file under an excluded directory stays excluded.
This is a warning heuristic, not a reimplementation of Kiro's matcher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bcp.textio import read_text


@dataclass(frozen=True)
class Rule:
    """One pattern line."""

    source: str
    lineno: int
    raw: str
    negate: bool
    dir_only: bool
    regex: re.Pattern[str]


def _translate(pattern: str) -> str:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("/.*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def parse_rules(text: str, source: str) -> list[Rule]:
    """Parse ignore-file text into rules."""
    rules: list[Rule] = []
    for lineno, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        raw = line.rstrip()
        if not raw or raw.startswith("#"):
            continue
        pat = raw
        negate = pat.startswith("!")
        if negate:
            pat = pat[1:]
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if not pat:
            continue
        anchored = "/" in pat
        pat = pat.lstrip("/")
        body = _translate(pat)
        regex = re.compile(("^" if anchored else "^(?:.*/)?") + body + "$")
        rules.append(Rule(source, lineno, raw, negate, dir_only, regex))
    return rules


@dataclass
class IgnoreFile:
    """An ignore file and the directory (relative to the root) its patterns apply to."""

    source: str
    base: str
    rules: list[Rule] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, base: str, label: str | None = None) -> "IgnoreFile":
        """Read an ignore file from disk."""
        name = label or str(path)
        return cls(name, base.strip("/"), parse_rules(read_text(path), name))

    def relative(self, rel: str) -> str | None:
        """``rel`` relative to this file's base, or None when outside it."""
        if not self.base:
            return rel
        if rel == self.base or rel.startswith(self.base + "/"):
            return rel[len(self.base) + 1:]
        return None


def _last_match(files: list[IgnoreFile], rel: str, is_dir: bool) -> Rule | None:
    decided: Rule | None = None
    for ign in files:
        sub = ign.relative(rel)
        if sub is None or sub == "":
            continue
        for rule in ign.rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(sub):
                decided = rule
    return decided


def ignored_by(files: list[IgnoreFile], rel: str, is_dir: bool = False) -> Rule | None:
    """The rule that ignores ``rel`` (forward-slash, root-relative), or None if not ignored."""
    parts = rel.strip("/").split("/")
    for depth in range(1, len(parts)):
        parent = "/".join(parts[:depth])
        rule = _last_match(files, parent, True)
        if rule is not None and not rule.negate:
            return rule  # a parent directory is excluded; cannot be re-included
    rule = _last_match(files, "/".join(parts), is_dir)
    if rule is not None and not rule.negate:
        return rule
    return None
