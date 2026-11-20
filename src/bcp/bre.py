"""Business-rules extraction (BRE) folder layout discovery."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

from bcp import claims as claims_mod
from bcp.claims import Claim
from bcp.config import BcpConfig
from bcp.textio import read_text, to_posix

log = logging.getLogger(__name__)

DOMAIN_FILES = (
    "overview.md",
    "features.md",
    "business-rules.md",
    "workflows.md",
    "validations.md",
    "integrations.md",
)


class BreLayout:
    """Finds domain folders and well-known documents inside a BRE root.

    Missing items are warned about once each, so callers may ask repeatedly.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._warned: set[str] = set()
        self._md_files: list[Path] | None = None

    def _warn_once(self, key: str, message: str, *args: object) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    def rel(self, path: Path) -> str:
        """BRE-relative forward-slash path."""
        return to_posix(Path(path).relative_to(self.root))

    def domain_dir(self, slug: str) -> Path | None:
        """``detailed-domain-bre/domain-<slug>/`` or None (WARN once)."""
        path = self.root / "detailed-domain-bre" / f"domain-{slug}"
        if path.is_dir():
            return path
        self._warn_once(f"dir:{slug}", "domain %s: no detailed BRE folder (%s)", slug, self.rel_or_str(path))
        return None

    def rel_or_str(self, path: Path) -> str:
        """BRE-relative path when possible, else the string form."""
        try:
            return self.rel(path)
        except ValueError:
            return str(path)

    def domain_file(self, slug: str, name: str, warn: bool = True) -> Path | None:
        """A file in the domain folder, or None (WARN once unless the folder itself is missing)."""
        folder = self.domain_dir(slug)
        if folder is None:
            return None
        path = folder / name
        if path.is_file():
            return path
        if warn:
            self._warn_once(f"file:{slug}:{name}", "domain %s: %s not found in the BRE", slug, name)
        return None

    def diagrams(self, slug: str) -> list[Path]:
        """Sorted diagram Markdown files of a domain (WARN once when none)."""
        folder = self.domain_dir(slug)
        if folder is None:
            return []
        diag = folder / "diagrams"
        files = sorted(p for p in diag.glob("*.md") if p.is_file()) if diag.is_dir() else []
        if not files:
            self._warn_once(f"diag:{slug}", "domain %s: no diagrams found in the BRE", slug)
        return files

    def markdown_files(self) -> list[Path]:
        """All ``.md`` files under the BRE root, sorted."""
        if self._md_files is None:
            self._md_files = sorted(p for p in self.root.rglob("*.md") if p.is_file())
        return self._md_files

    def find_root_doc(self, name_glob: str) -> Path | None:
        """First Markdown file (sorted) whose file name matches ``name_glob``."""
        for path in self.markdown_files():
            if fnmatch.fnmatchcase(path.name.lower(), name_glob.lower()):
                return path
        return None

    def inventory_docs(self, globs: list[str]) -> list[Path]:
        """Markdown files whose name matches any of ``globs``."""
        return [
            p
            for p in self.markdown_files()
            if any(fnmatch.fnmatchcase(p.name.lower(), g.lower()) for g in globs)
        ]

    def find_doc(self, file_name: str) -> Path | None:
        """A document by exact file name anywhere in the BRE (sorted first)."""
        return self.find_root_doc(file_name)

    def load_claims(self, slug: str, config: BcpConfig) -> tuple[list[Claim], str | None]:
        """Parse a domain's ``business-rules.md``; returns (claims, BRE-relative file or None)."""
        path = self.domain_file(slug, "business-rules.md")
        if path is None:
            return [], None
        parsed = claims_mod.parse(
            read_text(path), config.rule_heading_re, config.source_line_re, config.source_extensions
        )
        return parsed, self.rel(path)
