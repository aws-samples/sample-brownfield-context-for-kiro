# Brownfield context reference

This reference supports the single instruction guide in `README.md`. The guide has the steps, in order,
from unpacking the kit to teardown. This file holds the background a reader needs beside those steps:
what the solution does and does not do, the context contract Kiro follows, the configuration keys, the
IAM policies, cost, security, and the material that does not fit a numbered step. It does not repeat the
procedure. Where a step is named below, see that step in `README.md`.

`bcp` is a Python command-line tool. AWS Transform custom (the `atx` CLI) drafts the business-rules
extraction (BRE). `bcp` resolves citations against the code, runs mechanical checks, generates Kiro
steering and context, builds a code graph, and provisions and syncs an Amazon Bedrock knowledge base.
Kiro and a subject-matter expert (SME) do the judgement.

## What this solves

- Functional onboarding. Kiro answers "how does X work" from reviewed business rules per domain, with
  links into the code. A new engineer gets the domain map, the rules and each rule's review status.
- Impact analysis. Kiro answers "what breaks if I change X" from an impact map, a variant map and, when
  you export it, a per-program configuration map. The configuration map shows which programs a change to
  a configuration key affects, using values that live in database tables.
- Cross-component dependencies. `bcp deps` builds a dependency map from the code (imports, calls, shared
  tables, integration points) and compares it with the extraction's claims. An edge the extraction
  claims but the code does not show is marked as a claim.
- Documentation drift. `bcp docdrift` compares exported documents with the code. `bcp drift` compares the
  extraction and the review evidence with the current code, and `bcp drift --since-generation` lists the
  rules to re-review after a release.

## What this does not solve

- Traceability and verification. These need requirements, tickets, commits and logs. The tool does not
  read an issue tracker, a wiki, a git host or logs. MCP integration below shows how to give Kiro
  read-only access to them; that wiring is guidance, not shipped code.
- Design and code quality. This layer makes Kiro's answers about today's behaviour correct and grounded.
  Architecture review, refactoring advice and code-quality rules are a separate layer.
- Runtime facts. This is static analysis. Values in each deployment's database, behaviour in other
  repositories, and framework behaviour the code does not show still need an SME, an export or a test.
- Proof of intent. The extraction copies today's defects faithfully. A verified rule can still describe a
  bug. The SME decides what is intended.

## The context contract

Every generated file follows one contract. Kiro is told it in every agent prompt, and `bcp validate`
fails on a prompt that breaks it.

### Trust tiers

Every generated Markdown context file carries a `> Trust:` line straight after the generated marker. A
JSON file cannot carry one, so its tier is named in the agent prompts and in
`.kiro/context/trust-tiers.md`.

| Tier | What it is | Examples | How Kiro uses it |
|---|---|---|---|
| Tier 1, mechanical | Tool-derived or team-maintained | file index, citation baseline, review status, framework rules, code-derived dependency edges | Facts about the code and the review |
| Tier 2, AI narrative | Reviewed or reviewable extraction text | `reviewed-rules.md` (each rule carries its own status), `errata.md`, domain summaries, impact and variant maps, program configuration map, glossary | Orientation; check the code for anything that matters |
| Tier 3, hypothesis only | The least reliable extraction text | workflows, diagrams, cross-cutting error and integration documents, coupling hotspots | Never preloaded; used only to find code |

### Review verdicts

Every rule has one review verdict, recorded with tool-captured evidence.

| Verdict | Meaning | What Kiro sees |
|---|---|---|
| `verified` | The behaviour and every detail match the code | The rule, marked VERIFIED |
| `needs-edit` | The main behaviour is right; a detail is wrong | Only the full corrected rule, with a `> What changed:` line |
| `incorrect` | The core is wrong | The body is removed; the correction is listed under "Known BRE corrections" |
| `needs-sme` | Intent, configuration or data decides it | The rule, marked NEEDS SME, with the question |

A rule with no review is labelled UNREVIEWED. A Kiro review is labelled "AI-reviewed, not SME-signed".
Only an SME sign-off (`--reviewer-type sme`) gives "SME-signed". Sign-off decisions are `approved`,
`approved-with-exceptions` (name the exception rules) and `rejected`; a rejection returns the domain's
rules to UNREVIEWED.

### Errata

`.kiro/context/errata.md` lists every `needs-edit` and `incorrect` claim and the other extraction
passages that repeat it, marked superseded. Every agent reads it first. It is capped at 12,000 bytes;
occurrence lists shrink first, and no entry is ever dropped.

### Code-is-truth verification rules

Whatever a rule's status, Kiro reads the code before it states anything about:

