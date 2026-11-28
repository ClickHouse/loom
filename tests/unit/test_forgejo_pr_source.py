"""Merged-PR history from a Forgejo host.

An entry whose `forge` is `forgejo` reads its pull requests from the host's REST API;
the client maps them onto the GitHub client's `MergedPR`, so the worker writes the same
`code_pr_history` rows from either source. The responses replayed here are recorded ones
(`fixtures/forgejo_pull_requests.json`). CH-free.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from server.clients import forgejo
from server.clients.github import GitHubError, RateLimited
from server.code.domain.index import repos as r
from server.code.domain.index.remote import forgejo_api_repo, pr_web_url
from server.settings import settings
from tests.unit.code_fixtures import repo_config

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "forgejo_pull_requests.json").read_text()
)
API = "https://forgejo.example.org/api/v1/repos/acme/widgets"
REMOTE = "https://forgejo.example.org/acme/widgets.git"


# ---- choosing the source -----------------------------------------------------------


def test_pr_source_is_github_for_github_and_forgejo_only_when_the_entry_says_so() -> None:
    assert repo_config(repo="acme/widgets").pr_source == "github"
    assert repo_config(git_remote="https://github.com/acme/widgets.git").pr_source == "github"
    assert repo_config(git_remote=REMOTE).pr_source == ""
    assert repo_config(git_remote=REMOTE, forge="forgejo").pr_source == "forgejo"


def test_entry_forge_key_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        {
            "org": "o",
            "namespace": "code-a",
            "repo": "forgejo.example.org/acme/widgets",
            "git_remote": REMOTE,
            "git_branch": "main",
            "forge": " Forgejo ",
        },
        {"org": "o", "namespace": "code-b", "repo": "acme/other", "git_branch": "main"},
    ]
    monkeypatch.setattr(settings, "code_repos_json", json.dumps(entries))
    forge, plain = r.configured_repos()
    assert (forge.forge, forge.pr_source) == ("forgejo", "forgejo")
    assert (plain.forge, plain.pr_source) == ("", "github")


def test_a_forgejo_entry_never_inherits_the_deployment_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_github_token", "deploy-token")
    host = {"org": "o", "repo": "forgejo.example.org/acme/widgets", "git_remote": REMOTE}
    entries = [
        {**host, "namespace": "code-a", "git_branch": "main", "forge": "forgejo"},
        {**host, "namespace": "code-b", "forge": "forgejo", "github_token": "host-token"},
        {"org": "o", "namespace": "code-c", "repo": "acme/other", "git_branch": "main"},
    ]
    monkeypatch.setattr(settings, "code_repos_json", json.dumps(entries))
    own, named, github = r.configured_repos()
    assert (own.github_token, named.github_token) == ("", "host-token")
    assert github.github_token == "deploy-token"
    plain = {**host, "namespace": "code-d", "forge": "github"}
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([plain]))
    assert r.configured_repos()[0].github_token == "deploy-token"


@pytest.mark.asyncio
async def test_a_forgejo_request_carries_no_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_github_token", "deploy-token")
    entry = {
        "org": "o",
        "namespace": "code-a",
        "repo": "forgejo.example.org/acme/widgets",
        "git_remote": REMOTE,
        "git_branch": "main",
        "forge": "forgejo",
    }
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))
    (cfg,) = r.configured_repos()
    calls: list[httpx.Request] = []
    with patch.object(forgejo, "get_client", return_value=_replay({"/pulls": []}, calls)):
        await forgejo.merged_prs_since(forgejo_api_repo(cfg.git_remote), cfg.github_token, None)
    assert calls and all("Authorization" not in c.headers for c in calls)


@pytest.mark.parametrize(
    ("remote", "api"),
    [
        (REMOTE, API),
        ("git@forgejo.example.org:acme/widgets.git", API),
        ("ssh://git@forgejo.example.org:2222/acme/widgets.git", API),
        (
            "https://forgejo.example.org:8443/acme/widgets.git",
            "https://forgejo.example.org:8443/api/v1/repos/acme/widgets",
        ),
    ],
)
def test_api_url_is_https_on_the_remote_host(remote: str, api: str) -> None:
    assert forgejo_api_repo(remote) == api


def test_api_url_needs_an_owner_and_a_repository() -> None:
    with pytest.raises(ValueError, match="owner"):
        forgejo_api_repo("https://forgejo.example.org/widgets")


def test_pr_web_url_per_host() -> None:
    assert pr_web_url("acme/widgets", 7) == "https://github.com/acme/widgets/pull/7"
    assert (
        pr_web_url("forgejo.example.org/acme/widgets", 7)
        == "https://forgejo.example.org/acme/widgets/pulls/7"
    )
    assert pr_web_url("", 7) == "" and pr_web_url("acme/widgets", 0) == ""


# ---- the client over recorded responses --------------------------------------------


def _replay(routes: dict[str, Any], calls: list[httpx.Request]) -> httpx.AsyncClient:
    """A client answering each path from `routes` (a page of a list as `path?page=N`,
    else the bare path): a body (200), or (status, body, headers)."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        key = request.url.path.removeprefix("/api/v1/repos/acme/widgets")
        page = request.url.params.get("page", "1")
        answer = routes.get(f"{key}?page={page}", routes.get(key))
        if answer is None:
            return httpx.Response(404, json={"message": "The target couldn't be found."})
        if isinstance(answer, tuple):
            status, body, headers = answer
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(200, json=answer)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _no_files(pulls: list[dict[str, Any]]) -> dict[str, Any]:
    """An empty file list for every recorded PR (the recording kept one PR's files)."""
    return {f"/pulls/{p['number']}/files": [] for p in pulls}


