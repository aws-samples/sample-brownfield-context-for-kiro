# Wiring Jira, Confluence and git MCP servers into the agents

This is guidance, not shipped code. The generated agents use only local files today. When a team already runs MCP servers for its issue tracker (for example Jira), its wiki (for example Confluence) and its git host, you can give Kiro read access to them. Each source then gets a trust tier, like the generated context.

## Principles

- Read-only. Give the agents read tools only (search, get issue, get page, get commit, get diff). No create, update, comment or merge tools.
- Least data. Ask for the fields the task needs (summary, description, components, fix versions, links). Do not pull attachments or personal fields.
- Masking. Do not paste credentials, tokens or personal data into prompts or context files. The server configuration holds the credentials, never a steering file.
- Provenance. Answers tag MCP content like other context: `[issue: KEY-123]`, `[wiki: <page title>, <last updated>]`, `[commit: <sha>]`.

## Trust tiers for MCP sources

| Source | Tier | Why |
|---|---|---|
| Git commits, diffs, blame, tags | Tier 1 | Mechanical record of what changed and when |
| Issue fields: status, fix version, component, links to commits | Tier 1 | Recorded facts about the ticket |
| Issue descriptions, comments, root-cause text | Tier 2 | Human narrative; often right about symptoms, often wrong about causes |
| Wiki pages: requirements, features, architecture, how-to | Tier 2 | Documented intent; may lag the code. Run the doc-drift check before relying on it |
| Wiki diagrams and old design pages | Tier 3 | Hypothesis only; use them to find code |

The code still wins. A wiki page that disagrees with the code is a drift finding, not a fact.

## Step 1: add the servers to Kiro

Add the servers to the workspace MCP configuration, `.kiro/settings/mcp.json`. The names and commands below are placeholders; use the server packages and endpoints your team already runs.

```json
{
  "mcpServers": {
    "issues": {
      "command": "<issue-tracker-mcp-command>",
      "args": ["--read-only"],
      "env": { "ISSUES_URL": "https://<issue-tracker-host>", "ISSUES_TOKEN": "${ISSUES_TOKEN}" },
      "autoApprove": ["search_issues", "get_issue"]
    },
    "wiki": {
      "command": "<wiki-mcp-command>",
      "args": ["--read-only"],
      "env": { "WIKI_URL": "https://<wiki-host>", "WIKI_TOKEN": "${WIKI_TOKEN}" },
      "autoApprove": ["search_pages", "get_page"]
    },
    "git": {
      "command": "<git-mcp-command>",
      "args": ["--repository", "<repo-path-or-url>", "--read-only"],
      "autoApprove": ["git_log", "git_show", "git_diff"]
    }
  }
}
```

Keep tokens in environment variables. Do not commit them.

## Step 2: give the agents the tools

`bcp generate` owns `.kiro/agents/*.json` and rewrites them. Two options:

1. Copy an agent to a new name (for example `.kiro/agents/defect-triage-mcp.json`). bcp does not own the copy, so it survives regeneration. Add the servers and read tools:

   ```json
   {
     "mcpServers": { "issues": { "command": "<issue-tracker-mcp-command>", "args": ["--read-only"] } },
     "tools": ["read", "@issues/search_issues", "@issues/get_issue", "@git/git_log", "@git/git_show"],
     "allowedTools": ["read", "@issues/search_issues", "@issues/get_issue", "@git/git_log", "@git/git_show"]
   }
   ```

   Exact field names follow the Kiro custom-agent documentation for your Kiro version.
2. Or keep the generated agents and add a short steering file (not owned by bcp) that tells Kiro which MCP tools to use for which question.

Re-run `bcp validate` after editing agents. It checks that shell stays denied.

## Step 3: use each source

