# Loom Java client

A minimal client for Loom's memory ops (`memory.set`, `memory.get`, `memory.search` and
`memory.delete`) and the conversation ops a chat memory needs (`conversation.append`,
`get`, `create`, `delete`, `threads` and `set_logging`). It is a step towards a full Java
SDK ([#53](https://github.com/ClickHouse/loom/issues/53)); the Python and TypeScript SDKs
cover every op. Java also exposes generic wire calls and map-based MCP source
controls. See [protected source sessions](SESSION_PROTECTION.md) for the required
application-owned conversation authority, explicit human calls, retained callback
captures, Linux storage limits and hosted qualification status.

Two chat memory modules build on it, each its own Maven project so the client keeps no
framework dependency:

- [`langchain4j/`](langchain4j/README.md): a LangChain4j `ChatMemoryStore`.
- [`spring-ai/`](spring-ai/README.md): a Spring AI `ChatMemoryRepository`.

Requires Java 17+. Dependencies: `java.net.http.HttpClient` and Jackson.

## Example

Against a single-user install on this machine (`http://localhost:7777`, `dev/local`,
no token):

```java
LoomClient loom = LoomClient.builder().baseUrl("http://localhost:7777")
    .org("dev").namespace("local").agent("java-demo").build();
loom.set("seat_pref", "Prefers an aisle seat on long flights.");
System.out.println(loom.get("seat_pref").value());
loom.search("which seat should I book?", 5).hits().forEach(h -> System.out.println(h.memoryKey()));
loom.delete("seat_pref");
```

For a team server, pass `.token(...)` and your org and namespace, or build with
`LoomClient.fromEnv(org, namespace, agent)` to read `LOOM_BASE_URL` and `LOOM_API_KEY`.

A key that holds nothing is not an error: `get` returns a result whose `found()` is false.

Each method has an overload taking a `Map` of the op's other input fields (`tags`,
`memory_type`, `user_id`, `session_id`, …), named as the operations reference
([memory](../../docs/reference/operations/memory.md),
[conversation](../../docs/reference/operations/conversation.md)) names them. Each
result keeps the whole response as `raw()`.

## Conversations

```java
loom.conversationSetLogging(true);                  // admin; off by default
loom.conversationAppend("order-1234", "user", "Where is my order?");
ConversationGetResult t = loom.conversationGet("order-1234", 100, 0); // a page of messages
t.messages().forEach(m -> System.out.println(m.role() + ": " + m.content()));
loom.conversationDelete("order-1234");             // admin; sends confirm_thread_id
```

An append the server did not keep (recording is off, or the turn was deleted meanwhile) is
not an error: `stored()` is false and `reason()` says why. `requireStored()` turns that into
a `LoomException` with `code()` `LOGGING_DISABLED` or `DELETED`.

`ConversationWindow` keeps a chat memory's message list in one thread for the modules
above: the log stays append-only and a window pointer in the thread's metadata says which
turns the memory holds.

## Errors

A non-2xx answer throws `LoomException`. Match on `code()` (`NOT_FOUND`,
`RATE_LIMITED`, …), not on the message. `category()` says whose move it is (`input`,
`auth`, `capacity`, `dependency`, `configuration`, `internal`) and `retryable()` whether
resending can succeed later, after `retryAfterSeconds()` when set. When the body names
neither, both follow the HTTP status, as [the errors reference](../../docs/reference/errors.md)
describes. A call that got no answer (connection refused, timeout) has `status()` 0 and
is a retryable `dependency` error.

```java
try {
  loom.get("seat_pref");
} catch (LoomException e) {
  if (e.code().equals("RATE_LIMITED")) { /* wait e.retryAfterSeconds() */ }
}
```

## Tests

The tests run against a local fake HTTP server; they need no running Loom.

```bash
mvn -f sdk/java verify
```

`mvn -f sdk/java install` also installs a test jar holding that fake server, which the
chat memory modules' tests use.
