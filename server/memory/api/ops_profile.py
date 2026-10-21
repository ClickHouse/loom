"""Operations over the wire models in `server.memory.api.models_profile`."""

from __future__ import annotations

from server.memory.api.models_profile import (
    ProfileActivityInput,
    ProfileActivityOutput,
    ProfileAppendInput,
    ProfileMutationOutput,
    ProfileRenderInput,
    ProfileRenderOutput,
    ProfileReplaceInput,
    ProfileSchemaInput,
    ProfileSchemaOutput,
    ProfileSchemaSetInput,
    ProfileSetInput,
    ProfileStructuredInput,
    ProfileStructuredOutput,
)
from server.memory.app.profile import (
    handle_profile_activity,
    handle_profile_append,
    handle_profile_render,
    handle_profile_replace,
    handle_profile_set,
)
from server.memory.app.profile_schema import (
    handle_profile_schema_clear,
    handle_profile_schema_get,
    handle_profile_schema_set,
    handle_profile_structured,
)
from server.operations.base import Operation, Role

# --------------------------------------------------------------------
# Operation registry
# --------------------------------------------------------------------


profile_set = Operation(
    name="profile.set",
    maturity="experimental",
    input_model=ProfileSetInput,
    output_model=ProfileMutationOutput,
    handler=handle_profile_set,
    idempotent=True,  # idempotent on (identity, label)
    required_role=Role.WRITER,
    audit=False,
)

profile_append = Operation(
    name="profile.append",
    maturity="experimental",
    input_model=ProfileAppendInput,
    output_model=ProfileMutationOutput,
    handler=handle_profile_append,
    # Not idempotent: a retry appends the addition twice.
    idempotent=False,
    required_role=Role.WRITER,
    audit=False,
)

profile_replace = Operation(
    name="profile.replace",
    maturity="experimental",
    input_model=ProfileReplaceInput,
    output_model=ProfileMutationOutput,
    handler=handle_profile_replace,
    # Idempotent at the (old, new) level: replaying the same op against
    # the post-replace section is a no-op (old isn't there anymore →
    # SUBSTRING_NOT_FOUND, caller treats as "already done").
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,
)

profile_render = Operation(
    name="profile.render",
    input_model=ProfileRenderInput,
    output_model=ProfileRenderOutput,
    handler=handle_profile_render,
    idempotent=True,  # pure read
    required_role=Role.VIEWER,
    audit=False,
)

profile_activity = Operation(
    name="profile.activity",
    input_model=ProfileActivityInput,
    output_model=ProfileActivityOutput,
    handler=handle_profile_activity,
    idempotent=True,  # pure read
    required_role=Role.VIEWER,
    audit=False,
)

profile_schema_set = Operation(
    name="profile_schema.set",
    maturity="experimental",
    input_model=ProfileSchemaSetInput,
    output_model=ProfileSchemaOutput,
    handler=handle_profile_schema_set,
    idempotent=True,  # the same schema again keeps its hash, so no profile goes stale
    required_role=Role.ADMIN,
    audit=True,
)

profile_schema_get = Operation(
    name="profile_schema.get",
    maturity="experimental",
    input_model=ProfileSchemaInput,
    output_model=ProfileSchemaOutput,
    handler=handle_profile_schema_get,
    idempotent=True,  # pure read
    required_role=Role.VIEWER,
    audit=False,
)

profile_schema_clear = Operation(
    name="profile_schema.clear",
    maturity="experimental",
    input_model=ProfileSchemaInput,
    output_model=ProfileSchemaOutput,
    handler=handle_profile_schema_clear,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)

profile_structured = Operation(
    name="profile.structured",
    maturity="experimental",
    input_model=ProfileStructuredInput,
    output_model=ProfileStructuredOutput,
    handler=handle_profile_structured,
    idempotent=True,  # pure read, no model call
    required_role=Role.VIEWER,
    audit=False,
)


OPERATIONS: list[Operation] = [
    profile_set,
    profile_append,
    profile_replace,
    profile_render,
    profile_activity,
    profile_structured,
    profile_schema_set,
    profile_schema_get,
    profile_schema_clear,
]
