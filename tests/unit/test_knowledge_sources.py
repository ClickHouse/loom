"""knowledge.source.*: the document-source registry ops, over an in-memory store
and a stub probe, and the ADMIN gate on every write and on `check`."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

import server.auth.authz as authz
from server.auth.authz import Role, check_authorized
from server.auth.jwt import AuthInfo
from server.core import leases
from server.core.errors import (
    NotFoundError,
    OverloadedError,
    SourceReplacedError,
    ValidationError,
)
from server.core.secrets_at_rest import unseal
from server.knowledge.api.models_sources import (
    KnowledgeSourceAddInput,
    KnowledgeSourceCheckInput,
    KnowledgeSourceInspectInput,
    KnowledgeSourceListInput,
    KnowledgeSourceRemoveInput,
    KnowledgeSourceRotateInput,
    KnowledgeSourceSyncNowInput,
)
from server.knowledge.app import source_probes
from server.knowledge.app import sources as app
from server.knowledge.domain import sources as domain
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import sources as store
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from tests.unit.ch_fake import RecordingCH
from tests.unit.schema_ddl import column_names

ORG, NS = "acme", "code-acme-app"
FOLDER = "1AbCdEfGhIjKlMnOp"
SECRET = '{"type": "service_account", "client_email": "a@b", "private_key": "k"}'


class _Store:
    """The store module's functions over a dict of the latest row per name."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], KnowledgeSource] = {}
        self.states: dict[str, dict[str, str]] = {}
        # Live documents the sync worker wrote with each name's tag.
        self.documents: dict[str, int] = {}

    async def list_sources(self, org: str, ns: str) -> list[KnowledgeSource]:
        found = [s for (o, n, _), s in self.rows.items() if (o, n) == (org, ns) and s.active]
        return sorted(found, key=lambda s: s.name)

    async def get_source(self, org: str, ns: str, name: str) -> KnowledgeSource | None:
        src = self.rows.get((org, ns, name))
        return src if src and src.active else None

    async def latest_source(self, org: str, ns: str, name: str) -> KnowledgeSource | None:
        return self.rows.get((org, ns, name))

    async def active_count(self, org: str, ns: str) -> int:
        return len(await self.list_sources(org, ns))

    async def insert_source(self, src: KnowledgeSource) -> None:
        self.rows[(src.org, src.namespace, src.name)] = src

    async def read_states(self, org: str, ns: str, names: list[str]) -> dict[str, Any]:
        return {n: self.states[n] for n in names if n in self.states}

    async def write_state(self, org: str, ns: str, name: str, *, state_value: str, detail: Any):
        self.states[name] = {"state_value": state_value, "detail": json.dumps(detail)}

    async def source_documents(self, org: str, ns: str, name: str) -> list[dict[str, Any]]:
        return [{"source_uri": "https://x/1", "title": "Deck", "source_revision": "r1"}]

    async def count_documents(self, org: str, ns: str, name: str) -> int:
        return self.documents.get(name, 0)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fake = _Store()
    monkeypatch.setattr(app, "store", fake)
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-root-secret")
    probes: list[tuple[str, dict[str, Any], str]] = []

    async def _probe(kind: str, config: dict[str, Any], credential: str) -> source_probes.Probe:
        probes.append((kind, config, credential))
        return source_probes.Probe(documents=3, sample=["Deck"])

    async def _trunk(org: str, ns: str) -> bool:
        return ns == NS

    monkeypatch.setattr(source_probes, "probe", _probe)
    monkeypatch.setattr(app, "is_trunk_namespace", _trunk)

    async def _registered(org: str, ns: str) -> bool:
        return ns == NS

    monkeypatch.setattr(app, "_registered", _registered)
    leases_taken: list[str] = []

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any) -> AsyncIterator[bool]:
        leases_taken.append(name)
        yield True

    monkeypatch.setattr(leases, "holding", _holding)
    fake.probes = probes  # type: ignore[attr-defined]
    fake.leases = leases_taken  # type: ignore[attr-defined]
    return fake


def _add(**kw: Any) -> KnowledgeSourceAddInput:
    body: dict[str, Any] = {
        "org": ORG,
        "namespace": NS,
        "name": "design-decks",
        "kind": "gdrive",
        "config": {"folder_ids": [FOLDER]},
        "credential": SECRET,
    }
    body.update(kw)
    return KnowledgeSourceAddInput(**body)


def _code(call: Any) -> str:
    with pytest.raises((ValidationError, NotFoundError)) as e:
        asyncio.run(call)
    return str(e.value.to_detail()["code"])