- authorization, or its absence;
- transactions, rollback, commit or error end states;
- concurrency;
- absolute claims (only, every, never, always, unique, none) and counts;
- what a diagram or workflow asserts.

Kiro tags each statement `[code: path#Lx-Ly]`, `[reviewed context: <file> BR-n]` or
`[unreviewed extraction, unverified: <file>]`, and ends every answer with a `Context used:` line.

How this interacts with the knowledge base: a fact the retrieval hook tags `[verified-fresh]` is
answered from the fact and the evidence lines already recorded for it, and its recorded evidence
satisfies the mandatory check for the topic it covers. Kiro opens the code for a fact tagged
`[verified-stale]` or quarantined, for a point that rests on unreviewed or unverified extraction, for a
question no fact covers, and for any task that changes code or asks for verification. Each fact stores a
hash of the code lines it cites, and `bcp kb refresh` quarantines a fact when those lines change until an
SME reviews it again. See `README.md`, step 6 (the agent designs) and step 7 (keep it current).

## Per-program configuration

Many systems keep per-program behaviour in database tables. The tool never connects to a database.
Someone with read access exports the configuration table (key and value columns only, never business
data).

The CSV is UTF-8, with the header exactly `program_id,property,property_value` (or
`program_id,key,value`), one row per key:

```csv
program_id,property,property_value
north,feature.mode,strict
north,feature.mapping,"1:1, 2:2"
north,feature.label,
```

- An empty value means the row exists with an empty string. A missing row means absent: outputs show
  "absent: code default applies" with the default found in the code. Keep that difference in the export.
- JSON is also accepted: `{"program_id": "north", "synthetic": false, "properties": {"feature.mode": "strict"}}`,
  or a list of rows.
- Masking. `variant_mask_patterns` (default: password, passwd, secret, token, credential, private,
  apikey, api_key) replaces a matching value with `***masked***` at import, and any other value is
  clipped to 200 characters. Add patterns for keys that can hold personal data.
- SYNTHETIC label. A file named `SYNTHETIC-*.csv`, or JSON with `"synthetic": true`, is labelled
  SYNTHETIC in every output, and `program-config.md` opens with a banner. Use it for demos and tests.

Point `program_snapshots` in `bcp.config.json` at the export folder so `generate` writes
`.kiro/context/program-config.md`, or run `bcp variants` for a one-off map. The export format for the
Kiro agent to read is `brownfield-context-setup/references/program-snapshots.md` in the Power.

## Dependency map and framework rules

`bcp generate` always writes `.kiro/context/dependency-map.md` and `.json`. `bcp deps` writes the full
map anywhere. Each edge is marked code-derived or extraction-claimed; a `bre-only` edge is a Tier 3 claim
until someone checks it. Integration points come from `integration_patterns` in the config (http,
messaging, scheduling, events and file by default).

Static reading misses some framework semantics: where a transaction starts and ends, what rolls back,
where authorization is enforced, and what runs on another thread. `.kiro/context/framework-rules.md`
holds these rules, and Kiro applies them before it states an end state or an authorization fact.

- `framework_rules: "auto"` (the default) uses the Java and Spring example when the manifest lists Java,
  and a blank template otherwise. The other keywords are `java-spring` and `blank`.
- For any other stack, copy the blank template's six sections and fill them in: transaction boundaries,
  errors and rollback, authorization, indirect calls and interception, scheduled and background work, and
  persistence side effects. Each rule ends with "How to check in code". Point `framework_rules` at your
  file.

## SME effort guidance

`bcp review workbook export` produces a per-domain effort estimate from stated assumptions, not from
measured time. Calibrate the assumptions on the first pilot domain. The assumptions are:

- 6 minutes per verified spot-check;
- 12 minutes per `needs-edit` or `needs-sme` claim;
- 15 minutes per `incorrect` claim;
- 10 minutes per unreviewed claim;
- 5 minutes per forced-check topic (authorization, transactions, concurrency, counts, diagrams);
- 5 minutes per guard contradiction.

The workbook ranks every flagged claim (needs-edit, incorrect, needs-sme, unreviewed, counterexample
found) highest-risk first, and adds a spot-check sample of a quarter of the verified claims. Start the
review with absolute claims (only, every, never) and authorization claims. Integration-heavy and
transaction-heavy domains take the most time.

## Recommended additionalPlanContext for the extraction

The extraction step (`README.md`, step 1) passes an AWS Transform custom configuration whose
`additionalPlanContext` carries the grounding rules. The full text to paste into the configuration is
below. It has not been run as a single block yet, so trim it if the command line rejects it; the
character limit for the field is not published.

