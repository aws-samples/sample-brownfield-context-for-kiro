---
name: bre-doc-drift-check
description: "Check whether documentation, the business-rules extraction and the generated Kiro context still match the current code: missing classes or methods, documented values that differ from code defaults, changed files since generation, new unmapped files. Use before a release, after large merges, or when docs may be stale."
---

# Documentation and context drift check

The scripts are in the `scripts/` folder beside this SKILL.md. Use the absolute path of the folder you loaded this file from.

Below, `<scripts>` is that absolute folder path; quote every path. Git is used read-only (`git diff`, `git status`, `git log`); nothing is committed.

## Procedure

1. Release drift list (what changed since the context was generated):
   `python "<scripts>/bcp_cli.py" drift --since-generation --bre "<bre>" --repo "<repo>" --out "<out>" [--base <rev>]`
   The base is the `repo_head` recorded in `<repo>/.kiro/context/citation-baseline.json`, unless `--base` is given. Open `<out>/release-drift.md`: changed files (committed and uncommitted) → domains → claims that cite them (with review status) → the domain's extraction documents to re-check.
2. Documentation drift (documented behaviour versus code):
   `python "<scripts>/bcp_cli.py" docdrift --docs "<docs-folder-or-glob>" --repo "<repo>" --out "<out>"`
   Open `<out>/docdrift-report.md`:
   - HIGH `missing-class`, `missing-method`, `missing-constant`: the doc names code that does not exist (renamed, removed, or never existed).
   - MEDIUM `value-mismatch`: the doc states a configuration value that differs from the code default. MEDIUM `missing-api-path`: no code mentions the path.
   - LOW `doc-older-than-code`: the doc's last commit is older than the code it names.
   - "Claims for Kiro to check": behaviour sentences about named code. Open the code and mark each confirmed, wrong or unverified.
3. Extraction and citation drift:
   `python "<scripts>/bcp_cli.py" drift --bre "<bre>" --repo "<repo>" --review-dir "<review>" --out "<out>"`
   (add `--review-dir` when a review exists, so prepare-time citation states are the baseline; otherwise `<repo>/.kiro/context/citation-baseline.json` written by `generate` is used). Open `<out>/drift-report.md` and explain each finding:
   - HIGH `unresolved-citation`: a cited method, class or file that resolved at baseline is gone now. This is real drift. Read the attached investigation ("Why not found:" and candidates: moved, renamed, field, deleted) and the git history line.
   - INFO `pre-existing-unresolved`: the extraction cited something that never resolved to a method (often a field or parameter). Ask an SME to confirm or correct it; it is not new drift.
   - HIGH/MEDIUM `evidence-moved` and HIGH `evidence-changed`: recorded review evidence no longer matches; the claim's verdict may be stale.
   - HIGH `missing-file`, MEDIUM `loc-changed`, LOW `unmapped-file`: the domain map is out of date.
4. Recommend actions per domain: re-review the affected claims with the `bre-review` skill; fix or flag the drifted docs; re-run the extraction for a component that changed substantially; then regenerate the context with the `brownfield-context-setup` skill.

## Wiki pages (Confluence or similar)

`docdrift` reads Markdown or text files. To check wiki pages, export them to a folder first, through the team's wiki MCP server or the wiki's own export, then pass that folder as `--docs`. Wiki content is Tier 2: documented intent that may lag the code. See `../brownfield-context-setup/references/mcp-integration.md`.

## Notes

- The citation drift git check is read-only (`git log -S`, `git diff --name-only`) and runs for HIGH findings by default (`--git-history high|all|none`). Without git it says so.
- `--loc-threshold 0.2` sets the relative line-count change that counts as `loc-changed`.
- `docdrift` is mechanical. It finds names and values, not wrong behaviour; Kiro checks the listed claims in the code.
