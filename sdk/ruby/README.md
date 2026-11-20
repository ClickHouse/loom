# Loom Ruby client

A minimal Ruby client for [ClickHouse Loom](https://clickhouse.github.io/loom/)'s memory
operations: `memory.set`, `memory.get`, `memory.search` and `memory.delete`. It depends on
the standard library only (`net/http` and `json`) and needs Ruby 3.1 or later.

The gem is named `clickhouse-loom`. Build and install it from a checkout:

```bash
cd sdk/ruby && gem build clickhouse-loom.gemspec && gem install clickhouse-loom-*.gem
```

The full surface (every operation, typed from the server's registry) is in the
[Python](https://github.com/ClickHouse/loom/tree/main/sdk/python) and
[TypeScript](https://github.com/ClickHouse/loom/tree/main/sdk/typescript) SDKs.

## Example

Against a
[single-user install](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md)
on this machine (`http://localhost:7777`, org `dev`, namespace `local`, no token):

```ruby
require "clickhouse/loom"

loom = ClickHouse::Loom::Client.new(org: "dev", namespace: "local", agent: "assistant")
loom.set("deploy_window", "Deploys run Tue and Thu only.")
puts loom.get("deploy_window").value                   # Deploys run Tue and Thu only.
puts loom.search("when do we deploy?").results.first.memory_key
loom.delete("deploy_window")
```

## Configuration

`ClickHouse::Loom::Client.new(...)` binds the server and the identity once; every call sends
the org, namespace and agent in its body.

| Argument                       | Default                                                         |
| ------------------------------ | --------------------------------------------------------------- |
| `base_url`                     | `LOOM_BASE_URL`, else `http://localhost:7777`                   |
| `api_key`                      | `LOOM_API_KEY`; with neither, no `Authorization` header         |
| `org`, `namespace`, `agent`    | Left out of the body when blank, so the server's defaults apply |
| `open_timeout`, `read_timeout` | 10 and 60 seconds                                               |

The client sends one request per call and never retries. It does not follow redirects, so a
memory body and token never go to another origin: a 3xx answer raises a
`ClickHouse::Loom::Error`.

## Calls

| Call                                                     | Answers                                                            |
| -------------------------------------------------------- | ------------------------------------------------------------------ |
| `set(key, value, memory_type: nil, tags: nil)`           | `memory_id`, `version`, `content_sha256`, `status`, `noop`         |
| `get(key)`                                               | `found`, `memory_id`, `value`, `memory_type`, `version`, `tags`    |
| `search(query, top_k: nil, memory_type: nil, tags: nil)` | `results`: hits with `memory_key`, `content_excerpt`, `similarity` |
| `delete(key)`                                            | `found`, `tombstoned`, `version`                                   |

`value` is plain text or any Hash, Array or scalar. A missing key is not an error: `get`
answers with `found` false.

## Errors

A non-2xx answer raises a `ClickHouse::Loom::Error` carrying the server's `code`, its
`category` (`input`, `auth`, `capacity`, `dependency`, `configuration` or `internal`),
whether resending can succeed (`retryable?`), the wait it asked for in seconds
(`retry_after`), the `request_id` and the HTTP `status`. Match on `code`, never on the
message:

```ruby
begin
  loom.get("deploy_window")
rescue ClickHouse::Loom::Error => e
  sleep(e.retry_after || 1) if e.code == "RATE_LIMITED"
end
```

A call that gets no complete answer raises an `Error` with the code `NETWORK`, and a 2xx
answer that is not a JSON object one with `INVALID_RESPONSE`; neither is marked retryable,
since the call may have run. The codes and what to do about each are in the
[errors reference](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).

## Tests

```bash
cd sdk/ruby && bundle install && bundle exec rake test
```

The tests run against a local fake server and need no running Loom.
