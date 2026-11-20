---
name: brownfield-context-setup
description: "Generate, regenerate or validate Kiro steering and context from an AWS Transform business-rules extraction (BRE) and its SME review record. Use when setting up a brownfield repository for Kiro, refreshing context after a new BRE or review, or validating .kiro steering."
---

# Brownfield context setup

This skill writes Kiro context into the open repository's `.kiro/` folder from a business-rules extraction (BRE) produced by AWS Transform custom (`AWS/business-rules-extraction`) and, when available, its review record.

The scripts are in the `scripts/` folder beside this SKILL.md. Use the absolute path of the folder you loaded this file from.

Below, `<scripts>` is that absolute folder path. Every command is `python "<scripts>/bcp_cli.py" <command> ...`; quote every path.

## Procedure

1. Check Python: run `python --version` (or `py -3 --version` on Windows). It must be 3.12 or newer. If not, stop and tell the user.
2. Find the inputs, asking the user when you cannot detect them:
   - BRE folder: the folder holding `domain-decomposition-overview/domain-manifest.json` (often `business-rules-extraction/` in the repository root).
   - Review folder (optional): the folder holding `review-record.json` and `signoff.json` from the `bre-review` skill.
   - Config (optional): `bcp.config.json` (see `references/config-reference.md`).
   - Target: the repository root (the workspace folder).
3. Plan: run `python "<scripts>/bcp_cli.py" generate --bre "<bre>" --target "<repo>" --review-dir "<review>" --config "<config>" --dry-run`. Show the user the file list, sizes and the always-included total.
4. Generate: run the same command without `--dry-run`. Without a review folder every rule is labelled UNREVIEWED; say so.
5. Validate: run `python "<scripts>/bcp_cli.py" validate --target "<repo>"`. Exit 0 means no errors.
6. Summarize for the user: files written, warnings (missing domain folders, missing inventory, stale review verdicts, ignored references), and the always-included size.
7. Tell the user to review the generated steering like code before committing `.kiro/` (the tool never commits).

## Notes

- Existing `.kiro/steering/product.md`, `tech.md` or `structure.md` files that bcp did not write are never overwritten; bcp writes `bre-product.md` (and so on) instead.
- Hand-edited generated files are kept unless `--force` is given.
- `--unreviewed exclude` drops unreviewed rules from `reviewed-rules.md` instead of flagging them.
- Exit codes: 0 ok, 1 validation errors, 2 input error, 3 review record error, 4 write conflict or write error.
- Output layout: `references/output-layout.md`.
- Per-program configuration held in database tables: export the configuration table to CSV or JSON (format in `references/program-snapshots.md`), then either run `python "<scripts>/bcp_cli.py" variants --bre "<bre>" --repo "<repo>" --snapshots "<folder>" --out "<out>" --key <key>` for a one-off map, or add `program_snapshots` to the config so `generate` writes `.kiro/context/program-config.md`.
- Cross-component dependencies: `generate` always writes `.kiro/context/dependency-map.md`; `python "<scripts>/bcp_cli.py" deps --bre "<bre>" --repo "<repo>" --out "<out>"` writes the full map anywhere.
- Issue tracker, wiki and git MCP servers: `references/mcp-integration.md` (read-only wiring, trust tiers, the root-cause record format).
- Knowledge base (optional): `python "<scripts>/bcp_cli.py" kb provision|status|teardown --config "<kb-config.json>"` creates, checks or deletes the validated knowledge base in the user's AWS account (needs boto3 1.42 or newer); `kb export`, `kb sync` and `kb stale` keep it current. Follow `references/reference.md`; the CloudFormation template, IAM policies and config example are in `references/infra/`. `kb teardown` without `--confirm` is a dry run; pass `--confirm` only after the user says so.
