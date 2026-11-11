# Claude Code plugin

The `loom` plugin for Claude Code does three things in one install:

- **Mounts the Loom tools** over MCP: memory, working memory, `context.pack`, the code
  index, the duplicate check.
- **Captures the session** through lifecycle hooks: tool calls, chat turns, a run record,
  a working-memory note, a session brief and the session's facts. The hooks make no model
  call, and the agent has nothing to remember to do.
- **Steers the agent to the code index** inside a Loom-indexed repository. Whole-file
  reads and tree-wide greps are redirected to `code.*` operations, and an edit gets the
  file's rules before it and its blast radius after it.

Source: `integrations/claude-code-plugin/`, Node ESM scripts with no dependencies. The
Codex build runs the same scripts; see [codex.md](codex.md).

## Data handling

This section covers the Claude Code plugin and the Codex plugin, which run the same
scripts; Codex wires all but the token-usage hook. The hooks and the plugin's scripts send
data only to the Loom server the plugin is configured with; the two skills named at the
end of this section are the exceptions. The plugin sends:

- whatever the agent passes to a Loom MCP tool: memories it saves, search and code
  queries;
- the repository's identity. The owner and name of each git remote go to `code.repos`
  to find its code index. The owner and name of the matched remote go with every
  observation, run record and session brief; the branch and `HEAD` commit go with each
  brief, and the absolute working directory with the run record. Observations and session
  memory are tagged with the checkout's directory name and a hash of the repository, and
  observations also carry the working subdirectory;
- prompts, to fetch recalled memories and code pointers for them;
- each tool call's input, output or error (`observe.record`), and the paths of files read
  or edited, for the code shunt, file rules and blast radius; a symbol name taken from a
  redirected grep pattern (`code.symbol`); and the search query of a `gh` issue search the
  tracker shunt answers (`tracker.duplicate_check`), with the owner and name of a repository
  the command names (`code.repos`);
- chat turns (`conversation.append`), which the server stores only when the namespace has
  conversation logging on;
- the session's last turns (up to 200), to `memory.set_from_messages`, which keeps them as
  source evidence for the facts it extracts whatever the logging setting is, with up to
  80,000 characters of the session's Loom tool arguments and results
  (`LOOM_SESSION_MEMORY=off` stops it);
- a run record (the first prompt as its goal, the last reply) and a working-memory note
  (goal, git status, files edited, last reply);
- a session brief at each compaction and at session end: the client's compaction summary
  of the whole context window (up to 15,000 characters) and a log of the session's events,
  with each ask and reply, command, file read or edited, search pattern, web address or
  query, subagent prompt, error, todo list, other tool's name, and pull request and commit
  identifier. The server stores none of it, summary or log, when the namespace has
  conversation logging off (`LOOM_SESSION_BRIEF=off` stops sending it);
- token usage and the model name (Claude Code only);
- after each edit in an indexed checkout, the outline of that file's uncommitted diff
  against `HEAD`, to `code.impact` (`LOOM_BLAST_RADIUS=off` stops it);
- at Stop in an indexed checkout, the outline of the diff of the files the session
  changed, or of the whole uncommitted tree when the client gives no transcript, to
  `code.test_gate` (`LOOM_TEST_GATE=off` stops it). A diff's outline, up to 200,000
  characters, is its file paths, its hunk line ranges and, in a code file, the last
  identifier before the first `(` of each hunk header's context, usually the enclosing
  function's name. Git takes that context from a line above the hunk in the old file,
  which an earlier hunk of the same diff may have changed; no other part of a changed
  line is sent;
- with no credential, at the first session, your OS user name and host name, to request a
  self-signup token (refused unless the server allows self-signup; `LOOM_AUTO_SIGNUP=off`
  stops it);
- in agent mode, the workload's identity token and `LOOM_CLIENT_ID`, to the server's
  `/oauth/token`, in exchange for a Loom token.

