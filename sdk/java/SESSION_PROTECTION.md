# Protected source sessions

`LoomClient` supports generic outbound MCP source controls through its normal HTTP boundary. `sourceMcpRead`, `sourceMcpList`, `sourceMcpPause` and `sourceMcpRefresh` accept the operation reference's input maps and default only `org` and `namespace`. `connectionMcpBrowse` defaults only `org`. Other controls use `call(operation, completeWireInput)`, which adds no identity fields. Results are Jackson `JsonNode` values. These methods do not infer upstream MCP methods or claim compatibility with every MCP server.

Bind one `SessionProtection` to the application's logical conversation. Store its `Pointer` independently from mutable client settings, credentials and namespace selection. Reopen that exact pointer on resume; failure must stop the conversation's automatic dispatch. `create` is only for a new conversation and refuses an existing directory. Never recover a missing pointer by creating another family.

```java
SessionProtection family = SessionProtection.create(privateParent.resolve("conversation"));
// Save every Pointer field in the application's trusted conversation record.
SessionProtection.Pointer pointer = family.pointer();
LoomClient loom = LoomClient.builder().baseUrl(serverUrl).token(token)
    .org(org).namespace(namespace).agent(agent).sessionProtection(family).build();
JsonNode result = loom.sourceMcpRead(Map.of("name", sourceName, "resource_id", resourceId));
// A later process uses SessionProtection.open(pointer), never create(...).
```

A protected response latches the family before returning bytes or typed results. Observation includes successful envelopes, `error.data` at any HTTP status, nested results, items, structured content and JSON text blocks. Invalid JSON, invalid protection metadata, interrupted responses, unsupported media or lost proof fail closed and retain protection. Only the installed durable guard and observer add exactly one `x-loom-session-protection: v1`. Ordinary unbound calls keep their existing behavior and send no declaration. There is no caller header override API.

Calls on a bound client are automatic by default. No Java read-only operation catalog is generated yet, so every operation, including reads and unknown operations, is conservatively subject to the mutation guard. Once protected, `callHuman(operation, completeWireInput)` authorizes one explicit invocation. It still observes the response and does not clear the latch. This method must be chosen by the application for actual human intent, never exposed as a globally enabled automatic facade.

```java
loom.callHuman("memory.set", Map.of("org", org, "namespace", destination,
    "agent", agent, "memory_key", "chosen-key", "value", humanChosenValue));
```

Retain contributors before queuing work or invoking another client. `Capture.call` installs a thread-local scope for its duration and restores the previous scope on exceptions. Nested scopes union their contributors. Thread pools and newly spawned threads receive no implicit inherited context; pass the capture explicitly. Every automatic call unions its retained contributors with the current client's bound family. A clean retained family cannot bypass a protected bound family.

```java
SessionProtection.Capture capture = family.capture();
// Persist the contributing family pointers with durable queued work; rebuild this capture on resume.
executor.submit(() -> anotherClient.callAutomatic(capture, "memory.set", completeWireInput));
// Synchronous callbacks can use capture.call(() -> anotherClient.set(key, value)).
```

Changing client, namespace, token or origin creates no protection authority. Rebuilt clients must bind the same family; callbacks that use an unbound client must retain the capture. A callback queued during an explicit human call receives no lasting human authority. Java has no client clone API, SDK retries or pacing policy. Applications that retry must use the same bound client or retained capture on every attempt. Protected clients reject redirect-following HTTP clients and authenticators that could resend a request outside the admission boundary. Custom HTTP clients and their configuration are trusted application code.

## Durable storage limits

The built-in store supports Linux local POSIX filesystems with stable file identities, advisory file locking, private file permissions, file fsync and directory fsync. Windows, network filesystems and stores without these properties are unsupported; this SDK supplies no alternative storage adapter. Unsupported storage refuses readiness. Native behavior remains unqualified until the hosted tests pass.

The application owns a private, trusted parent directory and all its ancestors. Each conversation directory is created with mode 0700 and its authority file with mode 0600. The independently stored pointer binds directory and authority file identities and a random family identity. Missing, corrupt or replaced authority refuses even explicit dispatch. A checksummed one-way protected bit and durable pending tokens in that same authority file survive process interruption. Pending proof prevents concurrent automatic requests and latches protection on resume. An explicit human invocation may proceed with pending proof, but cannot remove an abandoned marker or restore automatic authority.

Trusted storage and the host must prevent rollback of both the authority and its pointer. This store cannot detect a coordinated filesystem snapshot rollback, a privileged host rewriting both records, or an application intentionally discarding its conversation identity. It is not an isolation boundary against hostile application code. It stores no credentials, response bodies or private document content. No cleanup API resets a family; retention and deletion belong to the application owner.

Protected requests and responses are limited to 4 MiB, envelope work to 10,000 nodes, JSON text parsing to 4 MiB, contributors to 32 pending tokens to 1,024 and the authority file to 64 KiB. The conversation directory
accepts only its authority file; any extra entry refuses readiness. Protected responses use a bounded subscriber and an overall request deadline. Cancellation joins the owned subscriber callback boundary and cancels the HTTP future and body subscription before returning; incomplete responses retain pending proof. The owned-loopback cancellation test must confirm peer connection closure on the hosted JDK. Streaming and download publication are unsupported: media other than JSON is refused before bytes escape. The SDK creates no background retry or cleanup workers.

## Qualification

`SessionProtectionHttpTest` contains actual owned-loopback HTTP tests for success, HTTP errors, clean and ordinary traffic, retained contributors, cross-client/namespace/token/origin refusal, explicit human writes, lost and replaced proof, pending concurrency, malformed metadata, bounded bodies, stream refusal and cancellation. These tests need no service, database or provider. They are authored and UNRUN locally. Native compilation, formatting and tests belong to the hosted environment. No real Loom service, upstream MCP server, authorization or storage qualification follows from authoring these cases.
