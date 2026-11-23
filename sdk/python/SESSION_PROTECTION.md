# Protected source clients

Bind an application-owned `SessionProtection` to the existing `Agent` or `Transport`
before reading a protected source. The transport validates that capsule and its
response observer before declaring the server's existing request protection
protocol. An unbound ordinary client sends no declaration. Caller-supplied
declarations, duplicate declaration headers and protected requests to another
origin refuse before dispatch.

```python
from loom.session_protection import SessionProtection
from loom.transport import Transport

family = SessionProtection.open(saved_pointer)
transport = Transport(
    "https://loom.example", api_key=token,
    session_protection=family,
)
```

Use `SessionProtection.create()` only for an explicitly new independent family.
Save its content-free `pointer()` in trusted application control state. Restores
reopen that pointer; missing or incompatible state refuses. Share the capsule
across clients, tokens, servers and sessions that contribute to the same logical
conversation. Independent conversations use independent capsules.

CLI and framework owners can pass `session_protection=family` to `Transport`
without changing operation bodies. Wrap every automatic capture callback in
`with family.automatic():`, including writes to another namespace. Marked replies
protect the family before any explicit response is returned. Later automatic
reads and writes raise `AutomaticCaptureSuppressed`. Explicit operations remain
available and still observe their responses. Retain the contributing family for
buffered or deferred work; opening another client must not reset it.

JSON envelopes are observed before status handling and typed conversion. SSE
observers retain opening families, inspect bounded complete frames before output,
and protect the family on interruption or early close. HTTP errors retain their
envelopes and headers. Invalid JSON and unsupported protected response types
withhold private body diagnostics. No credential is sent to an upstream MCP
server; these clients talk to the configured Loom API.

The command owner must bind this interface in the CLI. Dashboard wiring, other
maintained clients, durable browser storage and hosted source qualification have
separate integration requirements. These facilities do not activate mirrors.

`Transport.download(path, params=...)` returns bounded opaque GET bytes. It marks
all opening families before reading or returning the body, including JSON exports
that have no operation-envelope contract. The request makes no retry. The caller
can lower `max_bytes` from its 64 MiB ceiling and `timeout_s` from 60 seconds;
response bookkeeping also stops at 4096 chunks. The owned HTTP response closes on
success, failure or cancellation. Downloads request identity encoding and refuse
a compressed content-encoding response. Ordinary JSON operations keep their
existing response observer. Server authorization still decides which bytes may
be read, and an unbound public download makes no private-result capability claim.
