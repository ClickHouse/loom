---
title: Memory linked to code for coding agents
description: Link agent lessons to functions, classes and files. Check what stays current across trunk and release branches as code changes, moves or disappears.
seo:
  title: Code-linked agent memory
---

# Memory linked to code for coding agents

Loom links coding agents' memories to the functions, classes and files they name, follows
them through refactors, and flags a memory when its code changes. By default, search leaves
out a memory whose code is all gone unless a person pinned, locked or verified it, and every
read that serves a lesson says when its code changed or is gone. It runs over MCP, HTTP and
the SDKs on the repositories a self-hosted Loom indexes (the
[code index](code-intelligence.md)), and the
`memory_code_links` worker that keeps the links is on by default.

To see the change in a read response, [save a lesson, edit its source and check the
reported commit](#check-a-lesson-after-a-code-edit).

## What you get

- **Lessons attached to the code they are about.** A memory tagged `repo:<id>` (the Claude
  Code and Codex plugins tag the lessons extracted from a session's transcript) or written
  in the repository's code namespace is linked to each definition (function, class or other
  symbol) it names in
  backticks, or anywhere in its text as `A::b` or a call (`a.b()`, `name()`), that resolves
  to exactly one place (an unqualified name needs four or more characters); to definitions
  in the files it lists that it names by a bare camelCase or snake_case word; and to each
  file it lists that holds none of them. The worker retries pending names as definitions
  reach trunk.
  [Memories linked to code](../concepts/memory.md#memories-linked-to-code)
- **Links that follow refactors.** On every indexed commit, each link reads `fresh`,
  `moved`, `changed` (naming the commit that changed it), `unresolved` or `missing`. A link
  records the definition's tokens, so whitespace, comments and most formatter changes leave
  it `fresh`. A definition renamed as it moves to another file or class is followed
  when one new definition there is it: `moved` with the same tokens, `changed` with an
  edit or outside its class. A link to a file follows the lines that hold a name the
  memory cites, and is replaced by a link to a definition the memory names once the
  file defines it.
  [Link states](../concepts/memory.md#memories-linked-to-code)
- **The current name beside a memory written against the old one.** A rename leaves the
  memory's text as it was written. These reads name where the code is now:
  `memory.get`, `memory.search` and the `loom_search` text, `memory.for_file` and the
  pre-edit block, `lessons` on code answers, `context.pack` and the dashboard, and the
  note `memory.ask` gives its synthesis (where the deployment sets
  `memory_ask_code_namespace` to a code namespace the caller may read and leaves
  `memory_ask_code_staleness` on). A line reads
  ``(code moved: `reader::fetch_rows` is now `reader::load_rows` at src/reader.py:12-40)``,
  and the structured answer carries the same as `code_moved`.
  [What reads name](../concepts/memory.md#memories-linked-to-code)
- **Stale memories handled the way you choose.** The namespace's
  [`code_change_policy`](../reference/operations/namespace.md#namespaceregister) decides:
  `retire_removed` (the default) leaves a memory whose code is all gone out of search and
  flags changed code; `flag` only flags; `revise` also leaves gone-code memories out, and
  has the model check a changed one's claim against the old and new code: one that still
  holds is re-anchored, and a contradicted one gets a suggested correction. A memory a
  person pinned, locked or verified is never left out. Under every policy, search usually
  ranks a memory whose code changed below the next three results instead of hiding it; a
  held memory and a row found by name keep their place. Loom never rewrites the memory
  itself. [Policies](../concepts/memory.md#memories-linked-to-code)
- **Lessons wherever the agent reads code.** `code.symbol` returns the memories linked to a
  definition as `lessons`; `code.search`, `code.callers`, `code.callees`, `code.impact`,
  `code.outline` and a task's `code.context_pack` attach the lessons for the files they
  return; each lesson carries its link state. A slow or failed file read leaves the
  completed files' lessons intact within the enrichment timeout. An edit lookup can
  include [fresh lessons from an indexed caller](../features/code-index/operations.md#read-caller-lessons-before-editing-a-dependency)
  when it returns that caller's verified source. The Claude Code and
  Codex hooks show a file's memories (`memory.for_file`) before an agent edits it, and
  the Cline hooks when it reads
  or edits one. [`code.symbol`](../reference/operations/code.md#codesymbol) ·
  [Claude Code plugin](../features/integrations/claude-code.md) ·
  [Codex plugin](../features/integrations/codex.md) ·
  [Cline hooks](../../integrations/cline/README.md) (experimental)
- **Shared with the team.** Lessons are read from the repository's code namespace and the
  namespaces the call names (over MCP, the caller's own by default), so a lesson saved in
  the repository's code namespace reaches every agent that may read that namespace (a token
  narrowed to its own user or session reads no lessons).
  [Memories linked to code](../concepts/memory.md#memories-linked-to-code)
- **Branches read their own code.** On another branch, a lesson is checked against that
  branch's own definition, so a release branch that still has the old text reads `fresh` (on
  a host with `LOOM_GIT_HISTORY=none`, which has no git mirror, a lesson carries trunk's
  state). [Memories linked to code](../concepts/memory.md#memories-linked-to-code)
- **Documents beside the code.** Documents from a source registered on the repository's code
  namespace (its trunk's) carry the repository's tag, so searches over the repository read
  them with its code (the sync worker needs `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`).
  [Document sources](../features/knowledge.md#document-sources)
- **A review list.**
  [`namespace.validate`](../reference/operations/namespace.md#namespacevalidate) lists the
  memories whose code changed, was not found again or is gone, and the dashboard shows each
  memory's links and their state.
- **The edit a rename allows.** After a rename or a move the link follows, the same report
  lists the exact token edits that bring the memory's text up to date, or the reason it
  offers none. [Bring a lesson's text up to a rename](#bring-a-lessons-text-up-to-a-rename)

![A memory's detail in the dashboard: its linked code, one definition current and one changed since a commit](../assets/screenshots/memory-linked-code.webp)

## Start

[Quickstart](../getting-started/quickstart.md) boots Loom, and
[adding repositories](../features/code-index/repositories.md) turns the code index on for a
repository. Connect your coding agent with its plugin or over MCP
([connect a coding agent](../getting-started/connect-a-coding-agent.md)). Save a lesson that
names a definition of four or more characters in backticks or as a call (`` `name()` ``), or
a qualified one such as `` `Reader.run` ``. The Claude Code and Codex plugins tag the
lessons extracted from a session's transcript with `repo:<id>`, and the session primer names
the id
([how it is derived](../features/integrations/claude-code.md#configuration)). A lesson the
agent saves itself with `loom_memory_set` links when it carries that tag
(`tags: ["repo:<id>"]`) or is written in the repository's code namespace; any client can do
either. Over HTTP (or `agent.call` in the Python SDK), a lesson saved in your own namespace
is read only when the call names that namespace in `lesson_namespaces`. Each link pass links
writes at least 60 seconds old and passes run every 60 seconds, so once trunk is indexed,
`code.symbol` on that definition returns the lesson under `lessons` within about two
minutes.

## Check a lesson after a code edit

Try the check with a sample retry rule in the
[code-linked memory Playground](https://loom.clickhouse.com/playground/source-change).
Change the retry status and see whether the saved lesson still matches the function.

On the repository's indexed trunk, save a lesson such as `` `fetch_page()` retries on code 115 ``
with the repository tag or in its code namespace, as above. Use a name that resolves to
one definition. After the link pass, read that definition with `code.symbol` and look for
the saved memory under `lessons`. When included, it carries `code_state: "fresh"`.
Include the memory's namespace in `lesson_namespaces` when the call needs it.

Change an expression in the function's body, such as `return url` to `return url.strip()`.
Commit the edit and push it to the indexed trunk. After the index and link worker process
the commit, read the definition again in the same trunk namespace. Under the default policy, a returned
lesson carries `code_state: "changed"`; `changed_since` names the indexed
commit. This marks changed code, not a judgment that the lesson is false. Read the
current body before applying it. Uncommitted editor changes do not trigger this check.

Symbol cards cap the lessons they return and can omit the saved lesson. If it is absent,
use `memory.get` to inspect that memory's `code` links and their states.

The existing `test_memories_follow_their_code`
[integration regression](https://github.com/ClickHouse/loom/blob/main/tests/integration/test_memory_code_links.py)
checks these states across commits. It also checks a pure move (`moved`), removed code
(`missing`) and a release branch whose unchanged definition still reads `fresh`.

To see a rename, change only the function's name on trunk and push. After the index and
link worker process the commit, `memory.get` on the lesson lists the link under `code`
with the new `symbol`, its `start_line` and `end_line`, and `was_symbol`, the name the
lesson's text still uses. A search that serves the lesson carries the same link in
`code_moved`, and its line in the `loom_search` text names both.

The packaged [MCP smoke](https://github.com/ClickHouse/loom/blob/main/scripts/gates/docker_code_smoke.py)
saves a lesson, verifies `fresh`, commits a body edit and waits for the indexing and link
workers to return `changed` with that exact commit. The
[quickstart harness](https://github.com/ClickHouse/loom/blob/main/scripts/gates/docker_quickstart_smoke.py)
restarts the Loom service, checks the link again and deletes the test memory.
[CI](https://github.com/ClickHouse/loom/actions/workflows/docker-smoke.yml) runs these checks
on amd64 and arm64 with authentication off, a local embedder and a fake model provider.
These checks verify code-link state; authenticated clients, answer quality and agent
tool selection are not tested.

For removal, held-memory exceptions and model-backed revision, see the
[link policies](../concepts/memory.md#memories-linked-to-code).

## Bring a lesson's text up to a rename

When a definition is renamed or moved with nothing else changed, its link follows it and
reads `moved`. A lesson that wrote the old name, module or path still says it. Loom does
not rewrite it; `namespace.validate` lists what a rewrite would be:

1. Call `namespace.validate` on the memory namespace (with `keys` to check the lessons
   one session wrote) and read the `code_rename_edits` finding. Each sample is one lesson
   with either `edits` and a `content_sha256`, or a `reason`.
2. For a lesson with `edits`, `start` and `end` count Unicode code points of its text (a
   Python string index; not the UTF-16 units of a JavaScript string, nor bytes). Check
   that the text from `start` to `end` reads `old`, and apply nothing when it does not:
   the positions were counted in another unit. Then replace it with `new`, applying the
   last edit first so the earlier positions hold. Read the edited sentence: an edit is
   offered on the spelling and the index, and whether the claim survives the swap is
   yours to judge.
3. Call `memory.update` on the key with the revised text and `if_match_sha256` set to the
   sample's `content_sha256`. A lesson that changed since the report returns `409`; run
   the report again.
4. After the next link pass the lesson is linked at the new name and reads `fresh`, and
   the finding lists it no more.

A lesson with a `reason` gets no edit: the old name is a word of the prose, sits in a
quoted snippet, still names another definition, or the sentence is about the name itself
([every reason](../concepts/memory.md#memories-linked-to-code)). Check it against the code
and rewrite it by hand.

The finding's `totals` count a namespace's lessons by `edits` and by reason: how often a
mechanical edit is offered there, and what stops the rest. Applying the edits without a
person reading them is worth considering only for a namespace whose `totals` come from its
own lessons and where at least 100 listed edits were checked by hand against the lesson
text with none wrong.

The `test_validate_lists_the_edit_a_followed_rename_allows_and_an_update_applies_it`
[integration regression](https://github.com/ClickHouse/loom/blob/main/tests/integration/test_memory_code_links.py)
renames a function, reads the listed edit, applies it with `memory.update` and checks the
link at the new name.

## Compare a code-linked lesson across branches

When trunk and a maintained release branch diverge, a lesson can still apply to one of
them. Index both branches of the same repository in [separate code namespaces](../features/code-index/overview.md#one-namespace-is-one-branch)
and run this check on a Loom host with local git mirrors (`LOOM_GIT_HISTORY=local`).
Use credentials that can read both code namespaces and the lesson.

1. Start with the same definition on both branches. [Save a lesson](#start) about it and
   use `code.symbol` in each code namespace. Pass the same `lesson_namespaces` list when
   reading a separate memory namespace. Check that both responses include the same
   lesson and that its `code_state` is `fresh`.
2. Push a commit to the indexed trunk and leave the release branch unchanged. After the
   index and link worker process that commit, read each branch again. Run either example
   from the original definition.

   | Change on trunk                                         | What to check on trunk                      | What to check on the unchanged release branch    |
   | ------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------ |
   | Change an expression inside the function                | The lesson reads `changed`.                 | The same lesson reads `fresh`.                   |
   | Rename a nonrecursive function without editing its body | Look up the new name and check for `moved`. | Look up the original name and check for `fresh`. |

3. Review the definition on the branch you are about to edit. `fresh` means the linked
   code still matches on that branch. Check that the lesson's claim applies to the task
   before using it.

Some ops on this page are marked experimental on the [status page](../reference/status.md).
