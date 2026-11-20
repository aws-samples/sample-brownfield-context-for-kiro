---
name: bre-review
description: "Review a business-rules extraction against source code, claim by claim: run mechanical pre-checks, record verdicts (verified, needs-edit, incorrect, needs-sme) with evidence, run an optional counterexample pass, and export a risk-ranked SME workbook and import the SME's decisions. Use for BRE review, SME review prep, or checking extracted business rules."
---

# BRE review (step 2)

The extraction is evidence, not truth: it describes today's code, including today's defects, and it was written from static reading. Its main behaviour is usually right; its side details often are not. This skill checks each extracted rule (BR-n) against the source and records a verdict with evidence the tool captures from disk. Kiro does the first pass; a named SME confirms or overrides it.

The scripts are in the `scripts/` folder beside this SKILL.md. Use the absolute path of the folder you loaded this file from.

Below, `<scripts>` is that absolute folder path; run `python "<scripts>/bcp_cli.py" <command> ...` and quote every path.

## Procedure

1. Prepare one domain at a time, right before reviewing it (the report measures review time from this point):
   `python "<scripts>/bcp_cli.py" review prepare --bre "<bre>" --repo "<repo>" --out "<review>" --domains <slug>`
   This writes `claims-<slug>.json` and creates or extends `review-record.json`. Claims whose cited method, class or file cannot be found are set to needs-sme automatically, with an investigation ("Why not found:" plus candidate locations). Never treat that automatic verdict as a review.
2. Run the mechanical pre-checks (no AI, a few seconds):
   `python "<scripts>/bcp_cli.py" precheck --bre "<bre>" --repo "<repo>" --out "<review>" --domains <slug>`
   Read `<review>/precheck/precheck.md`. Start with its guard contradictions: the rule says a method is unguarded, but a guard annotation was found on the interface or the implementation. Then look at absolute words (only, every, never, no X), forced-check topics and claims repeated in other extraction files.
3. For each claim in `claims-<slug>.json`:
   - open every candidate location and read the whole method, not only the cited line;
   - for authorization, read the annotations on the interface and the implementation, across line breaks;
   - for error end states, trace the exception to its catch sites and the transaction boundary (see `framework-rules.md`);
   - for each investigation, open its candidates and decide whether the code moved, was renamed, is a field rather than a method, or is gone;
   - compare every assertion in the rule body with the code, including every absolute word and count.
4. Record the verdict (the tool reads the quote from disk; never type quotes yourself):
   `python "<scripts>/bcp_cli.py" review record --out "<review>" --repo "<repo>" --claim <slug>/BR-n --verdict verified|needs-edit|incorrect|needs-sme --evidence <path>:<start>-<end> --note "<what you checked>" --reviewer "<name>" --reviewer-type <type>`
   - `needs-edit`: the main behaviour is right but a detail is wrong. Write the full corrected rule to a file and pass `--replacement-file "<file>"` (or `--replacement "<text>"`) and `--what-changed "<one line>"`. The corrected rule replaces the original in the generated context; the original stays only in the review record.
   - `incorrect`: the core is wrong. Pass `--correction "<what the code actually does>"`; the rule body is removed from the context.
   - When the same wrong claim appears in other extraction files, add `--superseded-pattern "<regex>"` (repeatable). The errata index marks those passages superseded.
   - For an absence claim ("no purge method exists", "no lock method"), use `--evidence-search "<regex>" --in "<scope>"` so the record holds the search, the files scanned and the zero hits. List the scope's files first: `*` matches across `/` (Python fnmatch), unlike a shell glob.
   - Use `needs-sme` for intent, runtime configuration or requirement questions; write the question in `--note`.
   - Add `--suspected-defect` when the code looks wrong against plausible intent.
   - Reviewer types: `kiro`, `sme`, or `kiro (AI stand-in for SME)` for a demo without an SME. Only `sme` ever counts as SME.
5. Optional counterexample pass (a second AI pass). Write one prompt per needs-edit or incorrect claim, plus claims with a forced-check topic:
   `python "<scripts>/bcp_cli.py" review counterexample prepare --bre "<bre>" --out "<review>" --domains <slug> --include-tier3`
   For each prompt in `<review>/counterexample/`, look for code that contradicts the rule and the correction, then record the outcome:
   `python "<scripts>/bcp_cli.py" review counterexample record --out "<review>" --repo "<repo>" --claim <slug>/BR-n --outcome upheld|counterexample-found --note "..." --evidence <path>:<start>-<end> --reviewer "<name>" --reviewer-type kiro`
   A counterexample is shown on the rule and ranks it first in the SME workbook. Then re-record the verdict if the claim needs it.
6. Render: `python "<scripts>/bcp_cli.py" review render --out "<review>" --repo "<repo>"`. This writes `review-report-<slug>.md` and `signoff-checklist-<slug>.md`.
7. Export the SME workbook: `python "<scripts>/bcp_cli.py" review workbook export --out "<review>" --domains <slug>`. It writes `sme-workbook.md` (risk-ranked rows and an effort estimate per domain) and `sme-workbook.xlsx`, or `sme-workbook.csv` when openpyxl is not installed. The SME fills in the `sme_*` columns. Import their decisions:
   `python "<scripts>/bcp_cli.py" review workbook import --out "<review>" --file "<filled workbook>" [--dry-run]`
   Each row needs a verdict, the reviewer and an ISO date; needs-edit needs the replacement text and what changed; incorrect needs notes. Rows whose rule text changed since export are skipped as stale.
8. Ask the human whether they are the SME for this domain. If not, or if no human confirms, sign off as Kiro:
   `python "<scripts>/bcp_cli.py" review signoff --out "<review>" --domain <slug> --reviewer "<name>" --role "<role>" --reviewer-type kiro --decision approved|approved-with-exceptions|rejected --exceptions BR-n,... --checklist-file "<filled checklist>"`
   Use `--reviewer-type sme` only when a named human SME confirms.
9. Summarize: counts per verdict, guard contradictions, suspected defects, the SME effort estimate, and the rows the SME should start with.

## References

- `references/review-guide.md`: verdict definitions, pre-checks, absence claims, scope matching, what static review cannot prove.
- `references/signoff-checklist.md`: the six sign-off checklist items.
