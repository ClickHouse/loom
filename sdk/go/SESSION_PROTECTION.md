# Protected source sessions

The application owns one durable family per conversation and binds it through
`Config.SessionProtection`. The SDK sends exactly one
`x-loom-session-protection: v1` declaration only after durable admission, with its
raw response observer installed. An unbound ordinary client sends no declaration.

## Provision and restore

Provision a new family once under an application-owned private directory:

```go
family, err := loom.CreateSessionProtection(conversationStorageDirectory)
if err != nil {
	return err
}
pointer := family.Pointer()
// Persist every pointer field in the application's trusted conversation record.
client := loom.New(loom.Config{
	BaseURL: serverURL, APIKey: token, Org: org, Namespace: namespace,
	SessionProtection: family,
})
```

Restoring a conversation uses its existing trusted pointer:

```go
family, err := loom.OpenSessionProtection(pointer)
if err != nil {
	return err
}
```

Do not provision a replacement when restore fails. Storage is independent of user
configuration, client identity, origin and credentials. Changing any of those or
copying a client must retain the same family. A new pointer belongs to a new
conversation; it cannot clear protected history in an existing one.

The built-in store uses a new private directory, a random identity, stable file
identities, an in-place monotonic state byte, a cross-process lock and durable
pending records. It stores no source bytes, credentials or messages. Missing,
replaced, corrupt, symlinked or inaccessible authority refuses, including explicit
human calls. Interrupted observations retain pending proof; reopening pending
proof marks the family protected. There is no SDK reset or pending-clear API.
Store scans cap at 10,000 entries; overflow refuses further dispatch.

The host and trusted application storage must prevent rollback of the pointer
and store together, unauthorized writes and deletion. File identities detect
replacement; they are not a secure hardware counter or protection against a
compromised host. Keep the store and its parent private to the application user,
on a local filesystem supporting durable file/directory sync and POSIX locks.
Lock waits are bounded; blocking kernel storage operations require a healthy
host filesystem. Network filesystems and cross-host authority are unsupported.

The store is selected on Linux, macOS, FreeBSD, NetBSD, OpenBSD and DragonFly build
targets. Other targets refuse protected provisioning and restore; ordinary
unbound clients remain available. Platform compilation and behavior require
hosted qualification.

## Calls and retained work

Bound typed methods and `Call` are automatic. Every invocation rechecks the
durable latch and pending responses immediately before HTTP dispatch. Protected
content and incomplete concurrent responses refuse new automatic operations,
including unknown operations and calls addressed to another namespace. Go has no
generated read-only operation metadata, so it conservatively applies this guard
to reads too.

Use the actual source-read arguments:

```go
capture := loom.CaptureSession(ctx, family)
readContext := capture.Context(ctx)
var result map[string]any
err := client.Call(readContext, "knowledge.source.mcp_read", map[string]any{
	"name": sourceName,
	"resource_id": resourceID,
}, &result)
```

Retain `capture` with callbacks and queued tasks, then call
`capture.Context(taskContext)` when they run. The context can cross goroutines and
HTTP clients. `CaptureSession` and `Context` union existing contributors; dispatch
also includes the client's currently bound family. A clean retained family cannot
hide a protected bound family. Responses observe every contributing family before
returning. At most 64 contributors are accepted. Invalid or empty captures refuse.
The application must retain all contributors when scheduling work; the SDK does
not track arbitrary tasks or infer their provenance.

After obtaining explicit intent from the application user for a specific
operation, invoke `CallHuman` once:

```go
err := client.CallHuman(readContext, "memory.set", map[string]any{
	"memory_key": chosenKey,
	"value": userSelectedValue,
}, &result)
```

This authorizes only that invocation. It validates proof, observes the response
and keeps the latch. It does not place authority in a context, client facade,
callback or task. Every later explicit operation requires new human intent;
automatic work keeps using guarded `Call` or the typed methods.

## HTTP and storage limits

Protected calls use the standard `http.Transport`; custom `RoundTripper` wrappers
refuse because they can conceal resends or publish unobserved responses. Configure
the transport before use and keep it immutable. Application dial/proxy callbacks
must honor context cancellation. Redirects and internal body replays refuse.
The SDK has no retry, pacing, streaming or download API. Caller retry loops must
make a new guarded invocation after every wait.

The observer examines JSON at every HTTP status before API errors, typed
conversion or body publication. It follows `mirrored`, `result`,
`structuredContent`, `items`, JSON text content blocks and `error.data`. Invalid
metadata, duplicate keys, malformed JSON, truncated bodies and unsupported media
retain protection and refuse publication. SSE and binary responses refuse before
any bytes reach the caller. Empty 204/304 responses acknowledge observation; the
typed API can still report an invalid empty JSON result.

Requests and response bodies are capped at 4 MiB. Request validation bounds
encoding work to 10,000 nodes and 64 nesting levels and conservatively budgets
escaped strings. Protected requests reject custom JSON/text marshalers; pass
plain JSON data. Response parsing shares a 10,000-node budget across nested text
documents and limits each JSON nesting depth to 64. HTTP work has a 30-second
context deadline, shortened by caller/client deadlines. Each lock wait has a
two-second deadline. Cancellation closes the owned response and completes bounded
proof cleanup before returning; failed cleanup leaves pending records.

The authored native tests use owned loopback HTTP fixtures. Compilation,
formatting, tests and platform behavior are unrun in this authoring environment.
Actual service authorization, source ingestion and vendor compatibility require
separate hosted qualification. No compatibility claim follows from these fixtures.
