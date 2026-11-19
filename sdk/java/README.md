# Loom Java client

A minimal client for Loom's memory ops: `memory.set`, `memory.get`, `memory.search` and
`memory.delete`. It is the first step towards a full Java SDK
([#53](https://github.com/ClickHouse/loom/issues/53)); the Python and TypeScript SDKs
cover every op.

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

Each method has an overload taking a `Map` of the op's other input fields (`tags`,
`memory_type`, `user_id`, `session_id`, …), named as
[the operations reference](../../docs/reference/operations/memory.md) names them. Each
result keeps the whole response as `raw()`.

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
cd sdk/java
mvn test
```
