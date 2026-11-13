# Loom hooks for Cline

Two Cline hooks give a Cline agent Loom's memory without a tool call. The agent gets the session primer and the memories recalled for its task on its first tool call, and a file's memories when it reads that file (and after it edits one). They run the Claude Code plugin's hook scripts unchanged (`../claude-code-plugin/hooks/`), through `loom-cline.mjs`, which translates Cline's payload to theirs. Loom's tools in Cline come separately, through MCP: for the CLI, `cline mcp add` ([MCP clients](../../docs/features/integrations/mcp-clients.md#other-editors-and-agents)).

Experimental: tested with the Cline CLI 3.0.74 on Linux (`cline --json -y`), with project hooks in `.clinerules/hooks/`. On that version the hooks did not run from the directory the CLI's `--hooks-dir` option names, so install them in the project.

## What Cline shows the model

On Cline CLI 3.0.74, a hook's `contextModification` reaches the model's next request only when PreToolUse returns it. What TaskStart and UserPromptSubmit return does not reach the model: asked to quote every hook note in its context, a model saw only PreToolUse's. So the hooks work like this:

| Cline hook                                                                                                                              | What it does                                                                                                                                                                                                                                                                                                                                                     |
| --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `UserPromptSubmit`                                                                                                                      | Keeps the task's first prompt. Cline's own `[SYSTEM]` continuation prompts are skipped.                                                                                                                                                                                                                                                                          |
| `PreToolUse`, the task's first tool call                                                                                                | Adds the session primer (`loom-session-start.mjs`) and the memories recalled for that prompt (`loom-prompt-recall.mjs`). The primer's conversation-recording line is left out, because it speaks of the plugin's Stop hook, which Cline does not run.                                                                                                            |
| `PreToolUse` on a file read (`read_files`, its first three files) or edit (`editor`, `write_to_file`, `replace_in_file`, `apply_patch`) | Adds the memories about that file (`loom-file-context.mjs`, `memory.for_file`), once per file per task within 30 minutes (`LOOM_FILE_CONTEXT_TTL_MIN`). Since the model sees them on its next request, the read that comes before an edit is what brings them in time. On a read the block's heading still speaks of an edit: it is the plugin's pre-edit block. |

Every hook answers `{"cancel": false}`, with `contextModification` when there is something to add, and exits 0. If the Loom directory is missing or Node 18+ is not found, it adds nothing. If the server is unreachable, it adds only the primer's static notes. The task goes on in each case.

Keep the [agent instructions](../../docs/features/integrations/agent-instructions.md) block in `.clinerules/` beside the hooks: it covers what the hooks do not, such as saving stated facts and searching memory mid-task, and replace its paragraph that starts "Every task:" with "Every task: make these two calls." as that page says, so both calls stay required.

## Install

1. Get a Loom checkout, or a release's source archive, and note the absolute path of its `integrations/cline` directory.
2. Copy the two hooks into the project's `.clinerules/hooks/` and keep them executable. A project whose rules are one `.clinerules` file turns it into a directory first, moving the rules into a file inside it:

   ```bash
   mkdir -p .clinerules/hooks
   cp /path/to/loom/integrations/cline/hooks/* .clinerules/hooks/
   ```

3. Give the hooks their environment, in the shell that starts `cline`:

   ```bash
   export LOOM_CLINE_DIR=/path/to/loom/integrations/cline
   export LOOM_BASE_URL=https://loom.example.com
   ```

   Keep a token for the namespace in `~/.loom/token` (`loom init` and `loom mint-token --save` write it there) or `~/.loom/config.json`, or set `LOOM_API_KEY`. With no token, the hooks send nothing and do not sign the machine up. The namespace is `LOOM_NAMESPACE`, else the `namespace` that `loom init` saved in `~/.loom/config.json`, else the token's own (`coding` for a token whose only namespace is `*`). Of the plugin's settings, these apply here: `LOOM_INJECT_CONTEXT=false` turns off the primer, `LOOM_PROMPT_RECALL=off` the recall (only the task's first prompt is ever recalled), and `LOOM_FILE_CONTEXT=false` the file block, with `LOOM_FILE_CONTEXT_TTL_MIN` and `LOOM_FILE_CONTEXT_TOKENS` as in the plugin; `LOOM_AUTO_SIGNUP` defaults to off ([plugin README](../claude-code-plugin/README.md)).

## Check it

From the Loom checkout:

```bash
node --test integrations/cline/tests/*.test.mjs
```

Most tests fake the plugin's scripts; the ones that need them run the real scripts with no server, or with one that must receive nothing. To see the hooks in a real task, run `cline --json -y "<task>"` in the project after writing a memory whose `files` names a file the task reads, and ask the agent which Loom notes it saw.
