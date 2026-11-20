# Review guide

## Verdicts

| Verdict | Meaning | Evidence | What Kiro sees |
|---|---|---|---|
| `verified` | The main behaviour and every detail match the code you read. | At least one range (`--evidence path:start-end`, max 40 lines) or search (`--evidence-search REGEX --in SCOPE`). | The rule, marked VERIFIED. |
| `needs-edit` | The main behaviour is right; a detail is wrong. Supply the full corrected rule and a one-line "what changed". | At least one range or search. | Only the corrected rule, marked NEEDS EDIT, with a `> What changed:` line. |
| `incorrect` | The core of the rule is wrong. State what the code does in `--correction`. | At least one range or search showing the actual behaviour. | The body is removed; the correction is listed under "Known BRE corrections". |
| `needs-sme` | Intent, runtime configuration, data or a requirement decides it; static reading cannot. | Optional; write the SME's question in `--note`. | The rule, marked NEEDS SME with the question. |

Prefer `needs-edit` over `incorrect` when most of the rule is right: a rule whose main behaviour is right but whose body is removed loses useful context. Every needs-edit and incorrect claim goes into `.kiro/context/errata.md`, which every agent reads first.

Older review records (`bcp.review/2`) load unchanged. An old `incorrect` entry becomes `needs-edit` only when it carries a full `replacement`; delta-only corrections keep rendering as Known BRE corrections.

Add `--suspected-defect` to any verdict when the code looks wrong against plausible intent (for example a guard that can never fire). The extraction faithfully copies today's defects, so a `verified` claim can still describe a defect.

## Pre-checks (`bcp precheck`)

The pre-checks are mechanical and cheap. A flag is a reason to look, not a verdict.

- Guard check: for a rule that says a method is unguarded or has no authorization check, the tool resolves the cited method and reads the annotations on the method and its class, in the implementation and in every type it implements, across line breaks. `contradicted` means a guard annotation was found; the rule is almost certainly wrong. The annotation names come from the `guard_annotations` config key.
- Absolute words: only, every, all, never, always, unique, none, no X, cannot, must not. About two-thirds of rejected extraction details were absolute claims; check each one.
- Forced-check topics: authorization, transactions and rollback, concurrency, counts, diagrams. Agents must check the code for these whatever the review status, so review them with extra care.
- Citations: every cited `Class.method` resolved to a file and line range.
- Repeats: near-duplicate sentences in other extraction files. When you correct the claim, those passages are listed as superseded in the errata.

## Citations that cannot be found

`review prepare` resolves every cited location. When a cited method, class or file cannot be found, the claim is set to `needs-sme` automatically and an investigation is attached. The investigation tries, in order:

1. similar names in the cited class (same prefix, or edit distance ≤ 2);
2. the same method name declared in another class (possible move);
3. a field, parameter or constant with that name (the extraction cited a non-method);
4. a missing class: a similar class or file name elsewhere (possible rename);
5. malformed extraction text (not code; treated as text);
6. in `drift` only: read-only `git log -S <name>` to find when the name disappeared.

The report shows each as `Why not found: ...` with candidate `file#Lnn` links. Open the candidates, then record your own verdict; the automatic `needs-sme` is never a review.

## Absence claims

A claim such as "no purge method exists" or "there is no lock method" cannot be proven with a code range. Record a search instead:

```
review record ... --verdict verified --evidence-search "\b(purge|delete)\w*\s*\(" --in "src/main/*Order*.java"
```

The tool stores the pattern, the scope, the files scanned with their hashes, the hits (first 20) and the hit count. Zero hits means "searched these files and found nothing". For "no guard" claims, search the interface as well as the implementation, and use a pattern that also matches an annotation split over two lines.

Scope matching: `*` matches across `/` (Python fnmatch), unlike a shell glob. For example the scope `src/main/*Order*.java` also matches `src/main/db/HibernateOrderDAO.java` and `src/main/handler/OrderSaveHandler.java`, not only `src/main/OrderService.java`. Check which files a scope matches before recording; a scope must match 1–200 indexed files.

## Counterexample pass

The second pass asks Kiro to try to break the reviewed text: find code that contradicts the rule, or the correction. It is most useful for needs-edit and incorrect claims (a correction is itself a claim) and for rules on forced-check topics. Record `upheld` or `counterexample-found` with evidence. A counterexample shows as `> Second pass:` on the rule and ranks the claim first in the SME workbook.

## SME workbook and effort

`review workbook export` ranks every flagged claim (needs-edit, incorrect, needs-sme, unreviewed, counterexample found) plus a spot-check sample of verified claims (a quarter, at least one, highest precheck risk first). The effort estimate per domain uses stated assumptions (minutes per claim by verdict, extra time per forced-check topic and per guard contradiction). Calibrate them on the first pilot domain. When review timing exists, the workbook also shows the measured AI review span.

`review workbook import` writes each decided row into the review record: the SME's verdict, replacement text, notes, reviewer and date. The AI evidence is kept and an `sme` evidence item is added; the previous entry moves to `history`.

## What static review cannot prove

- runtime configuration values in each deployment (configuration keys, feature flags, environment);
- behaviour supplied by other repositories, plugins, modules or services;
- data-dependent behaviour and database triggers not in the source;
- framework behaviour the code does not show, such as transaction propagation and rollback-only marking (see `framework-rules.md`);
- whether today's behaviour is intended (that is the SME's call).

Use `needs-sme` for these.
