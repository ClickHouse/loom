"""tracker.* operation declarations: names, models, handlers and budgets."""

from __future__ import annotations

from server.operations.base import Operation, Role
from server.tracker.api.models import (
    TrackerAnswerInput,
    TrackerAnswerOutput,
    TrackerBindingInput,
    TrackerBindingOutput,
    TrackerComponentInput,
    TrackerComponentOutput,
    TrackerContractInput,
    TrackerContractOutput,
    TrackerDuplicateCheckInput,
    TrackerDuplicateCheckOutput,
    TrackerFlakyInput,
    TrackerFlakyOutput,
    TrackerItemInput,
    TrackerItemOutput,
    TrackerLinksInput,
    TrackerLinksOutput,
    TrackerReleaseInput,
    TrackerReleaseOutput,
    TrackerSimilarInput,
    TrackerSimilarOutput,
    TrackerSourceAddInput,
    TrackerSourceAddOutput,
    TrackerSourceCheckInput,
    TrackerSourceCheckOutput,
    TrackerSourceInspectInput,
    TrackerSourceInspectOutput,
    TrackerSourceListInput,
    TrackerSourceListOutput,
    TrackerSourceRemoveInput,
    TrackerSourceRemoveOutput,
    TrackerSourceRotateInput,
    TrackerSourceRotateOutput,
    TrackerSourceSetMappingInput,
    TrackerSourceSetMappingOutput,
    TrackerTestSignalInput,
    TrackerTestSignalOutput,
)
from server.tracker.app.dupcheck import handle_tracker_duplicate_check
from server.tracker.app.handlers import (
    handle_tracker_answer,
    handle_tracker_binding,
    handle_tracker_component,
    handle_tracker_flaky,
    handle_tracker_item,
    handle_tracker_links,
    handle_tracker_release,
)
from server.tracker.app.similar import handle_tracker_similar
from server.tracker.app.sources import (
    handle_tracker_contract,
    handle_tracker_source_add,
    handle_tracker_source_check,
    handle_tracker_source_inspect,
    handle_tracker_source_list,
    handle_tracker_source_remove,
    handle_tracker_source_rotate,
    handle_tracker_source_set_mapping,
    source_add_audit_subject,
    source_rotate_audit_subject,
)
from server.tracker.app.test_signal import handle_tracker_test_signal

tracker_item = Operation(
    name="tracker.item",
    input_model=TrackerItemInput,
    output_model=TrackerItemOutput,
    handler=handle_tracker_item,
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)
tracker_release = Operation(
    name="tracker.release",
    input_model=TrackerReleaseInput,
    output_model=TrackerReleaseOutput,
    handler=handle_tracker_release,
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)
tracker_component = Operation(
    name="tracker.component",
    input_model=TrackerComponentInput,
    output_model=TrackerComponentOutput,
    handler=handle_tracker_component,
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)
tracker_flaky = Operation(
    name="tracker.flaky",
    input_model=TrackerFlakyInput,
    output_model=TrackerFlakyOutput,
    handler=handle_tracker_flaky,
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)

# the history read scans the remote's whole window (1.5–3.7 s live for 30
# days of a hot test) and the co-failure read follows it
tracker_test_signal = Operation(
    name="tracker.test_signal",
    input_model=TrackerTestSignalInput,
    output_model=TrackerTestSignalOutput,
    handler=handle_tracker_test_signal,
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)

tracker_answer = Operation(
    name="tracker.answer",
    input_model=TrackerAnswerInput,
    output_model=TrackerAnswerOutput,
    handler=handle_tracker_answer,
    # up to five templates gathered, each a bounded 300 ms read
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)

# settings and the gate only — no upstream read
tracker_binding = Operation(
    name="tracker.binding",
    input_model=TrackerBindingInput,
    output_model=TrackerBindingOutput,
    handler=handle_tracker_binding,
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)

tracker_similar = Operation(
    name="tracker.similar",
    input_model=TrackerSimilarInput,
    output_model=TrackerSimilarOutput,
    handler=handle_tracker_similar,
    # the item read, then the legs side by side, each a bounded text read
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)

tracker_duplicate_check = Operation(
    name="tracker.duplicate_check",
    input_model=TrackerDuplicateCheckInput,
    output_model=TrackerDuplicateCheckOutput,
    handler=handle_tracker_duplicate_check,
    # tracker.similar's legs side by side (600 ms) plus one state read;
    # a `related` verdict (~7% of calls) adds one small-model judge call of
    # ~1.3 s — the budget covers the judged path.
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)

