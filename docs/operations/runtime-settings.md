# Runtime settings

Every setting is an environment variable `LOOM_<FIELD>` read at startup, and can also go
in the YAML settings file ([configuration reference](../reference/configuration.md)). A
setting the reference marks **live** also takes a deployment-wide override while Loom
runs (among them retrieval and ranking, `memory_search_*`; answer synthesis,
`memory_ask_*`; extraction, context and knowledge tuning; and ClickHouse insert batching,
`ch_batched_insert*`). Every other setting is read once at startup and changes with a
restart. The similarity gates that calibration replaces
(`memory_search_min_similarity`, `memory_search_abstain_threshold`,
`memory_search_hyde_fallback_threshold`, `memory_search_query_rewrite_threshold`) are not
live: with `LOOM_ADAPTIVE_THRESHOLDS` on they apply only when there is no calibration.

## See what is in force

```bash
loom settings list --changed              # off its default, or overridden
loom settings list --prefix memory_ask_   # one area
loom settings list --json
```

Each line shows the value, where it came from and whether it is live:

```text
memory_ask_recency_weight = 0.35  (override, live)
ch_host = "clickhouse"  (env)
```

| Source          | Meaning                                                                                  |
| --------------- | ---------------------------------------------------------------------------------------- |
| `override`      | set with `loom settings set` or `/v1/admin/settings.set`                                 |
| `dashboard`     | the model configuration saved in Settings → Models & keys                                |
| `env`           | the process environment                                                                  |
| `env_file`      | the dotenv file (`LOOM_ENV_FILE`, default `.env`), also when copied into the environment |
| `settings_file` | the YAML settings file (`LOOM_SETTINGS_FILE`), also when `scripts/serve.sh` exports it   |
| `inferred`      | filled in at startup from other settings (the LLM provider, a local model's width)       |
| `default`       | the built-in default                                                                     |

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
the environment and the settings file until it is reset; resetting puts back each
process's own value or the default. `reset` refuses a setting with no stored override, and
removes the override of a setting a later build no longer has. Each change writes a
`setting.changed` audit row with the value before and after.

`--expect <value>` (`expected` over HTTP) changes nothing unless that is the value in
force after a fresh read of the stored overrides, and answers `409 PRECONDITION_FAILED`
with the value it found: a change made since through another replica or the CLI is not
overwritten unseen. Two changes sent in the same instant can still both pass.

## See and undo past changes

```bash
loom settings history memory_ask_recency_weight
loom settings history --limit 20      # every setting
```

Each line is one recorded change, newest first: when, who (`cli:<user>` or the token
subject) and the value before and after. A value before that was an override is the
deployment's: undo by setting it back with `loom settings set`. Any other was the value
on the process that made the change (its environment or a default), marked
`(undo: reset)`: undo with `loom settings reset`, which puts back each replica's own. A
change with no value before (the reset of a setting this build no longer has) has nothing
to undo.

An override is the deployment's default, so what a namespace or a request sets still wins
over it: a namespace's `retrieval_defaults` (`top_k`, `min_similarity`,
`reinforcement_weight` and the other search knobs `namespace.register` takes) and a
request's own fields.

The same operations over HTTP, with a platform admin token. With auth off, a change is
accepted only on a loopback address (`localhost`): a web page could otherwise reach it
through DNS rebinding.

```bash
curl -s "$LOOM_BASE_URL/v1/admin/settings.list" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"changed": true}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "value": 0.35}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "clear": true}'
```

`POST /v1/admin/settings.history` takes `{"name": "...", "limit": 50}`. The Python SDK
has `agent.deployment.settings()`, `set_setting(name, value, expected=...)`,
`reset_setting(name, expected=...)` and `setting_history(name)`.

## During a rolling upgrade

A build that does not have a setting, does not read it live, or whose field refuses the
stored value, logs the stored override once and ignores it; the other processes keep
applying it. A build from before runtime settings existed ignores the stored overrides
without a word, so while one still serves, an override applies on the newer replicas
only. Ranking settings change what `memory.search` returns: measure a change with a
benchmark before you keep it ([running a benchmark](../benchmarks/running.md)).
