"""Every input field that names a namespace or org besides `org` / `namespace`
is authorized by its handler.

The transport gate checks the request's `org` and `namespace` only. A field
naming another namespace (`namespaces`, `to_namespace`, `extra_namespaces`, ...)
reaches the handler unchecked, so each one is declared here with the check its
handler runs; a new such field fails this test until it is declared. A
one-entry list counts too: `code.search` ran a one-entry `namespaces` as the
search's namespace without authorizing it.
"""

from __future__ import annotations

import re

from server.operations.registry import ALL_OPERATIONS

# A field naming a namespace, an org or an inbox other than the gated pair.
_SCOPE_FIELD = re.compile(r"(^|_)namespaces?$|(^|_)org$|_inbox$")

# (op, field) -> where the handler authorizes it.
AUTHORIZED: dict[tuple[str, str], str] = {
    ("code.search", "namespaces"): "search_fanout.authorize, a one-entry list included",
    ("code.symbol", "namespaces"): "presence.py: search_fanout.authorize",
    ("code.grep", "namespaces"): "presence.py: search_fanout.authorize",
    ("code.symbol", "lesson_namespaces"): "code_plane_reads.lesson_namespaces: readable only",
    ("code.verify_citations", "base_namespace"): "citations._readable_namespaces",
    ("knowledge.search", "namespaces"): "knowledge_search: each namespace row_in_scope",
    ("memory.for_file", "extra_namespaces"): "for_file: row_in_scope per namespace",
    ("memory.cross_promote", "from_namespace"): "copy_ops: row_in_scope ADMIN on both ends",
    ("memory.cross_promote", "to_namespace"): "copy_ops: row_in_scope ADMIN on both ends",
    ("memory.cross_promote", "from_org"): "copy_ops: row_in_scope(from_org, ...)",
    ("memory.copy_to", "from_namespace"): "copy_ops: row_in_scope ADMIN on both ends",
    ("memory.copy_to", "to_namespace"): "copy_ops: row_in_scope ADMIN on both ends",
    ("memory.copy_to", "from_org"): "copy_ops: row_in_scope(from_org, ...)",
    ("procedure.fork", "from_namespace"): "procedure_evolve._assert_can_read_namespace",
    ("procedure.fork", "from_org"): "procedure_evolve._assert_can_read_namespace",
    ("tracker.duplicate_check", "namespaces"): "dupcheck: authorized_tracker_namespaces",
    ("tracker.test_signal", "code_namespace"): "test_signal._code_namespace_authorized",
    ("tracker.source.add", "namespaces"): "sources: the namespaces the source creates",
    ("code.repo.set_source", "tracker_namespaces"): "repo_admin._tracker_or_refuse",
    ("namespace.restore", "target_namespace"): "namespace_snapshot: account_change claim",
    ("task.reassign", "to_inbox"): "transitions: _require_poster on the destination",
}


def _scope_fields() -> set[tuple[str, str]]:
    return {
        (op.name, field)
        for op in ALL_OPERATIONS
        for field in op.input_model.model_fields
        if field not in ("org", "namespace") and _SCOPE_FIELD.search(field)
    }


def test_every_second_scope_field_is_declared_with_its_check() -> None:
    undeclared = sorted(_scope_fields() - AUTHORIZED.keys())
    assert not undeclared, (
        f"{undeclared}: the gate checks only `org` and `namespace`; authorize these "
        "in the handler (row_in_scope, search_fanout.authorize) and declare it here"
    )


def test_no_declaration_outlives_its_field() -> None:
    assert not sorted(AUTHORIZED.keys() - _scope_fields())
