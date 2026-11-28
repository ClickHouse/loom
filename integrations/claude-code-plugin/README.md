# Loom for Claude Code

For tool discovery, default code-tool use and hook-versus-agent call counts, see
[Claude does not call Loom tools](../../docs/features/integrations/claude-code.md#claude-does-not-call-loom-tools).

Shared long-term memory and code intelligence for Claude Code, self-hosted on
ClickHouse. The MCP tools save and recall decisions, preferences and corrections
across sessions and teammates. Lifecycle hooks capture each session with no model
call and no trigger phrase: each tool action is recorded as it happens, errors
are typed for triage, and the session becomes durable memory at compaction and
exit. In a Loom-indexed repository, the code tools answer where-is, who-calls and
blast-radius questions with line-accurate units instead of whole-file reads.

The tools and the hooks share one identity (org, namespace, agent) and the same
conflict detection, blacklist and erasure. What the plugin sends, and where, is
under [data handling](#data-handling).

The plugin also mounts the MCP tools: `.mcp.json` points at
`${user_config.server_url}/mcp?toolset=coding&schema_profile=plugin&mode=${user_config.mode}` (the
`server_url` and `mode` plugin options; `server_url` defaults to
`$LOOM_BASE_URL`, else `http://localhost:7777`; the `plugin` profile lists every
tool of the toolset in the compact shapes and sends one paragraph of startup
instructions, since the hooks carry the
rest of the routing; a server that does not know
the profile answers with its default listing), and its `headersHelper`
(`bin/loom-mcp-headers.mjs`) sends `Authorization: Bearer <token>` from
`~/.loom/config.json`, then `~/.loom/token`
([where the token is read](#where-the-token-is-read)). The helper also sends `X-Loom-Code-Scope: org/namespace` — the repository this checkout resolved to (the shunt's cache, else one bounded lookup) — and the server applies it to every `loom_code_*` call that names no namespace, so the model cannot land on the deployment's default repository by forgetting the primer's scope line. `LOOM_MCP_CODE_SCOPE=off` disables it.

`coding` is the coding-agent toolset: 23 tools (save /
revise / read / delete, the working-memory scratchpad, `context.pack`, the
eight code tools, the ranged read and the pre-edit context, the duplicate
check, bisect candidates, PR check and blast
radius, `loom_search`, `loom_activate`, `loom_op`), about 6k
tokens per `tools/list` in the plugin's compact shapes, of which a session loads two code tools up front; every other op runs through `loom_op {op, arguments}`. The
`mode` plugin setting narrows the listing to one plane ([modes](#modes)). A session that wants another roster, such as the tracker plane, mounts a second MCP entry on it (`?toolset=tracker`). See
[MCP clients](https://loom.clickhouse.com/docs/features/integrations/mcp-clients/).

> **Bare mode skips hooks.** `claude --bare` (chosen by some agent harnesses for prompt-cache and cost reasons) does not run PreToolUse hooks, so the token shunt and the outcome capture in this plugin do not fire there. A harness that wants the shunt under bare mode needs the SDK backend's hook path instead.

## Modes

The `mode` option installs one of Loom's planes alone, so a session pays only for
the plane it uses: `claude plugin install loom@loom --config mode=code`.

| Mode             | The hooks                                                                             | The MCP entry lists                          |
| ---------------- | ------------------------------------------------------------------------------------- | -------------------------------------------- |
| `both` (default) | capture the session, recall memory and answer from the code index                     | 23 tools: every tool of the `coding` toolset |
| `memory`         | capture the session and recall memory; no request reaches the code index              | 10 tools: the listing without its code tools |
| `code`           | answer from the code index; nothing of the session is captured, recalled or extracted | 13 tools: the code tools alone               |

- **`code`** is for a session that wants the index and no memory. The hooks
  record no tool call, chat turn, brief or note and hand the server no session
  to extract facts from, so the server makes no model call for the session.
  They read no memory either: no primer, no recall, no file memories.
- **`memory`** is for a repository with no index, or a session that wants only
  what was learned before. No `loom_code_*` tool is listed and no hook sends a
  `code.*` request: no redirect of a read or grep, no code pointers at the
  prompt, no blast radius, no test gate. The file lookup covers your own
  namespace; a repository's team rules need the index, so they need `both`.
- **Every mode keeps the run ledger**: the session's first prompt, its last
  reply and its token usage ([data handling](#data-handling)).
  `LOOM_RUN_LEDGER=off` and `LOOM_USAGE=off` turn those off.
- **The three skills are listed in every mode.** A skill's description is
  fixed text the client loads at start.
- **The listing needs a server that reads `?mode=`.** An older server ignores
  it and lists both planes; the hooks follow the mode either way.
- **Other clients** set `LOOM_MODE` for the hooks (the option outranks it
  under Claude Code). The Codex plugin's MCP entry is fixed and lists what it
  always did.

[HOOKS.md](hooks/HOOKS.md#modes) names each hook script's plane.

## Data handling

The hooks and scripts send data only to the Loom server the plugin is configured
with (the two skills below are the exceptions). Two terms first: the plugin
_redirects_ a bulk file read or grep by answering it from Loom's code index
instead, and a `gh` issue search by answering it from Loom; a _namespace_ is the
space on the server that your token writes to. The hooks and scripts send:

- prompts, tool calls' input and output, and the paths of files read or edited;
- the symbol name from a grep that the plugin redirects to Loom's code index,
  and the search text of a `gh issue list --search` or `gh search issues`
  command it redirects to Loom, with the repository that command names;
- for each bulk file read or grep in an indexed repository that the plugin
  redirects or lets through, the tool name, file path and line range, and an
  estimate of the tokens it kept out of the context or let in
  (`LOOM_SHUNT_REPORT=off` stops it);
- chat turns and session briefs (the compaction summary and a log of asks and
  replies, commands, files and web addresses), stored only when the namespace
  has conversation logging on;
- up to the session's last 200 turns, which the server keeps as the source of
  the facts it extracts whatever the logging setting is, with up to 80,000
  characters of the session's Loom tool arguments and results
  (`LOOM_SESSION_MEMORY=off` stops it);
- the file paths and diff outlines of uncommitted changes (a diff's outline is
  its file paths, hunk line ranges and usually the enclosing function's name; no
  other part of a changed line is sent), a record of the session's first prompt
  and last reply, a note with the goal, git status, files edited and last reply,
  token usage and the model name;
- the repository's identity (each remote's owner and name, the branch and `HEAD`
  commit) and the working directory's absolute path;
- what the agent passes to Loom's tools (memories, search and code queries);
- on first use with no credential, the OS user name and host name; in agent
  mode (an automated agent whose platform gives it an identity token, set in
  `LOOM_WORKLOAD_TOKEN` or `LOOM_WORKLOAD_TOKEN_FILE` with `LOOM_CLIENT_ID`),
  that identity token and `LOOM_CLIENT_ID`, exchanged for a Loom token.

The `mode` option narrows the list ([modes](#modes)). In `code` mode the hooks
send no tool call's input or output, no chat turn, no brief, no note and no
session for the server to extract facts from. They still send each prompt's
text as a code-index search (`LOOM_PROMPT_PACK=off` stops it), the record of
the session's first prompt and last reply with token usage and the model name
(`LOOM_RUN_LEDGER=off` and `LOOM_USAGE=off` stop those), and the code-index
items above: a redirected grep's symbol, the read and grep records, diff
outlines and the repository's identity. In `memory` mode they send nothing to
the code index: no redirect, no read or grep record, no diff outline.

A client-side redaction removes common credential shapes from what the hooks
send; it is best effort, not a guarantee, and MCP tool arguments and a brief the
`loom-session` skill saves are sent without it. What is sent, with each item's
off switch, is listed in
[data handling](https://loom.clickhouse.com/docs/features/integrations/claude-code/#data-handling).

The server keeps this in its own databases (ClickHouse, and Postgres when the
operator stores memories there) and passes text to an LLM or embedding provider
only if its operator configured one
([models and keys](https://loom.clickhouse.com/docs/getting-started/models-and-keys/));
deleting data is covered in
[retention and erasure](https://loom.clickhouse.com/docs/features/memory/retention-and-erasure/).
Users whose grants allow them to read a stored row, such as teammates, and the
server's operator can read it
([namespaces and sharing](https://loom.clickhouse.com/docs/features/namespaces-and-sharing/)).

The plugin's hooks and its MCP header helper (the script that adds the token to
Loom's MCP requests) run `node`, `git` and `grep` on your machine: `git` reads
the checkout's remotes, branch, `HEAD` commit, status and diffs, and `grep`
searches uncommitted files for the pattern of a grep the plugin redirects to the
code index. The `loom-session` skill's brief script also runs `git log` for the
commits since the last brief. The test gate (the hook that names the tests of a
file you change) runs no Python: for a pytest test it picks the
interpreter of the checkout's virtualenv, the active one (`VIRTUAL_ENV`) or a
`python3` or `python` on `PATH`, checking that the interpreter's file exists
without running it.

The plugin keeps the token it saves (a copy of the one the hooks were given, or
a self-signup token, in `~/.loom/config.json`; in agent mode the exchanged one in
`~/.loom/agent-token-<hash of LOOM_CLIENT_ID>.json`) and its other state in
`~/.loom/`, mode 0600, until you remove it.

Its files in the system temp directory, which can hold prompt and reply text,
are also mode 0600. After a tool call and at the end of a reply one of them
holds the event as the client sent it, tool input and output included and not
yet redacted, until the process that records it has finished. A queued write or
event whose process stopped before sending it stays until you replay or delete
it; `loom-status` counts these and names the directory. A write the server
refuses, or that cannot reach the server, is
discarded. A batch of the per-file read and grep records above that fails to
send is discarded too. Chat turns that fail are sent again at the end of the
next reply, and a brief's events at the next brief save. At session start the
plugin tries to remove other state files untouched for a day. They can remain
longer, especially when no new session starts. The log of which tool calls the plugin redirected
or let through (no prompt or file content) stays until you remove it
(`LOOM_SHUNT_LOG=off` stops it).

Two skills reach other services when you run them. `loom-status` can install
Node.js after you agree: on Linux, as root or with passwordless sudo, it
installs curl if missing, downloads and runs NodeSource's setup script and
installs nodejs (apt, dnf or yum), or runs `apk add --no-cache nodejs`; on macOS
it runs `brew upgrade node || brew install node` as you.

`improve-loom-for-my-repo`, after building its report, searches the
ClickHouse/loom GitHub issues for each finding's symbol or query without asking,
and files a public issue there or comments on an existing one only after you
approve; the issue holds the report as shown to you: the server version,
namespace, indexed commit and how much of the repository is indexed, and for
each finding a reproduction with its queries, symbols and file names.

SessionStart reads the user-scope MCP entries in `~/.claude.json`, on this
machine only, and warns once a day when one points at the same Loom server: that
entry registers a second Loom server beside the plugin's. The warning, which the
session sees, names the entry; nothing else from that file is sent. To remove
the duplicate, run `claude mcp remove <name> -s user`.

## What it wires

| Lifecycle event                                           | Script                      | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------------------------------------- | --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `UserPromptSubmit`                                        | `loom-record.mjs`           | The prompt's one process. Runs the two lookups the prompt waits for (`loom-prompt-pack`, `loom-prompt-recall`) in the hook itself and prints what they found as one `additionalContext`; writes the run's begin to the session's run state and hands the prompt's recording scripts (`loom-run`, `loom-namespace-health`'s audit) to a detached process and exits, as after a tool call.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `PostToolUse`, `PostToolUseFailure`, `Stop`, `SessionEnd` | `loom-record.mjs`           | Runs the event's recording scripts (`loom-capture`, `loom-conversation`, `loom-usage`, `loom-working-memory`, `loom-run`'s close, `loom-session-memory`, `loom-session-brief`) in one process. After a tool call and at `Stop` that process is detached and the hook exits, so the session does not wait for their requests. At `SessionEnd` it is the hook itself, which exits when they have returned: a host torn down when the client exits would end a detached process first.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `PostToolUse`                                             | `loom-capture.mjs`          | Records one episodic observation of the tool action (heuristic type, file list).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `PostToolUseFailure`                                      | `loom-capture.mjs`          | Same, typed `error` — the bug-triage signal (errors + the subsequent fix).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `SessionStart`                                            | `loom-session-start.mjs`    | **First session, no credential:** signs this machine up (see `LOOM_AUTO_SIGNUP`) and points the user to the claim page for the namespace; nothing to paste. Then: injects a token-budgeted context primer (profile + relevant memories) via `context.pack`, so the agent starts knowing your context. Beside it, one line names the checkout's code namespace and the repository's memory tag, or says why no index was looked up, so a session start that prints nothing is a fault unless `LOOM_CODE_MAP=false`. How to route a lookup is in the MCP entry's startup instructions on a toolset with a `coding` part; any other toolset, and a wiring whose entry the plugin does not mount (Codex, a hand-wired client), gets it in the primer. **On by default**, once per session; `LOOM_INJECT_CONTEXT=false` to disable.                                                                                                                                                            |
| `PostToolUse` (Read / Grep / Bash), `PreCompact`          | `loom-file-context.mjs`     | Asks `memory.for_file` what loom knows about a file the call read (its files facet) and attaches the rendered block to the read's result, at no model request: the first local read of a file in the checkout — `Read`, a `Grep` of one file, or a shell `cat` / `sed -n` / `head` / `tail` / `grep pattern file` — brings its memory, so a session that only reads, or that edits through the shell, gets it too. One lookup per session, agent context and file (a subagent's read does not use up the main agent's block); a compaction clears what was shown. A file with no memory adds nothing. Reads ask about at most three files per call and 40 per session, wait at most 1 s, and stop asking after three unanswered lookups.                                                                                                                                                                                                                                                  |
| `PreToolUse` (Edit / Write / MultiEdit / NotebookEdit)    | `loom-file-context.mjs`     | Before an edit of a file this agent context has not been shown, the same lookup and block — a constraint arrives **before** the edit, not as a review comment. A file shown more than 30 min ago is asked about again. Never a permission decision. `LOOM_FILE_CONTEXT=off` disables.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `PostToolUse` (loom tools)                                | `loom-mentor-hint.mjs`      | **The store's advice reaches the model**: a loom tool result may carry one `hint` — the plane this agent is not using, named from the call it just made (a `memory.get` that missed, a search that abstained where the code index answers, a stack trace written as a memory, a duplicate write). The hook lifts it out of the result JSON into additional context, once per code per session. No request of its own. `LOOM_MENTOR_HINTS=off` disables.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `PostToolUse` (Edit / Write / MultiEdit)                  | `loom-blast-radius.mjs`     | In a loom-indexed repo, diffs the edited file against HEAD and asks `code.impact` (depth 1) what the touched symbols reach. Injects **one line**: callers, tests reaching the change, and the co-change partner files this session has not touched — the sibling-file miss, surfaced while the edit is still open. Same touched set reported once per session. The first edit of a file in a session also carries the test gate's line (next row), which stands in for the radius's count of tests where it names the file's tests, so an edit gets one line. `LOOM_BLAST_RADIUS=off` disables the radius.                                                                                                                                                                                                                                                                                                                                                                                |
| `PostToolUse` (Bash / PowerShell)                         | `loom-test-gate.mjs`        | **The tests are named when the file changes.** After a shell command in a loom-indexed repo, asks `code.test_gate` about each file the command changed (a dirty path it names, written since the session began) and attaches **one line** per file with the command of the tests the index links to it, or of the file itself when it is a test: `Loom tests for src/a.py: .venv/bin/python -m pytest tests/test_a.py`. Once per file and once per test per session, on the tool result, so it costs no model request; an edit tool's call gets the same line from `loom-blast-radius.mjs`. A file with no such test gets the changed functions the index links no test to: `Loom tests for src/a.py: the index links no test to Router::match`. A comment-only change names nothing (**Tests at edit time**, below). `LOOM_TEST_GATE=off` disables; `LOOM_TEST_GATE_DEPTH` sets the hops (1–3).                                                                                          |
| `UserPromptSubmit`                                        | `loom-prompt-recall.mjs`    | **Default `first`**: one bounded recall on the first qualifying prompt of a session, including empty or abstaining answers. Clear named code-only questions skip default recall without consuming the attempt; mixed memory requests still qualify. `LOOM_PROMPT_RECALL=on` for every prompt, `off` to disable. One `context.pack` per prompt restricted to `memories`, `decisions`, `procedures` with the prompt as the query; injects the plane-tagged blocks the session has not seen, only when memory is not abstaining and `retrieval_confidence ≥ 0.5`, capped at 1,200 chars (`LOOM_PROMPT_RECALL_MAX_CHARS`). The memory-side sibling of the prompt pack: active retrieval — the model gets what memory knows about the prompt without asking — done per prompt, never per tool turn.                                                                                                                                                                                            |
| `UserPromptSubmit`                                        | `loom-prompt-pack.mjs`      | On by default (`LOOM_PROMPT_PACK=off` turns it off). One bounded lookup: `code.symbol`, `code.callers` or `code.impact` for an explicit named question, otherwise `code.search` (top 3, no bodies). Self-contained requests, local-only changes and explicit memory questions skip code retrieval. Search emits only where the index labels its own answer low-risk (or, with `LOOM_PROMPT_PACK_MIN_CONF` set, from that confidence), and without abstention; its pointers are candidates, and nothing tells the model to stop searching; typed answers preserve ambiguity and static-analysis limits. Named implementation or behavior questions request a whole definition body within 600 server-budget tokens; location/signature requests stay pointer-only and oversized bodies stay elided. No fallback request follows an empty typed answer. Pointers are deduplicated within the current answer, so later prompts can receive changed evidence.                                 |
| `Stop`                                                    | `loom-test-gate.mjs`        | **Holds a session that ran no test since its last code change.** With uncommitted changes **this session made** in a loom-indexed repo (the dirty paths the transcript shows an edit tool targeted or a shell command named, written since the session began, by the file's modification time; a tree left dirty before the session gates nothing, even when a command reads or runs its files), asks `code.test_gate` what the change obliges — the tests to run, in evidence order with a `run` command when the harness is derivable, and the touched definitions a coverage run measured unexecuted — and blocks the stop **once** per distinct change with a reason that names them. The second stop on the same change passes. By default any test run since the last code change lets the stop pass, and nothing is asked (**Stops that pass**, below); with `LOOM_TEST_GATE=block` only a run of the named tests does (**Answered stops**, below). `LOOM_TEST_GATE=off` disables. |
| `Stop`                                                    | `loom-validate-gate.mjs`    | **Compile green before done**: collects the memory keys this session wrote from the transcript, asks `namespace.validate` for a report scoped to exactly those keys, and blocks the stop **once** per distinct key set with the error and warning findings that name them (a conflict you opened, a fact whose `valid_to` has passed, an attestation your edit made stale) and each check's remedy. `info` findings never block; the second stop on the same keys passes. `LOOM_VALIDATE_GATE=off` disables.                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `Stop`                                                    | `loom-namespace-health.mjs` | **The store tells you when the namespace is misused**: once per session, one `namespace.audit` over the last day (no samples) for the session's namespace, asked by the first prompt's detached process so that a stop does not wait for it (a stop asks in place only when that process died before its answer); when it carries a critical finding — exact-key reads dwarfing searches, double-encoded values, rows the embedder gave up on, leases in the memory plane — you get **one line** naming the codes and `/loom-optimize`, once per session, at the first stop that finds the verdict, as a block-once reason (the one channel a Stop hook has to the model). Healthy namespace, no token, slow or down server: nothing. The same summary rides `context.pack` as the `namespace_health` section at session start. `LOOM_NAMESPACE_HEALTH=off` disables.                                                                                                                     |
| `Stop`                                                    | `loom-usage.mjs`            | Posts the session's own model usage (input, cache read, cache creation by TTL, output; deduped by message id, sidechains skipped) to `cost.record_turns`; the **server** prices it into `loom.agent_turn_usage` from the same table it prices its own upstream spend with. Turns "did loom make this session cheaper" into a standing series instead of a hand-run paired experiment. Needs a writer token. `LOOM_USAGE=off` disables.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `UserPromptSubmit`                                        | `loom-run.mjs`              | Opens the session's **run** on the first prompt (`run.begin`: goal = the prompt, `run_id = cc-<session_id>`, or the id `run.begin` answers instead; the prompt does not wait for that answer, so the SessionStart primer, the prompt's recall and a first tool observation keep the derived id, or on a resumed session the previous run's id). Later prompts are no-ops once a begin was answered or refused; after a timeout, a 408, 429 or 5xx the next prompt asks again with the same id, and SessionEnd closes it if no answer ever came. Until then the session's run-state file in the temp directory (readable by you only) holds that begin's body, which every retry re-sends: the prompt, redacted and cut to 8000 characters. One row per session in `loom.agent_runs`; every observation the capture hook records carries the same `run_id`, so `run.get` replays the session — goal, observations, memories written, cost. `LOOM_RUN_LEDGER=off` disables.                 |
| `SessionEnd`                                              | `loom-run.mjs`              | Closes the run (`run.complete`) with the transcript's last assistant text as `final_response` and `outcome: none` — the hook knows the run ended, not whether it succeeded, and never guesses which memories were used; that stays the agent's own `run.complete` call (`memories_used`, `reflection`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `PreCompact`                                              | `loom-capture.mjs`          | Crystallizes the session before the context window is dropped.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `Stop`, `PreCompact`                                      | `loom-working-memory.mjs`   | Writes the session's **working-memory note** — one `working_memory.set` per project (`session:<project>`, agent scope): the goal (first prompt), `git status --short --branch`, the files edited this session, the last reply — framed as a bookmark, not a source of facts (the next session's model answered a fact question from this line without searching). Deduped on content, so an unchanged turn writes nothing. `context.pack` loads working memory first in the SessionStart primer and Claude Code fires SessionStart again after a compaction, so the note written a moment before the drop is the first thing the agent reads after it. Not retrieval memory: never embedded or ranked. `LOOM_WORKING_MEMORY=off` disables. With `LOOM_PLUGIN_SCRATCHPAD=1` the Stop hook also appends the turn's one-line conclusion to the run's `scratchpad` block at run scope.                                                                                                        |
| `Stop` / `SessionEnd`                                     | `loom-capture.mjs`          | Crystallizes the finished session.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `PreCompact`, `SessionEnd`                                | `loom-session-brief.mjs`    | Revises the branch's **session brief**: the transcript since the last save becomes typed events (asks, replies, edits, commands with their descriptions, errors, reads, todo lists, PR/commit identifiers a command printed; zero model tokens) posted to `session.brief_save`, which condenses them under a budget and revises `auto/<project>/<branch>` — goal, status, next steps, decisions and why, what was tried and dropped, what was done, open questions, files, the rules you set, pointers. The `loom-session` skill restores one on request: you pick which (a number from `list`, a key, or a session) and restores it with the client's compaction summary (or `--transcript` for every recorded turn as well). Not retrieval memory: never embedded or ranked. Detached flusher; per-session cursor; a first save needs an ask and some work behind it. `LOOM_SESSION_BRIEF=off` disables.                                                                                |
| `SessionEnd`                                              | `loom-session-memory.mjs`   | Hands the session's real turns (last 200, bounded) to `memory.set_from_messages(defer=true)` once, so loom extracts the facts, writes the **session summary**, judges the outcome and distils reflections. The turns are kept as the facts' source evidence. A session with under 40 bytes (UTF-8) of user and assistant text is not sent, because the extraction call's fixed prompt outweighs what it can yield: a rule stated in it is not extracted, and it gets no session summary, outcome judgement or reflections. `LOOM_SESSION_MEMORY=off` disables.                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `Stop` / `SessionEnd`                                     | `loom-conversation.mjs`     | Appends new chat turns from the transcript to the conversation plane (→ the dashboard's Conversations page). Deterministic, every turn; per-session cursor + `msg_id`=transcript uuid dedup. Text blocks only — an image the user pasted becomes a `[image: <media type>]` placeholder beside its text, and a tool result that returned images leaves one `tool` turn `[image: N image results returned by a tool]`; the bytes never leave the machine. Off-switch: `LOOM_CAPTURE_CONVERSATIONS=false`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

**Team file rules.** The file-context hook reads two places in one `memory.for_file` request: your own namespace, and the repository's **primary code namespace** (its trunk branch's — `code-clickhouse` for every ClickHouse branch), named by the checkout's cached `code.repos` entry. A namespace you cannot read is skipped silently, so access follows repository access. Rules from the code namespace render as `Team rule (by alice, 3 days ago) [constraint]: … [id: <key>]` and design decisions (typed or tagged `decision`) as `Decision (by alice, 3 days ago) (pinned): … — source: <source_resource_id> [id: <key>]` (`(pinned)` only when someone pinned it, else no mark; `(source changed)` once its source document no longer says it), a newer rule the conflict check linked to an older one sits above it (`supersedes rule by …`, or `supersedes decision by …`), and one footer line says how to remove a wrong one. Every member of the repository may add a rule there (`memory.set` with `files` and the tag `constraint`) and revise or delete the rules they wrote; a maintainer may delete any — from an agent (`memory.delete` on the `[id:]` key) or from the dashboard (pick the code namespace under _show system_, open the memory, Delete). A refused delete answers a `hint` naming the author and what to do instead. `memory.set` fills `files` from the value when you do not pass it. Auto-extracted session facts never go to the code namespace.

**Tests at edit time.** The test gate names a file's tests on the tool call that changes it, where the agent is still working on the file. For a source file they are the tests the index links to it through a coverage run, its name, the test map or the call graph, or failing those up to four tests that call other definitions of the file, each of them present in the checkout, with the tests that have no known runner listed by path; for a test file, the file's own command. A file with no test to name in the checkout gets the changed definitions the index links no test to instead (`the index links no test to Router::match`), worded as the index's view, because its call graph is a floor. A file is asked about once per session, at its first change, so a function changed later in the same file is not named; a file the server gave no answer for is asked about once more, at the next call that names it; at most four files are asked about per tool call. A test is named once per session, so the test you edit after its source file adds no second line. A test the tool call's own command runs is not named on the result of that run: its command stands there whole, in this checkout, so `pytest tests/test_a.py::test_one`, `docker exec app pytest tests/test_a.py` and `echo "pytest tests/test_a.py"` still get the line.

**Stops that pass.** By default the test gate holds the end of a session only when the session ran no test since its last code change. Whether a session ran the right tests, and how they went, cannot be read reliably from its shell commands: a session that runs the whole suite, compares against a stashed tree in one command or pipes pytest through `grep` has run the tests in a way no fixed rule reads, and a gate that holds the stop on that reading holds sessions that did the work. So the stop passes, before anything is asked of the server, when the transcript holds a shell command that starts a test runner and finished no earlier than the newest write among the code files the session changed. A changed file that is documentation or prose by its name is left out of that comparison, so a note written after the run holds nothing; so is a deleted file. Such a name ends in `.md`, `.mdx`, `.markdown`, `.rst`, `.adoc` or `.asciidoc`, or is `CHANGELOG`, `CHANGES`, `HISTORY` or `NEWS`. A `.txt` file is one when it has one of those four names or is `README.txt`, `LICENSE.txt`, `NOTICE.txt` or `AUTHORS.txt`, or lies under a `docs` or `doc` directory with no `requirements` or `constraints` in its path below it (`docs/releases/5.2.txt`); any other `.txt` file is code, as a requirements file or a test fixture is. A `git stash pop` in a later command than the run rewrites the changed files, so that stop is held. A runner is `pytest`, `unittest`, `tox`, `nox`, `jest`, `vitest`, `mocha`, `nextest`, `rspec`, `phpunit`, `ctest` or `clickhouse-test`, or `go`, `cargo`, `npm`, `pnpm`, `yarn`, `bun`, `deno`, `make`, `mvn`, `gradle`, `gradlew`, `dotnet`, `swift`, `bazel`, `mix`, `rake` or `node` with an argument in which `test` or `tests` follows no letter and no lower-case letter follows it (`make test-unit`, `node --test`, `gradle :app:test`, `gradlew testDebugUnitTest`, but not `make latest`, `go vet ./internal/testutil/...` or `cargo build -p testcontainers`); or `make`, `npm`, `pnpm`, `yarn`, `bun` or `rake` with a target or a script whose name starts with `test` (`make testall`, `npm run testci`); or `gradle` or `gradlew` with a task that ends in `Test` after a lower-case letter (`gradlew integrationTest`, but not `gradlew compileTestJava`). A name after `install`, `uninstall`, `add`, `i`, `ci`, `get`, `remove`, `rm`, `update`, `upgrade`, `download` or `require` in the same simple command is a package, not a run (`pip install -U pytest`, `npm i -D @types/jest`, `yarn remove jest`), and neither the word after `-x` (`gradle build -x test`) nor `test.skip` (`mvn package -Dmaven.test.skip=true`) is one. A backslash at a line's end joins the next line to the command. Two shapes are misread: a build tool's target that has a verb's name hides the run after it, so `make install test` and `mvn clean install test` are taken for no run and that stop is held once; and a target that only builds counts when its name reads as a run (`gradlew assembleAndroidTest`, `make testdata`). The command's output is not read: a run that failed, ran other tests or ran in a container passes, and so does a command that only names a runner, such as `grep -rn pytest .`. A session that wrote a changed code file after its last such command, or that ran none, is held once per distinct change with the tests to run and the functions the index links no test to; a harness the list does not name is not seen, and that stop is held once too. A latest prompt that ends with "Do not run the tests" passes when the gate names tests the checkout has. With no transcript the stop is not held. In a Codex rollout the session's changes are read from `apply_patch` and from the shell tool under each of its names (`exec`, `exec_command`, `shell`, `shell_command`, `container.exec`), so a rollout that changed a file through any of them is held, with `LOOM_TEST_GATE=block` too. A team that wants the stop held until the named tests ran on the final tree sets `LOOM_TEST_GATE=block` in the environment Claude Code starts in; the two paragraphs below describe that mode, which names nothing at edit time.

**Unlinked and untested definitions.** The index's call graph is a floor: a test may reach a definition the graph holds no edge to, through a fixture, a string-dispatched entry point or a public API. The reason of a held stop therefore lists a touched definition with no linked test as the index's view, `the index links no test to: … (the tests named here may still reach it)`, and such a definition alone holds no stop and asks for no test. Only a definition an imported coverage run measured unexecuted reads `measured unexecuted at <commit>: …` and asks for a test.

**Answered stops.** With `LOOM_TEST_GATE=block`, the test gate does not block, and so names no unexecuted definition, when it names tests, each of them is in the checkout and the transcript already answers the ask to run them, in one of two ways. Your latest prompt ends with the sentence "Do not run the tests" (also `don't` or `never`, and `tests`, `any tests` or `the test suite`). Or the transcript shows a Bash command that ran pytest on each named test, by its file or a directory above it that is below the checkout root, and whose output holds a whole summary line for each pytest in it: one that counts tests failed or passed and none deselected. A run that failed answers, since the session has read its failures; a collection that stopped at an error (`1 error in 0.12s`) does not, and neither does a line that counts a failure or an error when a pytest in the command has `--exitfirst`, `--maxfail` or a short flag holding `x` (`-x`, `-qx`, also `-rx`), since that run stopped before the files after it. No output answers that holds the line pytest prints above the summary of a run it ended early (`stopping after 1 failures`, `Interrupted: …`), which also shows a limit or `--sw` set in a configuration file or in `PYTEST_ADDOPTS`; `| tail -1` cuts that line, and such a run then answers. The command has no `--ignore`, `--deselect`, `--lf`, `--sw` or `--continue-on-collection-errors` and no quote in pytest's arguments; its runs count from the first pytest after which it runs only pytest, `tail`, `head`, `grep` and `echo` and holds no backquote and no redirect to a file other than `/dev/null`, so the second run of `git stash; pytest tests; git stash pop; pytest tests` answers and no run of `pytest tests; git stash; pytest tests; git stash pop` or of `pytest tests; git diff --stat` does; the transcript shows no edit-tool call and no other Bash or PowerShell call after that; no tracked file is deleted; and no uncommitted or untracked file was modified later. A run of the whole checkout names no path below the root, so a bare `pytest` and `pytest .` do not answer. Before pytest in its simple command only variable assignments, `env`, `timeout`, `uv run` or `poetry run`, an interpreter's `-m` and a path may stand: `python -u -m pytest tests` and `docker exec app pytest tests` do not answer. Runs in several commands add up only while each later command holds nothing before the first of its runs that count but what launches it: one that does anything else first, a `cd` included, answers for its own runs alone. The word after a pytest flag written without `=` is read as that flag's value, not as a path that ran, unless the flag is a cluster of `-q`, `-v`, `-x`, `-s` and `-l`: `pytest --no-header tests` does not answer. Quotes in the rest of the command are not read, so a `|`, `;` or `>` inside one counts as if unquoted: a run piped into `grep -E "passed|failed"` does not answer.

The scripts are standalone Node ESM (`.mjs`) with **zero dependencies** — no Loom
SDK, no `node_modules`. They run on any Node ≥ 18 (global `fetch` +
`AbortSignal.timeout`).

**Which Node.** Claude Code runs every hook as `node` with the script's path as
its argument, spawned with no shell, and the MCP header helper as
`node "${CLAUDE_PLUGIN_ROOT}/bin/loom-mcp-headers.mjs"`: both take the `node` on
the PATH Claude Code starts with, on every OS, with or without Git Bash. That
PATH is the agent's own: an agent started from the Dock or a desktop launcher,
or a version manager that sets PATH only in interactive shells, leaves `node`
off it (on macOS a Dock launch gets only `/usr/bin:/bin:/usr/sbin:/sbin`, with
no Homebrew or nodejs.org Node): set `PATH` in the `env` block of
`~/.claude/settings.json` to the whole search path with that Node's directory
first, such as `"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"`. The
Codex wiring, `hooks.generic.json` and the skills' commands start Node through
`sh bin/loom-node`, which runs the first Node 18+ among `LOOM_NODE`, `node` on
PATH, a version manager's default (or its newest install when the default
cannot be read or is older than 18), and the usual install directories. With
none, it exits 1 with one line naming the Node it found too old, or none, and
`LOOM_NODE`
([requirements](https://loom.clickhouse.com/docs/features/integrations/claude-code/#requirements)
has the platforms).

## Safety model: fire-and-forget, hard-bounded

Loom's server is **remote** (not localhost). A capture hook fires on the agent's
prompt boundary, so a slow or hung request must **never** wedge the agent.
Every write path is:

- **unawaited** `fetch(...)` (detached promise), with
- `AbortSignal.timeout(<bound>)` (the request hard-aborts at the request
  bound: 3 s by default, or 5× the round trip SessionStart measured against the
  server, clamped to 3–8 s; `lib/hook-bounds.mjs`), and
- `.catch(() => {})` (every error swallowed), and
- an **unref'd** `setTimeout(() => process.exit(0), <bound>)` so the process
  self-terminates regardless of socket state.

Claude Code does not wait for those writes at a prompt, after a tool call or
at Stop: it runs `loom-record.mjs`, which writes the payload to a private job file, starts
one detached process that runs the event's recording scripts, and exits
(`hooks/HOOKS.md`, "What every hook guarantees"). That process outlives the
client's process but not a machine or container torn down when the client
exits, so at SessionEnd `loom-record.mjs` runs the scripts itself and exits
when they have returned. The bounds above hold inside those processes, and for
Codex and a hand-wired client, which start each script themselves.

The one path that needs a response body — the SessionStart primer — is still
hard-bounded by the same `AbortSignal` and degrades to "no primer" on any
failure. Measured behaviour of a script started on its own against an
unreachable server, at the default 3 s bound:

```
PostToolUseFailure → exit 0 in ~0.7s   (detached fetch abandoned)
Stop (crystallize) → exit 0 in ~0.7s
SessionStart primer (off) → exit 0 in ~0.15s, no output
SessionStart primer (on), hung server → exit 0 at ~3s, no output (bounded)
no token configured → exit 0 in ~0.5s, no network
```

The exit grace is the request bound (3 s by default), so a _hung_ server holds a capture script started on its own for up to that long; a refused or unresolvable one still fails in well under a second, and a reachable one exits the moment its request settles.

## Setup

Install either this plugin or the standalone `loom-code-skill`, not both:
both ship a skill named `loom-code`, and this plugin already carries it.

```bash
claude plugin marketplace add ClickHouse/loom   # or a local clone path
claude plugin install loom@loom --config server_url=https://<loom> --config token=<writer JWT>
```

Optional: `--config local_first=true` (or `LOOM_CODE_LOCAL_FIRST=on` in your environment) is the one switch for "I work from my local tree": the code shunt stands down entirely and grep/Read run on your working tree, whatever commit you are on. Off by default: the index answers every file you have not modified, however far behind upstream your checkout is, and only your modified files are read or grepped locally.

Both options are optional: without `--config`, Claude Code
prompts for them, and an empty answer leaves them unset — the server
URL falls back to `$LOOM_BASE_URL`, then `http://localhost:7777`, and the token to
`$LOOM_API_KEY`, `~/.loom/config.json`, `~/.loom/token`. A token given here is
stored by Claude Code (keychain where available) and reaches the hooks as an
environment variable ([where the token is read](#where-the-token-is-read)).

Already installed? Hooks land with the plugin version, not with the server:
a client installed before a hook was added keeps posting from the hooks it
has and never sees the new one. Update after a server deploy that mentions a
plugin change:

```bash
claude plugin update loom@loom
```

That's it — the hooks wire themselves (Claude Code injects `${CLAUDE_PLUGIN_ROOT}`;
no `settings.json` editing). All you supply is a **writer token**: export
`LOOM_API_KEY` or write it to `~/.loom/token` (one line). **Org and namespace are
read from the token's own claims** — nothing else to declare. The plugin's MCP
block (the Loom _tools_) gets the token through a
`headersHelper` (`bin/loom-mcp-headers.mjs`), which reads the two files
([where the token is read](#where-the-token-is-read)), and defaults the
server to `http://localhost:7777` — set `LOOM_BASE_URL` for a remote server, or run
`loom install --client claude-code`, which writes both into
`~/.claude/settings.json` `env`.

```bash
loom mint-token --subject you --org <your-org> --namespaces coding --roles writer > ~/.loom/token
```

### Where the token is read

| Reader                                 | Reads, in order                                                                                                                                                        |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| the hooks                              | `LOOM_API_KEY` → the `token` install option → the file `LOOM_API_KEY_FILE` names → `token` in `~/.loom/config.json` → `~/.loom/token` (with `LOOM_API_KEY_FILE` unset) |
| the MCP header helper (the Loom tools) | `token` in `~/.loom/config.json` (`LOOM_CONFIG_FILE` moves it) → `~/.loom/token`                                                                                       |

Claude Code gives a plugin's header helper neither the install option nor
`LOOM_API_KEY` or `LOOM_API_KEY_FILE` (measured on Claude Code 2.1.332). So the
first hook of a session saves the token the hooks were given (`LOOM_API_KEY`,
else the option, else the file `LOOM_API_KEY_FILE` names) to
`~/.loom/config.json` (0600), with the server URL beside it, when no token is
saved there or in `~/.loom/token`; a helper with no token waits up to 5 s for
that save, and the `loom` CLI and the SDK read the file too. A saved token is
never replaced: when the hooks use another one, from `LOOM_API_KEY` or the
named file, the Loom tools send the saved one and the SessionStart primer says
to save the hooks' token in `~/.loom/config.json`. The hook saves
`LOOM_API_KEY` or the named file's token, and the primer says that line, only
where Claude Code runs it for the plugin (`CLAUDE_PLUGIN_ROOT` set): not for a
hand-wired client, Codex or an agent.

### Hosted / remote Loom — one config file

Pointing the plugin at a remote server? Give the MCP entry the server at install
time (`--config server_url=https://loom.your-host.example`, or `LOOM_BASE_URL` in the
shell that starts Claude Code; the entry never reads `~/.loom/config.json`), and drop
a single `~/.loom/config.json` for the hooks and the header helper:

```json
{
  "url": "https://loom.your-host.example",
  "token": "<writer JWT>",
  "namespace": "your-namespace"
}
```

The plugin reads `url` / `token` / `namespace` / `org` from it — no shell
exports, no `settings.json` edits. Environment variables still take priority
(CI / one-off overrides). `chmod 600 ~/.loom/config.json` — it holds a token.

### Generic / non-plugin clients

Not on Claude Code's plugin system? Hand-wire `hooks/hooks.generic.json` into
your settings, replacing `${LOOM_PLUGIN_DIR}` with this directory's absolute
path.

### Environment (all optional — sane defaults + token-derived identity)

| Var                                   | Default                                                                                  | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_NODE`                           | _(unset)_                                                                                | Path to the Node binary `bin/loom-node` runs (the Codex hooks, `hooks.generic.json` and the skills' commands); it wins over the search. A path that is not Node 18+ fails them. Claude Code's hooks and header helper run the `node` on PATH.                                                                                                                                                                                                                                                                                                                                                                           |
| `LOOM_NODE_SYSTEM_DIRS`               | Homebrew, Linuxbrew, `/usr/local/bin`, `/usr/bin`, `/bin`, `/snap/bin`, `/opt/local/bin` | Colon-separated directories `bin/loom-node` searches last, after PATH and the version managers.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `LOOM_BASE_URL`                       | _(`~/.loom/config.json` → )_ `http://localhost:7777`                                     | Control-plane base URL.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `LOOM_API_KEY`                        | _(unset)_                                                                                | Bearer JWT for the hooks; the first session saves it for the MCP tools ([where the token is read](#where-the-token-is-read)). Falls back to `~/.loom/config.json`, then `~/.loom/token`.                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `LOOM_CONFIG_FILE`                    | `~/.loom/config.json`                                                                    | JSON config (`url`/`token`/`namespace`/`org`); env vars override it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `LOOM_ORG`                            | _(token's `org` claim)_ → `default`                                                      | Org segment of the identity tuple.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `LOOM_NAMESPACE`                      | _(token's first non-`*` namespace)_ → `coding`                                           | Base namespace when scoping by tag.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `LOOM_AGENT`                          | `claude-code`                                                                            | Agent segment of the identity tuple.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `LOOM_PROJECT_SCOPE`                  | `tag`                                                                                    | `tag` (one namespace, `project:<name>` tag) or `namespace` (project basename _is_ the namespace).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `LOOM_PROJECT_NAME`                   | _(git toplevel basename)_                                                                | Override the resolved project.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `LOOM_MODE`                           | `both`                                                                                   | `both`, `memory` or `code`: the planes the hooks run ([modes](#modes)). Under Claude Code set the `mode` option, which outranks it and also picks the MCP listing.                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `LOOM_INJECT_CONTEXT`                 | `true`                                                                                   | SessionStart context primer (once per session). Set `false` to disable.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `LOOM_TASK_INBOX_LINE`                | `on`                                                                                     | SessionStart task inbox line: one `task.counts`, cached a minute, and one line when inboxes the namespace owns hold tasks in triage or todo — `Loom task inbox: N in triage, M to do in <inbox> (oldest 40 min) — pull with the loom `tasks` toolset (task.pull)`. Silent on an empty board or when the server does not answer. Set `off` to disable.                                                                                                                                                                                                                                                                   |
| `LOOM_COLLECTIONS_LINE`               | `on`                                                                                     | SessionStart collections line: one `collection.list`, cached a minute, and a few lines naming up to six shared collections the token can read, with your role and each one's description inside the recalled-data block (their creators' words); how to write one only when you write one and the toolset allows writes. Silent when there are none or the server does not answer. Set `off` to disable.                                                                                                                                                                                                                |
| `LOOM_AUTO_SIGNUP`                    | `on`                                                                                     | First session with no credential: sign this machine up via `/v1/auth/agent-signup`, save the token to `~/.loom/config.json` and the claim code to `~/.loom/claim-<namespace>.txt`, both 0600. `off` never signs up. Needs `LOOM_SELF_SIGNUP=open` on the server; otherwise the primer says how to get a token by hand.                                                                                                                                                                                                                                                                                                  |
| `LOOM_OBSERVE_DEDUPE_S`               | `60`                                                                                     | A read-shaped observation (`file_read`) of the same file or grep within this many seconds is not posted again — the sensory filter. `0` disables. Edits, commands and errors always post.                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `LOOM_PROMPT_RECALL`                  | `first`                                                                                  | Per-prompt recall via `context.pack`: `on` = every qualifying prompt; `first` = one attempt on the first qualifying prompt, including empty or abstaining answers; `off` = never. Default `first` skips clear named code-only questions without consuming the attempt; mixed memory requests and conceptual tasks still qualify.                                                                                                                                                                                                                                                                                        |
| `LOOM_CONTEXT_RUNS`                   | `false`                                                                                  | The SessionStart pack also asks for the `runs` manifest: one line per recent session of this identity (date, run status, goal, tool calls by kind with errors, files, digest) to shortlist from before opening a session with `memory.evidence` / `memory.digests`. Off by default: the list changes every session, and the primer is re-sent uncached each time it changes. Set `true` to include it.                                                                                                                                                                                                                  |
| `LOOM_CONTEXT_RUNS_LIMIT`             | `8`                                                                                      | Rows in the runs manifest (max 50).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `LOOM_OBSERVE_SKIP_TOOLS`             | loom's MCP tools + meta tools                                                            | Tool names the capture hook does not observe (comma-separated; a trailing `*` matches a prefix). A call to `loom_memory_set` carries a memory as its input — observing it would store that value a second time, outside the key you can delete — and `ToolSearch`, `AskUserQuestion`, `TodoWrite` say nothing about the task. The server applies the same list (`observe_skip_tools`) to older hooks.                                                                                                                                                                                                                   |
| `LOOM_CAPTURE_CONVERSATIONS`          | `true`                                                                                   | Set `false` to stop logging chat turns to the conversation plane. Client-side only — the **server** decides whether turns are kept (see below).                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `LOOM_HOOK_TIMEOUT_MS`                | derived (3000–8000)                                                                      | Request abort ceiling. By default it is 5× the round trip the SessionStart hook measured against this server (`GET /v1/health`, cached per server for six hours), clamped to 3–8 s; setting the variable overrides that and `loom doctor --client` flags the override.                                                                                                                                                                                                                                                                                                                                                  |
| `LOOM_CODE_SHUNT`                     | `on`                                                                                     | `off` disables the bulk-read shunt entirely.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `LOOM_SHUNT_MAX_LINES`                | `800`                                                                                    | `Read`/`cat` of an indexed, unmodified file longer than this is shunted.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `LOOM_SHUNT_READ_CAP`                 | `0` (off)                                                                                | Read-window cap in lines: a **ranged** `Read` (`offset`/`limit`) of an indexed, unmodified file whose span is longer than this is denied with the declarations in the span, at most twice per file per session; then it passes as `cap_spent`. The agent bench's arm D sets `200`.                                                                                                                                                                                                                                                                                                                                      |
| `LOOM_SHUNT_RESOLVE_TIMEOUT_MS`       | `8000`                                                                                   | Bound for the shunt's once-per-ten-minutes repository lookup (`code.repos` for the checkout's `owner/name`, then `code.index_status`), separate from the 3 s per-tool-call default: the listing takes the server 1–2 s and a remote round trip rides on top. A lookup that does not answer is cached as _unresolved_ for 60 s and retried, never as "not indexed".                                                                                                                                                                                                                                                      |
| `LOOM_SHUNT_GREP` / `LOOM_SHUNT_BASH` | `on`                                                                                     | Shunt tree-wide `Grep` / bulk `cat`,`nl`,`tac`,`rg`,`grep -r` in `Bash`; a `sed -n 'A,Bp'`, `head -n N`, `tail -n +A` or `awk 'NR>=A && NR<=B'` slice of one file is a ranged read under the read-window cap.                                                                                                                                                                                                                                                                                                                                                                                                           |
| `LOOM_TRACKER_SHUNT`                  | `on`                                                                                     | `off` lets `gh issue list --search` / `gh search issues` run without the tracker record answering first. `LOOM_TRACKER_SHUNT_TIMEOUT_MS` (default `3000`) bounds the one tracker.duplicate_check call.                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `LOOM_CODE_REPO`                      | _(the checkout's remotes)_                                                               | The fallback repository for the code index (`owner/name`, or a bare name one readable repository has; also the `code_repo` option or `code_repo` in `~/.loom/config.json`): tried after the checkout's remotes, so it serves a session outside any git checkout or a checkout with no index (a local mirror); an indexed checkout keeps its own index. Unset, every hook tries the checkout's remotes in order — `origin`, then `upstream`, then the rest — and a fork with no indexed remote resolves to its GitHub parent's index when the caller can read it; the primer then says `… is a fork of … → <namespace>`. |
| `LOOM_SHUNT_LOG`                      | `on`                                                                                     | Append one TSV line per **decision** (hit and pass: ts, decision, tool, tokens saved, reason head) to `$TMPDIR/loom-shunt-log-<hash of the uid>.log`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `LOOM_SHUNT_REPORT`                   | `on`                                                                                     | Report batched decisions to loom as `code.outcome` `shunt_hit` / `shunt_pass` rows (see below). `off` keeps the local log only.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `LOOM_FILE_CONTEXT`                   | `on`                                                                                     | `off` disables the file-context injection, after a read and before an edit.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `LOOM_FILE_CONTEXT_TOKENS`            | `300`                                                                                    | Budget per file for the block's rows other than decisions and constraints, which print whole.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `LOOM_FILE_CONTEXT_TTL_MIN`           | `30`                                                                                     | Before an edit, re-inject for the same file after this many minutes.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `LOOM_HOOK_EXIT_MS`                   | derived (= the request bound)                                                            | Hard exit grace after dispatch. Unref'd, so a hook exits as soon as its request settles; the grace is the ceiling. A shorter grace loses every fire-and-forget capture to a remote server whose TLS handshake alone takes ~570 ms.                                                                                                                                                                                                                                                                                                                                                                                      |
| `LOOM_WORKING_MEMORY`                 | `on`                                                                                     | `off` disables the Stop/PreCompact working-memory note.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `LOOM_SESSION_BRIEF`                  | `on`                                                                                     | `off` disables the PreCompact/SessionEnd session brief and its primer line.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `LOOM_SESSION_BRIEF_KEY`              | unset                                                                                    | Names the brief this checkout revises (default `auto/<project>/<branch>`, chosen by the server).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `LOOM_SESSION_BRIEF_PRIMER`           | `off`                                                                                    | `on` adds one SessionStart line naming the saved briefs for the project. Off by default: restoring is the person's call (the `loom-session` skill).                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `LOOM_PLUGIN_SCRATCHPAD`              | unset                                                                                    | `1` appends each turn's one-line conclusion to the run's `scratchpad` block (`working_memory.append`, `run_id = cc-<session_id>`); `memory.search`/`memory.ask`/`context.pack` calls carrying the run_id read it back. A write per turn, so opt-in.                                                                                                                                                                                                                                                                                                                                                                     |
| `LOOM_PLUGIN_RUN_RETAIN`              | unset                                                                                    | `keep` \| `demote` \| `expire`: what `run.complete` does to the memory rows the session wrote. Unset = the server default (`keep`). `demote` lowers their confidence at SessionEnd; `expire` sets a 7-day expiry with `on_expire=demote` so the expiry sweep does it when the expiry passes. Pinned, verified, promoted and locked rows are always kept.                                                                                                                                                                                                                                                                |

Capture is on the moment Claude Code reloads its hooks.

> **Conversation recording is opt-in per namespace.** `LOOM_CAPTURE_CONVERSATIONS=true`
> only means the hook _sends_ turns; `conversation.append` drops them unless the
> target namespace has `conversation_logging_enabled`, which is **off** for
> new namespaces. The call still succeeds, so a closed
> gate looks exactly like a working one — if the dashboard's Conversations page stays empty, turn
> on its **Record conversations** switch (or call `conversation.set_logging`) before debugging
> the hooks. Tool/episodic capture (`observe.record`) is unaffected.

## loom-code-shunt: the bulk-read reflex goes to the index

Most of what a coding agent spends is not thinking, it is I/O: whole-file
`Read`s and tree-wide `Grep`s issued reflexively before any reasoning. The
`PreToolUse` hook `hooks/loom-code-shunt.mjs` catches that reflex **inside a
loom-indexed repository** and denies it with a reason that names the code op to
call instead — the index already holds every symbol with its `path:line` span,
its callers, and an as-of commit, so the redirect is line-accurate and costs a
vector query rather than a model call (a summariser would cost a model call
and lose line numbers).

What is shunted, and what passes:

| Tool call                                                                                    | Shunted when                                                                                                                                                                                                                                                                   | Passes                                                                                                                                                                                                                   |
| -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Read file`                                                                                  | file is under the indexed prefixes, unmodified in the working tree, longer than `LOOM_SHUNT_MAX_LINES` (800), and outlined in full by the index: the deny lists its declarations                                                                                               | `offset`/`limit` given; small, binary, unindexed or locally modified files; a file whose outline is cut short or unanswered (`index_unanswered`)                                                                         |
| `Read file offset/limit`                                                                     | `LOOM_SHUNT_READ_CAP` > 0 and the span (limit defaults to Claude Code's 2000, clipped to the file) is longer than the cap, on an indexed, unmodified file — until the file's two denials this session are spent; the deny lists the declarations in the span                   | spans within the cap; modified, untracked, binary or unindexed files (`targeted_span`); the third and later over-cap reads of the same file (`cap_spent`); a span the index cannot describe in full (`index_unanswered`) |
| `Grep pattern [dir]`                                                                         | never: a grep over a directory runs                                                                                                                                                                                                                                            | matching lines (`tree_content_grep`); a file list or counts (`file_list_grep`); a single file as scope (`single_file_grep`)                                                                                              |
| `Grep "Class::method" file`, `Bash: grep -n "Class::method" file`                            | the pattern names one symbol — a `::` path, or a mixed-case / snake_case identifier of 8+ characters with a lowercase letter — the file is under the indexed prefixes, and the index holds exactly one definition: denied **once per symbol per session** with that definition | the second grep for the same symbol (`symbol_grep_spent`); a symbol with no definition or several, or no answer in 2.5 s (`index_unanswered`); ALL_CAPS constants, short words, regexes, strings (`single_file_grep`)    |
| `Bash: cat/bat/nl/tac file`, `rg pat [dir]`, `grep -r pat [dir]`                             | the whole-file `Read` rule for `cat`/`bat`/`nl`/`tac` and the symbol rule for a one-file `grep`, when the read or grep is the whole command; never `rg` or `grep -r` over a directory                                                                                          | a command of several parts (`;`, `&&`, `\|\|`, a newline or a pipe: `compound_command`), redirects, pattern-driven `sed`/`awk`, non-recursive grep                                                                       |
| `Bash: sed -n 'A,Bp' file`, `head -n N file`, `tail -n +A file`, `awk 'NR>=A && NR<=B' file` | the ranged-Read rule: `LOOM_SHUNT_READ_CAP` > 0 and the slice is longer than the cap, on an indexed, unmodified file, when the slice is the whole command; `tail -n N file` is judged as the file's last N lines                                                               | slices within the cap; no cap configured; several files; a command of several parts; `-c` bytes                                                                                                                          |
| `Bash: gh issue list … --search q`, `gh search issues q …` (as the whole command)            | the checkout is indexed and the tracker record answers one bounded `tracker.duplicate_check` on the search words and any `Ns::Class::method` frames in them: the deny carries the verdict and the closest reported items                                                       | the same command on the next try, `gh issue view N`, `gh pr …`, a listing without `--search`, a command of several parts, an unbound or unauthorized tracker namespace, a timeout, `LOOM_TRACKER_SHUNT=off`              |

A deny carries the index's answer (a file's declarations with their lines and
headers, a symbol's definition, the tracker's verdict) and says how to pass
through (a ranged read). The declarations are the kinds `code.outline` lists by
default: functions, methods, classes, structs and the units of tests, build
files and SQL. An interface, type alias, enum or constant is not among them,
and the deny says so. A refusal costs the agent a request, so the hook
gives one only with that answer in full: when the index does not hold it or
does not reply in 2.5 s, the call runs. A Bash command of several parts is
never refused, because a refusal withholds every part and only one of them is
the read the hook judged; its reads and greps are still counted as passes,
under `compound_command` where the same call alone would have asked the index.
A whole file's outline is asked for with a budget of 4,000 tokens, and a file
whose outline does not fit is read. With that budget the longest deny measured
over 301 large files of a C++ repository was 8,400 characters; the server
counts an entry at a quarter of its JSON length, so long signatures make a
longer one. A whole file whose lookup gave no declarations (no reply, an empty
outline or a cut one) is not asked about again in that session. The
deny never names the
`LOOM_CODE_SHUNT=off` switch: a deny that names its own bypass gets
`LOOM_CODE_SHUNT=off grep -r …` as the agent's next command, which measures
compliance rather than the index. Repo detection is one
`code.repos` + `code.index_status` call per repo root, cached 10 minutes in the
OS temp dir. Index freshness never disables the shunt: however
far the checkout or the index is behind, a file you have not modified is answered
from the index and the deny names its `as of` commit; the only opt-out is
`local_first` / `LOOM_CODE_LOCAL_FIRST=on`, which stands the shunt down entirely. Everything fails open: no token, unreachable server,
unknown repo → the tool call proceeds untouched. Cached decisions take ~60 ms.

**"Is this already reported?" goes to the tracker record**. Left alone, a model asked whether a crash is already filed runs several `gh issue list --search` calls (often empty — GitHub search wants the exact phrase) and never calls a tracker op, although `tracker.duplicate_check` has the calibrated answer in about a second. A `gh issue list --search` / `gh search issues` inside an indexed checkout is answered first by one bounded `tracker.duplicate_check` (`LOOM_TRACKER_SHUNT_TIMEOUT_MS`, 3 s) addressed to the checkout's code namespace — the server binds it to the tracker namespace the caller may read — and denied once per query with the verdict and up to five closest items; the same command passes on the next try, so a deliberate GitHub search costs one retry. The SessionStart primer names `loom_tracker_duplicate_check` for the question, so the first choice is the calibrated call with title, body and frames. The check reads every tracker record the caller may read, and the deny names the record on every item (`[tracker-acme] #64905 …`).

**The read window and its budget** (`LOOM_SHUNT_READ_CAP`,
default off). A ranged `Read` passes the whole-file rule by definition, so an
agent that has learned the rule reads `offset=1 limit=2000` instead — the same
bulk read with a different shape. With the cap set, a ranged read of an
indexed, unmodified file whose span is longer than the cap is denied with the
declarations in the span (lines, qualified name, header) from one
`code.outline` call, and "read the ones you need, at most `<cap>` lines at a
time". A span the index cannot describe in full passes and spends no denial.
Each file may be denied this way **twice per session**, and the third over-cap
read of that file passes with reason `cap_spent`, so a model that insists is never
denied the same file forever. The count is kept in
`$TMPDIR/loom-shunt-cap-<hash of the session id>.json` as `{"<path>": n}`, beside
the symbol-grep (`sym:`) and tracker-search (`qa:`) budgets and the whole files
whose lookup gave no declarations (`miss:`). A denial is given
only when its spend was written: while that file cannot be kept (tmp not
writable, or a file planted there), every over-cap read, symbol grep and answered
tracker search passes as if its budget were spent. A
modified or untracked file is never capped. The bench harness's arm D should
set `LOOM_SHUNT_READ_CAP=200` so the measured arm is this product rule rather
than its own clamp.

**Production counters.** Every decision the hook makes for a bulk-read-shaped
call inside an indexed repo — a _hit_ (denied) or a _pass_ (allowed) — is one
line in `$TMPDIR/loom-shunt-log-<hash of the uid>.log` and in the session's
unflushed batch file next to it. A hook that flushes takes every batch of its
server, token and repository: its own, those ended sessions left and those of
sessions still running, each posted once. The
batch is reported to loom as two `code.outcome` rows (`action=shunt_hit` with
`tokens_saved` = bytes of the avoided file ÷ 4 — or of the avoided span, for a
read-cap denial — less the deny text's own characters ÷ 4, and `action=shunt_pass`; `targets`
holds one tool name per decision, so `length(targets)` is the count) —
fire-and-forget with a 300 ms ceiling, and **only** on a deny (which already
paid a server round trip) or once 50 lines are pending, so a pass-through never
waits on the network. On the server the rows land in
`loom.code_search_outcome` (`tokens_saved` column) and tick the
`loom.code.shunt` / `loom.code.shunt_tokens_saved` OTel counters. The
measured-saving series from `bench/code_shunt_tokens.py --post` uses the same
row shape (`source=bench-shunt-tokens`), so bench and live numbers sit in one
table.

Outcome labels: when any `loom_code_*` MCP tool that returns a `trace_run_id`
(search, symbol, ask, trace, callers, callees, impact, verify, grep,
context_pack, examples, history, map) answers, the `PostToolUse` capture hook
stashes its trace id and the repo paths in the response for 10 minutes; a
`Read` of one of those paths reports `opened_file`, a grep right after reports
`fell_back_grep`. That is the feedback the ranking fold consumes.

Measured (`bench/code_shunt_tokens.py`, 30 recent ClickHouse PRs, 5.8 changed
files each, tokens = bytes/4 on both sides): reading every changed file in full
costs a mean 43k tokens per PR; context pack + one path-scoped `code.search` per
file costs 5.0k (mean saving 64%, median 82%). Simulating the hook exactly —
files over 350 lines shunted, the rest read in full — gives 6.9k per PR, a
**55% mean / 72% median saving**, with a handful of tiny PRs costing more
because the pack is a fixed ~2.5k. On the shunted reads alone the median
saving is 82%.

## Skills

Seven skills ship with the plugin and load on their triggers:

| Skill                      | Trigger                                                                                                                                                                                  | What it settles                                                                                                                                                                                                                                                              |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom-code`                | about to grep, glob or open files in a loom-indexed repo                                                                                                                                 | which `code.*` op replaces the reflex, how to read a result, when to stop                                                                                                                                                                                                    |
| `loom-memory`              | about to write a fact; the user states, decides or corrects one; end of a substantial task                                                                                               | durable vs scratchpad, how to write a fact a future session can trust (no laundered inferences, source pointer, same key on change), how to read `memory_id` / `conflict` / `abstain` / `verification`, the end-of-task review                                               |
| `improve-loom-for-my-repo` | end of a session in which loom's code tools missed                                                                                                                                       | turns this session's transcript into reproducible findings for the maintainers                                                                                                                                                                                               |
| `loom-status`              | a loom tool answers "unauthenticated" or empty for something the user says was saved; the primer did not appear; "is loom working?"; a hook says it found no Node                        | read-only checks in order — the hook runtime (`ensure-node.sh --check`), the packaged client check (`bin/loom-client-status.mjs`: server, token, where the token came from, auth, writer grant, stranded captures), plugin and MCP registration — and the one cause and fix  |
| `loom-optimize`            | "optimize my loom namespace", "audit memory", "my memory search is bad"; a mentor hint naming rows to rework                                                                             | runs `namespace.audit`, writes a plan the owner approves, reworks rows with `memory.move` so every old key stays readable through a redirect; retires nothing                                                                                                                |
| `loom-session`             | only an explicit "save this session", "hand this off", "resume", "where was I on this branch"; a brief the primer mentions is not a request                                              | loads a session brief with what moved in the checkout since it was saved (`bin/loom-brief.mjs get`), or saves one the agent writes from its own context — goal, status, decisions and why, dead ends, done, next steps, files, rules, pointers                               |
| `loom-mentor`              | a hint (a result's `hint`, or a "Loom mentor" line) names a plane or op you do not know how to use; "what can loom do", "am I using loom well" (a hint that names its op needs no skill) | the situation → plane → op table for every plane (code index, tracker record, documents, records, working memory, offload, graph, runs, decisions, procedures, duplicates, conflicts, bisect), what each hint code means and what to do, and `mentor.brief` for the evidence |

The memory skill complements ambient capture: the hooks record what happened
(conversation turns included, by the Stop hook); the skill covers the facts
only the agent can identify — a decision, a constraint, a correction — and
keeps guesses out of the store.

Precedence when more than one skill fits: `loom-mentor` only routes, and the
skill it routes to owns the detail — `loom-memory` the shape of a write,
`loom-code` code questions, `loom-optimize` reworking stored rows (after an
audit and the owner's approval), `loom-status` client failures. `loom-session`
runs only on the user's explicit ask, and `improve-loom-for-my-repo` only on
misses this session hit in an indexed repository. Each skill names ops by
their dotted name; an op the tool list does not show runs through `loom_op`.

## Project scoping

The project name is the **git toplevel basename** of the working directory
(`git rev-parse --show-toplevel` → basename; falls back to cwd basename, then
`LOOM_PROJECT_NAME` overrides). Sibling repos stay isolated.

The repository's **stable identity is its remote**: every
capture, run and session memory also carries `repo:<id>`, where `id` is the
first ten hex digits of `sha256` over the normalised `origin` URL (scheme, user,
`.git`, port, trailing slash and case dropped, so `git@github.com:Acme/App.git`
and `https://github.com/acme/app` agree). Two clones of different repositories
both called `app` do not share memory, and a renamed clone keeps its
history. `LOOM_REPO_REMOTE` overrides the URL the hooks read; a checkout with
no remote carries the `project:` tag only. Observations also record
`repo_slug` (`owner/name`) and `rel_dir` (where under the root the agent was
working).

This rides Loom's identity tuple — it does **not** introduce a new tenancy
model. By default the project is carried as a `project:<name>` tag inside one
namespace; set `LOOM_PROJECT_SCOPE=namespace` to give each repo its own
namespace instead (the `repo:` tag still rides). Loom's JWT/org/namespace/role
governance is unchanged.

## Zero-token capture (and where the LLM work happens)

The capture path runs **no LLM**. It does cheap heuristic typing from the tool
name (`file_edit` / `command_run` / `error` / `web_fetch` / `file_read`), pulls
file paths out of the tool input, truncates the narrative, and POSTs the raw row
to Loom's episodic plane. Embedding, fact extraction, conflict detection, and
blacklist checks are **deferred to Loom's server** — run async / at
crystallization, on the ClickHouse substrate, not inline on every tool call.

## Other clients

**Codex** has its own build of this plugin — the same scripts and skills,
Codex's hook wiring — at
[`integrations/codex-plugin/`](https://github.com/ClickHouse/loom/tree/main/integrations/codex-plugin):
`codex plugin marketplace add ClickHouse/loom && codex plugin add loom@loom`;
the plugin bundles its MCP entry with the URL and
`toolset=core,coding,tracker,tasks` fixed (Codex plugins have no install-time
options). See
[Codex](https://loom.clickhouse.com/docs/features/integrations/codex/).
The scripts learn which client runs them from their third argument
(`… PostToolUse codex`); unnamed, they are Claude Code's.

`hooks/hooks.generic.json` maps the same lifecycle events to the same
scripts for any client that can exec a command and pass the hook payload as JSON
on stdin. The scripts probe several common field-name shapes
(`tool_name`/`toolName`/`tool`, etc.) and degrade gracefully if a field is
absent.

## Uses the `observe` server ops

Captures POST to the `observe.record` wire op and crystallization to
`observe.crystallize`. A failed POST is swallowed — the primer (`context.pack`)
and the safety model work regardless. See
[Claude Code](https://loom.clickhouse.com/docs/features/integrations/claude-code/).
