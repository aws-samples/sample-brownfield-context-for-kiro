"""Java plugin: brace-matched method ranges, multi-line annotations, implemented types, imports.

All structure is read from a masked copy of the source in which comments, string, char and text-block
contents are blanked, so braces and parentheses inside them never count.
"""

from __future__ import annotations

import re

from bcp.lang import Annotation
from bcp.sourceindex import declaration_name, type_name

IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;")
ANNOTATION_NAME_RE = re.compile(r"@\s*([A-Za-z_][\w.]*)")
_NOT_DECL_START = {"return", "new", "throw", "else", "case", "yield", "assert"}


def mask(lines: list[str]) -> list[str]:
    """Copy of ``lines`` with comments and literal contents replaced by spaces (quotes kept)."""
    out: list[str] = []
    in_block = in_text = False
    for line in lines:
        buf: list[str] = []
        i, n = 0, len(line)
        while i < n:
            if in_block:
                if line.startswith("*/", i):
                    in_block = False
                    buf.append("  ")
                    i += 2
                else:
                    buf.append(" ")
                    i += 1
                continue
            if in_text:
                if line.startswith('"""', i):
                    in_text = False
                    buf.append('"""')
                    i += 3
                else:
                    buf.append(" ")
                    i += 1
                continue
            if line.startswith("//", i):
                buf.append(" " * (n - i))
                break
            if line.startswith("/*", i):
                in_block = True
                buf.append("  ")
                i += 2
                continue
            if line.startswith('"""', i):
                in_text = True
                buf.append('"""')
                i += 3
                continue
            char = line[i]
            if char in "\"'":
                buf.append(char)
                i += 1
                while i < n and line[i] != char:
                    if line[i] == "\\":
                        step = min(2, n - i)
                        buf.append(" " * step)
                        i += step
                        continue
                    buf.append(" ")
                    i += 1
                if i < n:
                    buf.append(char)
                    i += 1
                continue
            buf.append(char)
            i += 1
        out.append("".join(buf))
    return out


class JavaSupport:
    """Java (and close relatives that share its syntax)."""

    name = "java"

    def method_decls(self, lines: list[str], method: str) -> list[int]:
        """Declaration lines of ``method``: the shared declaration heuristics first, then a looser
        ``Type name(`` match for generic return types."""
        masked = mask(lines)
        hits = [n for n, line in enumerate(masked, start=1) if declaration_name(line) == method]
        if hits:
            return hits
        loose = re.compile(
            rf"^\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:[\w<>\[\],.?]+\s+)+{re.escape(method)}\s*\("
        )
        out = []
        for n, line in enumerate(masked, start=1):
            if loose.match(line) and line.split()[0] not in _NOT_DECL_START:
                out.append(n)
        return out

    def method_range(self, lines: list[str], decl_line: int) -> tuple[int, int]:
        """From the declaration line to the matching closing brace (or the ``;`` of an abstract
        method). An unclosed body runs to the end of the file."""
        masked = mask(lines)
        paren = 0
        depth = 0
        opened = False
        for idx in range(decl_line - 1, len(masked)):
            for char in masked[idx]:
                if not opened:
                    if char == "(":
                        paren += 1
                    elif char == ")":
                        paren -= 1
                    elif char == ";" and paren <= 0:
                        return decl_line, idx + 1
                    elif char == "{" and paren <= 0:
                        opened = True
                        depth = 1
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        return decl_line, idx + 1
        return decl_line, len(lines)

    def _parse_block(self, lines: list[str], masked: list[str], start: int, end: int) -> list[Annotation]:
        text = " ".join(" ".join(lines[i].strip() for i in range(start, end + 1)).split())
        names = ANNOTATION_NAME_RE.findall(" ".join(masked[i] for i in range(start, end + 1)))
        return [Annotation(start + 1, end + 1, text, name.rsplit(".", 1)[-1]) for name in names]

    def _inline(self, lines: list[str], masked: list[str], idx: int) -> list[Annotation]:
        """Annotations written on the declaration line itself, before the declaration."""
        line = masked[idx]
        pos, out = 0, []
        while True:
            while pos < len(line) and line[pos].isspace():
                pos += 1
            if pos >= len(line) or line[pos] != "@":
                break
            match = re.match(r"@\s*([A-Za-z_][\w.]*)", line[pos:])
            if not match or match.group(1) == "interface":
                break
            begin = pos
            pos += match.end()
            rest = pos
            while rest < len(line) and line[rest].isspace():
                rest += 1
            if rest < len(line) and line[rest] == "(":
                depth = 0
                for j in range(rest, len(line)):
                    depth += {"(": 1, ")": -1}.get(line[j], 0)
                    if depth == 0:
                        pos = j + 1
                        break
                else:
                    pos = len(line)
            out.append(Annotation(idx + 1, idx + 1, " ".join(lines[idx][begin:pos].split()),
                                  match.group(1).rsplit(".", 1)[-1]))
        return out

    def annotations_before(self, lines: list[str], decl_line: int) -> list[Annotation]:
        """Annotations above the declaration (blank lines, Javadoc and line comments skipped; an
        annotation's arguments may span lines) plus those inline before it, top to bottom."""
        masked = mask(lines)
        found: list[Annotation] = []
        i = decl_line - 2
        depth = 0
        end: int | None = None
        while i >= 0:
            stripped = masked[i].strip()
            if not stripped and depth == 0:
                i -= 1
                continue
            if end is None:
                end = i
            depth += masked[i].count(")") - masked[i].count("(")
            if depth < 0:
                break
            if depth == 0:
                if not stripped.startswith("@"):
                    break
                found = self._parse_block(lines, masked, i, end) + found
                end = None
            i -= 1
        if 0 < decl_line <= len(lines):
            found += self._inline(lines, masked, decl_line - 1)
        return found

    def _type_line(self, masked: list[str]) -> int | None:
        for idx, line in enumerate(masked):
            if type_name(line):
                return idx
        return None

    def class_annotations(self, lines: list[str]) -> list[Annotation]:
        """Annotations on the first type declared in the file."""
        masked = mask(lines)
        idx = self._type_line(masked)
        return [] if idx is None else self.annotations_before(lines, idx + 1)

    def implemented_types(self, lines: list[str]) -> list[str]:
        """``extends`` and ``implements`` names of the first type, read across lines, generics removed."""
        masked = mask(lines)
        idx = self._type_line(masked)
        if idx is None:
            return []
        parts = []
        for line in masked[idx:idx + 15]:
            parts.append(line.split("{", 1)[0])
            if "{" in line:
                break
        head = " ".join(" ".join(parts).split())
        while re.search(r"<[^<>]*>", head):
            head = re.sub(r"<[^<>]*>", "", head)
        names: list[str] = []
        for keyword in ("extends", "implements"):
            match = re.search(rf"\b{keyword}\s+(.+?)(?=\bimplements\b|\bpermits\b|$)", head)
            if match:
                for item in match.group(1).split(","):
                    item = item.strip()
                    if item:
                        names.append(item.split()[0].rsplit(".", 1)[-1])
        return [n for n in names if n and n[0].isupper()]

    def imports(self, lines: list[str]) -> list[str]:
        """Imported names (static imports included)."""
        return [m.group(2) for line in lines if (m := IMPORT_RE.match(line))]


JAVA = JavaSupport()