- **Wiki (Confluence or similar), Tier 2.** For requirement and onboarding questions, search the wiki for the feature, read the page, and compare it with `reviewed-rules.md` and the code. For a doc-drift run, export the relevant pages to Markdown in a folder (the wiki MCP server or the wiki's own export), then run `bcp docdrift --docs <folder> --repo <repo> --out <out>`. The report lists missing identifiers, value mismatches and sentences for Kiro to check.
- **Issue tracker (Jira or similar).** For defect triage, search for earlier defects with the same symptom, error code or component. Read their root-cause records (below). Treat each one as a lead to check in the code, not as the answer.
- **Git.** For impact analysis and release drift, read the commits and diffs since the context was generated (`bcp drift --since-generation` does this locally, read-only). For a fix, link the commit to its issue and to the business rules it touches.

## Root-cause (RCA) record format

When a defect is closed, record its root cause in a fixed shape, in the issue or in a team file. Kiro can then search earlier causes and check them against the code. One record per defect:

`symptom → root cause → fix commit → affected programs → regression test`

| Field | Content | Example |
|---|---|---|
| `issue` | Issue key | `PROJ-1234` |
| `symptom` | What the user or log saw, with error code or message | "Order saved twice; message `Order.duplicate`" |
| `root_cause` | What the code did and why, with file and method | "`OrderServiceImpl.saveOrder` retried after a rollback-only transaction" |
| `rules` | Business rules involved, by domain and BR id | `order/BR-12` |
| `fix_commit` | Commit SHA(s) of the fix | `a1b2c3d` |
| `affected_programs` | Programs or deployments affected, and the configuration that made them so | "north, south (feature.mode = strict)" |
| `regression_test` | Test class and method that fails without the fix | `OrderServiceTest.shouldNotSaveTwiceAfterRollback` |
| `status` | `confirmed` (checked in code) or `suspected` | `confirmed` |

JSON schema for tools that store records as files:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "rca-record",
  "type": "object",
  "required": ["issue", "symptom", "root_cause", "fix_commit", "affected_programs", "regression_test", "status"],
  "properties": {
    "issue": { "type": "string" },
    "symptom": { "type": "string" },
    "root_cause": { "type": "string" },
    "rules": { "type": "array", "items": { "type": "string", "pattern": "^[a-z0-9-]+/BR-\\d+$" } },
    "fix_commit": { "type": "array", "items": { "type": "string", "pattern": "^[0-9a-f]{7,40}$" } },
    "affected_programs": { "type": "array", "items": { "type": "string" } },
    "regression_test": { "type": "string" },
    "status": { "enum": ["confirmed", "suspected"] }
  }
}
```

A record with no regression test is incomplete. Kiro should say so when it finds one.

## Optional: the verified knowledge base over MCP in the IDE

The supported path to the verified knowledge base is the `kb-retrieve` hook (`bcp kb hook`), which `bcp generate` adds to the `brownfield` agent when the config has a `kb` section. It works headless and in the IDE, needs no MCP server, and falls back to the code graph when AWS is slow.

In the Kiro IDE you can also give the chat an ad hoc search tool with the AWS Labs Bedrock Knowledge Base Retrieval MCP server (`awslabs.bedrock-kb-retrieval-mcp-server`). This is optional and IDE only: in headless Kiro CLI runs, MCP servers on the top-level agent did not load, and a sub-agent with an inline MCP server adds a model turn per question. Add it to the workspace `.kiro/settings/mcp.json` (not the user file), for example:

```json
{
  "mcpServers": {
    "bedrock-kb": {
      "command": "<path-to>/awslabs.bedrock-kb-retrieval-mcp-server.exe",
      "env": { "AWS_PROFILE": "<profile>", "AWS_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true" },
      "autoApprove": ["QueryKnowledgeBases"]
    }
  }
}
```

Use `uvx awslabs.bedrock-kb-retrieval-mcp-server@latest` as the command when the server is not installed locally. The tool names follow the server's documentation for your version. Results from it are the same verified facts as the hook's: tag them `[verified KB <fact-id>]`, treat them as a map, and apply the mandatory code check.

## What this does not change

- The mandatory code check still applies. An issue or a wiki page never replaces reading the method.
- Per-program configuration still comes from read-only exports (`references/program-snapshots.md`), not from a live database connection.