def test_add_probes_then_stores_the_credential_sealed(fake: _Store) -> None:
    out = asyncio.run(app.handle_knowledge_source_add(_add(name=" Design-Decks ")))
    assert not out.dry_run and out.name == "design-decks"
    assert out.probe.documents == 3 and out.probe.sample == ["Deck"]
    assert out.config == {"folder_ids": [FOLDER], "include": ["docs", "slides", "pdf", "pptx"]}
    src = fake.rows[(ORG, NS, "design-decks")]
    assert src.kind == "gdrive" and src.active and src.config == out.config
    assert SECRET not in src.credential_enc
    assert unseal("knowledge_sources.credential", src.credential_enc) == SECRET
    assert SECRET not in out.model_dump_json()


def test_a_page_store_source_stores_its_ids_in_one_form(fake: _Store) -> None:
    page = "0123456789abcdef0123456789ABCDEF"
    dashed = "01234567-89ab-cdef-0123-456789abcdef"
    config = {"page_ids": [page, dashed], "database_ids": ["fedcba98-7654-3210-fedc-ba9876543210"]}
    out = asyncio.run(app.handle_knowledge_source_add(_add(kind="notion", config=config)))
    assert out.config == {
        "page_ids": [dashed],
        "database_ids": ["fedcba98-7654-3210-fedc-ba9876543210"],
    }
    assert fake.probes[-1][0] == "notion"  # type: ignore[attr-defined]


def test_a_dry_run_probes_and_writes_nothing(fake: _Store) -> None:
    out = asyncio.run(app.handle_knowledge_source_add(_add(dry_run=True)))
    assert out.dry_run
    assert fake.rows == {} and len(fake.probes) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"name": "x"}, "BAD_NAME"),
        ({"kind": "ftp"}, "BAD_KIND"),
        ({"kind": "notion"}, "BAD_CONFIG"),  # a folder config is not a page store's
        ({"kind": "notion", "config": {"page_ids": []}}, "BAD_CONFIG"),
        ({"kind": "notion", "config": {"page_ids": ["not-an-id"]}}, "BAD_CONFIG"),
        ({"kind": "notion", "config": {"page_ids": "a" * 32}}, "BAD_CONFIG"),
        (
            {"kind": "notion", "config": {"page_ids": [f"{n:032x}" for n in range(21)]}},
            "BAD_CONFIG",
        ),
        ({"kind": "notion", "config": {"database_ids": [{"id": 1}]}}, "BAD_CONFIG"),
        ({"config": {"folder_ids": ["bad id'"]}}, "BAD_CONFIG"),
        ({"config": {"folder_ids": [FOLDER + "\n"]}}, "BAD_CONFIG"),
        ({"config": {"folder_ids": [FOLDER], "include": ["xls"]}}, "BAD_CONFIG"),
        ({"config": {"folder_ids": [FOLDER], "include": [["docs"]]}}, "BAD_CONFIG"),
        ({"config": {"folder_ids": [FOLDER], "include": [{"k": 1}]}}, "BAD_CONFIG"),
        ({"config": {"folder_ids": [["x"]]}}, "BAD_CONFIG"),
        ({"config": {"folder_ids": FOLDER}}, "BAD_CONFIG"),
        ({"config": {"folder_ids": []}}, "BAD_CONFIG"),
        ({"config": {"folder_ids": [FOLDER], "host": "h"}}, "BAD_CONFIG"),
        ({"namespace": "notes"}, "NOT_A_CODE_NAMESPACE"),
        ({"credential": " "}, "CREDENTIAL_REFUSED"),
    ],
)
def test_add_refuses_before_any_probe(fake: _Store, kw: dict[str, Any], code: str) -> None:
    assert _code(app.handle_knowledge_source_add(_add(**kw))) == code
    assert fake.rows == {} and fake.probes == []  # type: ignore[attr-defined]


