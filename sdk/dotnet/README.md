# Loom .NET client

A minimal client for Loom's memory ops: `memory.set`, `memory.get`, `memory.search` and
`memory.delete`. It is the first step towards a full .NET SDK; the Python and TypeScript SDKs
cover every op.

Requires .NET 8. Dependencies: `HttpClient` and `System.Text.Json`, both part of .NET.

## Example

Against a single-user install on this machine (`http://localhost:7777`, `dev/local`, no token):

```csharp
using ClickHouse.Loom;

using var loom = new LoomClient(new LoomClientOptions { BaseUrl = "http://localhost:7777", Org = "dev", Namespace = "local", Agent = "dotnet-demo" });
await loom.SetAsync("seat_pref", "Prefers an aisle seat on long flights.");
Console.WriteLine((await loom.GetAsync("seat_pref")).Value);
foreach (var hit in (await loom.SearchAsync("which seat should I book?", 5)).Hits) Console.WriteLine(hit.MemoryKey);
await loom.DeleteAsync("seat_pref");
```

For a team server, set `Token` and your org and namespace, or call
`LoomClient.FromEnvironment(org, ns, agent)` to read `LOOM_BASE_URL` and `LOOM_API_KEY`. A
`BaseUrl` or `Token` left unset falls back to its variable.

A key that holds nothing is not an error: `GetAsync` returns a result whose `Found` is false.

Each method takes an optional dictionary of the op's other input fields (`tags`, `memory_type`,
`user_id`, `session_id`, …), named as
[the operations reference](../../docs/reference/operations/memory.md) names them, and a
`CancellationToken`. Each result keeps the whole response as `Raw`.

## Errors

A non-2xx answer throws `LoomException`. Match on `Code` (`NOT_FOUND`, `RATE_LIMITED`, …), not
on the message. `Category` says whose move it is (`input`, `auth`, `capacity`, `dependency`,
`configuration`, `internal`) and `Retryable` whether resending can succeed later, after
`RetryAfterSeconds` when set. When the body names neither, both follow the HTTP status, as
[the errors reference](../../docs/reference/errors.md) describes. A call that got no answer has
`Status` 0 and is a retryable `dependency` error: code `UNREACHABLE` when the server could not be
reached, `TIMEOUT` when the call ran past `Timeout` (30 s by default). Cancelling through your own
`CancellationToken` throws `OperationCanceledException`, as elsewhere in .NET.

```csharp
try
{
    await loom.GetAsync("seat_pref");
}
catch (LoomException e) when (e.Code == "RATE_LIMITED")
{
    // wait e.RetryAfterSeconds
}
```

## Redirects

The `HttpClient` the client creates does not follow redirects: a 3xx answer throws a
`LoomException`, so a memory write is never sent on to another address. An `HttpClient` you pass
to the constructor keeps its own settings. .NET's default handler follows a 307 or 308 and sends
the body again; set `AllowAutoRedirect = false` on its handler for the same behavior.

## Tests

The tests use a stubbed `HttpMessageHandler` and local sockets; they need no running Loom.

```bash
cd sdk/dotnet
dotnet test
```
