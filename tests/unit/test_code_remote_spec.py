from __future__ import annotations

import pytest

from server.code.domain.index.remote import parse_remote


@pytest.mark.parametrize(
    ("value", "repo", "url", "provider"),
    [
        ("acme/widget", "acme/widget", "https://github.com/acme/widget.git", "github"),
        (
            "https://github.com/acme/widget.git",
            "acme/widget",
            "https://github.com/acme/widget.git",
            "github",
        ),
        (
            "https://gitlab.com/acme/platform/widget",
            "gitlab.com/acme/platform/widget",
            "https://gitlab.com/acme/platform/widget.git",
            "gitlab",
        ),
        (
            "git@bitbucket.org:acme/widget.git",
            "bitbucket.org/acme/widget",
            "git@bitbucket.org:acme/widget.git",
            "bitbucket",
        ),
        (
            "ssh://git@git.example.com:2222/team/widget",
            "git.example.com/team/widget",
            "ssh://git@git.example.com:2222/team/widget.git",
            "git",
        ),
    ],
)
def test_parse_remote(value: str, repo: str, url: str, provider: str) -> None:
    got = parse_remote(value)
    assert (got.repo, got.url, got.provider) == (repo, url, provider)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "./repo",
        "/srv/repo",
        "file:///srv/repo",
        "https://token@example.com/a/b",
        "https://x/a/../b",
    ],
)
def test_parse_remote_refuses_unsafe_or_local_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_remote(value)
