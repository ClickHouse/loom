"""The dashboard's pagers offer no page that starts past the largest `offset` an op serves,
so each constant in frontend/src/api must equal that op's `le`, or a page is a 422."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from server.knowledge.api.models_conversations import ConversationThreadsInput
from server.tasks.api.models_run import RunListInput

FRONTEND_API = Path(__file__).resolve().parents[2] / "frontend/src/api"


def _offset_le(model: type) -> int:
    for meta in model.model_fields["offset"].metadata:
        if hasattr(meta, "le"):
            return int(meta.le)
    raise AssertionError(f"{model.__name__}.offset has no upper bound")


def _constant(file: str, name: str) -> int:
    src = (FRONTEND_API / file).read_text()
    m = re.search(rf"export const {name} = ([0-9_]+);", src)
    assert m, f"{name} not found in frontend/src/api/{file}"
    return int(m.group(1).replace("_", ""))


@pytest.mark.parametrize(
    ("model", "file", "name"),
    [
        (RunListInput, "runs.ts", "RUN_LIST_MAX_OFFSET"),
        (ConversationThreadsInput, "conversations.ts", "THREADS_MAX_OFFSET"),
    ],
)
def test_the_pager_limit_is_the_ops_offset_bound(model: type, file: str, name: str) -> None:
    assert _constant(file, name) == _offset_le(model)