tracker_links = Operation(
    name="tracker.links",
    input_model=TrackerLinksInput,
    output_model=TrackerLinksOutput,
    handler=handle_tracker_links,
    # a point read on loom's own table beside one bounded QA edge read
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)

# Org-level (no namespace to bind); the handler checks the
# caller belongs to the org. Two directory reads, one cached contract verdict
# and two cached count reads per source.
tracker_source_list = Operation(
    name="tracker.source.list",
    input_model=TrackerSourceListInput,
    output_model=TrackerSourceListOutput,
    handler=handle_tracker_source_list,
    idempotent=True,
    required_role=Role.VIEWER,
    skip_namespace_authz=True,
    # A scoped (ns_roles) token has no single namespace to
    # bind an org-level read to; the gate admits it by its best namespace
    # role, the handler checks the org
    deployment_scoped=True,
    maturity="experimental",
)

# Self-service registration. Org-level like code.repo.add; the
# handler checks the org, the host, the credential's grants and the contract.
tracker_source_add = Operation(
    name="tracker.source.add",
    input_model=TrackerSourceAddInput,
    output_model=TrackerSourceAddOutput,
    handler=handle_tracker_source_add,
    idempotent=False,
    required_role=Role.VIEWER,
    skip_namespace_authz=True,
    audit=True,
    audit_subject=source_add_audit_subject,
    maturity="experimental",
)
# Connect and describe — the gates of add, nothing written.
tracker_source_inspect = Operation(
    name="tracker.source.inspect",
    input_model=TrackerSourceInspectInput,
    output_model=TrackerSourceInspectOutput,
    handler=handle_tracker_source_inspect,
    idempotent=True,
    required_role=Role.VIEWER,
    skip_namespace_authz=True,
    maturity="experimental",
)
# An admin of every namespace of the source (checked in the handler).
tracker_source_set_mapping = Operation(
    name="tracker.source.set_mapping",
    input_model=TrackerSourceSetMappingInput,
    output_model=TrackerSourceSetMappingOutput,
    handler=handle_tracker_source_set_mapping,
    idempotent=True,
    required_role=Role.VIEWER,
    skip_namespace_authz=True,
    audit=True,
    maturity="experimental",
)
# Pure — the contract module's own tables, rendered.
tracker_contract = Operation(
    name="tracker.contract",
    input_model=TrackerContractInput,
    output_model=TrackerContractOutput,
    handler=handle_tracker_contract,
    idempotent=True,
    required_role=Role.VIEWER,
    skip_namespace_authz=True,
    # The contract is a fact of the deployment — any token
    # that reads anywhere may read it, scoped or not
    deployment_scoped=True,
    maturity="experimental",
)
tracker_source_check = Operation(
    name="tracker.source.check",
    input_model=TrackerSourceCheckInput,
    output_model=TrackerSourceCheckOutput,
    handler=handle_tracker_source_check,
    idempotent=True,
    required_role=Role.VIEWER,
    skip_namespace_authz=True,
    # A read; the gate admits a scoped token by its best role.
    # The writes (add/rotate/remove) stay closed to scoped tokens: a token
    # delegated for one namespace does not register or retire an org's tracker
    deployment_scoped=True,
    maturity="experimental",
)
# rotate / remove: the handler requires admin on every namespace of the source
# (`sources._authority`); an org-level op has no namespace for the transport gate
tracker_source_rotate = Operation(
    name="tracker.source.rotate",
    input_model=TrackerSourceRotateInput,
    output_model=TrackerSourceRotateOutput,
    handler=handle_tracker_source_rotate,
    idempotent=False,
    required_role=Role.VIEWER,
    skip_namespace_authz=True,
    audit=True,
    audit_subject=source_rotate_audit_subject,
    maturity="experimental",
)
tracker_source_remove = Operation(
    name="tracker.source.remove",
    input_model=TrackerSourceRemoveInput,
    output_model=TrackerSourceRemoveOutput,
    handler=handle_tracker_source_remove,
    idempotent=True,
    required_role=Role.VIEWER,
    skip_namespace_authz=True,
    audit=True,
    maturity="experimental",
)

OPERATIONS: list[Operation] = [
    tracker_item,
    tracker_release,
    tracker_component,
    tracker_flaky,
    tracker_test_signal,
    tracker_answer,
    tracker_similar,
    tracker_links,
    tracker_binding,
    tracker_source_list,
    tracker_contract,
    tracker_source_inspect,
    tracker_source_set_mapping,
    tracker_source_add,
    tracker_source_check,
    tracker_source_rotate,
    tracker_source_remove,
    tracker_duplicate_check,
]