```text
Run decomposition mode (Mode A) for the scoped components only: <list components>. Write every output file named in the definition, for every domain.
Grounding:
- Read every cited method in full. Do not use fixed-length windows. If a planned file cannot be read, retry it. Never write rules for a file from grep fragments.
- Cite each rule as "Evidence: path#start-end" (real line numbers) and add "Confidence: high|medium|low - <reason>".
Authorization:
- Parse annotations across line breaks. Read the service interface as well as the implementation. Never call a method unguarded unless you quote the interface declaration lines.
Absolutes and counts:
- Do not write only/every/never/no/unique/all unless you ran an exhaustive repository search. Cite the search pattern and the hit count. State counts with the list they were counted from.
Transactions and errors:
- For every error-handling or persistence rule, trace exception type -> catch sites -> transaction boundary (propagation, rollback-only marking by nested calls, self-invocation, objects created with new). State the final commit or rollback outcome.
Configuration:
- Per-program behaviour may be stored in database tables. Do not describe code defaults as the behaviour. List every rule whose outcome depends on a configuration table or property, and name the table, column or key.
Duplication:
- Write each behavioural claim once, in business-rules.md. Other files reference BR ids.
Tests:
- For each service, list the tests that cover each rule, or write "no test found".
```

Notes:

- Reading methods in full and listing tests cost more agent minutes.
- A transaction trace is still static reasoning. Keep the mandatory code check and the SME review.
- The definition's own gates can assume all domains, so a run scoped to one component can report FAIL or
  write all domains anyway. A partial run is untested.
- The configuration line helps only when the repository holds seed data or schema for the configuration
  tables. Real per-program values still come from an export (see Per-program configuration above).

Paste this text into the `additionalPlanContext` field of the AWS Transform custom configuration you pass
in step 1 of `README.md`. The sample repository ships no ready-made extraction configuration, because the
configuration depends on your Region, repository path and scope; `README.md`, step 1 shows how to write
one.

## Pilot checklist

- [ ] One component chosen, with a named owner and a named SME.
- [ ] SME hours agreed per domain, using the `bcp review workbook export` estimate, plus a calibration
      pass on the first domain.
- [ ] Security approval for AWS Transform on that repository. For managed mode, a read-only git-host
      credential.
- [ ] Languages and frameworks listed. Framework rules filled in for each (see Dependency map and
      framework rules above).
- [ ] Build and test ability checked: can Kiro build the component and run its tests locally or in a
      sandbox? If not, transaction and runtime claims stay `needs-sme` or need a team-written test.
- [ ] Per-program configuration export available (configuration table only, masked).
- [ ] MCP access confirmed, read-only, for the issue tracker, the wiki and the git host.
- [ ] Baseline questions written before the pilot: 5 to 10 real onboarding, impact, dependency and
      doc-gap questions, with the answers the SME expects.
- [ ] Success measures agreed, for example: the share of answers that cite code for authorization,
      transaction and absolute claims; wrong statements per answer, with and without the context; SME
      minutes per domain against the estimate; time for a new engineer to answer the baseline questions;
      doc-drift findings the SME confirms.
- [ ] Re-run plan: who runs `bcp drift --since-generation` and `bcp generate` after each release.

## MCP integration (guidance, not shipped code)

The generated agents use local files only. When the team already runs MCP servers for its issue tracker,
wiki and git host, Kiro can read them. The full guide for the Kiro agent is
`brownfield-context-setup/references/mcp-integration.md` in the Power. In short:

- Read-only. Give the agents search and get tools only. No create, update, comment or merge.
- Least data and masking. Ask for the fields the task needs. Credentials stay in the server configuration
  and environment variables, never in steering or context files.
- Trust tiers. Git commits, diffs and issue fields are Tier 1. Issue descriptions and wiki pages are
  Tier 2. Wiki diagrams and old design pages are Tier 3. A wiki page that disagrees with the code is a
  drift finding, not a fact.
- Wiring. Add the servers to `.kiro/settings/mcp.json`. `bcp generate` owns `.kiro/agents/*.json`, so
  copy an agent to a new name and add the read tools there, or add a steering file that `bcp` does not
  own, then re-run `bcp validate`.
- Root-cause records. Record each closed defect as symptom, root cause, fix commit, affected programs and
  regression test. Kiro can then search earlier causes and check them in the code.

Keep issue-tracker and wiki content out of the knowledge base; it holds reviewed, code-backed facts only.
For an ad-hoc search in the IDE you can add the AWS Labs Bedrock knowledge base retrieval MCP server to
`.kiro/settings/mcp.json`. It lists only knowledge bases tagged `mcp-multirag-kb=true`, which both
provisioning routes set. It needs more permission than the reader policy grants; see its documentation
for the exact actions.

