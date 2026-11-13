# Agent instructions

A short block in an agent's instruction file tells it when to load Loom's context, save
a fact and use the code index instead of grepping or reading files. This page gives that
block once and the file each client reads it from; paste it, fill in its two namespaces
from `code.repos` (below), and commit it with the repository.

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
listed runs through `loom_op` with the op's dotted name: on a connection without the
coding toolset, `loom_code_symbol` is `code.symbol` and `loom_working_memory_set` is
`working_memory.set`. `loom_op` with `{"op": "help", "query": "<words>"}` finds them.

Every task: make these two calls unless a Loom hook already put Loom's context in this
session (the Claude Code and Codex plugins do). Without one they are not optional.

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
- A rule the user states for the whole team about a file: `loom_memory_set` with
  `namespace` `<team-namespace>`, the file in `files` and the tag `constraint`.
- A fact that changed: `loom_memory_update` on the same key, in the namespace the row
  lives in, never a second copy. A team rule or decision someone else wrote is not yours
  to update: follow the refusal's `hint`.
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

`code.repos` lists the repositories the token can read or ask to join, one row per
indexed branch:

```bash
curl -s -X POST https://loom.example.com/v1/code.repos \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"repo": "<owner>/<name>", "fields": ["branch", "primary_namespace", "readable"]}'
```

Replace `<code-namespace>` with the `namespace` of the row for the branch this copy of
the file is committed on and `<team-namespace>` with its `primary_namespace` (the trunk
branch's, where team file rules and decisions live); for the trunk branch the two are
the same. A repository with no row (not indexed, or `code.repos` answers that code
intelligence is off), or whose row says `readable: false` (a repository this token
cannot read, listed so you can request access or adopt it), takes the block without
`extra_namespaces`, the team-rule line and the Code section.

## Where each client reads it

The block is the same everywhere; only the file and its header differ. Cursor, Cline,
Devin Desktop, VS Code and Zed also read `AGENTS.md`, so one file can serve them all;
put the block in one file a client reads, not in both.

| Client                                                         | File                                                                   | Header before the block                                                      |
| -------------------------------------------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Codex without the plugin, and any agent that reads `AGENTS.md` | `AGENTS.md` at the repository root                                     | none                                                                         |
| Zed                                                            | the first instruction file it finds (below)                            | none                                                                         |
| Claude Code without the plugin                                 | `CLAUDE.md` at the repository root                                     | none                                                                         |
| Cursor                                                         | `.cursor/rules/loom.mdc`                                               | `---`, `description: Loom memory and code index`, `alwaysApply: true`, `---` |
| Devin Desktop (Windsurf)                                       | `.devin/rules/loom.md` (`.windsurf/rules/loom.md` on a Windsurf build) | `---`, `trigger: always_on`, `---`                                           |
| Cline                                                          | `.clinerules/loom.md`                                                  | none                                                                         |
| VS Code (GitHub Copilot)                                       | `.github/copilot-instructions.md`                                      | none                                                                         |

Zed reads only the first instruction file it finds (`.rules`, `.cursorrules` and others
before `AGENTS.md`), so the block goes in that one, or in `AGENTS.md` when the repository
has none.

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
its questions from the [agent-integration
guide](../../guides/agent-integration.md#7-follow-the-memory-skills-review) under the
block. The skills carry the detail the block leaves out: the receipt fields, documents
versus memories, the stop rules. The `loom` and `loom-code` skills can be installed
beside the block for that detail ([coding agents](index.md#coding-agents)); `loom`
covers the memory loop, the write shapes, the receipts and the end-of-task review. They
have no hooks, so they do not replace the block's two calls.

## Related

- [MCP clients](mcp-clients.md): connecting each client.
- [Choosing a toolset](mcp-clients.md#choosing-a-toolset).
- [Integrations](index.md).
