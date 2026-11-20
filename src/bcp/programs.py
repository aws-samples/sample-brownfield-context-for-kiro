"""Per-program configuration map: ``bcp variants``.

Many systems keep per-program (per-deployment, per-tenant) configuration in database tables. This
module joins three sources into one map:

- a key catalogue from the code and the extraction: config key → constant(s) → read sites (with
  domains) → code default → behaviour per value (extraction rules that name the key, and literal values
  compared near the read sites);
- per-program value snapshots imported read-only from CSV or JSON exports of the configuration
  table (values masked by key pattern; no other data is read);
- a per-program comparison and, per key, "which programs a change to this key affects".

Snapshot formats (see ``power/.../references/program-snapshots.md``):

- CSV, UTF-8 (a BOM is tolerated), RFC 4180 quoting, exact header ``program_id,property,property_value``
  (or ``program_id,key,value``), one row per key. A file may hold one or more programs.
- JSON: ``{"program_id": "...", "synthetic": true|false, "properties": {"key": "value"}}``, or a list of
  ``{"program_id", "property", "property_value"}`` rows.

A key with no row is *absent*: the code default applies. An empty value is the empty string. A file
whose name starts with ``SYNTHETIC-``, or a JSON object with ``"synthetic": true``, marks its programs
SYNTHETIC in every output. Duplicate keys for one program and wrong headers are input errors.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bcp.bre import BreLayout
from bcp.config import BcpConfig
from bcp.errors import BcpInputError
from bcp.manifest import DomainManifest
from bcp.sourceindex import SourceIndex
from bcp.textio import atomic_write_text, dump_json, md_cell, read_text

log = logging.getLogger(__name__)

SCHEMA_PROGRAMS = "bcp.programs/1"
CSV_HEADERS = (("program_id", "property", "property_value"), ("program_id", "key", "value"))
MASKED = "***masked***"
MAX_VALUE_CHARS = 200
MAX_READ_SITES = 20
MAX_RULES = 10
MAX_NEAR_VALUES = 10
NEAR_LINES = 5
MD_SITES = 5
ABSENT = "absent: code default applies"
SNAPSHOT_SUFFIXES = (".csv", ".json")
NEAR_VALUE_RES = (
    re.compile(r'"([^"\n]{1,80})"\s*\.\s*equals(?:IgnoreCase)?\s*\('),
    re.compile(r'\.\s*equals(?:IgnoreCase)?\s*\(\s*"([^"\n]{1,80})"\s*\)'),
    re.compile(r'\bcase\s+"([^"\n]{1,80})"\s*:'),
)


@dataclass
class Program:
    """One program's imported values (already masked and clipped)."""

    program_id: str
    source: str
    synthetic: bool
    values: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- snapshot import


def _clip(value: str) -> str:
    return value if len(value) <= MAX_VALUE_CHARS else value[:MAX_VALUE_CHARS] + "…(truncated)"


def mask_value(key: str, value: str, patterns: list[str]) -> str:
    """``***masked***`` when ``key`` matches a mask pattern, else the value clipped to 200 chars."""
    if any(re.search(p, key) for p in patterns):
        return MASKED
    return _clip(value)


def _add(programs: dict[str, Program], pid: str, key: str, value: str, source: str, synthetic: bool,
         where: str) -> None:
    if not pid:
        raise BcpInputError(f"{where}: empty program_id")
    if not key:
        raise BcpInputError(f"{where}: empty property key")
    prog = programs.setdefault(pid, Program(pid, source, synthetic))
    if key in prog.values:
        raise BcpInputError(f"{where}: duplicate key {key!r} for program {pid!r}")
    prog.values[key] = value


def _load_csv(path: Path, synthetic: bool) -> list[Program]:
    text = path.read_bytes().decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    if not rows:
        raise BcpInputError(f"{path.name}: empty file")
    header = tuple(c.strip() for c in rows[0])
    if header not in CSV_HEADERS:
        raise BcpInputError(f"{path.name}: header must be {','.join(CSV_HEADERS[0])} "
                            f"(or {','.join(CSV_HEADERS[1])}), got {','.join(header)}")
    programs: dict[str, Program] = {}
    for number, row in enumerate(rows[1:], start=2):
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) != 3:
            raise BcpInputError(f"{path.name}:{number}: expected 3 fields, got {len(row)}")
        _add(programs, row[0].strip(), row[1].strip(), row[2], path.name, synthetic, f"{path.name}:{number}")
    return list(programs.values())


