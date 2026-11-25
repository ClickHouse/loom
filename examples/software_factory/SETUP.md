# Set up Claude Code with Loom in a software factory

Connect Claude Code to Loom over native MCP. Give Claude the repository's indexed
roots so it can call Loom for unchanged source, tests and linked lessons. Claude
uses its local tools for Git, edits, test execution and working-copy changes.

## Bind the worker to a repository

1. [Register and index the repository](../../docs/features/code-index/repositories.md).
   Cover implementation and test roots and include project docs that agents consult.
   Verify the index revision matches the worker's checkout before the first lookup.
   The Flask pilot compares tree-sitter with SCIP at the same revision and roots.
2. Give each worker a scoped Loom token with viewer access to its code namespace.
   If it uses historical memory, give the token access to a memory namespace and
   writer access only if the worker saves memories. [Link decisions and lessons to
   code](../../docs/use-cases/memory-linked-to-code.md) and check their link state
   before relying on them. Use a separate writable namespace per evaluation attempt.
3. Supply `LOOM_BASE_URL` without a trailing slash, `LOOM_API_KEY` from your secret
   store, and `LOOM_CODE_SCOPE` as `<org>/<code-namespace>` in the worker's environment.
   Put this entry in the repository's `.mcp.json` or pass the file to headless Claude
   Code with `--mcp-config`. The entry uses the pilot's compact catalog. Use the
   schema profile recorded in the result you want to reproduce.

```json
{
  "mcpServers": {
    "loom": {
      "type": "http",
      "url": "${LOOM_BASE_URL}/mcp?toolset=coding&schema_profile=compact",
      "headers": {
        "Authorization": "Bearer ${LOOM_API_KEY}",
        "X-Loom-Code-Scope": "${LOOM_CODE_SCOPE}"
      }
    }
  }
}
```

Leave `alwaysLoad` unset to use Loom's per-tool loading hints. With Loom defaults,
the compact catalog marks `loom_code_grep` and `loom_code_edit_context` to load
upfront. Other tools are available through Claude Code's tool discovery.

## Use the same source instructions

Copy [the project's retrieval instructions](harbor/project-guidance.md) into the
repository's `CLAUDE.md`. Add a line listing the indexed roots for this worker.
Keep the repository's existing build, test and contribution instructions.

For headless launches, also apply the conditional
`append_system_prompt` from [agent.json](harbor/agent.json). The instruction
uses Loom for the first unchanged-source lookup when Loom is connected. The
project instructions cover subsequent indexed reads, batching and local fallback.

From the Loom checkout, run the plan command to render the exact Flask pilot
configuration. It makes no model requests. The examples use `jq` to extract
fields from the plan and trace.

```bash
python3 -m examples.software_factory plan --eval flask --pilot-task pallets__flask-4074 \
  --pilot-repeats 1 --loom-schema-profile compact --disable-prompt-caching --json > factory-plan.json
jq -j '.project_guidance.text' factory-plan.json
jq -r '.native_solver.options.append_system_prompt' factory-plan.json
```

The plan includes the rendered `CLAUDE.md`, its hash, the native Claude Code
version and the solver model. Use the serving revision and plan for a published
result so later changes to these files do not change its recipe.

## Verify direct use and measure the result

Run one read-only question that needs an implementation and a related test.
Ask about the repository's behavior in the same way you would in a real task.
From the worker's checkout, with its `.mcp.json`, `CLAUDE.md` and a copy of the
rendered plan, capture Claude's native stream. Replace the question for your
repository. The smoke command preapproves Loom's code tools so a headless
worker can execute those calls.

```bash
FACTORY_SMOKE_QUESTION='Where does Flask register a blueprint, and which existing test covers nested registration? Cite paths and line numbers. Keep the checkout unchanged.'
claude -p --strict-mcp-config --mcp-config .mcp.json \
  --allowedTools 'mcp__loom__loom_code_*' \
  --model "$(jq -r '.model' factory-plan.json)" \
  --append-system-prompt "$(jq -r '.native_solver.options.append_system_prompt' factory-plan.json)" \
  --verbose --output-format stream-json "$FACTORY_SMOKE_QUESTION" > loom-smoke.jsonl
jq -r 'select(.type == "assistant") | .message.content[]? |
  select(.type == "tool_use" and (.name | startswith("mcp__loom__loom_code_"))) |
  [.id, .name] | @tsv' loom-smoke.jsonl
```

Check that the corresponding `tool_result` contains the expected indexed source.
For the memory setup, check that a relevant result carries the expected linked
lesson and inspect its `code_state`. A connected MCP server can still receive
zero calls. Check follow-up source lookups too, since Claude can use Loom first
and later search the same indexed roots locally.

Use the [Flask reproduction instructions](evals/flask/REPRODUCE.md) for a paired
coding comparison. Keep both arms' local tools available. Compare hidden-test
success, native session cost and agent time alongside actual indexed tool use.
Keep prompts and settings fixed within a run and retain their exact text with
the [results](evals/flask/results/README.md).
