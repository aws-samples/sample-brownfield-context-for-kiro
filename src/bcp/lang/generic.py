"""Generic language plugin: the declaration line only, no annotations."""

from __future__ import annotations

import re

from bcp.lang import Annotation
from bcp.sourceindex import declaration_name


class GenericSupport:
    """Fallback for languages without a plugin."""

    name = "generic"

    def method_decls(self, lines: list[str], method: str) -> list[int]:
        """Lines whose declaration name is ``method``; else the first ``method(`` line."""
        hits = [n for n, line in enumerate(lines, start=1) if declaration_name(line) == method]
        if hits:
            return hits
        regex = re.compile(rf"(?<![.\w]){re.escape(method)}\s*\(")
        return next(([n] for n, line in enumerate(lines, start=1) if regex.search(line)), [])

    def method_range(self, lines: list[str], decl_line: int) -> tuple[int, int]:
        """The declaration line only."""
        return decl_line, decl_line

    def annotations_before(self, lines: list[str], decl_line: int) -> list[Annotation]:
        """None known."""
        return []

    def class_annotations(self, lines: list[str]) -> list[Annotation]:
        """None known."""
        return []

    def implemented_types(self, lines: list[str]) -> list[str]:
        """None known."""
        return []

    def imports(self, lines: list[str]) -> list[str]:
        """None known."""
        return []


GENERIC = GenericSupport()