def test_add_refuses_a_taken_name_and_a_full_namespace(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    assert _code(app.handle_knowledge_source_add(_add())) == "SOURCE_EXISTS"
    monkeypatch.setattr(settings, "knowledge_sources_max_per_namespace", 1)
    assert _code(app.handle_knowledge_source_add(_add(name="other"))) == "SOURCE_QUOTA"


def test_add_without_a_root_secret_refuses(fake: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "")
    assert _code(app.handle_knowledge_source_add(_add())) == "NO_ROOT_SECRET"
    assert fake.rows == {}


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (source_probes.CredentialRefused("http_403"), "CREDENTIAL_REFUSED"),
        (source_probes.SourceUnreachable("http_500"), "SOURCE_UNREACHABLE"),
    ],
)
def test_a_failed_probe_stores_nothing(
    fake: _Store, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    async def _fail(*_: Any) -> source_probes.Probe:
        raise error

    monkeypatch.setattr(source_probes, "probe", _fail)
    assert _code(app.handle_knowledge_source_add(_add())) == code
    assert fake.rows == {}


def test_a_folder_config_naming_docx_is_stored_in_the_table_s_order_and_probed(
    fake: _Store,
) -> None:
    config = {"folder_ids": [FOLDER], "include": ["docx", "pdf"]}
    out = asyncio.run(app.handle_knowledge_source_add(_add(config=config)))
    assert out.config["include"] == ["pdf", "docx"]
    assert fake.probes[-1][1]["include"] == ["pdf", "docx"]  # type: ignore[attr-defined]


def test_list_and_inspect_return_config_and_sync_never_the_credential(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.states["design-decks"] = {
        "state_value": "stamp",
        "detail": json.dumps(
            {
                "requested_at": "t",
                "full": True,
                "pending_extract": 1,
                "mined": {"https://x/mined": "r1"},
                "status": "ok",
                "changed": 2,
                "full_listed_at": "t0",
            }
        ),
    }
    monkeypatch.setattr(settings, "knowledge_source_sync_enabled", True)
    listed = asyncio.run(
        app.handle_knowledge_source_list(KnowledgeSourceListInput(org=ORG, namespace=NS))
    )
    assert [s.name for s in listed.sources] == ["design-decks"]
    assert listed.enabled
    sync = listed.sources[0].sync.model_dump()
    assert (sync["requested_at"], sync["status"], sync["changed"]) == ("t", "ok", 2)
    # The pending count is shown, never the mined documents' addresses.
    assert set(sync) == set(domain.SYNC_FIELDS) and sync["pending_extract"] == 1
    body = KnowledgeSourceInspectInput(org=ORG, namespace=NS, name="design-decks")
    seen = asyncio.run(app.handle_knowledge_source_inspect(body))
    assert seen.documents[0].source_uri == "https://x/1"
    for text in (listed.model_dump_json(), seen.model_dump_json()):
        assert "credential" not in text and SECRET not in text and "x/mined" not in text


def test_unknown_sources_are_not_found(fake: _Store) -> None:
    assert (
        _code(
            app.handle_knowledge_source_inspect(
                KnowledgeSourceInspectInput(org=ORG, namespace=NS, name="nope")
            )
        )
        == "SOURCE_NOT_FOUND"
    )
    assert (
        _code(
            app.handle_knowledge_source_sync_now(
                KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="nope")
            )
        )
        == "SOURCE_NOT_FOUND"
    )
    listed = asyncio.run(
        app.handle_knowledge_source_list(KnowledgeSourceListInput(org=ORG, namespace="other"))
    )
    assert listed.sources == []


def test_rotate_replaces_only_the_credential(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    before = fake.rows[(ORG, NS, "design-decks")]
    new = SECRET.replace("a@b", "c@d")
    body = KnowledgeSourceRotateInput(org=ORG, namespace=NS, name="design-decks", credential=new)
    out = asyncio.run(app.handle_knowledge_source_rotate(body))
    after = fake.rows[(ORG, NS, "design-decks")]
    assert out.probe.documents == 3 and out.rotated_at
    assert unseal("knowledge_sources.credential", after.credential_enc) == new
    assert (after.kind, after.config, after.created_by, after.created_at) == (
        before.kind,
        before.config,
        before.created_by,
        before.created_at,
    )


def test_check_with_a_credential_that_no_longer_opens_fails_closed(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    monkeypatch.setattr(settings, "jwt_secret", "another-root-secret")
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    out = asyncio.run(
        app.handle_knowledge_source_check(
            KnowledgeSourceCheckInput(org=ORG, namespace=NS, name="design-decks")
        )
    )
    assert not out.reachable and out.summary.startswith("credential_invalid")
    assert len(fake.probes) == 1  # type: ignore[attr-defined]  # the add's only


def test_check_reports_the_probe(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    out = asyncio.run(
        app.handle_knowledge_source_check(
            KnowledgeSourceCheckInput(org=ORG, namespace=NS, name="design-decks")
        )
    )
    assert out.reachable and out.documents == 3 and out.checked_at
    assert fake.probes[-1][2] == SECRET  # type: ignore[attr-defined]


def test_remove_needs_the_name_and_keeps_documents_by_default(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    wrong = KnowledgeSourceRemoveInput(org=ORG, namespace=NS, name="design-decks", confirm="x")
    assert _code(app.handle_knowledge_source_remove(wrong)) == "CONFIRM_REQUIRED"
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
    )
    out = asyncio.run(app.handle_knowledge_source_remove(body))
    assert out.documents_pending == 0
    assert fake.rows[(ORG, NS, "design-decks")].active is False
    assert fake.states == {"design-decks": {"state_value": "", "detail": "{}"}}  # add's reset only
    # The name is free again.
    asyncio.run(app.handle_knowledge_source_add(_add()))


def test_remove_with_documents_marks_them_for_the_worker(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.states["design-decks"] = {"state_value": "stamp", "detail": json.dumps({"status": "ok"})}
    fake.documents["design-decks"] = 7
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks", delete_documents=True
    )
    assert asyncio.run(app.handle_knowledge_source_remove(body)).documents_pending == 7
    row = fake.rows[(ORG, NS, "design-decks")]
    assert (row.active, row.pending_delete, row.credential_enc) == (False, True, "")
    # The request is on the registry row; the sync state is left as it was.
    assert fake.states["design-decks"]["state_value"] == "stamp"
    assert json.loads(fake.states["design-decks"]["detail"]) == {"status": "ok"}


def test_a_delete_request_with_no_documents_yet_is_recorded_and_frees_the_name(
    fake: _Store,
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks", delete_documents=True
    )
    assert asyncio.run(app.handle_knowledge_source_remove(body)).documents_pending == 0
    # Recorded anyway: a sync turn running meanwhile may still write a document.
    assert fake.rows[(ORG, NS, "design-decks")].pending_delete is True
    # No document waits to be retired, so the name is free again.
    assert asyncio.run(app.handle_knowledge_source_add(_add())).name == "design-decks"


def test_sync_now_records_the_request_in_the_state_row(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    body = KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="design-decks", full=True)
    out = asyncio.run(app.handle_knowledge_source_sync_now(body))
    assert out.name == "design-decks"
    detail = json.loads(fake.states["design-decks"]["detail"])
    assert detail["full"] is True and detail["requested_at"]


def test_audit_subjects_carry_no_credential() -> None:
    add = OPERATIONS_BY_NAME["knowledge.source.add"]
    response = {"name": "design-decks", "kind": "gdrive", "config": {"folder_ids": [FOLDER]}}
    subject = add.audit_subject(_add(), response)  # type: ignore[misc]
    assert subject == {
        "org": ORG,
        "namespace": NS,
        "name": "design-decks",
        "kind": "gdrive",
        "config": {"folder_ids": [FOLDER]},
        "dry_run": False,
    }
    rotate = OPERATIONS_BY_NAME["knowledge.source.rotate"]
    body = KnowledgeSourceRotateInput(org=ORG, namespace=NS, name="design-decks", credential=SECRET)
    assert SECRET not in json.dumps(rotate.audit_subject(body, {"name": "design-decks"}))  # type: ignore[misc]


# ---- the gate --------------------------------------------------------------

ADMIN_OPS = (
    "knowledge.source.add",
    "knowledge.source.check",
    "knowledge.source.rotate",
    "knowledge.source.remove",
    "knowledge.source.sync_now",
)


@pytest.fixture
def no_grant_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(*_: Any) -> None:
        return None

    async def _false(*_: Any) -> bool:
        return False

    monkeypatch.setattr(authz, "_lookup_grant", _none)
    monkeypatch.setattr(authz, "claim_cap", _none)
    monkeypatch.setattr(authz, "may_be_restricted", _false)
    monkeypatch.setattr(authz, "is_org_readable_code_namespace", _false)


def _auth(role: Role, org: str = ORG) -> AuthInfo:
    return AuthInfo(
        subject=f"{role.name.lower()}@{org}",
        org=org,
        namespaces=(NS,),
        roles=(role,),
        is_service_account=False,
    )


@pytest.mark.parametrize("name", ADMIN_OPS)
def test_writes_and_check_need_the_namespace_admin(no_grant_reads: None, name: str) -> None:
    op = OPERATIONS_BY_NAME[name]
    assert op.required_role == Role.ADMIN and not op.skip_namespace_authz
    assert op.audit == (name != "knowledge.source.check")
    asyncio.run(check_authorized(_auth(Role.ADMIN), op.required_role, ORG, NS))
    for refused in (_auth(Role.VIEWER), _auth(Role.WRITER), _auth(Role.ADMIN, org="other")):
        with pytest.raises(PermissionError):
            asyncio.run(check_authorized(refused, op.required_role, ORG, NS))


@pytest.mark.parametrize("name", ["knowledge.source.list", "knowledge.source.inspect"])
def test_reads_are_open_to_the_repository_reader(no_grant_reads: None, name: str) -> None:
    op = OPERATIONS_BY_NAME[name]
    assert op.required_role == Role.VIEWER and op.read_only
    asyncio.run(check_authorized(_auth(Role.VIEWER), op.required_role, ORG, NS))
    with pytest.raises(PermissionError):
        asyncio.run(check_authorized(_auth(Role.VIEWER, org="other"), op.required_role, ORG, NS))


# ---- the statements ----------------------------------------------------------


def test_statements_read_final_on_the_key_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = (
        RecordingCH()
        .on("FROM loom.knowledge_sources", [])
        .on("FROM loom.code_index_state", [])
        .on("FROM loom.documents", [])
    )
    ch.install(monkeypatch)
    asyncio.run(store.list_sources(ORG, NS))
    asyncio.run(store.get_source(ORG, NS, "d"))
    asyncio.run(store.read_states(ORG, NS, ["d"]))
    asyncio.run(store.source_documents(ORG, NS, "d"))
    for st in ch.statements("FROM loom."):
        assert "FINAL" in st.sql and "org = {org:String} AND namespace = {ns:String}" in st.sql
    assert ch.statements("FROM loom.code_index_state")[0].params["keys"] == ["knowledge_source:d"]
    # A source's documents are those the sync worker wrote, never a caller's tagged ones.
    asyncio.run(store.count_documents(ORG, NS, "d"))
    for st in ch.statements("FROM loom.documents"):
        assert st.params["tag"] == "source:d"
        assert st.params["actor"] == domain.SYNC_WORKER_ACTOR == "worker:knowledge-source-sync"
        assert "actor = {actor:String}" in st.sql
    assert "LIMIT 200" in ch.statements("FROM loom.documents")[0].sql


def test_a_registry_write_is_a_full_row(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = RecordingCH()
    ch.install(monkeypatch)
    src = KnowledgeSource(
        org=ORG, namespace=NS, name="d", kind="gdrive", source_id=str(uuid.uuid4()), config={"a": 1}
    )
    asyncio.run(store.insert_source(src))
    [row] = ch.inserted("knowledge_sources")
    # Every column but `version`, which the column's DEFAULT (the server's clock) sets,
    # so the lease-serialized writes of every replica order by one clock.
    assert set(row) | {"version"} == set(column_names("knowledge_sources"))
    assert "version" not in row
    assert row["config_json"] == '{"a": 1}' and row["active"] is True


def test_a_sync_state_write_leaves_the_version_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = RecordingCH()
    ch.install(monkeypatch)
    asyncio.run(store.write_state(ORG, NS, "d", state_value="", detail={"requested_at": "t"}))
    [row] = ch.inserted("code_index_state")
    assert set(row) | {"version"} == set(column_names("code_index_state"))
    assert "version" not in row and row["state_key"] == "knowledge_source:d"


def test_sync_state_of_reports_only_reader_fields() -> None:
    detail = json.dumps(
        {"requested_at": "t", "full": True, "full_listed_at": "t0", "changed": "2", "removed": 1}
    )
    # The worker's bookkeeping stays out, and a field of the wrong type is left out.
    assert domain.sync_of(detail) == {"requested_at": "t", "removed": 1}
    assert domain.sync_of("not json") == {}


def test_rotate_does_not_bring_back_a_source_removed_during_its_probe(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))

    async def _probe_while_removed(*_: Any) -> source_probes.Probe:
        body = KnowledgeSourceRemoveInput(
            org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
        )
        await app.handle_knowledge_source_remove(body)
        return source_probes.Probe(documents=1)

    monkeypatch.setattr(source_probes, "probe", _probe_while_removed)
    body = KnowledgeSourceRotateInput(org=ORG, namespace=NS, name="design-decks", credential=SECRET)
    assert _code(app.handle_knowledge_source_rotate(body)) == "SOURCE_NOT_FOUND"
    row = fake.rows[(ORG, NS, "design-decks")]
    assert row.active is False and row.credential_enc == ""


def test_add_checks_the_name_again_after_its_probe(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _probe_while_added(*_: Any) -> source_probes.Probe:
        await fake.insert_source(
            KnowledgeSource(
                org=ORG,
                namespace=NS,
                name="design-decks",
                kind="gdrive",
                source_id=str(uuid.uuid4()),
            )
        )
        return source_probes.Probe(documents=1)

    monkeypatch.setattr(source_probes, "probe", _probe_while_added)
    assert _code(app.handle_knowledge_source_add(_add())) == "SOURCE_EXISTS"
    assert fake.rows[(ORG, NS, "design-decks")].created_by == ""  # the other add's row


def test_remove_clears_the_stored_credential(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
    )
    asyncio.run(app.handle_knowledge_source_remove(body))
    assert fake.rows[(ORG, NS, "design-decks")].credential_enc == ""


def test_a_name_with_documents_pending_deletion_cannot_be_added_again(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.documents["design-decks"] = 3
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks", delete_documents=True
    )
    asyncio.run(app.handle_knowledge_source_remove(body))
    assert _code(app.handle_knowledge_source_add(_add())) == "SOURCE_DELETE_PENDING"
    assert len(fake.probes) == 1  # type: ignore[attr-defined]  # the refused add never probed
    # Another name is free.
    asyncio.run(app.handle_knowledge_source_add(_add(name="design-decks-2")))
    # Once the documents are retired, the name is free too.
    fake.documents["design-decks"] = 0
    asyncio.run(app.handle_knowledge_source_add(_add()))


def test_a_sync_now_after_a_remove_leaves_the_delete_request(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # sync_now read the source; a remove with documents to retire lands before its lease.
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.documents["design-decks"] = 3
    real_lookup = app._source_in_repository

    async def _read_then_removed(org: str, ns: str, name: str) -> KnowledgeSource:
        src = await real_lookup(org, ns, name)
        body = KnowledgeSourceRemoveInput(
            org=org, namespace=ns, name=name, confirm=name, delete_documents=True
        )
        await app.handle_knowledge_source_remove(body)
        return src

    monkeypatch.setattr(app, "_source_in_repository", _read_then_removed)
    states = dict(fake.states)
    sync = KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="design-decks")
    assert _code(app.handle_knowledge_source_sync_now(sync)) == "SOURCE_NOT_FOUND"
    assert fake.states == states
    row = fake.rows[(ORG, NS, "design-decks")]
    assert (row.active, row.pending_delete) == (False, True)


def test_a_name_whose_kept_documents_remain_cannot_be_added_again(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.documents["design-decks"] = 4
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
    )
    asyncio.run(app.handle_knowledge_source_remove(body))
    with pytest.raises(ValidationError) as e:
        asyncio.run(app.handle_knowledge_source_add(_add()))
    assert e.value.to_detail()["code"] == "SOURCE_DOCUMENTS_KEPT"
    assert "knowledge.delete" in str(e.value) and "4 left" in str(e.value)


def test_add_checks_the_repository_again_after_its_probe(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _trunk_then_gone(org: str, ns: str) -> bool:
        calls.append(ns)
        return len(calls) == 1

    monkeypatch.setattr(app, "is_trunk_namespace", _trunk_then_gone)
    assert _code(app.handle_knowledge_source_add(_add())) == "NOT_A_CODE_NAMESPACE"
    assert calls == [NS, NS] and fake.rows == {}


def test_a_name_added_again_starts_with_a_fresh_sync_state(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.states["design-decks"] = {
        "state_value": "old-stamp",
        "detail": json.dumps({"status": "ok", "documents_seen": 9}),
    }
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
    )
    asyncio.run(app.handle_knowledge_source_remove(body))
    real_insert = fake.insert_source

    async def _insert_after_reset(src: KnowledgeSource) -> None:
        # The state is reset before the source goes active, never after.
        assert fake.states["design-decks"]["state_value"] == ""
        return await real_insert(src)

    fake.insert_source = _insert_after_reset  # type: ignore[method-assign]
    asyncio.run(app.handle_knowledge_source_add(_add()))
    assert fake.states["design-decks"] == {"state_value": "", "detail": "{}"}
    seen = asyncio.run(
        app.handle_knowledge_source_inspect(
            KnowledgeSourceInspectInput(org=ORG, namespace=NS, name="design-decks")
        )
    )
    assert seen.sync.model_dump() == app.KnowledgeSourceSync().model_dump()


def test_the_call_ledger_keeps_no_credential() -> None:
    # Only allow-listed argument names reach loom.agent_events (MCP and HTTP alike).
    from server.observability.event_args import IDENTIFIER_KEYS, TEXT_KEYS, project_args

    assert "credential" not in IDENTIFIER_KEYS | TEXT_KEYS
    args = project_args(_add().model_dump())
    assert "credential" not in args and SECRET not in json.dumps(args)


@pytest.mark.parametrize(
    "call",
    [
        lambda: app.handle_knowledge_source_check(
            KnowledgeSourceCheckInput(org=ORG, namespace=NS, name="design-decks")
        ),
        lambda: app.handle_knowledge_source_rotate(
            KnowledgeSourceRotateInput(
                org=ORG, namespace=NS, name="design-decks", credential=SECRET
            )
        ),
        lambda: app.handle_knowledge_source_sync_now(
            KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="design-decks")
        ),
    ],
    ids=["check", "rotate", "sync_now"],
)
def test_a_source_whose_namespace_is_no_trunk_is_refused(
    fake: _Store, monkeypatch: pytest.MonkeyPatch, call: Any
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    probes = len(fake.probes)  # type: ignore[attr-defined]

    async def _gone(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(app, "is_trunk_namespace", _gone)
    with pytest.raises(ValidationError) as e:
        asyncio.run(call())
    assert e.value.to_detail()["code"] == "NOT_A_CODE_NAMESPACE"
    assert "knowledge.source.remove retires it" in str(e.value)
    # Nothing probed or written; the source can still be read and removed.
    assert len(fake.probes) == probes and fake.states["design-decks"]["detail"] == "{}"  # type: ignore[attr-defined]
    seen = asyncio.run(
        app.handle_knowledge_source_inspect(
            KnowledgeSourceInspectInput(org=ORG, namespace=NS, name="design-decks")
        )
    )
    assert seen.name == "design-decks"
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
    )
    assert asyncio.run(app.handle_knowledge_source_remove(body)).name == "design-decks"


def test_rotate_refuses_a_source_removed_and_added_again_during_its_probe(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    first = fake.rows[(ORG, NS, "design-decks")]
    assert first.source_id
    # Added again within the same second, with the same kind and config: only the
    # registration id tells the two apart.
    readded = dataclasses.replace(first, source_id=str(uuid.uuid4()))
    assert readded.created_at == first.created_at

    async def _probe_while_replaced(*_: Any) -> source_probes.Probe:
        fake.rows[(ORG, NS, "design-decks")] = readded
        return source_probes.Probe(documents=1)

    monkeypatch.setattr(source_probes, "probe", _probe_while_replaced)
    body = KnowledgeSourceRotateInput(org=ORG, namespace=NS, name="design-decks", credential=SECRET)
    with pytest.raises(SourceReplacedError) as e:
        asyncio.run(app.handle_knowledge_source_rotate(body))
    assert e.value.to_detail()["code"] == "SOURCE_REPLACED" and e.value.status_code == 409
    # The source that holds the name now keeps its own credential.
    assert fake.rows[(ORG, NS, "design-decks")] is readded


def test_the_trunk_check_reads_the_cached_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import repo_registry
    from server.code.domain.index import repos

    calls: list[dict[str, Any]] = []

    async def _refresh(**kw: Any) -> list[Any]:
        calls.append(kw)
        return []

    monkeypatch.setattr(repo_registry, "refresh_db_repos", _refresh)
    monkeypatch.setattr(repos, "docs_namespaces", lambda: {(ORG, "acme/app"): NS})
    assert asyncio.run(app.is_trunk_namespace(ORG, NS)) is True
    assert asyncio.run(app.is_trunk_namespace(ORG, "code-acme-app-dev")) is False
    assert asyncio.run(app.is_trunk_namespace("other", NS)) is False
    # No forced read per call: the snapshot refreshes on its own schedule.
    assert calls == [{}, {}, {}]


def test_registration_is_read_from_the_registry_row_for_a_registered_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.code.domain.index import repos

    entries = {
        "code-reg": SimpleNamespace(source="registry"),
        "code-cfg": SimpleNamespace(source="config"),
    }
    monkeypatch.setattr(repos, "repo_for_namespace", lambda org, ns: entries.get(ns))
    reads: list[str] = []

    async def _active(org: str, ns: str) -> bool:
        reads.append(ns)
        return False

    from server.code.app.index import repo_registry

    monkeypatch.setattr(repo_registry, "row_active", _active)
    assert asyncio.run(app._registered(ORG, "code-reg")) is False
    # The operator's configuration owns this one; code.repo.remove cannot retire it.
    assert asyncio.run(app._registered(ORG, "code-cfg")) is True
    assert asyncio.run(app._registered(ORG, "code-none")) is False
    assert reads == ["code-reg"]


LEASE = f"knowledge-source:{ORG}:{NS}"


def test_each_registry_write_holds_the_namespace_lease(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    assert fake.leases == [LEASE]  # type: ignore[attr-defined]
    rotate = KnowledgeSourceRotateInput(
        org=ORG, namespace=NS, name="design-decks", credential=SECRET
    )
    asyncio.run(app.handle_knowledge_source_rotate(rotate))
    asyncio.run(
        app.handle_knowledge_source_check(
            KnowledgeSourceCheckInput(org=ORG, namespace=NS, name="design-decks")
        )
    )
    asyncio.run(
        app.handle_knowledge_source_sync_now(
            KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="design-decks")
        )
    )
    remove = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
    )
    asyncio.run(app.handle_knowledge_source_remove(remove))
    # add, rotate, sync_now and remove; check writes nothing.
    assert fake.leases == [LEASE] * 4  # type: ignore[attr-defined]
    # A dry run probes and writes nothing, so it takes none.
    asyncio.run(app.handle_knowledge_source_add(_add(name="other", dry_run=True)))
    assert len(fake.leases) == 4  # type: ignore[attr-defined]


def _write_calls() -> list[Any]:
    return [
        lambda: app.handle_knowledge_source_add(_add(name="other")),
        lambda: app.handle_knowledge_source_rotate(
            KnowledgeSourceRotateInput(
                org=ORG, namespace=NS, name="design-decks", credential=SECRET
            )
        ),
        lambda: app.handle_knowledge_source_remove(
            KnowledgeSourceRemoveInput(
                org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
            )
        ),
        lambda: app.handle_knowledge_source_sync_now(
            KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="design-decks")
        ),
    ]


@pytest.mark.parametrize("which", [0, 1, 2, 3], ids=["add", "rotate", "remove", "sync_now"])
def test_a_busy_lease_refuses_and_writes_nothing(
    fake: _Store, monkeypatch: pytest.MonkeyPatch, which: int
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    before = dict(fake.rows)

    @contextlib.asynccontextmanager
    async def _busy(name: str, **kw: Any) -> AsyncIterator[bool]:
        yield False

    monkeypatch.setattr(leases, "holding", _busy)
    states = dict(fake.states)
    with pytest.raises(OverloadedError):
        asyncio.run(_write_calls()[which]())
    assert fake.rows == before and fake.states == states


@pytest.mark.parametrize("which", [0, 1, 2, 3], ids=["add", "rotate", "remove", "sync_now"])
def test_a_lease_store_that_cannot_answer_refuses(
    fake: _Store, monkeypatch: pytest.MonkeyPatch, which: int
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    before = dict(fake.rows)

    @contextlib.asynccontextmanager
    async def _down(name: str, **kw: Any) -> AsyncIterator[bool]:
        raise leases.LeaseUnavailable("the lease store did not answer")
        yield True  # pragma: no cover

    monkeypatch.setattr(leases, "holding", _down)
    states = dict(fake.states)
    with pytest.raises(leases.LeaseUnavailable):
        asyncio.run(_write_calls()[which]())
    assert fake.rows == before and fake.states == states


def test_add_re_checks_registration_under_the_lease(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _removed(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(app, "_registered", _removed)
    assert _code(app.handle_knowledge_source_add(_add())) == "NOT_A_CODE_NAMESPACE"
    assert fake.rows == {} and fake.leases == [LEASE]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("rows", "active"), [([{"active": 1}], True), ([{"active": 0}], False), ([], False)]
)
def test_registration_reads_the_code_repos_row(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]], active: bool
) -> None:
    from server.code.app.index import repo_registry

    ch = RecordingCH().on("FROM loom.code_repos", rows)
    ch.install(monkeypatch)
    assert asyncio.run(repo_registry.row_active(ORG, NS)) is active
    [st] = ch.statements("FROM loom.code_repos")
    assert "FINAL" in st.sql and st.params == {"org": ORG, "ns": NS}


def test_a_sync_request_for_a_replaced_source_is_not_recorded_on_the_new_one(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.states["design-decks"] = {"state_value": "old-stamp", "detail": "{}"}
    real_lookup = app._source_in_repository

    async def _read_then_replaced(org: str, ns: str, name: str) -> KnowledgeSource:
        # sync_now read the first source; a remove and a new add land before its lease.
        src = await real_lookup(org, ns, name)
        remove = KnowledgeSourceRemoveInput(org=org, namespace=ns, name=name, confirm=name)
        await app.handle_knowledge_source_remove(remove)
        await app.handle_knowledge_source_add(_add())
        return src

    monkeypatch.setattr(app, "_source_in_repository", _read_then_replaced)
    body = KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="design-decks")
    with pytest.raises(SourceReplacedError, match="sync_now again for the current source"):
        asyncio.run(app.handle_knowledge_source_sync_now(body))
    assert fake.states["design-decks"]["state_value"] == ""


def test_remove_counts_documents_with_sequential_consistency(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.ch import sequential_reads_active

    asyncio.run(app.handle_knowledge_source_add(_add()))
    seen: list[bool] = []

    async def _count(org: str, ns: str, name: str) -> int:
        seen.append(sequential_reads_active())
        return 2

    fake.count_documents = _count  # type: ignore[method-assign]
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks", delete_documents=True
    )
    assert asyncio.run(app.handle_knowledge_source_remove(body)).documents_pending == 2
    assert seen == [True]


def test_each_registration_gets_its_own_id_carried_by_later_writes(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    first = fake.rows[(ORG, NS, "design-decks")].source_id
    uuid.UUID(first)
    rotate = KnowledgeSourceRotateInput(
        org=ORG, namespace=NS, name="design-decks", credential=SECRET
    )
    asyncio.run(app.handle_knowledge_source_rotate(rotate))
    assert fake.rows[(ORG, NS, "design-decks")].source_id == first
    remove = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
    )
    asyncio.run(app.handle_knowledge_source_remove(remove))
    assert fake.rows[(ORG, NS, "design-decks")].source_id == first
    asyncio.run(app.handle_knowledge_source_add(_add()))
    assert fake.rows[(ORG, NS, "design-decks")].source_id not in ("", first)


def test_a_name_with_a_trailing_newline_is_no_valid_name() -> None:
    # `$` would accept "name\n"; the rule is matched whole.
    assert domain.valid_name("design-decks")
    assert not domain.valid_name("design-decks\n")
