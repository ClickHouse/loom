# Loom Go client

A Go client for [ClickHouse Loom](https://clickhouse.github.io/loom/) with typed
`memory.set`, `memory.get`, `memory.search` and `memory.delete` methods and generic
operation calls. It depends on the standard library only (`net/http`), and every
call takes a `context.Context`.

```bash
go get github.com/ClickHouse/loom/sdk/go
```

Generated types for the full operation surface are in the
[Python](https://github.com/ClickHouse/loom/tree/main/sdk/python) and
[TypeScript](https://github.com/ClickHouse/loom/tree/main/sdk/typescript) SDKs.

## Example

Against a
[single-user install](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md)
on this machine (`http://localhost:7777`, org `dev`, namespace `local`, no token):

```go
package main

import (
	"context"
	"fmt"
	"log"

	loom "github.com/ClickHouse/loom/sdk/go"
)

func main() {
	ctx := context.Background()
	c := loom.New(loom.Config{BaseURL: "http://localhost:7777", Org: "dev", Namespace: "local", Agent: "assistant"})
	if _, err := c.Set(ctx, "deploy_window", "Deploys run Tue and Thu only.", nil); err != nil {
		log.Fatal(err)
	}
	m, err := c.Get(ctx, "deploy_window")
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(m.Found, string(m.Value)) // true "Deploys run Tue and Thu only."
}
```

## Configuration

`loom.New(loom.Config{...})` binds the server and the identity once; every call sends the
org, namespace and agent in its body.

| Field                       | Default                                                         |
| --------------------------- | --------------------------------------------------------------- |
| `BaseURL`                   | `LOOM_BASE_URL`, else `http://localhost:7777`                   |
| `APIKey`                    | `LOOM_API_KEY`; with neither, no `Authorization` header         |
| `Org`, `Namespace`, `Agent` | Left out of the body when blank, so the server's defaults apply |
| `HTTPClient`                | `http.DefaultClient`                                            |
| `SessionProtection`         | Unbound; ordinary requests send no protection declaration       |

The client sends one request per call and never retries. Redirects are refused by
default; ordinary clients can supply a redirect policy in `HTTPClient`. Protected
clients always refuse redirects and hidden transport retries.

## Generic source and connection operations

`Call(ctx, operation, arguments, &output)` uses the same HTTP boundary as the typed
methods. It copies arguments and fills absent `org`, `namespace` and `agent` from
the client. Output may be a typed value, a map or `json.RawMessage`. Use the current
server schema for each operation; Go has no generated source declarations.

| Operation | Arguments in addition to identity |
| --- | --- |
| `knowledge.connection.mcp_browse` | `slot`, `mode` (`resources` or `search`); optional `query`, `limit` |
| `knowledge.connection.mcp_disconnect` | `slot` |
| `knowledge.source.mcp_list` | Optional `cursor`, `limit`, `include_retired` |
| `knowledge.source.mcp_read` | `name`, `resource_id` |
| `knowledge.source.mcp_pause` | `name` |
| `knowledge.source.mcp_resume` | `name` |
| `knowledge.source.mcp_refresh` | `name` |

Protected source reads require an application-owned durable family through
`Config.SessionProtection`. Bound calls are automatic by default. After protected
content or while a response is pending, automatic calls refuse. There are no
generated read-only exemptions in Go, so this also refuses automatic reads.
`CallHuman` records explicit user intent for one invocation and still observes
the response. See [session protection](SESSION_PROTECTION.md) for storage,
restoration and deferred-task ownership.

## Errors

A non-2xx answer is a `*loom.Error` carrying the server's `code`, its `category`
(`input`, `auth`, `capacity`, `dependency`, `configuration` or `internal`), whether
resending can succeed (`Retryable`), the wait it asked for (`RetryAfter`) and the HTTP
status. Match on `Code`, never on the message:

```go
var e *loom.Error
if errors.As(err, &e) && e.Code == "RATE_LIMITED" {
	time.Sleep(e.RetryAfter)
}
```

A missing key is not an error: `Get` answers with `Found` false. The codes and what to
do about each are in the
[errors reference](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).

## Tests

```bash
cd sdk/go && go test ./...
```

The tests use owned loopback HTTP servers and need no running Loom. Protection
tests include response errors, chunking, cancellation, restoration and a child
process. Native compilation, formatting and these cases require hosted execution;
this change has no native pass receipt or real-service qualification.
