# Brownfield context (Kiro Power)

This Power packages the `bcp` tool as three Kiro skills so the brownfield-context workflow runs from the
Kiro IDE. It does not replace the repository guide: the full rebuild-and-test instructions, prerequisites,
AWS setup and teardown are in the repository `README.md`, and the background reference (the context
contract, configuration keys, IAM policies, cost, and how to extend the tool to another language) is in
`docs/reference.md`.

`bcp` is a Python command-line tool that runs no AI itself. AWS Transform custom drafts a business-rules
extraction (BRE) from your code. `bcp` resolves the drafted citations against the code, runs mechanical
checks, generates Kiro steering and context, builds a class-level code graph, and provisions and syncs an
optional Amazon Bedrock knowledge base. Kiro and a subject-matter expert (SME) do the judgement; only
reviewed rules become facts.

## Skills

| Skill | Use |
|---|---|
| `brownfield-context-setup` | Generate and validate the `.kiro` context, build the code graph, and provision and sync the knowledge base |
| `bre-review` | Review drafted rules against the code and record verdicts and sign-off (repository `README.md`, step 2) |
| `bre-doc-drift-check` | Compare the extraction and the review with the current code |

Each skill vendors its own copy of `bcp` under `skills/<name>/scripts/`, so the Power runs without a
separate install. The setup skill also carries `references/reference.md` and `references/infra/` (the
CloudFormation template, IAM policies and config example) so its instructions resolve inside the Power.

## Build and install

Build the Power from the repository root, then install the built package in Kiro.

```bash
python tools/build_power.py --out dist
```

The build writes `dist/brownfield-context/` and `dist/brownfield-context-<version>.zip`, and prints
`check-power: PASS` with the three skill names.

- Kiro IDE: open the Powers panel, choose install from a local directory, and point at the built
  `dist/brownfield-context` folder (the one with `plugin.json`). See
  [Install powers](https://kiro.dev/docs/powers/installation/).
- Fallback (older Kiro CLI, or Power import blocked): from the built `brownfield-context` folder, run
  `sh tools/install-skills.sh .` on Linux or macOS, or
  `powershell -ExecutionPolicy Bypass -File tools\install-skills.ps1 -Package .` on Windows. Both copy
  the three skills to `~/.kiro/skills/`.

## Use it

Follow the repository `README.md` end to end. In Kiro, call a skill by name, for example: "Use the
bre-review skill to review domain `<slug>`." The setup skill runs the generate, graph and knowledge-base
steps; `bre-doc-drift-check` runs after a code change.

## License

MIT-0. See the repository `LICENSE`.
