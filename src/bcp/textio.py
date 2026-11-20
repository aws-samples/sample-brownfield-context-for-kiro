"""Text I/O, hashing and the single safe path-joining helper."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

from bcp.errors import BcpInputError

log = logging.getLogger(__name__)


def read_text(path: Path) -> str:
    """Read a text file as UTF-8 (BOM tolerated); undecodable bytes are replaced with a WARN."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        log.warning("non-UTF-8 bytes replaced while reading %s", path)
        return data.decode("utf-8-sig", errors="replace")


def write_text(path: Path, text: str) -> None:
    """Write text as UTF-8 with LF line endings, creating parent folders."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalized)


REPLACE_RETRIES = 8
REPLACE_DELAY_S = 0.25


def replace_with_retry(src: Path, dest: Path) -> None:
    """``os.replace`` that retries ``PermissionError`` (on Windows an IDE, an indexer or a file-sync
    agent briefly holds the destination open), then re-raises the last error."""
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            log.debug("replace %s busy; retry %d", dest, attempt + 1)
            time.sleep(REPLACE_DELAY_S * (attempt + 1))


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8/LF text to a temp file beside ``path`` and ``os.replace`` it into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    write_text(tmp, text)
    replace_with_retry(tmp, path)


def sha256_text(text: str) -> str:
    """Return the hex SHA-256 of the UTF-8 encoding of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_safe_relpath(rel: str) -> bool:
    """Return True if ``rel`` is relative and has no ``..`` segment (either separator)."""
    if not rel or not rel.strip():
        return False
    if PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute():
        return False
    if PureWindowsPath(rel).drive or rel.startswith(("/", "\\")):
        return False
    parts = rel.replace("\\", "/").split("/")
    return ".." not in parts


def safe_join(root: Path, rel: str) -> Path:
    """Join ``rel`` under ``root``; reject absolute paths, ``..`` and anything escaping root.

    This is the only path joiner used for untrusted relative paths.
    """
    if not is_safe_relpath(rel):
        raise BcpInputError(f"unsafe relative path: {rel!r}")
    root_abs = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(root_abs / rel.replace("\\", "/")))
    try:
        candidate.relative_to(root_abs)
    except ValueError as exc:
        raise BcpInputError(f"path escapes {root_abs}: {rel!r}") from exc
    return candidate


def dump_json(data: object) -> str:
    """Deterministic JSON text: 2-space indent, sorted keys, UTF-8, trailing newline."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def to_posix(path: str | Path) -> str:
    """Return ``path`` with forward slashes."""
    return str(path).replace("\\", "/")


def truncate_words(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` chars at a word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit + 1].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut[:limit].rstrip(" ,;:")


def first_sentence(text: str, limit: int = 160) -> str:
    """First sentence of ``text`` (split after ``.``/``!``/``?`` + space), at most ``limit`` chars."""
    text = " ".join((text or "").split())
    match = re.search(r"(?<=[.!?])\s", text)
    sentence = text[: match.start()] if match else text
    return truncate_words(sentence, limit)


def md_cell(text: object) -> str:
    """Make a value safe inside a Markdown table cell."""
    return " ".join(str(text if text is not None else "").split()).replace("|", "\\|")
