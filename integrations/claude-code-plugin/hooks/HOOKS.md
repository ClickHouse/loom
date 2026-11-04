# Hook lifecycle

`hooks.json` is the Claude Code wiring; Claude Code reads it for the wiring
only and warns on any other key, so the notes live here. The Codex plugin
runs byte-identical copies of these scripts with its own wiring
(`integrations/codex-plugin/hooks/hooks.json`, which says what differs and
why). `hooks.generic.json` maps the same scripts onto a hand-wired client's
events. Every script is `node <script> <Event> [client]`: the event name is
the first argument, the client (`codex`, …) the optional second; the payload
is JSON on stdin.

## What every hook guarantees

- **It never fails the host session.** Every path exits 0 and a thrown error
  is swallowed. A hook that ends in `runHookMain` (`lib/loom-client.mjs`)
  writes that error's first line to stderr under `LOOM_HOOK_DEBUG=on`, with
  bearer tokens and JWTs redacted; nothing else reaches stderr.
- **It is bounded.** Each request is aborted at the request bound: 5× the
  round trip SessionStart measured against the server, clamped to
  3,000–8,000 ms (`lib/hook-bounds.mjs`); `LOOM_HOOK_TIMEOUT_MS` overrides it.
  An unref'd exit timer at the same bound ends the process if a request
  hangs. Stdin is awaited for at most one second, and a payload over 32 MB is
  dropped unparsed.
- **It is safe without Loom.** No credential means no request (the only
  exception is the first-session sign-up below). A server that is down,
  slow, or answers 401/403/404 degrades to "nothing injected, nothing sent".
- **It reads the transcript in bounded pieces.** A tail, a head and tail, or
  a byte window from a cursor (`lib/transcript.mjs`); only the test and
  validate gates read it whole, and not at all past 64 MB. A line without its
  newline is still being written and is never consumed.
- **It keeps its state in `$TMPDIR`** (`lib/tmp-state.mjs`,
  `loom-<kind>-<sha1(key)[0:12]>`). Losing a state file only makes the next
  run behave like the first.
- **It does not recurse.** A Stop gate blocks at most once per distinct
  state and never when `stop_hook_active` is set. No hook calls a tool, and
  the PostToolUse capture skips Loom's own tools.
- **Conversation capture has exactly one path**: `loom-conversation`. The
  primer tells the model never to call `conversation.append` itself.
- **Hooks do not retry.** A write that must land (conversation turns, a
  session brief) goes through the detached flusher and a cursor: the cursor
  moves only when the write landed, so the next event re-sends.

The payload is normalized once (`normalizePayload` in `lib/loom-client.mjs`):
Claude Code's field names are canonical, the other spellings a generic
client may use (`sessionId`, `transcriptPath`, `toolName`, `toolInput`,
`tool_result`, `output`, `user_prompt`) fold onto them, and a field of the
wrong type is dropped.

## Events, in firing order

Timeouts are the host's default unless noted. "Output" is what the host
receives on stdout.

