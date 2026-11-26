---
title: Go SDK for AI agent memory
description: Connect a Go service to self-hosted Loom to save, retrieve, search, and delete agent memories over HTTP.
---

# Go SDK for AI agent memory

The Go SDK stores and retrieves agent memory from a running Loom server over HTTP. Use it to persist facts and decisions from a Go service and recall them in another session. It needs Go 1.22 or later and uses the Go standard library.

## Install

Run this from your application's Go module.

```bash
go get github.com/ClickHouse/loom/sdk/go
```

## Save and read a decision

Start the [local stack](../getting-started/quickstart.md#1-boot-the-stack) and run this program. It uses the local single-user identity. The same client can be reused by concurrent requests.

```go
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	loom "github.com/ClickHouse/loom/sdk/go"
)

func main() {
	client := loom.New(loom.Config{
		Org: "dev", Namespace: "local", Agent: "release-assistant",
	})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	const key = "release_window"
	if _, err := client.Set(ctx, key, map[string]string{"day": "Thursday"}, nil); err != nil {
		log.Fatal(err)
	}
	memory, err := client.Get(ctx, key)
	if err != nil {
		log.Fatal(err)
	}
	if !memory.Found {
		log.Fatal("release window was not found")
	}
	var decision map[string]string
	if err := json.Unmarshal(memory.Value, &decision); err != nil {
		log.Fatal(err)
	}
	fmt.Println(decision["day"])
}
```

| Method                          | Operation                                            | Result                                                                                              |
| ------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `Set(ctx, key, value, options)` | [`memory.set`](operations/memory.md#memoryset)       | Memory ID, version, and write status. Accepts JSON objects, arrays, strings, numbers, and booleans. |
| `Get(ctx, key)`                 | [`memory.get`](operations/memory.md#memoryget)       | `Found` and the current value as `json.RawMessage`. A missing key returns `Found: false`.           |
| `Search(ctx, query, options)`   | [`memory.search`](operations/memory.md#memorysearch) | Ranked memory excerpts. `SearchOptions` can limit the results by count, memory type, or tags.       |
| `Delete(ctx, key)`              | [`memory.delete`](operations/memory.md#memorydelete) | `Found` and `Tombstoned` describe whether the operation found and retired a live value.             |

## Connect to a team server

Set `LOOM_BASE_URL` to the server URL and load the bearer token into `LOOM_API_KEY` from your secret store. Set `Org`, `Namespace`, and `Agent` for your application in `loom.Config`. Access comes from the token's [grants](../guides/tokens-and-grants.md). Use a writer grant on that namespace for the example's `Set` call.

| `loom.Config` field         | When empty                                                                        |
| --------------------------- | --------------------------------------------------------------------------------- |
| `BaseURL`                   | Reads `LOOM_BASE_URL`, then uses `http://localhost:7777`.                         |
| `APIKey`                    | Reads `LOOM_API_KEY`. With both empty, the request has no `Authorization` header. |
| `Org`, `Namespace`, `Agent` | The request omits the empty fields and Loom applies its defaults.                 |
| `HTTPClient`                | Uses a copy of `http.DefaultClient`.                                              |

The SDK returns a redirect as `*loom.Error` when the HTTP client has no custom `CheckRedirect` policy. Use the server's final URL for a request carrying a token or memory body.

## Handle errors and deadlines

Pass a `context.Context` to each operation. The SDK leaves retries to the caller. For an HTTP error, use `errors.As` to read `*loom.Error` and match on `Code`. Use `Retryable` and `RetryAfter` when deciding whether to retry. When present, `RequestID` identifies the request in server logs. Check the [HTTP idempotency contract](http-api.md#idempotency) before resending writes whose outcome is unknown.

Use the [HTTP API](http-api.md), [Python SDK](python-sdk.md), or [TypeScript SDK](typescript-sdk.md) for Loom operations beyond the Go client's memory methods. See the [Go source](https://github.com/ClickHouse/loom/tree/main/sdk/go) for the client and tests.