def _load_json(path: Path, synthetic: bool) -> list[Program]:
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise BcpInputError(f"{path.name}: invalid JSON: {exc}") from exc
    programs: dict[str, Program] = {}

    def text(value: Any, where: str) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return json.dumps(value)
        raise BcpInputError(f"{where}: values must be strings, numbers or booleans")

    if isinstance(data, dict):
        pid = str(data.get("program_id") or "").strip()
        props = data.get("properties")
        if not isinstance(props, dict):
            raise BcpInputError(f"{path.name}: expected an object with 'program_id' and 'properties'")
        is_synth = synthetic or data.get("synthetic") is True
        for key, value in props.items():
            _add(programs, pid, str(key).strip(), text(value, path.name), path.name, is_synth, path.name)
        if not props:
            programs[pid] = Program(pid, path.name, is_synth)
    elif isinstance(data, list):
        for number, row in enumerate(data, start=1):
            if not isinstance(row, dict):
                raise BcpInputError(f"{path.name}[{number}]: each row must be an object")
            key = row.get("property", row.get("key"))
            value = row.get("property_value", row.get("value"))
            if key is None or "program_id" not in row or value is None:
                raise BcpInputError(f"{path.name}[{number}]: needs program_id, property and property_value")
            _add(programs, str(row["program_id"]).strip(), str(key).strip(), text(value, path.name), path.name,
                 synthetic, f"{path.name}[{number}]")
    else:
        raise BcpInputError(f"{path.name}: expected a JSON object or list")
    return list(programs.values())


def load_snapshot(path: Path, mask_patterns: list[str]) -> list[Program]:
    """Programs from one CSV or JSON snapshot file, values masked and clipped."""
    path = Path(path)
    synthetic = path.name.upper().startswith("SYNTHETIC-")
    loader = _load_json if path.suffix.lower() == ".json" else _load_csv
    programs = loader(path, synthetic)
    for prog in programs:
        prog.values = {k: mask_value(k, v, mask_patterns) for k, v in prog.values.items()}
    return programs


def snapshot_files(paths: list[Path]) -> list[Path]:
    """Snapshot files from files and folders (a folder contributes its ``*.csv`` and ``*.json``)."""
    out: list[Path] = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            out += sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SNAPSHOT_SUFFIXES)
        elif path.is_file():
            out.append(path)
        else:
            raise BcpInputError(f"snapshot path not found: {path}")
    return out


def load_snapshots(paths: list[Path], mask_patterns: list[str]) -> list[Program]:
    """Every program from every snapshot; a program id in two files is an input error."""
    seen: dict[str, str] = {}
    programs: list[Program] = []
    for path in snapshot_files(paths):
        for prog in load_snapshot(path, mask_patterns):
            if prog.program_id in seen:
                raise BcpInputError(f"program {prog.program_id!r} appears in {seen[prog.program_id]} and {path.name}")
            seen[prog.program_id] = path.name
            programs.append(prog)
    return sorted(programs, key=lambda p: p.program_id)


# --------------------------------------------------------------------------- key catalogue


def _strip_quotes(text: str) -> tuple[str, bool]:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1], True
    return text, False


def find_constants(index: SourceIndex, patterns: list[str], literals: set[str], names: set[str]) -> dict[str, list[dict[str, Any]]]:
    """``{literal: [{constant, path, line}]}`` for constant definitions whose literal is wanted (or whose
    name is in ``names``)."""
    compiled = [re.compile(p) for p in patterns]
    out: dict[str, list[dict[str, Any]]] = {}
    for rel in index.paths:
        for number, line in enumerate(index.lines(rel), start=1):
            if '"' not in line or "=" not in line:
                continue
            for regex in compiled:
                for match in regex.finditer(line):
                    const, literal = match.group(1), match.group(2)
                    if literal in literals or const in names:
                        out.setdefault(literal, []).append({"constant": const, "path": rel, "line": number})
    return out


