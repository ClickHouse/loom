"""The `namespace` operations the registry serves — first-class metadata for
tenant namespaces. Wire models are in models.py; handlers in
server/identity/app/namespace*.py."""

from __future__ import annotations

from server.identity.api.models import (
    NamespaceBrowseInput,
    NamespaceBrowseOutput,
    NamespaceCancelRequestInput,
    NamespaceCancelRequestOutput,
    NamespaceDeleteInput,
    NamespaceDeleteOutput,
    NamespaceGetInput,
    NamespaceGetOutput,
    NamespaceJoinInput,
    NamespaceJoinOutput,
    NamespaceLeaveInput,
    NamespaceLeaveOutput,
    NamespaceListInput,
    NamespaceListMineInput,
    NamespaceListMineOutput,
    NamespaceListOutput,
    NamespaceMarkSeenInput,
    NamespaceMarkSeenOutput,
    NamespaceRegisterInput,
    NamespaceRegisterOutput,
    NamespaceRestoreInput,
    NamespaceRestoreOutput,
    NamespaceRollbackInput,
    NamespaceRollbackOutput,
    NamespaceSnapshotInput,
    NamespaceSnapshotOutput,
)
from server.identity.api.models_namespace_catalog import (
    NamespaceAuditInput,
    NamespaceAuditOutput,
    NamespaceListCategoriesInput,
    NamespaceListCategoriesOutput,
    NamespaceListOntologyInput,
    NamespaceListOntologyOutput,
    NamespaceRegisterCategoryInput,
    NamespaceRegisterCategoryOutput,
    NamespaceRegisterOntologyInput,
    NamespaceRegisterOntologyOutput,
    NamespaceSearchCategoriesInput,
    NamespaceSearchCategoriesOutput,
    NamespaceSummaryInput,
    NamespaceSummaryOutput,
    NamespaceTaxonomyInput,
    NamespaceTaxonomyOutput,
    NamespaceValidateInput,
    NamespaceValidateOutput,
)
from server.identity.app.namespace import (
    handle_namespace_browse,
    handle_namespace_delete,
    handle_namespace_get,
    handle_namespace_list,
    handle_namespace_register,
)
from server.identity.app.namespace_audit import handle_namespace_audit
from server.identity.app.namespace_catalog import (
    handle_namespace_list_categories,
    handle_namespace_register_category,
    handle_namespace_search_categories,
    handle_namespace_taxonomy,
)
from server.identity.app.namespace_membership import (
    handle_namespace_cancel_request,
    handle_namespace_join,
    handle_namespace_leave,
    handle_namespace_list_mine,
    handle_namespace_mark_seen,
)
from server.identity.app.namespace_ontology import (
    handle_namespace_list_ontology,
    handle_namespace_register_ontology,
)
from server.identity.app.namespace_rollback import handle_namespace_rollback
from server.identity.app.namespace_snapshot import (
    handle_namespace_restore,
    handle_namespace_snapshot,
)
from server.identity.app.namespace_summary import handle_namespace_summary
from server.identity.app.namespace_validate import handle_namespace_validate
from server.operations.base import Operation, Role

namespace_register = Operation(
    name="namespace.register",
    input_model=NamespaceRegisterInput,
    output_model=NamespaceRegisterOutput,
    handler=handle_namespace_register,
    idempotent=True,
    budget_p99_ms=30,
    # VIEWER can create a fresh namespace (auto-granted admin); updates to an
    # existing one require an admin grant. Handler enforces fresh-vs-update.
    required_role=Role.VIEWER,
    audit=True,
    skip_namespace_authz=True,
)


namespace_list = Operation(
    name="namespace.list",
    maturity="experimental",
    input_model=NamespaceListInput,
    output_model=NamespaceListOutput,
    handler=handle_namespace_list,
    idempotent=True,
    budget_p99_ms=80,
    # Org-wide enumeration (incl. private namespaces) — operator-only.
    # Regular users use namespace.list_mine.
    required_role=Role.ORG_ADMIN,
    skip_namespace_authz=True,
    audit=False,
)


