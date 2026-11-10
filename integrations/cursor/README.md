# Cursor transcript capture

`cursor-session-hook.sh` is a Cursor agent hook that hands a finished
conversation's transcript to `loom ingest-transcript`, which posts each turn
to `conversation.append`. For Loom's tools inside Cursor, use the MCP config
instead (`loom install --client cursor`, see [docs/features/integrations/mcp-clients.md](../../docs/features/integrations/mcp-clients.md));
the two are independent.

## Install

1. Install the `loom` CLI from a Loom checkout (`pip install -e .`) or from
   git (`pip install "git+https://github.com/ClickHouse/loom@<release tag>"`),
   and make sure `python3` is on `PATH`.
2. Copy the hook to `~/.cursor/hooks/cursor-session-hook.sh` and make it
   executable.
3. Register it in `~/.cursor/hooks.json` (paths in the user file are relative
   to `~/.cursor/`; a project's `.cursor/hooks.json` resolves them from the
   project root):

   ```json
   {
     "version": 1,
     "hooks": {
       "sessionEnd": [{ "command": "./hooks/cursor-session-hook.sh", "timeout": 150 }]
     }
   }
   ```

4. Give the hook its environment. Cursor passes its own environment to the
   hook, and an app started from the dock does not see your shell profile, so
   keep the token in `~/.loom/token` (`loom init` and `loom mint-token --save`
   write it there) and set the rest in a wrapper that `hooks.json` points at
   instead:

   ```bash
   #!/usr/bin/env bash
   export LOOM_BASE_URL=https://loom.example.com LOOM_NAMESPACE=my-namespace
   exec "$HOME/.cursor/hooks/cursor-session-hook.sh" "$@"
   ```

5. Turn on conversation logging for the namespace (the dashboard's **Record
   conversations** switch or `conversation.set_logging`). Until then the server accepts
   the turns and stores none.

| Variable                     | Default                 | Meaning                                                                                         |
| ---------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------- |
| `LOOM_NAMESPACE`             | required                | namespace the thread is written to                                                              |
| `LOOM_API_KEY`               | `~/.loom/token`         | the token; read by the CLI from the environment, never passed on the command line               |
| `LOOM_BASE_URL`              | `http://localhost:7777` | Loom server                                                                                     |
| `LOOM_AGENT`                 | `cursor`                | `agent` on the conversation rows                                                                |
| `LOOM_BIN`                   | `loom`                  | the CLI to run                                                                                  |
| `LOOM_CAPTURE_CONVERSATIONS` | `true`                  | `false` turns the hook into a no-op                                                             |
| `LOOM_INGEST_MAX_BYTES`      | `16777216`              | a larger transcript is skipped, not truncated                                                   |
| `LOOM_INGEST_TIMEOUT`        | `120`                   | seconds the CLI may run (needs `timeout` or `gtimeout`); keep it under the `hooks.json` timeout |

## What Cursor sends, and what the hook reads

Cursor starts a hook as a process and writes one JSON object to its stdin.
The hook reads these fields and ignores the rest:

| Field             | Use                                                                              |
| ----------------- | -------------------------------------------------------------------------------- |
| `transcript_path` | the transcript file; `null` when transcripts are off, and then nothing is sent   |
| `conversation_id` | thread id `cursor-<conversation_id>` (only `[A-Za-z0-9._:-]`, at most 128 chars) |

`CURSOR_TRANSCRIPT_PATH` in the environment is the fallback for the path.
Without a usable `conversation_id` the thread id is a hash of the transcript
path. `sessionEnd` is the event to register; `stop` also works and re-posts
the whole transcript after every agent turn.

Cursor documents the hook input but not the transcript's contents. The hook
reads the JSONL shape current Cursor builds write under
`~/.cursor/projects/<project>/agent-transcripts/`: one object per line with
`role` and `message.content`, where `content` is a string or a list of
blocks. A `.json` array of `{role, content}` or a `.md` file with `# user` /
`# assistant` sections is handed to the CLI unchanged. Any other file is
skipped with a message on stderr.

## Capture boundary

- Sent: the text of `user` and `assistant` lines, in file order. Text blocks
  only: `tool_use` blocks, tool results, thinking and every other role are
  dropped on this machine.
- Not sent: the hook input's `user_email`, `workspace_roots` or model fields.
  The writer is whoever the token names; the rows carry `agent` and nothing
  from the Cursor account.
- Not redacted: the turn text goes as written. Keep secrets out of chats you
  capture, or leave the namespace's logging off.

## Behaviour

- Never blocks Cursor. In hook mode every outcome exits 0 and prints `{}` on
  stdout (stdout is the hook's reply; a `stop` reply's `followup_message`
  would be submitted as a prompt, and exit 2 blocks the agent). The CLI's
  output goes to stderr.
- Safe when Loom is unreachable: the CLI fails, the hook logs one line and
  exits 0. It does not retry. The next run of the same conversation resends
  it, and turns that already landed are skipped by `msg_id` (a hash of the
  turn's role, its text and how many identical turns come before it), so a
  transcript that only grows is ingested once. A transcript Cursor rewrote in
  place sends its changed turns as new ones; the turns that landed stay.
- Bounded: the hook input is read up to 1 MiB, the transcript up to
  `LOOM_INGEST_MAX_BYTES`, the CLI for `LOOM_INGEST_TIMEOUT` seconds.
- Diagnostics are one line on stderr prefixed `loom-cursor-hook:` and name no
  transcript text and no token.
- Manual mode: `cursor-session-hook.sh <transcript>` ingests one file and
  exits with the CLI's code (`2` configuration, `3` the transcript was
  skipped, otherwise the CLI's).

## Test

`integrations/cursor/test.sh` runs the hook against a fake `loom` CLI and
checks the reply, exit codes, thread ids, the turns handed over, the off
switch, the size limit, the timeout, and that neither the token nor
transcript text reaches argv or stderr.
