# Session protection

Bind an application-owned `SessionProtection` to every client that reads protected sources. The handle uses a private POSIX directory containing an identity, a monotonic protection bit and pending request markers. It stores no response bodies, credentials or source identifiers. The application owns the directory and stores its pointer in the trusted conversation record.

```ruby
require "clickhouse/loom"

family = Loom::SessionProtection.create(conversation_state_directory)
conversation.save_protection_pointer(family.pointer)
client = Loom::Client.new(session_protection: family)
answer = client.call("knowledge.source.mcp_read", read_arguments)
```

On resume, use `SessionProtection.open(conversation.protection_pointer)`. Missing, invalid or inaccessible state raises `SessionProtectionUnavailable`. Never replace a failed restore with `create`. Reopening a directory with a pending request latches protection, including after a process interruption. Share the same handle among active clients rather than reopening it per request. Back up and restore the pointer and its directory together; the application must prevent rollback or replacement of trusted storage.

Retain a capture before scheduling automatic work. Its callback guards every operation inside its scope, including calls through another client, identity or namespace. An automatic operation refuses once any contributor is protected or has a response still pending. Explicit user operations remain available and still observe metadata.

```ruby
capture = family.capture
queue.enqueue do
  capture.call do
    another_client.call("memory.set", automatic_write_arguments)
  end
end
# For one operation:
# another_client.call_automatic(capture, "memory.set", automatic_write_arguments)
```

`capture.contribute(other_family)` returns a capture retaining both families. Creating a capture within another capture's callback retains its contributors too. Ruby's scope follows the current fiber; pass the retained capture into other fibers, threads and queued callbacks and enter `capture.call` there. Application code must use this API for every automatic write, including direct calls to another namespace. A new client alone carries no conversation identity or protection authority.

The actual `Client#post` boundary declares `x-loom-session-protection: v1` only after durable admission and with the installed observer. There is no public header override. Every retry passes the final automatic guard. Response JSON is bounded to 4 MiB and observed at every HTTP status before decoding or constructing API errors. Mirrored metadata latches every opening family. Invalid envelopes, oversized responses and interrupted body reads refuse and latch protection. An interrupted request retains a durable pending marker if cleanup cannot acknowledge completion.

Protected streaming is unavailable. Event streams and other unsupported media refuse without returning response bytes. Ordinary unbound requests preserve their existing behavior and send no declaration. The application must qualify its actual source operations and deferred work before activation; generated operation availability is independent of transport readiness.
