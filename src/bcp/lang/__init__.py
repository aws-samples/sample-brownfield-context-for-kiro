"""Language plugins for cheap, mechanical code reading (no LLM).

A plugin finds method declarations and their line ranges, the annotations above a declaration
(across line breaks), class-level annotations, the types a class implements, and imports.
``for_path`` picks the plugin by file extension; unknown languages get the generic plugin, which
only knows the declaration line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Annotation:
    """One annotation: 1-based start and end lines, the joined text and the simple name."""

    start: int
    end: int
    text: str
    name: str


class LanguageSupport(Protocol):
    """What precheck (and later dependency mapping) needs from a language."""

    name: str

    def method_decls(self, lines: list[str], method: str) -> list[int]:
        """1-based declaration lines of ``method`` (all overloads)."""

    def method_range(self, lines: list[str], decl_line: int) -> tuple[int, int]:
        """1-based (start, end) of the method declared on ``decl_line``."""

    def annotations_before(self, lines: list[str], decl_line: int) -> list[Annotation]:
        """Annotations attached to the declaration on ``decl_line`` (above it or inline before it)."""

    def class_annotations(self, lines: list[str]) -> list[Annotation]:
        """Annotations on the first top-level type declaration."""

    def implemented_types(self, lines: list[str]) -> list[str]:
        """Simple names in the first type's ``extends`` / ``implements`` clauses."""

    def imports(self, lines: list[str]) -> list[str]:
        """Imported names."""


def for_path(rel: str) -> LanguageSupport:
    """The plugin for a file path (Java for ``.java``, else generic)."""
    from bcp.lang.generic import GENERIC
    from bcp.lang.java import JAVA

    return JAVA if rel.lower().endswith(".java") else GENERIC
