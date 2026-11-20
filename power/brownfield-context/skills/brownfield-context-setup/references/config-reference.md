# bcp.config.json reference

All keys are optional. Unknown keys produce a warning. An invalid value stops the run (exit 2) and names the key.

| Key | Type | Default | Validation |
|---|---|---|---|
| `project_name` | string ≤ 80 | `manifest.project.name` | non-empty after strip |
| `source_extensions` | list of strings | `.java .kt .cs .py .ts .js .go .rb .scala .groovy .xml .sql .properties .yml .yaml` | each starts with `.` |
| `exclude_dirs` | list of strings | `.git .kiro node_modules target build dist bin obj .venv business-rules-extraction` | names only, no `/` |
| `variant_inventory_globs` | list of strings | `*feature-flag*`, `*config*inventory*`, `*global-propert*` | fnmatch patterns on BRE file names |
| `variant_key_patterns` | list of regexes | `[]` | each compiles; group 1 (if any) is the key |
| `glossary_extra` | path | none | relative to the config file; missing file → warning |
| `always_budget_bytes` | int | 16000 | 2000–100000; advisory bcp heuristic (WARN only), not a Kiro limit. See "Sizes and limits" |
| `domain_budget_bytes` | int | 50000 | 5000–500000; advisory bcp heuristic (WARN only), not a Kiro limit |
| `agent_budget_bytes` | int | 81920 | 10000–1000000; advisory bcp heuristic (WARN only), not a Kiro limit: startup bytes per custom agent (file:// + skill:// + prompt). Kiro's documented file:// caps are checked separately |
| `agent_domains` | list of strings | every domain, in reading order | domain slugs whose files the specialists load, most relevant first; unknown slugs → warning |
| `agent_design` | string | `hybrid` | `hybrid` (the entry agent answers narrow questions itself, delegates broad ones), `delegate`, `narrow-capped`, `hook-only`, or `kb-first` (opt-in, see output-layout.md). `hook-only` requires a `kb` section: the entry agent answers directly from the `kb-retrieve` hook's retrieved context with no sub-agent hop. `kb-first` also requires a `kb` section: the entry agent answers from the verified-fresh facts and their recorded evidence lines without opening the code for them, opening code only for stale or unverified facts, questions no fact covers, or a task that changes code. The specialists stay callable directly. `generate --agent-design` overrides it |
| `fast_entry_agent` | bool | `false` | also write the `brownfield-fast` entry agent; only with `agent_design` `delegate`; `generate --fast-entry-agent` sets it |
| `framework_rules` | string | `auto` | `auto` (the Java/Spring example when the manifest lists Java, else the blank template), `java-spring`, `blank`, or a path to the team's own file (relative to the config file; must exist). Written to `.kiro/context/framework-rules.md` |
| `guard_annotations` | list of strings | `PreAuthorize PostAuthorize PreFilter PostFilter Secured RolesAllowed Authorized` | annotation names `bcp precheck` treats as authorization guards; a leading `@` is dropped |
| `program_snapshots` | list of paths | `[]` | CSV or JSON exports of the per-program configuration table, files or folders, relative to the config file; each must exist. When set, `generate` writes `.kiro/context/program-config.md` and `.json`. Format: `references/program-snapshots.md` |
| `variant_constant_patterns` | list of regexes | `\b([A-Z][A-Z0-9_]*)\s*=\s*"([A-Za-z][\w.\-]+)"` | 2 groups: constant name, literal key. Maps keys to the constants code reads them through |
| `variant_default_patterns` | list of regexes | `new …Propert…(KEY, "default"` and `get…Propert…(KEY, default)` | named groups `key` and `default`; earlier patterns win (a declared seed default before a getter fallback) |
| `variant_mask_patterns` | list of regexes | `(?i)password\|passwd\|secret\|token\|credential\|private\|apikey\|api_key` | matched against the key; matching values show as `***masked***` in every output |
| `integration_patterns` | object label → regex | http, messaging, scheduling, events, file | integration points listed per domain in `dependency-map.md` |
| `main_source_dirs` | list of strings | derived from the manifest | repo-relative, forward slashes, no `..` |
| `kb` | object | none | optional verified knowledge base. Required: `kb_id`, `region`, `graph` (path to `graph.json` from `bcp graph build`, relative to the workspace). Optional: `python` (an interpreter with boto3; default this interpreter when it has boto3, else `C:\Program Files\Python313\python.exe`), `top_k` (1–10, default 5), `timeout_s` (1–25, default 12), `hops` (1–3, default 2). When set, `generate` adds the `kb-retrieve` UserPromptSubmit hook (`bcp kb hook`) to the `brownfield` agent, adds a "retrieved context first" step to its prompt, and writes `.kiro/context/kb-hook.json` and the IDE variant `.kiro/hooks/kb-retrieve.json` |
| `rule_heading_pattern` | regex | `^##\s+(BR-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)\s*[:.—-]?\s*(.*)$` | compiles, 2 groups |
| `source_line_pattern` | regex | `^\s*[-*]\s*\**(?:Source\|Evidence)\**\s*:?\**\s*(.+)$` | compiles, 1 group |

## Sizes and limits

Each number bcp checks is either a Kiro-documented limit (with its kiro.dev page), a measured value, or a configurable bcp heuristic. Kiro publishes no size limit or recommended size for steering ([steering](https://kiro.dev/docs/steering/), [context](https://kiro.dev/docs/cli/chat/context/)), so bcp's steering budgets are heuristics and only warn.

| Limit | Value | Source |
|---|---|---|
| Custom agent `file://` files | at most 32; later files are not loaded | Kiro limit (CLI 3.1.0+, IDE 1.4.86+): [agent configuration reference](https://kiro.dev/docs/custom-agents/configuration-reference/). `validate` ERROR |
| Custom agent `file://` total | at most 100,000 characters; later files are not loaded | Kiro limit: same page. `validate` ERROR |
| One `file://` file | over 50,000 characters is truncated; over 1 MiB is skipped | Kiro limit: same page. `validate` ERROR; `generate` never preloads such a file (it becomes read-on-demand) |
| `auto` steering | needs `name` and `description`; `fileMatchPattern` is a string or a list | Kiro rule: [steering](https://kiro.dev/docs/steering/). `validate` ERROR |
| Context files | at most 75% of the model's context window | Kiro limit: [context](https://kiro.dev/docs/cli/chat/context/). Not checked |
| Hook `timeout` | seconds, default 60 | Kiro: [hooks](https://kiro.dev/docs/hooks/). bcp writes 30 |
| MCP `timeout` | milliseconds | Kiro: [agent configuration reference](https://kiro.dev/docs/custom-agents/configuration-reference/) |
| Hook stdout summary | 2,500 bytes | Measured: Kiro CLI tests cut UserPromptSubmit stdout at about 3,000 bytes. kiro.dev states no limit for that trigger ([hook actions](https://kiro.dev/docs/hooks/actions/)); the documented 3,000-character cap is for the CLI PreCompact hook only ([hook types](https://kiro.dev/docs/hooks/types/)) |
| Knowledge base `Retrieve` budget | 12 s (`kb.timeout_s`, 1–25) | bcp heuristic: leaves headroom inside the 30 s hook timeout |
| `always_budget_bytes` | 16,000 bytes | bcp heuristic: keeps every prompt's always-on overhead small. Over it, `validate` and `generate` warn and recommend moving reference material out of `always` |
| Always file looks like reference material | more than 20 table rows, or `#[[file:]]` references expanding past 4,000 bytes | bcp heuristic. `validate` WARN, recommending `auto` for detailed reference material, `fileMatch` for file-specific guidance, `manual` for occasional use, or a skill, as the [steering docs](https://kiro.dev/docs/steering/) advise |
| `domain_budget_bytes` | 50,000 bytes | bcp heuristic for `auto`/`fileMatch` steering and `reviewed-rules.md`; it matches the 50,000-character truncation for files an agent loads via `file://` |
| `agent_budget_bytes` | 81,920 bytes | bcp heuristic. Bytes are never fewer than characters, so a total within it also stays within Kiro's 100,000-character cap. It counts the full SKILL.md, although Kiro loads only a skill's name and description at startup ([skills](https://kiro.dev/docs/skills/)) |
| `errata.md` | 16,000 bytes | bcp heuristic: every agent reads it first. `validate` WARN |

Steering in custom agents: the [steering docs](https://kiro.dev/docs/steering/) say custom agents do not get steering automatically, while the [agent configuration reference](https://kiro.dev/docs/custom-agents/configuration-reference/) says they inherit it unless `chat.disableInheritingDefaultResources` is true. Every generated agent therefore lists `product.md`, `tech.md`, `structure.md` and `bre-context-index.md` explicitly in `resources`, which works under both behaviours. The context index is `auto` inclusion (it no longer loads in every session, which keeps the always-on overhead to core orientation only, as the [steering docs](https://kiro.dev/docs/steering/) advise), but it is pinned into every agent via `file://` so the entry agent always has the domain directory. These pinned files are never dropped and count against the caps above.

Example:

```json
{
  "project_name": "Example Back Office",
  "source_extensions": [".java", ".xml", ".sql", ".properties"],
  "variant_key_patterns": ["\\b(PROGRAM_[A-Z_]+)\\b"],
  "glossary_extra": "team-terms.md"
}
```
