# Agent instructions

A short block in an agent's instruction file tells it to load Loom's context at the start
of each task and before each edit, when to save a fact and when to ask the code index
instead of grepping or reading files. This page gives that block once and the file each
client reads it from; paste it, fill in its two namespaces from `code.repos` (below),
and commit it with the repository.

The Claude Code and Codex plugins already do this through their hooks and skills
([Claude Code](claude-code.md#skills), [Codex](codex.md#skills)); the block is for every
other client, and for Claude Code or Codex connected without the plugin. It names the
tools the `coding` toolset lists, so connect the client on `?toolset=coding`:
`loom install --client <name> --toolset coding` for the clients it covers, else
`?toolset=coding` on the URL ([MCP clients](mcp-clients.md)).

## The block

```markdown
## Loom: memory and code index

Loom is connected as an MCP server; its tools are named `loom_*`. A tool you do not see
listed runs through `loom_op` with the op's dotted name: drop `loom_` and write the dots
(`loom_code_symbol` is `code.symbol`, `loom_working_memory_set` is `working_memory.set`).

Every task (no hook loads Loom for you, so these two calls are not optional)

- At the start of each task, before planning: `loom_context_pack` with the task as
  `query`. It returns the decisions, constraints and memories that bear on it.
- Before editing a file: `loom_op` with `op` set to `memory.for_file` and `arguments`
  holding `path` (the file's repo-relative path) and `extra_namespaces`
  (`["<team-namespace>"]`). Follow every constraint and decision it returns.

Memory

- To recall something mid-task, call `loom_search` with the topic, file or error. Treat
  `abstain` as "nothing relevant". A hit whose `code_state` is `changed`, `unresolved`
  or `missing` is about code that changed or went away since it was written: check the
  code before relying on it.
- When the user states a decision, preference, constraint or correction, save it with
  `loom_memory_set` right away: one plain-text fact per call under a readable key
  (`<scope>/<kind>/<id>`), dates as calendar dates. Never save a guess, your reasoning or
  the conversation.
- A fact that changed: `loom_memory_update` on the same key, never a second copy.
- Your plan and checklist for this task: `loom_working_memory_set`, not `loom_memory_set`.
- Before reporting done, one pass over what should outlive the session.

Code

- This repository's code namespace is `<code-namespace>`; pass it as `namespace` on
  every `loom_code_*` call.
- Where X is defined: `loom_code_symbol`. Who calls X: `loom_code_callers`. A concept
  with no exact keyword: `loom_code_search`. An exact string: `loom_code_grep`. What is
  in a file: `loom_code_outline`, then read one span. What a change breaks:
  `loom_code_impact`.
- Grep your own uncommitted files and repositories the index does not cover. When a code
  tool answers `abstain: true`, grep instead of retrying with synonyms.
```

`code.repos` (through `loom_op`) lists the repositories the token can read. Replace
`<code-namespace>` with the row's `namespace` (the indexed branch) and `<team-namespace>`
with its `primary_namespace` (the trunk branch's, where team file rules and decisions
live); for the trunk branch the two are the same.

## Where each client reads it

The block is the same everywhere; only the file and its header differ.

| Client                                           | File                               | Header before the block                                                      |
| ------------------------------------------------ | ---------------------------------- | ---------------------------------------------------------------------------- |
| Codex, Zed, and any agent that reads `AGENTS.md` | `AGENTS.md` at the repository root | none                                                                         |
| Claude Code without the plugin                   | `CLAUDE.md` at the repository root | none                                                                         |
| Cursor                                           | `.cursor/rules/loom.mdc`           | `---`, `description: Loom memory and code index`, `alwaysApply: true`, `---` |
| Windsurf                                         | `.windsurf/rules/loom.md`          | `---`, `trigger: always_on`, `---`                                           |
| Cline                                            | `.clinerules/loom.md`              | none                                                                         |
| Roo Code                                         | `.roo/rules/loom.md`               | none                                                                         |
| Continue                                         | `.continue/rules/loom.md`          | `---`, `name: Loom memory and code index`, `alwaysApply: true`, `---`        |
| VS Code (GitHub Copilot)                         | `.github/copilot-instructions.md`  | none                                                                         |

Each header line goes on its own line. The Cursor file, for example, starts:

```markdown
---
description: Loom memory and code index
alwaysApply: true
---

## Loom: memory and code index
```

An existing `AGENTS.md`, `CLAUDE.md` or Copilot file keeps its content: add the block as
a section of it.

## Why these rules

Nothing in a session without hooks prompts a recall, and a listed memory tool is one
choice among many, so the block makes loading context part of every task, as the
plugins' hooks do.

Each line is the short form of what the plugins teach:

- load context at the start of a task and before an edit: the plugins' session-start and
  file-context hooks, which call `context.pack` and `memory.for_file`;
- recall and save stated facts as they come: the
  [`loom-memory`](../../../integrations/claude-code-plugin/skills/loom-memory/SKILL.md)
  skill;
- the code index before grep, and grep where the index cannot answer: the
  [`loom-code`](../../../integrations/claude-code-plugin/skills/loom-code/SKILL.md)
  skill, which the plugins' code shunt enforces.

The block's last memory line stands for the end-of-task review; to spell it out, copy
its questions from the
[agent-integration guide](../../guides/agent-integration.md#7-follow-the-memory-skills-review)
under the block. The skills carry the detail the block leaves out: the receipt fields, documents versus
memories, the stop rules. To give an agent skills instead of the block, install the
`loom` and `loom-code` skills alone ([coding agents](index.md#coding-agents)); `loom`
covers what `loom-memory` teaches.

## Related

- [MCP clients](mcp-clients.md): connecting each client.
- [Choosing a toolset](mcp-clients.md#choosing-a-toolset).
- [Integrations](index.md).
