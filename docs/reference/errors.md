# Errors reference

Every refusal Loom makes carries a stable, upper-case `code`. The code is the
contract; the message is for people and can change in any release. Match on
`code`, never on message text.

## The error envelope over HTTP

A non-2xx response from `POST /v1/<op>` has one body shape:

```json
{
  "detail": {
    "code": "PRECONDITION_FAILED",
    "message": "…",
    "current_sha256": "…"
  }
}
```

- `detail.code` names the failure. `detail.message` explains it.
- Some codes add fields to `detail`: `errors` on a validation failure,
  `current_sha256` on a precondition failure, `retry_after_s` on the retryable
  refusals, `table` and `migration` on `SCHEMA_PENDING`. The table under
  [Common errors](#common-errors-and-what-to-do) names them.
- The `X-Request-Id` response header (echoed from your request when it is 1 to 64
  letters, digits, `.`, `_` or `-`, else a fresh UUID)
  identifies the call on the server's ledger and in its logs. Quote it in a bug
  report; the SDK errors carry it as `request_id` / `requestId`.
- An error that carries a retry hint also sets the standard `Retry-After`
  header, so a generic client or proxy can honour it.
- Every error with a `detail.code` carries a `Link` header to that code's entry in this
  catalogue, `Link: <…/docs/reference/errors.md#pinned_conflict>; rel="help"`
  (`server/core/errors.py`, `error_help_link`), so a client can show where to read about it.

Typed server errors subclass `LoomServerError` (`server/core/errors.py`), whose
`to_detail()` builds this body and whose `headers()` adds `Retry-After` when the
error has `retry_after_s`; `register_exception_handler` turns them into the
response. Handlers that refuse with a FastAPI `HTTPException` use the same
`{"code", "message", …}` detail, so a client sees one shape either way.
[System overview](../architecture/system-overview.md) shows the order in
which the gates that raise these errors run.

## Errors over MCP

MCP keeps two channels apart, because many clients discard session state on a
JSON-RPC error frame but show a tool result to the model:

- **A call that cannot run as sent is a tool result** with `isError: true`, a
  text block the model reads, and a `structuredContent` body. For a handler's
  4xx other than 401 (`LOCKED`, `PINNED_CONFLICT`, `BLACKLISTED`,
  `RESOURCE_NOT_FOUND`, `RATE_LIMITED`, `UNKNOWN_ARGUMENT`, …) the body is the
  HTTP `detail` plus `status`, and `retry_after_s` when the HTTP response would
  have carried `Retry-After`. Invalid arguments get a message that names each
  field error, each unknown key with the field it most likely meant, the
  required and optional arguments and one corrected example call.
- **Transport failures stay JSON-RPC errors**: parse error `-32700`, invalid
  request `-32600` (an auth refusal carries its HTTP status; a malformed request
  or a batch outside 1 to 64 answers 200), method
  not found `-32601`, invalid params `-32602` (a `tools/call` without a string
  `name` or with non-object `arguments`, an unknown resource), unsupported protocol version `-32022`, and a server fault
  `-32000 "tool execution failed"`. A fault Loom names (`OVERLOADED`,
  `BACKEND_TIMEOUT`, `SCHEMA_PENDING`, `EMBED_TIMEOUT`) puts its code and message in
  that error with the HTTP `detail` plus `status` in `error.data`; any other fault says
  only `internal error` and the request id.

Codes an op call answers only over MCP: `NAMESPACE_NOT_ACCESSIBLE` (a namespace the token
neither names nor holds a grant on), `ORG_MISMATCH` (an `org` argument the token
may not act in: not its own org, and the token is neither org-wide nor a platform admin's), `UNKNOWN_OP` (`loom_op` named no reachable op; the
result lists near matches with their arguments) and `OP_NOT_ON_MCP` (a hidden
op; the result names what to use instead). See [MCP](./mcp.md#errors).

## Errors in the SDKs

Both SDKs raise one exception type per family, chosen by `detail.code` and,
when a body carries no code, by HTTP status. Every exception keeps the server's
`code`, the HTTP `status`, the `request_id` and the whole `detail`. A code the
SDK has no class for raises the class its HTTP status maps to (a 409
`PINNED_CONFLICT` is a precondition failure), else the base class with that code
(a 423 `LOCKED`). The class tables are in
[Python SDK](./python-sdk.md#errors) and [TypeScript SDK](./typescript-sdk.md#errors).

## Retrying

| Code                   | Status | Retry?                                                                                                                                                                                                                                                                                                                                                                                           |
| ---------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `RATE_LIMITED`         | 429    | Yes, after `Retry-After` (`detail.retry_after_s`). The SDKs pace on the `X-RateLimit-*` headers and wait out a 429 for up to 15 s per call by default.                                                                                                                                                                                                                                           |
| `OVERLOADED`           | 503    | Yes, after `retry_after_s` (1 s by default). Any op is safe to resend: most requests are refused before they queue, and a `conversation.delete` or `conversation.compact` resend picks up what the failed call left (a compaction that failed before tombstoning its turns summarises again and replaces the summary it wrote).                                                                  |
| `AUTH_UNAVAILABLE`     | 503    | Yes, after `Retry-After` (5 s). The process has not loaded the token revocation list yet; the op did not run, so the SDKs retry it for any op.                                                                                                                                                                                                                                                   |
| `SCHEMA_PENDING`       | 503    | After the migration it names has run. No `Retry-After`: the wait is an operator action, not a timer.                                                                                                                                                                                                                                                                                             |
| `OVERRIDE_NOT_SHARED`  | 503    | After the runtime directory is fixed (permissions, space, or none private enough to use); the next update tries it again, and the server log says what failed. No `Retry-After`: nothing changed.                                                                                                                                                                                                |
| `CODED_WIDTH_MISMATCH` | 503    | After the embedder and the vector tables agree on a width; `/v1/health` (`coded_width_mismatch`) names the fix. A `memory.set`, `bulk_set`, `update` or `knowledge.*` write left nothing behind.                                                                                                                                                                                                 |
| `GATEWAY_REQUIRED`     | 503    | After a setting moves the call to a listed host: a model call named a host `LOOM_INFERENCE_GATEWAY_HOSTS` does not list while `LOOM_INFERENCE_GATEWAY_REQUIRED` (or a token exchange) is on. The error names the host. A server whose saved model configuration is refused answers every data-plane request with it, naming no host, until a replacement is saved or the configuration is reset. |
| `EMBED_TIMEOUT`        | 503    | The SDKs retry it for idempotent ops (`memory.search` is one: three attempts by default). A raw HTTP or MCP caller retries once, straight away: the search wrote nothing. If it repeats, the embedding provider is down; back off rather than retry in a loop.                                                                                                                                   |
| `BACKEND_TIMEOUT`      | 504    | Only an idempotent op, and preferably a narrower request. The statement exceeded its class deadline and the server does not retry it.                                                                                                                                                                                                                                                            |
| `INTERNAL_ERROR`       | 500    | An idempotent op, once. Report the `request_id` if it repeats.                                                                                                                                                                                                                                                                                                                                   |
| 4xx other than 429     | 4xx    | No. The same request gets the same answer; fix the request, the token or the data first.                                                                                                                                                                                                                                                                                                         |

`SCHEMA_PENDING`, `OVERRIDE_NOT_SHARED`, `CODED_WIDTH_MISMATCH` and `GATEWAY_REQUIRED`
are 503s because the server cannot serve the request in its current state, not because
the request is wrong; they send no `Retry-After` since only an operator's fix ends them,
and the SDKs never retry `CODED_WIDTH_MISMATCH` or `GATEWAY_REQUIRED`.

The SDKs' default policy: three attempts with exponential backoff; 408 and 5xx
retried for ops the registry marks idempotent (any other op only on a 408 or a
503 `AUTH_UNAVAILABLE`, which the server answers before running it); a read
timeout retried only for idempotent ops, whose `Idempotency-Key` the server
deduplicates; a `Retry-After` honoured up to 60 s. Each op's kind is in the
[operations reference](./operations.md).

## Common errors and what to do

| Code                                | HTTP | Cause                                                                                                                                                                                                                                                                                                                                                                                                                 | What to do                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------- | ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `VALIDATION_FAILED`                 | 422  | The body fails the op's input model. `detail.errors` names each field. A body that is not JSON at all is the same code with status 400.                                                                                                                                                                                                                                                                               | Read `detail.errors`; check field names and types against the op's schema (`/v1/api/openapi.json`, or `request_fields` in `ops.capabilities`).                                                                                                                                                                                                                        |
| `UNKNOWN_ARGUMENT`                  | 422  | An op that changes state (any op that is not read-only) got a key that is a near miss for one of its fields (`namepsace`, `tag`). Nothing was written.                                                                                                                                                                                                                                                                | Use the field in `did_you_mean`. Read ops serve unknown keys instead and name them in `ignored_args`; see [HTTP API](./http-api.md#unknown-and-removed-fields).                                                                                                                                                                                                       |
| `IDENTITY_INCOMPLETE`               | 400  | A namespace-scoped token called without `org` and `namespace`, or a `read_scope` read without the `user_id` / `session_id` it binds to.                                                                                                                                                                                                                                                                               | Put the identity in the body. This is not a permissions failure; the token may be fine.                                                                                                                                                                                                                                                                               |
| `AUTH_INVALID`                      | 401  | No bearer where auth is required, a header that is not `Bearer <token>`, or a malformed, expired, revoked or unverifiable token.                                                                                                                                                                                                                                                                                      | Mint or fetch a fresh token. A stale `LOOM_API_KEY` in the environment wins over `~/.loom/token`; check which one the client sends.                                                                                                                                                                                                                                   |
| `AUTH_INSUFFICIENT`                 | 403  | The token's role or namespace access does not cover the op (over MCP the result carries `required_role`).                                                                                                                                                                                                                                                                                                             | Compare the op's role in the [operations reference](./operations.md) with the token (`GET /v1/whoami`); ask a namespace admin for a grant.                                                                                                                                                                                                                            |
| `POLICY_FORBIDDEN`                  | 403  | A history-policy rule denied the call (`decision`, `determining_rules`, `errors`) with `LOOM_POLICY_MODE=enforce`.                                                                                                                                                                                                                                                                                                    | Read `determining_rules`; `errors` tells "denied by policy" from "could not evaluate". See [Tokens and grants](../guides/tokens-and-grants.md).                                                                                                                                                                                                                       |
| `RESOURCE_NOT_FOUND`                | 404  | The addressed row, namespace or document does not exist, or is not visible to the caller.                                                                                                                                                                                                                                                                                                                             | Check the identity tuple and key. Ops addressed by an org-wide id answer `found: false` rather than 404 for a row in another namespace, so they never confirm it exists.                                                                                                                                                                                              |
| `PRECONDITION_FAILED`               | 409  | `if_match_sha256` does not match the live row: another writer changed it. Under the Postgres authority, also an erasure that removed some of a snapshot's memories from the namespace while a `namespace.restore` of it ran.                                                                                                                                                                                          | `detail.current_sha256` is the live hash. Re-read if your change depends on the new value, then resend with that hash; no separate read is needed to retry a blind overwrite. For the restore (no hash), retry it: it leaves out the erased memories.                                                                                                                 |
| `PINNED_CONFLICT`                   | 409  | `memory.set` on a key, or close to a fact, that is pinned. A write that does not pin never replaces a pinned memory. `detail.pinned_blocker_id` names the blocking row.                                                                                                                                                                                                                                               | Change it with `memory.update` (keeps the pin) or `memory.pin` (re-pins), or store the new fact under its own key.                                                                                                                                                                                                                                                    |
| `IDEMPOTENT_CONFLICT`               | 409  | An `Idempotency-Key` (or MCP `_idempotency_key`) reused with a different body for the same op and caller.                                                                                                                                                                                                                                                                                                             | Use a new key per distinct request. Resending the original body with the old key replays the stored answer.                                                                                                                                                                                                                                                           |
| `READS_LLM_FREE`                    | 409  | `memory.ask` or `code.ask` asked for a model's answer while `LOOM_READS_LLM_FREE` is on.                                                                                                                                                                                                                                                                                                                              | Not retried: resending the same request is refused again. Ask with `allow_synthesis=false` / `synthesize=false` for the recalled evidence.                                                                                                                                                                                                                            |
| `IDEMPOTENT_IN_PROGRESS`            | 503  | The first request with this `Idempotency-Key` still runs (on any replica).                                                                                                                                                                                                                                                                                                                                            | Retry after `Retry-After` with the same key and body; the SDKs do. The retry gets the first request's answer.                                                                                                                                                                                                                                                         |
| `BLACKLISTED`                       | 422  | The value matches a refuse pattern registered with `blacklist.add` (`pattern_id`, `scope`, `reason`), or, with an empty `pattern_id`, the request's values are too long to check against the regexes that apply (a short value can be refused once earlier entries spent the budget): send less in one request, or remove regex patterns.                                                                             | Do not retry the value. If the pattern is wrong, an admin removes it (`blacklist.remove`). With an empty `pattern_id`, send the value in a smaller request; when the message says to shorten the value, it is too long to check even alone.                                                                                                                           |
| `LOCKED`                            | 423  | The row carries a `memory.lock` that refuses this write.                                                                                                                                                                                                                                                                                                                                                              | Wait for `lock_until` to pass, or have a namespace admin release it with `memory.unlock`.                                                                                                                                                                                                                                                                             |
| `RATE_LIMITED`                      | 429  | A namespace, org or actor token bucket is empty for the op's class.                                                                                                                                                                                                                                                                                                                                                   | Wait `Retry-After`. Sustained 429s mean the caller needs pacing or a batch op (`memory.bulk_set`); limits are in [HTTP API](./http-api.md#rate-limits).                                                                                                                                                                                                               |
| `OVERLOADED`                        | 503  | Every pooled ClickHouse connection of the serving process stayed busy longer than `LOOM_CH_POOL_WAIT_MS`, a write waited more than 5 s for its org's share of the blacklist's regex threads (refused, not stored unchecked), a budget or rate-limit update waited more than a second for another web process's, or a conversation delete or compaction met concurrent writes to the thread's embeddings for 8 rounds. | Retry after `retry_after_s`. From `conversation.delete` or `conversation.compact`, the work done so far stays: resend once the thread's writes settle. Repeated across callers, the deployment needs more capacity or has a slow query holding connections; see [Troubleshooting](../operations/troubleshooting.md).                                                  |
| `BACKEND_TIMEOUT`                   | 504  | A ClickHouse statement ran past its class deadline (by default reads 30 s, writes 60 s, DDL 300 s: `LOOM_CH_TIMEOUT_READ_S`, `_WRITE_S`, `_DDL_S`); `detail.statement_class` and `timeout_s` say which.                                                                                                                                                                                                               | Narrow the request (a smaller `top_k`, a namespace or path filter). Retry only an idempotent op.                                                                                                                                                                                                                                                                      |
| `SCHEMA_PENDING`                    | 503  | The call needs a table from a migration this build carries but the database has not applied (`table`, `migration`).                                                                                                                                                                                                                                                                                                   | An operator runs `loom migrate` (or restarts with `LOOM_BOOT_MIGRATE=true`); see [Upgrades](../operations/upgrades.md).                                                                                                                                                                                                                                               |
| `EMBED_TIMEOUT`                     | 503  | The embedding provider did not answer a search's query embedding within `LOOM_MEMORY_SEARCH_EMBED_BOUND_S` on any attempt (`LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`).                                                                                                                                                                                                                                                 | A raw HTTP or MCP caller retries once; an SDK caller has already retried, so it backs off. Repeated, the provider is slow or down: the log's `query embed exceeded` warnings and `loom.agent_events` rows with this code show it (a hung provider sets no health flag); see [Troubleshooting](../operations/troubleshooting.md#searches-fail-with-503-embed_timeout). |
| `UNKNOWN_OP`, `OP_NOT_ON_MCP` (MCP) | —    | `loom_op` named an op that does not exist or is not reachable on this connection, or a hidden op was called over MCP.                                                                                                                                                                                                                                                                                                 | Use the suggestion in the result (`did_you_mean`, `instead`), or `loom_op {"op": "help", "query": "<words>"}` to list ops.                                                                                                                                                                                                                                            |

## How the entries below are generated

`scripts/codegen/docs_reference.py` (`render_errors`) renders one entry per code, headed
by the code so its lowercase form is the anchor the `Link` header names:

- **Typed errors**: every `LoomServerError` subclass under `server/`, with its HTTP
  status (fixed by the class), the class and the first sentence of its docstring.
- **Other codes**: every `"code": "X"` dictionary entry and `code="X"` keyword
  argument under `server/` that is not a typed error, with the modules that
  contain it. This catches per-op codes (tracker source registration answers
  `HOST_REFUSED`, `CREDENTIAL_NOT_READONLY` or `CONTRACT_FAILED`, for example)
  that a hand-written list would miss. Because the scan is syntactic, it also
  lists a few codes the server records on its own ledger rather than sending
  (`HANDLER_HTTP_ERROR`), and codes answered outside the op endpoints (sign-up,
  OAuth, admin routes).
- A code the "Common errors" table above covers links back to it.

Each op documents its own codes in its description; they all use the envelope
above.

## Error codes

<!-- BEGIN GENERATED: errors -->
<!-- Generated by `python scripts/codegen/docs_reference.py`; edit the source, not this region. -->
<!-- prettier-ignore-start -->

Each code below is an anchor: `errors.md#<code in lowercase>` is the address an HTTP error's `Link` header names.

#### `ACTIVITY_UNAVAILABLE`

Raised in `server/memory/app/profile.py`.

#### `ADMISSION_CONTENDED`

HTTP 503, raised as `server.memory.store.pg_memory.AdmissionContendedError`. 503 — commits kept landing in this write's scope faster than it could re-check against them.

#### `AMBIGUOUS_KEY`

Raised in `server/memory/app/update.py`.

#### `AUTH_INSUFFICIENT`

HTTP 403, raised as `server.core.errors.AuthInsufficientError`. A public 403 that remains catchable as ``PermissionError`` internally. What to do: [common errors](#common-errors-and-what-to-do).

#### `AUTH_INVALID`

Raised in `server/auth/bearer.py`, `server/auth/delegation.py`, `server/auth/login.py`. What to do: [common errors](#common-errors-and-what-to-do).

#### `AUTH_UNAVAILABLE`

Raised in `server/auth/bearer.py`.

#### `BACKEND_TIMEOUT`

HTTP 504, raised as `server.core.errors.BackendTimeoutError`. 504 — a ClickHouse statement ran past its class deadline (reads 30 s, writes 60 s, DDL 300 s). What to do: [common errors](#common-errors-and-what-to-do).

#### `BACKEND_UNAVAILABLE`

HTTP 503, raised as `server.core.errors.BackendUnavailableError`. 503 — ClickHouse could not be reached after the bounded reconnect, or the document bucket failed a read, write or delete.

#### `BAD_BRANCH`

Raised in `server/code/app/repo_admin.py`.

#### `BAD_BRIEF_KEY`

Raised in `server/memory/app/session_brief.py`.

#### `BAD_CLASSIFICATION`

Raised in `server/code/app/repo_admin.py`.

#### `BAD_CONFIG`

Raised in `server/knowledge/app/sources.py`.

#### `BAD_CONSTRAINT`

Raised in `server/memory/app/records.py`.

#### `BAD_CURSOR`

Raised in `server/memory/app/recall.py`.

#### `BAD_ENUM_FIELD`

Raised in `server/memory/app/records.py`.

#### `BAD_FIELD_NAME`

Raised in `server/memory/app/records.py`, `server/memory/store/record_query.py`.

#### `BAD_FIELD_VALUE`

Raised in `server/memory/domain/record_values.py`.

#### `BAD_INPUT`

Raised in `server/tracker/app/sources.py`.

#### `BAD_JOIN_POLICY`

Raised in `server/code/app/repo_admin.py`.

#### `BAD_KIND`

Raised in `server/knowledge/app/sources.py`.

#### `BAD_MAPPING`

Raised in `server/tracker/app/sources.py`.

#### `BAD_NAME`

Raised in `server/knowledge/app/sources.py`, `server/tracker/app/sources.py`.

#### `BAD_NAMESPACE`

Raised in `server/tracker/app/sources.py`.

#### `BAD_ORG`

Raised in `server/tracker/app/sources.py`.

#### `BAD_PATTERN`

Raised in `server/memory/app/context_offload.py`.

#### `BAD_REPO`

Raised in `server/code/app/repo_admin.py`, `server/tracker/app/sources.py`.

#### `BAD_REQUEST`

Raised in `server/transport/mcp_call.py`.

#### `BAD_SHA`

Raised in `server/code/app/bisect.py`.

#### `BAD_TIME`

Raised in `server/code/app/bisect.py`, `server/code/domain/time_window.py`.

#### `BLACKLISTED`

Raised in `server/memory/app/blacklist_guard.py`. What to do: [common errors](#common-errors-and-what-to-do).

#### `BLACKLIST_PATTERN_INVALID`

Raised in `server/memory/app/blacklist.py`.

#### `BLOB_GONE`

Raised in `server/knowledge/app/download_routes.py`.

#### `BLOCK_PROMOTION_REFUSED`

Raised in `server/memory/app/promote.py`.

#### `BRIEF_KEY_REQUIRED`

Raised in `server/memory/app/session_brief.py`.

#### `BRIEF_REVISION_MISMATCH`

Raised in `server/memory/app/session_brief.py`.

#### `BRIEF_SELECTOR_REQUIRED`

Raised in `server/memory/app/session_brief.py`.

#### `CATEGORY_NOT_FOUND`

Raised in `server/memory/app/ops.py`.

#### `CHAR_LIMIT_EXCEEDED`

Raised in `server/memory/app/bulk_rows.py`, `server/memory/app/update.py`, `server/memory/app/write.py`.

#### `CODED_WIDTH_MISMATCH`

HTTP 503, raised as `server.core.errors.CodedWidthMismatchError`. 503 — a vector in the write is not the width its `Quantized` column was created at, so ClickHouse would refuse the insert.

#### `CONFIG_DRIFT`

Raised in `server/transport/model_maintenance.py`.

#### `CONFIRM_CANONICAL_KEY_MISMATCH`

Raised in `server/memory/app/dedup.py`.

#### `CONFIRM_DUPLICATE_URI_MISMATCH`

Raised in `server/knowledge/app/dedup.py`.

#### `CONFIRM_REQUIRED`

Raised in `server/code/app/repo_admin.py`, `server/knowledge/app/sources.py`, `server/tracker/app/sources.py`.

#### `CONFIRM_SUBJECT_MISMATCH`

Raised in `server/memory/app/handlers.py`.

#### `CONFIRM_THREAD_ID_MISMATCH`

Raised in `server/knowledge/app/conversations.py`.

#### `CONSTRAINT_VIOLATION`

Raised in `server/memory/domain/record_values.py`.

#### `CONTRACT_FAILED`

Raised in `server/tracker/app/sources.py`.

#### `CREDENTIAL_NOT_READONLY`

Raised in `server/tracker/app/sources.py`.

#### `CREDENTIAL_READONLY_MODE`

Raised in `server/tracker/app/sources.py`.

#### `CREDENTIAL_REFUSED`

Raised in `server/knowledge/app/sources.py`.

#### `CROSS_SITE_REQUEST`

Raised in `server/main.py`.

#### `DEDUP_PAIR_NOT_FOUND`

Raised in `server/memory/app/dedup.py`.

#### `DUPLICATE_FIELD`

Raised in `server/memory/app/records.py`.

#### `EMAIL_IN_USE`

Raised in `server/auth/self_signup.py`.

#### `EMBED_TIMEOUT`

HTTP 503, raised as `server.core.errors.EmbedTimeoutError`. 503 — the query embedding did not answer within `memory_search_embed_bound_s` on any attempt. What to do: [common errors](#common-errors-and-what-to-do).

#### `EMPTY_PREDICATE`

Raised in `server/memory/store/record_query.py`.

#### `ENTRY_LEASE_NOT_HELD`

HTTP 409, raised as `server.code.app.ingest.EntryLeaseNotHeld`. The pass that built this upload no longer holds its entry's lease: another pass may be indexing the entry, so its batch must not complete.

#### `ENUM_VIOLATION`

Raised in `server/memory/domain/record_values.py`.

#### `EXPIRED`

Raised in `server/memory/app/update.py`.

#### `EXTRACTION_FAILED`

Raised in `server/memory/app/extract/handler.py`.

#### `EXTRACTION_QUEUE_UNAVAILABLE`

Raised in `server/memory/app/extract/handler.py`.

#### `FORBIDDEN`

Raised in `server/memory/app/verify.py`.

#### `GATEWAY_REQUIRED`

HTTP 503, raised as `server.core.errors.GatewayRequiredError`. 503 — a model call named a host LOOM_INFERENCE_GATEWAY_HOSTS does not list while the gateway is required (`LOOM_INFERENCE_GATEWAY_REQUIRED`, or a workload token); the message names the host.

#### `GRANT_BUSY`

HTTP 503, raised as `server.core.errors.GrantBusyError`. 503 — another change to the same grant held it past the wait.

#### `GRANT_WOULD_DOWNGRADE`

HTTP 409, raised as `server.core.errors.GrantWouldDowngradeError`. 409 — `grants.create` asked for a role below the subject's current active grant on that namespace.

#### `HANDLER_HTTP_ERROR`

Raised in `server/transport/http.py`.

#### `HIGH_SENSITIVITY_PROMOTION_REFUSED`

Raised in `server/memory/app/promote.py`.

#### `HOST_REFUSED`

Raised in `server/tracker/app/sources.py`.

#### `IDEMPOTENT_CONFLICT`

Raised in `server/transport/http.py`. What to do: [common errors](#common-errors-and-what-to-do).

#### `IDEMPOTENT_TAKEN_OVER`

HTTP 503, raised as `server.memory.store.pg_memory.IdempotencyFenced`. 503 — this request's idempotency claim is no longer its own: a retry with the key took it over, or an erasure in the org fenced it.

#### `IDENTITY_INCOMPLETE`

HTTP 400, raised as `server.core.errors.IdentityIncompleteError`. A scoped token was presented without org+namespace in the request body. What to do: [common errors](#common-errors-and-what-to-do).

#### `INTEGRATION_NOT_CONFIGURED`

Raised in `server/insights/app/admin_routes.py`.

#### `INTERNAL_ERROR`

HTTP 500, raised as `server.core.errors.LoomServerError`. Base class.

#### `INVALID_AGG`

Raised in `server/memory/app/query.py`.

#### `INVALID_EMAIL`

Raised in `server/auth/self_signup.py`.

#### `INVALID_FORMAT`

Raised in `server/memory/app/profile.py`.

#### `INVALID_IDENTIFIER`

Raised in `server/memory/app/query.py`.

#### `INVALID_SCOPE`

Raised in `server/memory/app/profile.py`.

#### `KEY_HOLDS_FACT`

Raised in `server/memory/app/write.py`.

#### `LOCKED`

Raised in `server/memory/domain/lock.py`. What to do: [common errors](#common-errors-and-what-to-do).

#### `MAPPING_FAILED`

Raised in `server/tracker/app/sources.py`.

#### `MISDIRECTED_REQUEST`

Raised in `server/main.py`.

#### `MISSING_IDENTITY_FIELD`

Raised in `server/memory/app/decision.py`, `server/memory/app/records.py`.

#### `MISSING_REQUIRED_FIELD`

Raised in `server/memory/domain/record_values.py`.

#### `MISSING_SCOPE`

Raised in `server/memory/app/handlers.py`.

#### `MODEL_CALLS_DISABLED`

HTTP 403, raised as `server.llm.customer.ModelCallsDisabled`. The org's admin switched its model calls off: nothing is sent for it.

#### `MODEL_CALL_DEFERRED`

HTTP 503, raised as `server.core.errors.ModelCallDeferred`. 503 — the inference gateway asked the call's identity to wait for `retry_after_s`, its `Retry-After`.

#### `MODEL_CALL_REFUSED`

HTTP 403, raised as `server.core.errors.ModelCallRefused`. 403 — the inference gateway refused the call for the identity it was made for: that identity's refusal, not the provider's failure.

#### `MODEL_CALL_UNATTRIBUTED`

HTTP 500, raised as `server.llm.customer.Unattributed`. A model call with no customer to bill: a caller that set none (a job that did not name the org of its rows).

#### `MODEL_CHANGE_IN_PROGRESS`

Raised in `server/transport/model_maintenance.py`.

#### `MODEL_VALIDATION_FAILED`

Raised in `server/insights/app/provider_activation.py`.

#### `NAMESPACE_CLAIMED`

Raised in `server/auth/self_signup.py`.

#### `NAMESPACE_HAS_ADMIN`

Raised in `server/insights/app/admin_routes.py`.

#### `NAMESPACE_NOT_ACCESSIBLE`

Raised in `server/transport/mcp_auth.py`.

#### `NAMESPACE_NOT_FOUND`

Raised in `server/identity/app/claim_release.py`, `server/insights/app/admin_routes.py`.

#### `NAMESPACE_TAKEN`

Raised in `server/tracker/app/sources.py`.

#### `NOT_A_CODE_NAMESPACE`

Raised in `server/knowledge/app/sources.py`.

#### `NOT_A_REPO`

Raised in `server/code/app/repo_admin.py`.

#### `NOT_A_SOURCE`

Raised in `server/tracker/app/sources.py`.

#### `NOT_A_TRACKER_NAMESPACE`

Raised in `server/code/app/repo_admin.py`.

#### `NOT_FOUND`

Raised in `server/identity/app/me_service_accounts.py`, `server/knowledge/app/download_routes.py`, `server/knowledge/app/writes.py`, `server/memory/app/blacklist.py`.

#### `NO_FIELDS_PROVIDED`

Raised in `server/knowledge/app/conversations.py`.

#### `NO_ROOT_SECRET`

Raised in `server/knowledge/app/sources.py`.

#### `NO_SIGNING_KEY`

HTTP 503, raised as `server.auth.keyring.NoSigningKey`. Kid `default` is retired (or the root secret signs neither HS256 nor RS256) and no keyring key is active here: nothing can be signed until an operator acts, so it carries no Retry-After.

#### `OP_NOT_ON_MCP`

Raised in `server/transport/mcp_call.py`, `server/transport/mcp_op.py`.

#### `ORG_MISMATCH`

Raised in `server/transport/mcp_auth.py`.

#### `OVERLOADED`

HTTP 503, raised as `server.core.errors.OverloadedError`. 503 — too busy, retry: the ClickHouse pool or its streams, the document bucket's transfers or streams, an override's lock, a namespace's document-source lease, the blacklist's regex matchers, or a… What to do: [common errors](#common-errors-and-what-to-do).

#### `OVERRIDE_NOT_STORED`

HTTP 409, raised as `server.core.runtime_overrides.OverrideNotStoredError`. 409 — ClickHouse refused to store the override (a read-only user, for example), so no process changed; the server log has the reason.

#### `PASSWORD_LOGIN_SSO`

Raised in `server/auth/login.py`.

#### `PINNED_CONFLICT`

Raised in `server/memory/app/set_commit.py`, `server/memory/app/update.py`, `server/memory/app/write.py`. What to do: [common errors](#common-errors-and-what-to-do).

#### `POLICY_FORBIDDEN`

HTTP 403, raised as `server.identity.app.policy_gate.PolicyForbiddenError`. 403 with the decision attached: which rules denied, per-leaf values, and any evaluation errors — "denied by policy" and "denied because the check could not run" are distinguishable by `errors`. What to do: [common errors](#common-errors-and-what-to-do).

#### `POSTGRES_REFUSED`

HTTP 500, raised as `server.core.pg.PostgresRefusedError`. 500 — Postgres refused a statement with a data or constraint error.

#### `POSTGRES_UNAVAILABLE`

HTTP 503, raised as `server.core.pg.PostgresUnavailableError`. 503 — Postgres, the memory authority, could not answer.

#### `PRECONDITION_FAILED`

HTTP 409, raised as `server.core.errors.PreconditionFailedError`. 409 — `if_match_sha256` mismatch, or an erasure that landed while a `namespace.restore` ran. What to do: [common errors](#common-errors-and-what-to-do).

#### `PROJECTION_BEHIND`

HTTP 503, raised as `server.memory.app.pg_admission.ProjectionBehindError`. 503 — earlier commits in this namespace have not reached search, so a conflict check, a search, a search trace, a blacklist sweep or a namespace.inventory now would miss them.

#### `PUBLICATION_PENDING`

HTTP 503, raised as `server.memory.store.pg_memory.PublicationPending`. 503: the write is committed and the delivery worker publishes its rows; it is not to be made again.

#### `RATE_LIMITED`

HTTP 429, raised as `server.core.errors.RateLimitedError`. 429 — the caller is over one of its admission limits (the org's in-flight requests); `retry_after_s` is the hint the SDK's retry layer honours. What to do: [common errors](#common-errors-and-what-to-do).

#### `READS_LLM_FREE`

HTTP 409, raised as `server.core.errors.ReadsLlmFreeError`. 409 — `memory.ask` or `code.ask` asked for a model's answer while `LOOM_READS_LLM_FREE` is on. What to do: [common errors](#common-errors-and-what-to-do).

#### `RECALL_REQUIRES_FILTER`

Raised in `server/memory/app/recall.py`.

#### `REMOTE_REFUSED`

Raised in `server/code/app/repo_admin.py`.

#### `REPO_EXISTS`

Raised in `server/code/app/repo_admin.py`.

#### `REPO_FROM_CONFIG`

Raised in `server/code/app/repo_admin.py`.

#### `REPO_NOT_INDEXED`

Raised in `server/code/app/repo_admin.py`.

#### `REPO_QUOTA`

Raised in `server/code/app/repo_admin.py`.

#### `RESERVED_BLOB_KEY`

Raised in `server/memory/app/context_offload.py`.

#### `RESOURCE_NOT_FOUND`

HTTP 404, raised as `server.core.errors.NotFoundError`. What to do: [common errors](#common-errors-and-what-to-do).

#### `RESTRICTED_NOT_OPEN`

Raised in `server/code/app/repo_admin.py`.

#### `ROLE_CEILING`

Raised in `server/identity/app/grants.py`, `server/identity/app/namespace_membership.py`.

#### `SANDBOX_UNAVAILABLE`

Raised in `server/code/app/repo_admin.py`.

#### `SCHEMA_ACTIVE`

Raised in `server/memory/app/records.py`.

#### `SCHEMA_NOT_FOUND`

Raised in `server/memory/app/records.py`.

#### `SCHEMA_PENDING`

HTTP 503, raised as `server.core.errors.SchemaPendingError`. 503 — the statement names a table that a migration this build carries but the service has not applied would create. What to do: [common errors](#common-errors-and-what-to-do).

#### `SCHEMA_REQUIRES_KEY`

Raised in `server/memory/app/extract/handler.py`.

#### `SECTION_NOT_FOUND`

Raised in `server/memory/app/profile.py`.

#### `SECTION_NOT_STRING`

Raised in `server/memory/app/profile.py`.

#### `SELF_SERVICE_OFF`

Raised in `server/code/app/repo_admin.py`, `server/tracker/app/sources.py`.

#### `SELF_SIGNUP_DISABLED`

Raised in `server/auth/self_signup.py`.

#### `SELF_SIGNUP_SSO`

Raised in `server/auth/self_signup.py`.

#### `SHARED_CONFLICT`

Raised in `server/memory/app/promote.py`.

#### `SIGNUP_IN_PROGRESS`

Raised in `server/auth/self_signup.py`.

#### `SIGNUP_ORG_FIXED`

Raised in `server/auth/self_signup.py`.

#### `SIGNUP_UNAVAILABLE`

Raised in `server/auth/self_signup.py`.

#### `SOURCE_DELETE_PENDING`

Raised in `server/knowledge/app/sources.py`.

#### `SOURCE_DOCUMENTS_KEPT`

Raised in `server/knowledge/app/sources.py`.

#### `SOURCE_EXISTS`

Raised in `server/knowledge/app/sources.py`, `server/tracker/app/sources.py`.

#### `SOURCE_FROM_CONFIG`

Raised in `server/tracker/app/sources.py`.

#### `SOURCE_NOT_FOUND`

Raised in `server/knowledge/app/sources.py`.

#### `SOURCE_QUOTA`

Raised in `server/knowledge/app/sources.py`, `server/tracker/app/sources.py`.

#### `SOURCE_REPLACED`

HTTP 409, raised as `server.core.errors.SourceReplacedError`. 409 — the document source was removed and added again as another source during the call (a rotate's probe, a sync request); act on the current one.

#### `SOURCE_TAGS_LOCKED`

Raised in `server/knowledge/app/writes.py`.

#### `SOURCE_UNREACHABLE`

Raised in `server/knowledge/app/sources.py`, `server/tracker/app/sources.py`.

#### `SUBSTRING_NOT_FOUND`

Raised in `server/memory/app/profile.py`, `server/memory/app/working_memory.py`.

#### `TASK_NOT_BLOCKABLE`

Raised in `server/tasks/app/transitions.py`.

#### `TASK_NOT_DEFERRABLE`

Raised in `server/tasks/app/transitions.py`.

#### `TEAM_RULE_NOT_YOURS`

Raised in `server/code/app/member_writes.py`.

#### `TIMELINE_REQUIRES_FILTER`

Raised in `server/memory/app/curation.py`.

#### `TOKEN_NAME_TAKEN`

Raised in `server/identity/app/me_service_accounts.py`.

#### `UNKNOWN_ARGUMENT`

Raised in `server/transport/arg_check.py`. What to do: [common errors](#common-errors-and-what-to-do).

#### `UNKNOWN_FIELD`

Raised in `server/memory/domain/record_values.py`.

#### `VALIDATION`

Raised in `server/memory/app/apply.py`, `server/memory/app/lock.py`, `server/memory/app/verify.py`.

#### `VALIDATION_FAILED`

HTTP 422, raised as `server.core.errors.ValidationError`. What to do: [common errors](#common-errors-and-what-to-do).

#### `VIEWS_NOT_READABLE`

Raised in `server/tracker/app/sources.py`.

#### `WORKING_MEMORY_BLOCK_NOT_FOUND`

Raised in `server/memory/app/working_memory.py`.

#### `WORKING_MEMORY_LIMIT_EXCEEDED`

Raised in `server/memory/app/working_memory.py`.

<!-- prettier-ignore-end -->
<!-- END GENERATED: errors -->

## Related

- [HTTP API](./http-api.md): gates, headers, idempotency and rate limits.
- [MCP](./mcp.md#errors): how refusals reach a model.
- [Unknown and removed fields](./http-api.md#unknown-and-removed-fields), including `UNKNOWN_ARGUMENT`.
- [Troubleshooting](../operations/troubleshooting.md): operator-side causes of 5xx.
