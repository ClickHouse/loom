"""The extraction-policy operations the registry serves; wire models are in
models_extraction_policy.py."""

from __future__ import annotations

from server.memory.api.models_extraction_policy import (
    ExtractionPolicyClearInput,
    ExtractionPolicyGetInput,
    ExtractionPolicyOutput,
    ExtractionPolicySetInput,
)
from server.memory.app.extraction_policy import (
    handle_extraction_policy_clear,
    handle_extraction_policy_get,
    handle_extraction_policy_set,
)
from server.operations.base import Operation, Role

# The org admin of the org named, for the org's policy and for a namespace's
# alike: a namespace admin (or a writer, or an end user) neither sets nor clears
# the rules its own extractions follow.
extraction_policy_set = Operation(
    name="extraction_policy.set",
    maturity="experimental",
    input_model=ExtractionPolicySetInput,
    output_model=ExtractionPolicyOutput,
    handler=handle_extraction_policy_set,
    idempotent=True,
    required_role=Role.ORG_ADMIN,
    audit=True,
    skip_namespace_authz=True,
)

extraction_policy_get = Operation(
    name="extraction_policy.get",
    maturity="experimental",
    input_model=ExtractionPolicyGetInput,
    output_model=ExtractionPolicyOutput,
    handler=handle_extraction_policy_get,
    idempotent=True,
    required_role=Role.ORG_ADMIN,
    audit=False,
    skip_namespace_authz=True,
)

extraction_policy_clear = Operation(
    name="extraction_policy.clear",
    maturity="experimental",
    input_model=ExtractionPolicyClearInput,
    output_model=ExtractionPolicyOutput,
    handler=handle_extraction_policy_clear,
    idempotent=True,
    required_role=Role.ORG_ADMIN,
    audit=True,
    skip_namespace_authz=True,
)


OPERATIONS: list[Operation] = [
    extraction_policy_set,
    extraction_policy_get,
    extraction_policy_clear,
]
