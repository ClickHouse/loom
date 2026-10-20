"""Namespace registration, write marks and bench-namespace GC."""

from __future__ import annotations

from pydantic import BaseModel, Field


class NamespaceSettings(BaseModel):
    # ---- namespace write marks ----
    # loom.namespace_write_marks is an in-process max per (org, namespace,
    # plane), flushed as one INSERT this often.
    namespace_write_marks_flush_interval_s: float = 5.0

    # ---- namespace registration and GC ----
    # A principal's first write into a namespace with no loom.namespaces row
    # inserts one: owner = token subject, `kind = 'bench'` under a bench
    # prefix, `retention_days = 0` (never a TTL). No
    # ALTER.
    namespace_autoregister_enabled: bool = True
    # Op-name prefixes the auto-register hook ignores: ops on a namespace
    # rather than into it, the code plane (registers `kind = 'code'` itself),
    # and the operator planes.
    namespace_autoregister_skip_prefixes: str = (
        "namespace.,code.,admin.,ops.,grants.,policy.,blacklist.,erasure.,extraction_policy."
    )
    # Comma-separated name prefixes of throwaway harness namespaces: stamped
    # `kind = 'bench'` by `namespace.register` (when no kind is given) and
    # auto-register, and scanned by `namespaces-gc`.
    namespace_bench_prefixes: str = (
        "lme-,mb-,td-,locomo-,kcurve-,wpath-,bench-,ma-verbatim-,mab-,musique-"
    )
    # namespace_gc: `kind = 'bench'` namespaces with live memory and no write
    # for idle_hours are purged by one list-scoped erasure saga per batch, at
    # most sagas_per_pass a pass, kept to loom tables.
    namespace_gc_worker_enabled: bool = True
    # Seconds between namespace GC passes.
    namespace_gc_interval_seconds: int = 3600
    # Hours without a write before a bench namespace is purged.
    namespace_gc_idle_hours: int = 24
    # Namespaces one erasure saga purges, oldest write first (at least 1).
    namespace_gc_batch: int = Field(default=200, ge=1)
    # Erasure sagas one GC pass submits (at least 1).
    namespace_gc_sagas_per_pass: int = Field(default=1, ge=1)