## Cost notes

Use the official pricing pages and the AWS Pricing Calculator for your Region. This package quotes no
prices.

- Amazon S3 (facts bucket, versions, requests): the Amazon S3 pricing page.
- Amazon S3 Vectors (vector storage, PUT and query requests): the Amazon S3 pricing page.
- Amazon Bedrock (Amazon Titan Text Embeddings V2 tokens at ingestion and for each `Retrieve` query;
  Amazon Bedrock Knowledge Bases): the Amazon Bedrock pricing page.
- No always-on compute. There is no search cluster, database or server. Cost follows the number of facts,
  re-ingestions and queries.
- What drives cost: how many facts you ingest, how often you re-ingest (each sync embeds the changed
  documents), how many prompts the hook sees, and how many object versions you keep. Add an S3 lifecycle
  rule for old versions and the `quarantine/` prefix to cap storage.
- AWS Transform extraction runs are billed separately as agent minutes.

## IAM policies explained

Replace the placeholders before you attach a policy: `<REGION>`, `<ACCOUNT_ID>`, `<NAME_PREFIX>`, and
`<KB_ID>` for the reader. The ARNs use the `aws` partition and the derived names from `README.md`,
step 5; change them if you set explicit names. Do not give engineers the deployer policy.

### Service role

The knowledge base assumes this role. `bcp kb provision` or the CloudFormation template creates it.

| Statement | Actions | Scope | Why |
|---|---|---|---|
| Trust | `sts:AssumeRole` by `bedrock.amazonaws.com` | `aws:SourceAccount` your account, `aws:SourceArn` like `arn:aws:bedrock:<REGION>:<ACCOUNT_ID>:knowledge-base/*` | Only Amazon Bedrock knowledge bases in your account use the role |
| `InvokeEmbeddingModel` | `bedrock:InvokeModel` | The embedding model ARN | Embed facts and queries |
| `ListFactsBucket` | `s3:ListBucket` | The facts bucket | Find documents to ingest |
| `ReadFacts` | `s3:GetObject` | Objects in the facts bucket | Read the documents |
| `VectorIndex` | `s3vectors:PutVectors`, `GetVectors`, `DeleteVectors`, `QueryVectors`, `GetIndex`, `ListVectors` | The vector bucket and index | Write, delete and query vectors |

### Deployer policy (`infra/iam/deployer-policy.json`)

This policy provisions, syncs, refreshes and tears down. It is the policy for the person or pipeline that
runs `bcp kb provision`, `bcp kb sync`, `bcp kb stale` and `bcp kb teardown`, and for a CloudFormation
deploy.

| Statement | What it allows | Why |
|---|---|---|
| `DiscoveryReadOnly` | `sts:GetCallerIdentity`, `bedrock:GetFoundationModel`, `GetFoundationModelAvailability`, `ListFoundationModels`, `ListKnowledgeBases`, `s3vectors:ListVectorBuckets` on `*` | Account detection and preflight. These actions do not support resource scoping |
| `CreateKnowledgeBase` | `bedrock:CreateKnowledgeBase` on `*`, limited by `aws:RequestedRegion` | The knowledge base id is not known before it exists, so create cannot be scoped to an ARN |
| `ManageKnowledgeBaseAndSync` | Get, update, delete, tag, data source CRUD, `StartIngestionJob`, `GetIngestionJob`, `ListIngestionJobs`, `Retrieve` on `knowledge-base/*` in your account and Region | Provision, status, sync, stale, teardown and the smoke test |
| `PassKbServiceRoleToBedrockOnly` | `iam:PassRole` on the service role, only to `bedrock.amazonaws.com` | Attach the role to the knowledge base, and nothing else |
| `ManageKbServiceRole` | Role create, get, delete, tags, trust policy, inline policy | Create and maintain only the service role |
| `ManageFactsBucket` | Bucket create, delete, list, versions, location, public access block, encryption, versioning, tagging, ownership controls, bucket policy, lifecycle and logging on the facts bucket | Create and harden the bucket, and tear it down |
| `WriteFactsAndGraph` | `s3:GetObject`, `GetObjectVersion`, `PutObject`, `DeleteObject`, `DeleteObjectVersion` on objects in the facts bucket | Sync facts, move stale facts to `quarantine/`, upload the graph, empty the bucket on a forced teardown |
| `ManageVectorStore` | Vector bucket and index create, get, delete, list and tag on your vector bucket and its indexes | Create the vector store, and tear it down |
| `OptionalCloudFormationRoute` | Change set, describe, get template and delete stack on `stack/<NAME_PREFIX>-validated-kb/*`, plus the `LanguageExtensions` transform | The CloudFormation route only. Remove it for the `bcp kb provision` route |
| `OptionalCloudFormationValidate` | `cloudformation:ValidateTemplate`, `GetTemplateSummary` | The CloudFormation route only |