def _next_link(path: str, page: int) -> dict[str, str]:
    return {"Link": f'<{API}{path}?page={page}>; rel="next"'}


@pytest.mark.asyncio
async def test_merged_prs_since_filters_the_base_branch_and_reads_files_in_utc() -> None:
    pulls = FIXTURE["pulls_closed_recentupdate"]
    calls: list[httpx.Request] = []
    routes: dict[str, Any] = {
        **_no_files(pulls),
        "/pulls?page=1": (200, pulls, _next_link("/pulls", 2)),
        "/pulls?page=2": [],
        "/pulls/2231/files": FIXTURE["pull_files"],
    }
    with patch.object(forgejo, "get_client", return_value=_replay(routes, calls)):
        prs = await forgejo.merged_prs_since(API, "tok", None, max_prs=50, base="next")
    # 2277 was closed without merging; the others merged into `next`.
    expected = {p["number"] for p in pulls if p["merged"] and p["base"]["ref"] == "next"}
    assert 2277 not in expected and {p.number for p in prs} == expected
    pr = next(p for p in prs if p.number == 2231)
    # 14:28:29+01:00 is 13:28:29 UTC, stored naive.
    assert pr.merged_at == datetime(2026, 11, 26, 13, 28, 29)
    assert pr.files == ["docs/contributor/customization.md"]
    assert pr.target_branch == "next"
    assert pr.author == "bob" and pr.assignees == ["alice"]
    assert [p.updated_at for p in prs] == sorted(p.updated_at for p in prs)
    first = calls[0]
    assert first.headers["Authorization"] == "token tok"
    assert first.url.params["sort"] == "recentupdate" and first.url.params["state"] == "closed"
    assert "base" not in first.url.params, "the API has no base filter: filtered here"
    assert [c.url.params.get("page") for c in calls if c.url.path.endswith("/pulls")] == ["1", "2"]

    calls.clear()
    with patch.object(forgejo, "get_client", return_value=_replay(routes, calls)):
        other = await forgejo.merged_prs_since(API, "tok", None, max_prs=50, base="main")
    assert other == []


