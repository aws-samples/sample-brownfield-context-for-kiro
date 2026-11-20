# Output layout

`generate` writes only under `<target>/.kiro/`:

```
.kiro/
├── steering/
│   ├── product.md, tech.md, structure.md                               (always)
│   ├── bre-context-index.md                                            (auto; the per-domain directory, pinned into every agent)
│   ├── bre-use-brownfield-agent.md                                      (always; points default-agent users to `brownfield`)
│   ├── bre-domain-<slug>.md ×N                                          (auto)
│   ├── bre-domain-<slug>-code.md ×N                                     (fileMatch)
│   ├── bre-impact-analysis.md, bre-variants.md, bre-framework-rules.md  (auto)
│   └── bre-glossary.md                                                  (manual)
├── context/
│   ├── errata.md              (every corrected claim and the passages that repeat it; loaded first)
│   ├── framework-rules.md     (framework semantics static reading misses; team-maintained)
│   ├── trust-tiers.md         (legend of the trust tiers)
│   ├── domains/<slug>/summary.md, reviewed-rules.md
│   ├── impact-map.md, impact-map.json, file-index.json
│   ├── variant-map.md, variant-map.json, glossary.md
│   ├── dependency-map.md, dependency-map.json (code-derived versus extraction-claimed edges, interfaces,
│   │                          integration points, shared tables; the .md is scoped to agent_domains when set)
│   ├── program-config.md, program-config.json (only with program_snapshots: per-program values, comparison,
│   │                          "which programs a change to K affects")
│   ├── review-status.md       (counts per domain and verdict; sign-off state)
│   ├── citation-baseline.json (per-claim citation resolution states, drift baseline)
│   ├── generation-record.json (files bcp owns and their hashes)
│   └── bre/                   (only when the BRE is outside the target)
├── skills/{onboarding-guide, change-impact-analysis, requirement-gap-check, defect-triage, scoped-test-generation,
│          cross-component-dependencies, doc-drift-check}/SKILL.md
└── agents/brownfield.json, impact-analysis.json, product-onboarding.json, defect-triage.json, requirement-gap.json
```

## The context contract