For a sync-only pipeline, remove the `Delete*` actions and the two CloudFormation statements. A
CloudFormation deploy also needs the resource permissions above, because CloudFormation acts with the
caller's permissions unless you give it a service role. A statement on `*` appears only where the action
cannot be scoped: read-only discovery, `CreateKnowledgeBase` limited by Region, and `ValidateTemplate`.

### Reader policy (`infra/iam/reader-policy.json`)

This policy is for an engineer using Kiro. It grants only what the hook needs.

| Statement | What it allows | Why |
|---|---|---|
| `RetrieveVerifiedFacts` | `bedrock:Retrieve` on one knowledge base | The hook's only AWS call for facts |
| `ReadDependencyGraphSnapshot` | `s3:GetObject` on `graph/latest.json` in the facts bucket | Read the graph snapshot |

A reader cannot list, write or delete anything.

## Configuration reference (`infra/config/kb-config.example.json`)

Required keys: `region` and `name_prefix`. Every other key is optional, and JSON `null` means "use the
default". A key that starts with `_` is ignored, so you can use it for a note. An unknown key is
rejected, so a typo cannot fall back to a default silently. The Kiro agent reads the machine copy at
`brownfield-context-setup/references/config-reference.md` in the Power.

| Key | Default | Meaning |
|---|---|---|
| `region` | (required) | AWS Region code, for example `us-east-1` |
| `name_prefix` | (required) | 3 to 20 characters: lowercase letters and digits, single hyphens, starting with a letter |
| `kb_name` | `<prefix>-validated-kb` | Knowledge base name |
| `kb_description` | `Validated knowledge layer: reviewed facts only` | At most 200 characters |
| `embedding_model_id` | `amazon.titan-embed-text-v2:0` | Embedding model. The index is fixed at 1024 dimensions |
| `index_name` | `verified-facts` | Vector index name, 3 to 63 characters |
| `data_source_name` | `verified-facts` | Data source name |
| `inclusion_prefix` | `verified/` | Relative key prefix ending in `/` |
| `tags` | `{}` | String map applied to the bucket, vector store, role and knowledge base. Up to 40 tags; `aws:` is reserved; `mcp-multirag-kb` is set for you |
| `retain_data` | `true` | `true` keeps the facts bucket on teardown |
| `bucket_name` | `<prefix>-kb-<account>-<region>` | Explicit facts bucket name, if the derived one is taken or too long |
| `vector_bucket_name` | `<prefix>-vectors-<account>-<region>` | Explicit vector bucket name |
| `role_name` | `<prefix>-kb-role-<region>` | Explicit service role name |
| `expected_account_id` | none | 12-digit account id. Provision stops if the credentials belong to another account |
| `profile` | none | AWS named profile. Without it the default credential chain is used |
| `state_file` | `kb_state.json` | Where the state JSON is written |
| `iam_wait_seconds` | `15` | Wait after a new role is created, 0 to 120 |

The state file that `bcp kb provision` writes holds resource ids and ARNs, not secrets: `schema`,
`region`, `account`, `name_prefix`, `bucket`, `bucket_arn`, `vector_bucket`, `vector_bucket_arn`,
`index`, `index_arn`, `role_name`, `role_arn`, `kb_name`, `kb_id`, `data_source_name`, `data_source_id`,
`inclusion_prefix`, `embedding_model`, `model_check`, `retain_data` and `provisioned_at`. `bcp kb sync`,
`bcp kb stale`, `bcp kb audit` and `bcp kb check-retrieval` read `region`, `bucket`, `kb_id` and
`data_source_id` from it with `--state`; a command-line flag overrides a state value.

## Adapting to your codebase

Languages. The code graph (`bcp graph build`), the code-derived dependency edges (`bcp deps`) and the
precheck guard check read Java today. They read Java imports, packages, `extends`/`implements` and
annotations, and the graph keeps only `src/main/` Java types. Other files are indexed and their citations
resolve, but they get no edges and no guard check. To add a language, implement the language plugin
interface in `bcp.lang`. Until then, list your languages in the pilot checklist and expect fewer
mechanical checks outside Java.

Settings you can change per language in `bcp.config.json`: `source_extensions`, `guard_annotations` (the
names that count as an authorization guard), `integration_patterns`, `variant_constant_patterns`,
`variant_default_patterns` and `framework_rules`.

