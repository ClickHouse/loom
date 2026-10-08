"""A page's field list names only fields the
server's row model has — an unknown name is a 422 and a broken page (a key
of a TypeScript interface the server row does not have, say). Every page constant is checked here against its model."""

from __future__ import annotations

import json
import re
from pathlib import Path

from server.code.api import models_repos as code
from server.insights.api import models_audit
from server.knowledge.api import models_conversations as conversations
from server.knowledge.api import models_knowledge as knowledge
from server.memory.api import models_crud as memory_crud
from server.tasks.api import models_run as run

_ROOT = Path(__file__).resolve().parents[2]


def _const(path: str, name: str) -> set[str]:
    src = (_ROOT / path).read_text()
    i = src.index(f"{name} = [")
    body = src[i + len(f"{name} = [") : src.index("];", i)]
    body = re.sub(r"//[^\n]*", "", body)
    return set(json.loads("[" + body.strip().rstrip(",") + "]"))


def test_every_page_field_list_is_a_subset_of_its_row_model() -> None:
    checks = [
        ("frontend/src/api.ts", "CODE_REPOS_FIELDS", code.CodeRepoSummary),
        ("frontend/src/views/admin/Audit.tsx", "ACTIVITY_FIELDS", models_audit.AuditListRow),
        ("frontend/src/views/Runs.tsx", "RUNS_FIELDS", run.RunRow),
        (
            "frontend/src/views/Conversations.tsx",
            "THREADS_FIELDS",
            conversations.ConversationThreadSummary,
        ),
        ("frontend/src/views/Documents.tsx", "LIBRARY_LIST_FIELDS", knowledge._KnowledgeListEntry),
        ("frontend/src/views/Recent.tsx", "MEMORIES_LIST_FIELDS", memory_crud._MemoryListEntry),
    ]
    for path, name, model in checks:
        fields = _const(path, name)
        known = set(model.model_fields)
        assert fields <= known, (
            f"{name} names fields {sorted(fields - known)} that {model.__name__} does not have"
        )
        assert fields, name
