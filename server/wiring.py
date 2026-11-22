"""The composition root's wiring: each platform seam gets the domain piece
that fills it.

Platform packages never import a domain (tests/unit/test_package_contracts.py);
where one needs a domain's answer it declares a seam and this module hands
the answer in: the namespace directory the auth gate and the ledger read,
the domain facts the state gauges read, the observer the ClickHouse client
calls after each insert (the memory plane's recent writes and the entity
graph's write marks), and the writer a Postgres commit's audit row names.
Every process entry point calls `wire()` once before it serves: server/main.py
at import, the worker and the CLI before they run, the test suites from their
conftest.
"""

from __future__ import annotations


def wire() -> None:
    from server.auth.directory import use_directory
    from server.code.app.index import tier_health
    from server.code.domain.index import repos
    from server.code.domain.index.compiler_tiers import TIERS
    from server.core.ch import add_insert_observer
    from server.erasure.store.consent import ConsentPolicy, use_policy
    from server.identity.app.directory import IdentityDirectory
    from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
    from server.memory.app import blacklist_guard, provenance, recent_writes
    from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES
    from server.memory.store import blacklist_policy, pg_publish, refusals
    from server.memory.store.usage import AGENT_LABEL_EVENT_TYPES
    from server.observability.budget_notifications import use_observer
    from server.observability.state_inputs import StateInputs, use_state_inputs
    from server.tasks.app.budget_notifications import schedule

    add_insert_observer(recent_writes.observe_insert)
    use_directory(IdentityDirectory())
    use_observer(schedule)
    pg_publish.use_writer(provenance.writer_actor)
    refusals.use_writer(provenance.writer_actor)
    blacklist_policy.use_policy(
        blacklist_policy.BlacklistPolicy(
            capture=lambda org: blacklist_guard.capture_policy(org),
            enforce=lambda *args, **kwargs: blacklist_guard.enforce_rows(*args, **kwargs),
            matches=lambda **kwargs: blacklist_guard.find_match(**kwargs),
        )
    )
    use_policy(
        ConsentPolicy(
            fence_names=lambda org, kind, value: refusals.erasure_fence_names(org, kind, value),
            lease_client=lambda ch: refusals._lease_client(ch),
            lock_postgres=lambda *args, **kwargs: refusals.lock_postgres(*args, **kwargs),
            record_clickhouse=lambda *args, **kwargs: refusals.record_clickhouse(*args, **kwargs),
            record_postgres=lambda *args, **kwargs: refusals.record_postgres(*args, **kwargs),
        )
    )
    use_state_inputs(
        StateInputs(
            live_chunk_sql=LIVE_CHUNK_SQL,
            unembedded_memory_types=tuple(sorted(NON_SEARCHABLE_TYPES)),
            agent_label_kinds=AGENT_LABEL_EVENT_TYPES,
            # read through the module at every probe: the registry snapshot moves
            code_repos=lambda: repos.configured_repos(),
            repos_config_errors=lambda: repos.config_error_count(),
            compiler_tiers=TIERS,
            # the tiers the `code_tier_failing` problem reports, each with its grade
            failing_code_tiers=lambda: tier_health.problem_rows() or (),
        )
    )
