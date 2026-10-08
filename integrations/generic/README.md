# Generic transcript ingest

`ingest.sh` hands one exported chat transcript to `loom ingest-transcript`,
which posts each turn to `conversation.append` as one thread. Use it for any
tool that can export a chat to a file (Aider, Continue, OpenCode, JetBrains
AI), after the session or from that tool's own hook.

```bash
LOOM_NAMESPACE=my-namespace integrations/generic/ingest.sh --dry-run session.md
LOOM_NAMESPACE=my-namespace integrations/generic/ingest.sh session.md
```

## Input

| Format            | Shape                                                                                                   |
| ----------------- | ------------------------------------------------------------------------------------------------------- |
| `.json`           | a top-level array of `{"role": ..., "content": ...}` objects                                            |
| `.md`/`.markdown` | `# user` / `# assistant` (also `# system`, `# tool`) H1 headers; a turn is the text between two headers |

Anything else is refused before the CLI runs, as is a file over
`LOOM_INGEST_MAX_BYTES` (default 16 MiB). The CLI parses the whole file before
it sends the first turn, so a malformed item sends nothing.

## Environment

| Variable                | Default                 | Meaning                                                                                 |
| ----------------------- | ----------------------- | --------------------------------------------------------------------------------------- |
| `LOOM_NAMESPACE`        | required                | namespace the thread is written to                                                      |
| `LOOM_API_KEY`          | `~/.loom/token`         | the token; one of the two must exist. The CLI reads it from the environment, never argv |
| `LOOM_BASE_URL`         | `http://localhost:7777` | Loom server                                                                             |
| `LOOM_AGENT`            | `generic`               | `agent` on the conversation rows                                                        |
| `LOOM_BIN`              | `loom`                  | the CLI to run                                                                          |
| `LOOM_INGEST_MAX_BYTES` | `16777216`              | size limit                                                                              |

The writer is whoever the token names. Turns are stored only when the
namespace has conversation logging on (`conversation.set_logging`), and are
not mined for memories.

## Behaviour

- `--dry-run` prints the server, namespace, where the token comes from (not
  the token), the file size and the CLI's turn count, and sends nothing. It
  never prints transcript text; a parse error names the item index or JSON
  position only.
- `--thread-id ID` sets the thread. The default is a hash of the file's
  resolved path, so running the same file again, edited or not, appends to the
  same thread, and turns that already landed are skipped by `msg_id` (a hash
  of the turn's role, its text and how many identical turns come before it).
- A failure part-way (network, HTTP error) stops at that turn; the turns
  before it stay written. Run the same command again to finish: the landed
  turns are skipped. If the file moved in between, pass the same
  `--thread-id` both times.

| Exit | Meaning                                                            |
| ---- | ------------------------------------------------------------------ |
| 0    | ingested (or the dry run parsed)                                   |
| 2    | usage, or configuration missing: namespace, token, CLI             |
| 3    | the file is missing or unreadable                                  |
| 4    | unsupported format, over the size limit, or a malformed transcript |
| 5    | no turns parsed                                                    |
| 6, 7 | an HTTP error or a network error from the server at some turn      |
| 1    | the server stopped answering part-way                              |

## Test

`integrations/generic/test.sh` checks the argument, identity, format and size
gates against a fake CLI, and, with `PYTHON` set to a Loom virtualenv's
Python, the dry run through the real CLI.