| Event                                           | Script                | Reads                              | Output                   | Side effects                                                                                | Off                                |
| ----------------------------------------------- | --------------------- | ---------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------- | ---------------------------------- |
| SessionStart                                    | loom-session-start    | cwd, session_id                    | primer text              | context.pack, code.repos/index_status/map, task.counts, namespace.get; writes the RTT cache | `LOOM_INJECT_CONTEXT=false`        |
| UserPromptSubmit                                | loom-prompt-pack      | prompt, cwd, session_id            | JSON additionalContext   | code.search                                                                                 | `LOOM_PROMPT_PACK=off`             |
| UserPromptSubmit                                | loom-prompt-recall    | prompt, cwd, session_id            | JSON additionalContext   | context.pack                                                                                | `LOOM_PROMPT_RECALL=off`           |
| UserPromptSubmit                                | loom-run              | prompt, session_id                 | none                     | run.begin on the first prompt; appends the turn journal                                     | `LOOM_RUN_LEDGER=off`              |
| PreToolUse Read\|Grep\|Bash                     | loom-code-shunt       | tool_name, tool_input, cwd         | JSON permission decision | code.repos; deny text                                                                       | `LOOM_CODE_SHUNT=off`              |
| PreToolUse Edit\|Write\|MultiEdit\|NotebookEdit | loom-file-context     | tool_input, session_id             | JSON additionalContext   | memory.for_file                                                                             | `LOOM_FILE_CONTEXT=off`            |
| PostToolUse loom tools                          | loom-mentor-hint      | tool_name, tool_response           | JSON additionalContext   | none                                                                                        | `LOOM_MENTOR_HINTS=off`            |
| PostToolUse Edit\|Write\|MultiEdit              | loom-blast-radius     | tool_input, cwd                    | JSON additionalContext   | code.impact                                                                                 | `LOOM_BLAST_RADIUS=off`            |
| PostToolUse, PostToolUseFailure                 | loom-capture          | tool_*, session_id, cwd            | none                     | observe.record; code.outcome; the recalled-keys stash                                       | no token                           |
| PreCompact                                      | loom-capture          | session_id                         | none                     | observe.crystallize                                                                         | no token                           |
| PreCompact                                      | loom-working-memory   | transcript, cwd                    | none                     | working_memory.set                                                                          | `LOOM_WORKING_MEMORY=off`          |
| PreCompact                                      | loom-session-brief    | transcript, cwd                    | none                     | session.brief_save via the flusher                                                          | `LOOM_SESSION_BRIEF=off`           |
| Stop                                            | loom-capture          | session_id                         | none                     | observe.crystallize                                                                         | no token                           |
| Stop                                            | loom-conversation     | transcript, last_assistant_message | none                     | conversation.append via the flusher                                                         | `LOOM_CAPTURE_CONVERSATIONS=false` |
| Stop                                            | loom-usage            | transcript                         | none                     | cost.record_turns                                                                           | `LOOM_USAGE=off`                   |
| Stop                                            | loom-test-gate        | transcript, cwd                    | JSON block once          | code.test_gate                                                                              | `LOOM_TEST_GATE=off`               |
| Stop                                            | loom-validate-gate    | transcript                         | JSON block once          | namespace.validate                                                                          | `LOOM_VALIDATE_GATE=off`           |
| Stop                                            | loom-namespace-health | cwd, session_id                    | JSON block once          | namespace.audit (first Stop only)                                                           | `LOOM_NAMESPACE_HEALTH=off`        |
| Stop                                            | loom-working-memory   | transcript, cwd                    | none                     | working_memory.set when the note changed                                                    | `LOOM_WORKING_MEMORY=off`          |
| SessionEnd (timeout 10)                         | loom-capture          | session_id                         | none                     | observe.crystallize                                                                         | no token                           |
| SessionEnd (timeout 10)                         | loom-conversation     | transcript                         | none                     | the turns since the last Stop                                                               | as Stop                            |
| SessionEnd (timeout 10)                         | loom-run              | transcript, reason                 | none                     | run.complete, awaited                                                                       | `LOOM_RUN_LEDGER=off`              |
| SessionEnd (timeout 10)                         | loom-session-memory   | transcript                         | none                     | memory.set_from_messages via the flusher                                                    | `LOOM_SESSION_MEMORY=off`          |
| SessionEnd (timeout 10)                         | loom-session-brief    | transcript, cwd                    | none                     | session.brief_save via the flusher                                                          | `LOOM_SESSION_BRIEF=off`           |

`hooks/loom-flush.mjs` is not wired to an event: the hooks above spawn it
detached (`lib/flush-job.mjs` holds its job shapes and delivery policy).

## Hooks

- **loom-session-start, first session.** With no credential anywhere
  (LOOM_API_KEY, the `token` option, ~/.loom/config.json, ~/.loom/token) the
  hook calls the server's agent self-signup once, writes the token to
  ~/.loom/config.json (0600) and the one-time claim code to
  ~/.loom/claim-<namespace>.txt (0600), and the primer tells the user how to
  claim the namespace in the dashboard; a single-user server returns its one identity,
  so there is no claim code and the primer just says it connected. The MCP
  headers helper cannot see install options, so with no LOOM_BASE_URL or saved
  URL it waits for this hook's credential (`LOOM_MCP_HOOK_WAIT_MS`, 5 s, under
  Codex's 10 s helper limit) before signing up through the same lock itself.
  `LOOM_AUTO_SIGNUP=off` disables
  it; a server without self-signup (404) or rate-limiting it (429) leaves the
  machine without a credential and the primer says how to get a token by
  hand. A token given as the `token` install option is first copied into
  ~/.loom/config.json, where the headers helper can read it. In Claude Code,
  when that helper will send the config's token and the config names another
  server than the MCP entry's, the primer says so. The `server_url` option
  is saved to a tmp state file (`loom-install-<hash>.json`, one per plugin
  install) for the status script, whose shell carries no option.
