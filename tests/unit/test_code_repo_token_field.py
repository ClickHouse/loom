"""The repository ops take one token field, `access_token`; a `github_token` a
client still sends is served and named in `ignored_args`, never used."""

from __future__ import annotations

import pytest

from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import arg_check


@pytest.mark.parametrize("name", ["code.repo.add", "code.repo.remote_branches"])
def test_github_token_is_served_ignored_and_reported(name: str) -> None:
    op = OPERATIONS_BY_NAME[name]
    args = {"org": "o", "repo": "owner/name", "github_token": "t"}
    inp = op.input_model.model_validate(args)
    assert inp.access_token == ""
    assert "github_token" not in inp.model_dump()
    assert arg_check.named_argument_error(op, args) is None
    assert arg_check.ignored_block(op, args) == {"ignored_args": ["github_token"]}
