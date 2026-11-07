"""The composition root's wiring: each platform seam gets the domain piece
that fills it.

Platform packages never import a domain (tests/unit/test_package_contracts.py);
where one needs a domain's answer it declares a seam and this module hands
the answer in: the namespace directory the auth gate and the ledger read,
the domain facts the state gauges read, the observers the ClickHouse
client calls after each insert (the memory plane's recent writes and
graph write marks), and the writer a Postgres commit's audit row names.
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
    from server.core import namespace_write_marks
    from server.core.ch import add_insert_observer
    from server.identity.app.directory import IdentityDirectory
    from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
    from server.memory.app import provenance, recent_writes
    from server.memory.store import pg_publish
    from server.memory.store.usage import AGENT_LABEL_EVENT_TYPES
    from server.observability.state_inputs import StateInputs, use_state_inputs

    add_insert_observer(recent_writes.observe_insert)
    add_insert_observer(namespace_write_marks.observe_insert)
    use_directory(IdentityDirectory())
    pg_publish.use_writer(provenance.writer_actor)
    use_state_inputs(
        StateInputs(
            live_chunk_sql=LIVE_CHUNK_SQL,
            agent_label_kinds=AGENT_LABEL_EVENT_TYPES,
            # read through the module at every probe: the registry snapshot moves
            code_repos=lambda: repos.configured_repos(),
            repos_config_errors=lambda: repos.config_error_count(),
            compiler_tiers=TIERS,
            failing_code_tiers=tier_health.metric_rows,
        )
    )
