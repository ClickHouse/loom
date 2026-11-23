# Source CLI execution

Source controls execute in the installed Python SDK so browse and read use the
maintained session-protection transport. The server and SDK remain separate
Python distributions. The server parser is available without loading the SDK;
execution requires a matching SDK installation.

## Distribution boundary

`server/cli/knowledge_source.py` owns the server command paths and common flags.
It imports `loom.source_cli` only when executing a command, checks
`SOURCE_CLI_API`, and calls `run_server_entry(args)`. An absent or incompatible
SDK produces an actionable error. There is no source-checkout path discovery or
bare HTTP fallback.

`sdk/python/loom/source_cli.py` owns exact operation fields, request-file parsing,
credential input, the standalone module parser and execution. The SDK wheel's
existing package includes the helper. Hosted contract tests compare both parser
trees and compare the helper's field sets with the server wire models. This
keeps server-only help and CLI snapshot generation independent of SDK imports.

## Retained authority

The trusted application provisions `SessionProtection` before private output and
persists its content-free pointer with `save_session_pointer`. Pointer persistence
is exclusive, mode 0600 and fsynced in an owned mode-0700 directory. Execution only
reopens existing authority with `open_session`; unknown or missing proof refuses.
The pointer and SDK authority must remain trusted application state across restore.

`loom.cli_context.session_environment` creates a content-free environment capsule
that pins the pointer file and its expected SDK pointer. The trusted launcher retains
it unchanged across subprocesses, tasks and resumes. Automatic source commands require
the capsule to match `--session-file`; replacing the pointer with another family
refuses. A configured context is never repaired or replaced after failure.

`call_source` binds that family to `Transport`. Writes and unknown routes enter the
same family's `automatic` context before constructing the transport. Only generated
SDK metadata classifies a known read-only operation. The maintained
transport performs final admission and response observation, including error
responses, before CLI output. Every dispatch disables transport retries and
rate-limit replay, supplies the original request UUID where present, and closes
the transport. Save reconciliation is an explicit inspection using that UUID.

The server's `_client` JSON/raw-file boundary, async OKF/Recall writers and shell
search/transcript requests lazily restore the same capsule through `loom.cli_context`.
The delegated SDK CLI binds its maintained clients through the SDK observation and
automatic scopes. Unbound HTTP calls retain their ordinary behavior. A configured
context requires the matching installed SDK and refuses before an unobserved request
when that dependency is missing. Raw uploads use `Transport.request(content=...)`;
streaming uses the maintained `Transport.stream` through consumption and cleanup.

Root `--request-intent explicit` binds one direct human dispatch. Its revocable
scope expires on return, including contexts copied by retained callbacks. SDK parser
arguments consume this intent once; no Agent, Transport or returned writer callback
stores it. A synchronous helper called from an active event loop creates and closes
its async client in a joined thread with copied context, without nesting event loops.

## Invariants

- One complete application session retains the same authority across tasks,
  namespaces, credentials and server origins. Source content cannot choose it.
- CLI execution never creates a fresh family to recover from missing or protected
  state. Direct user intent is a trusted application decision.
- Automatic guards apply to writes and unknown routes, including collection grants
  and recording writes. A namespace change cannot clear protection. Generated
  read-only operations remain available with the same response observation.
- Credentials come from explicit environment or private file/stdin inputs and
  are excluded from request-file fields and diagnostics.
- Direct database administration and nonparticipating external clients are outside
  this HTTP client qualification. Each application must retain contributing authority.

## Source entry points

- `server/cli/knowledge_source.py`: `register_knowledge_source_cli`, `_dispatch`.
- `sdk/python/loom/source_cli.py`: `run_server_entry`, `open_session`, `call_source`.
- `sdk/python/loom/cli_context.py`: `session_environment`, `request`, `stream`, `run_sync`.
- `server/cli/_session_context.py`: `invocation`, `send`, `post`.
- `tests/unit/test_cli_knowledge_source_installed.py`: installed parser and wire contracts.
- `sdk/python/tests/test_source_cli.py`: restored authority and actual SDK dispatch cases.

The [source CLI guide](../guides/mcp-source-cli.md) describes fields, collection
destinations and reconciliation. Release qualification must exercise both
installed artifacts and actual protected browse/read before activation.
