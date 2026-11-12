"""knowledge.source.*: the document-source registry ops, over an in-memory store
and a stub probe, and the ADMIN gate on every write and on `check`."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

import server.auth.authz as authz
from server.auth.authz import Role, check_authorized
from server.auth.jwt import AuthInfo
from server.core import leases
from server.core.ch import sequential_reads_active
from server.core.errors import (
    ErasureRunningError,
    NotFoundError,
    OverloadedError,
    SourceReplacedError,
    ValidationError,
)
from server.core.principals import personal_name
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
from tests.unit.clock_fake import ManualClock
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
        self.inactive: set[tuple[str, str]] = set()
        self.claimed: set[str] = set()
        # Whether each namespace read was sequential.
        self.sequential: list[bool] = []

    async def inactive_namespaces(self, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        self.sequential.append(sequential_reads_active())
        return {p for p in pairs if p in self.inactive}

    async def directory_row(self, org: str, namespace: str) -> store.DirectoryRow:
        self.sequential.append(sequential_reads_active())
        inactive = (org, namespace) in self.inactive
        return store.DirectoryRow(inactive, not inactive and namespace in self.claimed)

    async def sources_in(self, org: str, names: list[str]) -> list[tuple[str, str]]:
        self.sequential.append(sequential_reads_active())
        found = [(n, name) for (o, n, name) in self.rows if o == org]
        return sorted(f for f in found if f[0] in names)

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

    monkeypatch.setattr(source_probes, "probe", _probe)
    leases_taken: list[str] = []

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any) -> AsyncIterator[bool]:
        leases_taken.append(name)
        yield True

    watched: list[str] = []

    async def _has_live_holder(name: str, **_kw: Any) -> bool:
        watched.append(name)
        return False

    monkeypatch.setattr(leases, "holding", _holding)
    monkeypatch.setattr(leases, "has_live_holder", _has_live_holder)
    erasures: list[dict[str, Any]] = []

    async def _open(org: str) -> list[dict[str, Any]]:
        return list(erasures)

    from server.erasure.app import ledger_reads

    monkeypatch.setattr(ledger_reads, "open_namespace_erasures", _open)
    fake.erasures = erasures  # type: ignore[attr-defined]
    fake.probes = probes  # type: ignore[attr-defined]
    fake.leases = leases_taken  # type: ignore[attr-defined]
    fake.watched = watched  # type: ignore[attr-defined]
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


def _after_first_read(fake: _Store, change: Any) -> None:
    """Run `change` right after the next read of a source, as a concurrent call would."""
    read = fake.get_source

    async def _read_then_change(org: str, ns: str, name: str) -> KnowledgeSource | None:
        fake.get_source = read  # type: ignore[method-assign]
        src = await read(org, ns, name)
        await change(org, ns, name)
        return src

    fake.get_source = _read_then_change  # type: ignore[method-assign]


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
    assert fake.rows[(ORG, NS, "design-decks")].config == out.config
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


def test_a_source_retired_with_its_namespace_can_still_have_its_documents_retired(
    fake: _Store,
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    asyncio.run(app.deactivate_namespace_sources(ORG, NS))
    fake.documents["design-decks"] = 3
    keep = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks"
    )
    assert _code(app.handle_knowledge_source_remove(keep)) == "SOURCE_NOT_FOUND"
    body = keep.model_copy(update={"delete_documents": True})
    assert asyncio.run(app.handle_knowledge_source_remove(body)).documents_pending == 3
    row = fake.rows[(ORG, NS, "design-decks")]
    assert (row.active, row.pending_delete) == (False, True)


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


def test_add_is_refused_while_an_erasure_of_its_namespace_runs(fake: _Store) -> None:
    erasures = fake.erasures  # type: ignore[attr-defined]
    erasures.append({"scope_type": "namespace_list", "scope_value": '{"namespaces": ["other"]}'})
    asyncio.run(app.handle_knowledge_source_add(_add(name="first")))
    erasures.append({"scope_type": "namespace", "scope_value": NS})
    with pytest.raises(ErasureRunningError):
        asyncio.run(app.handle_knowledge_source_add(_add(name="second")))
    assert len(fake.probes) == 1  # type: ignore[attr-defined]  # the refused add never probed


def test_an_erasure_that_starts_during_the_probe_refuses_the_add_under_its_lease(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    erasures = fake.erasures  # type: ignore[attr-defined]

    async def _probe(kind: str, config: dict[str, Any], credential: str) -> source_probes.Probe:
        erasures.append({"scope_type": "namespace", "scope_value": NS})
        return source_probes.Probe(documents=3, sample=["Deck"])

    monkeypatch.setattr(source_probes, "probe", _probe)
    with pytest.raises(ErasureRunningError):
        asyncio.run(app.handle_knowledge_source_add(_add()))
    assert not fake.rows


def test_sync_now_refuses_a_source_of_an_inactive_namespace(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.inactive.add((ORG, NS))
    body = KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="design-decks")
    assert _code(app.handle_knowledge_source_sync_now(body)) == "NAMESPACE_INACTIVE"
    assert "requested_at" not in json.loads(fake.states["design-decks"]["detail"])


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
def test_reads_are_open_to_the_namespace_reader(no_grant_reads: None, name: str) -> None:
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

    async def _remove(org: str, ns: str, name: str) -> None:
        body = KnowledgeSourceRemoveInput(
            org=org, namespace=ns, name=name, confirm=name, delete_documents=True
        )
        await app.handle_knowledge_source_remove(body)

    _after_first_read(fake, _remove)
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
    assert "delete_documents=true" in str(e.value) and "4 left" in str(e.value)


def test_a_source_lives_on_any_namespace(fake: _Store) -> None:
    out = asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-notes")))
    assert out.namespace == "team-notes"
    assert fake.rows[(ORG, "team-notes", "design-decks")].active


def test_add_refuses_an_inactive_namespace_before_its_probe(fake: _Store) -> None:
    fake.inactive.add((ORG, "team-notes"))
    assert _code(app.handle_knowledge_source_add(_add(namespace="team-notes"))) == (
        "NAMESPACE_INACTIVE"
    )
    assert fake.probes == [] and fake.rows == {}  # type: ignore[attr-defined]


def test_add_checks_the_namespace_again_after_its_probe(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _deleted_during_probe(*_: Any) -> source_probes.Probe:
        fake.inactive.add((ORG, "team-notes"))
        return source_probes.Probe(documents=1)

    monkeypatch.setattr(source_probes, "probe", _deleted_during_probe)
    assert _code(app.handle_knowledge_source_add(_add(namespace="team-notes"))) == (
        "NAMESPACE_INACTIVE"
    )
    assert fake.rows == {}


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


def test_a_sync_request_for_a_replaced_source_is_not_recorded_on_the_new_one(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.states["design-decks"] = {"state_value": "old-stamp", "detail": "{}"}

    async def _replace(org: str, ns: str, name: str) -> None:
        # sync_now read the first source; a remove and a new add land before its lease.
        remove = KnowledgeSourceRemoveInput(org=org, namespace=ns, name=name, confirm=name)
        await app.handle_knowledge_source_remove(remove)
        await app.handle_knowledge_source_add(_add())

    _after_first_read(fake, _replace)
    body = KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="design-decks")
    with pytest.raises(SourceReplacedError, match="sync_now again for the current source"):
        asyncio.run(app.handle_knowledge_source_sync_now(body))
    assert fake.states["design-decks"]["state_value"] == ""


def test_remove_counts_documents_with_sequential_consistency(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_rotate_serves_a_source_on_a_plain_namespace(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-notes")))
    body = KnowledgeSourceRotateInput(
        org=ORG, namespace="team-notes", name="design-decks", credential=SECRET
    )
    assert asyncio.run(app.handle_knowledge_source_rotate(body)).name == "design-decks"
    assert len(fake.probes) == 2  # type: ignore[attr-defined]


def test_rotate_stores_no_credential_on_a_source_whose_namespace_is_gone(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-notes")))
    before = fake.rows[(ORG, "team-notes", "design-decks")].credential_enc
    fake.inactive.add((ORG, "team-notes"))
    body = KnowledgeSourceRotateInput(
        org=ORG, namespace="team-notes", name="design-decks", credential=SECRET
    )
    probes = len(fake.probes)  # type: ignore[attr-defined]
    assert _code(app.handle_knowledge_source_rotate(body)) == "NAMESPACE_INACTIVE"
    assert fake.rows[(ORG, "team-notes", "design-decks")].credential_enc == before
    # Refused before the probe, as add is.
    assert len(fake.probes) == probes  # type: ignore[attr-defined]


def test_rotate_reads_the_namespace_again_sequentially_under_its_lease(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-notes")))
    fake.sequential.clear()
    body = KnowledgeSourceRotateInput(
        org=ORG, namespace="team-notes", name="design-decks", credential=SECRET
    )
    asyncio.run(app.handle_knowledge_source_rotate(body))
    assert fake.sequential == [False, True]


def test_deactivating_a_namespace_s_sources_clears_their_credentials(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-notes")))
    assert asyncio.run(app.deactivate_namespace_sources(ORG, "team-notes")) == ["design-decks"]
    row = fake.rows[(ORG, "team-notes", "design-decks")]
    assert (row.active, row.credential_enc) == (False, "")


def test_a_namespace_delete_retires_its_sources(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.app import namespace as ns_app

    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-notes")))
    taken: list[str] = fake.leases  # type: ignore[attr-defined]

    @contextlib.asynccontextmanager
    async def _claim(*_a: Any) -> AsyncIterator[None]:
        taken.append("account")
        yield

    async def _row(*_a: Any, **_k: Any) -> dict[str, Any] | None:
        return None  # a namespace with no directory row still loses its sources

    monkeypatch.setattr(ns_app, "account_change", _claim)
    monkeypatch.setattr(ns_app.namespaces_repo, "fetch_row", _row)
    taken.clear()
    out = asyncio.run(ns_app.delete_namespace(ORG, "team-notes"))
    assert (out.deleted, out.sources_retired) == (False, ["design-decks"])
    row = fake.rows[(ORG, "team-notes", "design-decks")]
    assert (row.active, row.credential_enc) == (False, "")
    # The sources lease, then the account's claim, as code.repo.remove takes them.
    assert taken == [f"knowledge-source:{ORG}:team-notes", "account"]


@pytest.mark.parametrize(
    "own",
    ["personal-bob", "personal-bob-1a2b3c", "u-bob-0a1b2c3d", personal_name(ORG, "user:bob")],
)
def test_a_person_s_own_place_holds_no_source(fake: _Store, own: str) -> None:
    assert _code(app.handle_knowledge_source_add(_add(namespace=own))) == "PERSONAL_NAMESPACE"
    assert fake.probes == []  # type: ignore[attr-defined]


def test_a_namespace_a_person_claimed_at_signup_holds_no_source(fake: _Store) -> None:
    fake.claimed.add("alice-notes")
    assert _code(app.handle_knowledge_source_add(_add(namespace="alice-notes"))) == (
        "PERSONAL_NAMESPACE"
    )
    assert fake.probes == []  # type: ignore[attr-defined]


def test_the_source_list_tells_a_persons_own_place(fake: _Store) -> None:
    fake.claimed.add("alice-notes")

    def accepts(namespace: str) -> bool:
        listed = asyncio.run(
            app.handle_knowledge_source_list(KnowledgeSourceListInput(org=ORG, namespace=namespace))
        )
        return listed.accepts_sources

    assert accepts(NS)
    assert not accepts("alice-notes")
    assert not accepts("personal-alice")
    assert not accepts("col-me-alice")
    fake.inactive.add((ORG, "retired"))
    assert not accepts("retired")


def test_a_collection_the_tree_no_longer_holds_takes_no_source(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _in_tree(org: str, ns: str) -> bool:
        return ns == "col-kept"

    from server.identity.app import collections

    monkeypatch.setattr(collections, "in_tree", _in_tree)
    assert _code(app.handle_knowledge_source_add(_add(namespace="col-gone"))) == (
        "NAMESPACE_INACTIVE"
    )
    assert asyncio.run(app.handle_knowledge_source_add(_add(namespace="col-kept"))).name


def test_a_namespace_erasure_retires_the_sources_of_every_namespace(fake: _Store) -> None:
    from server.erasure.app import saga_steps

    for ns in ("team-a", "team-b"):
        asyncio.run(app.handle_knowledge_source_add(_add(namespace=ns)))
    fake.leases.clear()  # type: ignore[attr-defined]
    fake.sequential.clear()
    erased = ["team-a", "team-b", "team-empty"]
    retired = asyncio.run(saga_steps._step_retire_knowledge_sources_by_ns(ORG, erased))
    assert retired == 2
    assert not any(src.active or src.credential_enc for src in fake.rows.values())
    # Every namespace's source lease is watched until no add holds it, one with no source
    # too: an add that holds it lands before the sequential read, and a later one sees the
    # erasure. Only namespaces with a source take the lease. Then each source's sync turn
    # lease is watched until free, so no turn running outlives the step.
    assert fake.sequential == [True]
    assert fake.leases == [  # type: ignore[attr-defined]
        f"knowledge-source:{ORG}:team-a",
        f"knowledge-source:{ORG}:team-b",
    ]
    assert fake.watched == [  # type: ignore[attr-defined]
        f"knowledge-source:{ORG}:team-a",
        f"knowledge-source:{ORG}:team-b",
        f"knowledge-source:{ORG}:team-empty",
        f"knowledge-source-sync:{ORG}/team-a/design-decks",
        f"knowledge-source-sync:{ORG}/team-b/design-decks",
    ]


def _turn(name: str, namespace: str = "team-a") -> str:
    return f"knowledge-source-sync:{ORG}/{namespace}/{name}"


def _clocked(monkeypatch: pytest.MonkeyPatch, busy: dict[str, float]) -> ManualClock:
    """A manual clock for `retire_sources_of`, and leases (by full name) held until
    `busy`'s times."""
    clock = ManualClock(t=100.0)
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(app, "asyncio", SimpleNamespace(sleep=clock.async_sleep))

    async def _has_live_holder(name: str, **_kw: Any) -> bool:
        return clock.t < busy.get(name, 0.0)

    monkeypatch.setattr(leases, "has_live_holder", _has_live_holder)
    return clock


