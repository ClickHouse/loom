"""Telemetry, audit, tracing and the ops surface."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from server.settings._env import CaseFolded, JsonObject


class ObservabilitySettings(BaseModel):
    # Root log level. (Bind host/port are flags of `loom serve`, not settings.)
    log_level: Annotated[Literal["debug", "info", "warning", "error", "critical"], CaseFolded] = (
        "info"
    )
    # "text" (dev) or "json" (production).
    log_format: Annotated[Literal["text", "json"], CaseFolded] = "text"

    # ops.health's create/read/update/delete mix over the window: warns when
    # reads per write exceed the bar (polling load) or, after `min_writes`,
    # the update share falls below its bar (append-only bank).
    ops_op_mix_window_days: int = 7
    # Reads per write above which the `op_mix` check warns (polling load).
    ops_op_mix_reads_per_write_warn: float = 20.0
    # Share of writes that are updates below which the check warns (append-only bank).
    ops_op_mix_update_share_warn: float = 0.01
    # Writes in the window before the update-share warning can fire.
    ops_op_mix_min_writes: int = 500
    # ops.health `procedure_plane`: warns when ≥ `min_packs` packs flowed and
    # none showed a procedure while procedures exist, or when success retention
    # over ≥ `min_pairs` pairs is below `retention_warn`.
    ops_procedure_plane_window_days: int = 7
    # Context packs in the window before "no pack showed a procedure" can warn.
    ops_procedure_plane_min_packs: int = 50
    # Success retention (fraction) below which the `procedure_plane` check warns.
    ops_procedure_plane_retention_warn: float = 0.5
    # Pairs the retention is measured over before its warning can fire.
    ops_procedure_plane_min_pairs: int = 20
    # Periodic re-hash of audit rows; a mismatch writes `audit.tamper_detected`.
    # Per-row detection, not a hash chain (see server/observability/audit.py).
    audit_verifier_enabled: bool = True
    # Seconds between audit verifier passes (row re-hash and checkpoint sealing).
    audit_verifier_interval_seconds: int = 3600
    # Length, seconds, of one audit window. Each closed window is sealed as a
    # chained checkpoint once `grace_seconds` old, and the last `verify_windows`
    # are recounted every pass: a short recount is a deletion, which per-row
    # hashes cannot see.
    audit_checkpoint_window_seconds: int = 3600
    # Seconds after a window closes before it is sealed.
    audit_checkpoint_grace_seconds: int = 600
    # Most recent sealed windows recounted every pass (at least 1).
    audit_checkpoint_verify_windows: int = Field(default=24, ge=1)
    # Most windows one pass seals; a backlog is sealed over later passes.
    audit_checkpoint_max_seal_per_pass: int = 48

    # OTel span exporter: "ch" writes to `default.otel_traces` on the same
    # service, "otlp" ships to otel_exporter_otlp_endpoint (headers: comma-
    # separated K=V), "" = auto (otlp when the endpoint is set, else ch).
    otel_exporter: Annotated[Literal["", "ch", "otlp"], CaseFolded] = ""
    # Seconds between refreshes of the per-namespace code-plane COUNT gauges
    # (one FINAL scan each of code_files, code_chunks, code_symbols and
    # code_edges, on the lease holder). The counts move once an index run, so
    # an hour is enough; each refresh reads every live code row. 0 = every export.
    otel_code_counts_cache_s: int = 3600
    # Seconds between recounts of the code.grep wrong-abstain gauge (a
    # `content` scan per namespace with abstains). 0 = gauge off.
    otel_code_grep_replay_cache_s: int = 3600
    # Max namespaces `loom.memory.agent_labels_24h` names per probe (top N by
    # label total); the rest sum under `loom.namespace = "other"`. 0 = no cap.
    otel_agent_labels_max_namespaces: int = 20
    # Seconds between the state probe's whole-table readings (embed backlog,
    # integrity invariants), taken on the lease holder only. The invariants
    # read 0 and exist to catch drift, so an hour is enough; each reading is
    # one FINAL pass over memory, memory_embeddings and entity_vectors.
    otel_invariants_probe_s: int = 3600
    # Seconds between retention censuses (`loom.retention.*`: per-org counts
    # over 7- and 45-day windows; one FINAL pass over loom.memory on the lease
    # holder). 0 = every export.
    otel_retention_census_s: int = 3600
    # Who takes the whole-table censuses (invariants, code-plane counts,
    # retention, agent labels, storage): "leader" = only the supervisor-lease
    # holder, "process" = every process (costly).
    otel_census_scope: Literal["leader", "process"] = "leader"
    # Metric export cadence, seconds. Spans keep their own 3 s batch delay,
    # which bounds span loss at SIGTERM.
    otel_metrics_export_interval_s: int = 60
    # Delta temporality for counters and histograms (idle series write no row).
    # Off: the dashboards (deploy/hyperdx_dashboards.md) read cumulative rows —
    # flip together with them.
    otel_metrics_delta_temporality: bool = False
    # Write a gauge point only when its value changed or this many seconds
    # after the last write; 0 = every export. The ClickHouse exporter only
    # (LOOM_OTEL_EXPORTER=ch); OTLP exports every point. Off because gauge
    # tiles fill gaps with 0 and would draw a sawtooth.
    otel_gauge_heartbeat_s: int = 0
    # OTLP/HTTP collector base URL (`/v1/traces` is appended); set with
    # `otel_exporter` = "", it selects the otlp exporter.
    otel_exporter_otlp_endpoint: str = ""
    # Comma-separated K=V headers sent to that endpoint, e.g.
    # `Authorization=Bearer <token>`.
    otel_exporter_otlp_headers: str = ""
    # OTel resource `service.name` and `deployment.environment` stamped on
    # every span, log and metric.
    otel_service_name: str = "loom"
    # The `deployment.environment` value.
    otel_environment: str = "dev"
    # Comma-separated path regexes the FastAPI instrumentation opens no span
    # for (health polls). "" = instrument every path.
    otel_excluded_urls: str = "/v1/health"

    # Scales every per-op `budget_p99_ms` (tuned for local CH) at the
    # comparison site; set e.g. 20.0 on Cloud so SLO alerts don't fire on
    # baseline RTT.
    latency_budget_multiplier: float = 1.0
    # JSON map {consumer: {op | "*": ms | "<n>x"}}: an integer is a budget in
    # ms, "3x" multiplies the op's declared budget. Checked after runtime
    # `admin/budget.set` overrides, before per-op ones; malformed = logged once
    # and ignored. '' = none.
    latency_budget_overrides_by_consumer: Annotated[str, JsonObject] = ""

    # One agent_events row per worker pass that spent something or failed
    # (operation worker.<name>.pass, kind='worker'); loom.worker_status records
    # every pass.
    worker_ledger_rows: bool = True

    # Master switch for OTel emission. `init_otel()` also refuses under pytest
    # regardless: server/main.py calls it at import, and a test run must never
    # ship rows into production observability tables.
    otel_enabled: bool = True

    # Langfuse: with both set, OTel spans are forwarded to Langfuse's OTLP
    # endpoint as a second span processor beside the ClickHouse exporter,
    # retrieval scores are posted, and the erasure saga makes real HTTP DELETE
    # calls (empty = log intent only). The token is `pk-lf-…:sk-lf-…`.
    langfuse_url: str = ""
    # Langfuse `pk-lf-…:sk-lf-…` key pair for erasure, scores and the OTLP forward.
    langfuse_admin_token: str = ""
    # Fraction of TRACES forwarded, sampled by trace id so a trace arrives
    # whole. Span volume is dominated by worker embeddings; turn down in
    # production.
    langfuse_sample_rate: float = 1.0
    # Span-name prefixes never forwarded: high-volume infrastructure spans with
    # no LLM work (they belong in ClickStack).
    langfuse_span_denylist: str = "ch.query,ch.insert,GET /v1/health,POST /v1/health"
    # Score-POST circuit breaker: consecutive failures that open it.
    langfuse_circuit_failure_threshold: int = 5
    # Seconds the open breaker waits before a trial call (floor 1).
    langfuse_circuit_cooldown_s: float = Field(default=30.0, ge=1.0)
    # How much LLM prompt/completion text is recorded, by what the prompt holds:
    #   "off"   — none (default; consent is given, not withdrawn).
    #   "query" — only legs whose prompt is the user's own search text (hyde,
    #             query_rewrite, enrichment, …).
    #   "all"   — also legs carrying retrieved memories or conversation turns
    #             (inline_rerank, iterative_judge, extract, session_summary, …).
    # A namespace's `capture_generation_io` overrides it (false ⇒ off, true ⇒
    # all, NULL ⇒ this). It also gates loom.search_trace_log.query. Token
    # counts, cost, latency and quality scores are never gated.
    langfuse_capture_io_level: Annotated[Literal["off", "query", "all"], CaseFolded] = "off"

    # Per-field cap on recorded prompt/completion text; truncation adds an
    # explicit marker.
    langfuse_generation_io_max_chars: int = 2000
    # Fraction of searches sent to an independent LLM judge, written back as a
    # `retrieval_relevance` score (unlike loom's own confidence scores, it
    # shares no state with retrieval). 0.0 = off: it costs money per search;
    # a few percent (e.g. 0.05) suits production.
    retrieval_judge_sample_rate: float = 0.0
