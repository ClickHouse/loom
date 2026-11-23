# Examples

Run these examples from a Loom checkout with Cargo against a running Loom server.
The three basic examples below write, read and delete their own memory keys and knowledge URIs. Installation
and client configuration are covered in the [SDK README](../README.md#install).

| Example                        | Client   | Demonstrates                                                                           |
| ------------------------------ | -------- | -------------------------------------------------------------------------------------- |
| [memory.rs](memory.rs)         | Async    | Memory save, key lookup, lexical search, deletion and absence check                    |
| [operations.rs](operations.rs) | Async    | Capabilities, memory and knowledge calls, request options, metadata and request `send` |
| [blocking.rs](blocking.rs)     | Blocking | The same memory, knowledge and context flow from a synchronous `main`                  |

## Configure and run

Use the current stable Rust toolchain and an existing namespace. For a local
server, follow the [quickstart](../../../docs/getting-started/quickstart.md).
For a team server, use its address, a writer token and an authorized org and
namespace. Use a fresh namespace for the examples: memory deduplication can merge
a write with an existing row, which makes the example stop.

All commands below run from the repository root:

```bash
export LOOM_BASE_URL=http://localhost:7777
export LOOM_API_KEY=
export LOOM_ORG=dev
export LOOM_NAMESPACE=local

cargo run --manifest-path sdk/rust/Cargo.toml --example memory
cargo run --manifest-path sdk/rust/Cargo.toml --example operations
cargo run --manifest-path sdk/rust/Cargo.toml --features blocking --example blocking
```

The empty token is for a local server that permits unauthenticated requests.
For a team deployment, set `LOOM_API_KEY` to your token before running Cargo.

| Variable            | Default                 | Purpose                                                            |
| ------------------- | ----------------------- | ------------------------------------------------------------------ |
| `LOOM_BASE_URL`     | `http://localhost:7777` | Server address, read by the SDK                                    |
| `LOOM_API_KEY`      | Unset                   | Bearer token; empty means no Authorization header                  |
| `LOOM_ORG`          | `dev`                   | Organization, read by each example                                 |
| `LOOM_NAMESPACE`    | `local`                 | Namespace, read by each example                                    |
| `LOOM_CHECK_HEALTH` | Unset                   | `1` adds an `ops.health` call in `operations.rs` and `blocking.rs` |

The examples bind agent `rust-example`. The operations and blocking examples also
bind user `example-user` and session `example-session`. Their memory searches
explicitly select that agent and namespace; knowledge searches select the bound
namespace.

Writes use the server's configured embedding provider. Indexing may still be
pending when a search runs. `operations.rs` and `blocking.rs` print search results
without requiring an immediate hit; `memory.rs` requires its saved key to appear.

## memory.rs

The [memory.rs](memory.rs) example saves a value under an agent identity, reads
it by key, finds it with lexical search and deletes it.

`Client::builder(org, namespace, "rust-example")` binds org, namespace and agent
for the example's calls. Each invocation uses a unique memory key.

The example runs these steps:

1. `Client::set` — saves the memory and returns its ID and version.
2. `Client::get` — checks that the saved value is returned.
3. `Client::search` — finds the memory with lexical search in the bound namespace
   and agent.
4. `Client::delete` — tombstones the memory. Cleanup is attempted even when a read
   or search fails.
5. `Client::get` — confirms `found=false` after deletion.

Output has this form; IDs, keys, versions and search metadata vary:

```text
set: memory_id=<id> version=<version>
get: found=true value="Rust SDK memory demo: <key>"
search: 1 hits abstain=false risk=low pending_extraction=0 degraded=[]
delete: tombstoned=true
get after delete: found=false
```

## operations.rs

The [operations.rs](operations.rs) example mixes the memory facade with generated
knowledge methods and a generated request's `send` method.

1. `Client::capabilities` — checks that the server advertises the operations
   used by the example and prints its schema version.
2. `Client::health` — runs only when `LOOM_CHECK_HEALTH=1`.
3. `Client::set_with_options` — writes a unique memory with a 300 second TTL
   and request ID `example-save`. Prints the version, request ID and attempt count.
4. `Client::get` and `Client::search` — verify the saved value and report lexical
   recall metadata.
5. `Client::knowledge_add` — creates a document at a unique URI with a title and tags.
6. `Client::knowledge_get` and `Client::knowledge_search` — verify the document
   content and report lexical document search results.
7. `ContextPackInput::send` — assembles context for the key, includes knowledge,
   and sets a token budget of 2000. Prints rendered context and call metadata.
8. `Client::delete` and `Client::knowledge_delete` — attempt cleanup before
   returning the flow's result.

The capabilities check includes `ops.health` even when the health call is disabled.
Calling health requires an org-admin token. Enable it only with a token that also
permits the example's writes:

```bash
LOOM_CHECK_HEALTH=1 cargo run --manifest-path sdk/rust/Cargo.toml --example operations
```

Output has this form; schema versions, IDs, search results and context vary:

```text
schema: <schema-version>
set: version=<version> request=<request-id> attempts=<count>
memory search: hits=<count> abstain=<bool> degraded=<list>
knowledge add: doc=<id> chunks=<count>
knowledge search: hits=<count> skipped=<list>
context: truncated=<bool> request=<request-id>
<rendered context>
```

A health-enabled run also prints `health: <overall>` after the schema version.
Successful memory and knowledge lookups and cleanup do not print separate lines.

## blocking.rs

The [blocking.rs](blocking.rs) example calls `Client::probe` and prints the build,
schema, missing capabilities and count of operations unknown to the SDK. It
continues when capabilities are missing, then follows the memory, knowledge and
context sequence in `operations.rs`. It imports `blocking::Client`, uses a synchronous
`main`, and calls `context_pack_with_options` for context assembly.

The example requires the `blocking` Cargo feature:

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --features blocking --example blocking
```

With the required token, its optional health call is:

```bash
LOOM_CHECK_HEALTH=1 cargo run --manifest-path sdk/rust/Cargo.toml --features blocking --example blocking
```

The blocking client owns its runtime thread. Call it from an ordinary OS thread;
calls from a thread entered into Tokio return `Error::Configuration`. See the
[blocking client contract](../README.md#blocking-calls).

## Cleanup and failures

`memory.rs` attempts exact-key deletion even when its write, read or search fails.
After a successful flow and deletion, it verifies the key is absent.

`operations.rs` and `blocking.rs` attempt memory deletion after a successful write
that did not merge, and knowledge deletion whenever the knowledge write was
attempted. Both cleanups run even when a later step fails. Cleanup errors are
printed to stderr; a failed flow or cleanup returns a nonzero exit status.

A write can commit before the client receives a timeout. The operations and
blocking examples' memory TTL bounds the lifetime of that row if cleanup is not
reached. `memory.rs` does not set a TTL. Terminating any example can interrupt
cleanup. Deletion tombstones memory; it does not physically erase it.

If a server operation is missing, `operations.rs` stops at the capabilities check
before writing. `blocking.rs` prints the probe report and continues; a call to
a missing operation returns an HTTP error. If a memory write reports a merge, use a
fresh namespace. For API and transport failures, check the server address, token,
namespace grants and configured providers.

These examples demonstrate selected calls. The
[SDK README](../README.md#generated-contracts) explains method naming, constructors, nullable
fields and request `send`.

## Python scenario parity

The [Python examples](../../python/examples/README.md) contain eight standalone
scenarios. Seven have Rust counterparts, in addition to the three basic examples
above. Pipecat requires the Python integration.

| Python example               | Rust example                                                    | Scenario                                                                                                |
| ---------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `agent_memory.py`            | [agent_memory.rs](agent_memory.rs)                              | Travel preferences, semantic recall, grounded answer, cited-memory outcomes, missing key and unknown op |
| `document_search.py`         | [document_search.rs](document_search.rs)                        | Two documents, subject searches with citations, off-topic knowledge abstention                          |
| `code_lookup.py`             | [code_lookup.rs](code_lookup.rs)                                | Selected symbol, search, callers or impact lookup with bounded evidence                                 |
| `fact_document_evidence.py`  | [fact_document_evidence.rs](fact_document_evidence.rs)          | Read a fact and selected document section for operator review                                           |
| `multi_agent_handoff.py`     | [multi_agent_handoff.rs](multi_agent_handoff.rs)                | Planner and worker credentials, task lease, result memory and provenance                                |
| `support_agent_memory.py`    | [support_agent_memory.rs](support_agent_memory.rs)              | Customer isolation, fact revision, user erasure and residue checks                                      |
| `software_factory_memory.py` | [software_factory_memory.rs](software_factory_memory.rs)        | Source snapshot, derived note, review handoff, body recheck and run completion                          |
| `pipecat_text_memory.py`     | [Python Pipecat guide](../../python/examples/pipecat/README.md) | Native Pipecat frames and `LoomMemoryService`; no Rust adapter                                          |

The Rust flows call typed SDK methods with request builders and read response
fields directly. For example, memory recall uses `Client::search` with
`SearchRequest`; grounded answers use `Client::memory_ask` with `MemoryAskInput`.
Only the unknown-operation HTTP 404 demonstration uses `Client::call`.
The shared [common module](common/mod.rs) contains argument parsing, logs for
already received typed responses and cleanup helpers. Rust has no Python
`Agent.tools` dispatcher: `code_lookup.rs` selects a typed code method in
application code.

`serde_json::Value` is used for application data such as memory values and task
briefs, and for serializing code evidence before applying the output limit.
Operation names and JSON request envelopes are not passed to a network helper.

All seven accept `--help` before reading configuration or sending a request:

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --example agent_memory -- --help
```

Set `LOOM_BASE_URL` and an appropriate token as above. `agent_memory`,
`document_search` and `support_agent_memory` use `LOOM_ORG` (default `dev`).
When `LOOM_NAMESPACE` is unset, they register a unique bench namespace and retain
it as history. When it is set, it must already be registered and authorized.
Use a fresh namespace for semantic scenarios to avoid unrelated results.
The other four require explicit existing namespaces.

Semantic search uses the server's embedder. `agent_memory` also needs the
server's answer provider; writes can use other configured providers.
Running these scenarios against a provider-backed deployment can incur its
normal costs. `--help` and compilation do not call those providers.

### agent_memory.rs

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --example agent_memory
```

Prints a `Client::probe` report for the required operations and continues even
when the report lists missing capabilities. Writes three travel facts under unique keys,
recalls by meaning, asks for a grounded seat-and-meal answer and records `used`
outcomes only when it uses a nonempty answer that does not abstain, addressing
its own cited rows by their write receipt IDs. It checks a missing key and HTTP 404 for an
unknown operation. It attempts deletion of its own keys even after failure.
An abstaining answer is a valid response; this example prints it.

### document_search.rs

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --example document_search
```

Adds refund and shipping Markdown documents at unique URIs. Each subject search
must rank its expected document first. The off-topic query uses `search.query`
and must return no knowledge passages. This needs a real embedder and
`LOOM_MCP_LOOM_SEARCH_KNOWLEDGE_MIN_SIMILARITY` above unrelated-text scores;
the Python guide's local-embedder run used `0.5`. A fake n-gram embedder cannot
validate semantic behavior. Both attempted document URIs are deleted on exit.

### code_lookup.rs

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --example code_lookup -- \
  'package.function' --org acme --memory-namespace shared \
  --code-namespace source --kind symbol
```

Requires an existing code index and a reader token. `--kind` accepts `symbol`
(default), `search`, `callers` and `impact`. `--depth` applies to callers
(`1..3`, default `1`) and impact (`1..2`, default `2`). Search takes prose;
the other modes take a symbol reference or repository path with a line number.
No agent model is called. Search uses the server's embedding provider.

The code client binds `--code-namespace`; `--memory-namespace` is accepted to
match Python's CLI and is not queried by this flow. Output is capped at 12,000
UTF-16 code units. Whole bodies and rows are omitted with explicit
`client_truncation` metadata. If uncertainty metadata cannot fit, all evidence
is omitted. Ambiguity, elision and graph lower bounds still require operator
judgment. This read-only example creates nothing to clean up.

### fact_document_evidence.rs

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --example fact_document_evidence -- \
  --org acme --namespace shared --agent assistant \
  --user-id customer --session-id conversation \
  --memory-key preference --source-uri docs://policy.md \
  --section 'Refund policy' Eligibility
```

Requires existing memory and a document, plus read access. The selected agent,
user and session must match any returned fact before the section is read.
User and session default to `-`. A nonempty heading path selects a section
without children. No search, embedding or model call is made.

The CLI prints presence and selected field names, without bodies, locators or
provenance values. Typed responses supply defaults for omitted fields, so these
names describe the selection rather than claiming which wire fields were present.
It reports `relationship=caller_selected` and `support=not_evaluated`;
it does not infer that the document supports the fact. It creates no resources.

### multi_agent_handoff.rs

Set `LOOM_ORG` and `LOOM_NAMESPACE` to an existing shared namespace. Set
`LOOM_PLANNER_API_KEY` to an admin service-account token with subject
`handoff-planner`, and `LOOM_WORKER_API_KEY` to a distinct writer service-account
token with subject `handoff-worker`. The example verifies `process:` writer
actors, which require service-account credentials.

From an operator environment with `LOOM_JWT_SECRET` and verified stores:

```bash
export LOOM_PLANNER_API_KEY="$(loom mint-token --subject handoff-planner \
  --org "$LOOM_ORG" --namespaces "$LOOM_NAMESPACE" --roles admin --service-account)"
export LOOM_WORKER_API_KEY="$(loom mint-token --subject handoff-worker \
  --org "$LOOM_ORG" --namespaces "$LOOM_NAMESPACE" --roles writer --service-account)"
```

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --example multi_agent_handoff
```

Registers a uniquely named worker inbox, creates a square-number task, claims
and heartbeats its lease, writes the result and completes the task with the
memory key and ID. The planner reads by receipt ID and verifies the value,
worker agent, writing principal and asserted source class. Computation is
deterministic; memory writes use configured providers.

Cleanup cancels unfinished work only under the expected subject, owner and
unchanged fence. Memory deletion is attempted even if task cleanup fails.
The result key is deleted; inbox and task history remain.

### support_agent_memory.rs

This example requires an org-admin `LOOM_API_KEY` and a running erasure worker.

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --example support_agent_memory
```

Creates two new customer IDs and writes the same `plan` and `device` keys under
separate user identities. It checks scoped recall, revises a plan by its write
receipt ID and erases only the freshly generated second customer.
User erasure spans every namespace in the org, which is why that ID is unique.

Polls erasure status for up to 120 seconds, requiring completion and zero
residue. Checks that erased memories cannot be recalled or read and that the
other customer's values remain intact. Cleanup attempts all own customer keys,
including keys already removed by erasure.

### software_factory_memory.rs

Requires an existing public repository index, a shared memory namespace and
an admitted review inbox owned by `factory-reviewer`. Set distinct
`LOOM_IMPLEMENTATION_API_KEY` and `LOOM_REVIEW_API_KEY` service-account credentials with subjects
`factory-implementation` and `factory-reviewer`. Both need source read and memory
access, and their respective task permissions. The example verifies `process:`
writer actors, which require service-account credentials.

For the namespace arguments below, an operator with `LOOM_JWT_SECRET` and
verified stores can mint scoped tokens:

```bash
export LOOM_IMPLEMENTATION_API_KEY="$(loom mint-token --subject factory-implementation \
  --org acme --namespace-role shared=writer --namespace-role source=viewer --service-account)"
export LOOM_REVIEW_API_KEY="$(loom mint-token --subject factory-reviewer \
  --org acme --namespace-role shared=writer --namespace-role source=viewer --service-account)"
```

```bash
cargo run --manifest-path sdk/rust/Cargo.toml --example software_factory_memory -- \
  --org acme --memory-namespace shared --code-namespace source \
  --repo owner/project --commit '<full-40-character-commit>' \
  --issue-url https://github.com/owner/project/issues/1 \
  --symbol 'package.function' --repo-tag repo:project \
  --review-inbox review --lesson 'Operator claim to examine'
```

Checks repository, full commit, fetch freshness and compiler-tier commits;
reads one complete symbol body and confirms the index did not advance.
Hashes the returned body with SHA-256, records the operator claim as a derived
memory linked to its source and opens a run. The reviewer claims and heartbeats
the task, reads the note by receipt ID with provenance checks and re-reads the
source. The outcome reports whether path, symbol and returned-body hash changed.

Receipt logs exclude bodies and server errors. The outcome explicitly reports
`claim_truth=not_verified`, `code_review=not_performed`, `ci=not_run` and
`merge_ready=false`. This flow neither changes code nor performs a code review.
It completes the run, cancels its unfinished task under an unchanged fence and
deletes only its generated note. Run and task history remain.

### Scenario cleanup

Every flow attempts its applicable cleanup before returning an error. Cleanup
uses exact keys or URIs; a no-op merged write never authorizes peer deletion.
Timeouts can leave committed writes, so attempted keys are tracked before
sending. Process termination can interrupt cleanup. Memory deletion creates
tombstones; the support scenario separately demonstrates physical erasure.
Failures return a nonzero status and omit server error bodies from CLI logs.
