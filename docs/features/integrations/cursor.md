# Cursor

Cursor gets Loom in three pieces:

| Piece              | What it gives                                      | How                                                                                                                                                     |
| ------------------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MCP tools          | the `loom_*` tools in Cursor's agent               | the [plugin](#plugin), the [install link](#mcp-tools) for a local single-user server, or `loom install --client cursor` ([MCP clients](mcp-clients.md)) |
| Skills             | the `loom` and `loom-code` skills                  | the [plugin](#plugin), or `npx skills add ClickHouse/loom --skill loom loom-code`                                                                       |
| Transcript capture | each conversation's turns in `conversation.append` | the `sessionEnd` hook `integrations/cursor/cursor-session-hook.sh`                                                                                      |

The Cursor plugin carries the MCP entry and the `loom` and `loom-code` skills, and has no
hooks: no code shunt and no capture of tool calls, runs or working memory; the
[Claude Code](claude-code.md) and [Codex](codex.md) plugins have those.

## Plugin

`.cursor-plugin/` at the repository root makes the repository a Cursor plugin marketplace
with one plugin, `clickhouse-loom`: the `loom` MCP server and the root `skills/`. The
plugin is not listed in Cursor's marketplace. Its local single-user connection and
read-only memory search are tested in Cursor. On a Teams or Enterprise plan, a team
admin adds it under **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo**
with `https://github.com/ClickHouse/loom`.

The plugin has one variable, **Loom MCP URL**. A team admin sets it in Cursor's
dashboard under **Plugins & MCPs**, for every member.

| Server                                     | Loom MCP URL                                             | Sign-in             |
| ------------------------------------------ | -------------------------------------------------------- | ------------------- |
| single-user, on each member's machine      | `http://localhost:7777/mcp?toolset=coding` (the default) | none                |
| team server with `LOOM_OAUTH_ENABLED=true` | `https://<your server>/mcp`                              | browser, per person |

A server with OAuth on takes the bare URL, the resource its protected-resource metadata
names; tools outside its default toolset run through `loom_op`. The entry sends no
`Authorization` header, so a team server without OAuth answers 401. Such a team skips the
plugin: `loom install --client cursor` [below](#mcp-tools) writes the entry with a token,
and `npx skills add` adds the skills.
[Clients that sign in by browser](mcp-clients.md#clients-that-sign-in-by-browser) has the
OAuth details.

## MCP tools

A single-user server on the same machine takes MCP calls without a token.
[Add Loom to Cursor](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)
adds this entry under `mcpServers.loom`, on the `coding` toolset the dashboard gives
Cursor. The link's `config` is the entry, base64-encoded:

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

Then add `.cursor/rules/loom.mdc` from [agent instructions](agent-instructions.md), so
the agent knows when to recall, save and use the code index, unless the repository's
`AGENTS.md` already carries the block.

### Connect an indexed checkout

Use a project MCP entry to bind code tools to this checkout's indexed repository.
The global `~/.cursor/mcp.json` is shared by every workspace; a static repository scope
there would send other checkouts to the same index. Cursor merges the project and global
files, with the project entry taking priority when both use the same server name
([Cursor configuration](https://cursor.com/help/customization/mcp)).

1. In your signed-in Loom dashboard, open **Settings → Tokens → New token**. Select
   **Only what you choose** and include the repository. Include a memory namespace too
   if the agent should save lessons, and select it under **Saves memories to**.
   A restricted repository must be named explicitly: `*` does not cover it, and a
   service account does not inherit its creator's direct grant. Minting from your own
   identity writes the token subject's direct grant, capped at your access. If an
   existing service-account token answers `cannot delegate access on <namespace>`,
   use your own dashboard identity to mint the token. See
   [restricted namespaces](../../guides/tokens-and-grants.md#restricted-namespaces-classificationrestricted).
2. Create `.cursor/mcp.json` in the checkout root, or merge this `loom` entry into its
   existing `mcpServers` object. Replace the URL, token and scope with your values;
   `acme/code-myrepo` means org `acme`, repository namespace `code-myrepo`:

   ```json
   {
     "mcpServers": {
       "loom": {
         "url": "https://loom.example.com/mcp?toolset=coding",
         "headers": {
           "Authorization": "Bearer <token>",
           "X-Loom-Code-Scope": "acme/code-myrepo"
         }
       }
     }
   }
   ```

   Keep the token-bearing file untracked. Exclude it locally and restrict its
   permissions (an exclude rule does not hide changes to an already tracked file):

   ```bash
   printf '\n/.cursor/mcp.json\n' >> "$(git rev-parse --git-path info/exclude)"
   chmod 600 .cursor/mcp.json
   ```

   For a tracked project config, use `Bearer ${env:LOOM_API_KEY}` instead of a literal
   token, and make that variable available to Cursor
   ([config interpolation](https://cursor.com/docs/mcp#config-interpolation)).

   The [code-scope header](../../reference/mcp.md#sessions-and-identity) selects the namespace for
   `code.*` calls that omit it; authorization still checks the token's access there.
   Memory writes that omit a namespace use the token's save namespace.

3. Restart Cursor, enable Loom in its MCP settings, and start a fresh chat. For Cursor
   CLI, run these from the checkout root:

   ```bash
   agent mcp enable loom
   agent mcp list-tools loom
   ```

   `enable` adds Loom to Cursor CLI's local approved list. Until it is approved,
   `list-tools` can report `MCP server "loom" has not been approved` even with a valid
   token and configuration. See [Cursor CLI MCP commands](https://cursor.com/docs/cli/mcp).

If an existing chat reports `Timed out waiting for connection` after the configuration
changes, reload the editor window and test from a fresh chat. That error alone does not
show whether the token can read the repository.

Tool discovery checks the connection, not access to the repository. Ask the agent to
call `loom_code_search` for a known function without a `namespace` argument, then check
the returned path and `as_of` commit. This verifies the checkout's scope and the token's
access to that index. When saved lessons exist for the hit, they can accompany the code
([code and lessons](../code-index/operations.md)).

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
