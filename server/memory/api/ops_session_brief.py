"""Operations over the wire models in `server.memory.api.models_session_brief`."""

from __future__ import annotations

from server.memory.api.models_session_brief import (
    SessionBriefDeleteInput,
    SessionBriefDeleteOutput,
    SessionBriefGetInput,
    SessionBriefGetOutput,
    SessionBriefListInput,
    SessionBriefListOutput,
    SessionBriefSaveInput,
    SessionBriefSaveOutput,
)
from server.memory.app.session_brief import (
    handle_session_brief_delete,
    handle_session_brief_get,
    handle_session_brief_list,
    handle_session_brief_save,
)
from server.operations.base import Operation, Role

session_brief_save = Operation(
    name="session.brief_save",
    maturity="experimental",
    input_model=SessionBriefSaveInput,
    output_model=SessionBriefSaveOutput,
    handler=handle_session_brief_save,
    # A retry with the same session + cursor is a no-op; a new window is a
    # new revision.
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)

session_brief_get = Operation(
    name="session.brief_get",
    maturity="experimental",
    input_model=SessionBriefGetInput,
    output_model=SessionBriefGetOutput,
    handler=handle_session_brief_get,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

session_brief_list = Operation(
    name="session.brief_list",
    maturity="experimental",
    input_model=SessionBriefListInput,
    output_model=SessionBriefListOutput,
    handler=handle_session_brief_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

session_brief_delete = Operation(
    name="session.brief_delete",
    maturity="experimental",
    input_model=SessionBriefDeleteInput,
    output_model=SessionBriefDeleteOutput,
    handler=handle_session_brief_delete,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)


OPERATIONS: list[Operation] = [
    session_brief_save,
    session_brief_get,
    session_brief_list,
    session_brief_delete,
]
