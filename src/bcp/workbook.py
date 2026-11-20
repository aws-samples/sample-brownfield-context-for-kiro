"""SME workbook (export and import) and the optional counterexample second pass.

The Kiro AI review records verdicts with evidence. Two optional steps follow it:

- a counterexample pass: ``prepare`` writes one prompt per NEEDS EDIT / INCORRECT claim (and, on
  request, per claim with a forced-check topic) asking Kiro to look for code that contradicts both the
  rule and its correction; ``review.add_pass`` records the outcome;
- the SME workbook: a risk-ranked list of every flagged claim plus a spot-check sample of verified
  ones, with a per-domain effort estimate. SMEs fill in the ``sme_*`` columns; ``import_workbook``
  writes their decisions back into the review record (the AI entry moves to ``history``).

``.xlsx`` needs openpyxl (imported lazily); without it the editable file is CSV.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bcp.bre import BreLayout
from bcp.config import BcpConfig
from bcp.errors import BcpInputError
from bcp.precheck import flags_line, load_precheck, spot_check_count
from bcp.review import (
    RECORD_VERDICTS,
    REVIEWER_TYPES,
    _rule_num,
    counterexample_found,
    load_review_record,
    load_worksheets,
    put_entry,
    review_span,
    sanitize_replacement,
    validate_record,
)
from bcp.textio import atomic_write_text, dump_json, md_cell

log = logging.getLogger(__name__)

COLUMNS = (
    "rank", "claim_id", "domain", "title", "body_sha256", "ai_verdict", "ai_note",
    "ai_correction_or_replacement", "precheck_flags", "evidence", "sme_verdict", "sme_replacement_text",
    "sme_what_changed", "sme_notes", "sme_reviewer", "sme_reviewer_type", "sme_date",
)
VERDICT_WEIGHT = {"incorrect": 5, "needs-edit": 4, "needs-sme": 4, "unreviewed": 2, "verified": 1}
COUNTEREXAMPLE_BONUS = 3
FLAGGED = ("needs-edit", "incorrect", "needs-sme", "unreviewed")
# Effort assumptions (minutes); calibrate them on the first pilot domain.
MINUTES = {"verified": 6, "needs-edit": 12, "needs-sme": 12, "incorrect": 15, "unreviewed": 10}
MINUTES_PER_TOPIC = 5
MINUTES_PER_GUARD = 5
WORKBOOK_MD = "sme-workbook.md"
WORKBOOK_CSV = "sme-workbook.csv"
WORKBOOK_XLSX = "sme-workbook.xlsx"
COUNTEREXAMPLE_DIR = "counterexample"
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _openpyxl() -> Any | None:
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError:
        return None
    return openpyxl


def _evidence_text(entry: dict[str, Any] | None) -> str:
    parts = []
    for ev in (entry or {}).get("evidence") or []:
        if ev.get("kind") == "range":
            parts.append(f"{ev['path']}:L{ev['start']}-L{ev['end']}")
        elif ev.get("kind") == "search":
            parts.append(f"search /{ev.get('pattern')}/ in {ev.get('scope')}: {ev.get('hit_count')} hits")
        elif ev.get("kind") == "sme":
            parts.append(f"SME {ev.get('reviewer')} {ev.get('date')}")
    return "; ".join(parts)


def recorded_verdict(entry: dict[str, Any] | None, body_sha: str | None) -> str:
    """The record's verdict, ``unreviewed`` when absent or stale."""
    if not entry or entry.get("verdict") in (None, "unreviewed"):
        return "unreviewed"
    if body_sha is not None and entry.get("body_sha256") != body_sha:
        return "unreviewed"
    return str(entry["verdict"])


def _domains(sheets: dict[str, dict[str, Any]], domains: list[str] | None) -> list[str]:
    if not domains:
        return sorted(sheets)
    missing = [d for d in domains if d not in sheets]
    if missing:
        raise BcpInputError(f"no worksheet for domain(s): {', '.join(missing)}; run review prepare first")
    return list(domains)