[What the hooks do](#what-the-hooks-do) lists the hooks behind these requests. A
client-side redaction removes common credential shapes from the prompts, tool calls, turns,
run records, notes and briefs the hooks send. It is best effort, not a guarantee. MCP tool
arguments and a brief the `loom-session` skill saves are sent without it.

The server keeps this in its own ClickHouse database. It passes text to an LLM or embedding
provider only if its operator configured one ([models and keys](../../getting-started/models-and-keys.md)).
Deleting data is covered in [retention and erasure](../memory/retention-and-erasure.md).
Two skills reach other services, each only when you ask for it. The `loom-status` skill
can install Node.js from NodeSource through apt, dnf or yum, or from apk or Homebrew, and
runs that installer only after you agree. The `improve-loom-for-my-repo` skill searches
the ClickHouse/loom GitHub issues for each finding it reports and, after you approve,
files its report there, or adds it as a comment on the issue that already reports it.
The issue is public, and names the namespace and the symbols and queries behind each
finding.

## Requirements

- A reachable Loom server ([quickstart](../../getting-started/quickstart.md)).
- Node.js 18 or newer. The hooks and the MCP header helper start through the plugin's
  `bin/loom-node`, which runs the first Node 18+ among `LOOM_NODE`, `node` on the `PATH`
  Claude Code hands its hooks, a version manager's default or newest install, and the
  usual install directories. That `PATH` is Claude Code's own, so Claude Code started
  from the Dock or a desktop launcher does not see one a shell profile sets up. For a
  Node the launcher misses, set `LOOM_NODE=/path/to/node` in the `env` block of
  `~/.claude/settings.json`. If a hook says it found no Node, run the `loom-status`
  skill; it diagnoses read-only and offers to install Node, running the installer only
  when you say yes.
- Supported platforms: macOS (Apple silicon, Intel), Linux (x86_64, arm64; glibc or musl;
  WSL 2 included) and Windows (x86_64, arm64). On macOS and Linux every hook and the
  header helper go through `bin/loom-node` under the system `sh`. On Windows the header
  helper runs through `cmd.exe` and the hooks through Git Bash, or through PowerShell when
  Git Bash is absent; the header helper under `cmd.exe` and the hooks under PowerShell
  run the `node` on `PATH`, so Windows needs Node.js 18 or newer on the `PATH` Claude Code
  starts with; without Git Bash the hooks run that `node` without checking its version. A
  hook that finds no `node` lets the session continue; it writes one `loom:` line to its
  stderr, which Claude Code shows as a hook error under `sh` (macOS, Linux, Git Bash) and,
  under PowerShell (where the hook exits 0), only in `claude --debug`. Without Git Bash,
  commands run through Claude Code's PowerShell tool are captured and the test gate reads
  the paths they name, but the code shunt judges only `sh` commands.
- A **writer** token for the namespace the session writes to. A viewer token lets the
  tools read, but the server refuses every capture.

## Install

The install steps are in [connect a coding agent](../../getting-started/connect-a-coding-agent.md).
In short:

```bash
claude plugin marketplace add ClickHouse/loom        # or a path to a local clone
claude plugin install loom@loom \
  --config server_url=https://loom.example.com \
  --config token=<writer JWT>
```

Install either this plugin or `loom-code-skill`, not both; the plugin already ships the
`loom-code` skill. Update with `claude plugin update loom@loom`: hooks ship with the
plugin, not the server.

| Option        | Default                                        | Meaning                                                                               |
| ------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------- |
| `server_url`  | `$LOOM_BASE_URL`, else `http://localhost:7777` | Loom base URL, no trailing slash                                                      |
| `token`       | empty                                          | a writer JWT, stored as a sensitive value                                             |
| `toolset`     | `coding`                                       | the MCP listing (`?toolset=`), e.g. `coding+tracker`; every op stays callable by name |
| `local_first` | `false`                                        | `true` stands the code shunt down (same as `LOOM_CODE_LOCAL_FIRST=on`)                |

Every option is optional; Claude Code asks once, and an empty answer falls back to the
[lookups below](#configuration).

- **Install token.** The MCP header helper cannot see install options, so the first hook
  of a session copies the token and URL into `~/.loom/config.json` (0600). It does so only
  when no other token exists (`LOOM_API_KEY`, `config.json`, `~/.loom/token`).
- **No credential at all.** On a server with `LOOM_SELF_SIGNUP=open`, the first session
  signs the machine up: token to `~/.loom/config.json`, a claim code to
  `~/.loom/claim-<namespace>.txt`, and the primer says how to claim the namespace (once,
  at `/oauth/agent-claim`, with a password of your own). `LOOM_AUTO_SIGNUP=off` disables
  this. Otherwise the primer says how to get a token.
- **One MCP entry only.** Do not add a user-scope `loom` entry as well: every tool would
  be listed twice, and that copy carries no code scope.

## Configuration

**Where the hooks find the server and the identity.** Environment first, then the install
option, then `~/.loom/config.json`, then the token's claims, then the defaults
(`lib/loom-client.mjs`: `resolveServerUrl`, `resolveToken`, `resolveScope`):

| What      | Lookup order                                                                                             |
| --------- | -------------------------------------------------------------------------------------------------------- |
| server    | `LOOM_BASE_URL` → `server_url` option → `config.json` `url` → `http://localhost:7777`                    |
| token     | `LOOM_API_KEY` → `token` option → `config.json` `token` → `~/.loom/token` (moved by `LOOM_API_KEY_FILE`) |
| org       | `LOOM_ORG` → `config.json` `org` → the token's `org` claim → `default`                                   |
| namespace | `LOOM_NAMESPACE` → `config.json` `namespace` → the token's first non-wildcard namespace → `coding`       |
| agent     | `LOOM_AGENT` → `config.json` `agent` → `claude-code`                                                     |

For a remote server the hooks and the header helper need only one file
(`LOOM_CONFIG_FILE` moves it):

```json
{ "url": "https://loom.example.com", "token": "<writer JWT>", "namespace": "my-team" }
```

The MCP entry does not read it: its URL is the `server_url` install option, whose
default is `LOOM_BASE_URL`, else localhost. Set one of those too, or the entry sends the
file's token to localhost and the primer says so.

**Project scoping.** The project is the basename of the git toplevel
(`LOOM_PROJECT_NAME` overrides it). With the default `LOOM_PROJECT_SCOPE=tag`, every
project writes to one namespace and each row carries a `project:<name>` tag.
`LOOM_PROJECT_SCOPE=namespace` makes the project name the namespace instead. Rows also
carry `repo:<id>`, a hash of the normalised `origin` URL (`LOOM_REPO_REMOTE` overrides
the remote), so two clones of different repositories with the same directory name do
not share memory.

## The MCP entry and its header helper

The plugin's `.mcp.json` points at `<server_url>/mcp?toolset=<toolset>` and gets its
headers from `bin/loom-mcp-headers.mjs`. Claude Code runs the helper in the plugin root,
so the URL ends in `#${CLAUDE_PROJECT_DIR}`: Claude Code hands the helper the URL with the
project directory filled in, and never sends the fragment to the server. The helper
sends:

- `Authorization: Bearer <token>`, from the same lookup as the hooks except the install
  option (see above). With no token, the helper performs the same first-session sign-up
  as the SessionStart hook, because Claude Code connects the MCP entry while that hook is
  still running.
- `X-Loom-Code-Scope: <org>/<namespace>`, the code namespace the project's checkout
  resolves to. The server applies it to every `code.*` call that names no namespace, so
  the model does not have to pass one. `LOOM_MCP_CODE_SCOPE=off` disables it.

The checkout's repository is found from its remotes (`origin`, then `upstream`, then the
rest) through `code.repos`. A GitHub fork with no index of its own resolves to its
parent's index when you can read it. `LOOM_CODE_REPO=<owner>/<name>` or a bare name (or
the `code_repo` option, or `code_repo` in `~/.loom/config.json`) is the fallback candidate
after the remotes: it serves a session outside any git checkout, or a checkout with no
index, and gives it that repository's index and scope header. The helper's own lookup is bounded at 2.5 s; a miss costs only the header.
See [code index overview](../code-index/overview.md) and the
[MCP reference](../../reference/mcp.md) for how the server uses the scope.