@pytest.mark.asyncio
async def test_merged_prs_since_stops_at_the_cursor() -> None:
    pulls = FIXTURE["pulls_closed_recentupdate"]
    routes = {"/pulls": pulls, "/pulls/2231/files": FIXTURE["pull_files"]}
    since = datetime(2026, 11, 26, 13, 0, 0)  # after 2277's update, UTC
    with patch.object(forgejo, "get_client", return_value=_replay(routes, [])):
        prs = await forgejo.merged_prs_since(API, "tok", since, max_prs=50, base="next")
    assert [p.number for p in prs] == [2231]


@pytest.mark.asyncio
async def test_merged_prs_back_to_walks_by_close_time_and_resumes_at_a_page() -> None:
    pulls = FIXTURE["pulls_closed_recentclose"]
    routes: dict[str, Any] = {
        **_no_files(pulls),
        "/pulls?page=1": (200, pulls, _next_link("/pulls", 2)),
        "/pulls?page=2": [],
    }
    calls: list[httpx.Request] = []
    oldest = datetime(2026, 11, 1)
    with patch.object(forgejo, "get_client", return_value=_replay(routes, calls)):
        prs, nxt = await forgejo.merged_prs_back_to(API, "tok", oldest, base="next")
    # The walk stops at the first PR closed before `oldest`: nothing left to resume.
    expected = [
        p["number"]
        for p in pulls
        if p["merged"]
        and p["base"]["ref"] == "next"
        and (forgejo._parse_dt(p["closed_at"]) or oldest) >= oldest
    ]
    assert len(expected) == 7 and [p.number for p in prs] == expected and nxt == 0
    assert calls[0].url.params["sort"] == "recentclose"

    # The cap is checked between pages, so the page is read whole and a resume at the
    # returned page never repeats a PR.
    calls.clear()
    with patch.object(forgejo, "get_client", return_value=_replay(routes, calls)):
        prs, nxt = await forgejo.merged_prs_back_to(
            API, "tok", datetime(2020, 1, 1), base="next", max_prs=2
        )
    assert len(prs) == 8 and nxt == 2
    assert [c.url.params["page"] for c in calls if c.url.path.endswith("/pulls")] == ["1"]
    with patch.object(forgejo, "get_client", return_value=_replay(routes, [])):
        rest, nxt = await forgejo.merged_prs_back_to(
            API, "tok", datetime(2020, 1, 1), base="next", max_prs=2, start_page=nxt
        )
    assert rest == [] and nxt == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "limited"),
    [(429, True), (403, False), (404, False)],
)
async def test_errors_are_the_github_client_errors(status: int, limited: bool) -> None:
    routes = {"/pulls": (status, {"message": "refused"}, {"Retry-After": "30"})}
    with (
        patch.object(forgejo, "get_client", return_value=_replay(routes, [])),
        pytest.raises(GitHubError) as exc,
    ):
        await forgejo.merged_prs_since(API, "tok", None)
    assert isinstance(exc.value, RateLimited) is limited


@pytest.mark.asyncio
async def test_a_connection_failure_is_a_github_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.object(forgejo, "get_client", return_value=client), pytest.raises(GitHubError):
        await forgejo.pr_files(API, "tok", 1)


# ---- the worker writes the same rows from a Forgejo entry --------------------------


def _forgejo_entry() -> Any:
    return repo_config(
        org="o",
        namespace="code-w",
        repo="forgejo.example.org/acme/widgets",
        git_remote=REMOTE,
        git_branch="next",
        github_token="tok",
        forge="forgejo",
    )


