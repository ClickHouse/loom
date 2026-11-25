# Loom Go client

A minimal Go client for [ClickHouse Loom](https://loom.clickhouse.com/docs/)'s memory
operations: `memory.set`, `memory.get`, `memory.search` and `memory.delete`. It depends
on the standard library only (`net/http`), and every call takes a `context.Context`.

```bash
go get github.com/ClickHouse/loom/sdk/go
```

The full surface (every operation, typed from the server's registry) is in the
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

The client sends one request per call and never retries. It does not follow redirects, so a
memory body and token never go to another origin: a 3xx answer comes back as a `*loom.Error`.

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

The tests run against a local fake server and need no running Loom.
