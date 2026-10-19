# Runtime settings

Every setting is an environment variable `LOOM_<FIELD>` read at startup
([configuration reference](../reference/configuration.md)). A setting the reference marks
**live** also takes a deployment-wide override while Loom runs: retrieval and ranking
(`memory_search_*`), answer synthesis (`memory_ask_*`), extraction, context and knowledge
tuning. Every other setting is read once at startup and changes with a restart.

## See what is in force

```bash
loom settings list --changed              # every setting off its default
loom settings list --prefix memory_ask_   # one area
loom settings list --json
```

Each line shows the value, where it came from and whether it is live:

```text
memory_ask_recency_weight = 0.35  (override, live)
ch_host = "clickhouse"  (env)
```

| Source     | Meaning                                                  |
| ---------- | -------------------------------------------------------- |
| `override` | set with `loom settings set` or `/v1/admin/settings.set` |
| `env`      | the process environment                                  |
| `env_file` | the dotenv file (`LOOM_ENV_FILE`, default `.env`)        |
| `settings_file` | the YAML settings file (`LOOM_SETTINGS_FILE`, default `loom.yaml`) |
| `default`  | the built-in default                                     |

A secret shows only its length.

## Override a live setting

```bash
loom settings set memory_ask_recency_weight 0.35
loom settings reset memory_ask_recency_weight
```

The value is written as its environment variable would be and validated the same way: a
value out of range, or a setting that is not live, is refused and nothing is stored. The
override is one row of `loom.runtime_overrides`. The process that stores it applies it at
once; every other process, the workers included, applies it within
`LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS` (default 10). It outlives a restart and wins over
the environment until it is reset; resetting puts back each process's environment value or
the default. Each change writes a `setting.changed` audit row with the value before and
after.

The same operations over HTTP, with a platform admin token:

```bash
curl -s "$LOOM_BASE_URL/v1/admin/settings.list" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"changed": true}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "value": 0.35}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "clear": true}'
```

The Python SDK has `agent.deployment.settings()`, `set_setting(name, value)` and
`reset_setting(name)`.

## During a rolling upgrade

A build that does not have a setting, or does not read it live, logs the stored override
once and ignores it; the other processes keep applying it. Ranking settings change what
`memory.search` returns: measure a change with a benchmark before you keep it
([running a benchmark](../benchmarks/running.md)).
