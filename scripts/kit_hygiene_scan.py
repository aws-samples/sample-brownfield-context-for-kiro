"""Data-hygiene scan: no account ids, knowledge-base ids, access keys, personal paths or customer names.

Complements detect-secrets (which looks for credentials) with checks for identifiers that are not
secrets but must not ship in a customer-neutral kit:

* 12-digit AWS account ids, except the documented example ids in :data:`EXAMPLE_ACCOUNTS`;
* AWS access key ids (``AKIA``/``ASIA`` + 16 characters) and secret-key assignments;
* Amazon Bedrock knowledge-base ids next to ``kb_id`` / ``knowledgeBaseId`` / ``knowledge-base/``,
  except the example ids in :data:`EXAMPLE_KB_IDS`;
* e-mail addresses outside ``example.com`` / ``example.org`` / ``example.net``;
* home-folder paths with a real user name (``C:\\Users\\<name>``, ``/home/<name>``, ``/Users/<name>``);
* any word you list in ``--deny-terms-file`` (one per line, compared case-insensitively): put your
  account ids, resource ids, user names, company, program and system names there. Keep that file
  outside the kit. The unit test ``tests/test_neutrality.py`` covers the tool's own forbidden names.

Usage::

    python scripts/kit_hygiene_scan.py [--root <folder>] [--deny-terms-file <file>] [--out report.json]

Exit code 0 when nothing was found, 1 otherwise. Findings print as ``path:line: kind: match``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", "dist", ".mypy_cache"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".tmpl", ".ps1", ".sh", ".java",
                 ".xml", ".sql", ".html", ".csv", ".cfg", ".ini", ".gitignore", ""}
EXAMPLE_ACCOUNTS = {"111122223333", "123456789012", "999999999999", "000000000000", "444455556666"}
EXAMPLE_KB_IDS = {"KBEXAMPLE1", "KB12345678", "KBID000001", "EXAMPLEKB1", "KBTEST1234", "OTHER00001"}
EXAMPLE_MAIL_DOMAINS = ("example.com", "example.org", "example.net")
PUBLIC_MAILS = {"opensource-codeofconduct@amazon.com"}  # published Amazon open-source contact
PLACEHOLDER_USERS = {"<user>", "<name>", "you", "user", "username", "runner", "ubuntu", "ec2-user", "<you>",
                     "public", "default", "all"}

ACCOUNT_RE = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
SECRET_RE = re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{40}")
KB_ID_RE = re.compile(r"(?:kb_id|knowledgeBaseId|knowledge-base/|KnowledgeBaseId)[\"'\s:=/]*([A-Z0-9]{10})\b")
MAIL_RE = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,})\b")  # a letter TLD: not pkg@1.2.3
HOME_RE = re.compile(r"(?i)(?:[A-Z]:\\{1,2}Users\\{1,2}|/home/|/Users/)([A-Za-z0-9][A-Za-z0-9._-]*)")
WORD_RE = re.compile(r"[a-z0-9]+")


def _files(root: Path) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not (SKIP_DIRS & set(path.relative_to(root).parts)) \
                and (path.suffix.lower() in TEXT_SUFFIXES or path.name in {"SHA256SUMS", "LICENSE"}):
            out.append(path)
    return out


def scan_text(text: str, deny: set[str]) -> list[tuple[int, str, str]]:
    """Findings as (line number, kind, match) for one file's text."""
    found: list[tuple[int, str, str]] = []
    for no, line in enumerate(text.splitlines(), 1):
        for m in ACCOUNT_RE.finditer(line):
            if m.group(0) not in EXAMPLE_ACCOUNTS:
                found.append((no, "account-id", m.group(0)))
        for m in ACCESS_KEY_RE.finditer(line):
            found.append((no, "access-key-id", m.group(0)[:8] + "..."))
        if SECRET_RE.search(line):
            found.append((no, "secret-access-key", "aws_secret_access_key=..."))
        for m in KB_ID_RE.finditer(line):
            if m.group(1) not in EXAMPLE_KB_IDS:
                found.append((no, "knowledge-base-id", m.group(1)))
        for m in MAIL_RE.finditer(line):
            if not m.group(1).lower().endswith(EXAMPLE_MAIL_DOMAINS) and m.group(0).lower() not in PUBLIC_MAILS:
                found.append((no, "email", m.group(0)))
        for m in HOME_RE.finditer(line):
            if m.group(1).lower() not in PLACEHOLDER_USERS and not m.group(1).startswith(("$", "%", "{", "<")):
                found.append((no, "home-path-user", m.group(0)))
        for word in set(WORD_RE.findall(line.lower())):
            if word in deny:
                found.append((no, "denied-term", word[:2] + "*" * (len(word) - 2)))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--deny-terms-file", type=Path, help="extra words to refuse, one per line")
    parser.add_argument("--out", type=Path, help="write the findings as JSON")
    args = parser.parse_args(argv)
    deny: set[str] = set()
    if args.deny_terms_file:
        deny = {w.strip().lower() for w in args.deny_terms_file.read_text(encoding="utf-8-sig").splitlines()
                if w.strip() and not w.startswith("#")}
    root = args.root.resolve()
    rows = []
    files = _files(root)
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for no, kind, match in scan_text(text, deny):
            rel = path.relative_to(root).as_posix()
            rows.append({"path": rel, "line": no, "kind": kind, "match": match})
            print(f"{rel}:{no}: {kind}: {match}")
    result = {"schema": "kit.hygiene-scan/1", "root": root.name, "files_scanned": len(files),
              "extra_deny_terms": len(deny), "findings": rows, "count": len(rows)}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"hygiene scan: {len(files)} files, {len(rows)} findings")
    return 0 if not rows else 1


if __name__ == "__main__":
    sys.exit(main())
