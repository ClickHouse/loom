from __future__ import annotations

import pytest

from server.code.domain.index.remote import parse_remote, repo_key


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
        "acme/.git",
        "acme/.GIT",
        "https://github.com/acme/.GIT",
        "https://github.com/acme/./widget",
        "https://github.com/acme/%2e/widget",
        "https://github.com/acme/x/%2E%2E/widget",
        "https://github.com/acme//widget",
        "https://\uff47ithub.com/acme/widget",
        "git@\uff47ithub.com:acme/widget.git",
        "https://github%2ecom/acme/widget",
    ],
)
def test_parse_remote_refuses_unsafe_or_local_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_remote(value)


def test_an_absolute_scp_path_is_kept_as_given() -> None:
    got = parse_remote("git@git.example.com:/srv/team/widget.git")
    assert got.url == "git@git.example.com:/srv/team/widget.git"
    assert got.repo == "git.example.com/srv/team/widget"


def test_a_www_github_remote_parses_as_before() -> None:
    """Rows stored with this spelling keep their provider, slug and mirror."""
    from tests.unit.code_fixtures import repo_config

    got = parse_remote("https://www.github.com/acme/widget")
    assert (got.repo, got.url, got.provider) == (
        "www.github.com/acme/widget",
        "https://www.github.com/acme/widget.git",
        "git",
    )
    cfg = repo_config(repo=got.repo, git_remote=got.url)
    assert cfg.provider == "git"  # no GitHub API call with a www slug


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/acme/widget.git",
        "https://github.com/Acme/Widget",
        "https://www.github.com/acme/widget/",
        "https://github.com./acme/widget.GIT",
        "https://github.com:443/acme/widget",
        "ssh://git@ssh.github.com:443/acme/widget.git",
        "ssh://git@github.com:22/acme/widget",
        "git@github.com:acme/widget.git",
        "git@www.github.com:/acme/widget",
        "https://github.com/acme/wid%67et",
        "https://github.com/acme/./widget",
        "https://github.com/acme/%2e/widget",
        "https://github.com/acme/other/%2e%2e/widget",
        "https://github.com/acme//widget",
        "https://\uff47ithub.com/acme/widget",  # fullwidth g
        "https://\uff47ithub.com./acme/widget",  # and a trailing dot
    ],
)
def test_every_spelling_of_a_github_remote_has_one_key(remote: str) -> None:
    assert repo_key("", remote) == repo_key("acme/widget", "") == "github.com/acme/widget"


def test_keys_tell_repositories_and_hosts_apart() -> None:
    assert repo_key("acme/widget", "") != repo_key("other/widget", "")
    assert repo_key("group/tool", "https://gitlab.com/group/tool.git") == "gitlab.com/group/tool"
    assert repo_key("group/tool", "") != repo_key("", "https://gitlab.com/group/tool")
    # every host's path is compared without case
    assert repo_key("", "https://git.example.com/Team/Widget") == "git.example.com/team/widget"
    # a non-default port tells two servers on one host apart; 80, 443 and 22 do not
    assert repo_key("", "https://git.example.com:8443/t/w") == "git.example.com:8443/t/w"
    assert repo_key("", "http://git.example.com:80/t/w") == "git.example.com/t/w"
    # a local path or file:// mirror leaves the entry's repo to decide
    assert repo_key("acme/widget", "/srv/mirrors/widget.git") == "github.com/acme/widget"
    assert repo_key("acme/widget", "file:///srv/mirrors/widget.git") == "github.com/acme/widget"
    mirrored = repo_key("git.example.com/tool", "/srv/mirrors/tool.git")
    assert mirrored == repo_key("", "https://git.example.com/tool.git") == "git.example.com/tool"
    # a host taken from the entry's repo folds as a remote's host does
    assert repo_key("www.github.com/acme/widget", "/srv/m.git") == "github.com/acme/widget"
    assert repo_key("GitLab.example.com./g/t", "/srv/m.git") == "gitlab.example.com/g/t"
    assert repo_key("github.com:443/acme/widget", "/srv/m.git") == "github.com/acme/widget"
    assert repo_key("git.example.com:8443/t", "/srv/m.git") == "git.example.com:8443/t"
    # the other hosts' ssh-over-443 names and any port
    assert repo_key("", "ssh://git@altssh.gitlab.com:443/g/t.git") == "gitlab.com/g/t"
    assert repo_key("", "ssh://git@altssh.bitbucket.org:443/a/w") == "bitbucket.org/a/w"
    assert repo_key("", "https://www.gitlab.com/g/t") == "gitlab.com/g/t"
    assert repo_key("", "https://www.bitbucket.org/a/w.git") == "bitbucket.org/a/w"
    assert repo_key("", "https://bitbucket.org:8443/a/w") == "bitbucket.org/a/w"