- The context is a map; the code is the truth. Every generated context Markdown file carries a `> Trust:` line after the generated marker:
  - Tier 1, mechanical: tool-derived or team-maintained files (`file-index.json`, `citation-baseline.json`, `review-status.md`, `framework-rules.md`, `dependency-map.*` (its `bre-only` rows are Tier 3 claims), the extraction's database documentation and data ownership).
  - Tier 2, AI narrative for orientation: `reviewed-rules.md` (each rule has its own status), `errata.md`, `summary.md`, the impact and variant maps, `program-config.*`, the glossary, and extraction overviews, features, validations and integrations.
  - Tier 3, hypothesis only: extraction workflows, every diagram, cross-cutting error and integration documents, the external-integrations map and coupling hotspots. Tier 3 files are never preloaded; agents list them as on-demand navigation aids.
- `reviewed-rules.md` gives each rule a `> Review:` line (status, reviewer, evidence path and line range). VERIFIED rules keep the extraction text. NEEDS EDIT rules show only the reviewer's full corrected rule, with a `> What changed:` line. INCORRECT claims move to "Known BRE corrections". A `> Second pass:` line shows a counterexample found in the optional second review pass.
- `errata.md` lists every NEEDS EDIT and INCORRECT claim with the other extraction passages that repeat it (from `bcp precheck` and the review's superseded patterns), marked superseded. Its size is capped at 12,000 bytes: occurrence lists shrink first, entries are never dropped.
- Every agent prompt says: the context is a map and the code is the truth; a mandatory code check applies, whatever the review status, before stating anything about authorization or its absence, transactions, rollback, commit or error end states, concurrency, absolute claims, counts, or what a diagram asserts; and each statement is tagged `[code: path#Lx-Ly]`, `[reviewed context: <file> BR-n]` or `[unreviewed extraction, unverified: <file>]`. Every answer ends with `Context used:`. `bcp validate` fails on prompts that call the context the source of truth.

## Agents

- Use the context through the `brownfield` custom agent (`kiro-cli chat --agent brownfield`). It has `read` plus `subagent/<specialist>` tools, an explicit shell deny rule and a subagent allow rule. It loads `product.md`, `tech.md`, `structure.md`, `bre-context-index.md`, `errata.md`, `framework-rules.md`, the four task skills and the `cross-component-dependencies` and `doc-drift-check` skills. Default design (`hybrid`): it scopes each question. A narrow question (one component, rule, setting, error or change in one or two domains) it answers itself, after reading those domains' `reviewed-rules.md` in one batch. A broad question (three or more domains, an end-to-end flow, system-wide impact) it delegates to one of four specialists:
  - `impact-analysis`: errata, framework rules, impact and variant maps, the program configuration map (when configured), the dependency map, feature-flag inventory and data ownership. On demand (Tier 3): coupling hotspots, external integrations, integration and event-publishing patterns.
  - `product-onboarding`: errata, framework rules, bounded contexts, decomposition overview and each agent domain's `overview.md`. On demand (Tier 3): architecture and sequence diagrams, each domain's `workflows.md`.
  - `defect-triage`: errata, framework rules, each agent domain's `reviewed-rules.md`, `validations.md` and `integrations.md`. On demand (Tier 3): error-handling patterns, common error codes.
  - `requirement-gap`: errata, framework rules, each agent domain's `features.md` and `reviewed-rules.md`, plus the API endpoint catalog.
- Specialists have `read` only, shell denied, their matching `skill://`, and `file://` resources that point only at existing files. `domain-manifest.json` is never loaded. Each prompt names its startup files with their tier.
- Every agent also lists `product.md`, `tech.md`, `structure.md` and `bre-context-index.md` explicitly as `file://` resources, first and never dropped. The Kiro docs disagree on whether custom agents get steering automatically ([steering](https://kiro.dev/docs/steering/) says no, the [agent configuration reference](https://kiro.dev/docs/custom-agents/configuration-reference/) says they inherit it), and listing it works under both.
- The [agent configuration reference](https://kiro.dev/docs/custom-agents/configuration-reference/) says a sub-agent that declares its own `file://` resources loads them. In Kiro CLI runs, specialists invoked by `brownfield` as sub-agents did not have them in context. Each specialist prompt therefore lists its startup files and tells it to read them all first, in one parallel batch, when they are not already in its context.
- Each agent's startup files stay within Kiro's documented `file://` caps (32 files, 100,000 characters in total, 50,000 characters per file) and the advisory `agent_budget_bytes` (a bcp heuristic, not a Kiro limit; see config-reference.md). Files are taken in the order above (most relevant first) and kept while they fit; a file that does not fit is dropped, logged and named in the prompt as read-on-demand.
- `bcp validate` also checks that every agent keeps shell denied, that every `subagent/<name>` tool names an agent in `.kiro/agents`, that every rule heading in `reviewed-rules.md` has its `> Review:` line (and a NEEDS EDIT rule its `> What changed:` line), and that every generated context Markdown file has a `> Trust:` line.

## Opt-in variants

All are off by default; the default output above does not change unless you ask for one.

- **Delegate design** (`--agent-design delegate`, or `"agent_design": "delegate"`): `brownfield` loads only the pinned steering (foundation files and the index) and routes every question to a specialist. Same contract and code rule.
- **Fast entry agent** (`--agent-design delegate --fast-entry-agent`, or `"fast_entry_agent": true` with the delegate design): also writes `.kiro/agents/brownfield-fast.json`, which answers narrow questions itself and delegates broad ones. The hybrid default already does this in `brownfield`.
- **Narrow-capped design** (`--agent-design narrow-capped`; experimental): VERIFIED rules with range evidence get a `> Anchors` line and the header says to cite them without opening code. `brownfield` answers narrow questions itself; every prompt and three task skills cap code reads at 3 per answer, after the mandatory check. In an evaluation this design scored lower, because the read cap left answers unconfirmed.
- **Hook-only design** (`--agent-design hook-only`, or `"agent_design": "hook-only"`; needs a `kb` section): `brownfield` keeps the `kb-retrieve` hook and the task skills but has no `subagent/*` tools, so it answers directly from the hook's retrieved context, the reviewed rules and the code, with no sub-agent hop. The four specialists are still generated as standalone agents, so genuinely system-wide work is handed to one directly (`--agent impact-analysis`). The knowledge-base PoC measured the extra tokens and time of the specialist path as the cost of that hop, not of the knowledge base, so this is the recommended default for question-and-answer personas.
- **Knowledge-base-first design** (`--agent-design kb-first`, or `"agent_design": "kb-first"`; needs a `kb` section): like hook-only (same plumbing, the `kb-retrieve` hook, no `subagent/*` tools), but `brownfield` answers from the verified-fresh facts and their recorded evidence lines without opening the code for them; a fact's recorded evidence satisfies the mandatory check for the topic it covers. It opens the code only for a fact the hook tags `[verified-stale]` or quarantined, a point resting on unreviewed extraction, a question no fact covers, or a task that changes code. Use it when the knowledge base already holds the SME-verified facts and you want answers served from them rather than re-read from code.