Per-program configuration from database tables. The tool never connects to a database. Someone with read
access exports the configuration table to CSV or JSON (key and value columns only), masked, and points
`program_snapshots` at the folder. Do not export business data.

Issue tracker and wiki. Requirements, tickets and wiki pages are Tier 2 intent: useful for "why", never
proof of behaviour. Connect them read-only through MCP servers. Do not ingest them into the knowledge
base.

## Security notes

- Least privilege. Three identities, three policies: the service role (Amazon Bedrock only, scoped to
  your account and knowledge bases), the deployer, and the reader (`Retrieve` on one knowledge base and
  one graph object). Do not give engineers the deployer policy.
- Encryption. The facts bucket uses SSE-S3 (AES256) with versioning and all public access blocked. Its
  bucket policy denies any request that is not HTTPS or that uses TLS older than 1.2, and ACLs are
  disabled. S3 Vectors encrypts at rest by default. To use a customer managed AWS KMS key, add it and
  grant the service role `kms:Decrypt` and `kms:GenerateDataKey`. The KMS variant is not built or tested.
- No customer data in facts. A fact is a reviewed business rule, a code pointer and a hash. Do not put
  production records, personal data, or configuration values that hold personal data in a fact. Masking
  for configuration exports is on by default.
- Scan before ingest. Run your secret scanner and `scripts/kit_hygiene_scan.py` over the export folder
  before each sync, and stop the sync on any finding. A fact quotes short code excerpts, and code
  sometimes holds credentials.
- Credentials. The hook and the CLI use the standard AWS credential chain (a profile, IAM Identity Center
  or a role). Never put a key in `.kiro/`, a steering file, the config or the state file.
- Logging. Turn on AWS CloudTrail for the account; management events cover create, delete and ingestion
  calls.

## Known limits

These add detail to the "Known limits" section of `README.md`; they are not repeated there.

- A fact's metadata sidecar is at most 1,024 bytes (the Amazon Bedrock limit). The export trims the
  evidence list to fit and records what it trimmed.
- S3 Vectors index settings are fixed. You cannot change the dimension, distance metric or non-filterable
  keys of an index. A different embedding model with another dimension needs a new index.
- A large domain can exceed the advisory 50,000-byte per-domain steering warning (`domain_budget_bytes`,
  a `bcp` heuristic, not a Kiro limit).
- Each agent's startup files stay within the advisory `agent_budget_bytes` (a `bcp` heuristic, default
  81,920) and Kiro's documented `file://` caps. Files that do not fit are named in the prompt as
  read-on-demand.
- `bcp drift --since-generation` needs `--repo` and the `generate --target` at the git repository root.
  In a subfolder of a larger repository it reports no changes and gives no warning.

## Troubleshooting (`bcp kb provision` route)

These messages come from the `bcp kb provision` route. For the CloudFormation route and the real-run
symptoms, see "Troubleshooting" in `README.md`.

| Message starts with | Cause | Fix |
|---|---|---|
| `kb config:` | A key is missing, unknown or invalid | Fix the key the message names |
| `credentials belong to account` | Wrong profile or `expected_account_id` | Switch profile or fix the config |
| `embedding model ...: not available` / `no entitlement` / `not authorised` | Model not offered in the Region, access not enabled, or an IAM or SCP policy denies it | Enable model access in the Amazon Bedrock console for that Region, or pick another Region |
| `...: no endpoint in this region` / `not available in` | S3 Vectors or Amazon Bedrock Knowledge Bases not in the Region | Pick a supported Region |
| `...: access denied` | Missing deployer permission | Attach the deployer policy with your values |
| `bucket name ... is taken globally` | S3 bucket names are global | Set `bucket_name` |
| `existing index ... cannot be reused` | An index with the same name has other settings | Tear it down, or set another `index_name` |
| `bucket ... is not empty; nothing was deleted` | Teardown with `retain_data: false` on a bucket that holds data | Back it up, then use `--force`, or set `retain_data: true` |

## What is supported today and how to extend it

This section describes only what the code does today. Every statement is cited to a file and function so
you can check each claim in `src/bcp`. No new language support is built here; this is the map for adding
it.

### What works today: Java

The tool reads Java source. Three parts of the pipeline look at code, and all three assume Java:

1. **Extraction (the draft business rules).** The draft rules come from AWS Transform custom
   (transformation `AWS/business-rules-extraction`), run outside this tool. The tool consumes that
   extraction as a directory of Markdown and JSON (see the `bre-mini` fixture under
   `tests/fixtures/bre-mini/` for the expected shape). The extraction the solution was validated against
   was produced from a Java codebase, and the drafted rules cite Java symbols (`Class.method`, package
   paths, `path/to/File.java#L12-L20`). The tool does not run the extraction itself.

