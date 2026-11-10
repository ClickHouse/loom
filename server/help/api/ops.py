"""The `help` operations the registry serves; wire models are in models.py."""

from __future__ import annotations

from server.help.api.models import HelpGetInput, HelpGetOutput, HelpSearchInput, HelpSearchOutput
from server.help.app.docs import handle_help_get, handle_help_search
from server.operations.base import Operation, Role

# The same text for every caller: no namespace is read, so none is authorized.
help_search = Operation(
    name="help.search",
    maturity="experimental",
    input_model=HelpSearchInput,
    output_model=HelpSearchOutput,
    handler=handle_help_search,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
    skip_namespace_authz=True,
    deployment_scoped=True,
)

help_get = Operation(
    name="help.get",
    maturity="experimental",
    input_model=HelpGetInput,
    output_model=HelpGetOutput,
    handler=handle_help_get,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
    skip_namespace_authz=True,
    deployment_scoped=True,
)

OPERATIONS: list[Operation] = [help_search, help_get]