@pytest.mark.asyncio
async def test_worker_writes_pr_rows_for_a_forgejo_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.workers import pr_history as w

    # Issues, PR heads and reviews are GitHub's: on, they still never reach Forgejo.
    monkeypatch.setattr(settings, "code_gh_items_enabled", True)
    monkeypatch.setattr(settings, "code_pr_reviews_enabled", True)
    repo = AsyncMock()

    async def _state(org: str, ns: str, key: str) -> str:
        return {"git_head": "a" * 40, "pr_cursor": "2026-01-01T00:00:00"}.get(key, "")

    repo.get_state.side_effect = _state
    repo.gh_item_titles.return_value = {}
    pulls = FIXTURE["pulls_closed_recentupdate"]
    routes = {**_no_files(pulls), "/pulls": pulls, "/pulls/2231/files": FIXTURE["pull_files"]}
    calls: list[httpx.Request] = []
    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch("server.code.domain.index.repos.configured_repos", return_value=[_forgejo_entry()]),
        patch.object(forgejo, "get_client", return_value=_replay(routes, calls)),
    ):
        result = await w.run_pass()
    assert result.errors == [] and result.prs_upserted == 4 and result.reviews_upserted == 0
    table, rows = repo.insert_rows.call_args_list[0].args
    assert table == "code_pr_history"
    row = next(x for x in rows if x["pr_number"] == 2231)
    assert row["files"] == ["docs/contributor/customization.md"]
    assert row["target_branch"] == "next" and row["namespace"] == "code-w"
    assert {q.url.path.rsplit("/", 1)[-1] for q in calls} <= {"pulls", "files"}
    assert all(str(q.url).startswith(API) for q in calls)
    newest = max(forgejo._parse_dt(p["updated_at"]) or datetime.min for p in pulls if p["merged"])
    assert repo.set_state.call_args.args[2:] == ("pr_cursor", newest.isoformat())


@pytest.mark.asyncio
async def test_worker_skips_a_host_with_no_pr_source() -> None:
    from server.code.workers import pr_history as w

    cfg = repo_config(repo="forgejo.example.org/acme/w", git_remote=REMOTE)
    with (
        patch("server.code.store.index.CodeRepo", return_value=AsyncMock()),
        patch("server.code.domain.index.repos.configured_repos", return_value=[cfg]),
    ):
        result = await w.run_pass()
    assert result.note == "PR history is not available for git remotes"


@pytest.mark.asyncio
async def test_backfill_walks_by_close_time_and_keeps_its_page() -> None:
    from types import SimpleNamespace

    from server.cli import code_prs

    pulls = FIXTURE["pulls_closed_recentclose"]
    routes = {**_no_files(pulls), "/pulls": pulls}
    repo = AsyncMock()
    repo.get_state.return_value = ""
    repo.gh_item_titles.return_value = {}
    args = SimpleNamespace(months=1, max_prs=10)
    with (
        patch.object(forgejo, "get_client", return_value=_replay(routes, [])),
        patch.object(code_prs, "naive_utc_now", return_value=datetime(2026, 11, 27)),
        patch.object(code_prs, "connect_code_plane", AsyncMock()),
        patch("server.code.store.index.CodeRepo", return_value=repo),
    ):
        rc = await code_prs._backfill_prs_forgejo(args, _forgejo_entry())
    assert rc == 0
    table, rows = repo.insert_rows.call_args.args
    # Merged into `next` and closed within the month; the walk stops at the first PR
    # closed before it, so the one closed a month earlier is not read.
    assert table == "code_pr_history"
    assert sorted(x["pr_number"] for x in rows) == [2231, 2281, 2285, 2286, 2287, 2288, 2290]
    assert repo.set_state.call_args.args[2:] == ("pr_backfill_page", "1")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["_backfill_reviews", "_backfill_items", "_backfill_assignees"])
async def test_the_github_only_backfills_send_nothing_for_a_forgejo_entry(command: str) -> None:
    from types import SimpleNamespace

    from server.cli import code_prs

    repo = AsyncMock()
    repo._ch.query_all.return_value = [{"pr_number": 2231}]
    repo.get_state.return_value = ""
    calls: list[httpx.Request] = []
    args = SimpleNamespace(prs=5, pages=1, since=None)
    with (
        patch("server.clients.github.get_client", return_value=_replay({}, calls)),
        patch.object(code_prs, "_repo_cfg", return_value=_forgejo_entry()),
        patch.object(code_prs, "connect_code_plane", AsyncMock()),
        patch("server.code.store.index.CodeRepo", return_value=repo),
    ):
        rc = await getattr(code_prs, command)(args)
    assert rc == 2 and calls == []
