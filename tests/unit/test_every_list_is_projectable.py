"""Every list is projectable.

Six list operations the dashboard reads gain `fields`, resolved once and
stamped on rows that extend `ProjectedRow`; unset, a page is byte-identical.
The ratchet: every list-shaped input that takes `limit` either takes
`fields` or is named below with a reason — and the list can only shrink."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from server.code.api import models_repos as code
from server.core.errors import ValidationError
from server.identity.api import models_grants
from server.insights.api import models_audit
from server.knowledge.api import models_conversations as conversations
from server.memory.api import models_conflicts as conflicts
from server.operations import read_projection
from server.tasks.api import models_run as run

_SERVER = Path(__file__).resolve().parents[2] / "server"


def _wire_model_files() -> list[Path]:
    """The registry core's modules and every domain package's api modules."""
    return sorted({*(_SERVER / "operations").rglob("*.py"), *_SERVER.glob("*/api/**/*.py")})


# List-shaped inputs that take `limit` but not yet `fields`. A new list
# operation ships with `fields`; an entry here needs a reason. Remove a line
# when its operation gains the projection; never add one without a reason.
UNPROJECTED: dict[str, str] = {
    "HelpSearchInput": "at most ten short hits of the shipped docs, read whole",
    "CollectionListInput": "four short fields per collection; a page is read whole",
    "BlacklistListInput": "a short admin list; no page reads it in part",
    "CodeHistoryInput": "a per-file history read whole",
    "CodeSymbolHistoryInput": "a per-symbol history read whole",
    "ConflictsAllowlistListInput": "a short admin list",
    "ConversationListInput": "messages carry content; a caller reads them whole",
    "ErasureListInput": "an admin history list read whole",
    "FactsListInput": "graph rows read whole by the explorer",
    "KnowledgeDedupListInput": "an admin list read whole",
    "LearningsListInput": "SDK-only",
    "MemoryDedupListInput": "an admin list read whole",
    "MemoryRecordHistoryInput": "a history read whole",
    "MemorySchemaListInput": "a short schema list",
    "NamespaceGuideHistoryInput": "six short fields per revision, never the guide's text",
    "NamespaceListInput": "the directory; every column is shown",
    "ProcedureListInput": "SDK-only",
    "ProjectionFailureListInput": "an ops list read whole",
    "SearchTraceLogInput": "a diagnostic list read whole",
}

PROJECTED = {
    "AuditListInput",
    "RunListInput",
    "ConversationThreadsInput",
    "ConflictsListInput",
    "GrantsListInput",
    "CodeReposInput",
    "KnowledgeListInput",
}


def _list_inputs() -> dict[str, str]:
    """{class name: body} for every list-shaped input model on the wire."""
    out: dict[str, str] = {}
    pat = re.compile(
        r"^class (\w*(?:List|Threads|Repos|History|Search)\w*Input)\((?:BaseModel|_CodeIdentity)\):\n"
        r"((?:    .*\n|\n)*?)(?=^class |^[a-z_]+ = |\Z)",
        re.M,
    )
    for f in _wire_model_files():
        for m in pat.finditer(f.read_text()):
            out[m.group(1)] = m.group(2)
    return out


def test_every_list_with_a_limit_takes_fields_or_is_named() -> None:
    inputs = _list_inputs()
    with_limit = {n for n, body in inputs.items() if re.search(r"^\s+limit: ", body, re.M)}
    assert PROJECTED <= set(inputs), sorted(PROJECTED - set(inputs))
    for name in sorted(with_limit):
        has = "fields: list[str] | None" in inputs[name]
        if name in PROJECTED:
            assert has, f"{name} lost its fields projection"
        elif not has:
            assert name in UNPROJECTED, (
                f"{name} takes limit but not fields: give it the projection "
                "(ProjectedRow + project_rows) or name it in UNPROJECTED with a reason"
            )
    stale = set(UNPROJECTED) - (
        with_limit - {n for n in with_limit if "fields: list[str] | None" in inputs[n]}
    )
    assert not stale, f"projected now — remove from UNPROJECTED: {sorted(stale)}"


def test_the_six_rows_share_the_projected_base() -> None:
    for model in (
        models_audit.AuditListRow,
        run.RunRow,
        conversations.ConversationThreadSummary,
        conflicts._ConflictRow,
        models_grants._GrantEntry,
        code.CodeRepoSummary,
    ):
        assert issubclass(model, read_projection.ProjectedRow), model.__name__


def test_project_rows_stamps_the_page_and_leaves_it_alone_when_unset() -> None:
    rows = [
        models_grants._GrantEntry(
            subject="u", role="viewer", granted_at="2026-09-20T00:00:00Z", granted_by="a"
        )
        for _ in range(3)
    ]
    read_projection.project_rows(
        rows, models_grants._GrantEntry, None, always=frozenset({"subject"})
    )
    assert "granted_by" in rows[0].model_dump()
    read_projection.project_rows(
        rows, models_grants._GrantEntry, ["role"], always=frozenset({"subject"})
    )
    assert all(r.model_dump() == {"subject": "u", "role": "viewer"} for r in rows)
    with pytest.raises(ValidationError):
        read_projection.project_rows(
            rows, models_grants._GrantEntry, ["nope"], always=frozenset({"subject"})
        )


def test_the_six_inputs_take_fields() -> None:
    for model in (
        models_audit.AuditListInput,
        run.RunListInput,
        conversations.ConversationThreadsInput,
        conflicts.ConflictsListInput,
        models_grants.GrantsListInput,
        code.CodeReposInput,
    ):
        assert isinstance(model, type) and issubclass(model, BaseModel)
        assert "fields" in model.model_fields, model.__name__
        sig = inspect.signature(model)
        assert sig is not None