- **loom-session-start, primer.** One `context.pack` (mode startup, the
  project's working-memory block first, the recent-runs shortlist only with
  `LOOM_CONTEXT_RUNS=true`), plus: the repository tag line; in an indexed
  checkout the code scope line, the arbitration rules and, with a
  `LOOM_CODE_MAP_TOKEN_BUDGET` of 100 or more, the repo map; the task inbox line
  (`LOOM_TASK_INBOX_LINE=off`); the saved-briefs line (only with
  `LOOM_SESSION_BRIEF_PRIMER=on`); the collections line
  (`LOOM_COLLECTIONS_LINE=off`); the recording line, which states the
  owner's Recording switch; and a once-a-day notice when ~/.claude.json also
  mounts the same server.
- **loom-session-start, collections line.** One `collection.list` for the
  session's org, cached a minute per (server, org, token, toolset); when the token
  reads shared collections, a few lines say that `loom_search` can search
  them and list up to six, those the caller holds most on first, one JSON line
  each with the caller's role and description. When the caller writes one of
  them and the toolset is not read-only, they also say how `knowledge.add`
  writes one when asked. The names and descriptions are their creators'
  words, so the list sits inside the recalled-data block. None on the assistant
  toolset; silent with no collection, or when the server does not answer.
- **loom-session-start, task inbox line.** One `task.counts` for the
  session's scope, cached a minute per (server, org, namespace); when inboxes
  the namespace owns hold tasks in triage or todo, one line names the counts
  and `task.pull`. Silent on an empty board, a 404, a 403 or a slow server.
- **loom-file-context.** Before Edit/Write, asks memory.for_file what Loom
  knows about the target file (its files[] facet) and injects the rendered
  block as additionalContext. One lookup per (session, file) per 30 min;
  fail-open; never a permission decision. LOOM_FILE_CONTEXT=off disables.
- **loom-code-shunt.** In a loom-indexed repo, whole-file Reads (>
  LOOM_SHUNT_MAX_LINES, default 800), tree-wide Greps and bulk `cat`/`rg` in
  Bash are denied with a reason naming the code.* op to call instead.
  Fail-open; targeted reads (offset/limit, single file) and modified files
  pass. With LOOM_SHUNT_READ_CAP=<lines> (default 0 = off) a ranged Read
  whose span is longer than the cap on an indexed, unmodified file is denied
  too — redirect to code.enclosing/code.symbol/code.outline — at most twice
  per file per session ($TMPDIR/loom-shunt-cap-<session_id>.json), then it
  passes as cap_spent. The deny text never names
  LOOM_CODE_SHUNT=off. The once-per-10-min repository lookup names
  the checkout's repo (code.repos repo=owner/name), has its own bound
  (LOOM_SHUNT_RESOLVE_TIMEOUT_MS, default 8000) and, when it does not answer,
  is recorded as unresolved and retried within a minute, never cached as
  not-indexed. A sed -n 'A,Bp' / head -n N / tail -n +A / awk NR-range slice
  of one indexed file in Bash is a ranged read under the same cap.
  LOOM_CODE_SHUNT=off disables.
- **loom-mentor-hint.** On a loom tool result, lifts the store's one `hint`
  (and `hint_code`) out of the result and hands it back as
  additionalContext. The server limits hints to one per call and each code
  to once per session; the hook repeats that limit (six per session) and
  makes no request of its own.
- **loom-blast-radius.** After Edit/Write/MultiEdit in a loom-indexed repo,
  diffs the file against HEAD, sends its paths, hunk ranges and function names
  only (`diffHeaders`) and asks code.impact (depth 1) what the touched symbols
  reach; injects one line — callers, tests reaching the change, and co-change
  partner files this session has not touched. The same touched set is
  reported once per session. LOOM_BLAST_RADIUS=off disables.
