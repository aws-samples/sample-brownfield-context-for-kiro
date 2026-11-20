"""AC 10: the shipped package and tests are customer- and project-neutral.

Scans src/, power/, tools/, dist/ (if built), docs/, README.md outside the demo block, and tests/ (except
this file). Also checks that shipped Markdown says "Kiro", never "coding agent".

The forbidden words are stored as SHA-256 digests of the lower-case word, so this file does not name
them either. Every word in a scanned file (letters and digits, split on anything else) is lower-cased,
hashed and compared. Add a digest with:
``python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].lower().encode()).hexdigest())" <word>``
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SHA256 = frozenset({
    "ca3f67cfd416e76f63e399dcae7459b8923c95d53b3c6b3e4616e5945b08912a",  # customer program name
    "a4b8a7551f1447f6814b2fc9c3472a91be74ff217eb3f61f80cfe0107fe4e792",  # customer deployment name
    "ce371d3c6bedcfcabbfe11c748778817e8020f151ccc53f2831008ac6eff9ddb",  # customer name
    "5dff34b08b9cd4f1eb3b09ae08962261d390157212dda3614106b335d76c2d98",  # reference codebase (demo block only)
})
WORD_RE = re.compile(r"[a-z0-9]+")
DEMO_BLOCK = re.compile(r"<!-- demo:start -->.*?<!-- demo:end -->", re.S)
TEXT_SUFFIXES = {".py", ".md", ".json", ".tmpl", ".ps1", ".sh", ".txt", ".toml", ".java", ".xml", ".sql", ".html"}


def _files() -> list[Path]:
    out: list[Path] = []
    for folder in ("src", "power", "tools", "dist", "tests", "docs"):
        base = ROOT / folder
        if base.is_dir():
            out += [p for p in base.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES
                    and "__pycache__" not in p.parts and p.name != Path(__file__).name]
    return out


def _text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return DEMO_BLOCK.sub("", text) if path.name == "README.md" else text


def forbidden_words(text: str) -> list[str]:
    """Words in ``text`` whose lower-case SHA-256 is in :data:`FORBIDDEN_SHA256`."""
    words = set(WORD_RE.findall(text.lower()))
    return sorted(w for w in words if hashlib.sha256(w.encode("utf-8")).hexdigest() in FORBIDDEN_SHA256)


def test_forbidden_word_check_works() -> None:
    # a digest computed here must match the stored one, so a typo in the set cannot silently pass
    sample = "".join(chr(c) for c in (99, 117, 98, 105, 99))
    assert forbidden_words(f"x {sample.upper()} y") == [sample]
    assert forbidden_words("nothing to see") == []


def test_no_customer_or_project_names() -> None:
    files = _files() + ([ROOT / "README.md"] if (ROOT / "README.md").is_file() else [])
    hits = []
    for path in files:
        for word in forbidden_words(_text(path)):
            hits.append(f"{path.relative_to(ROOT)}: {word}")
    assert not hits, hits


def test_shipped_markdown_names_kiro() -> None:
    shipped = [p for p in _files() if p.suffix in (".md", ".tmpl") and "tests" not in p.relative_to(ROOT).parts]
    if (ROOT / "README.md").is_file():
        shipped.append(ROOT / "README.md")
    offenders = [str(p.relative_to(ROOT)) for p in shipped if re.search(r"(?i)coding agents?", _text(p))]
    assert not offenders, offenders
