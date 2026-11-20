"""Command-line interface: ``python -m bcp <command>`` or ``python scripts/bcp_cli.py <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from bcp import __version__
from bcp.config import AGENT_DESIGNS, load_config
from bcp.errors import BcpError, BcpInputError
from bcp.review import RECORD_VERDICTS, REVIEWER_TYPES

log = logging.getLogger("bcp.cli")

FNMATCH_NOTE = (
    "`*` matches across `/` (Python fnmatch), unlike a shell glob. Example: the scope "
    "`src/main/*Order*.java` also matches `src/main/db/HibernateOrderDAO.java` and "
    "`src/main/handler/OrderSaveHandler.java`, not only `src/main/OrderService.java`; list the scope's "
    "files first (a scope must match 1-200 indexed files)."
)


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level = "WARN" if record.levelno == logging.WARNING else record.levelname
        return f"{level} {record.name}: {record.getMessage()}"


def _setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter())
    root = logging.getLogger("bcp")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree (eleven top-level commands)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help="DEBUG logs")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable summary on stdout")
    parser = argparse.ArgumentParser(
        prog="bcp",
        description="Brownfield context: review a business-rules extraction (BRE) and generate Kiro context.",
    )
    parser.add_argument("--version", action="version", version=f"bcp {__version__}")
    parser.add_argument("--verbose", action="store_true", default=False, help="DEBUG logs")
    parser.add_argument("--json", action="store_true", default=False, help="machine-readable summary on stdout")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    gen = sub.add_parser("generate", parents=[common], help="generate .kiro steering and context from a BRE")
    gen.add_argument("--bre", required=True, type=Path, help="BRE output folder")
    gen.add_argument("--target", required=True, type=Path, help="target workspace (repository root)")
    gen.add_argument("--review-dir", type=Path, help="folder with review-record.json / signoff.json")
    gen.add_argument("--config", type=Path, help="bcp.config.json")
    gen.add_argument("--unreviewed", choices=("flag", "exclude"), default="flag",
                     help="flag (default) or exclude unreviewed rules")
    gen.add_argument("--no-copy-bre", action="store_true", help="fail instead of copying a BRE outside the target")
    gen.add_argument("--force", action="store_true", help="overwrite hand-edited generated files")
    gen.add_argument("--refresh-baseline", action="store_true", help="re-resolve every citation baseline entry")
    gen.add_argument("--dry-run", action="store_true", help="print the planned files and sizes; write nothing")
    gen.add_argument("--agent-design", choices=AGENT_DESIGNS, default=None,
                     help="agent and context design: hybrid (default; the entry agent answers narrow one- or "
                          "two-domain questions itself after reading their reviewed rules and delegates broad "
                          "ones), delegate (opt-in: the entry agent routes every question to a specialist), "
                          "narrow-capped (opt-in: anchors in reviewed rules, code reads capped per answer), "
                          "hook-only (opt-in, needs a kb section: the entry agent answers directly from the "
                          "kb-retrieve hook's retrieved context with no sub-agent hop) or kb-first (opt-in, "
                          "needs a kb section: answers from verified-fresh facts and their recorded evidence "
                          "lines without opening the code for them); overrides the config")
    gen.add_argument("--fast-entry-agent", action="store_true", default=None,
                     help="with --agent-design delegate only: also write .kiro/agents/brownfield-fast.json, which "
                          "answers narrow questions itself; overrides the config")

    pre = sub.add_parser("precheck", parents=[common],
                         help="mechanical pre-checks of extracted rules (no AI): absolutes, guards, citations, "
                              "repeated claims")
    pre.add_argument("--bre", required=True, type=Path)
    pre.add_argument("--repo", required=True, type=Path)
    pre.add_argument("--out", required=True, type=Path, help="review folder; writes <out>/precheck/")
    pre.add_argument("--domains", help="comma-separated slugs (default all)")
    pre.add_argument("--config", type=Path)

    val = sub.add_parser("validate", parents=[common], help="validate a workspace's .kiro folder")
    val.add_argument("--target", required=True, type=Path)
    val.add_argument("--budget", type=int, help="advisory always-included steering size in bytes (bcp heuristic, "
                                                "not a Kiro limit; default 16000)")
    val.add_argument("--domain-budget", type=int, default=50000,
                     help="per-file WARN threshold in bytes (bcp heuristic, not a Kiro limit)")
    val.add_argument("--agent-budget", type=int, help="advisory per-agent startup bytes (bcp heuristic; default "
                                                      "81920); Kiro's documented file:// caps are checked separately")

    rev = sub.add_parser("review", help="step 2: claim review worksheets, verdicts, reports and sign-off")
    rsub = rev.add_subparsers(dest="review_command", required=True, metavar="subcommand")
    prep = rsub.add_parser("prepare", parents=[common], help="write claims-<slug>.json worksheets")
    prep.add_argument("--bre", required=True, type=Path)
    prep.add_argument("--repo", required=True, type=Path)
    prep.add_argument("--out", required=True, type=Path)
    prep.add_argument("--domains", help="comma-separated slugs (default all)")
    prep.add_argument("--config", type=Path)
    prep.add_argument("--refresh-baseline", action="store_true")

    rec = rsub.add_parser(
        "record", parents=[common], help="record one verdict with tool-captured evidence",
        description="Record one verdict. Scope matching for --in: " + FNMATCH_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rec.add_argument("--out", required=True, type=Path)
    rec.add_argument("--claim", required=True, help="slug/BR-n")
    rec.add_argument("--verdict", required=True, choices=RECORD_VERDICTS,
                     help="verified; needs-edit (main behaviour right, give the full corrected rule); incorrect "
                          "(core wrong, give --correction); needs-sme")
    rec.add_argument("--evidence", action="append", default=[], help="path:start-end (repeatable, <=40 lines)")
    rec.add_argument("--evidence-search", action="append", default=[], metavar="REGEX",
                     help="regex for an absence/presence search (pair with --in)")
    rec.add_argument("--in", dest="scope", action="append", default=[], metavar="SCOPE",
                     help="repo-relative path or glob for the matching --evidence-search. " + FNMATCH_NOTE)
    rec.add_argument("--note", required=True)
    rec.add_argument("--correction")
    rec.add_argument("--replacement-file", type=Path, help="needs-edit: file holding the full corrected rule")
    rec.add_argument("--replacement", help="needs-edit: the full corrected rule as text")
    rec.add_argument("--what-changed", help="needs-edit: one line on what differs from the extraction")
    rec.add_argument("--superseded-pattern", action="append", default=[], metavar="REGEX",
                     help="regex for other extraction passages that repeat this claim (repeatable)")
    rec.add_argument("--suspected-defect", action="store_true")
    rec.add_argument("--reviewer", required=True)
    rec.add_argument("--reviewer-type", required=True, choices=REVIEWER_TYPES)
    rec.add_argument("--repo", type=Path)
    rec.add_argument("--config", type=Path)

    cex = rsub.add_parser("counterexample", help="optional second AI pass looking for code counterexamples")
    csub = cex.add_subparsers(dest="cex_command", required=True, metavar="subcommand")
    cprep = csub.add_parser("prepare", parents=[common], help="write one counterexample prompt per claim")
    cprep.add_argument("--bre", required=True, type=Path)
    cprep.add_argument("--out", required=True, type=Path)
    cprep.add_argument("--domains", help="comma-separated slugs (default all worksheets)")
    cprep.add_argument("--include-tier3", action="store_true",
                       help="also claims whose precheck shows a forced-check topic")
    cprep.add_argument("--config", type=Path)
    crec = csub.add_parser("record", parents=[common], help="record a counterexample-pass outcome")
    crec.add_argument("--out", required=True, type=Path)
    crec.add_argument("--repo", type=Path)
    crec.add_argument("--claim", required=True)
    crec.add_argument("--outcome", required=True, choices=("upheld", "counterexample-found"))
    crec.add_argument("--note", required=True)
    crec.add_argument("--evidence", action="append", default=[], help="path:start-end (repeatable)")
    crec.add_argument("--reviewer", required=True)
    crec.add_argument("--reviewer-type", required=True, choices=REVIEWER_TYPES)

    wbk = rsub.add_parser("workbook", help="risk-ranked SME workbook: export, then import SME decisions")
    wsub = wbk.add_subparsers(dest="wb_command", required=True, metavar="subcommand")
    wexp = wsub.add_parser("export", parents=[common], help="write sme-workbook.md and .xlsx (or .csv)")
    wexp.add_argument("--out", required=True, type=Path)
    wexp.add_argument("--domains", help="comma-separated slugs (default all worksheets)")
    wexp.add_argument("--format", choices=("auto", "csv", "xlsx"), default="auto",
                      help="auto (xlsx when openpyxl is installed, else csv)")
    wimp = wsub.add_parser("import", parents=[common], help="import SME decisions into review-record.json")
    wimp.add_argument("--out", required=True, type=Path)
    wimp.add_argument("--file", required=True, type=Path)
    wimp.add_argument("--repo", type=Path, help="accepted for symmetry; evidence is not re-captured")
    wimp.add_argument("--dry-run", action="store_true")

    ren = rsub.add_parser("render", parents=[common], help="write review reports and sign-off checklists")
    ren.add_argument("--out", required=True, type=Path)
    ren.add_argument("--repo", required=True, type=Path, help="used only to build evidence links")

    sig = rsub.add_parser("signoff", parents=[common], help="upsert a domain sign-off in signoff.json")
    sig.add_argument("--out", required=True, type=Path)
    sig.add_argument("--domain", required=True)
    sig.add_argument("--reviewer", required=True)
    sig.add_argument("--role", required=True)
    sig.add_argument("--reviewer-type", required=True, choices=REVIEWER_TYPES)
    sig.add_argument("--decision", required=True, choices=("approved", "approved-with-exceptions", "rejected"))
    sig.add_argument("--exceptions", help="comma-separated BR ids")
    sig.add_argument("--checklist-file", type=Path)

    drf = sub.add_parser("drift", parents=[common], help="compare the BRE and review evidence with current code")
    drf.add_argument("--bre", required=True, type=Path)
    drf.add_argument("--repo", required=True, type=Path)
    drf.add_argument("--review-dir", type=Path)
    drf.add_argument("--baseline", type=Path, help="default <repo>/.kiro/context/citation-baseline.json")
    drf.add_argument("--out", required=True, type=Path)
    drf.add_argument("--loc-threshold", type=float, default=0.2)
    drf.add_argument("--config", type=Path)
    drf.add_argument("--git-history", choices=("high", "all", "none"), default="high",
                     help="which citation findings run read-only git log -S (default high)")
    drf.add_argument("--since-generation", action="store_true",
                     help="write the release drift list instead: files changed since the context was generated "
                          "(read-only git diff and status) → domains → claims to re-review")
    drf.add_argument("--base", help="with --since-generation: base revision (default: citation-baseline repo_head)")

    var = sub.add_parser("variants", parents=[common],
                         help="per-program configuration map from code, the BRE and read-only configuration exports")
    var.add_argument("--bre", required=True, type=Path)
    var.add_argument("--repo", required=True, type=Path)
    var.add_argument("--snapshots", action="append", default=[], type=Path, metavar="FILE_OR_DIR",
                     help="CSV or JSON snapshot file or folder (repeatable; default: config program_snapshots)")
    var.add_argument("--out", required=True, type=Path, help="writes program-config.md and .json here")
    var.add_argument("--key", action="append", default=[], metavar="K",
                     help="key (or its constant) for 'which programs a change to K affects' (repeatable; "
                          "default: every differing key)")
    var.add_argument("--config", type=Path)

    dep = sub.add_parser("deps", parents=[common],
                         help="cross-component dependency map: code-derived versus extraction-claimed edges")
    dep.add_argument("--bre", required=True, type=Path)
    dep.add_argument("--repo", required=True, type=Path)
    dep.add_argument("--out", required=True, type=Path, help="writes dependency-map.md and .json here")
    dep.add_argument("--config", type=Path)

    ddr = sub.add_parser("docdrift", parents=[common],
                         help="compare documented behaviour (Markdown or text docs) with the code")
    ddr.add_argument("--docs", required=True, action="append", metavar="DIR_OR_GLOB",
                     help="folder, glob or file of .md/.txt documents (repeatable); for wiki pages, export them "
                          "to a folder first")
    ddr.add_argument("--repo", required=True, type=Path)
    ddr.add_argument("--out", required=True, type=Path, help="writes docdrift-report.md and .json here")
    ddr.add_argument("--config", type=Path)

    chk = sub.add_parser("check-power", parents=[common], help="check a Kiro Power package structure")
    chk.add_argument("--package", required=True, type=Path)

    kb = sub.add_parser("kb", help="knowledge-base layer: provision the AWS resources, export reviewed facts, "
                                   "sync, audit, quarantine stale facts, retrieval hook")
    ksub = kb.add_subparsers(dest="kb_command", required=True, metavar="subcommand")
    kexp = ksub.add_parser(
        "export", parents=[common],
        help="export verified rules and needs-edit rules with an approved replacement, one fact per document",
    )
    kexp.add_argument("--repo", required=True, type=Path, help="source repository (read only; cited lines are hashed)")
    kexp.add_argument("--out", required=True, type=Path,
                      help="writes <out>/verified/<domain>/<FACT-ID>.md(.metadata.json) and <out>/export-audit.json")
    kexp.add_argument("--context", type=Path, help="reviewed context folder (default <repo>/.kiro/context)")
    kexp.add_argument("--review-dir", type=Path, help="review record folder (default <repo>/.kiro/bre-review)")
    kexp.add_argument("--domains", help="comma-separated slugs (default every domain with reviewed-rules.md)")
    kexp.add_argument("--config", type=Path)

    def _kb_aws(p: argparse.ArgumentParser) -> None:
        p.add_argument("--facts", required=True, type=Path, help="export folder holding verified/ (bcp kb export --out)")
        p.add_argument("--state", type=Path, help="kb_state.json (region, bucket, kb_id, data_source_id defaults)")
        p.add_argument("--region", help="AWS Region (default from --state, else us-east-1)")
        p.add_argument("--out", type=Path, help="JSON report file")

    ksync = ksub.add_parser(
        "sync", parents=[common],
        help="upload changed facts to s3://<bucket>/verified/, delete objects no longer exported, ingest",
    )
    _kb_aws(ksync)
    ksync.add_argument("--bucket")
    ksync.add_argument("--kb-id")
    ksync.add_argument("--data-source-id")
    ksync.add_argument("--prefix", default="verified/", help="object key prefix (default verified/)")
    ksync.add_argument("--no-ingest", action="store_true", help="sync S3 only, do not start an ingestion job")
    ksync.add_argument("--dry-run", action="store_true", help="show what would be uploaded and deleted")
    ksync.add_argument("--timeout", type=float, default=900.0, help="ingestion wait limit in seconds")
    kaud = ksub.add_parser("audit", parents=[common],
                           help="list every object under verified/ with its status read back from S3")
    _kb_aws(kaud)
    kaud.add_argument("--bucket")
    kaud.add_argument("--prefix", default="verified/")
    kaud.add_argument("--vector-bucket", help="also count vectors per source in this S3 Vectors bucket")
    kaud.add_argument("--index", help="vector index name (with --vector-bucket)")
    kret = ksub.add_parser("check-retrieval", parents=[common],
                           help="one Retrieve per fact (query from its title): is the fact in the top k")
    _kb_aws(kret)
    kret.add_argument("--kb-id")
    kret.add_argument("--top-k", type=int, default=3)
    kret.add_argument("--workers", type=int, default=4)
    kst = ksub.add_parser(
        "stale", parents=[common],
        help="recompute each fact's source hash from the code; quarantine changed facts, mark them for "
             "re-review, release re-reviewed ones, resync",
    )
    _kb_aws(kst)
    kst.add_argument("--bucket")
    kst.add_argument("--kb-id")
    kst.add_argument("--data-source-id")
    kst.add_argument("--prefix", default="verified/", help="indexed key prefix (default verified/)")
    kst.add_argument("--quarantine-prefix", default="quarantine/", help="quarantine key prefix (default quarantine/)")
    kst.add_argument("--repo", type=Path, help="source repository (default: repo in export-audit.json)")
    kst.add_argument("--source-root", type=Path, help="read cited files from this folder instead of --repo")
    kst.add_argument("--path-map", action="append", default=[], metavar="REL=FILE",
                     help="read repository file REL from FILE (repeatable; e.g. a scratch copy)")
    kst.add_argument("--review-dir", type=Path, help="review record folder (default <repo>/.kiro/bre-review)")
    kst.add_argument("--no-mark", action="store_true", help="do not mark stale facts in the review record")
    kst.add_argument("--no-release", action="store_true", help="never move quarantined facts back")
    kst.add_argument("--no-ingest", action="store_true", help="move objects only, do not start an ingestion job")
    kst.add_argument("--dry-run", action="store_true", help="report only; move and mark nothing")
    kst.add_argument("--timeout", type=float, default=900.0, help="ingestion wait limit in seconds")

    kref = ksub.add_parser(
        "refresh", parents=[common],
        help="deterministic sync pipeline: re-anchor moved-but-unchanged facts, quarantine changed ones, "
             "list coverage gaps, rebuild the graph (optional) and re-ingest once",
    )
    _kb_aws(kref)
    kref.add_argument("--bucket")
    kref.add_argument("--kb-id")
    kref.add_argument("--data-source-id")
    kref.add_argument("--prefix", default="verified/", help="indexed key prefix (default verified/)")
    kref.add_argument("--quarantine-prefix", default="quarantine/", help="quarantine key prefix (default quarantine/)")
    kref.add_argument("--repo", type=Path, help="source repository (default: repo in export-audit.json)")
    kref.add_argument("--source-root", type=Path, help="read cited files from this folder instead of --repo")
    kref.add_argument("--path-map", action="append", default=[], metavar="REL=FILE",
                      help="read repository file REL from FILE (repeatable; e.g. a scratch copy)")
    kref.add_argument("--review-dir", type=Path, help="review record folder (default <repo>/.kiro/bre-review)")
    kref.add_argument("--config", type=Path, help="bcp.config.json (source extensions for the coverage gap)")
    kref.add_argument("--graph-domains", help="comma-separated demo domain slugs: rebuild the graph first")
    kref.add_argument("--context", type=Path, help="generated context folder for the graph rebuild")
    kref.add_argument("--changed-file", action="append", default=[], metavar="PATH",
                      help="limit the coverage gap to these changed files (repeatable; default: scan all source)")
    kref.add_argument("--no-release", action="store_true", help="never move quarantined facts back")
    kref.add_argument("--no-ingest", action="store_true", help="move and upload objects only, do not ingest")
    kref.add_argument("--dry-run", action="store_true", help="report only; move, mark and write nothing to S3")
    kref.add_argument("--timeout", type=float, default=900.0, help="ingestion wait limit in seconds")

    kprov = ksub.add_parser("provision", parents=[common],
                            help="create or reuse the validated-KB AWS resources (idempotent); writes the state file")
    kprov.add_argument("--config", required=True, type=Path,
                       help="kb config JSON (see infra/config/kb-config.example.json)")
    kstat = ksub.add_parser("status", parents=[common], help="read-only status of the validated-KB AWS resources")
    kstat.add_argument("--config", required=True, type=Path, help="kb config JSON")
    ktear = ksub.add_parser("teardown", parents=[common],
                            help="delete the validated-KB resources in dependency order (dry run without --confirm)")
    ktear.add_argument("--config", required=True, type=Path, help="kb config JSON")
    ktear.add_argument("--confirm", action="store_true", help="actually delete")
    ktear.add_argument("--force", action="store_true",
                       help="empty a non-empty facts bucket first (retain_data false only)")

    khook = ksub.add_parser(
        "hook",
        help="Kiro UserPromptSubmit hook: event JSON on stdin; top verified facts + code-graph neighbourhood; "
             "summary (<= 2,500 bytes) on stdout, full result in .kiro/context/retrieved/latest.md; always exit 0",
    )
    from bcp.kb_hook import add_arguments as _hook_arguments

    _hook_arguments(khook)

    gr = sub.add_parser("graph", help="persistent class-level component graph: build it, query it in memory")
    gsub = gr.add_subparsers(dest="graph_command", required=True, metavar="subcommand")
    gbuild = gsub.add_parser(
        "build", parents=[common],
        help="build graph.json (classes, tables, config keys, endpoints; typed edges with evidence and commit)",
    )
    gbuild.add_argument("--repo", required=True, type=Path, help="source repository (read only)")
    gbuild.add_argument("--domains", required=True, help="comma-separated demo domain slugs")
    gbuild.add_argument("--context", type=Path, help="generated context folder (default <repo>/.kiro/context)")
    gbuild.add_argument("--out", type=Path, help="graph file (default <repo>/.kiro/context/graph.json)")
    gbuild.add_argument("--copy-to", action="append", default=[], type=Path, metavar="DIR_OR_FILE",
                        help="also write a copy here (repeatable)")
    gbuild.add_argument("--hops", type=int, default=3,
                        help="classes within this many dependency hops of a demo class are kept (default 3; "
                             "0 = demo classes only)")
    gbuild.add_argument("--upload", metavar="S3_URI",
                        help="s3://bucket[/prefix/] (default prefix graph/): writes graph-<commit>.json and latest.json")
    gbuild.add_argument("--region", default="us-east-1")
    gbuild.add_argument("--config", type=Path)

    gq = gsub.add_parser("query", help="query graph.json (JSON on stdout)")
    gq.add_argument("--graph", type=Path, help="graph file (default .kiro/context/graph.json in the current folder)")
    qsub = gq.add_subparsers(dest="query", required=True, metavar="query")
    for name, text in (("dependents", "what depends on X (depth <= 3)"),
                       ("dependencies", "what X depends on (depth <= 6)")):
        qp = qsub.add_parser(name, help=text)
        qp.add_argument("node", help="node id, class (simple or qualified) name, path, table, key or endpoint")
        qp.add_argument("--depth", type=int, default=1)
        qp.add_argument("--edge-types", help="comma-separated edge types to follow (default all but in-domain)")
        qp.add_argument("--include-structural", action="store_true", help="also follow in-domain edges")
    qpath = qsub.add_parser("path", help="shortest dependency chain from A to B (or B to A)")
    qpath.add_argument("a")
    qpath.add_argument("b")
    qpath.add_argument("--edge-types")
    qpath.add_argument("--undirected", action="store_true", help="allow hops against the edge direction")
    qaff = qsub.add_parser("affected-programs", help="programs and components a configuration key change affects")
    qaff.add_argument("key", help="configuration key or its constant name")
    qaff.add_argument("--depth", type=int, default=1, help="dependents of the reading classes (<= 3)")
    qnode = qsub.add_parser("node", help="one node with its edges")
    qnode.add_argument("node")
    return parser


def _emit(args: argparse.Namespace, summary: dict[str, Any], lines: list[str]) -> None:
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        for line in lines:
            print(line)


def _run(args: argparse.Namespace) -> int:
    if args.command == "generate":
        from bcp.generate import generate

        cfg = load_config(args.config)
        if args.agent_design is not None:
            cfg.agent_design = args.agent_design
        if args.fast_entry_agent:
            cfg.fast_entry_agent = True
        result = generate(args.bre, args.target, args.review_dir, cfg, args.unreviewed,
                          args.no_copy_bre, args.force, args.refresh_baseline, args.dry_run)
        lines = [f"{size:>9}  {rel}" for rel, size in result.files.items()] if args.dry_run else []
        lines.append(
            f"{'planned' if args.dry_run else 'wrote'} {len(result.files) if args.dry_run else len(result.written)} "
            f"files for {result.domains} domains ({result.rules} rules); always-included {result.always_bytes} bytes"
        )
        for name, info in result.agents.items():
            lines.append(f"agent {name}: {info['startup_bytes']} startup bytes, {len(info['resources'])} files"
                         + (f", dropped for budget: {', '.join(info['dropped'])}" if info["dropped"] else ""))
        _emit(args, result.summary(), lines)
        return 0
    if args.command == "validate":
        from bcp.validate import validate

        report = validate(args.target, args.budget, args.domain_budget, args.agent_budget)
        lines = [f.line() for f in report.findings]
        lines += [f"agent {rel}: {size} startup bytes" for rel, size in report.agent_bytes.items()]
        lines.append(f"validate: {report.checked} files, {report.count('ERROR')} errors, {report.count('WARN')} "
                     f"warnings, always-included {report.always_bytes} bytes")
        _emit(args, report.summary(), lines)
        return report.exit_code
    if args.command == "precheck":
        from bcp.precheck import run_precheck

        data = run_precheck(args.bre, args.repo, args.out, _csv(args.domains) or None, load_config(args.config))
        s = data["summary"]
        _emit(args, {"summary": s, "written": data["written"]},
              [f"precheck: {s['claims']} claims, {s['with_absolutes']} with absolutes, {s['guard_contradictions']} "
               f"guard contradictions, {s['clustered']} clustered; wrote {', '.join(data['written'])}"])
        return 0
    if args.command == "review":
        from bcp import review

        if args.review_command == "counterexample":
            if args.cex_command == "prepare":
                from bcp.workbook import prepare_counterexamples

                written = prepare_counterexamples(args.bre, args.out, load_config(args.config),
                                                  _csv(args.domains) or None, args.include_tier3)
                _emit(args, {"written": [str(p) for p in written]},
                      [f"wrote {p}" for p in written] + [f"counterexample prompts: {len(written)}"])
                return 0
            result = review.add_pass(args.out, args.claim, args.outcome, args.note, args.reviewer,
                                     args.reviewer_type, args.evidence, args.repo)
            _emit(args, {"claim": args.claim, "pass": result}, [f"recorded {args.claim}: {args.outcome}"])
            return 0
        if args.review_command == "workbook":
            from bcp import workbook

            if args.wb_command == "export":
                book = workbook.export_workbook(args.out, _csv(args.domains) or None, args.format)
                total = sum(e["minutes"] for e in book.effort)
                _emit(args, {"rows": len(book.rows), "effort": book.effort, "written": [str(p) for p in book.written]},
                      [f"wrote {p}" for p in book.written]
                      + [f"workbook: {len(book.rows)} rows, estimate {total} min ({total / 60:.1f} h)"])
                return 0
            res = workbook.import_workbook(args.out, args.file, args.dry_run)
            lines = [f"skipped {where}: {reason}" for where, reason in res.skipped]
            lines.append(f"{'would import' if res.dry_run else 'imported'} {len(res.imported)} row(s), "
                         f"skipped {len(res.skipped)}")
            _emit(args, {"imported": res.imported, "skipped": res.skipped, "dry_run": res.dry_run}, lines)
            return 0
        if args.review_command == "prepare":
            domains = _csv(args.domains) or None
            written = review.prepare(args.bre, args.repo, args.out, domains,
                                     load_config(args.config), args.refresh_baseline)
            counts = review.citation_counts(args.out, domains)
            lines = [f"wrote {p}" for p in written]
            lines.append(f"citation check: verified-citation {counts['verified-citation']}, "
                         f"needs-sme {counts['needs-sme']}, no-citation {counts['no-citation']}")
            _emit(args, {"written": [str(p) for p in written], "citation_counts": counts}, lines)
            return 0
        if args.review_command == "record":
            if len(args.evidence_search) != len(args.scope):
                raise BcpInputError("each --evidence-search needs one --in SCOPE")
            if args.replacement_file is not None and args.replacement is not None:
                raise BcpInputError("give --replacement-file or --replacement, not both")
            replacement = args.replacement
            if args.replacement_file is not None:
                if not args.replacement_file.is_file():
                    raise BcpInputError(f"replacement file not found: {args.replacement_file}")
                replacement = args.replacement_file.read_text(encoding="utf-8-sig")
            entry = review.record(args.out, args.claim, args.verdict, args.note, args.reviewer, args.reviewer_type,
                                  load_config(args.config), args.evidence,
                                  list(zip(args.evidence_search, args.scope)), args.correction,
                                  args.suspected_defect, args.repo, replacement, args.what_changed,
                                  args.superseded_pattern)
            hits = [f"{e['kind']}: {e.get('path') or e.get('scope')}" + (
                f" ({e['hit_count']} hits in {e['files_scanned']} files)" if e["kind"] == "search" else "")
                for e in entry["evidence"]]
            _emit(args, {"claim": args.claim, "entry": entry},
                  [f"recorded {args.claim}: {args.verdict}"] + [f"  evidence {h}" for h in hits])
            return 0
        if args.review_command == "render":
            written = review.render(args.out, args.repo)
            _emit(args, {"written": [str(p) for p in written]}, [f"wrote {p}" for p in written])
            return 0
        entry = review.signoff(args.out, args.domain, args.reviewer, args.role, args.reviewer_type, args.decision,
                               _csv(args.exceptions), args.checklist_file)
        _emit(args, {"domain": args.domain, "entry": entry}, [f"signed off {args.domain}: {args.decision}"])
        return 0
    if args.command == "variants":
        from bcp.programs import run_variants

        data = run_variants(args.bre, args.repo, args.snapshots, args.out, args.key, load_config(args.config))
        progs = data["programs"]
        summary = {"keys": len(data["catalogue"]), "programs": len(progs),
                   "synthetic": sum(1 for p in progs if p["synthetic"]), "differing": data["differing_keys"],
                   "written": data["written"]}
        _emit(args, summary, [f"variants: {summary['keys']} keys, {summary['programs']} programs "
                              f"({summary['synthetic']} synthetic), {len(data['differing_keys'])} differing keys; "
                              f"wrote {', '.join(data['written'])}"])
        return 0
    if args.command == "deps":
        from bcp.deps import run_deps

        data = run_deps(args.bre, args.repo, args.out, load_config(args.config))
        c = data["edge_counts"]
        _emit(args, {"edge_counts": c, "written": data["written"]},
              [f"deps: {len(data['edges'])} edges (" + ", ".join(f"{v} {k}" for k, v in c.items())
               + f"); wrote {', '.join(data['written'])}"])
        return 0
    if args.command == "docdrift":
        from bcp.docdrift import run_docdrift

        data = run_docdrift(args.docs, args.repo, args.out, load_config(args.config))
        c = data["counts"]
        _emit(args, {"counts": c, "docs": len(data["docs"]), "written": data["written"]},
              [f"docdrift: {len(data['docs'])} docs, HIGH {c['HIGH']}, MEDIUM {c['MEDIUM']}, LOW {c['LOW']}, "
               f"{len(data['claims_to_check'])} claims to check; wrote {', '.join(data['written'])}"])
        return 0
    if args.command == "drift" and args.since_generation:
        from bcp.drift import release_drift, write_release

        data = release_drift(args.bre, args.repo, load_config(args.config), args.base, args.review_dir)
        written = write_release(data, args.out)
        _emit(args, {"base": data["base"], "changed": len(data["changed_files"]), "domains": list(data["domains"]),
                     "written": [str(p) for p in written]},
              [f"release drift: {len(data['changed_files'])} changed files since {data['base'][:12]}, "
               f"{len(data['domains'])} domains touched"] + [f"wrote {p}" for p in written])
        return 0
    if args.command == "drift":
        from bcp.drift import check_drift, write_reports

        report = check_drift(args.bre, args.repo, load_config(args.config), args.review_dir, args.baseline,
                             args.loc_threshold, git_history=args.git_history)
        written = write_reports(report, args.out)
        counts = report.counts()
        _emit(args, {"counts": counts, "high_citation": report.high_citation(),
                     "written": [str(p) for p in written]},
              [f"drift: " + ", ".join(f"{k} {v}" for k, v in counts.items())] + [f"wrote {p}" for p in written])
        return 0
    if args.command == "kb" and args.kb_command == "hook":
        from bcp import kb_hook

        try:
            stdin_text = "" if sys.stdin is None or sys.stdin.isatty() else kb_hook.read_stdin()
            summary, _, _ = kb_hook.run_hook(stdin_text, kb_hook.overrides_from(args))
        except BaseException as exc:  # noqa: BLE001 - the hook never fails the session
            summary = f"## Retrieved context (bcp kb hook): unavailable ({type(exc).__name__})."
        kb_hook.emit(summary)
        return 0
    if args.command == "kb" and args.kb_command in ("provision", "status", "teardown"):
        return _run_kb_provision(args)
    if args.command == "kb" and args.kb_command != "export":
        return _run_kb_aws(args)
    if args.command == "kb":
        from bcp.kb import export_facts

        res = export_facts(args.repo, args.out, load_config(args.config), args.context, args.review_dir,
                           _csv(args.domains) or None)
        lines = [f"kb export {dom}: " + ", ".join(f"{n} {status}" for status, n in sorted(per.items()))
                 for dom, per in res.counts().items()]
        skipped = res.audit["summary"]["skipped_by_reason"]
        lines.append(f"kb export: {len(res.facts)} facts from {res.audit['summary']['rules_seen']} rules seen; "
                     f"skipped " + (", ".join(f"{n} {r}" for r, n in skipped.items()) or "none")
                     + f"; metadata trimmed for {len(res.audit['summary']['metadata_truncated'])}; "
                     f"removed {len(res.removed)} stale files")
        _emit(args, res.summary(), lines)
        return 0
    if args.command == "graph":
        return _run_graph(args)
    if args.command == "check-power":
        from bcp.powercheck import check_power

        report = check_power(args.package)
        lines = [f"ERROR {e}" for e in report.errors]
        lines.append(f"check-power: {'PASS' if report.ok else 'FAIL'} ({len(report.skills)} skills: "
                     f"{', '.join(report.skills)})")
        _emit(args, report.summary(), lines)
        return 0 if report.ok else 1
    raise AssertionError(args.command)


def _run_kb_provision(args: argparse.Namespace) -> int:
    # imported lazily: boto3 stays optional for every other command
    from bcp import kb_provision as kp

    say = logging.getLogger("bcp.kb_provision").info  # progress on stderr, so --json stays clean
    cfg = kp.load_config(args.config)
    if args.kb_command == "provision":
        res = kp.provision(cfg, log=say)
        state_file = kp.validate_config(cfg).state_file
        _emit(args, res, [f"kb provision: kb_id {res['kb_id']}, data_source_id {res['data_source_id']}, "
                          f"bucket {res['bucket']} ({res['region']})", f"state written: {state_file}"])
        return 0
    if args.kb_command == "status":
        res = kp.status(cfg)
        lines = [f"kb status: ready {str(res['ready']).lower()} (account {res['account']}, {res['region']})"]
        for name, info in res["resources"].items():
            detail = info.get("id") or info.get("arn") or ""
            extra = f", status {info['status']}" if info.get("status") else ""
            job = info.get("last_ingestion")
            if job:
                extra += f", last ingestion {job.get('status')}"
            lines.append(f"  {name} {info['name']}: {'exists' if info['exists'] else 'missing'}"
                         + (f" ({detail}{extra})" if detail or extra else ""))
        _emit(args, res, lines)
        return 0
    res = kp.teardown(cfg, confirm=args.confirm, force=args.force, log=say)
    lines = [f"kb teardown{' (dry run, pass --confirm to delete)' if res['dry_run'] else ''}: "
             f"{len(res['plan'])} planned, {len(res['deleted'])} deleted, {len(res['retained'])} retained"]
    lines += [f"  plan: {p}" for p in res["plan"]]
    lines += [f"  deleted: {d}" for d in res["deleted"]]
    lines += [f"  retained: {r}" for r in res["retained"]]
    _emit(args, res, lines)
    return 0


def _run_kb_aws(args: argparse.Namespace) -> int:
    from bcp import kb_sync

    state = kb_sync.load_state(args.state) if args.state else {}

    def pick(name: str, key: str) -> Any:
        value = getattr(args, name, None) or state.get(key)
        return value

    region = pick("region", "region") or "us-east-1"
    if args.kb_command == "sync":
        bucket = pick("bucket", "bucket")
        if not bucket:
            raise BcpInputError("--bucket (or --state) is required")
        res = kb_sync.sync(args.facts, bucket, pick("kb_id", "kb_id"), pick("data_source_id", "data_source_id"),
                           args.prefix, region=region, ingest=not args.no_ingest, dry_run=args.dry_run,
                           timeout_s=args.timeout, report=args.out)
        c, ing = res["counts"], res["ingestion"]
        lines = [f"kb sync{' (dry run)' if args.dry_run else ''}: uploaded {c['uploaded']}, deleted {c['deleted']}, "
                 f"unchanged {c['unchanged']} (s3://{bucket}/{res['prefix']})"]
        if ing:
            lines.append(f"ingestion {ing['job_id']}: {ing['status']} in {ing['seconds']} s "
                         + json.dumps(ing["statistics"], sort_keys=True))
        _emit(args, res, lines)
        return 0
    if args.kb_command == "audit":
        bucket = pick("bucket", "bucket")
        if not bucket:
            raise BcpInputError("--bucket (or --state) is required")
        res = kb_sync.audit(args.facts, bucket, args.prefix, region=region, out=args.out,
                            vector_bucket=pick("vector_bucket", "vector_bucket"), index=pick("index", "index"))
        s = res["summary"]
        lines = [f"kb audit: {s['documents']} documents, {s['metadata_objects']} metadata objects; by status "
                 + json.dumps(s["by_status"], sort_keys=True) + f"; all allowed: {s['all_allowed']}"]
        lines += [f"  violation: {v}" for v in s["violations"][:20]]
        _emit(args, res["summary"], lines)
        return 0 if s["all_allowed"] else 1
    if args.kb_command == "stale":
        from bcp import kb_stale

        res = kb_stale.stale(args.facts, pick("bucket", "bucket"), pick("kb_id", "kb_id"),
                             pick("data_source_id", "data_source_id"), args.repo, args.source_root,
                             kb_stale.parse_path_map(args.path_map), args.review_dir, args.prefix,
                             args.quarantine_prefix, region=region, mark=not args.no_mark,
                             release=not args.no_release, ingest=not args.no_ingest, dry_run=args.dry_run,
                             timeout_s=args.timeout, report=args.out)
        c, ing = res["counts"], res["ingestion"]
        lines = [f"kb stale{' (dry run)' if args.dry_run else ''}: {c['checked']} facts, {c['clean']} clean, "
                 f"quarantined {c['quarantined']}, released {c['released']}, held {c['held']}, "
                 f"marked {c['marked']}"]
        lines += [f"  {r['action']} {r['fact_id']}: {'; '.join(r['reasons']) or 'hash matches, review ok'}"
                  for r in res["facts"]]
        if ing:
            lines.append(f"ingestion {ing['job_id']}: {ing['status']} in {ing['seconds']} s "
                         + json.dumps(ing["statistics"], sort_keys=True))
        _emit(args, res, lines)
        return 0
    if args.kb_command == "refresh":
        from bcp import kb_refresh

        res = kb_refresh.refresh(
            args.facts, load_config(args.config), pick("bucket", "bucket"), pick("kb_id", "kb_id"),
            pick("data_source_id", "data_source_id"), args.repo, args.source_root,
            kb_refresh.kb_stale.parse_path_map(args.path_map), args.review_dir, args.prefix,
            args.quarantine_prefix, graph_domains=_csv(args.graph_domains) or None, context=args.context,
            changed_files=args.changed_file or None, region=region, release=not args.no_release,
            ingest=not args.no_ingest, dry_run=args.dry_run, timeout_s=args.timeout, report=args.out)
        c, ing = res["counts"], res["ingestion"]
        lines = [f"kb refresh{' (dry run)' if args.dry_run else ''}: {c['checked']} facts, "
                 f"re-anchored {c['reanchored']}, quarantined {c['quarantined']}, released {c['released']}, "
                 f"held {c['held']}, marked {c['marked']}, coverage gaps {c['coverage_gap']}"]
        lines += [f"  re-anchored {r['fact_id']}: {', '.join(r['old_ranges'])} -> {', '.join(r['new_ranges'])}"
                  for r in res["reanchored"]]
        lines += [f"  coverage gap: {g}" for g in res["coverage_gap"][:20]]
        if ing:
            lines.append(f"ingestion {ing['job_id']}: {ing['status']} in {ing['seconds']} s "
                         + json.dumps(ing["statistics"], sort_keys=True))
        _emit(args, res, lines)
        return 0
    kb_id = pick("kb_id", "kb_id")
    if not kb_id:
        raise BcpInputError("--kb-id (or --state) is required")
    res = kb_sync.check_retrieval(args.facts, kb_id, region=region, top_k=args.top_k, out=args.out,
                                  workers=args.workers)
    s = res["summary"]
    _emit(args, s, [f"kb check-retrieval: {s['hits']}/{s['facts']} in top {s['top_k']} ({s['hit_rate']:.1%}), "
                    f"hit@1 {s['hit_at_1']}, p50 {s['latency_ms']['p50']:.0f} ms, p95 {s['latency_ms']['p95']:.0f} ms"])
    return 0


def _run_graph(args: argparse.Namespace) -> int:
    if args.graph_command == "build":
        from bcp.graph import run_build

        if args.hops < 0:
            raise BcpInputError("--hops must be >= 0")
        res = run_build(args.repo, load_config(args.config), _csv(args.domains), args.out, args.copy_to,
                        args.context, args.hops, args.upload, args.region)
        lines = [f"graph {res['commit'][:12]}: {res['nodes']} nodes ("
                 + ", ".join(f"{n} {t}" for t, n in res["node_counts"].items()) + f"), {res['edges']} edges ("
                 + ", ".join(f"{n} {t}" for t, n in res["edge_counts"].items()) + f"), {res['bytes']} bytes"]
        lines += [f"wrote {p}" for p in res["written"]] + [f"uploaded {u['uri']}" for u in res["uploaded"]]
        _emit(args, res, lines)
        return 0
    from bcp.graphq import Graph, GraphError

    path = args.graph or Path(".kiro") / "context" / "graph.json"
    try:
        graph = Graph.load(path)
        types = _csv(getattr(args, "edge_types", None)) or None
        if args.query == "dependents":
            result = graph.dependents(args.node, args.depth, types, args.include_structural)
        elif args.query == "dependencies":
            result = graph.dependencies(args.node, args.depth, types, args.include_structural)
        elif args.query == "path":
            result = graph.path(args.a, args.b, types, args.undirected)
        elif args.query == "affected-programs":
            result = graph.affected_programs(args.key, args.depth)
        else:
            node_id = graph.resolve(args.node)
            result = {"query": "node", "node": graph.nodes[node_id], "commit": graph.commit,
                      "out": graph.out.get(node_id, []), "in": graph.inc.get(node_id, [])}
    except GraphError as exc:
        raise BcpInputError(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the command and map errors to exit codes."""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.verbose = getattr(args, "verbose", False)
    args.json = getattr(args, "json", False)
    _setup_logging(args.verbose)
    try:
        return _run(args)
    except BcpError as exc:
        log.error("%s", exc)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - last-resort mapping to exit 70
        log.debug("unexpected error", exc_info=True)
        log.error("unexpected error: %s: %s", type(exc).__name__, exc)
        return 70


if __name__ == "__main__":
    sys.exit(main())