namespace_get = Operation(
    name="namespace.get",
    input_model=NamespaceGetInput,
    output_model=NamespaceGetOutput,
    handler=handle_namespace_get,
    idempotent=True,
    budget_p99_ms=30,
    required_role=Role.VIEWER,
    audit=False,
)


namespace_delete = Operation(
    name="namespace.delete",
    maturity="experimental",
    input_model=NamespaceDeleteInput,
    output_model=NamespaceDeleteOutput,
    handler=handle_namespace_delete,
    idempotent=True,
    budget_p99_ms=60,
    required_role=Role.ADMIN,
    audit=True,
)


namespace_browse = Operation(
    name="namespace.browse",
    input_model=NamespaceBrowseInput,
    output_model=NamespaceBrowseOutput,
    handler=handle_namespace_browse,
    idempotent=True,
    budget_p99_ms=120,
    required_role=Role.VIEWER,
    audit=False,
    deployment_scoped=True,
)


namespace_join = Operation(
    name="namespace.join",
    maturity="experimental",
    input_model=NamespaceJoinInput,
    output_model=NamespaceJoinOutput,
    handler=handle_namespace_join,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=True,
    # Handler enforces visibility=public + writes the grants row.
    skip_namespace_authz=True,
)


namespace_leave = Operation(
    name="namespace.leave",
    maturity="experimental",
    input_model=NamespaceLeaveInput,
    output_model=NamespaceLeaveOutput,
    handler=handle_namespace_leave,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=True,
    # Handler enforces that the caller has a grant on the target.
    skip_namespace_authz=True,
)


namespace_cancel_request = Operation(
    name="namespace.cancel_request",
    maturity="experimental",
    input_model=NamespaceCancelRequestInput,
    output_model=NamespaceCancelRequestOutput,
    handler=handle_namespace_cancel_request,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=True,
    # Handler is self-only: it always operates on the calling subject.
    skip_namespace_authz=True,
)


namespace_list_mine = Operation(
    name="namespace.list_mine",
    input_model=NamespaceListMineInput,
    output_model=NamespaceListMineOutput,
    handler=handle_namespace_list_mine,
    idempotent=True,
    budget_p99_ms=120,
    required_role=Role.VIEWER,
    audit=False,
    # The caller's own memberships, no namespace — a scoped
    # token is gated on its best namespace role, not refused as
    # IDENTITY_INCOMPLETE.
    deployment_scoped=True,
)


namespace_mark_seen = Operation(
    name="namespace.mark_seen",
    maturity="experimental",
    input_model=NamespaceMarkSeenInput,
    output_model=NamespaceMarkSeenOutput,
    handler=handle_namespace_mark_seen,
    idempotent=True,
    budget_p99_ms=50,
    required_role=Role.VIEWER,
    writes=True,
    audit=False,
    # Touching own visit row — no namespace authz required.
    skip_namespace_authz=True,
)


namespace_snapshot = Operation(
    name="namespace.snapshot",
    maturity="experimental",
    input_model=NamespaceSnapshotInput,
    output_model=NamespaceSnapshotOutput,
    handler=handle_namespace_snapshot,
    idempotent=True,
    budget_p99_ms=2000,  # bounded by namespace size
    required_role=Role.ADMIN,
    audit=True,
)


namespace_restore = Operation(
    name="namespace.restore",
    maturity="experimental",
    input_model=NamespaceRestoreInput,
    output_model=NamespaceRestoreOutput,
    handler=handle_namespace_restore,
    idempotent=True,  # snapshot_id-keyed
    budget_p99_ms=3000,
    # VIEWER, like namespace.register: restore into a fresh target (auto-granted
    # admin); handler refuses an existing target unless caller is already admin.
    required_role=Role.VIEWER,
    audit=True,
    skip_namespace_authz=True,
)


namespace_rollback = Operation(
    name="namespace.rollback",
    maturity="experimental",
    input_model=NamespaceRollbackInput,
    output_model=NamespaceRollbackOutput,
    handler=handle_namespace_rollback,
    # A dry run is idempotent; an apply commits, and a repeat finds nothing to change.
    idempotent=False,
    budget_p99_ms=10000,  # bounded by max_keys: a journal read per 50 keys, one batch of commits
    required_role=Role.ADMIN,
    audit=True,
)