def test_the_erasure_step_s_turn_waits_share_one_deadline(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("decks", "notes"):
        asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-a", name=name)))
    # decks's turn gives its lease up after 20 s; notes's never does.
    clock = _clocked(monkeypatch, {_turn("decks"): 120.0, _turn("notes"): float("inf")})
    with pytest.raises(OverloadedError):
        asyncio.run(app.retire_sources_of(ORG, ["team-a"], wait_s=30.0))
    assert clock.t == 130.0


def test_the_erasure_step_waits_less_than_the_recovery_workers_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.app import saga_steps
    from server.workers.registry import pass_deadline_seconds

    waits: list[float] = []

    async def _retire(org: str, namespaces: list[str], *, wait_s: float) -> int:
        waits.append(wait_s)
        return 0

    monkeypatch.setattr(saga_steps, "retire_sources_of", _retire)
    asyncio.run(saga_steps._step_retire_knowledge_sources_by_ns(ORG, ["team-a"]))
    # A turn still running fails the step; the saga's retries wait it out.
    assert waits and waits[0] < pass_deadline_seconds("erasure_recovery") / 2


def test_the_erasure_step_waits_out_a_running_turn_and_its_retry_waits_again(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-a")))
    busy = {_turn("design-decks"): float("inf")}
    clock = _clocked(monkeypatch, busy)
    with pytest.raises(OverloadedError):
        asyncio.run(app.retire_sources_of(ORG, ["team-a"], wait_s=1.0))
    assert not any(src.active for src in fake.rows.values())
    # The retry finds no active source, and still waits for the turn until it ends.
    busy[_turn("design-decks")] = clock.t + 0.5
    assert asyncio.run(app.retire_sources_of(ORG, ["team-a"], wait_s=1.0)) == 0
    assert clock.sleeps[-1] == 0.5


def test_the_erasure_step_waits_for_a_source_an_older_build_added_after_its_read(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An older build's add takes the lease but reads no ledger, so it can land between
    # the step's read and its lease: the re-list under the lease waits for its turn too.
    for name in ("decks", "notes"):
        asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-a", name=name)))
    read = fake.sources_in

    async def _before_notes(org: str, names: list[str]) -> list[tuple[str, str]]:
        return [row for row in await read(org, names) if row[1] == "decks"]

    monkeypatch.setattr(fake, "sources_in", _before_notes)
    _clocked(monkeypatch, {_turn("notes"): float("inf")})
    with pytest.raises(OverloadedError):
        asyncio.run(app.retire_sources_of(ORG, ["team-a"], wait_s=1.0))
    assert not any(src.active for src in fake.rows.values())


def test_add_reads_the_namespace_again_sequentially_under_its_lease(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-notes")))
    # Before the probe any replica will do; under the lease, the read sees every write.
    assert fake.sequential == [False, True]


def test_the_erasure_step_waits_while_an_add_holds_the_sources_lease(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An add holding the lease may have checked the ledger before the erasure: the step
    # reads the sources only once it lets go, and fails (retried) while it holds on.
    held = f"knowledge-source:{ORG}:team-a"
    busy = {held: float("inf")}
    clock = _clocked(monkeypatch, busy)
    with pytest.raises(OverloadedError, match="document sources"):
        asyncio.run(app.retire_sources_of(ORG, ["team-a"], wait_s=1.0))
    assert clock.t == 101.0
    assert fake.sequential == []  # the sources were never read
    # Once it lets go, the read follows and sees what the add wrote.
    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-a")))
    fake.sequential.clear()
    busy[held] = clock.t + 0.5
    assert asyncio.run(app.retire_sources_of(ORG, ["team-a"], wait_s=1.0)) == 1
    assert fake.sequential == [True]


def test_the_erasure_step_fails_at_its_deadline_while_the_retirement_lease_is_held(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-a")))
    # The lease is free when the step looks, then another change takes it for good: the
    # retirement waits out what is left of the 30 s, not 5 s more.
    clock = _clocked(monkeypatch, {})

    @contextlib.asynccontextmanager
    async def _held_elsewhere(name: str, *, wait_s: float, **_kw: Any) -> AsyncIterator[bool]:
        await clock.async_sleep(wait_s)
        yield False

    monkeypatch.setattr(leases, "holding", _held_elsewhere)
    with pytest.raises(OverloadedError):
        asyncio.run(app.retire_sources_of(ORG, ["team-a"], wait_s=30.0))
    assert clock.t == 130.0
