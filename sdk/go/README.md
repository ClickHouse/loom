# Loom Go client

A minimal Go client for [ClickHouse Loom](https://loom.clickhouse.com/docs)'s memory
operations: `memory.set`, `memory.get`, `memory.search` and `memory.delete`. It depends
on the standard library only (`net/http`), and every call takes a `context.Context`.

```bash
go get github.com/ClickHouse/loom/sdk/go
```

The [Go SDK guide](https://loom.clickhouse.com/docs/reference/go-sdk) covers
configuration, team access, and error handling. Every operation has a typed method in
the [Python](https://loom.clickhouse.com/docs/reference/python-sdk) and
[TypeScript](https://loom.clickhouse.com/docs/reference/typescript-sdk) SDKs.

## Example

Against a
[single-user install](https://loom.clickhouse.com/docs/getting-started/quickstart)
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

## Tests

```bash
cd sdk/go && go test ./...
```

The tests run against a local fake server and need no running Loom.
