"""Emit and strictly parse the small YAML front-matter subset that bcp writes."""

from __future__ import annotations

import re

KEY_ORDER = ("inclusion", "name", "description", "fileMatchPattern")
_BARE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Bare words YAML would read as non-strings; these are always quoted.
_YAML_SPECIAL = {"true", "false", "yes", "no", "on", "off", "null", "y", "n", "~"}
_NUMERIC_RE = re.compile(r"^[-+]?(\d[\d_]*)?(\.\d+)?([eE][-+]?\d+)?$|^0[xob][0-9a-fA-F_]+$")
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s(.*))?$")


class FrontMatterError(ValueError):
    """Raised when text does not follow the strict front-matter subset."""


def _quote(value: str) -> str:
    value = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _scalar(value: str) -> str:
    text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if (
        _BARE_RE.match(text)
        and text.lower() not in _YAML_SPECIAL
        and not _NUMERIC_RE.match(text)
    ):
        return text
    return _quote(text)


def emit(data: dict[str, object]) -> str:
    """Return a front-matter block (``---`` ... ``---`` plus newline) for ``data``.

    Keys follow ``KEY_ORDER`` first, then any others in insertion order.
    Lists become flow arrays of double-quoted strings.
    """
    keys = [k for k in KEY_ORDER if k in data] + [k for k in data if k not in KEY_ORDER]
    lines = ["---"]
    for key in keys:
        value = data[key]
        if isinstance(value, (list, tuple)):
            items = ", ".join(_quote(str(item)) for item in value)
            lines.append(f"{key}: [{items}]")
        else:
            lines.append(f"{key}: {_scalar(str(value))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _parse_quoted(text: str, pos: int) -> tuple[str, int]:
    """Parse a double-quoted string starting at ``text[pos] == '"'``; return (value, next pos)."""
    if text[pos] != '"':
        raise FrontMatterError(f"expected '\"' at column {pos}")
    out: list[str] = []
    i = pos + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text) or text[i + 1] not in '\\"':
                raise FrontMatterError("unsupported escape in quoted string")
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise FrontMatterError("unterminated quoted string")


def _parse_value(raw: str) -> object:
    raw = raw.strip()
    if raw.startswith("["):
        items: list[str] = []
        i = 1
        while True:
            while i < len(raw) and raw[i] == " ":
                i += 1
            if i < len(raw) and raw[i] == "]" and not items:
                i += 1
                break
            value, i = _parse_quoted(raw, i)
            items.append(value)
            while i < len(raw) and raw[i] == " ":
                i += 1
            if i < len(raw) and raw[i] == ",":
                i += 1
                continue
            if i < len(raw) and raw[i] == "]":
                i += 1
                break
            raise FrontMatterError("malformed flow array")
        if raw[i:].strip():
            raise FrontMatterError("trailing text after flow array")
        return items
    if raw.startswith('"'):
        value, end = _parse_quoted(raw, 0)
        if raw[end:].strip():
            raise FrontMatterError("trailing text after quoted string")
        return value
    if not _BARE_RE.match(raw):
        raise FrontMatterError(f"unsupported bare value: {raw!r}")
    return raw


def split(text: str) -> tuple[list[str], str]:
    """Split ``text`` into front-matter lines and the body; raise if the block is malformed."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        raise FrontMatterError("front matter must start with '---' on line 1")
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r") == "---":
            body = "\n".join(lines[index + 1:])
            return [ln.rstrip("\r") for ln in lines[1:index]], body
    raise FrontMatterError("front matter is not closed with '---'")


def parse(text: str) -> tuple[dict[str, object], str]:
    """Strictly parse front matter emitted by :func:`emit`; return (data, body)."""
    block, body = split(text)
    data: dict[str, object] = {}
    for line in block:
        if not line.strip():
            continue
        match = _KEY_RE.match(line)
        if not match:
            raise FrontMatterError(f"bad front-matter line: {line!r}")
        key, raw = match.group(1), match.group(2) or ""
        if key in data:
            raise FrontMatterError(f"duplicate key: {key}")
        data[key] = _parse_value(raw)
    return data, body
