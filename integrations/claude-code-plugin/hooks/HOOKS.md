# Hook lifecycle

`hooks.json` is the Claude Code wiring; Claude Code reads it for the wiring
only and warns on any other key, so the notes live here. The Codex plugin
runs byte-identical copies of these scripts with its own wiring
(`integrations/codex-plugin/hooks/hooks.json`, which says what differs and
why). `hooks.generic.json` maps the same scripts onto a hand-wired client's
events. Claude Code runs each script as `node <script> <Event>` with no
shell; the other wirings as `sh bin/loom-node <script> <Event> [client]`,
where the launcher finds Node 18+ (README, "Which Node"). The event name is
the first argument, the client (`codex`, …) the optional second; the payload
is JSON on stdin.

## What every hook guarantees

- **It never fails the host session.** Every path exits 0 and a thrown error
  is swallowed. A hook that ends in `runHookMain` (`lib/loom-client.mjs`)
  writes that error's first line to stderr under `LOOM_HOOK_DEBUG=on`, with
  bearer tokens and JWTs redacted, also when `loom-record` runs it in the
  hook; nothing else reaches stderr. The one
  exception is a missing Node: Claude Code reports `node` not found, and the
  launcher exits 1 with one `loom:` line on stderr; both are non-blocking
  errors.
- **It is bounded.** Each request is aborted at the request bound: 5× the
  round trip SessionStart measured against the server, clamped to
  3,000–8,000 ms (`lib/hook-bounds.mjs`); `LOOM_HOOK_TIMEOUT_MS` overrides it.
  An unref'd exit timer at the same bound ends the process if a request
  hangs. Stdin is awaited for at most one second, and a payload over 32 MB is
  dropped unparsed.
- **A recording script does not hold the session.** The scripts that only
  send data print nothing the host reads. Claude Code's wiring starts them
  through `loom-record`. At a prompt, after a tool call and at Stop it writes
  the payload to a job file (`loom-record-<Event>-<ms>-<pid>.json`, mode 0600), starts
  one detached process that runs the event's recording scripts on it, and
  exits, so the host waits for node to start, one file write and one spawn,
  whatever the server's round trip. With no credential it starts nothing.
  The job file is on disk before the hook returns and the detached process
  has its own session and no inherited stdio, so the hand-off outlives the
  hook and the host process. It does not outlive a machine or container
  that is torn down when the client exits: what the process had not sent by
  then is lost. At SessionEnd `loom-record` therefore runs the scripts in
  its own process and exits when they have returned (**SessionEnd budget**,
  below). When the detached process cannot be started, the event's records
  are lost; when it dies, its job file stays, the client status check counts
  it, and `node loom-record.mjs <Event> - <job file>` replays it. Either
  way no cursor has moved, so the next Stop sends the conversation turns
  again. The job file holds the payload as the host sent it, the prompt or
  the tool input and output included; what the scripts send is redacted.
  That process has no stderr: to see a recording script's error under
  `LOOM_HOOK_DEBUG=on`, start the script itself with the payload on stdin.
  PreCompact stays direct, so the working-memory note is written before the
  compaction's primer reads it.
- **A prompt waits for one process.** `loom-record` runs the two lookups a
  prompt waits for (`loom-prompt-pack`, `loom-prompt-recall`) in the hook
  itself, side by side, and prints what they found as one
  `additionalContext`, the code pointers first, the recall on the next line.
  Before it starts the detached process it writes the run's begin to the
  session's run state (**loom-run**, below), with the checkout lookup the
  lookups share.
  The other gates and the scripts that inject context run as hooks of their
  own: the host reads their output.
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