- **loom-capture.** PostToolUse and PostToolUseFailure record one episodic
  `observe.record` per tool call (zero-token heuristic typing, inputs and
  outputs truncated to 800 characters, the session's run id attached);
  input, output and error pass through client-side secret redaction first
  (private keys, AWS/GitHub/Slack tokens, JWTs, bearer headers,
  `password=`-style assignments: best effort, not a guarantee);
  Loom's own tools and the harness's meta tools are skipped
  (`LOOM_OBSERVE_SKIP_TOOLS` overrides the list), and a read of the same
  file repeated within `LOOM_OBSERVE_DEDUPE_S` (60) seconds is posted once.
  After a loom_code_* result it stashes the trace id and paths, and a later
  Read of a returned path or a grep reports `code.outcome`. After a Loom
  memory read it stashes the keys served, for the run's `recalled_keys`.
  PreCompact, Stop and SessionEnd post `observe.crystallize`.
- **loom-prompt-recall.** Default `first`: one bounded `context.pack`
  (memories, decisions, procedures, the prompt as the query) on the first
  qualifying prompt of a session; `LOOM_PROMPT_RECALL=on` for every prompt.
  Injects only unseen blocks, only when memory is not abstaining and
  retrieval_confidence ≥ `LOOM_PROMPT_RECALL_MIN_CONF` (0.5), capped at
  `LOOM_PROMPT_RECALL_MAX_CHARS` (1200).
- **loom-prompt-pack.** On by default (`LOOM_PROMPT_PACK=off` turns it off).
  Per prompt in an indexed checkout, one code.search for the request (a
  `<question>`-style wrapper's contents, without sentences that only tell the
  agent how to work or answer; top 3, no bodies) and up to 3 unseen pointers,
  only when the index is not abstaining and retrieval_confidence ≥ 0.4.
- **loom-run.** The run ledger. The first prompt opens a run (goal = the
  prompt, run_id = cc-<session_id>, cx- for Codex; when run.begin answers
  another id, it is kept in the session's run state and every hook uses it;
  the SessionStart primer and the first prompt's recall run before that answer,
  so their context.pack exposure rows carry the derived id, or on a resumed
  session the previous run's id; a begin that timed out or hit a 408, 429 or 5xx is asked
  again on the next prompt with the same id, and closed at SessionEnd if it was
  never answered; any other 4xx is final for the session); every prompt is also
  appended to the turn journal, which clients without a parseable
  transcript read. SessionEnd closes the run with the last assistant text,
  `outcome: none` and the recalled keys, awaited and then exited at once;
  the hook never claims which memories were used.
- **loom-validate-gate.** Collects the memory keys this session wrote
  (assistant tool_use blocks naming memory.set / update / update_by_id /
  apply), asks namespace.validate for a report scoped to exactly those keys,
  and blocks the stop once per distinct key set with the error and warning
  findings that name them, plus each check's remedy. info findings never
  block; the second stop on the same keys passes. LOOM_VALIDATE_GATE=off
  disables.
- **loom-namespace-health.** On the first Stop of a session, one
  namespace.audit over the last day (sample_limit=0); a critical finding
  gives the agent one line naming the codes and /loom-optimize, as a
  block-once reason. Later stops make no call.
- **loom-test-gate.** With uncommitted changes this session made in a
  loom-indexed repo (dirty paths the transcript shows an edit tool targeted
  or a shell command named; a tree left dirty before the session gates
  nothing), sends the change's paths, hunk ranges and function names only
  (`diffHeaders`) to code.test_gate for its obligations and blocks the stop
  once per distinct change with a reason naming them. LOOM_TEST_GATE=off
  disables; LOOM_TEST_GATE_DEPTH sets hops (1-3).
- **loom-usage.** Reads the transcript's last MiB, dedupes assistant turns
  by message id (sidechains skipped) and posts their raw usage counters to
  cost.record_turns; the server prices them. Not wired for Codex, whose
  transcript carries no per-turn usage.
- **loom-working-memory.** The session's "where was I" note: one
  working_memory.set per project (block session:<project>) with the goal,
  `git status --short --branch`, the files edited this session and the last
  assistant text, written only when it changed (always at PreCompact). The
  primer after a compaction loads it first. With `LOOM_PLUGIN_SCRATCHPAD=on`
  each Stop also appends the turn's one-line conclusion to the run's
  scratchpad block.
- **loom-conversation.** See the guarantees above. It reads at most 8 MB and
  sends at most 50 turns per Stop from its byte cursor; the rest follows at
  the next Stop. With the owner's Recording switch cached off it sends
  nothing and moves the cursor past the window. Without a parseable
  transcript it posts the turn journal's rows plus `last_assistant_message`.
- **loom-session-brief.** Reads the transcript from its byte cursor (the
  window's first 2 MB and last 6 MB when larger), turns it into typed events
  (asks, replies, edits, commands, errors, reads, todos, printed PR and commit
  identifiers) and posts them to session.brief_save through the flusher; the
  server revises auto/<project>/<branch>. The cursor moves before the post
  and the flusher restores it if the save fails. A first save needs a user
  ask plus an edit or a dozen events. Recording off skips the window.
- **loom-session-memory.** Hands the session's user prompts and assistant
  text (the last 8 MB of the transcript, else the turn journal) to
  memory.set_from_messages(defer=true) once, through the flusher; the server
  extracts the facts and the summary. It then removes the session's
  recalled-key stash, whatever it sent (loom-capture removes its code-trace
  stash). The turn journal stays, because the other SessionEnd hooks run
  alongside it and read it too; loom-session-start removes this user's tmp
  state files untouched for 24 hours. Every tmp state file the hooks keep is
  mode 0600, and none is read or written through a symlink or from a file
  another user owns.

## SessionEnd budget

Claude Code gives all SessionEnd hooks a shared 1.5 s budget by default;
Loom registers five. Every entry declares `timeout: 10`, which raises the
event budget to match, and `loom-run.mjs` awaits its `run.complete` and
exits at once. Without this the last hook is killed after its row already
landed and the terminal prints `Hook cancelled`.

## Local-first mode (off by default)

`LOOM_CODE_LOCAL_FIRST=on` (or the plugin option `local_first`) is the one switch for people who want their working tree searched, not the index: the code guard stands down entirely, whatever the commits (every grep and Read runs locally), and the session primer says so and skips the repo map. Everyone else keeps the default: the index answers files without local changes however far behind the checkout or the index is — no commit-distance or fetch-overdue stand-down — and only modified files are read or grepped locally.

## Settings

Install-time options (`claude plugin install loom@loom --config key=value`)
reach the hooks as `CLAUDE_PLUGIN_OPTION_<KEY>`: `server_url` and `token`
(read by `lib/loom-client.mjs`), `toolset` (the MCP URL only),
`local_first` and `code_repo` (`CLAUDE_PLUGIN_OPTION_CODE_REPO`). Resolution order: the environment, then the option, then
~/.loom/config.json (`LOOM_CONFIG_FILE`), then ~/.loom/token
(`LOOM_API_KEY_FILE`, token only), then the plugin's `plugin-defaults.json`
(URL only), then http://localhost:7777.

An agent with no Loom token of its own sets `LOOM_CLIENT_ID` (a workload trust,
`loom bot create --agent-claim`) and `LOOM_WORKLOAD_TOKEN` or
`LOOM_WORKLOAD_TOKEN_FILE` (the OIDC token its control plane issued it). Right
after `LOOM_API_KEY`, the hooks and the MCP headers helper then use the token
`bin/loom-agent-token.mjs` exchanges it for (RFC 8693), which never outlives the
workload token: saved 0600 in ~/.loom/agent-token-<hash of LOOM_CLIENT_ID>.json,
bound to the workload token it was exchanged for, renewed halfway through its
life, and not retried for 30 s after a failed renewal. Agents of one client that
share a home overwrite each other's file and exchange on every call; give each
its own `HOME`. Use the file form for a workload token that lives minutes and is
rotated: one in `LOOM_WORKLOAD_TOKEN` serves only until it expires. When the
exchange fails the agent has no credential: never `~/.loom/config.json`, never
`~/.loom/token`, never a sign-up, and the scope a person saved there is not used
either; SessionStart says so once.

Switches read on/true/1/yes and off/false/0/no. Defaults in parentheses.

| Setting                                                                                                                                       | Read by                       | Meaning                                                                                  |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- | ---------------------------------------------------------------------------------------- |
| `LOOM_API_KEY`, `LOOM_BASE_URL`                                                                                                               | every hook                    | the credential and the server                                                            |
| `LOOM_ORG`, `LOOM_NAMESPACE`, `LOOM_AGENT`                                                                                                    | every hook                    | identity; default from the token's claims (`coding` namespace)                           |
| `LOOM_PROJECT_NAME`, `LOOM_PROJECT_SCOPE` (tag)                                                                                               | every hook                    | project name override; `namespace` makes the project the namespace                       |
| `LOOM_REPO_REMOTE`                                                                                                                            | every hook                    | the remote the `repo:<id>` tag hashes                                                    |
| `LOOM_CLIENT` (claude-code)                                                                                                                   | every hook                    | the client name when no argument names it                                                |
| `LOOM_HOOK_TIMEOUT_MS`, `LOOM_HOOK_EXIT_MS`                                                                                                   | every hook                    | request bound and exit grace overrides                                                   |
| `LOOM_HOOK_DEBUG` (off)                                                                                                                       | hooks ending in `runHookMain` | swallowed errors to stderr, redacted                                                     |
| `LOOM_AUTO_SIGNUP` (on)                                                                                                                       | session-start, headers helper | first-session sign-up                                                                    |
| `LOOM_INJECT_CONTEXT` (on)                                                                                                                    | session-start                 | the primer                                                                               |
| `LOOM_CONTEXT_TOKEN_BUDGET` (1200), `LOOM_CONTEXT_RUNS` (off), `LOOM_CONTEXT_RUNS_LIMIT` (8)                                                  | session-start                 | primer budget and the runs shortlist                                                     |
| `LOOM_CODE_MAP` (on), `LOOM_CODE_MAP_TOKEN_BUDGET` (0 = no map)                                                                               | session-start                 | the repo map                                                                             |
| `LOOM_TASK_INBOX_LINE` (on)                                                                                                                   | session-start                 | the task inbox line                                                                      |
| `LOOM_COLLECTIONS_LINE` (on)                                                                                                                  | session-start                 | the collections line                                                                     |
| `LOOM_SESSION_BRIEF` (on), `LOOM_SESSION_BRIEF_PRIMER` (off), `LOOM_SESSION_BRIEF_KEY`, `LOOM_SESSION_BRIEF_TIMEOUT_MS` (90000)               | session-brief, session-start  | briefs, the primer line, a fixed key, the flusher's bound                                |
| `LOOM_PROMPT_RECALL` (first)                                                                                                                  | prompt-recall                 | first, on or off                                                                         |
| `LOOM_PROMPT_RECALL_MIN_CONF` (0.5), `LOOM_PROMPT_RECALL_TOP_K` (3), `LOOM_PROMPT_RECALL_BUDGET` (600), `LOOM_PROMPT_RECALL_MAX_CHARS` (1200) | prompt-recall                 | the confidence bar, hits per plane, pack budget, injected cap                            |
| `LOOM_PROMPT_PACK` (on), `LOOM_PROMPT_PACK_MIN_CONF` (0.4), `LOOM_PROMPT_PACK_TOP_K` (3), `LOOM_PROMPT_PACK_TIMEOUT_MS` (2500)                | prompt-pack                   | per-prompt code pointers                                                                 |
| `LOOM_RUN_LEDGER` (on), `LOOM_PLUGIN_RUN_RETAIN` (server default)                                                                             | run                           | the run ledger; keep, demote or expire the run's rows                                    |
| `LOOM_OBSERVE_SKIP_TOOLS`, `LOOM_OBSERVE_DEDUPE_S` (60)                                                                                       | capture                       | tools never observed; read dedupe window                                                 |
| `LOOM_CAPTURE_CONVERSATIONS` (on), `LOOM_CONV_SETTLE_MS` (350)                                                                                | conversation                  | capture; wait for the last reply to flush                                                |
| `LOOM_USAGE` (on)                                                                                                                             | usage                         | usage counters                                                                           |
| `LOOM_WORKING_MEMORY` (on), `LOOM_PLUGIN_SCRATCHPAD` (off)                                                                                    | working-memory                | the note; the per-turn scratchpad                                                        |
| `LOOM_SESSION_MEMORY` (on)                                                                                                                    | session-memory                | the session hand-off                                                                     |
| `LOOM_MENTOR_HINTS` (on)                                                                                                                      | mentor-hint                   | mentor hints                                                                             |
| `LOOM_NAMESPACE_HEALTH` (on)                                                                                                                  | namespace-health              | the first-Stop audit                                                                     |
| `LOOM_MCP_CODE_SCOPE` (on)                                                                                                                    | headers helper                | the X-Loom-Code-Scope header                                                             |
| `LOOM_MCP_HOOK_WAIT_MS` (5000)                                                                                                                | headers helper                | how long a first session waits for the hook's credential                                 |
| `LOOM_CODE_REPO` (unset), the `code_repo` option / config key                                                                                 | repo-scope, every code hook   | the fallback repository for the index: outside a checkout, or when the checkout has none |

The code-plane hooks' own settings (`LOOM_CODE_SHUNT`, `LOOM_SHUNT_*`,
`LOOM_FILE_CONTEXT*`, `LOOM_BLAST_*`, `LOOM_TEST_GATE*`, `LOOM_VALIDATE_GATE`,
`LOOM_TRACKER_SHUNT*`, `LOOM_CODE_LOCAL_FIRST`) are described with their
hooks above and in the plugin README.
