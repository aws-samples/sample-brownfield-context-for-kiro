# Brownfield context for Kiro

Make a legacy brownfield codebase **AI-ready** for [Kiro](https://kiro.dev/): build a reviewed, cited
context of the system once, keep it current as the code changes, and give Kiro the most relevant verified
facts with every prompt.

In a large legacy application, Kiro sees the files in the current session, not the whole system. Questions
that span the application, such as how a business flow works end to end, which components and
configuration a change affects, where a defect most likely starts, or whether a requirement already exists,
depend on rules and dependencies in files Kiro has not opened. Those answers can be incomplete or wrong.
This sample gives Kiro reviewed context for the entire application, so its answers are traceable and
verified: each one cites the rule ID, its review status and the source file and line, and the team can check
it before acting on it.

Think of a senior engineer. Steering is how they work: the standards and habits they bring to every task.
The knowledge base is the ten years of system knowledge in their head. Today Kiro has the habits but not
the memory. This sample gives it the memory.

The approach is also frugal. The expensive work, reading the code and extracting the rules, runs once per
module and is reviewed once. After that, developers and Kiro agents read the same summarized, vectorized
facts, so neither has to go through the whole codebase for every question. When the code changes,
`bcp kb refresh` re-anchors or quarantines only the facts whose cited code changed.

This sample provides the `bcp` command-line tool, a Kiro Power with three skills, AWS CloudFormation and
AWS Identity and Access Management (IAM) templates for an Amazon Bedrock knowledge base, and the scripts and
tests to run it in your own AWS account. [AWS Transform custom](https://docs.aws.amazon.com/transform/latest/userguide/custom.html)
drafts the business rules from your code, `bcp` checks every rule against the code, a Kiro review and a
subject-matter expert (SME) review the rules, and only reviewed rules become facts. Each fact carries an ID,
a review status and a source file and line, so every answer Kiro gives can be traced back to the code.

The approach puts into practice the Kiro frontier engineering practice
[Build your codebase for agents](https://kiro.dev/topics/frontier-engineering/build-for-agents/): an agent
onboards every session, so give it explicit context, and prepare a large legacy codebase one module at a
time.

---

## Architecture

![Flow from a legacy Java codebase through AWS Transform custom extraction, mechanical checks, a Kiro review and an SME sign-off gate, into an Amazon Bedrock knowledge base and a code graph, then retrieved by a Kiro hook before each prompt, with a dashed keep-current loop back to the review gate.](docs/images/architecture.png)

AWS Transform custom extracts draft business rules from the Java code. `bcp` resolves each rule's
citations on disk, Kiro reviews the rules against the cited code, and an SME signs them off. Reviewed rules
become Kiro steering and context, and verified facts in an Amazon Bedrock knowledge base (Amazon Titan Text
Embeddings V2, Amazon Simple Storage Service (Amazon S3) Vectors). `bcp` also builds a class-level code
graph. Before each prompt, a Kiro hook retrieves the top verified facts and the code-graph neighbourhood.
When the code changes, `bcp kb refresh` re-anchors facts whose lines only moved and quarantines facts whose
cited code changed until they are reviewed again.

---

## How it works

A build-once, retrieve-every-prompt workflow with a human gate:

```text
┌─────────────┐   ┌──────────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
│ Step 1      │──▶│ Step 2: review       │──▶│ Steps 3-5: build │──▶│ Step 6: retrieve per │
│ AWS         │   │ mechanical checks,   │   │ Kiro context,    │   │ prompt (kb-retrieve  │
│ Transform   │   │ Kiro review, SME     │   │ code graph,      │   │ hook): facts + graph │
│ extracts    │   │ sign-off (the gate)  │   │ knowledge base   │   │ slice, cited answers │
└─────────────┘   └──────────────────────┘   └──────────────────┘   └──────────────────────┘
                            ▲                                                   │
                            └──────── Step 7: keep current (bcp kb refresh) ◀───┘
```

1. **Extract.** The AWS-managed `AWS/business-rules-extraction` transformation statically analyzes the
   code and drafts business rules per domain. It never changes source files.
2. **Review and sign off.** `bcp precheck` runs deterministic checks, `bcp review prepare` resolves each
   rule's citations on disk, Kiro reviews each rule against the cited code, and an SME signs off. Only
   reviewed rules pass the gate.
3. **Generate.** `bcp generate` writes the Kiro steering, agents, skills and maps under `.kiro/`.
4. **Graph.** `bcp graph build` writes a class-level code graph of the chosen domains.
5. **Knowledge base.** CloudFormation provisions the knowledge base; `bcp kb sync` ingests the verified
   facts.
6. **Retrieve.** The `kb-retrieve` hook puts the top facts and a graph slice in front of every prompt.
7. **Keep current.** `bcp kb refresh` re-anchors or quarantines facts when the code changes.

Steps 1 to 4 give working context on their own, including the local code graph. Steps 5 to 7 add the
knowledge base. What stays manual: a person decides what a rule means and signs it off, a person reads the
generated steering like code, and the code stays the source of truth.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| AWS account and Region | Amazon Bedrock Knowledge Bases, Amazon S3 Vectors and Amazon Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`) available in the Region |
| IAM for the deployer | `infra/iam/deployer-policy.json` with your values (see [the reference](docs/reference.md), "IAM policies explained") |
| IAM for engineers | `infra/iam/reader-policy.json`: `bedrock:Retrieve` on one knowledge base and `s3:GetObject` on one graph object |
| AWS Transform custom | Access to `AWS/business-rules-extraction`, the `atx` command line, and security approval to run the extraction on the repository |
| Kiro | Kiro IDE, or Kiro CLI with V3 (`kiro-cli chat --v3`). Powers, hooks and `auto` or `fileMatch` steering need one of them |
| Python | 3.12 or later |
| Git | The target codebase is a git working tree (the extraction refuses a non-git target; `bcp drift` reads history) |

Verify your credentials and tools:

```bash
aws sts get-caller-identity
atx custom def list
kiro-cli --version
python3 --version          # Windows: py -3 --version
```

> **Note:** write every JSON or configuration file as UTF-8 without a byte-order mark (BOM). Windows
> PowerShell 5.1 `Out-File` and `Set-Content -Encoding UTF8` add a BOM; use PowerShell 7 with
> `-Encoding utf8NoBOM`. Never put credentials in `.kiro/`, a steering file, the configuration or the state
> file; use the standard AWS credential chain. A fact holds a reviewed rule, a code pointer and a hash, so
> do not put production records or personal data in one.

---

## Quick start

The commands below are for Linux and macOS. Each step has a Windows (PowerShell) version you can expand.

### Install and run the unit tests

```bash
git clone <your-fork-or-repo-url> brownfield-context-for-kiro
cd brownfield-context-for-kiro
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
python -m pip install -e .
python -m pytest -q -p no:cacheprovider
```

<details><summary>Windows (PowerShell)</summary>

```powershell
git clone <your-fork-or-repo-url> brownfield-context-for-kiro
cd brownfield-context-for-kiro
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
python -m pip install -e .
python -m pytest -q -p no:cacheprovider
```

</details>

**Check it worked:** the suite ends with all tests passed and one skipped integration test (it needs
`BCP_INTEGRATION_*` variables). `requirements-lock.txt` pins every package exactly as tested.

### Build and install the Kiro Power

```bash
python tools/build_power.py --out dist
```

**Check it worked:** the build prints `check-power: PASS (3 skills: bre-doc-drift-check, bre-review,
brownfield-context-setup)` and writes `dist/brownfield-context/` and a zip. Install the built
`dist/brownfield-context` folder from the Kiro Powers panel, or use the fallback installer. See
[power/brownfield-context/README.md](power/brownfield-context/README.md).

### Set shell variables

Set these once per shell. `<prefix>` is 3 to 20 characters: lowercase letters and digits, single hyphens,
starting with a letter. Pick a prefix no one else in the account uses.

```bash
REPO=/src/my-app
BRE="$REPO/business-rules-extraction"
REVIEW="$REPO/.kiro/bre-review"
FACTS=/work/kb-facts
KBCFG=/work/kb-config.json
STATE=/work/kb_state.json
REGION=us-east-1
PREFIX=myteam
```

<details><summary>Windows (PowerShell)</summary>

```powershell
$Repo   = "C:\src\my-app"
$Bre    = "$Repo\business-rules-extraction"
$Review = "$Repo\.kiro\bre-review"
$Facts  = "C:\work\kb-facts"
$KbCfg  = "C:\work\kb-config.json"
$State  = "C:\work\kb_state.json"
$Region = "us-east-1"
$Prefix = "myteam"
```

</details>

**Check it worked:** `bcp --help` prints the command list.

### Step 1: extract draft business rules

Run the extraction on a stable host, not a laptop that sleeps or changes networks. Write an extraction
configuration whose `additionalPlanContext` carries the grounding rules; the full text to paste is in
[the reference](docs/reference.md), section "Recommended additionalPlanContext for the extraction". Pass
the configuration as a `file://` URL; a bare or absolute path is rejected.

```bash
cd "$REPO"
atx custom def exec -n AWS/business-rules-extraction -p . -x -t --limit 6000 -g "file://$REPO/atx-bre-config.json"
```

<details><summary>Windows (PowerShell)</summary>

```powershell
# a file:/// URL with forward slashes
atx custom def exec -n AWS/business-rules-extraction -p . -x -t --limit 6000 -g "file:///C:/src/my-app/atx-bre-config.json"
```

</details>

`-x` runs non-interactively, `-t` trusts the tools, and `--limit` is the agent-minute budget (set it high;
a run that hits the limit stops and resumes). Run time scales with code size:
<!-- demo:start -->on the public OpenMRS Core example (see [Validated results](#validated-results)), a scope
of one package extracted in about 36 to 42 minutes.<!-- demo:end --> A whole large repository can run for
hours. Options are in the
[AWS Transform custom command reference](https://docs.aws.amazon.com/transform/latest/userguide/custom-command-reference.html).

**Check it worked:** the `business-rules-extraction/` folder contains
`domain-decomposition-overview/domain-manifest.json` and
`detailed-domain-bre/domain-<slug>/business-rules.md`.

### Step 2: review and sign off

A person is required for the SME sign-off. Start with the domains you are about to change. `<slug-a>` is a
domain slug from the manifest.

```bash
bcp generate --bre "$BRE" --target "$REPO"                                 # first context, all UNREVIEWED
bcp precheck --bre "$BRE" --repo "$REPO" --out "$REVIEW" --domains <slug-a>
bcp review prepare --bre "$BRE" --repo "$REPO" --out "$REVIEW" --domains <slug-a>
```

<details><summary>Windows (PowerShell)</summary>

```powershell
bcp generate --bre $Bre --target $Repo
bcp precheck --bre $Bre --repo $Repo --out $Review --domains <slug-a>
bcp review prepare --bre $Bre --repo $Repo --out $Review --domains <slug-a>
```

</details>

1. **Mechanical pre-check** (`bcp precheck`, no AI): absolute words used without a search, guard annotations
   that contradict an "unguarded" claim, missing or unresolved citations, and repeated claims. It writes
   `<review>/precheck/`. The guard check is Java-only.
2. **Citation check** (`bcp review prepare`): each claim's cited file, class and method is resolved on disk
   as `verified-citation`, `needs-sme` (with a reason and candidate `file#Lnn` links) or `no-citation`. It
   reads citation lines that start with `Source:` or `Evidence:`; set `source_line_pattern` in
   `bcp.config.json` if your extraction labels them differently. Resolving a citation is not a review.
3. **Kiro first pass:** in Kiro, ask "Use the bre-review skill to review domain `<slug>`." Kiro reads the
   cited code and records each verdict with `bcp review record` (`verified`, `needs-edit`, `incorrect` or
   `needs-sme`). The Kiro pass is AI review, not SME approval.
4. **SME workbook and sign-off:** export a risk-ranked workbook, let the SME fill in the decisions, import
   them, then sign off:

   ```bash
   bcp review workbook export --out "$REVIEW" --domains <slug-a> --format csv
   # the SME fills in the sme_* columns, then:
   bcp review workbook import --out "$REVIEW" --file "$REVIEW/sme-workbook.csv" --dry-run
   bcp review workbook import --out "$REVIEW" --file "$REVIEW/sme-workbook.csv"
   bcp review render --out "$REVIEW" --repo "$REPO"
   bcp review signoff --out "$REVIEW" --domain <slug-a> --reviewer "<name>" --role "<role>" \
       --reviewer-type sme --decision approved --checklist-file "$REVIEW/signoff-checklist-<slug-a>.md"
   ```

`--reviewer-type` is one of `sme`, `kiro`, or `kiro (AI stand-in for SME)`. Only `sme` produces SME-signed
rules. For a demo or test run with no human SME, use `--reviewer-type "kiro (AI stand-in for SME)"` so no
record claims a human signed.

**Check it worked:** `bcp review prepare` ends with a `citation check: verified-citation <n>, needs-sme <n>,
no-citation <n>` line; `bcp review render` writes `review-report-<slug>.md` and
`signoff-checklist-<slug>.md`; and `bcp review signoff` writes the decision into `<review>/signoff.json`.

### Step 3: generate and validate the Kiro context

```bash
bcp generate --bre "$BRE" --target "$REPO" --review-dir "$REVIEW" --dry-run   # plan
bcp generate --bre "$BRE" --target "$REPO" --review-dir "$REVIEW"
bcp validate --target "$REPO"
```

`generate` writes only under `<repo>/.kiro/`, never changes source files and never runs git. Re-run both
after each review session or new extraction; a hand-edited generated file is kept unless you pass `--force`.
Review the generated steering like code.

**Check it worked:** `bcp validate` ends with `0 errors`. A WARN line (for example a domain over the
advisory size) is informational.

### Step 4: build the code graph

The graph is a class-level map of the chosen domains and their neighbours: classes, tables, configuration
keys and endpoints, with typed edges and the commit it was built at.

```bash
bcp graph build --repo "$REPO" --domains <slug-a>,<slug-b>
bcp graph query --graph "$REPO/.kiro/context/graph.json" node "<ClassName>"
```

`--hops N` keeps classes within N dependency hops of a chosen class (default 3). `graph query` takes
`dependents`, `dependencies`, `path`, `affected-programs` or `node` and prints JSON. The graph is Java-only
today; on another language, skip it and the hook works from facts alone.

**Check it worked:** `.kiro/context/graph.json` exists, and `bcp graph query ... node "<ClassName>"` prints
a JSON object with the node and its edges.

### Step 5: provision the knowledge base

The knowledge base keeps only reviewed facts. Use the AWS CloudFormation route; it is the route this
repository verifies end to end. (`bcp kb provision` is an alternative that adds a model-access preflight;
see [the reference](docs/reference.md), "Configuration reference". Use one route per environment.)

```bash
python scripts/cfn_stack.py deploy --stack-name "$PREFIX-validated-kb" --region "$REGION" \
  --param NamePrefix="$PREFIX" --param Owner=platform-team --param RetainData=true --tag cost-center=replace-me
python scripts/cfn_stack.py state --stack-name "$PREFIX-validated-kb" --region "$REGION" --out "$STATE"
```

The template uses the `AWS::LanguageExtensions` transform, so a deploy needs `CAPABILITY_NAMED_IAM` and
`CAPABILITY_AUTO_EXPAND`; `cfn_stack.py` sets them. Parameters are `NamePrefix` (required), `Owner`
(required), `Project`, `RetainData` (default `true`: the facts bucket survives a stack delete),
`EmbeddingModelId`, `IndexName`, `InclusionPrefix` and `AccessLogBucketName`.

**Check it worked:** the deploy ends with `CREATE_COMPLETE` and `cfn_stack.py state` writes the state file
with a `kb_id` and a `bucket`.

Run the smoke test right after provisioning, before any real facts go in. It uses two synthetic rules and
proves the full gate and quarantine loop (see [Example output](#example-output)):

```bash
python scripts/smoke_test.py --state "$STATE" --out /work/smoke-report.json
```

Then export the verified facts, scan them, upload the graph and sync:

```bash
bcp kb export --repo "$REPO" --out "$FACTS" --domains <slug-a>
# read "$FACTS/export-audit.json": every rule, exported or not, and why
python scripts/kit_hygiene_scan.py --root "$FACTS"   # and your own secret scanner; stop on any finding
BUCKET=$(python -c "import json,sys;print(json.load(open(sys.argv[1]))['bucket'])" "$STATE")
bcp graph build --repo "$REPO" --domains <slug-a> --upload "s3://$BUCKET/graph/" --region "$REGION"
bcp kb sync --facts "$FACTS" --state "$STATE" --dry-run     # what would upload and delete
bcp kb sync --facts "$FACTS" --state "$STATE" --out "$FACTS/sync-report.json"
bcp kb audit --facts "$FACTS" --state "$STATE"
bcp kb check-retrieval --facts "$FACTS" --state "$STATE"
```

Only `verified` rules and `needs-edit` rules with an approved replacement are exported. A fact quotes short
code excerpts, and code sometimes holds credentials, so scan the export before every sync. The graph upload
copies the graph to the knowledge base's S3 bucket under `graph/`; the knowledge base indexes only
`verified/`, so the graph is stored there but not searched.

**Check it worked:** the smoke test prints `smoke test: PASS (13/13 checks)`; `bcp kb sync` reports the
ingestion job `COMPLETE` with `numberOfDocumentsFailed: 0`; `bcp kb check-retrieval` reports each fact's hit
in the top k.

### Step 6: wire the retrieval hook

Add a `kb` section to `bcp.config.json` (copy `kb_id` and `region` from the state file; `graph` is the
graph file from step 4), then regenerate with an agent design.

```json
{
  "kb": {
    "kb_id": "<kb_id from the state file>",
    "region": "<region from the state file>",
    "graph": ".kiro/context/graph.json",
    "python": "<absolute path to a Python that has boto3, for example the .venv>"
  }
}
```

```bash
bcp generate --bre "$BRE" --target "$REPO" --review-dir "$REVIEW" \
    --config "$REPO/bcp.config.json" --agent-design hook-only
bcp validate --target "$REPO"
```

On each prompt the hook calls `Retrieve` for the top facts, loads the code-graph neighbourhood, writes the
full result to `.kiro/context/retrieved/latest.md`, and prints a summary of at most 2,500 bytes. On a
timeout or error it falls back to the graph alone and always exits 0. Set `kb.python` to a boto3-capable
interpreter so the hook does not silently fall back.

**Check it worked:** `.kiro/agents/brownfield.json` lists the `kb-retrieve` hook,
`.kiro/hooks/kb-retrieve.json` exists, and after one prompt `.kiro/context/retrieved/latest.md` contains a
`[verified KB <fact-id>]` line.

### Step 7: keep it current

```bash
bcp drift --bre "$BRE" --repo "$REPO" --out "$REVIEW/drift" --since-generation   # what changed
bcp kb refresh --facts "$FACTS" --state "$STATE" --repo "$REPO" --dry-run         # preview
bcp kb refresh --facts "$FACTS" --state "$STATE" --repo "$REPO" --out "$FACTS/refresh-report.json"
```

`bcp kb refresh` is deterministic (no AI). It re-anchors a fact whose cited lines moved but whose content is
unchanged, quarantines a fact whose cited lines changed, lists changed or new source files that no fact
cites as a coverage gap, then re-ingests `verified/` once. Re-review the quarantined facts (step 2), then
export and sync again (step 5). Run this on merge to a release branch, wired into whatever pipeline you run.

**Check it worked:** `bcp kb refresh` writes `refresh-queue.json` and `coverage-gap.json`, and its output
lists the counts re-anchored, quarantined and marked.

### Clean up

```bash
python scripts/cfn_stack.py delete --stack-name "$PREFIX-validated-kb" --region "$REGION"            # dry run
python scripts/cfn_stack.py delete --stack-name "$PREFIX-validated-kb" --region "$REGION" --confirm
```

With `RetainData=true` (the default) the facts bucket is kept on purpose. To remove it too, empty every
version and then delete it:

```bash
python -c "import json,boto3; s=json.load(open('$STATE')); b=boto3.resource('s3', region_name='$REGION').Bucket(s['bucket']); b.object_versions.delete(); b.delete(); print('deleted', s['bucket'])"
```

Verify nothing is left:

```bash
aws cloudformation describe-stacks --stack-name "$PREFIX-validated-kb" --region "$REGION"   # "does not exist"
aws s3api list-buckets --query "Buckets[?starts_with(Name, '$PREFIX-')].Name"
aws bedrock-agent list-knowledge-bases --region "$REGION" --query "knowledgeBaseSummaries[?starts_with(name, '$PREFIX-')]"
aws iam get-role --role-name "$PREFIX-kb-role-$REGION"                                       # NoSuchEntity
```

---

## Repository structure

```text
brownfield-context-for-kiro/
├── README.md                     # this file
├── LICENSE                       # MIT-0
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── pyproject.toml                # package metadata and the bcp entry point
├── requirements.txt              # runtime dependency pins
├── requirements-lock.txt         # every package, including test tools, exactly as tested
├── docs/
│   ├── reference.md              # background, configuration, IAM, cost notes, how to extend
│   └── images/architecture.png   # the architecture diagram (SVG source beside it)
├── src/bcp/                      # the bcp tool (standard library; boto3 for the AWS commands)
├── tests/                        # unit tests (pytest); no test calls AWS
├── power/brownfield-context/     # the Kiro Power source (three skills)
├── tools/                        # build_power.py and the skill install fallbacks
├── infra/
│   ├── cloudformation/validated-kb.yaml   # knowledge base, S3 Vectors, facts bucket, service role
│   ├── iam/                      # deployer and reader policies
│   └── config/kb-config.example.json
└── scripts/                      # cfn_stack.py (deploy, state, delete), smoke_test.py, kit_hygiene_scan.py
```

Running the steps writes into **your target repository**, not this one: AWS Transform custom adds
`business-rules-extraction/` (step 1), and `bcp` adds `.kiro/` (steps 2 to 6). Committing `.kiro/` is your
team's choice.

---

## Validated results

<!-- demo:start -->
The approach was validated on the public OpenMRS Core repository
([github.com/openmrs/openmrs-core](https://github.com/openmrs/openmrs-core)), an open-source electronic
medical record platform. Clone it yourself to reproduce the run; no OpenMRS source is included here.

On a bounded scope of one package (`org.openmrs.notification`), the recorded clean-room run produced these
results (OpenMRS Core; AWS Transform custom command line 3.21.0; a single run):

| Stage | Result |
|---|---|
| Extraction (step 1) | about 36 to 42 minutes; 31 alert-domain claims drafted |
| Mechanical pre-check and Kiro review (step 2) | 15 verified facts, 16 marked `needs-sme` (the review queue a human SME signs off; in this run Kiro stood in for the SME) |
| Ingestion (step 5) | 29 facts ingested, `COMPLETE`, 0 failed |
| Retrieval check (step 5) | all 29 facts in the top 3, median latency 413 milliseconds |
| Smoke test (step 5) | 13 of 13 checks, including the full quarantine-and-restore loop |

These are conditions-labelled results from one run, not promises for your codebase.

> The example codebase is OpenMRS Core (https://github.com/openmrs/openmrs-core), copyright OpenMRS Inc.,
> licensed under the Mozilla Public License 2.0. The rules, facts and code-graph slices referenced here
> are artifacts generated by analyzing that codebase; no OpenMRS source is redistributed in this
> repository. OpenMRS is a trademark of OpenMRS Inc. and is used here for identification only.
<!-- demo:end -->

> **Note on scope.**
> - The SME sign-off was done by Kiro standing in for a person in this repository's tests, labelled
>   `kiro (AI stand-in for SME)`. A human reviewer is part of the design; only `--reviewer-type sme`
>   produces SME-signed rules.
> - The keep-current step in a continuous integration (CI) pipeline is a template: the `bcp kb refresh`
>   call is real and tested by a script; no pipeline in this repository is deployed.
> - It is static analysis. Runtime behaviour, values in a deployment's database, and anything outside the
>   repository are only as good as the extraction, which describes today's code, including today's
>   defects. One extraction and one graph per repository.

---

## What is supported today and how to extend it

What works now: Java. Three parts of the pipeline read source and all three assume Java: the business-rules
extraction through AWS Transform custom, the class-level code graph (`bcp graph build`), and the mechanical
guard and citation checks (`bcp precheck`). The citation resolver, the review workflow, the knowledge base,
the hook and the keep-current logic are language-independent, because they work on the reviewed rules and
the graph artifact, not on source code directly.

The path beyond Java is a language-plugin seam at `src/bcp/lang/`. A new language reader implements the
`LanguageSupport` protocol in `src/bcp/lang/__init__.py` (six methods: `method_decls`, `method_range`,
`annotations_before`, `class_annotations`, `implemented_types`, `imports`), registers itself in `for_path`,
and (for graph support) generalizes the Java assumptions in `src/bcp/graph.py` and `src/bcp/deps.py`. Test
it by copying `tests/test_lang_java.py`, `tests/test_sourceindex.py`, `tests/test_precheck.py` and
`tests/test_graph.py`. The module-by-module map, the method contract and a suggested order of work are in
[the reference](docs/reference.md), section "What is supported today and how to extend it".

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `precheck` or `generate` parses 0 rules | The rule-heading pattern does not match your extraction's rule ids | Set `rule_heading_pattern` in `bcp.config.json` |
| Rules parse but show no resolved citations | The rules cite abbreviated paths, or label citation lines differently | `bcp` reads `Source:` and `Evidence:` by default; write full repo-relative paths, or set `source_line_pattern` |
| A JSON or configuration file is rejected | A UTF-8 BOM from Windows PowerShell 5.1 | Write UTF-8 without a BOM (see [Prerequisites](#prerequisites)) |
| `graph-only fallback (KB retrieve timed out)` | `Retrieve` exceeded its budget; a cold client costs several seconds | Normal on the first prompt. The hook still answers from the graph and exits 0 |
| `graph build`: "no main-source Java file is mapped" | The graph and the precheck guard check are Java-only | Skip the graph on another language; the hook works from facts alone |
| Ingestion `numberOfDocumentsFailed` greater than 0 | Metadata over 1,024 bytes, or a non-UTF-8 file | Re-export with this `bcp`; read `export-audit.json` |
| First CloudFormation deploy fails on the knowledge base | IAM propagation | Delete the rolled-back stack and deploy again |
| The AWS Transform output format changed | A future extraction labels citations or headings differently | Set `source_line_pattern` or `rule_heading_pattern` in `bcp.config.json` |

For the `bcp kb provision` route messages, see [the reference](docs/reference.md), "Troubleshooting".

---

## Example output

The smoke test (step 5) runs the whole gate on two synthetic rules in your knowledge base: export, ingest,
retrieve, the hook, a forced fallback, quarantine when cited code changes, and release only after
re-review. This output comes from one of our verification runs (paths shortened and the detail column
trimmed):

```console
$ python scripts/smoke_test.py --state kb_state.json --out smoke-report.json
PASS  export: both reviewed rules exported
PASS  guard: knowledge base holds no facts other than the smoke test's
PASS  sync: ingestion COMPLETE, 0 failed
PASS  retrieve: both rule facts in the top 3
PASS  hook: verified fact injected, summary <= 2,500 bytes, no fallback
PASS  hook: unreachable endpoint falls back, still returns a summary
PASS  stale: BR-1 and its derived facts quarantined
PASS  stale: S3 object moved verified/ -> quarantine/
PASS  stale: Retrieve no longer returns BR-1
PASS  restore code only: facts held until re-review (no silent re-trust)
PASS  restore review: facts released
PASS  restore: Retrieve returns BR-1 in the top 3 again
PASS  audit: every object allowed and identical to the export
smoke test: PASS (13/13 checks); report smoke-report.json
```

On a real codebase, the hook's summary is what Kiro sees before each prompt. Each line carries the fact ID,
its freshness, the rule and the evidence lines, so a reviewer can check an answer against the code:

<!-- demo:start -->
```text
## Retrieved context (bcp kb hook): 5 verified KB facts, 7 components, code graph 2224325ce8f2
- [verified KB visit-BR-9] [verified-fresh] BR-9: Visit date-ordering rule: IF visit.stopDatetime IS BEFORE
  visit.startDatetime THEN reject "stopDatetime" with "Visit.error.endDateBeforeStartDate"
  (evidence VisitValidator.java#L83-L91)
```

(From a run on OpenMRS Core; the line is wrapped here for width.)
<!-- demo:end -->

---

## Best practices

- **Start small.** Prepare one component and two or three domains first; widen after the first domain proves
  the effort.
- **Keep the human gate.** Let Kiro do the first review pass, then have an SME sign off. Never label an AI
  review as SME approval.
- **Treat generated context like code.** Review the generated steering and agents before you rely on them.
- **Scan before every sync.** Run `scripts/kit_hygiene_scan.py` and your own secret scanner on the export.
- **Least privilege.** Give engineers the reader policy only; keep the deployer policy for the people who
  provision.
- **Keep it current.** Run `bcp kb refresh` on merge to a release branch and re-review what it quarantines.
- **Verify independently.** Run the smoke test after provisioning and `bcp kb check-retrieval` after every
  sync; do not rely on a run's status alone.

---

## Cost

There is no always-on compute: the hook runs locally and the keep-current step runs as a script. Cost
follows AWS Transform custom agent-minutes for the extraction, the tokens embedded at ingestion, the facts
stored in Amazon S3 Vectors and Amazon S3, and the queries the hook makes. See
[the reference](docs/reference.md), "Cost notes", and the pricing pages below.

---

## Links

- [AWS Transform custom documentation](https://docs.aws.amazon.com/transform/latest/userguide/custom.html)
- [AWS Transform pricing](https://aws.amazon.com/transform/pricing/)
- [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)
- [Amazon S3 pricing](https://aws.amazon.com/s3/pricing/)
- [Kiro powers](https://kiro.dev/docs/powers/) and [Kiro hooks](https://kiro.dev/docs/hooks/)
- [Kiro frontier engineering: Build your codebase for agents](https://kiro.dev/topics/frontier-engineering/build-for-agents/)
- [docs/reference.md](docs/reference.md): the context contract, per-program configuration, the dependency
  map and framework rules, SME effort guidance, the extraction grounding text, the pilot checklist, MCP
  integration, cost notes, the IAM policies explained and the configuration reference.

---

## Security

- Three identities and three policies: the service role (Amazon Bedrock only, scoped to your account and
  knowledge bases), the deployer, and the reader (`Retrieve` on one knowledge base and one graph object).
- The facts bucket uses server-side encryption with Amazon S3 managed keys (SSE-S3), versioning and all
  public access blocked, and its bucket policy denies any request that is not HTTPS.
- Credentials use the standard AWS credential chain; never put a key in `.kiro/`, a steering file, the
  configuration or the state file.

See [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) for how to report security issues.

## License

This sample is licensed under the MIT-0 License. See [LICENSE](LICENSE). It is sample code: review it, test
it, and secure it for your environment before you use it on anything that matters.