| Event                                                          | Script                | Reads                              | Output                   | Side effects                                                                                                                                                                                                          | Off                                |
| -------------------------------------------------------------- | --------------------- | ---------------------------------- | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| SessionStart                                                   | loom-session-start    | cwd, session_id                    | primer text              | context.pack, code.repos/index_status/map, task.counts, namespace.get, session.brief_list, collection.list; GET /v1/health: its round trip is cached as the RTT bound, its `version` feeds the server version warning | `LOOM_INJECT_CONTEXT=false`        |
| UserPromptSubmit                                               | loom-record           | the payload                        | JSON additionalContext   | keeps the run's begin in the run state; starts one detached process, which runs the event's `detached` rows; runs the event's `in loom-record` rows itself and prints their context as one answer                     | each script's own                  |
| UserPromptSubmit, in loom-record                               | loom-prompt-pack      | prompt, cwd, session_id            | its context              | code.search                                                                                                                                                                                                           | `LOOM_PROMPT_PACK=off`             |
| UserPromptSubmit, in loom-record                               | loom-prompt-recall    | prompt, cwd, session_id            | its context              | context.pack                                                                                                                                                                                                          | `LOOM_PROMPT_RECALL=off`           |
| UserPromptSubmit, detached                                     | loom-run              | prompt, session_id                 | none                     | run.begin on the first prompt; appends the turn journal                                                                                                                                                               | `LOOM_RUN_LEDGER=off`              |
| UserPromptSubmit, detached                                     | loom-namespace-health | cwd, session_id                    | none                     | namespace.audit, once per session; the verdict goes to the session's state file                                                                                                                                       | `LOOM_NAMESPACE_HEALTH=off`        |
| PreToolUse Read\|Grep\|Bash                                    | loom-code-shunt       | tool_name, tool_input, cwd         | JSON permission decision | code.repos; one code.outline, code.symbol or tracker.duplicate_check before a deny; deny text                                                                                                                         | `LOOM_CODE_SHUNT=off`              |
| PreToolUse Edit\|Write\|MultiEdit\|NotebookEdit                | loom-file-context     | tool_input, session_id             | JSON additionalContext   | memory.for_file                                                                                                                                                                                                       | `LOOM_FILE_CONTEXT=off`            |
| PostToolUse loom tools                                         | loom-mentor-hint      | tool_name, tool_response           | JSON additionalContext   | none                                                                                                                                                                                                                  | `LOOM_MENTOR_HINTS=off`            |
| PostToolUse Edit\|Write\|MultiEdit                             | loom-blast-radius     | tool_input, cwd, transcript head   | JSON additionalContext   | code.impact; code.test_gate for a file's first edit (`LOOM_TEST_GATE=off` or `block` stops that one)                                                                                                                  | `LOOM_BLAST_RADIUS=off`            |
| PostToolUse Bash\|PowerShell                                   | loom-test-gate        | tool_input, cwd, transcript head   | JSON additionalContext   | code.test_gate for each changed file the command names, once per file                                                                                                                                                 | `LOOM_TEST_GATE=off` or `block`    |
| PostToolUse, PostToolUseFailure, Stop, SessionEnd (timeout 10) | loom-record           | the payload                        | none                     | after a tool call and at Stop, starts one detached process, which runs the event's `detached` rows; at SessionEnd, runs the event's `in loom-record` rows itself                                                      | each script's own                  |
| PostToolUse, PostToolUseFailure, detached                      | loom-capture          | tool_*, session_id, cwd            | none                     | observe.record; code.outcome; the recalled-keys stash                                                                                                                                                                 | no token                           |
| PreCompact                                                     | loom-capture          | session_id                         | none                     | observe.crystallize                                                                                                                                                                                                   | no token                           |
| PreCompact                                                     | loom-working-memory   | transcript, cwd                    | none                     | working_memory.set                                                                                                                                                                                                    | `LOOM_WORKING_MEMORY=off`          |
| PreCompact                                                     | loom-session-brief    | transcript, cwd                    | none                     | session.brief_save via the flusher                                                                                                                                                                                    | `LOOM_SESSION_BRIEF=off`           |
| Stop, detached                                                 | loom-capture          | session_id                         | none                     | observe.crystallize                                                                                                                                                                                                   | no token                           |
| Stop, detached                                                 | loom-conversation     | transcript, last_assistant_message | none                     | conversation.append via the flusher                                                                                                                                                                                   | `LOOM_CAPTURE_CONVERSATIONS=false` |
| Stop, detached                                                 | loom-usage            | transcript                         | none                     | cost.record_turns                                                                                                                                                                                                     | `LOOM_USAGE=off`                   |
| Stop                                                           | loom-test-gate        | transcript, cwd                    | JSON block once          | code.test_gate, when the transcript shows no test run since the last code change (`testedSince`); with LOOM_TEST_GATE=block, unless the named tests ran                                                               | `LOOM_TEST_GATE=off`               |
| Stop                                                           | loom-validate-gate    | transcript                         | JSON block once          | namespace.validate                                                                                                                                                                                                    | `LOOM_VALIDATE_GATE=off`           |
| Stop                                                           | loom-namespace-health | cwd, session_id                    | JSON block once          | none: tells the stored verdict once (namespace.audit is asked here by a wiring with no detached process, on the first Stop, and when the prompt's ask is overdue)                                                     | `LOOM_NAMESPACE_HEALTH=off`        |
| Stop, detached                                                 | loom-working-memory   | transcript, cwd                    | none                     | working_memory.set when the note changed                                                                                                                                                                              | `LOOM_WORKING_MEMORY=off`          |
| SessionEnd, in loom-record                                     | loom-capture          | session_id                         | none                     | observe.crystallize                                                                                                                                                                                                   | no token                           |
| SessionEnd, in loom-record                                     | loom-conversation     | transcript                         | none                     | the turns since the last Stop                                                                                                                                                                                         | as Stop                            |
| SessionEnd, in loom-record                                     | loom-run              | transcript, reason                 | none                     | run.complete, awaited                                                                                                                                                                                                 | `LOOM_RUN_LEDGER=off`              |
| SessionEnd, in loom-record                                     | loom-session-memory   | transcript                         | none                     | memory.set_from_messages via the flusher                                                                                                                                                                              | `LOOM_SESSION_MEMORY=off`          |
| SessionEnd, in loom-record                                     | loom-session-brief    | transcript, cwd                    | none                     | session.brief_save via the flusher                                                                                                                                                                                    | `LOOM_SESSION_BRIEF=off`           |

A `detached` row runs in the process `loom-record` starts and an
`in loom-record` row in `loom-record` itself, not as a hook of its own; the
Codex and generic wirings start those scripts directly, each at the event
its row names (`loom-namespace-health` at Stop only).
`hooks/loom-flush.mjs` is not wired to an event: the hooks above spawn it
detached (`lib/flush-job.mjs` holds its job shapes and delivery policy).

## Hooks

- **loom-session-start, first session.** With no credential anywhere
  (LOOM_API_KEY, the `token` option, ~/.loom/config.json, ~/.loom/token) the
  hook calls the server's agent self-signup once, writes the token to
  ~/.loom/config.json (0600) and the claim code to ~/.loom/claim-<namespace>.txt
  (0600), and the primer points the user to the claim page (`claim_url`), where
  the code works once with a password of their own; a single-user server
  returns its one identity, so there is no claim code and the primer just says it connected. The MCP
  headers helper may see neither the install options nor LOOM_API_KEY, so with
  no token it waits for this hook to save one (`LOOM_MCP_HOOK_WAIT_MS`, 5 s, under
  Codex's 10 s helper limit) before signing up through the same lock itself.
  `LOOM_AUTO_SIGNUP=off` disables
  it; a server without self-signup (404) or rate-limiting it (429) leaves the
  machine without a credential and the primer says how to get a token by
  hand. A token given as the `token` install option is first copied into
  ~/.loom/config.json, where the headers helper can read it. So is
  LOOM_API_KEY, or the token in the file LOOM_API_KEY_FILE names, where
  Claude Code runs the hook for its plugin (CLAUDE_PLUGIN_ROOT set, not an
  agent): it keeps both variables from the plugin's helper. None replaces a
  saved token: when the hooks use another token than the helper will send,
  from LOOM_API_KEY or the named file, or the helper will find none, the
  primer says to save the hooks' token in the config. There too,
  when that helper will send the config's token and the config names another
  server than the MCP entry's, the primer says so. The `server_url` option
  and whether the hook ran for the plugin are saved to a tmp state file
  (`loom-install-<hash>.json`, one per plugin install) for the status script,
  whose shell carries neither.
- **loom-session-start, primer.** One `context.pack` (mode startup, the
  project's working-memory block first, the recent-runs shortlist only with
  `LOOM_CONTEXT_RUNS=true`), plus: the repository's memory tag; in an indexed
  checkout the code scope line and, with a
  `LOOM_CODE_MAP_TOKEN_BUDGET` of 100 or more, the repo map; the task inbox line
  (`LOOM_TASK_INBOX_LINE=off`); the saved-briefs line (only with
  `LOOM_SESSION_BRIEF_PRIMER=on`); the collections line
  (`LOOM_COLLECTIONS_LINE=off`); where the session's toolset lists the
  conversation tools, the recording line, which states the owner's Record
  conversations switch; and a once-a-day notice when
  ~/.claude.json also mounts the same server. Outside an indexed checkout the
  code line says why no index was looked up (no checkout, no remote, not
  indexed, no answer in time), so the hook prints nothing only with
  `LOOM_CODE_MAP=false` and no other part to show.
- **loom-session-start, the plugin's own MCP entry.** Claude Code runs its
  plugin's hooks with CLAUDE_PLUGIN_ROOT, and that plugin mounts its own MCP
  entry: its helper binds loom_code_* calls to the checkout's repository when
  the checkout resolved to an indexed one, its URL names the `plugin` profile
  and its toolset is the `toolset` option. On a
  toolset with a `coding` part (the default) that profile's startup
  instructions say how to route a lookup, so the checkout is one line (the
  code namespace, the fetch time of a stalled mirror, the memory tag). The
  tag is one short instruction to save memory about the repository with it:
  the server adds the tag to a save only on a bound connection, and the hook
  cannot see whether the helper sent the header, so the model is told to pass
  it in every session of this entry. Any
  other toolset (`all`, `core`, `tracker`) gets the server's general rules,
  which route no code lookup, so its primer keeps the arbitration rules under
  the short scope line. The recording line is printed only when the toolset
  lists the conversation tools (`all`). A union holding `all`, `readonly` or
  `assistant` is no toolset the server serves and takes the deployment's
  default, so it keeps both the arbitration rules and the recording line. Any
  other wiring (Codex, whose hooks
  name their client, or a hand-wired client) mounts an entry the hook cannot
  read, so its primer keeps the full scope line, the arbitration rules, the
  full repository tag line and the recording line.
- **loom-session-start, collections line.** One `collection.list` for the
  session's org, cached a minute per (server, org, token, toolset); when the token
  reads shared collections, a few lines list up to six (how a search covers
  them is the server's own instructions' to say), those the caller holds most on first, one JSON line
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
  knows about the target file (its files[] facet, the person's namespace and
  the repository's team-rules namespace) and injects, as additionalContext, a
  heading the plugin writes, the server's text inside a recalled-data block
  whose tags carry an id only this install can compute, and after the
  block, in the plugin's words, how to remove a wrong team rule and read
  rows left out (each only when the server's text ends with that footer). One lookup per
  (session, file) per 30 min; fail-open; never a permission decision.
  LOOM_FILE_CONTEXT=off disables.
- **loom-code-shunt.** In a loom-indexed repo, a whole-file Read or `cat` of
  a file longer than LOOM_SHUNT_MAX_LINES (default 800) is denied with the
  file's declarations (lines, qualified name, header) from one code.outline
  call (its default kinds: functions, methods, classes, structs and the like,
  not interfaces, type aliases, enums or constants, which the denial says),
  and a one-file grep for a symbol with its definition from one
  code.symbol call, once per symbol per session. A denial needs that answer
  in full: an outline its 4,000-token budget cut short, no single definition,
  or no reply within 2.5 s lets the call run (pass reason index_unanswered),
  and a whole file whose lookup gave no declarations, answered or not, is
  asked about once per session.
  A Bash command of several parts (`;`, `&&`, `||`, a newline or a pipe) is
  never denied, since a denial withholds every part; its reads and greps are
  counted as passes, under compound_command where the same call alone would
  have asked the index, and cost no lookup.
  Fail-open; targeted reads (offset/limit, single file) and modified files
  pass. With LOOM_SHUNT_READ_CAP=<lines> (default 0 = off) a ranged Read
  whose span is longer than the cap on an indexed, unmodified file is denied
  too, with the declarations in those lines, at most twice
  per file per session ($TMPDIR/loom-shunt-cap-<hash of session_id>.json),
  then it passes as cap_spent. The deny text never names
  LOOM_CODE_SHUNT=off. The once-per-10-min repository lookup names
  the checkout's repo (code.repos repo=owner/name), has its own bound
  (LOOM_SHUNT_RESOLVE_TIMEOUT_MS, default 8000) and, when it does not answer,
  is recorded as unresolved and retried within a minute, never cached as
  not-indexed. A sed -n 'A,Bp' / head -n N / tail -n +A / awk NR-range slice
  of one indexed file in Bash is a ranged read under the same cap;
  tail -n N is judged as the file's last N lines.
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
  reported once per session. It is the one hook wired for an edit tool, so
  it also carries the test gate's line for the file's first edit (see
  loom-test-gate), which replaces its own count of tests where it names
  the file's tests: one line per edit. LOOM_BLAST_RADIUS=off disables the radius, not that line.
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
  only when the index is not abstaining and labels its own answer low-risk
  (`retrieval_risk`, banded by the deployment's settings); with
  `LOOM_PROMPT_PACK_MIN_CONF` set, when retrieval_confidence is at least that
  value instead. A search with no semantic leg (a server with no embedding
  model, a vector leg that failed or missed its deadline) reads `medium` at
  confidence 0.5 and sends no pointers; `LOOM_PROMPT_PACK_MIN_CONF=0.5` sends
  them there too. The pointers are candidates: the text never tells the
  model to stop searching.
- **loom-run.** The run ledger. The first prompt opens a run (goal = the
  prompt, run_id = cc-<session_id>, cx- for Codex; when run.begin answers
  another id, it is kept in the session's run state and every hook uses it;
  the SessionStart primer and the first prompt's recall run before that answer,
  so their context.pack exposure rows carry the derived id, or on a resumed
  session the previous run's id; a begin that timed out or hit a 408, 429 or 5xx is asked
  again on the next prompt with the same id, and closed at SessionEnd if it was
  never answered; any other 4xx is final for the session); every prompt is also
  appended to the turn journal, which clients without a parseable
  transcript read. Under Claude Code the prompt does not wait for the begin,
  and a tool observation sent before the answer carries the derived id as
  well: the hook writes the begin's body to the session's run state and the
  detached process asks it. A SessionEnd that comes before the answer, or
  before that process has started, asks the begin itself and closes the run:
  a process that starts after it asks nothing, and an answer that arrives
  after it is dropped. SessionEnd closes the run with the last
  assistant text, `outcome: none` and the recalled keys, awaited and then
  exited at once; the hook never claims which memories were used.
- **loom-validate-gate.** Collects the memory keys this session wrote
  (assistant tool_use blocks naming memory.set / update / update_by_id /
  apply), asks namespace.validate for a report scoped to exactly those keys,
  and blocks the stop once per distinct key set with the error and warning
  findings that name them, plus each check's remedy. info findings never
  block; the second stop on the same keys passes. LOOM_VALIDATE_GATE=off
  disables.
- **loom-namespace-health.** Once per session, one namespace.audit over the
  last day (sample_limit=0); a critical finding gives the agent one line
  naming the codes and /loom-optimize, as a block-once reason at a Stop.
  Under Claude Code the detached process of the session's first prompt asks
  and keeps the verdict in the session's state file, and the first Stop that
  finds it tells it with no call of its own; a Stop that comes while the
  audit is unanswered passes, and a later one tells, so a session whose only
  Stop comes before the answer is not told. A Stop that finds the ask older
  than twice the request bound with no verdict (the detached process died:
  a live one looks the checkout up and then answers or gives up inside one
  bound) asks in place. The verdict is the namespace's last day as of that
  prompt. A wiring that starts the script at Stop only (Codex, a hand-wired
  client) asks on the first Stop and waits for the answer. Later stops make
  no call.
- **loom-test-gate.** Names the tests of a changed file, and holds the
  stop of a session that ran no test since its last code change. By default it names
  them when the file changes: after a Bash or
  PowerShell command (and, through loom-blast-radius, after an edit tool)
  it takes the dirty paths the call named or targeted that were written
  since the session's first row, asks code.test_gate about each one not
  asked about yet this session (`adviceFiles`, `adviceLines` in
  lib/test-gate.mjs; at most four per call; a comment-only change is not
  asked about) and prints one line per file as additionalContext on that
  tool result: the commands of the tests the index links to the file that
  the checkout has, or the file's own command when it is a test. A file
  with no such test gets the changed functions the index links no test
  to. A file is asked about at its first change only, so a function
  changed later in it is not named; one the server gave no answer for is
  asked about once more. A test is named once per session, and not when
  the call's own command runs it in this checkout as a whole command
  (`runsHere`). The line costs no model request. A call that names no
  unasked dirty file costs one git call and no request; in a checkout the
  shared cache says is not indexed, no git call either.
  At Stop, with uncommitted changes this session made in a loom-indexed
  repo (dirty paths the transcript shows an edit tool targeted or a shell
  command named, written since the session's first row; a tree left dirty
  before the session gates nothing), it sends the change's paths, hunk
  ranges and function names only (`diffHeaders`) to code.test_gate for its
  obligations and blocks the stop once per distinct change with a reason
  naming them. By default a stop passes, and asks the server nothing, when
  the transcript holds a shell command that starts a test runner and
  finished no earlier than the newest write among the changed code files,
  documentation and prose left out by name
  (`testedSince`, `lastCodeChange`): the command's text is read for a
  runner's name only, not for which tests ran or how they went, so a run
  that failed, was piped or ran between `git stash` and `git stash pop`
  passes, in a Claude Code transcript or a Codex rollout. With no
  transcript the default holds no stop.
  With LOOM_TEST_GATE=block nothing is printed at edit time, and a Claude
  Code stop passes only when its transcript already answers the ask to run
  the named tests (`runAnswered` in lib/test-gate.mjs); a Codex rollout
  answers nothing there.
  LOOM_TEST_GATE=off disables both; LOOM_TEST_GATE_DEPTH sets hops (1-3).
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
  the next Stop. With the owner's Record conversations switch cached off it
  sends nothing and moves the cursor past the window. Without a parseable
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
  extracts the facts and the summary. A session with fewer than two turns, no
  user prompt or under 40 bytes of user and assistant text is not sent. The
  hook removes the session's recalled-key stash, whatever it sent
  (loom-capture removes its code-trace stash). The turn journal stays, because
  the other SessionEnd hooks run alongside it and read it too;
  loom-session-start removes this user's tmp state files untouched for 24
  hours. Every tmp state file the hooks keep is mode 0600, and none is read or
  written through a symlink or from a file another user owns.

## SessionEnd budget

Claude Code gives all SessionEnd hooks a shared 1.5 s budget by default.
Loom registers one, `loom-record`, which runs the event's five recording
scripts in its own process and exits when they have returned: about half a
second, most of it the conversation script's settle. It is not handed off,
because nothing may be left of the host a moment later. By the time the hook
returns `run.complete` is answered, `observe.crystallize` is sent, and the
flushers that carry session memory and the brief have run beside it for most
of that time; the last turns are handed to a flusher just before, and a host
torn down in that instant can lose them. The entry declares `timeout: 10`,
which raises the event budget to match: on the default budget a slow server
or a loaded machine gets the launcher killed with its requests unsent. Codex
gives the event one second and starts each script itself, so `loom-run.mjs`
awaits its `run.complete` and exits at once.

## Local-first mode (off by default)

`LOOM_CODE_LOCAL_FIRST=on` (or the plugin option `local_first`) is the one switch for people who want their working tree searched, not the index: the code guard stands down entirely, whatever the commits (every grep and Read runs locally), and the session primer says so and skips the repo map. Everyone else keeps the default: the index answers files without local changes however far behind the checkout or the index is — no commit-distance or fetch-overdue stand-down — and only modified files are read or grepped locally.

## Settings

Install-time options (`claude plugin install loom@loom --config key=value`)
reach the hooks as `CLAUDE_PLUGIN_OPTION_<KEY>`: `server_url` and `token`
(read by `lib/loom-client.mjs`), `toolset` (the MCP URL only),
`local_first` and `code_repo` (`CLAUDE_PLUGIN_OPTION_CODE_REPO`). Resolution order: the environment, then the option, then
the file `LOOM_API_KEY_FILE` names (token only), then
~/.loom/config.json (`LOOM_CONFIG_FILE`), then ~/.loom/token
(token only, with `LOOM_API_KEY_FILE` unset), then the plugin's `plugin-defaults.json`
(URL only), then http://localhost:7777. Claude Code gives the MCP headers
helper of a plugin neither the options nor `LOOM_API_KEY` or
`LOOM_API_KEY_FILE`: there it reads ~/.loom/config.json, then ~/.loom/token.

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
| `LOOM_PROMPT_PACK` (on), `LOOM_PROMPT_PACK_MIN_CONF` (unset: low-risk), `LOOM_PROMPT_PACK_TOP_K` (3), `LOOM_PROMPT_PACK_TIMEOUT_MS` (2500)    | prompt-pack                   | per-prompt code pointers                                                                 |
| `LOOM_RUN_LEDGER` (on), `LOOM_PLUGIN_RUN_RETAIN` (server default)                                                                             | run                           | the run ledger; keep, demote or expire the run's rows                                    |
| `LOOM_OBSERVE_SKIP_TOOLS`, `LOOM_OBSERVE_DEDUPE_S` (60)                                                                                       | capture                       | tools never observed; read dedupe window                                                 |
| `LOOM_CAPTURE_CONVERSATIONS` (on), `LOOM_CONV_SETTLE_MS` (350)                                                                                | conversation                  | capture; wait for the last reply to flush                                                |
| `LOOM_USAGE` (on)                                                                                                                             | usage                         | usage counters                                                                           |
| `LOOM_WORKING_MEMORY` (on), `LOOM_PLUGIN_SCRATCHPAD` (off)                                                                                    | working-memory                | the note; the per-turn scratchpad                                                        |
| `LOOM_SESSION_MEMORY` (on)                                                                                                                    | session-memory                | the session hand-off                                                                     |
| `LOOM_MENTOR_HINTS` (on)                                                                                                                      | mentor-hint                   | mentor hints                                                                             |
| `LOOM_NAMESPACE_HEALTH` (on)                                                                                                                  | namespace-health              | the session's namespace audit                                                            |
| `LOOM_MCP_CODE_SCOPE` (on)                                                                                                                    | headers helper                | the X-Loom-Code-Scope header                                                             |
| `LOOM_MCP_HOOK_WAIT_MS` (5000)                                                                                                                | headers helper                | how long a helper with no token waits for the hook to save one                           |
| `LOOM_CODE_REPO` (unset), the `code_repo` option / config key                                                                                 | repo-scope, every code hook   | the fallback repository for the index: outside a checkout, or when the checkout has none |

The code-plane hooks' own settings (`LOOM_CODE_SHUNT`, `LOOM_SHUNT_*`,
`LOOM_FILE_CONTEXT*`, `LOOM_BLAST_*`, `LOOM_TEST_GATE*`, `LOOM_VALIDATE_GATE`,
`LOOM_TRACKER_SHUNT*`, `LOOM_CODE_LOCAL_FIRST`) are described with their
hooks above and in the plugin README.
