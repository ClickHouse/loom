"""Finite real-store MCP qualification; run only on the owned hosted fixture."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from loom.errors import LoomError
from loom.session_protection import AutomaticCaptureSuppressed

from server.auth.jwt import mint_token
from server.core.ch import get_client, set_query_tag
from server.knowledge.app import mcp_profiles, mcp_source_authority
from tests.integration.mcp_source_qualification_fixtures import (
    Reader,
    World,
    cancel_owned,
    qualification_app,
    require_clean_selection,
    world,
)
from tests.integration.mcp_source_qualification_servers import SECOND, SELECTED, UNSELECTED

# Imported fixtures are collected by pytest; every case owns its application lifespan.
__all__ = ["qualification_app", "world"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("world", ["resources", "tools"], indirect=True),
]


async def imported(case: World, **kwargs: Any) -> Any:
    await case.add(**kwargs)
    result = await case.sync()
    assert result.status == "ok" and result.complete
    head = await case.head()
    assert head.content == case.source.text and head.revision == 1
    return head


def no_content(value: Any, case: World, *, raw_identity: bool = True) -> None:
    encoded = json.dumps(value, ensure_ascii=False)
    assert case.private_marker not in encoded
    assert case.source.bearer not in encoded
    if raw_identity:
        assert SELECTED not in encoded and UNSELECTED not in encoded


async def admitted_reads(case: World, actor: Reader) -> None:
    namespace = case.entry["namespace"]
    got = await case.get(actor)
    assert got["found"] is True and got["content"] == case.source.text
    assert got["source_uri"] == case.uri and got["revision"] >= 1
    assert actor.protection.protected
    for op, body in (
        ("knowledge.list", {"limit": 20}),
        ("knowledge.search", {"query": case.source.marker, "search_mode": "hybrid"}),
        ("knowledge.outline", {"source_uri": case.uri}),
        ("knowledge.inspect", {"sample_limit": 20}),
    ):
        set_query_tag("test.mcp_source_qualification.admitted")
        result = await actor.call(op, namespace=namespace, **body)
        assert case.uri in json.dumps(result), op
        assert case.source.bearer not in json.dumps(result)
    download = await case.download(actor)
    assert download.status_code == 200
    assert download.headers["x-loom-download-source"] == "blob"
    assert download.content == case.source.text.encode()


async def denied_reads(
    case: World, actor: Reader, *, grant_denied: bool = False, allow_other: bool = False
) -> None:
    namespace = case.entry["namespace"]
    for op, body in (
        ("knowledge.get", {"source_uri": case.uri}),
        ("knowledge.get", {"source_uri": case.uri, "include_superseded": True}),
        ("knowledge.list", {"include_superseded": True, "limit": 20}),
        ("knowledge.search", {"query": "Selected document", "search_mode": "hybrid"}),
        ("knowledge.outline", {"source_uri": case.uri}),
        ("knowledge.inspect", {"sample_limit": 20}),
        ("memory.export_evidence", {"kind": "chunks", "include_high": True, "limit": 20}),
    ):
        set_query_tag("test.mcp_source_qualification.denied")
        try:
            result = await actor.call(op, namespace=namespace, **body)
        except LoomError as error:
            assert grant_denied and error.status in {403, 404}
            no_content(error.detail, case)
        else:
            no_content(result, case, raw_identity=not allow_other)
            if op in {"knowledge.get", "knowledge.outline"}:
                assert result["found"] is False
            elif op != "memory.export_evidence":
                assert case.uri not in json.dumps(result)
    try:
        await case.download(actor)
    except LoomError as error:
        assert error.status in ({403, 404} if grant_denied else {404})
        no_content(error.detail, case)
    else:
        pytest.fail("Denied original was released")


async def test_writes_refresh(world: World) -> None:
    first = await imported(world)
    chunks = await world.chunks()
    assert chunks and any(world.source.marker in chunk["content"] for chunk in chunks)
    assert all(
        chunk["embedding_dim"] == 1024
        and len(chunk["embedding"]) == 1024
        and chunk["embedding_model"] == "fake-ngram-1024"
        for chunk in chunks
    )
    assert first.blob_path and first.blob_size == len(world.source.text.encode())
    assert first.source_class == "imported" and first.actor
    row = await world.source_row()
    blob = await get_client().query_one(
        "SELECT location, length(data) AS inline_bytes, size FROM loom.document_blobs FINAL "
        "WHERE org = {org:String} AND namespace = {namespace:String} "
        "AND sha256 = {sha:String} LIMIT 1",
        params={"org": row.org, "namespace": row.mirror, "sha": first.blob_path},
    )
    assert blob and blob["location"].startswith("s3://") and blob["inline_bytes"] == 0
    assert blob["size"] == len(world.source.text.encode())
    metadata = json.loads(first.metadata)
    assert metadata["mcp_source_id"] == world.entry["source_id"]
    assert metadata["mcp_read_at"] and len(metadata["mcp_scope_sha256"]) == 64
    assert metadata["mcp_read_operation"] == (
        "tools/call" if world.source.kind == "tools" else "resources/read"
    )
    await admitted_reads(world, world.owner)
    embeds = list(world.embed_calls)
    assert (await world.sync()).complete
    unchanged = await world.head()
    assert unchanged.doc_id == first.doc_id and unchanged.revision == first.revision
    assert unchanged.blob_path == first.blob_path and await world.chunks() == chunks
    assert unchanged.model_dump(exclude={"version", "metadata"}) == first.model_dump(
        exclude={"version", "metadata"}
    )
    assert world.embed_calls == embeds
    world.source.text += "\nChanged selected content.\n"
    await world.control("refresh")
    assert (await world.sync()).complete
    changed = await world.head()
    assert changed.content == world.source.text and changed.revision == first.revision + 1
    assert changed.blob_path != first.blob_path and changed.content_sha256 != first.content_sha256
    await admitted_reads(world, world.owner)
    require_clean_selection(world)


async def test_owner_privacy(world: World) -> None:
    await imported(world)
    await admitted_reads(world, world.owner)
    await denied_reads(world, world.other)
    await denied_reads(world, world.foreign, grant_denied=True)
    with pytest.raises(LoomError) as cross_org:
        await world.foreign.call(
            "knowledge.get", org=world.owner.org, namespace=world.namespace, source_uri=world.uri
        )
    assert cross_org.value.status == 403
    no_content(cross_org.value.detail, world)
    listed = await world.other.call("knowledge.connection.mcp_list")
    assert listed["connections"] == []
    inspected = await world.other.call("knowledge.connection.mcp_inspect", slot=world.slot)
    assert inspected["found"] is False
    # A protected response must stop automatic derived capture before it dispatches.
    with pytest.raises(AutomaticCaptureSuppressed), world.owner.protection.automatic():
        await world.owner.call(
            "memory.set",
            namespace=world.namespace,
            agent="qualification",
            memory_key="automatic-copy",
            content=world.source.text,
        )
    history = await world.owner.call(
        "memory.history",
        namespace=world.namespace,
        agent="qualification",
        memory_key="automatic-copy",
        limit=20,
    )
    no_content(history, world)


async def test_source_identity(world: World) -> None:
    await imported(world)
    first_text = world.source.text
    await world.owner.call(
        "grants.create",
        namespace=world.namespace,
        subject=world.other.subject,
        role="writer",
        replace=True,
    )
    connection = await world.other.call(
        "knowledge.connection.mcp_connect",
        profile_id="qualification-" + world.source.kind,
        request_id=str(uuid4()),
        bearer=world.source.bearer,
    )
    second_marker = "qualificationcontent" + uuid4().hex
    world.source.text = "# Selected document\n\n" + second_marker
    second = replace(
        world,
        owner=world.other,
        other=world.owner,
        slot=connection["connection"]["slot"],
        entry={},
        private_marker=second_marker,
    )
    await imported(second)
    assert second.uri != world.uri and second.entry["source_id"] != world.entry["source_id"]
    assert (await world.get())["content"] == first_text
    assert (await second.get())["content"] == world.source.text
    await denied_reads(world, world.other, allow_other=True)
    await denied_reads(second, world.owner, allow_other=True)


async def test_team_copy_grants(world: World) -> None:
    await imported(world, namespace=world.child, copy=True)
    await admitted_reads(world, world.other)
    await world.owner.call("grants.revoke", namespace=world.team, subject=world.other.subject)
    # The same warmed client and capsule must lose inherited access.
    await denied_reads(world, world.other, grant_denied=True)
    await admitted_reads(world, world.owner)


async def test_copy_consent(world: World) -> None:
    with pytest.raises(LoomError):
        await world.add(namespace=world.child, copy=True, confirm=False)
    assert world.source.reads == []
    await writer_cannot_publish(world)
    await operator_copy_approval(world)


async def operator_copy_approval(world: World) -> None:
    replacement = mcp_profiles.MCPProfileCatalog.parse(
        json.dumps(
            {
                "version": 1,
                "profiles": [
                    world.source.profile([world.owner.org, world.foreign.org], copy_allowed=False)
                ],
            }
        )
    )
    world.catalog.current = replacement
    connection = await world.owner.call(
        "knowledge.connection.mcp_connect",
        profile_id="qualification-" + world.source.kind,
        request_id=str(uuid4()),
        bearer=world.source.bearer,
    )
    world.slot = connection["connection"]["slot"]
    with pytest.raises(LoomError):
        await world.add(namespace=world.child, copy=True)
    await world.add()


async def writer_cannot_publish(world: World) -> None:
    # A writer can import privately but cannot publish to collection readers.
    await world.owner.call(
        "grants.create",
        namespace=world.team,
        subject=world.other.subject,
        role="writer",
        replace=True,
    )
    connection = await world.other.call(
        "knowledge.connection.mcp_connect",
        profile_id="qualification-" + world.source.kind,
        request_id=str(uuid4()),
        bearer=world.source.bearer,
    )
    private = await world.other.call(
        "knowledge.source.mcp_add",
        namespace=world.child,
        slot=connection["connection"]["slot"],
        mode="snapshot",
        reader_policy="owner_only",
        resource_ids=[SELECTED],
        request_id=str(uuid4()),
    )
    assert private["source"]["reader_policy"] == "owner_only"
    with pytest.raises(LoomError):
        await world.other.call(
            "knowledge.source.mcp_add",
            namespace=world.child,
            slot=connection["connection"]["slot"],
            mode="snapshot",
            reader_policy="collection_copy",
            resource_ids=[SELECTED],
            request_id=str(uuid4()),
            confirm_copy_authority=True,
        )
    assert world.source.reads == []


async def test_account_revocation(world: World) -> None:
    await imported(world)
    await admitted_reads(world, world.owner)
    await world.owner.call("knowledge.connection.mcp_disconnect", slot=world.slot)
    await denied_reads(world, world.owner)
    assert (await world.head()).content == world.source.text


async def test_owner_grant_revocation(world: World) -> None:
    await imported(world)
    await admitted_reads(world, world.owner)
    # A second administrator revokes the source owner's destination authority.
    await world.owner.call(
        "grants.create",
        namespace=world.namespace,
        subject=world.other.subject,
        role="admin",
        replace=True,
    )
    await world.other.call("grants.revoke", namespace=world.namespace, subject=world.owner.subject)
    await denied_reads(world, world.owner, grant_denied=True)
    await denied_reads(world, world.other)


async def test_profile_revocation(world: World) -> None:
    await imported(world)
    await admitted_reads(world, world.owner)
    # Mutate the owned operator catalog, leaving sealed credentials and rows intact.
    world.catalog.current = mcp_profiles.MCPProfileCatalog.parse(
        json.dumps({"version": 1, "profiles": []})
    )
    await denied_reads(world, world.owner)
    assert (await world.head()).content == world.source.text


async def test_pause(world: World) -> None:
    await imported(world)
    await admitted_reads(world, world.owner)
    await world.control("pause")
    await denied_reads(world, world.owner)
    assert (await world.head()).content == world.source.text
    await world.control("resume")
    await admitted_reads(world, world.owner)


async def test_read_failure(world: World) -> None:
    head = await imported(world)
    world.source.denied = True
    result = await world.sync()
    assert result.status != "ok" and not result.complete
    invalidated = await world.head()
    metadata = json.loads(invalidated.metadata)
    assert "mcp_scope_sha256" not in metadata and "mcp_read_at" not in metadata
    assert invalidated.content == head.content and invalidated.blob_path == head.blob_path
    assert invalidated.superseded_at is None
    await denied_reads(world, world.owner)
    world.source.denied = False
    assert (await world.sync()).complete
    assert (await world.head()).revision == head.revision
    await admitted_reads(world, world.owner)


async def test_scope_revision(world: World) -> None:
    first = await imported(world)
    old_uri, old_id = world.uri, world.entry["source_id"]
    revision = world.entry["scope_revision"]
    with pytest.raises(LoomError) as audience:
        await world.control(
            "update",
            resource_ids=[SELECTED],
            mode="selected_refresh",
            if_scope_revision=revision,
            reader_policy="collection_copy",
        )
    assert audience.value.status == 422
    assert world.entry["source_id"] == old_id and world.entry["reader_policy"] == "owner_only"
    await world.control(
        "update",
        resource_ids=[SECOND],
        mode="selected_refresh",
        if_scope_revision=revision,
    )
    assert world.entry["scope_revision"] > revision and world.entry["source_id"] == old_id
    assert (await world.get())["found"] is False
    assert (await world.head()).blob_path == first.blob_path
    assert (await world.sync()).complete
    revision = world.entry["scope_revision"]
    await world.control(
        "update",
        resource_ids=[SELECTED],
        mode="snapshot",
        if_scope_revision=revision,
    )
    assert world.entry["scope_revision"] > revision and world.uri == old_uri
    assert (await world.get())["found"] is False
    assert (await world.sync()).complete
    await admitted_reads(world, world.owner)
    with pytest.raises(LoomError):
        await world.control(
            "update",
            resource_ids=[SECOND],
            mode="live",
            if_scope_revision=revision,
        )
    require_clean_selection(world)


async def test_snapshot(world: World) -> None:
    head = await imported(world, mode="snapshot")
    reads = list(world.source.reads)
    world.source.text += "\nUnrequested later content.\n"
    assert (await world.sync()).complete
    assert world.source.reads == reads and (await world.head()).content == head.content
    await world.control("refresh")
    assert (await world.sync()).complete
    assert (await world.head()).content == world.source.text


async def test_schema_drift(world: World) -> None:
    head = await imported(world)
    if world.source.kind == "tools":
        world.source.drift = True
    else:
        # Resource servers have no mapped tool schema; operator approval drift still fences them.
        changed = world.source.profile([world.owner.org, world.foreign.org])
        changed["config"]["limits"]["max_pages"] = 1
        replacement = mcp_profiles.MCPProfileCatalog.parse(
            json.dumps({"version": 1, "profiles": [changed]})
        )
        world.catalog.current = replacement
    reads = len(world.source.reads)
    result = await world.sync()
    assert result.status != "ok" and not result.complete
    assert len(world.source.reads) == reads
    assert (await world.head()).content == head.content
    await denied_reads(world, world.owner)


async def test_partial_listing(world: World) -> None:
    await world.add(ids=[SELECTED, SECOND])
    world.source.partial = True
    bound = await mcp_source_authority.validate_source(await world.source_row())
    async with bound.client() as client:
        await client.discover()
        page = await (
            client.search("Selected document")
            if world.source.kind == "tools"
            else client.list_resources()
        )
        assert page.items and page.complete is False
        assert all(item.receipt is None for item in page.items)
    assert world.source.reads == []
    listing_calls = list(world.source.calls)
    assert (await world.sync()).complete
    assert world.source.calls.count("resources/list") == listing_calls.count("resources/list")
    assert world.source.calls.count("search_documents") == listing_calls.count("search_documents")
    first, second = await world.head(), await world.head(SECOND)
    # Selected refresh never enumerates upstream or treats an omitted page as deletion.
    world.source.denied = True
    assert not (await world.sync()).complete
    assert (await world.head()).content == first.content
    assert (await world.head(SECOND)).content == second.content
    inspected = await world.control("inspect")
    source = inspected["source"]
    assert source["complete_enumeration"] is source["upstream_deletion_tracking"] is False
    assert source["current_access_attested"] is False
    require_clean_selection(world)


async def test_cancellation(world: World) -> None:
    await world.add()
    world.source.block_read = True
    task = asyncio.create_task(world.sync(), name="mcp-source-qualification-owned-turn")
    try:
        async with asyncio.timeout(15):
            await world.source.started.wait()
    finally:
        await cancel_owned(task, world.source)
    row = await world.source_row()
    assert await get_client().query_one(
        "SELECT count() AS count FROM loom.documents WHERE org = {org:String} "
        "AND namespace = {namespace:String} AND source_uri = {uri:String}",
        params={"org": row.org, "namespace": row.mirror, "uri": world.uri},
    ) == {"count": 0}
    world.source.block_read = False
    result = await world.sync()
    assert result.complete and (await world.head()).content == world.source.text


async def test_live(world: World) -> None:
    await world.add(mode="live")
    assert (await world.sync()).complete and world.source.reads == []
    selected = await world.owner.call(
        "knowledge.source.mcp_read",
        namespace=world.namespace,
        name=world.entry["name"],
        resource_id=SELECTED,
    )
    assert selected["text"] == world.source.text and selected["uri"] == world.uri
    assert selected["source_id"] == world.entry["source_id"]
    assert selected["resource_id"] == SELECTED
    assert selected["remote_identity_verified"] is selected["upstream_acl_verified"] is False
    assert world.owner.protection.protected
    for actor, identity in ((world.other, SELECTED), (world.owner, UNSELECTED)):
        set_query_tag("test.mcp_source_qualification.live_denied")
        reads = list(world.source.reads)
        with pytest.raises(LoomError) as refused:
            await actor.call(
                "knowledge.source.mcp_read",
                namespace=world.namespace,
                name=world.entry["name"],
                resource_id=identity,
            )
        assert refused.value.status in {400, 403, 404, 422}
        no_content(refused.value.detail, world, raw_identity=False)
        assert world.source.reads == reads
    if world.source.kind == "tools":
        result = await world.owner.call(
            "search.query",
            namespace=world.namespace,
            query="Selected document",
            live=True,
        )
        assert world.source.marker in json.dumps(result)
        assert world.uri in json.dumps(result) and world.source.reads
        assert UNSELECTED not in world.source.reads
        denied = await world.other.call(
            "search.query",
            namespace=world.namespace,
            query="Selected document",
            live=True,
        )
        no_content(denied, world)
    for table in ("documents", "chunks", "memory"):
        set_query_tag("test.mcp_source_qualification.no_import")
        assert await get_client().query_one(
            f"SELECT count() AS count FROM loom.{table} WHERE org = {{org:String}}",
            params={"org": world.owner.org},
        ) == {"count": 0}
    require_clean_selection(world)


async def test_cli_context(world: World, qualification_app: Any) -> None:
    """A real private response protects subsequent installed-CLI subprocesses."""
    await world.add(mode="live")
    tokens = [
        mint_token(
            subject,
            world.owner.org,
            ["*"],
            ["admin"],
            ttl_seconds=1800,
            is_service_account=False,
            session_id=str(uuid4()),
        )
        for subject in (world.owner.subject, world.other.subject)
    ]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    listener.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(
            qualification_app,
            host="127.0.0.1",
            port=listener.getsockname()[1],
            lifespan="off",  # qualification_app owns the actual startup and shutdown.
            access_log=False,
            log_config=None,
            log_level="critical",
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]), name="mcp-cli-owned-http")
    child: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    pytest.fail("Owned CLI HTTP fixture did not start")
                await asyncio.sleep(0.01)
        env = {
            **os.environ,
            "LOOM_CLI_QUAL_BASE_URL": f"http://127.0.0.1:{listener.getsockname()[1]}",
            "LOOM_CLI_QUAL_ORG": world.owner.org,
            "LOOM_CLI_QUAL_TOKEN": tokens[0],
            "LOOM_CLI_QUAL_OTHER_TOKEN": tokens[1],
            "LOOM_CLI_QUAL_WRITE_NAMESPACE": world.team,
            "LOOM_CLI_QUAL_PRIVATE_READ_REQUEST": json.dumps(
                {"namespace": world.namespace, "name": world.entry["name"], "resource_id": SELECTED}
            ),
            "LOOM_CLI_QUAL_PRIVATE_MARKER": world.source.marker,
        }
        target = "sdk/python/tests/qualify_cli_source_context.py"
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            str(target),
            "-q",
            "--tb=no",
            "--show-capture=no",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        async with asyncio.timeout(180):
            assert await child.wait() == 0
        assert world.source.reads == [SELECTED]
    finally:
        if child is not None and child.returncode is None:
            # This process group contains only the explicitly owned CLI qualification.
            import signal

            os.killpg(child.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(5):
                    await child.wait()
            except TimeoutError:
                os.killpg(child.pid, signal.SIGKILL)
                await child.wait()
        server.should_exit = True
        try:
            async with asyncio.timeout(5):
                await asyncio.shield(serving)
        except TimeoutError:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
        finally:
            listener.close()
        assert serving.done()
