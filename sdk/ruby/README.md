# Loom Ruby client

A Ruby client for [ClickHouse Loom](https://clickhouse.github.io/loom/). Every operation
the server serves is a method, generated from the server's registry
(`client.memory.set(...)`, `client.task.create(...)`), so a new operation reaches the gem
by regeneration alone. It depends on the standard library only (`net/http` and `json`)
and needs Ruby 3.1 or later.

The gem is named `clickhouse-loom`. Build and install it from a checkout:

```bash
cd sdk/ruby && gem build clickhouse-loom.gemspec && gem install clickhouse-loom-*.gem
```

## Example

Against a
[single-user install](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md)
on this machine (`http://localhost:7777`, org `dev`, namespace `local`, no token):

```ruby
require "clickhouse/loom"

loom = ClickHouse::Loom::Client.new(org: "dev", namespace: "local", agent: "assistant")
loom.memory.set(memory_key: "deploy_window", value: "Deploys run Tue and Thu only.")
puts loom.memory.get(memory_key: "deploy_window")["value"]   # Deploys run Tue and Thu only.
puts loom.memory.search(query: "when do we deploy?")["results"].first["memory_key"]
loom.memory.delete(memory_key: "deploy_window")
```

`Loom` is the short name of `ClickHouse::Loom` (`Loom::Client`, `Loom.configure`) unless
your application already defines a `Loom` of its own.

## Configuration

Set the defaults once, in a Rails initializer for example:

```ruby
Loom.configure do |c|
  c.base_url = "https://loom.example.com"
  c.api_key = ENV.fetch("LOOM_API_KEY")
  c.org = "acme"
  c.namespace = "support"
  c.agent = "rails-app"
end

client = Loom::Client.new                    # takes every default above
client = Loom::Client.new(namespace: "user-42") # an argument wins over the configured value
```

| Setting                        | Default                                                                      |
| ------------------------------ | ---------------------------------------------------------------------------- |
| `base_url`                     | `LOOM_BASE_URL`, else `http://localhost:7777`                                |
| `api_key`                      | `LOOM_API_KEY`; with neither, no `Authorization` header                      |
| `org`, `namespace`, `agent`    | Left out of the body when blank, so the server's defaults apply              |
| `open_timeout`, `read_timeout` | 10 and 60 seconds                                                            |
| `retry_policy`                 | `{ max_attempts: 3, backoff: 0.25, ... }` (see [Retries](#retries))          |
| `rate_limit_max_wait`          | 15 seconds of waiting out `429` answers per call; `0` raises the first `429` |

An argument to `Client.new` wins over `Loom.configure`, which wins over the environment.

## Calls

`client.<group>.<method>(**fields)` posts `/v1/<group>.<rest>`: the group is the op name's
first segment and the method is the rest with `.` turned into `_`, as in the Python SDK
(`tracker.source.add` is `client.tracker.source_add`). A method that would shadow a method
every Ruby object has takes a trailing underscore (`memory.inspect` is
`client.memory.inspect_`). Each method returns the decoded JSON answer as a Hash.

- The fields are keywords. The bound `org`, `namespace` and `agent` are sent with every
  call, and a keyword of the same name overrides one for that call:
  `client.memory.set(memory_key: "k", value: "v", namespace: "user-42")`.
- A keyword the op does not take raises `ArgumentError` before anything is sent, with the
  nearest field name. A `nil` keyword is left out; to send an explicit `null`, pass a Hash
  body first: `client.memory.set({ "ttl_seconds" => nil }, memory_key: "k", value: "v")`.
- `request_options:` takes `idempotency_key:` (your own key, for a job that may run twice)
  and `retry_policy:` (fields set over the client's policy for this call).
- `Loom::Generated::OPS` holds every op's spec: idempotency, read-only, role, maturity,
  its fields and the required ones. `client.call("some.op", { ... })` calls an op this
  version of the gem does not know yet.

`client.search` with no argument is the `search.*` group (`client.search.query(...)`).

### Pagination

`client.paginate` walks a cursor-paginated op (`knowledge.list`, `conversation.list`,
`audit.list`, `code.search`) and returns a lazy `Enumerator` of its rows, as `paginate` does
in the TypeScript SDK. The block fetches one page for the cursor it is given (`nil` for the
first page):

```ruby
docs = client.paginate { |cursor| client.knowledge.list(limit: 50, cursor: cursor) }
docs.first(200)   # fetches only the pages it needs
docs.each { |doc| puts doc["source_uri"] }
```

A page's rows are its `rows`, else its `results`. Reading stops when a page has no
`next_cursor`, hands back the cursor it was fetched with, or after `max_pages:` pages.

### Memory shorthands

The typed memory calls of the first version of the gem remain:

| Call                                                     | Answers                                                            |
| -------------------------------------------------------- | ------------------------------------------------------------------ |
| `set(key, value, memory_type: nil, tags: nil)`           | `memory_id`, `version`, `content_sha256`, `status`, `noop`         |
| `get(key)`                                               | `found`, `memory_id`, `value`, `memory_type`, `version`, `tags`    |
| `search(query, top_k: nil, memory_type: nil, tags: nil)` | `results`: hits with `memory_key`, `content_excerpt`, `similarity` |
| `delete(key)`                                            | `found`, `tombstoned`, `version`                                   |

`value` is plain text or any Hash, Array or scalar. A missing key is not an error: `get`
answers with `found` false.

## Retries

The retry and idempotency rules of the TypeScript and Python SDKs:

- An op the server deduplicates (idempotent and not read-only, such as `memory.set`) gets
  an `Idempotency-Key`, and every resend of the call repeats it, with the same
  `X-Request-Id`.
- A connection that failed before the request went out is resent.
- `408` and `5xx` answers are resent for an op the registry marks idempotent; any other op
  only on a `408` or a refusal the server made before running it (`AUTH_UNAVAILABLE`). An
  error the server classes `configuration`, and the codes the SDKs never resend, are not.
- A timeout or an answer cut off after sending is resent only for an op the server
  deduplicates (`retry_timeouts: :idempotent`; `:never` and `:always` change that).
- The server's `Retry-After` (seconds or an HTTP-date) or `detail.retry_after_s` replaces
  the backoff when it is at most `max_retry_after` (60) seconds.
- A `429` is pacing, not a retry: the call waits out `Retry-After` for at most
  `rate_limit_max_wait` seconds, then raises `Loom::RateLimited`. A missing, zero or past
  `Retry-After` (or one over 60 s) is waited as 1 s, and a shorter one as at least 0.1 s, so
  every resend spends some of that budget.

| `retry_policy` field      | Default                                                     |
| ------------------------- | ----------------------------------------------------------- |
| `max_attempts`            | 3, the first included; 1 disables retries                   |
| `backoff`, `backoff_max`  | 0.25 s doubled per attempt, at most 5 s                     |
| `backoff_jitter`          | 0.5: each sleep is `delay * (1 - jitter/2 + rand * jitter)` |
| `statuses`                | `408` and `500` to `599`                                    |
| `retry_connection_errors` | `true`                                                      |
| `retry_timeouts`          | `:idempotent`                                               |
| `respect_retry_after`     | `true`, up to `max_retry_after` (60 s)                      |
| `total_backoff_budget`    | unbounded seconds of sleep per call                         |

Not carried over yet: pacing on the `X-RateLimit-*` headers before a `429` arrives, a
per-call timeout, and resending a `401` with a fresh token (the gem takes a fixed
`api_key`).

The client does not follow redirects, so a memory body and token never go to another
origin: a `3xx` answer raises a `Loom::Error`.

## Errors

A non-2xx answer raises a `Loom::Error` carrying the server's `code`, its `category`
(`input`, `auth`, `capacity`, `dependency`, `configuration` or `internal`), whether
resending can succeed (`retryable?`), the wait it asked for in seconds (`retry_after`),
the `request_id`, the HTTP `status` and the whole `detail`. Each server code has its own
subclass, generated from the server's code list (`PINNED_CONFLICT` raises
`Loom::PinnedConflict`, `RATE_LIMITED` raises `Loom::RateLimited`); a code this version
does not know raises `Loom::Error` itself. Match on the class or the `code`, never on the
message:

```ruby
begin
  client.memory.set(memory_key: "deploy_window", value: "Mondays")
rescue Loom::PinnedConflict => e
  client.memory.update(memory_key: "deploy_window", value: "Mondays") # keeps the pin
rescue Loom::RateLimited => e
  sleep(e.retry_after || 1)
end
```

A call that gets no complete answer raises an `Error` with the code `NETWORK`, and a 2xx
answer that is not a JSON object one with `INVALID_RESPONSE`; neither is marked retryable,
since the call may have run. The codes and what to do about each are in the
[errors reference](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).

## Generated files

`lib/clickhouse/loom/generated/ops.rb` and `errors.rb` are written by
`python scripts/codegen/sdk_rb_gen.py` from the server's registry and error codes; never
edit them by hand. `python scripts/codegen/sdk_rb_gen.py --check` (in `make lint` and the
unit suite) fails when they are stale.

## Tests

```bash
cd sdk/ruby && bundle install && bundle exec rake test
```

The tests run against a local fake server and need no running Loom.
