"""Errata index (``.kiro/context/errata.md``): every corrected claim and where the same claim recurs.

One entry per effective NEEDS EDIT or INCORRECT claim. Each entry lists the extraction passages that
repeat the corrected claim, from two sources: the precheck cluster hits for that claim, and a regex
search of every extraction Markdown file for each of the claim's ``superseded_patterns``. Those
passages are marked superseded. The file is loaded first by every agent, so its size is capped:
occurrence lists are shortened first, then the correction text; entries are never dropped.
"""

from __future__ import annotations

import logging
import posixpath
import re
from typing import Any

from bcp.bre import BreLayout
from bcp.claims import Claim
from bcp.render import CONTEXT_DIR, marked_doc
from bcp.review import ReviewData, effective_status
from bcp.textio import read_text
from bcp.trust import TIER2

log = logging.getLogger(__name__)

ERRATA_CAP_BYTES = 12000
ERRATA_WARN_BYTES = 16000  # configurable bcp heuristic, not a Kiro limit
MAX_OCCURRENCES = 10
OCCURRENCE_STEPS = (10, 5, 3, 1, 0)
TEXT_STEPS = (240, 160, 100)
EMPTY = "No corrections recorded."
TRUST_ERRATA = "corrections are reviewed claims; apply the mandatory code check before relying on one"
INTRO = (
    "Read this first. Each entry is a rule whose extraction text was wrong in part (NEEDS EDIT: the corrected "
    "full rule is in reviewed-rules.md) or in its core (INCORRECT: the original text is removed). The same "
    "claim often appears in other extraction files. Those passages are listed as superseded: do not rely on "
    "them, and use the corrected rule instead. A correction is itself a reviewed claim, not a fact."
)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _pattern_hits(layout: BreLayout, patterns: list[str], texts: dict[str, list[str]]) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for pat in patterns:
        try:
            regex = re.compile(pat)
        except re.error:
            log.warning("errata: invalid superseded pattern %r ignored", pat)
            continue
        for rel, lines in texts.items():
            for number, line in enumerate(lines, start=1):
                if regex.search(line):
                    hits.append((rel, number))
    return hits


def build_errata(
    review: ReviewData,
    claims_by_domain: dict[str, list[Claim]],
    layout: BreLayout,
    precheck: dict[str, Any] | None,
    bre_prefix: str,
    domain_order: list[str] | None = None,
) -> dict[str, Any]:
    """Errata data: ``{"entries": [...]}`` with superseded occurrences (workspace-relative paths)."""
    pre_claims = (precheck or {}).get("claims", {})
    texts: dict[str, list[str]] | None = None
    entries: list[dict[str, Any]] = []
    order = domain_order or sorted(claims_by_domain)
    for slug in order:
        signoff = review.domain_signoff(slug)
        for claim in sorted(claims_by_domain.get(slug, []), key=lambda c: (c.number, c.rule_id)):
            claim_id = f"{slug}/{claim.rule_id}"
            record = review.claim(claim_id)
            verdict, who = effective_status(record, signoff, claim.rule_id, claim.body_sha256)
            if verdict not in ("needs-edit", "incorrect") or record is None:
                continue
            if verdict == "needs-edit":
                change = record.get("what_changed") or record.get("correction") or record.get("note") or ""
            else:
                change = record.get("correction") or record.get("note") or "no correction recorded"
            occurrences: list[tuple[str, int]] = []
            pre = pre_claims.get(claim_id)
            if pre and pre.get("body_sha256") == claim.body_sha256:
                occurrences += [(h["path"], int(h["line"])) for h in pre.get("cluster", [])]
            patterns = [p for p in record.get("superseded_patterns") or [] if isinstance(p, str)]
            if patterns:
                if texts is None:
                    texts = {}
                    for path in layout.markdown_files():
                        try:
                            texts[layout.rel(path)] = read_text(path).replace("\r\n", "\n").split("\n")
                        except OSError:
                            continue
                occurrences += _pattern_hits(layout, patterns, texts)
            seen: set[tuple[str, int]] = set()
            unique = []
            for rel, line in sorted(occurrences):
                if (rel, line) not in seen:
                    seen.add((rel, line))
                    unique.append((rel, line))
            entries.append({
                "claim_id": claim_id,
                "status": verdict,
                "title": claim.title,
                "change": " ".join(str(change).split()),
                "reviewer": who,
                "pointer": f"{CONTEXT_DIR}/domains/{slug}/reviewed-rules.md",
                "occurrences": [
                    {"path": posixpath.join(bre_prefix, rel) if bre_prefix else rel, "line": line}
                    for rel, line in unique
                ],
            })
    return {"entries": entries}


POINTER_LINE = (
    f"The full corrected rule (NEEDS EDIT) or the correction (INCORRECT) is in "
    f"`{CONTEXT_DIR}/domains/<slug>/reviewed-rules.md` under the same BR id, with its reviewer and evidence."
)
TITLE_CHARS = 90


def _render(data: dict[str, Any], max_occ: int, max_text: int | None) -> str:
    entries = data.get("entries", [])
    lines = ["# Errata: corrected extraction claims", "", INTRO, ""]
    if not entries:
        lines.append(EMPTY)
        return marked_doc("\n".join(lines), TIER2, TRUST_ERRATA)
    lines += [POINTER_LINE, "", f"{len(entries)} corrected claim(s)."]
    for entry in entries:
        label = "NEEDS EDIT" if entry["status"] == "needs-edit" else "INCORRECT"
        change = _clip(entry["change"], max_text) if max_text else entry["change"]
        lines += ["", f"## {entry['claim_id']} ({label}): {_clip(entry['title'], TITLE_CHARS)}"]
        what = "What changed" if entry["status"] == "needs-edit" else "Correction"
        lines.append(f"- {what}: {change}")
        occ = entry["occurrences"]
        if occ:
            if max_occ == 0:
                lines.append(f"- Superseded elsewhere: {len(occ)} passage(s) (list left out to fit the size cap)")
            else:
                shown = ", ".join(f"`{o['path']}:L{o['line']}`" for o in occ[:max_occ])
                more = len(occ) - min(len(occ), max_occ)
                lines.append(f"- Superseded elsewhere: {shown}" + (f" (+{more} more)" if more else ""))
    return marked_doc("\n".join(lines), TIER2, TRUST_ERRATA)


def render_errata_md(data: dict[str, Any], cap: int = ERRATA_CAP_BYTES) -> str:
    """Errata Markdown within ``cap`` bytes: occurrence lists shrink first, then correction text."""
    text = _render(data, MAX_OCCURRENCES, None)
    if len(text.encode("utf-8")) <= cap:
        return text
    for max_occ in OCCURRENCE_STEPS:
        text = _render(data, max_occ, None)
        if len(text.encode("utf-8")) <= cap:
            return text
    for max_text in TEXT_STEPS:
        text = _render(data, 0, max_text)
        if len(text.encode("utf-8")) <= cap:
            return text
    log.warning("errata.md is %d bytes, over the %d-byte cap, with every entry kept",
                len(text.encode("utf-8")), cap)
    return text