2. **The class-level code graph (`graph.json`).** Built by `bcp.graph.build_graph`
   (`src/bcp/graph.py`). This is Java-only by construction:
   - `graph.py` imports the masker and import regex from the Java plugin directly:
     `from bcp.lang.java import IMPORT_RE, mask` (`graph.py` line 39).
   - `is_main_java(rel, main_dirs)` (`graph.py` line 64) returns `False` for any path that does not end
     in `.java`, and otherwise selects files under `main_source_dirs` (or a `/src/main/` folder).
     `build_graph` builds its node set from this (`main = {p for p in index.paths if is_main_java(...)}`)
     and raises `BcpInputError("no main-source Java file is mapped to ...")` when none match.
   - `class_edges` (`graph.py` line 117), `_decl` (line 85) and `_heritage` (line 94) read Java types,
     `extends`/`implements` clauses, and `import` lines through the Java masker.
   - Table edges come from JPA annotations (`_annotation_tables`, `graph.py` line 237) and Hibernate
     `.hbm.xml` mapping files (`_hbm_tables`, line 213), both Java and JPA specific.
   - The dependency model behind the graph, `bcp.deps.TypeTable` (`src/bcp/deps.py` line 55), skips every
     file that is not `.java` (`deps.py` line 63) and resolves references by Java package and import
     (`TypeTable.resolve_import`, `deps.py` line 75; `file_references`, line 88).

3. **The mechanical checks (rule citations against code).** These live in `src/bcp/precheck.py` and
   `src/bcp/sourceindex.py`. They split into a language-neutral layer and a Java-only layer:
   - **Language-neutral (works for any extension through regex heuristics):** `SourceIndex`
     (`sourceindex.py`) walks the repo by file extension, indexes files by stem and name, and resolves a
     cited token to `resolved`, `method-missing` or `unresolved` using `SourceIndex.find_class`,
     `SourceIndex.find_method_line` (a regex `(?<![.\w])method\s*\(`), and the declaration and type
     heuristics `declaration_name` and `type_name` (`sourceindex.py`). Those heuristics already carry
     patterns for several languages (`_DECL_RES` and `_TYPE_RE` match Java, C# and Kotlin, Python `def`,
     JavaScript and TypeScript `function`, Go `func` and `type`, Kotlin `fun`, Perl `sub`). Basic
     "does this cited symbol exist in the code" resolution is not Java-locked.
   - **Java-only (deep structure through the language plugin):** `precheck.method_citations`
     (`precheck.py` line 383) calls `bcp.lang.for_path(path)` and uses `method_decls` and `method_range`;
     `precheck.guard_check` (line 395) uses `annotations_before` and `class_annotations`; and
     `precheck._related_types` (line 357) uses `implemented_types`. Only the Java plugin returns real
     annotations, implemented types and brace-matched method ranges. For a non-`.java` file, `for_path`
     returns the generic plugin, whose `annotations_before`, `class_annotations` and `implemented_types`
     all return `[]` (`src/bcp/lang/generic.py`), so the guard-annotation contradiction check is Java-only
     in practice.

Everything else in the tool (the review workflow, the SME workbook, the knowledge-base provisioning and
sync, the kb-retrieve hook, the keep-current and quarantine logic) operates on the reviewed rules and the
`graph.json` artifact, not on source code directly, and is language-independent.

### Where a new language reader plugs in

The plugin seam is `src/bcp/lang/`:

- `src/bcp/lang/__init__.py` defines the `LanguageSupport` Protocol (the six methods a plugin must
  provide), the `Annotation` dataclass, and `for_path(rel)`, which currently returns the Java plugin for
  `.java` and the generic plugin otherwise.
- `src/bcp/lang/java.py` is the reference implementation: `JavaSupport` plus the module-level singleton
  `JAVA` and the `mask()` helper (blanks comments and string, char and text-block contents so braces and
  parentheses inside them do not count).
- `src/bcp/lang/generic.py` is the fallback: `GenericSupport` and `GENERIC`, which only knows the
  declaration line.

To add a language `<lang>`:

1. **Add `src/bcp/lang/<lang>.py`** implementing the `LanguageSupport` Protocol and exposing a
   module-level singleton (mirror `JAVA = JavaSupport()`).
2. **Register it in `for_path`** in `src/bcp/lang/__init__.py` (today this is a single
   `return JAVA if rel.lower().endswith(".java") else GENERIC`; extend it to select your plugin by
   extension).