namespace_taxonomy = Operation(
    name="namespace.taxonomy",
    input_model=NamespaceTaxonomyInput,
    output_model=NamespaceTaxonomyOutput,
    handler=handle_namespace_taxonomy,
    idempotent=True,
    budget_p99_ms=120,
    required_role=Role.VIEWER,
    audit=False,
)


namespace_register_category = Operation(
    name="namespace.register_category",
    maturity="experimental",
    input_model=NamespaceRegisterCategoryInput,
    output_model=NamespaceRegisterCategoryOutput,
    handler=handle_namespace_register_category,
    idempotent=False,
    budget_p99_ms=400,
    required_role=Role.WRITER,
    audit=True,
)


namespace_list_categories = Operation(
    name="namespace.list_categories",
    maturity="experimental",
    input_model=NamespaceListCategoriesInput,
    output_model=NamespaceListCategoriesOutput,
    handler=handle_namespace_list_categories,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=False,
)


namespace_search_categories = Operation(
    name="namespace.search_categories",
    maturity="experimental",
    input_model=NamespaceSearchCategoriesInput,
    output_model=NamespaceSearchCategoriesOutput,
    handler=handle_namespace_search_categories,
    idempotent=True,
    budget_p99_ms=150,
    required_role=Role.VIEWER,
    audit=False,
)


namespace_summary = Operation(
    name="namespace.summary",
    input_model=NamespaceSummaryInput,
    output_model=NamespaceSummaryOutput,
    handler=handle_namespace_summary,
    idempotent=True,
    budget_p99_ms=300,
    required_role=Role.VIEWER,
    audit=False,
)


namespace_validate = Operation(
    name="namespace.validate",
    input_model=NamespaceValidateInput,
    output_model=NamespaceValidateOutput,
    handler=handle_namespace_validate,
    idempotent=True,
    # Ten FINAL-scoped aggregates plus two identity joins; namespace-sized,
    # not cluster-sized.
    budget_p99_ms=1500,
    required_role=Role.VIEWER,
    audit=False,
)


namespace_audit = Operation(
    name="namespace.audit",
    input_model=NamespaceAuditInput,
    output_model=NamespaceAuditOutput,
    handler=handle_namespace_audit,
    idempotent=True,
    # Three summary reads, ten FINAL-scoped aggregates over
    # the namespace and one Python scan of up to 20k (key, clipped value)
    # rows for the hand-rolled index check. Namespace-sized; the ledger
    # reads are bounded by window_seconds.
    budget_p99_ms=3000,
    required_role=Role.VIEWER,
    audit=False,
)


namespace_register_ontology = Operation(
    name="namespace.register_ontology",
    input_model=NamespaceRegisterOntologyInput,
    output_model=NamespaceRegisterOntologyOutput,
    handler=handle_namespace_register_ontology,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.ADMIN,
    audit=True,
)


namespace_list_ontology = Operation(
    name="namespace.list_ontology",
    maturity="experimental",
    input_model=NamespaceListOntologyInput,
    output_model=NamespaceListOntologyOutput,
    handler=handle_namespace_list_ontology,
    idempotent=True,
    budget_p99_ms=60,
    required_role=Role.VIEWER,
    audit=False,
)


OPERATIONS: list[Operation] = [
    namespace_register,
    namespace_list,
    namespace_get,
    namespace_delete,
    namespace_browse,
    namespace_join,
    namespace_leave,
    namespace_cancel_request,
    namespace_list_mine,
    namespace_mark_seen,
    namespace_snapshot,
    namespace_restore,
    namespace_rollback,
    namespace_taxonomy,
    namespace_register_category,
    namespace_list_categories,
    namespace_search_categories,
    namespace_summary,
    namespace_validate,
    namespace_audit,
    namespace_register_ontology,
    namespace_list_ontology,
]
