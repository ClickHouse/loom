"""A memory excerpt attached to a code result or symbol definition."""

from pydantic import TypeAdapter
from typing_extensions import TypedDict

from server.memory.domain.use_guidance import UseGuidance


class CodeLesson(TypedDict, total=False):
    memory_key: str
    namespace: str
    excerpt: str
    code_state: str
    path: str
    changed_since: str
    supersedes_key: str
    suggestion: str
    owner_symbol: str
    linked_symbols: list[str]
    use_guidance: UseGuidance


code_lessons = TypeAdapter(list[CodeLesson])
