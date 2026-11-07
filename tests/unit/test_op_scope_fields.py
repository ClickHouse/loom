"""Every input field that names a namespace, org or inbox besides `org` /
`namespace` is declared with the function that authorizes it.

The transport gate checks the request's `org` and `namespace` only. A field
naming another one (`namespaces`, `to_namespace`, `inbox`, ...) reaches the
handler unchecked, so each is declared here with the function that checks it;
a new such field fails this test until it is declared, and the function must
exist (by name: that the handler calls it is the reviewer's to read). A
`namespaces` field is declared even when its handler rewrites a one-entry list
into `namespace`.

Out of this pattern: names that resolve to a namespace indirectly (`code.repos`'
`repo` / `repos`, filtered by readability), `namespace_scope` (the `all_mine`
fan-out, which `server/auth/fanout.py` filters through the gate), a
collection's `parent` (`collection.create` / `move`, checked for WRITER by
`server/identity/app/collections.py`), `scope_value` (`erasure.request` /
`erasure.export`, whose target `scope_type` decides; checked for an org-wide or
namespace ADMIN by `server.erasure.app.handlers.handle_erasure_request` and
`handle_erasure_export`), and fields of nested models.
"""

from __future__ import annotations

import importlib
import re

import pytest

from server.operations.registry import ALL_OPERATIONS

_SCOPE_FIELD = re.compile(r"(^|_)namespaces?$|(^|_)orgs?$|(^|_)inbox(es)?$")

_FANOUT = "server.code.app.search_fanout.authorize"
# checked in the handler itself: ADMIN on both ends to promote; read the source,
# write the target to copy
_PROMOTE = "server.memory.app.copy_ops.handle_memory_cross_promote"
_COPY = "server.memory.app.copy_ops.handle_memory_copy_to"
_READABLE_INBOX = "server.tasks.app.common._load_readable_inbox"

# (op, field) -> the function that authorizes the field's value.
AUTHORIZED: dict[tuple[str, str], str] = {
    ("code.search", "namespaces"): _FANOUT,
    ("code.symbol", "namespaces"): _FANOUT,
    ("code.grep", "namespaces"): _FANOUT,
    ("code.symbol", "lesson_namespaces"): "server.memory.app.code_plane_reads.lesson_namespaces",
    ("code.verify_citations", "base_namespace"): "server.code.app.citations._readable_namespaces",
    ("knowledge.search", "namespaces"): "server.knowledge.app.knowledge_search._named_namespaces",
    ("memory.for_file", "extra_namespaces"): "server.memory.app.for_file._readable_extras",
    ("memory.cross_promote", "from_namespace"): _PROMOTE,
    ("memory.cross_promote", "to_namespace"): _PROMOTE,
    ("memory.cross_promote", "from_org"): _PROMOTE,
    ("memory.copy_to", "from_namespace"): _COPY,
    ("memory.copy_to", "to_namespace"): _COPY,
    ("memory.copy_to", "from_org"): _COPY,
    ("procedure.fork", "from_namespace"): (
        "server.memory.app.procedure_evolve._assert_can_read_namespace"
    ),
    ("procedure.fork", "from_org"): "server.memory.app.procedure_evolve._assert_can_read_namespace",
    ("tracker.duplicate_check", "namespaces"): (
        "server.tracker.app.dupcheck_fanout.readable_tracker_namespaces"
    ),
    ("tracker.test_signal", "code_namespace"): (
        "server.tracker.app.test_signal._code_namespace_authorized"
    ),
    # names the namespaces the source creates; a taken one is refused
    ("tracker.source.add", "namespaces"): "server.tracker.app.sources._namespace_taken",
    ("code.repo.set_source", "tracker_namespaces"): "server.code.app.repo_admin._tracker_or_refuse",
    (
        "namespace.restore",
        "target_namespace",
    ): "server.identity.app.namespace.require_admin_of_existing",
    ("task.reassign", "to_inbox"): "server.tasks.app.trust._require_poster",
    ("task.create", "inbox"): "server.tasks.app.trust._require_poster",
    ("task.pull", "inbox"): _READABLE_INBOX,
    ("task.list", "inbox"): _READABLE_INBOX,
    ("task.counts", "inbox"): _READABLE_INBOX,
    ("task.updates", "inbox"): _READABLE_INBOX,
    ("inbox.policy_set", "inbox"): "server.tasks.app.common._require_admin_side",
    ("inbox.policy_list", "inbox"): _READABLE_INBOX,
    ("inbox.principals", "inbox"): "server.tasks.app.common._require_admin_side",
    # registers the inbox in the gated namespace; a name owned elsewhere is a conflict
    # that names the owner only to its readers
    ("inbox.register", "inbox"): "server.tasks.app.inboxes.handle_inbox_register",
    ("code.coverage.watch", "inbox"): "server.code.app.coverage_watch._authorize",
    ("code.coverage.unwatch", "inbox"): "server.code.app.coverage_watch._authorize",
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
        "in the handler (row_in_scope, search_fanout.authorize) and declare the check here"
    )


def test_no_declaration_outlives_its_field() -> None:
    stale = sorted(AUTHORIZED.keys() - _scope_fields())
    assert not stale, f"{stale}: these fields are gone; drop their declarations"


@pytest.mark.parametrize("ref", sorted(set(AUTHORIZED.values())))
def test_each_declared_check_exists(ref: str) -> None:
    module, name = ref.rsplit(".", 1)
    assert callable(getattr(importlib.import_module(module), name, None)), ref
