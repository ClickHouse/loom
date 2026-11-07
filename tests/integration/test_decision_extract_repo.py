"""`loom decisions extract --repo` end to end against a live ClickHouse: a design document
added with knowledge.add to a person's namespace names a class of an indexed repository;
the extractor resolves it in the repository's trunk and writes the decision beside the
document (`--into source`) or into the trunk (`--into repo`); memory.for_file, asked the
way the coding plugins' pre-edit hook asks, serves it to a caller who reads where it went
and to no one else.

Handlers run in-process, as the CLI runs them; skips without LOOM_CH_HOST (house rule).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

ORG = unique_org("decrepo")
REMOTE = "https://github.com/acme/widgets.git"
PATH = "widgets/page_reader.py"
_CODE = """class PageReader:
    def read_page(self, offset):
        return offset
"""
DOC = (
    "# Widgets storage\n\n"
    "## Page reads\n\n"
    "PageReader reads one page of rows at a time. "
    "A whole-table read would hold every row of a large table in memory.\n"
)


def _auth(*namespaces: str) -> Any:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    return AuthInfo(
        subject="reader@example.com",
        org=ORG,
        namespaces=namespaces,
        roles=(Role.VIEWER,),
        is_service_account=False,
    )


@pytest.fixture
async def env(ch_client, monkeypatch):
    """A repository acme/widgets indexed at its trunk, and a design document in a person's
    namespace; yields (trunk, person namespace, the repository's `repo:<id>` tag)."""
    from server.code.app.index import repo_registry
    from server.code.app.index.ingest import ingest_file
    from server.code.domain.index.remote import repo_tag
    from server.code.domain.index.repos import RepoConfig, set_registry_repos
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client
    from server.embedding.fake import FakeNGramEmbedder
    from server.knowledge.api.models_knowledge import KnowledgeAddInput
    from server.knowledge.app.writes import handle_knowledge_add
    from server.settings import Settings, settings

    await get_client().connect()
    tag = os.urandom(3).hex()
    trunk, person = f"code-widgets-{tag}", f"person-{tag}"
    set_registry_repos(
        [
            RepoConfig(
                org=ORG,
                namespace=trunk,
                repo="acme/widgets",
                git_dir="",
                git_remote=REMOTE,
                git_branch="main",
                path_prefixes=("",),
                github_token="",
                source="registry",
            )
        ]
    )
    # The registry snapshot set above is the one the run reads: no refresh replaces it.
    monkeypatch.setattr(settings, "code_self_service_enabled", True)

    async def no_refresh(**kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(repo_registry, "refresh_db_repos", no_refresh)
    monkeypatch.setattr(repo_registry, "registry_read", lambda: True)
    # The fake model, chosen on purpose: the run would refuse one it fell back to.
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(Settings, "llm_provider_inferred", lambda self: False)
    code = CodeRepo()
    await ingest_file(
        code,
        ORG,
        trunk,
        path=PATH,
        source=_CODE.encode(),
        commit_sha="a" * 40,
        embedding_model=settings.embedding_model,
        embedder=FakeNGramEmbedder(dim=settings.embedding_dim),
    )
    await code.set_state(ORG, trunk, "git_head", "a" * 40)
    await handle_knowledge_add(
        KnowledgeAddInput(
            org=ORG, namespace=person, source_uri="design/storage.md", content=DOC, title="Storage"
        )
    )
    yield trunk, person, repo_tag(REMOTE)
    set_registry_repos([])


async def _extract(person: str, into: str) -> Any:
    from server.memory.app.decision_extract import extract_document
    from server.memory.domain.provenance import cli_actor, pipeline_actor

    # As the CLI runs it.
    with pipeline_actor(cli_actor("decisions")):
        report = await extract_document(
            ORG, person, "design/storage.md", dry_run=False, repo="acme/widgets", into=into
        )
    assert report.error == "", report.error
    assert [r.action for r in report.records] == ["written"], report
    assert any(f"exact -> {PATH}" in s for s in report.records[0].signals)
    return report


async def _for_file(namespace: str, tags: list[str], extra: list[str], auth: Any) -> list[Any]:
    """memory.for_file with the plugin hook's body (`forFileBody`): the person's namespace
    and tags, the repository's trunk as the extra namespace, under `auth`."""
    from server.auth.context import set_current_auth
    from server.memory.api.models_for_file import MemoryForFileInput
    from server.memory.app.for_file import handle_memory_for_file

    set_current_auth(auth)
    try:
        out = await handle_memory_for_file(
            MemoryForFileInput(
                org=ORG,
                namespace=namespace,
                path=PATH,
                tags=tags,
                token_budget=300,
                extra_namespaces=extra,
            )
        )
    finally:
        set_current_auth(None)
    return [h for h in out.hits if h.memory_type == "decision"]


async def _assert_written(hit: Any, person: str) -> None:
    """The row as the extractor wrote it, wherever it went."""
    import server.memory.store as memory_store
    from server.identity.domain.read_scope import WHOLE_NAMESPACE

    ident = memory_store.Identity(
        org=ORG, namespace=hit.namespace, agent="loom-decisions", user_id="-", session_id="-"
    )
    row = await memory_store.get_by_key(ident, hit.memory_key, scope=WHOLE_NAMESPACE, strong=True)
    assert row is not None
    assert (row.user_id, row.session_id) == ("-", "-")
    assert row.source_class == "derived" and row.actor == "process:cli:decisions"
    assert hit.memory_key.startswith("decision:") and hit.memory_type == "decision"
    assert hit.agent == "loom-decisions" and hit.status == "active"
    assert hit.confidence == 0.5 and not hit.pinned
    assert hit.matched_files == [PATH]
    assert "PageReader reads one page of rows at a time" in hit.value
    assert "A whole-table read would hold every row of a large table in memory." in hit.value
    assert "source-kind:design_doc" in hit.tags
    # The document cited in its own namespace.
    assert (
        hit.source_resource_id
        == f"knowledge:{person}/design/storage.md#Widgets storage > Page reads"
    )


async def test_into_source_serves_the_decision_in_the_documents_namespace(env) -> None:
    trunk, person, tag = env
    await _extract(person, "source")
    hook_tags = ["project:widgets", tag]
    (hit,) = await _for_file(person, hook_tags, [trunk], _auth(person))
    assert hit.namespace == person and tag in hit.tags
    await _assert_written(hit, person)
    # A session in another repository (another tag) is not served it.
    assert await _for_file(person, ["project:other", "repo:0000000000"], [], _auth(person)) == []
    # Nor is a repository reader working from their own namespace: the row is the person's.
    reader = f"reader-{person}"
    assert await _for_file(reader, hook_tags, [trunk], _auth(reader, trunk)) == []


async def test_into_repo_serves_the_decision_to_repository_readers_only(env) -> None:
    trunk, person, tag = env
    await _extract(person, "repo")
    reader = f"reader-{person}"
    hook_tags = ["project:widgets", tag]
    (hit,) = await _for_file(reader, hook_tags, [trunk], _auth(reader, trunk))
    assert hit.namespace == trunk and tag not in hit.tags
    await _assert_written(hit, person)
    # Nothing was written beside the document.
    assert await _for_file(person, hook_tags, [], _auth(person)) == []
    # A caller who cannot read the trunk is not served it.
    assert await _for_file(reader, hook_tags, [trunk], _auth(reader)) == []