3. **For graph support, generalize the Java assumptions** in `src/bcp/graph.py` (`is_main_java`, the
   `from bcp.lang.java import IMPORT_RE, mask` import, `class_edges`, `_decl`, `_heritage`, and the
   JPA and Hibernate table extraction) and in `src/bcp/deps.py` (`TypeTable`, which skips non-`.java`
   files, and `resolve_import` and `file_references`, which assume a Java package and import model). The
   mechanical citation checks need only the plugin from step 1; the deep graph needs this step too.

### What a new language reader must provide

The plugin interface is the `LanguageSupport` Protocol in `src/bcp/lang/__init__.py`. Input to every
method is `lines: list[str]`, the file content split on newlines with no line endings, exactly what
`SourceIndex.lines(rel)` returns (`sourceindex.py`). Line numbers are 1-based. The methods and their
outputs:

| Method | Returns | Meaning |
| --- | --- | --- |
| `method_decls(lines, method)` | `list[int]` | 1-based declaration lines of `method` (all overloads) |
| `method_range(lines, decl_line)` | `tuple[int, int]` | 1-based inclusive `(start, end)` of the method body (or the `;` of an abstract or interface member) |
| `annotations_before(lines, decl_line)` | `list[Annotation]` | annotations attached to the declaration (above it or inline before it) |
| `class_annotations(lines)` | `list[Annotation]` | annotations on the first top-level type in the file |
| `implemented_types(lines)` | `list[str]` | simple names in the first type's `extends` and `implements` clauses |
| `imports(lines)` | `list[str]` | imported names |

`Annotation` (defined in `src/bcp/lang/__init__.py`) is a frozen dataclass with `start` and `end` (1-based
line span), `text` (the joined annotation text) and `name` (the simple annotation name). A plugin that
cannot provide a given fact returns `[]` (the generic plugin does this), which disables only the checks
that depend on it, not the whole pipeline.

Two inputs are driven by config (`src/bcp/config.py`), not by the plugin, and a new language usually
needs them set:

- `source_extensions` (default list at `config.py` line 17 includes `.java`, `.kt`, `.cs`, `.py`, `.ts`,
  `.js`, `.go`, `.rb`, `.scala`, `.groovy` and more) controls which files `SourceIndex` discovers.
- `main_source_dirs` (`config.py` line 81) and `guard_annotations` (`config.py` line 84, default Spring
  Security annotation names) feed the graph's main-file selection and the guard-annotation check.

### How to test a new reader

- **Unit-test the plugin** by copying `tests/test_lang_java.py` as the pattern. It tests each plugin
  method against fixtures and includes `test_for_path`, `test_generic_plugin` and masking tests. Use the
  Java fixtures under `tests/fixtures/repo-mini/src/main/java/x/` (for example `GuardedApi.java` and
  `GuardedApiImpl.java`) as the model, and add equivalent fixtures in your language.
- **Test citation resolution** by copying `tests/test_sourceindex.py` and `tests/test_precheck.py`, which
  exercise `SourceIndex.resolve`, `method_citations` and `guard_check` end to end against the
  `repo-mini` fixtures and a `bre-mini` extraction (`tests/fixtures/bre-mini/`).
- **Test the graph** by copying `tests/test_graph.py` once you have generalized `graph.py` and `deps.py`;
  it builds a graph from the `repo-mini` fixtures and asserts nodes and edges.
- Run everything the same way this repo is tested: from the repo root, with `src` on the path
  (`pyproject.toml` sets `pythonpath = ["src"]`), `python -m pytest`.

### Suggested order of work

1. Write `src/bcp/lang/<lang>.py` and its unit tests (copy `tests/test_lang_java.py`); register it in
   `for_path`. This alone makes the mechanical citation checks (`method_decls`, `method_range`, and, if
   your plugin returns annotations, `guard_check`) work for the new language, because `SourceIndex` and
   the declaration heuristics are already language-neutral.
2. Add or confirm `source_extensions`, `main_source_dirs` and `guard_annotations` for the language
   (`config.py`), and verify `SourceIndex` discovers the files and resolves cited symbols
   (`tests/test_sourceindex.py` as the pattern).
3. Generalize the graph: replace the Java-only pieces in `graph.py` and `deps.py` with your language's
   module and import model and, if relevant, its data-mapping convention (the Java version uses JPA
   annotations and Hibernate `.hbm.xml`). Cover it with a copy of `tests/test_graph.py`.
4. Produce a business-rules extraction for the language (through AWS Transform custom or another
   extractor) in the directory format the tool expects (`tests/fixtures/bre-mini/` shows the shape), then
   run the full flow (`bcp precheck`, review, `bcp graph build`, knowledge-base sync) against a small
   real codebase.
