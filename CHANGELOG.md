# Changelog

All notable changes to Loom (the server, its web app and its coding-agent plugins). The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). The SDKs keep their own changelogs in
[sdk/python](./sdk/python/CHANGELOG.md) and [sdk/typescript](./sdk/typescript/CHANGELOG.md).
A release tag `vX.Y.Z` ships only when the top entry reads `## [X.Y.Z] - YYYY-MM-DD`.

## [Unreleased]

### Added

- `CODED_WIDTH_MISMATCH` (503): a write whose vector is not the width its coded column was
  created at; `memory.set`, `bulk_set`, `update` and the `knowledge.*` writes are refused before
  any of their rows lands, and `/v1/health` reports `coded_width_mismatch` with the fix.
- `OVERRIDE_NOT_SHARED` (503): a budget or rate-limit override the server's web processes could
  not share is refused and changes nothing; the server log names the file.
- `LOOM_RUNTIME_DIR`: the directory one server's web processes share files in.
  `scripts/serve.sh` chooses it once and exports it; set it for another launcher that starts
  several workers.
- `LOOM_LISTEN_HOST`: the address `scripts/serve.sh` binds; loopback by default, every
  interface in the image.
- Migration `026` adds `run_id` skip indexes to `agent_events` and `agent_runs`.

### Changed

- **Breaking:** `rerank` (`memory.search`, `memory.search_trace`, `code.search`, `code.ask`)
  and a namespace's `rerank_endpoint` take a name, not a URL: `none`, `builtin:openai[:model]`,
  `builtin:cohere[:model]`, or an endpoint the operator names in `LOOM_RERANK_ENDPOINTS_JSON`.
  A URL or an unknown name is a 422. A namespace that still stores a URL is no longer reranked.
- **Operators:** every stack runs ClickHouse **26.9.17.8**. A fresh install creates its vector
  columns coded (`Quantized('rabitq', dim, 0)`) when the width is a multiple of 8; an install
  whose vector tables are plain stays plain until `scripts/ops/vector_codec_rebuild.py` moves
  it. `LOOM_CLICKHOUSE_IMAGE` overrides the compose image (for example
  `clickhouse/clickhouse-server:head`, unsupported).
- **Operators:** with `LOOM_WEB_WORKERS` unset, the image starts one web process per CPU the
  container may use, at most one per GiB of memory and at most 8, instead of one. Set it to
  keep the old behaviour or to leave cores for ClickHouse on a shared host.
- **Operators:** the image owns `/run/loom` (uid 999) and the compose files mount it as a
  tmpfs. Runtime files under the earlier names (`loom-ratelimit-<pid>.buckets`,
  `loom-graph-marks-<pid>/`) are not removed automatically; delete them once the old server is
  gone.
- With `LOOM_EMBEDDING_DIM` unset, a `local:*` embedding model sets the width itself (384 for
  `bge-small`).
- `loom reembed` refuses a width the coded vector tables do not take; change a model of another
  width in Settings → Models & keys, which rebuilds the tables.
- An MCP session's implicit run is shared by every web process of the server, filed in the
  caller's default namespace under the user and session the call acts as. Its id binds the
  token's subject and is no longer `mcp-<Mcp-Session-Id>`; a `run.begin` naming an id of its
  shape (`mcp-` and 40 hex digits) is answered with an id of its own. Its close no longer
  writes `metadata.calls`: count the `agent_events` rows with its `run_id`. The new
  `mcp_session_run_sweep` worker closes an idle one from the ledger, so it closes after a
  restart too, and so do the implicit runs an earlier server left open. **Operators:** a web
  process no longer closes idle runs itself, so a deployment that runs no background workers
  anywhere (`LOOM_BACKGROUND_WORKERS=off` on every process) keeps them open.

### Fixed

- A request whose body holds a number JSON cannot carry back (`1e400` parses to infinity) gets
  a 422 that echoes it as the string `"Infinity"`, instead of a 500.
- `admin/budget.set` refuses an integer too large for a float with a 422; it was stored, and
  the server's other web processes then ignored every budget override.

## [1.0.0] - 2026-10-16

First public release.