## What the hooks do

The wiring is `hooks/hooks.json`; `hooks/HOOKS.md` has per-hook notes.

| Event                                             | Script                      | Effect                                                                                                                                                                                                                                                                                                                          | Off switch                                                                                                  |
| ------------------------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| SessionStart                                      | `loom-session-start.mjs`    | one budgeted `context.pack` primer (working-memory note, pins, profile, memories, decisions, procedures, recent runs), the code map for an indexed checkout, a line naming the project's saved session briefs, a task-inbox line (`task.counts`), and a few lines naming the collections the token can read (`collection.list`) | `LOOM_INJECT_CONTEXT=false`, `LOOM_CODE_MAP=false`, `LOOM_TASK_INBOX_LINE=off`, `LOOM_COLLECTIONS_LINE=off` |
| UserPromptSubmit                                  | `loom-prompt-pack.mjs`      | up to three `code.search` pointers for the prompt, only when the index does not abstain                                                                                                                                                                                                                                         | `LOOM_PROMPT_PACK=off`                                                                                      |
| UserPromptSubmit                                  | `loom-prompt-recall.mjs`    | one bounded `context.pack` recall for the prompt (memories, decisions, procedures), injected only when memory does not abstain                                                                                                                                                                                                  | `LOOM_PROMPT_RECALL=first` (default) / `on` / `off`                                                         |
| UserPromptSubmit                                  | `loom-run.mjs`              | opens the session's run (`run.begin`, `run_id = cc-<session_id>`, goal = the first prompt)                                                                                                                                                                                                                                      | `LOOM_RUN_LEDGER=off`                                                                                       |
| PreToolUse (Read, Grep, Bash)                     | `loom-code-shunt.mjs`       | the code shunt and the tracker shunt ([next section](#the-code-shunt))                                                                                                                                                                                                                                                          | `LOOM_CODE_SHUNT=off`, `LOOM_TRACKER_SHUNT=off`                                                             |
| PreToolUse (Edit, Write, MultiEdit, NotebookEdit) | `loom-file-context.mjs`     | what Loom knows about the file about to change (`memory.for_file`): your rules and the repository's shared rules and design decisions; once per file per 30 min                                                                                                                                                                 | `LOOM_FILE_CONTEXT=off`                                                                                     |
| PostToolUse (Loom tools)                          | `loom-mentor-hint.mjs`      | lifts a result's `hint` into context, once per hint code per session                                                                                                                                                                                                                                                            | `LOOM_MENTOR_HINTS=off`                                                                                     |
| PostToolUse (Edit, Write, MultiEdit)              | `loom-blast-radius.mjs`     | one line from `code.impact`: callers, tests reaching the change, co-change files not yet touched                                                                                                                                                                                                                                | `LOOM_BLAST_RADIUS=off`                                                                                     |
| PostToolUse, PostToolUseFailure (any tool)        | `loom-capture.mjs`          | one episodic observation per tool call (`observe.record`); a failed call is typed `error`                                                                                                                                                                                                                                       | none                                                                                                        |
| Stop, SessionEnd                                  | `loom-conversation.mjs`     | appends new chat turns to the conversation plane (`conversation.append`)                                                                                                                                                                                                                                                        | `LOOM_CAPTURE_CONVERSATIONS=false`                                                                          |
| Stop                                              | `loom-usage.mjs`            | posts the session's token usage to `cost.record_turns`                                                                                                                                                                                                                                                                          | `LOOM_USAGE=off`                                                                                            |
| Stop                                              | `loom-test-gate.mjs`        | for changes this session made, blocks "done" once with the tests `code.test_gate` says the change obliges                                                                                                                                                                                                                       | `LOOM_TEST_GATE=off`                                                                                        |
| Stop                                              | `loom-validate-gate.mjs`    | blocks once when `namespace.validate` finds errors or warnings on the memory keys this session wrote                                                                                                                                                                                                                            | `LOOM_VALIDATE_GATE=off`                                                                                    |
| Stop                                              | `loom-namespace-health.mjs` | on the first Stop, one line when `namespace.audit` finds a critical misuse of the namespace, pointing at the `loom-optimize` skill                                                                                                                                                                                              | `LOOM_NAMESPACE_HEALTH=off`                                                                                 |
| Stop, PreCompact                                  | `loom-working-memory.mjs`   | refreshes the working-memory note (`session:<project>`): goal, git status, files edited, last reply                                                                                                                                                                                                                             | `LOOM_WORKING_MEMORY=off`                                                                                   |
| Stop, PreCompact, SessionEnd                      | `loom-capture.mjs`          | crystallises the session (`observe.crystallize`)                                                                                                                                                                                                                                                                                | none                                                                                                        |
| PreCompact, SessionEnd                            | `loom-session-brief.mjs`    | revises the branch's session brief (`session.brief_save`, key `auto/<project>/<branch>`)                                                                                                                                                                                                                                        | `LOOM_SESSION_BRIEF=off`                                                                                    |
| SessionEnd                                        | `loom-run.mjs`              | closes the run (`run.complete`, `outcome: none`, the last reply as the final response)                                                                                                                                                                                                                                          | `LOOM_RUN_LEDGER=off`                                                                                       |
| SessionEnd                                        | `loom-session-memory.mjs`   | hands the session's turns to `memory.set_from_messages(defer=true)`, which extracts facts and writes the session summary                                                                                                                                                                                                        | `LOOM_SESSION_MEMORY=off`                                                                                   |

The plugin manifest declares `minServerVersion`, the oldest Loom server its hooks support. SessionStart reads the server's `version` from the `GET /v1/health` answer it already requests and, when that version is older, prints one `loom:` line naming both versions and asking for a server upgrade. The session goes on either way; an unreachable server, an answer other than 2xx, a server that reports no `version`, a version that is not SemVer or a manifest it cannot read prints nothing. The `loom-status` skill (`bin/loom-client-status.mjs`) reports the same as a warning. The minimum rises with the release that first serves what a hook needs, so between that hook's merge and that release a server of the previous release gets no warning.

**Why the run closes with `outcome: none`.** The hook knows the session ended, not
whether it succeeded, and it never guesses which memories the agent relied on. An agent
that knows its outcome calls `run.complete` itself.

**What is stored where.**

- Conversation turns are stored only when the namespace has conversation logging on
  (off for a new namespace; `conversation.set_logging`). `conversation.append` succeeds
  even when it drops a turn. See [conversations and sessions](../conversations-and-sessions.md).
- Raw observations become durable memories only when the server runs the
  `observe_extract` worker (`LOOM_OBSERVE_EXTRACT_ENABLED=true`, which needs LLM and
  embedder credentials; see [configuration](../../reference/configuration.md#extraction)).
- Session memory is independent of logging: `memory.set_from_messages` writes facts and
  a summary, and keeps the turns it was given as their source evidence.

**Safety.** A hook never blocks the agent on the network.

- Writes are fire-and-forget; slow ones (conversation, session memory, session brief) go
  to a detached flusher (`hooks/loom-flush.mjs`) that outlives the hook.
- Every request aborts after 5 × the `/v1/health` round trip, clamped to 3–8 s and cached
  six hours per server. `LOOM_HOOK_TIMEOUT_MS` and `LOOM_HOOK_EXIT_MS` override it.
- Errors are swallowed. Only the primer waits for a body; if it fails, the session starts
  without it. With no token or no server, every hook exits 0.

**SessionEnd budget.** Claude Code shares about 1.5 s across all SessionEnd hooks. The
plugin's five entries each declare `timeout: 10` so the last one is not cancelled.

### Recalled content and local data

The primer, the per-prompt recall, the saved-brief list, the file rules injected before an
edit (`loom-file-context`) and the tracker items the code shunt quotes when it answers a
GitHub issue search can carry other people's writes: team file rules live in the
repository's code namespace, where every member writes, and anyone who can file an issue
writes its title. Each item is labelled with its writer (`by=` in recall, the author on a
team file rule), and the plugin wraps every one of these in a `<loom-recalled-data>` block
that says it is data and not instructions. Each block's tags carry an id,
`<loom-recalled-data id="…">` and `</loom-recalled-data id="…">`, derived from the block's
text under a secret kept in `~/.loom/recall-key` (created once, `0600`), and the note says
only that closing tag ends the block: the text's writer cannot compute the id, so stored
text cannot write the block's end, and the same text keeps the same id, so the primer
stays cacheable across sessions. The plugin also
rewrites the tag name inside the text, including common lookalike spellings, to
`[loom tag removed]`. Only procedures the caller wrote are injected (a teammate's still
answers an explicit `procedure.match`). Loom cannot control what an agent then does:
keeping tool authority independent of retrieved text is the client's job.

The hooks' temp files (turn journal, cursors, stashes, flush jobs, and the code shunt's
decision log, pending batches and denial counts) are created `0600`, never written through
a symlink or into another user's file, and never read from a planted symlink, a hard link
or another user's file; the turn journal and stashes are removed at SessionEnd. What the
hooks redact before sending is in [data handling](#data-handling).

### Tuning knobs

Beyond the off switches in the table:

| Variable                       | Default                   | Effect                                                                                     |
| ------------------------------ | ------------------------- | ------------------------------------------------------------------------------------------ |
| `LOOM_CONTEXT_TOKEN_BUDGET`    | `1200`                    | token budget of the SessionStart primer                                                    |
| `LOOM_CODE_MAP_TOKEN_BUDGET`   | `0`                       | token budget of the code map in the primer; `0` leaves the map out                         |
| `LOOM_CONTEXT_RUNS`            | `false`                   | include recent runs in the primer; `LOOM_CONTEXT_RUNS_LIMIT` (8) bounds them               |
| `LOOM_PROMPT_RECALL_MIN_CONF`  | `0.5`                     | minimum `retrieval_confidence` before the prompt recall injects anything                   |
| `LOOM_PROMPT_RECALL_MAX_CHARS` | `1200`                    | cap on the injected recall                                                                 |
| `LOOM_PROMPT_PACK_TOP_K`       | `3`                       | pointers per prompt (`LOOM_PROMPT_PACK=off` turns the pack off)                            |
| `LOOM_FILE_CONTEXT_TTL_MIN`    | `30`                      | minutes before the same file's context is fetched again                                    |
| `LOOM_BLAST_DEPTH`             | `1`                       | `code.impact` depth for the blast-radius line                                              |
| `LOOM_TEST_GATE_DEPTH`         | `2`                       | hops the test gate follows (1–3)                                                           |
| `LOOM_OBSERVE_DEDUPE_S`        | `60`                      | a repeated read in this window is not observed again; `0` disables                         |
| `LOOM_OBSERVE_SKIP_TOOLS`      | a built-in list           | comma-separated tool names (with `*` globs) that produce no observation                    |
| `LOOM_PLUGIN_SCRATCHPAD`       | unset                     | `1` appends each turn's one-line conclusion to the run's `scratchpad` working-memory block |
| `LOOM_SESSION_BRIEF_KEY`       | `auto/<project>/<branch>` | the key the session brief is saved under                                                   |

`integrations/claude-code-plugin/README.md` documents the remaining knobs.

## The code shunt

Inside an indexed checkout, `loom-code-shunt.mjs` denies bulk reads and names the op to
call instead: an identifier goes to `code.symbol`, a literal or regex to `code.grep`,
words to `code.search`. The index answers with the enclosing unit and its callers, not a
whole file.

| Tool call                                                                     | Denied when                                                                           |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `Read` of a whole file                                                        | the file is indexed, unmodified, and longer than `LOOM_SHUNT_MAX_LINES` (800)         |
| `Read` with `offset` / `limit`, or a `sed -n` / `head` / `tail` / `awk` slice | the span exceeds `LOOM_SHUNT_READ_CAP` (0 = off); at most twice per file per session  |
| `Grep` over a directory; `rg`, `grep -r`, `cat` in Bash                       | the scope is inside the indexed tree                                                  |
| a grep for one symbol name in one file                                        | once per symbol per session, with the exact `code.symbol` call                        |
| `gh issue list --search`, `gh search issues`                                  | once per query, answered by `tracker.duplicate_check` with the closest reported items |

Everything else passes: targeted reads, single-file greps, pipelines, files you modified
or have not committed, unindexed repositories. The same command passes on the next try
after a symbol or tracker-shunt denial.

Staleness does not turn the shunt off: an unmodified file is answered as of its indexed
commit, and the deny text names the commit. To search your working tree instead, set
`local_first` / `LOOM_CODE_LOCAL_FIRST=on`; the shunt stands down and the primer skips the
code map.

Repository detection is cached ten minutes. A lookup that does not answer within
`LOOM_SHUNT_RESOLVE_TIMEOUT_MS` (8 s) is retried after a minute rather than cached as
"not indexed". Decisions are logged to `$TMPDIR/loom-shunt-log-<hash of the uid>.log`
(`LOOM_SHUNT_LOG=off` disables) and reported to the server as `code.outcome` rows
(`LOOM_SHUNT_REPORT=off` disables). `LOOM_SHUNT_GREP=off` and `LOOM_SHUNT_BASH=off` exempt
the Grep tool and the Bash tool respectively.

`claude --bare` skips hooks, so none of this runs there; the
[grep shim](grep-shim.md) covers that case. The operations are described in
[code index operations](../code-index/operations.md).

## Skills

| Skill                      | Loads when                                                               | Does                                                                                                                                                                                                       |
| -------------------------- | ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom-code`                | about to grep, glob or open files in an indexed repository               | which `code.*` op replaces the reflex and how to read its answer                                                                                                                                           |
| `loom-memory`              | a fact is stated, decided or corrected; the end of a substantial task    | what is durable memory and what is scratchpad, how to write a fact a later session can trust, the end-of-task review                                                                                       |
| `loom-mentor`              | a hint names a plane or op you do not know; "what can Loom do"           | the situation → plane → op map and what each hint code means                                                                                                                                               |
| `loom-optimize`            | "optimize my namespace", "memory search is bad", a namespace-health line | `namespace.audit`, a plan the owner approves, `memory.move` with redirects ([guide](../../guides/optimize-namespace.md))                                                                                   |
| `loom-session`             | "save this session", "resume", "where was I on this branch"              | lists the saved briefs, restores the one the user picks (the brief with the client's compaction summary, or with `--transcript` every recorded turn as well), or saves one; it never loads a brief unasked |
| `loom-status`              | a tool says unauthenticated, the primer is missing, "is Loom working?"   | checks the Node runtime (installs it only on your yes), runs the client check, names the one fix                                                                                                           |
| `improve-loom-for-my-repo` | the end of a session in which the code tools missed                      | turns the session's misses into reproducible findings for maintainers                                                                                                                                      |

`loom-session` runs `bin/loom-brief.mjs` (`list`, `get [N | KEY] [--transcript]`,
`save`); nothing in it runs on its own.

## Checking it works

- After a few tool calls, the dashboard shows the session's observations and,
  with logging on, its conversation. `run.get` on `cc-<session_id>` replays the run.
- `/loom:loom-status` runs `bin/loom-client-status.mjs` from the plugin: the server it
  would call, which lookup supplied the token and whether `~/.loom/config.json` names
  another server, whether the token is accepted and can write the namespace, and capture
  batches that never reached the server. It needs nothing but the plugin.
- `loom doctor --client` (the operator CLI, from a source checkout or the server image)
  adds the Claude Code and Codex wiring checks.

## Troubleshooting

Start with `/loom:loom-status`; it names the cause in one line. It runs its checks with
`sh`: on Windows without Git Bash, run `node <plugin root>/bin/loom-client-status.mjs`
instead.

| Symptom                                                                     | Cause                                              | Fix                                                                                                                     |
| --------------------------------------------------------------------------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `node: not found` from a hook                                               | the installed plugin predates its Node launcher    | `claude plugin update loom@loom`                                                                                        |
| a hook prints a `loom:` line about Node.js 18+                              | the launcher found no Node 18+, or only an old one | `LOOM_NODE=/path/to/node`, or the `loom-status` skill; restart                                                          |
| nothing captured on Windows; `claude --debug` shows `loom: no node on PATH` | no `node` on the `PATH` Claude Code starts with    | install Node.js 18+ on the system `PATH` (the header helper runs through `cmd.exe`, which only finds that one); restart |
| MCP server "needs authentication"                                           | no Node for the header helper, or no token found   | the `loom-status` skill names which                                                                                     |
| no primer, nothing captured                                                 | server unreachable, or no token found              | `loom-status`; check `~/.loom/config.json`                                                                              |
| tools answer, captures do not                                               | the token is viewer-only on the write namespace    | mint a writer token                                                                                                     |
| Conversations page empty                                                    | conversation logging is off for the namespace      | the **Record conversations** switch on the Conversations page, or `conversation.set_logging`                            |
| a hook added in a new release never fires                                   | the installed plugin is older than the server      | `claude plugin update loom@loom`                                                                                        |
| every tool listed twice                                                     | a user-scope `loom` MCP entry beside the plugin    | `claude mcp remove loom`                                                                                                |
| reads denied in a repo you work on locally                                  | the checkout is indexed and the file is unmodified | `--config local_first=true` or `LOOM_CODE_LOCAL_FIRST=on`                                                               |
| `Hook cancelled` at exit                                                    | SessionEnd hooks ran past the client's budget      | update the plugin (its entries declare `timeout: 10`)                                                                   |

## Other clients

`hooks/hooks.generic.json` maps the same lifecycle events to the same scripts for any
client that runs a command with the hook payload as JSON on stdin; replace
`${LOOM_PLUGIN_DIR}` with the plugin directory's absolute path. The scripts probe several
common payload field names, and read the client name from their third argument
(`… PostToolUse codex`) or `LOOM_CLIENT`; unnamed, they assume Claude Code. For a client
that can only export a transcript, see
[transcript capture hooks](index.md#transcript-capture-hooks).

## Related

- [Codex plugin](codex.md): the same scripts with Codex wiring.
- [MCP clients](mcp-clients.md): connecting without the plugin.
- [Agent integration guide](../../guides/agent-integration.md).
- [Operations reference](../../reference/operations.md): every op the hooks call.
