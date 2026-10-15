from server.memory.api.models_crud import MemorySetInput
from server.memory.app.write import _stated


def _input(**kw: object) -> MemorySetInput:
    return MemorySetInput(org="o", namespace="n", agent="a", memory_key="k", value="v", **kw)


def test_only_a_field_that_changes_something_is_stated() -> None:
    assert _stated(_input()) == frozenset()
    # "detect", "keep" and "keep": nothing stated.
    assert _stated(_input(sensitivity="", ttl_seconds=None, clear_expiry=False)) == frozenset()
    assert _stated(_input(sensitivity="high")) == {"sensitivity"}
    assert _stated(_input(ttl_seconds=60)) == {"expires_at"}
    assert _stated(_input(clear_expiry=True)) == {"expires_at"}
    assert _stated(_input(pinned=False)) == {"pinned"}
