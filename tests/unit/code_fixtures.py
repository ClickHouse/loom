"""Test builders for code-plane configuration."""

from __future__ import annotations

from typing import Any

from server.code.domain.index.repos import RepoConfig


def git_config(env: dict[str, str]) -> dict[str, str]:
    """The `-c key=value` pairs an environment hands git (`GIT_CONFIG_KEY_i`)."""
    n = int(env.get("GIT_CONFIG_COUNT", "0"))
    return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(n)}


def repo_config(**overrides: Any) -> RepoConfig:
    """A `RepoConfig` the way the scalar settings build one: the bare clone and the
    checkout come from `code_git_dir` / `code_scip_checkout_dir` as they stand when it
    is built, every other field from the keyword arguments or the dataclass default."""
    from server.settings import settings

    fields: dict[str, Any] = {
        "org": "o",
        "namespace": "code-x",
        "repo": "Org/Repo",
        "git_dir": settings.code_git_dir,
        "git_remote": "",
        "git_branch": "",
        "path_prefixes": (),
        "github_token": "",
        "scip_checkout_dir": settings.code_scip_checkout_dir,
    }
    fields.update(overrides)
    return RepoConfig(**fields)