def _site_regexes(literal: str, constants: list[str]) -> list[re.Pattern[str]]:
    regs = [re.compile(re.escape(f'"{literal}"'))]
    regs += [re.compile(rf"\b{re.escape(c)}\b") for c in constants]
    return regs


def find_read_sites(index: SourceIndex, literal: str, constants: list[str], definitions: set[tuple[str, int]],
                    file_index: dict[str, list[str]]) -> dict[str, Any]:
    """Lines that use the key (its constant or its quoted literal), excluding the constant definitions."""
    regs = _site_regexes(literal, constants)
    needles = [f'"{literal}"'] + constants
    sites: list[dict[str, Any]] = []
    total = 0
    domains: set[str] = set()
    for rel in index.paths:
        lines = index.lines(rel)
        blob = "\n".join(lines)
        if not any(n in blob for n in needles):
            continue
        for number, line in enumerate(lines, start=1):
            if (rel, number) in definitions or not any(r.search(line) for r in regs):
                continue
            total += 1
            domains.update(file_index.get(rel, []))
            sites.append({"path": rel, "line": number, "domains": file_index.get(rel, [])})
    # mapped main-source sites first, then other main sources, tests and resources last
    sites.sort(key=lambda s: (not s["domains"], "/test/" in f"/{s['path']}", not s["path"].endswith(
        tuple(e for e in index.extensions if e not in (".xml", ".yml", ".yaml", ".properties", ".sql"))),
        s["path"], s["line"]))
    return {"sites": sites[:MAX_READ_SITES], "total": total, "domains": sorted(domains)}


def find_defaults(index: SourceIndex, patterns: list[str], const_to_literal: dict[str, str],
                  wanted: set[str]) -> dict[str, list[dict[str, Any]]]:
    """``{literal: [{value, expression, path, line, pattern}]}``, earlier patterns first."""
    compiled = [re.compile(p) for p in patterns]
    needles = set(const_to_literal) | {f'"{w}"' for w in wanted}
    out: dict[str, list[dict[str, Any]]] = {}
    for order, regex in enumerate(compiled):
        for rel in index.paths:
            for number, line in enumerate(index.lines(rel), start=1):
                if not any(n in line for n in needles):
                    continue
                for match in regex.finditer(line):
                    key, quoted = _strip_quotes(match.group("key"))
                    literal = key if quoted else const_to_literal.get(key.rsplit(".", 1)[-1])
                    if literal not in wanted:
                        continue
                    value, literal_default = _strip_quotes(match.group("default"))
                    out.setdefault(literal, []).append({"value": value, "expression": not literal_default,
                                                        "path": rel, "line": number, "pattern": order})
    return out


def near_values(index: SourceIndex, sites: list[dict[str, Any]], literal: str) -> list[dict[str, Any]]:
    """String literals compared within ±5 lines of a read site (``"x".equals(``, ``equals("x")``, ``case "x":``)."""
    found: dict[str, dict[str, Any]] = {}
    for site in sites:
        lines = index.lines(site["path"])
        low, high = max(0, site["line"] - 1 - NEAR_LINES), min(len(lines), site["line"] + NEAR_LINES)
        for idx in range(low, high):
            for regex in NEAR_VALUE_RES:
                for match in regex.finditer(lines[idx]):
                    value = match.group(1)
                    if value != literal and value not in found:
                        found[value] = {"value": value, "path": site["path"], "line": idx + 1}
    return list(found.values())[:MAX_NEAR_VALUES]


