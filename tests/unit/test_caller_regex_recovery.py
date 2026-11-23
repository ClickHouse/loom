"""Invalid code-shaped regexes give callers a concrete syntax correction."""

import pytest

from server.core.caller_regex import compile_caller_regex
from server.core.errors import ValidationError


@pytest.mark.parametrize(
    "pattern",
    [
        "reversed(request.blueprints)|chain(request.blueprints|request.blueprints",
        "reversed(request.blueprints)|chain([None], reversed",
    ],
)
def test_unescaped_code_punctuation_explains_recovery(pattern: str) -> None:
    with pytest.raises(ValidationError) as error:
        compile_caller_regex(pattern)
    detail = error.value.to_detail()
    assert detail["code"] == "VALIDATION_FAILED"
    assert "not a valid RE2" in detail["message"]
    assert r"\( matches a literal opening parenthesis" in detail["message"]
    assert "no backreferences or lookaround" in detail["message"]


def test_escaped_code_alternatives_keep_regex_semantics() -> None:
    pattern = compile_caller_regex(r"reversed\(request\.blueprints\)|chain\(\[None\], reversed")
    assert pattern.search("for bp in reversed(request.blueprints):")
    assert pattern.search("for bp in chain([None], reversed(request.blueprints)):")
    assert not pattern.search("for bp in request.blueprints:")