@dataclass
class Workbook:
    """Ranked rows plus the per-domain effort table."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    effort: list[dict[str, Any]] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)


def _row_minutes(row: dict[str, Any]) -> int:
    minutes = MINUTES.get(row["_verdict"], 10)
    minutes += MINUTES_PER_TOPIC * len(row["_topics"])
    minutes += MINUTES_PER_GUARD if row["_guard"] else 0
    return minutes


def build_workbook(out: Path, domains: list[str] | None = None) -> Workbook:
    """Ranked workbook rows for the selected domains (no files written)."""
    out = Path(out)
    record_path = out / "review-record.json"
    record = load_review_record(record_path) if record_path.is_file() else {"claims": {}}
    sheets = load_worksheets(out)
    precheck = load_precheck(out) or {}
    pre_claims = precheck.get("claims", {})
    book = Workbook()
    for slug in _domains(sheets, domains):
        sheet = sheets[slug]
        flagged, verified = [], []
        for claim in sheet.get("claims", []):
            entry = record["claims"].get(claim["id"])
            verdict = recorded_verdict(entry, claim.get("body_sha256"))
            pre = pre_claims.get(claim["id"])
            if pre and pre.get("body_sha256") != claim.get("body_sha256"):
                pre = None
            found = counterexample_found(entry) if verdict != "unreviewed" else None
            score = VERDICT_WEIGHT[verdict] + (COUNTEREXAMPLE_BONUS if found else 0) + float(
                (pre or {}).get("risk_score", 0))
            live = entry if verdict != "unreviewed" else None
            row = {
                "claim_id": claim["id"],
                "domain": slug,
                "title": claim.get("title", ""),
                "body_sha256": claim.get("body_sha256", ""),
                "ai_verdict": verdict + (" (counterexample found)" if found else ""),
                "ai_note": str((live or {}).get("note") or ""),
                "ai_correction_or_replacement": str((live or {}).get("replacement")
                                                    or (live or {}).get("correction") or ""),
                "precheck_flags": flags_line(pre),
                "evidence": _evidence_text(live),
                "_score": round(score, 2),
                "_verdict": verdict,
                "_num": _rule_num(claim.get("rule_id", "")),
                "_risk": float((pre or {}).get("risk_score", 0)),
                "_topics": list((pre or {}).get("topics", [])),
                "_guard": ((pre or {}).get("guard_check") or {}).get("status") == "contradicted",
                "_spot": False,
            }
            for col in COLUMNS:
                if col.startswith("sme_"):
                    row[col] = ""
            (flagged if verdict in FLAGGED or found else verified).append(row)
        verified.sort(key=lambda r: (-r["_risk"], r["_num"]))
        spot = verified[:spot_check_count(len(verified))]
        for row in spot:
            row["_spot"] = True
        rows = flagged + spot
        book.rows += rows
        minutes = sum(_row_minutes(r) for r in rows)
        span = review_span(sheet, record["claims"])
        book.effort.append({
            "domain": slug,
            "claims": len(sheet.get("claims", [])),
            "flagged": len(flagged),
            "spot_checks": len(spot),
            "to_confirm": len(rows),
            "guard_contradictions": sum(1 for r in rows if r["_guard"]),
            "minutes": minutes,
            "ai_review_span": span,
        })
    book.rows.sort(key=lambda r: (-r["_score"], r["domain"], r["_num"]))
    for rank, row in enumerate(book.rows, start=1):
        row["rank"] = rank
    return book


def spreadsheet_safe(value: str) -> str:
    """Text a spreadsheet will not run: a leading ``'`` before ``= + - @`` TAB or CR (CWE-1236).

    Rule titles, notes and evidence come from extraction output and code, so a cell could otherwise start
    with ``=`` and run as a formula when the SME opens the CSV in a spreadsheet, and openpyxl stores such
    a string as a formula in ``.xlsx``. Only exported display columns are affected; the import reads
    ``claim_id``, ``body_sha256`` and the ``sme_*`` columns, which are never prefixed on export.
    """
    return "'" + value if value.startswith(FORMULA_PREFIXES) else value


def _public(row: dict[str, Any]) -> dict[str, str]:
    return {col: spreadsheet_safe(str(row.get(col, ""))) for col in COLUMNS}


def render_workbook_md(book: Workbook, editable: str) -> str:
    """Read-only Markdown view: effort table, assumptions, ranked rows."""
    total = sum(e["minutes"] for e in book.effort)
    lines = [
        "# SME review workbook",
        "",
        f"Edit `{editable}`, not this file. Fill in the `sme_*` columns for the rows you decide, then run "
        "`bcp review workbook import`. Rows are ranked by risk: verdict weight, a counterexample found in the "
        "second pass, and the mechanical precheck flags.",
        "",
        "## Effort estimate",
        "",
        "| Domain | Claims | Flagged | Verified spot checks | To confirm | Guard contradictions | Estimate | "
        "AI review span |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in book.effort:
        span = e["ai_review_span"].split("(", 1)[-1].rstrip(")") if e["ai_review_span"] else "not measured"
        lines.append(f"| `{e['domain']}` | {e['claims']} | {e['flagged']} | {e['spot_checks']} | {e['to_confirm']} | "
                     f"{e['guard_contradictions']} | {e['minutes']} min ({e['minutes'] / 60:.1f} h) | {md_cell(span)} |")
    lines.append(f"| total | | | | {sum(e['to_confirm'] for e in book.effort)} | | {total} min "
                 f"({total / 60:.1f} h) | |")
    lines += [
        "",
        "Assumptions, to calibrate on the first pilot domain: "
        + ", ".join(f"{v} min per {k} claim" for k, v in MINUTES.items())
        + f" (verified claims are spot checks); +{MINUTES_PER_TOPIC} min per forced-check topic (authorization, "
        f"transactions, concurrency, counts, diagrams); +{MINUTES_PER_GUARD} min per guard contradiction. "
        "The spot-check sample is a quarter of the verified claims (at least one), highest precheck risk first.",
        "",
        "## Rows",
        "",
        "| Rank | Claim | AI verdict | Precheck flags | Title |",
        "|---|---|---|---|---|",
    ]
    for row in book.rows:
        verdict = row["ai_verdict"] + (" (spot check)" if row["_spot"] else "")
        lines.append(f"| {row['rank']} | {row['claim_id']} | {md_cell(verdict)} | {md_cell(row['precheck_flags'])} | "
                     f"{md_cell(row['title'])} |")
    if not book.rows:
        lines.append("| - | none | - | - | - |")
    return "\n".join(lines) + "\n"


def export_workbook(out: Path, domains: list[str] | None = None, fmt: str = "auto") -> Workbook:
    """Write ``sme-workbook.md`` and the editable ``.xlsx`` (openpyxl) or ``.csv``."""
    if fmt not in ("auto", "csv", "xlsx"):
        raise BcpInputError("--format must be auto, csv or xlsx")
    out = Path(out)
    book = build_workbook(out, domains)
    xl = _openpyxl() if fmt in ("auto", "xlsx") else None
    if fmt == "xlsx" and xl is None:
        raise BcpInputError("--format xlsx needs openpyxl; install it or use --format csv")
    if xl is not None:
        path = out / WORKBOOK_XLSX
        wb = xl.Workbook()
        ws = wb.active
        ws.title = "claims"
        ws.append(list(COLUMNS))
        for row in book.rows:
            ws.append([_public(row)[c] for c in COLUMNS])
        effort = wb.create_sheet("effort")
        effort.append(["domain", "claims", "flagged", "spot_checks", "to_confirm", "guard_contradictions", "minutes"])
        for e in book.effort:
            effort.append([e["domain"], e["claims"], e["flagged"], e["spot_checks"], e["to_confirm"],
                           e["guard_contradictions"], e["minutes"]])
        wb.save(path)
    else:
        path = out / WORKBOOK_CSV
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(COLUMNS), lineterminator="\n")
        writer.writeheader()
        for row in book.rows:
            writer.writerow(_public(row))
        path.write_text(buf.getvalue(), encoding="utf-8-sig", newline="")
    md = out / WORKBOOK_MD
    atomic_write_text(md, render_workbook_md(book, path.name))
    book.written = [md, path]
    return book


# --------------------------------------------------------------------------- import


def _read_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".xlsx":
        xl = _openpyxl()
        if xl is None:
            raise BcpInputError("reading .xlsx needs openpyxl; save the sheet as CSV instead")
        wb = xl.load_workbook(path, read_only=True, data_only=True)
        ws = wb["claims"] if "claims" in wb.sheetnames else wb.active
        it = ws.iter_rows(values_only=True)
        header = [str(h or "").strip() for h in next(it, [])]
        rows = [{header[i]: ("" if v is None else str(v)) for i, v in enumerate(vals) if i < len(header)}
                for vals in it]
        wb.close()
    else:
        text = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        header = [h.strip() for h in reader.fieldnames or []]
        rows = [{(k or "").strip(): (v or "") for k, v in r.items()} for r in reader]
    missing = [c for c in ("claim_id", "body_sha256", "sme_verdict", "sme_reviewer", "sme_date") if c not in header]
    if missing:
        raise BcpInputError(f"{path.name}: missing column(s): {', '.join(missing)}")
    return rows


def _iso(date: str) -> str:
    date = date.strip()
    if len(date) == 10:
        datetime.strptime(date, "%Y-%m-%d")
        return f"{date}T00:00:00Z"
    parsed = datetime.fromisoformat(date.replace("Z", "+00:00").replace(" ", "T"))
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ImportResult:
    """Outcome of one import."""

    imported: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    dry_run: bool = False


def import_workbook(out: Path, file: Path, dry_run: bool = False) -> ImportResult:
    """Import every row whose ``sme_verdict`` is filled; stale or invalid rows are skipped with a reason."""
    out, file = Path(out), Path(file)
    if not file.is_file():
        raise BcpInputError(f"workbook file not found: {file}")
    record_path = out / "review-record.json"
    if not record_path.is_file():
        raise BcpInputError(f"{record_path} not found; run review prepare first")
    data = load_review_record(record_path)
    sheets = load_worksheets(out)
    claims = {c["id"]: c for s in sheets.values() for c in s.get("claims", [])}
    result = ImportResult(dry_run=dry_run)
    for number, row in enumerate(_read_rows(file), start=2):
        verdict = row.get("sme_verdict", "").strip().lower()
        if not verdict:
            continue
        claim_id = row.get("claim_id", "").strip()
        where = f"row {number} ({claim_id or 'no claim id'})"

        def skip(reason: str) -> None:
            result.skipped.append((where, reason))

        claim = claims.get(claim_id)
        if claim is None:
            skip("claim is not in any worksheet")
            continue
        if row.get("body_sha256", "").strip() != claim.get("body_sha256"):
            skip("stale: the rule text changed since the workbook was exported")
            continue
        if verdict not in RECORD_VERDICTS:
            skip(f"sme_verdict must be one of {', '.join(RECORD_VERDICTS)}")
            continue
        reviewer = row.get("sme_reviewer", "").strip()
        if not reviewer:
            skip("sme_reviewer is required")
            continue
        try:
            stamp = _iso(row.get("sme_date", ""))
        except ValueError:
            skip("sme_date must be an ISO date (YYYY-MM-DD)")
            continue
        rtype = row.get("sme_reviewer_type", "").strip() or "sme"
        if rtype not in REVIEWER_TYPES:
            skip(f"sme_reviewer_type must be one of: {', '.join(REVIEWER_TYPES)}")
            continue
        notes = " ".join(row.get("sme_notes", "").split())
        replacement = row.get("sme_replacement_text", "").strip()
        changed = " ".join(row.get("sme_what_changed", "").split())
        if verdict == "needs-edit" and not (replacement and changed):
            skip("needs-edit needs sme_replacement_text and sme_what_changed")
            continue
        if verdict == "incorrect" and not notes:
            skip("incorrect needs sme_notes (used as the correction)")
            continue
        prior = data["claims"].get(claim_id) or {}
        prior_live = prior if prior.get("body_sha256") == claim.get("body_sha256") else {}
        evidence = [ev for ev in prior_live.get("evidence") or [] if ev.get("kind") != "sme"]
        evidence.append({"kind": "sme", "reviewer": reviewer, "date": stamp[:10], "note": notes})
        if verdict in ("verified", "needs-edit", "incorrect") and len(evidence) == 1 and rtype != "sme":
            skip("without prior code evidence only reviewer type sme may decide this verdict")
            continue
        entry: dict[str, Any] = {
            "verdict": verdict,
            "body_sha256": claim["body_sha256"],
            "reviewer": reviewer,
            "reviewer_type": rtype,
            "reviewed_at": stamp,
            "note": notes or f"SME decision: {verdict}",
            "correction": notes if verdict == "incorrect" else None,
            "suspected_defect": bool(prior_live.get("suspected_defect")),
            "evidence": evidence,
        }
        if verdict == "needs-edit":
            entry["replacement"] = sanitize_replacement(replacement)
            entry["what_changed"] = changed
        if prior_live.get("superseded_patterns"):
            entry["superseded_patterns"] = list(prior_live["superseded_patterns"])
        put_entry(data, claim_id, entry)
        result.imported.append(claim_id)
    validate_record(data)
    if result.imported and not dry_run:
        atomic_write_text(record_path, dump_json(data))
    return result


# --------------------------------------------------------------------------- counterexample pass

COUNTEREXAMPLE_INSTRUCTIONS = (
    "Look for a counterexample in the code, to the rule and to the correction alike. Read whole methods, not "
    "snippets. Read method-security annotations on the interface and on the implementation, across line "
    "breaks. For error end states, trace the exception type to its catch sites and to the transaction "
    "boundary (propagation, rollback-only marking, self-invocation). Check every absolute word and count. "
    "Then record one outcome: `upheld` when the reviewed text survives, `counterexample-found` when code "
    "contradicts it (cite the lines)."
)


def prepare_counterexamples(bre: Path, out: Path, config: BcpConfig, domains: list[str] | None = None,
                            include_tier3: bool = False) -> list[Path]:
    """Write ``<out>/counterexample/<slug>-<BR>.md`` prompts; return the paths written."""
    bre, out = Path(bre), Path(out)
    record_path = out / "review-record.json"
    record = load_review_record(record_path) if record_path.is_file() else {"claims": {}}
    sheets = load_worksheets(out)
    pre_claims = (load_precheck(out) or {}).get("claims", {})
    layout = BreLayout(bre)
    folder = out / COUNTEREXAMPLE_DIR
    written: list[Path] = []
    for slug in _domains(sheets, domains):
        parsed, _ = layout.load_claims(slug, config)
        texts = {c.rule_id: c for c in parsed}
        for claim in sheets[slug].get("claims", []):
            entry = record["claims"].get(claim["id"])
            verdict = recorded_verdict(entry, claim.get("body_sha256"))
            pre = pre_claims.get(claim["id"])
            if pre and pre.get("body_sha256") != claim.get("body_sha256"):
                pre = None
            wanted = verdict in ("needs-edit", "incorrect") or (include_tier3 and bool((pre or {}).get("topics")))
            if not wanted:
                continue
            parsed_claim = texts.get(claim.get("rule_id"))
            rule_text = parsed_claim.text if parsed_claim else "(rule text not found in the extraction)"
            live = entry if verdict != "unreviewed" else {}
            lines = [
                f"# Counterexample pass: {claim['id']}",
                "",
                f"- Title: {claim.get('title', '')}",
                f"- Current verdict: {verdict}",
                f"- Precheck: {flags_line(pre)}",
                "",
                "## Rule text (extraction)",
                "",
                "```markdown",
                rule_text,
                "```",
            ]
            if verdict == "needs-edit":
                lines += ["", "## Corrected rule (review)", "", f"What changed: {live.get('what_changed')}", "",
                          "```markdown", str(live.get("replacement") or ""), "```"]
            elif verdict == "incorrect":
                lines += ["", "## Correction (review)", "", str(live.get("correction") or "")]
            if live.get("evidence"):
                lines += ["", f"Review evidence: {_evidence_text(live)}"]
            lines += [
                "",
                "## Task",
                "",
                COUNTEREXAMPLE_INSTRUCTIONS,
                "",
                "```",
                f"bcp review counterexample record --out <review-dir> --repo <repo> --claim {claim['id']} "
                "--outcome upheld|counterexample-found --note \"...\" --evidence path:start-end "
                "--reviewer \"...\" --reviewer-type kiro",
                "```",
            ]
            path = folder / f"{slug}-{claim.get('rule_id')}.md"
            folder.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, "\n".join(lines) + "\n")
            written.append(path)
    return written