def rules_naming(layout: BreLayout, manifest: DomainManifest, config: BcpConfig,
                 literals: dict[str, list[str]]) -> dict[str, list[dict[str, str]]]:
    """Extraction rules whose text names the key literal or one of its constants."""
    regs = {lit: [re.compile(re.escape(lit))] + [re.compile(rf"\b{re.escape(c)}\b") for c in consts]
            for lit, consts in literals.items()}
    out: dict[str, list[dict[str, str]]] = {lit: [] for lit in literals}
    for dom in manifest.ordered_domains():
        claims, _ = layout.load_claims(dom.slug, config)
        for claim in claims:
            for lit, rs in regs.items():
                if len(out[lit]) < MAX_RULES and any(r.search(claim.text) for r in rs):
                    out[lit].append({"claim": f"{dom.slug}/{claim.rule_id}", "title": claim.title[:90]})
    return out


def _inventory_by_key(variants: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {e["key"]: e for e in (variants or {}).get("entries", [])}


def build_program_config(
    layout: BreLayout,
    manifest: DomainManifest,
    config: BcpConfig,
    index: SourceIndex | None,
    file_index: dict[str, list[str]],
    programs: list[Program],
    keys: list[str] | None = None,
    variants: dict[str, Any] | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    """The program configuration map (JSON-friendly)."""
    inventory = _inventory_by_key(variants)
    requested = list(keys or [])
    wanted = {k for p in programs for k in p.values} | set(requested)
    constants = find_constants(index, config.variant_constant_patterns, wanted, set(requested) | set(inventory)) \
        if index is not None else {}
    const_to_literal = {d["constant"]: lit for lit, defs in constants.items() for d in defs}
    # a requested constant name becomes its literal key
    resolved_requested = [const_to_literal.get(k, k) for k in requested]
    catalogue_keys = sorted({k for p in programs for k in p.values} | set(resolved_requested))
    if not catalogue_keys:
        catalogue_keys = sorted(lit for lit, defs in constants.items() if any(d["constant"] in inventory for d in defs))
    wanted = set(catalogue_keys)
    definitions = {(d["path"], d["line"]) for defs in constants.values() for d in defs}
    defaults = find_defaults(index, config.variant_default_patterns, const_to_literal, wanted) \
        if index is not None else {}
    names = {lit: sorted({d["constant"] for d in constants.get(lit, [])}) for lit in catalogue_keys}
    rules = rules_naming(layout, manifest, config, names)
    catalogue: dict[str, Any] = {}
    for lit in catalogue_keys:
        consts = names[lit]
        reads = find_read_sites(index, lit, consts, definitions, file_index) if index is not None else \
            {"sites": [], "total": 0, "domains": []}
        inv = [inventory[k] for k in [lit, *consts] if k in inventory]
        found = sorted(defaults.get(lit, []), key=lambda d: (d["pattern"], d["path"], d["line"]))
        default = found[0] if found else None
        if default is not None:
            default = dict(default, value=mask_value(lit, default["value"], config.variant_mask_patterns))
        catalogue[lit] = {
            "constants": [dict(d) for d in constants.get(lit, [])],
            "inventory": [{"key": e["key"], "domain": e.get("domain"), "effect": e.get("effect"),
                           "source": e.get("source")} for e in inv],
            "default": default,
            "other_defaults": [dict(d, value=mask_value(lit, d["value"], config.variant_mask_patterns))
                               for d in found[1:]],
            "read_sites": reads["sites"],
            "read_site_total": reads["total"],
            "read_by": reads["domains"],
            "rules": rules.get(lit, []),
            "values_near_reads": near_values(index, reads["sites"], lit) if index is not None else [],
        }
    # comparison
    table: dict[str, dict[str, Any]] = {}
    differing: list[str] = []
    for lit in catalogue_keys:
        default = catalogue[lit]["default"]
        row: dict[str, Any] = {}
        compare: set[str] = set()
        for prog in programs:
            if lit in prog.values:
                row[prog.program_id] = {"present": True, "value": prog.values[lit], "effective": prog.values[lit]}
                compare.add(prog.values[lit])
            else:
                eff = default["value"] if default and not default["expression"] else None
                label = f"{ABSENT} ({default['value']})" if default else f"{ABSENT} (default not found in code)"
                row[prog.program_id] = {"present": False, "value": None, "effective": label}
                compare.add(eff if eff is not None else f"<absent:{lit}>")
        table[lit] = row
        literal_default = default["value"] if default and not default["expression"] else None
        if len(compare) > 1 or (programs and literal_default is not None and compare != {literal_default}):
            differing.append(lit)
    affects_keys = resolved_requested or differing
    affects = {}
    for lit in affects_keys:
        groups: dict[str, list[str]] = {}
        for prog in programs:
            groups.setdefault(table.get(lit, {}).get(prog.program_id, {}).get("effective", f"{ABSENT}"), []).append(
                prog.program_id)
        affects[lit] = {"groups": [{"effective": k, "programs": v} for k, v in groups.items()],
                        "read_by": catalogue.get(lit, {}).get("read_by", []),
                        "read_sites": catalogue.get(lit, {}).get("read_sites", [])[:MD_SITES]}
    return {
        "schema": SCHEMA_PROGRAMS,
        "sources": sources or sorted({p.source for p in programs}),
        "programs": [{"program_id": p.program_id, "source": p.source, "synthetic": p.synthetic,
                      "keys": len(p.values)} for p in programs],
        "mask_patterns": list(config.variant_mask_patterns),
        "code_scanned": index is not None,
        "catalogue": catalogue,
        "comparison": table,
        "differing_keys": differing,
        "affects": affects,
    }


# --------------------------------------------------------------------------- rendering


def _label(prog: dict[str, Any]) -> str:
    return f"{prog['program_id']} (SYNTHETIC)" if prog["synthetic"] else prog["program_id"]


def _loc(item: dict[str, Any]) -> str:
    return f"{item['path']}:L{item['line']}"


def _value(text: str | None) -> str:
    if text is None:
        return "-"
    return f"`{md_cell(text)}`" if text else "(empty string)"


def render_program_config_md(data: dict[str, Any]) -> str:
    """Markdown body (no marker or Trust line) for ``program-config.md``."""
    progs = data["programs"]
    synthetic = [p["program_id"] for p in progs if p["synthetic"]]
    lines = ["# Per-program configuration map", ""]
    if synthetic:
        lines += [f"> SYNTHETIC DATA: program(s) {', '.join(synthetic)} are invented demo snapshots, not real "
                  "deployments. Do not present their values as customer facts.", ""]
    lines += [
        "Per-program values come from read-only exports of the configuration table. A key with no row is "
        f"\"{ABSENT}\"; an empty value is the empty string. Values of keys that match a mask pattern show as "
        f"`{MASKED}`. Read sites, constants and defaults are mechanical (code scan). Extraction rules and "
        "behaviour notes are Tier 2: check the code before you state them.",
        "",
        "Sources: " + (", ".join(data["sources"]) or "none"),
        "",
    ]
    if not data["code_scanned"]:
        lines += ["Code scan skipped (no source files); read sites and defaults are not listed.", ""]
    lines += ["## Programs", "", "| Program | Source | Synthetic | Keys |", "|---|---|---|---|"]
    lines += [f"| {md_cell(p['program_id'])} | {md_cell(p['source'])} | {'yes' if p['synthetic'] else 'no'} | "
              f"{p['keys']} |" for p in progs]
    if not progs:
        lines.append("| - | - | - | 0 |")
    lines += ["", "## Per-program comparison", ""]
    if data["differing_keys"]:
        lines += ["Only keys whose effective value differs between programs or from the code default.", "",
                  "| Key | Code default | " + " | ".join(md_cell(_label(p)) for p in progs) + " |",
                  "|---|---|" + "---|" * len(progs)]
        for key in data["differing_keys"]:
            default = data["catalogue"][key]["default"]
            cells = []
            for prog in progs:
                cell = data["comparison"][key][prog["program_id"]]
                cells.append(_value(cell["value"]) if cell["present"] else md_cell(cell["effective"]))
            lines.append(f"| `{md_cell(key)}` | {_value(default['value']) if default else 'not found'} | "
                         + " | ".join(cells) + " |")
    else:
        lines.append("No key differs between programs or from its code default.")
    lines += ["", "## Programs a change affects", ""]
    for key, item in data["affects"].items():
        lines += [f"### `{key}`", "", "| Effective value | Programs |", "|---|---|"]
        for group in item["groups"]:
            lines.append(f"| {md_cell(group['effective'])} | {md_cell(', '.join(group['programs']))} |")
        sites = ", ".join(_loc(s) for s in item["read_sites"]) or "none found"
        lines += ["", f"Read by domains: {', '.join(item['read_by']) or 'none mapped'}. Read sites: {sites}.", ""]
    if not data["affects"]:
        lines += ["No key selected.", ""]
    lines += ["## Key catalogue", ""]
    for key, entry in data["catalogue"].items():
        consts = ", ".join(f"`{c['constant']}` ({_loc(c)})" for c in entry["constants"]) or "none found"
        default = entry["default"]
        dline = (f"{_value(default['value'])}{' (expression)' if default['expression'] else ''} ({_loc(default)})"
                 if default else "not found in code")
        if entry["other_defaults"]:
            dline += "; also " + ", ".join(f"{_value(d['value'])} ({_loc(d)})" for d in entry["other_defaults"][:3])
        sites = ", ".join(_loc(s) for s in entry["read_sites"][:MD_SITES])
        more = entry["read_site_total"] - min(MD_SITES, len(entry["read_sites"]))
        lines += [f"### `{key}`", "", f"- Constant: {consts}", f"- Code default: {dline}",
                  f"- Read sites ({entry['read_site_total']}; domains {', '.join(entry['read_by']) or 'none mapped'}): "
                  f"{sites or 'none found'}" + (f" (+{more} more)" if more > 0 else "")]
        if entry["inventory"]:
            inv = "; ".join(f"{e['domain'] or '-'}: {e['effect'] or '-'}" for e in entry["inventory"])
            lines.append(f"- Extraction inventory (Tier 2): {md_cell(inv)}")
        if entry["rules"]:
            lines.append("- Extraction rules naming it (Tier 2, verify): "
                         + ", ".join(f"{r['claim']} ({md_cell(r['title'])})" for r in entry["rules"]))
        if entry["values_near_reads"]:
            lines.append("- Values seen in code near read sites (mechanical): "
                         + ", ".join(f"`{md_cell(v['value'])}` ({_loc(v)})" for v in entry["values_near_reads"]))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def repo_index(repo: Path, bre: Path, config: BcpConfig) -> SourceIndex | None:
    """Source index of ``repo`` (the BRE folder excluded when it sits inside), or None without source."""
    exclude = list(config.exclude_dirs)
    try:
        exclude.append(Path(bre).resolve().relative_to(Path(repo).resolve()).parts[0])
    except (ValueError, IndexError):
        pass
    index = SourceIndex(Path(repo), config.source_extensions, exclude)
    return index if index.has_source() else None


def run_variants(bre: Path, repo: Path, snapshots: list[Path], out: Path, keys: list[str],
                 config: BcpConfig) -> dict[str, Any]:
    """``bcp variants``: build the map and write ``program-config.md`` / ``.json`` into ``out``."""
    from bcp.impact import build_file_index
    from bcp.manifest import load_manifest
    from bcp.variants import build_variants

    paths = [Path(p) for p in snapshots] or [Path(p) for p in config.program_snapshots]
    programs = load_snapshots(paths, config.variant_mask_patterns)
    manifest = load_manifest(Path(bre))
    layout = BreLayout(Path(bre))
    index = repo_index(Path(repo), Path(bre), config)
    file_index = build_file_index(manifest)
    variants = build_variants(layout, config, manifest.slugs, index, file_index)
    data = build_program_config(layout, manifest, config, index, file_index, programs, keys, variants,
                                [p.name for p in snapshot_files(paths)])
    data["written"] = [str(p) for p in write_program_config(data, out)]
    return data


def write_program_config(data: dict[str, Any], out: Path) -> list[Path]:
    """Write ``program-config.md`` and ``program-config.json`` into ``out``."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "program-config.md", out / "program-config.json"
    atomic_write_text(md, render_program_config_md(data))
    atomic_write_text(js, dump_json(data))
    return [md, js]
