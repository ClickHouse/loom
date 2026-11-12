# Cursor

Cursor gets Loom in two independent pieces:

| Piece              | What it gives                                      | How                                                                                                                                |
| ------------------ | -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| MCP tools          | the `loom_*` tools in Cursor's agent               | the [install link](#mcp-tools) for a local single-user server, else `loom install --client cursor` ([MCP clients](mcp-clients.md)) |
| Transcript capture | each conversation's turns in `conversation.append` | the `sessionEnd` hook `integrations/cursor/cursor-session-hook.sh`                                                                 |

There is no Cursor plugin, so no code shunt and no capture of tool calls, runs or
working memory; the [Claude Code](claude-code.md) and [Codex](codex.md) plugins have those.

## MCP tools

Against a single-user server on the same machine, which takes MCP calls without a token,
[Add Loom to Cursor](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)
adds this entry under `mcpServers.loom` in Cursor's MCP settings, on the `coding` toolset
the dashboard gives Cursor; the link's `config` is the entry, base64-encoded:

```json
{ "url": "http://localhost:7777/mcp?toolset=coding" }
```

The entry points at the default port 7777; on another port, edit `url` in
`~/.cursor/mcp.json`. For any other server, write the entry with its token:

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

This merges a `mcpServers.loom` entry (`url` plus an `Authorization` header) into
`~/.cursor/mcp.json`. Restart Cursor afterwards. Toolsets, the agent label and the
per-client file table are in [MCP clients](mcp-clients.md#wire-a-client-with-loom-install).

## Transcript capture

A hook you register yourself. `cursor-session-hook.sh` hands a finished conversation's
transcript to `loom ingest-transcript`, which posts each turn to `conversation.append`.
Turns are stored only when the namespace has conversation logging on.

### Install

1. Install the `loom` CLI from a Loom checkout (`pip install -e .`) or from git
   (`pip install "git+https://github.com/ClickHouse/loom"`), with `python3` on `PATH`.
2. Copy the hook to `~/.cursor/hooks/cursor-session-hook.sh` and make it executable.
3. Register it in `~/.cursor/hooks.json`. Paths in the user file are relative to
   `~/.cursor/`; a project's `.cursor/hooks.json` resolves them from the project root.

   ```json
   {
     "version": 1,
     "hooks": {
       "sessionEnd": [{ "command": "./hooks/cursor-session-hook.sh", "timeout": 150 }]
     }
   }
   ```

4. Give the hook its environment. An app started from the dock does not see your shell
   profile, so keep the token in `~/.loom/token` (`loom mint-token --save` writes it) and
   set the rest in a wrapper that `hooks.json` points at:

   ```bash
   #!/usr/bin/env bash
   export LOOM_BASE_URL=https://loom.example.com LOOM_NAMESPACE=my-namespace
   exec "$HOME/.cursor/hooks/cursor-session-hook.sh" "$@"
   ```

5. Turn on conversation logging for the namespace (the **Record conversations** switch
   on the dashboard's Conversations page, or `conversation.set_logging`). Until then the
   server accepts the turns and stores none.

| Variable                     | Default                 | Meaning                                                                                         |
| ---------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------- |
| `LOOM_NAMESPACE`             | required                | namespace the thread is written to                                                              |
| `LOOM_API_KEY`               | `~/.loom/token`         | the token; the CLI reads it from the environment, so it never appears on the command line       |
| `LOOM_BASE_URL`              | `http://localhost:7777` | Loom server                                                                                     |
| `LOOM_AGENT`                 | `cursor`                | `agent` on the conversation rows                                                                |
| `LOOM_BIN`                   | `loom`                  | the CLI to run                                                                                  |
| `LOOM_CAPTURE_CONVERSATIONS` | `true`                  | `false` turns the hook into a no-op                                                             |
| `LOOM_INGEST_MAX_BYTES`      | `16777216`              | a larger transcript is skipped, not truncated                                                   |
| `LOOM_INGEST_TIMEOUT`        | `120`                   | seconds the CLI may run (needs `timeout` or `gtimeout`); keep it under the `hooks.json` timeout |

### What the hook reads

Cursor starts a hook as a process and writes one JSON object to its stdin. The hook reads
two fields and ignores the rest:

| Field             | Use                                                                              |
| ----------------- | -------------------------------------------------------------------------------- |
| `transcript_path` | the transcript file; `null` when transcripts are off, and then nothing is sent   |
| `conversation_id` | thread id `cursor-<conversation_id>` (only `[A-Za-z0-9._:-]`, at most 128 chars) |

`CURSOR_TRANSCRIPT_PATH` in the environment is the fallback for the path. Without a
usable `conversation_id`, the thread id is `cursor-` plus a hash of the transcript path.
Register `sessionEnd`; `stop` also works, and re-posts the whole transcript after every
agent turn.

The hook reads the JSONL Cursor writes under
`~/.cursor/projects/<project>/agent-transcripts/`: one object per line with `role` and
`message.content` (a string or a list of blocks). A `.json` array of `{role, content}` or
a `.md` file with `# user` / `# assistant` sections is passed through unchanged. Any other
file is skipped with a message on stderr.

### Capture boundary

- **Sent:** the text of `user` and `assistant` lines, in file order. Text blocks only:
  `tool_use` blocks, tool results, thinking and every other role are dropped on the
  machine.
- **Not sent:** the hook input's `user_email`, `workspace_roots` or model fields. The
  writer is whoever the token names; the rows carry `agent` and nothing from the Cursor
  account.
- **Not redacted:** the turn text goes as written. Keep secrets out of chats you capture,
  or leave the namespace's logging off.

### Behaviour

- **Never blocks Cursor.** In hook mode every outcome exits 0 and prints `{}` on stdout;
  the CLI's output goes to stderr.
- **Safe when Loom is unreachable.** The hook logs one line and exits 0, with no retry.
  The next run resends the transcript, and `conversation.append` skips turns that already
  landed ([turn ids](index.md#transcript-capture-hooks)).
- **Bounded.** The hook input is read up to 1 MiB, the transcript up to
  `LOOM_INGEST_MAX_BYTES`, the CLI for `LOOM_INGEST_TIMEOUT` seconds.
- **Quiet.** Diagnostics are one line on stderr prefixed `loom-cursor-hook:` and name no
  transcript text and no token.
- **Manual mode.** `cursor-session-hook.sh <transcript>` ingests one file and exits with
  the CLI's code (`2` configuration, `3` the transcript was skipped, otherwise the
  CLI's).

## Limits

- Stored turns are a record, not an extraction source: nothing is mined from them. To
  write memories from a conversation, post its turns to `memory.set_from_messages`.
- The transcript format is Cursor's, undocumented by Cursor; a format change shows up as
  "no user or assistant text in the transcript" on stderr.

## Related

- [Integrations](index.md#transcript-capture-hooks): the other transcript templates.
- [MCP clients](mcp-clients.md).
- [Conversations and sessions](../conversations-and-sessions.md).
- [Source README](../../../integrations/cursor/README.md).
