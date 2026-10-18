"""The repository ops take one token field, `access_token`; a `github_token` a
client still sends is served and named in `ignored_args`, never used."""

from __future__ import annotations

import pytest

from server.code.api.models_repos import CodeRepoAddInput, CodeRepoRemoteBranchesInput
from server.transport.arg_check import unknown_args


@pytest.mark.parametrize("model", [CodeRepoAddInput, CodeRepoRemoteBranchesInput])
def test_github_token_is_ignored_and_reported(model) -> None:
    args = {"org": "o", "repo": "owner/name", "github_token": "t"}
    inp = model.model_validate(args)
    assert inp.access_token == ""
    assert "github_token" not in inp.model_dump()
    assert unknown_args(model, args) == ["github_token"]
