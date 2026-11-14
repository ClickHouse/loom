"""knowledge.source.*: the document-source registry ops, over an in-memory store
and a stub probe, and the ADMIN gate on every write and on `check`."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import server.auth.authz as authz
from server.auth.authz import Role, check_authorized
from server.auth.jwt import AuthInfo
from server.core import leases
from server.core.ch import sequential_reads_active
from server.core.errors import (
    BadConfigError,
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
    KnowledgeSourceParseLinksInput,
    KnowledgeSourceRemoveInput,
    KnowledgeSourceRotateInput,
    KnowledgeSourceSyncNowInput,
    KnowledgeSourceUpdateInput,
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
        # What the fixture's fakes saw: each probe (kind, config, credential) and its
        # items, the leases taken and watched, and the open erasures to answer.
        self.probes: list[tuple[str, dict[str, Any], str]] = []
        self.probed_items: list[Any] = []
        self.leases: list[str] = []
        self.watched: list[str] = []
        self.erasures: list[dict[str, Any]] = []

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

    async def _probe(
        kind: str, config: dict[str, Any], credential: str, *, items: Any = None
    ) -> source_probes.Probe:
        fake.probes.append((kind, config, credential))
        fake.probed_items.append(items)
        return source_probes.Probe(documents=3, sample=["Deck"], items=items or [])

    monkeypatch.setattr(source_probes, "probe", _probe)

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any) -> AsyncIterator[bool]:
        fake.leases.append(name)
        yield True

    async def _has_live_holder(name: str, **_kw: Any) -> bool:
        fake.watched.append(name)
        return False

    monkeypatch.setattr(leases, "holding", _holding)
    monkeypatch.setattr(leases, "has_live_holder", _has_live_holder)

    async def _open(org: str) -> list[dict[str, Any]]:
        return list(fake.erasures)

    from server.erasure.app import ledger_reads

    monkeypatch.setattr(ledger_reads, "open_namespace_erasures", _open)
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
    assert src.kind == "gdrive" and src.active and domain.id_list_config(src) == out.config
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
    assert fake.probes[-1][0] == "notion"


def test_a_dry_run_probes_and_writes_nothing(fake: _Store) -> None:
    out = asyncio.run(app.handle_knowledge_source_add(_add(dry_run=True)))
    assert out.dry_run
    assert fake.rows == {} and len(fake.probes) == 1


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
    assert fake.rows == {} and fake.probes == []


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
    async def _fail(*_: Any, **__: Any) -> source_probes.Probe:
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
    assert fake.rows[(ORG, NS, "design-decks")].config == {"include": ["pdf", "docx"]}
    assert fake.probes[-1][1]["include"] == ["pdf", "docx"]


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
    assert len(fake.probes) == 1  # the add's only


def test_check_reports_the_probe(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    out = asyncio.run(
        app.handle_knowledge_source_check(
            KnowledgeSourceCheckInput(org=ORG, namespace=NS, name="design-decks")
        )
    )
    assert out.reachable and out.documents == 3 and out.checked_at
    assert fake.probes[-1][2] == SECRET


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
        lambda: app.handle_knowledge_source_update(
            KnowledgeSourceUpdateInput(org=ORG, namespace=NS, name="design-decks", include=["pdf"])
        ),
    ],
)
def test_a_mirrored_source_is_refused(fake: _Store, call: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    key = (ORG, NS, "design-decks")
    fake.rows[key] = dataclasses.replace(fake.rows[key], access="mirrored")
    assert _code(call()) == "SOURCE_NOT_SERVED"


def test_a_mirrored_source_stays_mirrored_when_removed_or_retired(fake: _Store) -> None:
    # The later build retires its documents from the mirror by this value.
    for name in ("design-decks", "other"):
        asyncio.run(app.handle_knowledge_source_add(_add(name=name)))
        key = (ORG, NS, name)
        fake.rows[key] = dataclasses.replace(fake.rows[key], access="mirrored")
    body = KnowledgeSourceRemoveInput(
        org=ORG, namespace=NS, name="design-decks", confirm="design-decks", delete_documents=True
    )
    asyncio.run(app.handle_knowledge_source_remove(body))
    asyncio.run(app.deactivate_namespace_sources(ORG, NS))
    for name in ("design-decks", "other"):
        row = fake.rows[(ORG, NS, name)]
        assert not row.active and row.access == "mirrored"


def test_a_removed_mirrored_source_s_delete_request_outlasts_an_add_of_its_name(
    fake: _Store,
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    key = (ORG, NS, "design-decks")
    fake.rows[key] = dataclasses.replace(
        fake.rows[key], access="mirrored", active=False, pending_delete=True
    )
    assert _code(app.handle_knowledge_source_add(_add())) == "SOURCE_NOT_SERVED"
    row = fake.rows[key]
    assert row.access == "mirrored" and row.pending_delete


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
    erasures = fake.erasures
    erasures.append({"scope_type": "namespace_list", "scope_value": '{"namespaces": ["other"]}'})
    asyncio.run(app.handle_knowledge_source_add(_add(name="first")))
    erasures.append({"scope_type": "namespace", "scope_value": NS})
    with pytest.raises(ErasureRunningError):
        asyncio.run(app.handle_knowledge_source_add(_add(name="second")))
    assert len(fake.probes) == 1  # the refused add never probed


def test_an_erasure_that_starts_during_the_probe_refuses_the_add_under_its_lease(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    erasures = fake.erasures

    async def _probe(
        kind: str, config: dict[str, Any], credential: str, **_: Any
    ) -> source_probes.Probe:
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
    items = [{"type": "folder", "id": FOLDER, "recursive": False}]
    response = {
        "name": "design-decks",
        "kind": "gdrive",
        "items": items,
        "include": ["docs"],
        "config": {"folder_ids": [FOLDER]},
    }
    subject = add.audit_subject(_add(), response)  # type: ignore[misc]
    assert subject == {
        "org": ORG,
        "namespace": NS,
        "name": "design-decks",
        "kind": "gdrive",
        "items": items,
        "include": ["docs"],
        "config": {"folder_ids": [FOLDER]},
        "dry_run": False,
    }
    rotate = OPERATIONS_BY_NAME["knowledge.source.rotate"]
    body = KnowledgeSourceRotateInput(org=ORG, namespace=NS, name="design-decks", credential=SECRET)
    assert SECRET not in json.dumps(rotate.audit_subject(body, {"name": "design-decks"}))  # type: ignore[misc]


# ---- the gate --------------------------------------------------------------

ADMIN_OPS = (
    "knowledge.source.add",
    "knowledge.source.update",
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


@pytest.mark.parametrize(
    "name", ["knowledge.source.list", "knowledge.source.inspect", "knowledge.source.parse_links"]
)
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
    config = {"folder_ids": [FOLDER], "include": ["docs"]}
    src = KnowledgeSource(
        org=ORG, namespace=NS, name="d", kind="gdrive", source_id=str(uuid.uuid4()),
        config=config, items=domain.items_from_config("gdrive", config),
    )  # fmt: skip
    asyncio.run(store.insert_source(src))
    [row] = ch.inserted("knowledge_sources")
    # Every column but `version`, which the column's DEFAULT (the server's clock) sets,
    # so the lease-serialized writes of every replica order by one clock.
    assert set(row) | {"version"} == set(column_names("knowledge_sources"))
    assert "version" not in row
    assert json.loads(row["config_json"]) == config and row["active"] is True


def test_a_mirrored_source_is_stored_where_a_build_without_items_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = RecordingCH()
    ch.install(monkeypatch)
    config = {"folder_ids": [FOLDER]}
    src = KnowledgeSource(
        org=ORG, namespace=NS, name="d", kind="gdrive", source_id=str(uuid.uuid4()),
        config=config, items=domain.items_from_config("gdrive", config), access="mirrored",
    )  # fmt: skip
    asyncio.run(store.insert_source(app._with_credential(src, SECRET)))
    [row] = ch.inserted("knowledge_sources")
    assert row["kind"] == "" and row["source_kind"] == "gdrive"
    assert row["credential_enc"] == "" and row["secret_enc"] and row["access"] == "mirrored"
    # Read back, it is still mirrored; an empty value reads `namespace`.
    assert store._source(row).access == "mirrored"
    assert store._source({**row, "access": ""}).access == "namespace"


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

    async def _probe_while_removed(*_: Any, **__: Any) -> source_probes.Probe:
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
    async def _probe_while_added(*_: Any, **__: Any) -> source_probes.Probe:
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
    assert len(fake.probes) == 1  # the refused add never probed
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
    assert fake.probes == [] and fake.rows == {}


def test_add_checks_the_namespace_again_after_its_probe(
    fake: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _deleted_during_probe(*_: Any, **__: Any) -> source_probes.Probe:
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

    async def _probe_while_replaced(*_: Any, **__: Any) -> source_probes.Probe:
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
    assert fake.leases == [LEASE]
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
    assert fake.leases == [LEASE] * 4
    # A dry run probes and writes nothing, so it takes none.
    asyncio.run(app.handle_knowledge_source_add(_add(name="other", dry_run=True)))
    assert len(fake.leases) == 4


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
    assert len(fake.probes) == 2


def test_rotate_stores_no_credential_on_a_source_whose_namespace_is_gone(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add(namespace="team-notes")))
    before = fake.rows[(ORG, "team-notes", "design-decks")].credential_enc
    fake.inactive.add((ORG, "team-notes"))
    body = KnowledgeSourceRotateInput(
        org=ORG, namespace="team-notes", name="design-decks", credential=SECRET
    )
    probes = len(fake.probes)
    assert _code(app.handle_knowledge_source_rotate(body)) == "NAMESPACE_INACTIVE"
    assert fake.rows[(ORG, "team-notes", "design-decks")].credential_enc == before
    # Refused before the probe, as add is.
    assert len(fake.probes) == probes


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
    taken: list[str] = fake.leases

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
    assert fake.probes == []


def test_a_namespace_a_person_claimed_at_signup_holds_no_source(fake: _Store) -> None:
    fake.claimed.add("alice-notes")
    assert _code(app.handle_knowledge_source_add(_add(namespace="alice-notes"))) == (
        "PERSONAL_NAMESPACE"
    )
    assert fake.probes == []


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
    fake.leases.clear()
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
    assert fake.leases == [
        f"knowledge-source:{ORG}:team-a",
        f"knowledge-source:{ORG}:team-b",
    ]
    assert fake.watched == [
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
    # An add holds the lease for the first 10 s of the step's 30; then another change
    # takes it for good, and the retirement waits only the 20 s left (a fresh 30 s wait
    # would end at 140).
    clock = _clocked(monkeypatch, {f"knowledge-source:{ORG}:team-a": 110.0})

    @contextlib.asynccontextmanager
    async def _held_elsewhere(name: str, *, wait_s: float, **_kw: Any) -> AsyncIterator[bool]:
        await clock.async_sleep(wait_s)
        yield False

    monkeypatch.setattr(leases, "holding", _held_elsewhere)
    with pytest.raises(OverloadedError):
        asyncio.run(app.retire_sources_of(ORG, ["team-a"], wait_s=30.0))
    assert clock.t == 130.0


# ---- items, links and update -------------------------------------------------

LINK = f"https://drive.google.com/drive/folders/{FOLDER}"
FILE = "1FiLeIdOfADocument_x"


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Whether every live process runs the items migration; each read is counted."""
    from server.auth import machines

    state = {"ready": True, "reads": 0}

    async def _fleet_at(ch: Any, migration: int) -> bool:
        assert migration == app._ITEMS_MIGRATION
        state["reads"] += 1
        return bool(state["ready"])

    monkeypatch.setattr(machines, "fleet_at", _fleet_at)
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    return state  # type: ignore[return-value]


def _folder(folder_id: str = FOLDER, recursive: bool = False) -> dict[str, Any]:
    return {"type": "folder", "id": folder_id, "recursive": recursive}


def _stored(fake: _Store, name: str = "design-decks") -> KnowledgeSource:
    return fake.rows[(ORG, NS, name)]


def test_add_takes_links_and_stores_them_as_id_list_config(fake: _Store, fleet: Any) -> None:
    out = asyncio.run(app.handle_knowledge_source_add(_add(config=None, links=[LINK])))
    assert out.items == [_folder()] and out.config["folder_ids"] == [FOLDER]
    src = _stored(fake)
    assert domain.id_list_config(src) is not None and src.credential_enc and not src.secret_enc
    assert app.credential_of(src) == SECRET
    # The id-list config needs no gate.
    assert fleet["reads"] == 0


def test_add_takes_items_id_list_config_cannot_hold(fake: _Store, fleet: Any) -> None:
    items = [_folder(recursive=True), {"type": "file", "id": FILE}]
    out = asyncio.run(
        app.handle_knowledge_source_add(_add(config=None, items=items, include=["docs"]))
    )
    assert out.items == items and out.config == {} and out.include == ["docs"]
    src = _stored(fake)
    assert src.secret_enc and not src.credential_enc and app.credential_of(src) == SECRET
    assert out.probe.checked == 2 and not out.probe.sampled
    assert fleet["reads"] == 1


def test_add_takes_items_and_links_together(fake: _Store, fleet: Any) -> None:
    drive = {"type": "shared_drive", "id": "0AbCdEfGhIjKlMnOpQr"}
    out = asyncio.run(
        app.handle_knowledge_source_add(_add(config=None, items=[drive], links=[LINK]))
    )
    # The given items come first, the links' after them.
    assert out.items == [drive, _folder()]


def test_add_turns_a_config_into_items(fake: _Store) -> None:
    out = asyncio.run(app.handle_knowledge_source_add(_add()))
    assert out.items == [_folder()]
    assert fake.probed_items == [[_folder()]]


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"config": None}, "BAD_CONFIG"),
        ({"links": [LINK]}, "BAD_CONFIG"),
        ({"links": [LINK], "items": [_folder()]}, "BAD_CONFIG"),
        ({"config": None, "links": []}, "BAD_CONFIG"),
        ({"include": ["docs"]}, "BAD_CONFIG"),
        ({"kind": "recording", "config": None, "items": [_folder()]}, "BAD_CONFIG"),
        ({"config": None, "items": [{"type": "folder", "id": "x' or '1"}]}, "BAD_CONFIG"),
        ({"config": None, "links": [LINK, "https://example.com/a", "nope"]}, "BAD_LINK"),
        (
            {"config": None, "links": [f"https://www.notion.so/{'a' * 32}"]},
            "BAD_LINK",
        ),
    ],
)
def test_add_refuses_what_it_cannot_read_before_any_probe(
    fake: _Store, kw: dict[str, Any], code: str
) -> None:
    assert _code(app.handle_knowledge_source_add(_add(**kw))) == code
    assert fake.probes == []


def test_bad_link_names_each_unreadable_link_of_a_few(fake: _Store) -> None:
    body = _add(config=None, links=[LINK, "https://example.com/a", "https://example.com/b"])
    with pytest.raises(ValidationError) as e:
        asyncio.run(app.handle_knowledge_source_add(body))
    message = str(e.value)
    assert "example.com/a" in message and "example.com/b" in message and FOLDER not in message


def test_add_of_a_source_without_a_kind_waits_for_the_fleet(fake: _Store, fleet: Any) -> None:
    fleet["ready"] = False
    body = _add(config=None, items=[_folder(recursive=True)])
    fake.probes.clear()
    with pytest.raises(OverloadedError) as e:
        asyncio.run(app.handle_knowledge_source_add(body))
    assert e.value.extra["retry_after_s"] == 30
    # Refused before any probe.
    assert fake.rows == {} and fake.probes == []
    # A source the id-list config holds is not held back.
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, links=[LINK])))
    assert _stored(fake).kind == "gdrive"


def test_a_probe_checks_a_sample_of_many_items(fake: _Store, fleet: Any) -> None:
    items = [{"type": "file", "id": f"{FILE}{n:02d}"} for n in range(30)]
    out = asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    assert out.probe.checked == 20 and out.probe.sampled
    assert fake.probed_items == [items[10:]]


def _update(**kw: Any) -> KnowledgeSourceUpdateInput:
    return KnowledgeSourceUpdateInput(org=ORG, namespace=NS, name="design-decks", **kw)


def test_an_item_added_in_another_form_reports_the_held_form_removed(
    fake: _Store, fleet: Any
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    out = asyncio.run(
        app.handle_knowledge_source_update(_update(add_items=[_folder(recursive=True)]))
    )
    assert out.items == out.added == [_folder(recursive=True)]
    assert out.removed == [_folder()]


def test_an_add_of_a_folder_and_the_drive_of_its_id_keeps_the_drive(
    fake: _Store, fleet: Any
) -> None:
    drive = {"type": "shared_drive", "id": FOLDER}
    out = asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=[_folder(), drive])))
    assert out.items == [drive]


def test_update_adds_and_removes_items(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    before = _stored(fake)
    fake.probed_items.clear()
    out = asyncio.run(
        app.handle_knowledge_source_update(
            _update(
                add_items=[{"type": "file", "id": FILE}],
                add_links=[LINK.replace(FOLDER, "2QrStUvWxYzAbCdEf")],
            )
        )
    )
    assert out.items == [_folder(), {"type": "file", "id": FILE}, _folder("2QrStUvWxYzAbCdEf")]
    assert out.added == out.items[1:] and out.removed == [] and out.config == {}
    # Only the added items are probed.
    assert fake.probed_items == [out.items[1:]]
    after = _stored(fake)
    assert after.source_id == before.source_id
    # The source no longer fits the id-list config: its credential moved to the other column.
    assert after.secret_enc and not after.credential_enc and app.credential_of(after) == SECRET
    out = asyncio.run(
        app.handle_knowledge_source_update(_update(remove_items=[{"type": "file", "id": FILE}]))
    )
    assert out.items == [_folder(), _folder("2QrStUvWxYzAbCdEf")] and out.config["folder_ids"]
    assert _stored(fake).credential_enc and not _stored(fake).secret_enc


def test_update_changes_include_of_a_source_without_file_items(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.probes.clear()
    out = asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))
    assert out.include == ["pdf"] and out.config["include"] == ["pdf"]
    assert fake.probes == [] and out.probe.checked == 0


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({}, "BAD_CONFIG"),
        ({"remove_items": [_folder()]}, "BAD_CONFIG"),
        ({"remove_links": [LINK]}, "BAD_CONFIG"),
        ({"remove_items": [{"type": "file", "id": FILE}]}, "BAD_CONFIG"),
        ({"add_links": ["https://example.com/x"]}, "BAD_LINK"),
    ],
)
def test_update_refuses_what_it_cannot_do_before_any_probe(
    fake: _Store, kw: dict[str, Any], code: str
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.probes.clear()
    stored = _stored(fake)
    assert _code(app.handle_knowledge_source_update(_update(**kw))) == code
    assert fake.probes == [] and _stored(fake) == stored


def test_update_of_a_recording_source_is_refused(fake: _Store) -> None:
    config = {"provider": "zoom", "ids": ["123456789"]}
    asyncio.run(app.handle_knowledge_source_add(_add(kind="recording", config=config)))
    assert _code(app.handle_knowledge_source_update(_update(include=["pdf"]))) == "BAD_KIND"


def test_update_refuses_a_source_replaced_during_its_probe(
    fake: _Store, monkeypatch: pytest.MonkeyPatch, fleet: Any
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    readded = dataclasses.replace(_stored(fake), source_id=str(uuid.uuid4()))

    async def _probe_while_replaced(*_: Any, **__: Any) -> source_probes.Probe:
        fake.rows[(ORG, NS, "design-decks")] = readded
        return source_probes.Probe(documents=1)

    monkeypatch.setattr(source_probes, "probe", _probe_while_replaced)
    with pytest.raises(SourceReplacedError):
        asyncio.run(
            app.handle_knowledge_source_update(_update(add_items=[{"type": "file", "id": FILE}]))
        )
    assert _stored(fake) is readded


def test_update_retries_when_the_source_changed_during_its_probe(
    fake: _Store, monkeypatch: pytest.MonkeyPatch, fleet: Any
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    other = "2QrStUvWxYzAbCdEf"
    added_meanwhile = dataclasses.replace(_stored(fake), items=[_folder(), _folder(other)])

    async def _probe_while_updated(*_: Any, **__: Any) -> source_probes.Probe:
        fake.rows[(ORG, NS, "design-decks")] = added_meanwhile
        return source_probes.Probe(documents=1)

    monkeypatch.setattr(source_probes, "probe", _probe_while_updated)
    # What the probe checked holds for the source as it was read: the caller retries.
    with pytest.raises(OverloadedError):
        asyncio.run(
            app.handle_knowledge_source_update(_update(add_items=[{"type": "file", "id": FILE}]))
        )
    assert _stored(fake) == added_meanwhile


def test_update_of_a_source_without_a_kind_waits_for_the_fleet(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    stored = _stored(fake)
    fleet["ready"] = False
    fake.probes.clear()
    with pytest.raises(OverloadedError):
        asyncio.run(
            app.handle_knowledge_source_update(_update(add_items=[_folder(recursive=True)]))
        )
    # Refused before any probe: a rollout answer spends no store call.
    assert _stored(fake) == stored and fake.probes == []


def test_update_records_the_items_it_added_and_removed() -> None:
    op = OPERATIONS_BY_NAME["knowledge.source.update"]
    body = _update(add_items=[{"type": "file", "id": FILE}])
    response = {"name": "design-decks", "added": [{"type": "file", "id": FILE}], "removed": []}
    assert op.audit_subject(body, response) == {  # type: ignore[misc]
        "org": ORG,
        "namespace": NS,
        "name": "design-decks",
        "added": [{"type": "file", "id": FILE}],
        "removed": [],
        "include": None,
    }


def test_parse_links_answers_each_link_and_reads_no_store(fake: _Store) -> None:
    body = KnowledgeSourceParseLinksInput(org=ORG, namespace=NS, links=[LINK, "nope"])
    out = asyncio.run(app.handle_knowledge_source_parse_links(body))
    assert [(p.kind, p.item, bool(p.unreadable)) for p in out.links] == [
        ("gdrive", _folder(), False),
        ("", {}, True),
    ]
    assert fake.rows == {} and fake.probes == []


def test_list_and_inspect_answer_items_and_id_list_config(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    items = [_folder(recursive=True)]
    asyncio.run(app.handle_knowledge_source_add(_add(name="walked", config=None, items=items)))
    listed = asyncio.run(
        app.handle_knowledge_source_list(KnowledgeSourceListInput(org=ORG, namespace=NS))
    )
    by_name = {s.name: s for s in listed.sources}
    assert by_name["design-decks"].config["folder_ids"] == [FOLDER]
    assert by_name["walked"].items == items and by_name["walked"].config == {}
    assert by_name["walked"].include == ["docs", "slides", "pdf", "pptx"]
    inspected = asyncio.run(
        app.handle_knowledge_source_inspect(
            KnowledgeSourceInspectInput(org=ORG, namespace=NS, name="walked")
        )
    )
    assert inspected.items == items and inspected.config == {}


def test_update_is_refused_on_a_gone_namespace_and_during_an_erasure(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.probes.clear()
    stored = _stored(fake)
    fake.erasures.append({"scope_type": "namespace", "scope_value": NS})
    body = _update(add_items=[{"type": "file", "id": FILE}])
    with pytest.raises(ErasureRunningError):
        asyncio.run(app.handle_knowledge_source_update(body))
    fake.erasures.clear()
    fake.inactive.add((ORG, NS))
    assert _code(app.handle_knowledge_source_update(body)) == "NAMESPACE_INACTIVE"
    # Refused before the probe, and nothing stored.
    assert fake.probes == [] and _stored(fake) == stored


def test_an_update_asks_the_next_turn_for_a_full_listing(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    assert not json.loads(fake.states["design-decks"]["detail"]).get("full")
    asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))
    detail = json.loads(fake.states["design-decks"]["detail"])
    # An incremental turn would skip an added item's older files.
    assert detail["full"] is True and detail["requested_at"]


def test_a_filter_names_no_person() -> None:
    # An owner email would stay in the registry after the person's erasure.
    with pytest.raises(BadConfigError, match="unknown"):
        domain.parse_items("gdrive", [{"type": "filter", "owner": "a.person@example.com"}])


def test_a_folder_link_removes_the_shared_drive_it_names(fake: _Store, fleet: Any) -> None:
    drive_id = "0AbCdEfGhIjKlMnOpQr"
    items = [{"type": "shared_drive", "id": drive_id}, _folder()]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    link = f"https://drive.google.com/drive/folders/{drive_id}"
    out = asyncio.run(app.handle_knowledge_source_update(_update(remove_links=[link])))
    assert out.items == [_folder()] and out.removed == [{"type": "shared_drive", "id": drive_id}]


def test_a_dry_run_is_answered_during_a_rollout(fake: _Store, fleet: Any) -> None:
    fleet["ready"] = False
    body = _add(config=None, items=[_folder(recursive=True)], dry_run=True)
    assert asyncio.run(app.handle_knowledge_source_add(body)).dry_run
    assert fake.rows == {} and fleet["reads"] == 0


def test_update_refuses_an_erasure_that_starts_during_its_probe(
    fake: _Store, monkeypatch: pytest.MonkeyPatch, fleet: Any
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    stored = _stored(fake)
    erasures = fake.erasures

    async def _probe(*_: Any, **__: Any) -> source_probes.Probe:
        erasures.append({"scope_type": "namespace", "scope_value": NS})
        return source_probes.Probe(documents=1)

    monkeypatch.setattr(source_probes, "probe", _probe)
    with pytest.raises(ErasureRunningError):
        asyncio.run(
            app.handle_knowledge_source_update(_update(add_items=[{"type": "file", "id": FILE}]))
        )
    assert _stored(fake) == stored


def test_update_refuses_a_person_s_own_place(fake: _Store) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.claimed.add(NS)
    assert _code(app.handle_knowledge_source_update(_update(include=["pdf"]))) == (
        "PERSONAL_NAMESPACE"
    )


def test_an_update_asks_again_over_a_full_request_already_waiting(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    waiting = "2026-01-01T00:00:00+00:00"
    fake.states["design-decks"] = {
        "state_value": "",
        "detail": json.dumps({"full": True, "requested_at": waiting}),
    }
    asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))
    # A turn already running over the old items clears only the request it read.
    detail = json.loads(fake.states["design-decks"]["detail"])
    assert detail["full"] is True and detail["requested_at"] != waiting


def test_an_update_s_request_differs_from_one_made_in_the_same_second(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    same_second = app._now_iso()
    fake.states["design-decks"] = {
        "state_value": "",
        "detail": json.dumps({"full": True, "requested_at": same_second}),
    }
    monkeypatch.setattr(app, "_now_iso", lambda: same_second)
    asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))
    assert json.loads(fake.states["design-decks"]["detail"])["requested_at"] != same_second


@pytest.mark.parametrize(("kind", "most"), [("linear", 20), ("notion", 20), ("gdrive", 300)])
def test_a_source_holds_as_many_items_as_its_kind_reads(kind: str, most: int) -> None:
    item = {
        "linear": lambda n: {"type": "project", "id": f"{n:08d}-0000-4000-8000-000000000000"},
        "notion": lambda n: {"type": "page", "id": f"{n:032x}"},
        "gdrive": lambda n: {"type": "file", "id": f"{FILE}{n:03d}"},
    }[kind]
    app._require_count(kind, [item(n) for n in range(most)], ["items"])
    with pytest.raises(BadConfigError, match=f"^items: at most {most} items"):
        app._require_count(kind, [item(n) for n in range(most + 1)], ["items"])


P1 = "11111111-1111-4111-8111-111111111111"
P2 = "22222222-2222-4222-8222-222222222222"
P3 = "33333333-3333-4333-8333-333333333333"


def _linear(monkeypatch: pytest.MonkeyPatch, slugs: dict[str, str]) -> list[list[str]]:
    """A tracker whose short ids name `slugs`; each lookup's short ids, in order."""
    from server.clients import linear

    asked: list[list[str]] = []

    async def _ids(credential: str, wanted: list[str]) -> dict[str, str]:
        asked.append(list(wanted))
        return {s: slugs[s] for s in wanted if s in slugs}

    monkeypatch.setattr(linear, "project_ids", _ids)
    return asked


def test_update_resolves_added_and_removed_short_ids_in_one_lookup(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"project_ids": [P1, P2]}
    asyncio.run(app.handle_knowledge_source_add(_add(kind="linear", config=config)))
    asked = _linear(monkeypatch, {"aaaaaaaaaaaa": P3, "bbbbbbbbbbbb": P2})
    out = asyncio.run(
        app.handle_knowledge_source_update(
            _update(
                add_items=[{"type": "project", "slug_id": "aaaaaaaaaaaa"}],
                remove_items=[{"type": "project", "slug_id": "bbbbbbbbbbbb"}],
            )
        )
    )
    assert asked == [["aaaaaaaaaaaa", "bbbbbbbbbbbb"]]
    assert out.added == [{"type": "project", "id": P3}]
    assert out.removed == [{"type": "project", "id": P2}]
    assert _stored(fake).items == [{"type": "project", "id": P1}, {"type": "project", "id": P3}]


def test_a_project_named_by_id_and_by_short_id_is_stored_once(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _linear(monkeypatch, {"aaaaaaaaaaaa": P1})
    items = [{"type": "project", "id": P1}, {"type": "project", "slug_id": "aaaaaaaaaaaa"}]
    out = asyncio.run(
        app.handle_knowledge_source_add(_add(kind="linear", config=None, items=items))
    )
    assert out.items == [{"type": "project", "id": P1}]
    # Probed once, its duplicate dropped before the probe.
    assert fake.probed_items[-1] == [{"type": "project", "id": P1}]
    assert out.probe.checked == 1


def test_update_refuses_a_tracker_source_grown_past_its_twenty(fake: _Store, fleet: Any) -> None:
    held = [f"{n:08d}-0000-4000-8000-000000000000" for n in range(15)]
    asyncio.run(app.handle_knowledge_source_add(_add(kind="linear", config={"project_ids": held})))
    more = [
        {"type": "project", "id": f"{n:08d}-0000-4000-8000-000000000000"} for n in range(15, 21)
    ]
    with pytest.raises(BadConfigError, match="at most 20 items"):
        asyncio.run(app.handle_knowledge_source_update(_update(add_items=more)))


def test_update_refuses_an_item_both_added_and_removed(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    stored = _stored(fake)
    probes = len(fake.probes)
    body = _update(add_items=[_folder()], remove_items=[_folder()])
    assert _code(app.handle_knowledge_source_update(body)) == "BAD_CONFIG"
    # Refused before any probe.
    assert _stored(fake) == stored and len(fake.probes) == probes


def test_update_refuses_a_held_drive_both_added_and_removed_by_its_folder_link(
    fake: _Store, fleet: Any
) -> None:
    drive_id = "0AbCdEfGhIjKlMnOpQr"
    items = [{"type": "shared_drive", "id": drive_id}, _folder()]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    probes = len(fake.probes)
    link = f"https://drive.google.com/drive/folders/{drive_id}"
    body = _update(add_links=[link], remove_links=[link])
    assert _code(app.handle_knowledge_source_update(body)) == "BAD_CONFIG"
    assert len(fake.probes) == probes


def test_update_adds_and_removes_a_filter_item(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    item = {"type": "filter", "name_contains": "plan", "in": FOLDER}
    out = asyncio.run(app.handle_knowledge_source_update(_update(add_items=[item])))
    assert item in out.items
    out = asyncio.run(app.handle_knowledge_source_update(_update(remove_items=[item])))
    assert out.items == [_folder()] and out.removed == [item]


def test_update_answers_a_removed_item_as_the_source_held_it(fake: _Store, fleet: Any) -> None:
    items = [_folder(recursive=True), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    # The link reads `recursive: false`; the answer names the folder as it was stored.
    out = asyncio.run(app.handle_knowledge_source_update(_update(remove_links=[LINK])))
    assert out.removed == [_folder(recursive=True)]


def _foreign_probe(monkeypatch: pytest.MonkeyPatch, foreign: list[str]) -> list[Any]:
    """A probe that names `foreign` as files of a kind the source does not read."""
    calls: list[Any] = []

    async def _probe(
        kind: str, config: dict[str, Any], credential: str, **kw: Any
    ) -> source_probes.Probe:
        calls.append(kw.get("items"))
        named = {f: domain.GDRIVE_MIME_TYPES["docs"] for f in foreign}
        return source_probes.Probe(documents=0, items=kw.get("items") or [], foreign=named)

    monkeypatch.setattr(source_probes, "probe", _probe)
    return calls


def test_add_refuses_a_file_of_a_kind_its_include_does_not_name(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _foreign_probe(monkeypatch, [FILE])
    body = _add(config=None, items=[{"type": "file", "id": FILE}])
    assert _code(app.handle_knowledge_source_add(body)) == "BAD_CONFIG"
    assert fake.rows == {}


def test_an_include_update_probes_the_stored_files_and_rotate_is_not_refused(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    stored = _stored(fake)
    calls = _foreign_probe(monkeypatch, [FILE])
    # A narrower include that no longer names the stored file's kind is refused.
    assert _code(app.handle_knowledge_source_update(_update(include=["pdf"]))) == "BAD_CONFIG"
    assert calls == [[{"type": "file", "id": FILE}]] and _stored(fake) == stored
    # The shared probe names the file but refuses nothing, so rotate still works.
    body = KnowledgeSourceRotateInput(org=ORG, namespace=NS, name="design-decks", credential=SECRET)
    assert asyncio.run(app.handle_knowledge_source_rotate(body)).name == "design-decks"


def test_a_folder_added_beside_the_drive_of_its_id_adds_nothing(fake: _Store, fleet: Any) -> None:
    drive = {"type": "shared_drive", "id": "0AbCdEfGhIjKlMnOpQr"}
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=[drive])))
    out = asyncio.run(app.handle_knowledge_source_update(_update(add_items=[_folder(drive["id"])])))
    assert out.items == [drive] and out.added == []


def test_an_update_swapping_a_drive_for_its_root_folder_adds_the_folder(
    fake: _Store, fleet: Any
) -> None:
    drive = {"type": "shared_drive", "id": "0AbCdEfGhIjKlMnOpQr"}
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=[drive, _folder()])))
    out = asyncio.run(
        app.handle_knowledge_source_update(
            _update(remove_items=[drive], add_items=[_folder(drive["id"])])
        )
    )
    assert out.items == [_folder(), _folder(drive["id"])]
    assert out.added == [_folder(drive["id"])] and out.removed == [drive]


def test_an_add_of_a_drive_and_a_link_to_its_root_folder_keeps_the_drive(
    fake: _Store, fleet: Any
) -> None:
    drive = {"type": "shared_drive", "id": "0AbCdEfGhIjKlMnOpQr"}
    link = f"https://drive.google.com/drive/folders/{drive['id']}"
    body = _add(config=None, items=[drive], links=[link])
    assert asyncio.run(app.handle_knowledge_source_add(body)).items == [drive]


def test_a_filter_whose_kinds_include_does_not_name_is_refused(fake: _Store, fleet: Any) -> None:
    item = {"type": "filter", "kinds": ["docx"]}
    body = _add(config=None, items=[item], include=["docs"])
    assert _code(app.handle_knowledge_source_add(body)) == "BAD_CONFIG"
    asyncio.run(app.handle_knowledge_source_add(_add()))
    # Nor can an update narrow include below a stored filter's kinds.
    asyncio.run(
        app.handle_knowledge_source_update(
            _update(add_items=[{"type": "filter", "kinds": ["pdf"]}])
        )
    )
    assert _code(app.handle_knowledge_source_update(_update(include=["docs"]))) == "BAD_CONFIG"


def test_an_include_update_that_removes_the_file_it_would_refuse_succeeds(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    calls = _foreign_probe(monkeypatch, [FILE])
    body = _update(include=["pdf"], remove_items=[{"type": "file", "id": FILE}])
    out = asyncio.run(app.handle_knowledge_source_update(body))
    # The file goes with this call, so it is not probed against the new include.
    assert out.items == [_folder()] and calls == []


def test_an_include_restated_unchanged_is_no_change(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    calls = _foreign_probe(monkeypatch, [])
    include = list(_stored(fake).config["include"])
    stored = _stored(fake)
    # A retried call restates what landed: it answers the source, probing and writing none.
    out = asyncio.run(app.handle_knowledge_source_update(_update(include=include)))
    assert out.include == include and out.added == [] and calls == []
    assert _stored(fake) == stored
    # Restated beside an added item, only the added item is probed.
    asyncio.run(
        app.handle_knowledge_source_update(
            _update(add_items=[_folder("2QrStUvWxYzAbCdEf")], include=include)
        )
    )
    assert calls == [[_folder("2QrStUvWxYzAbCdEf")]]


def test_an_include_only_update_answers_the_stored_files_probe(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    _foreign_probe(monkeypatch, [])
    out = asyncio.run(app.handle_knowledge_source_update(_update(include=["docs"])))
    assert out.probe.checked == 1


def test_an_include_update_retries_when_another_update_adds_a_file_during_its_probe(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    other = {"type": "file", "id": f"{FILE}2"}

    async def _probe_while_added(*_: Any, **kw: Any) -> source_probes.Probe:
        row = _stored(fake)
        fake.rows[(ORG, NS, "design-decks")] = dataclasses.replace(row, items=[*row.items, other])
        return source_probes.Probe(documents=1, items=kw.get("items") or [])

    monkeypatch.setattr(source_probes, "probe", _probe_while_added)
    with pytest.raises(OverloadedError):
        asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))


def test_a_file_add_retries_when_another_update_changes_include_during_its_probe(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))

    async def _probe_while_narrowed(*_: Any, **kw: Any) -> source_probes.Probe:
        row = _stored(fake)
        fake.rows[(ORG, NS, "design-decks")] = dataclasses.replace(row, config={"include": ["pdf"]})
        return source_probes.Probe(documents=1, items=kw.get("items") or [])

    monkeypatch.setattr(source_probes, "probe", _probe_while_narrowed)
    body = _update(add_items=[{"type": "file", "id": FILE}])
    with pytest.raises(OverloadedError):
        asyncio.run(app.handle_knowledge_source_update(body))


def test_an_update_with_its_own_include_also_retries_when_another_changed_it(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))

    async def _probe_while_narrowed(*_: Any, **kw: Any) -> source_probes.Probe:
        row = _stored(fake)
        fake.rows[(ORG, NS, "design-decks")] = dataclasses.replace(row, config={"include": ["pdf"]})
        return source_probes.Probe(documents=1, items=kw.get("items") or [])

    monkeypatch.setattr(source_probes, "probe", _probe_while_narrowed)
    body = _update(add_items=[{"type": "file", "id": FILE}], include=["docs", "pdf"])
    # Any change during the probe is retried: the caller sees the other change first.
    with pytest.raises(OverloadedError):
        asyncio.run(app.handle_knowledge_source_update(body))


def test_a_plain_sync_now_leaves_an_update_s_full_request_as_it_is(
    fake: _Store, fleet: Any
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))
    before = fake.states["design-decks"]["detail"]
    body = KnowledgeSourceSyncNowInput(org=ORG, namespace=NS, name="design-decks")
    asyncio.run(app.handle_knowledge_source_sync_now(body))
    # Neither narrowed nor started over: the request's file waits are reset only once.
    assert fake.states["design-decks"]["detail"] == before


def test_an_update_retries_when_a_rotate_landed_during_its_probe(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))

    async def _probe_while_rotated(*_: Any, **kw: Any) -> source_probes.Probe:
        row = _stored(fake)
        fake.rows[(ORG, NS, "design-decks")] = dataclasses.replace(row, credential_enc="other")
        return source_probes.Probe(documents=1, items=kw.get("items") or [])

    monkeypatch.setattr(source_probes, "probe", _probe_while_rotated)
    # The added folder was probed with the old credential only.
    with pytest.raises(OverloadedError):
        asyncio.run(
            app.handle_knowledge_source_update(_update(add_items=[_folder("2QrStUvWxYzAbCdEf")]))
        )


def test_a_rotate_retries_when_an_update_landed_during_its_probe(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))

    async def _probe_while_updated(*_: Any, **kw: Any) -> source_probes.Probe:
        row = _stored(fake)
        fake.rows[(ORG, NS, "design-decks")] = dataclasses.replace(
            row, items=[*row.items, _folder("2QrStUvWxYzAbCdEf")]
        )
        return source_probes.Probe(documents=1, items=kw.get("items") or [])

    monkeypatch.setattr(source_probes, "probe", _probe_while_updated)
    body = KnowledgeSourceRotateInput(org=ORG, namespace=NS, name="design-decks", credential=SECRET)
    # The new credential never saw the folder the update added.
    with pytest.raises(OverloadedError):
        asyncio.run(app.handle_knowledge_source_rotate(body))


def test_bad_link_message_stays_bounded_for_overlong_links(fake: _Store) -> None:
    long = "https://drive.google.com/drive/folders/" + "a" * 2000
    with pytest.raises(ValidationError) as e:
        asyncio.run(app.handle_knowledge_source_add(_add(config=None, links=[long] * 3)))
    # Each link and its reason are cut, so the message never grows with the input, and
    # the reason still says why.
    assert e.value.to_detail()["code"] == "BAD_LINK" and len(str(e.value)) < 3 * 250
    assert "is not an id" in str(e.value)


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"config": None, "items": [{"type": "folder", "id": "a" * 100_000}]}, "BAD_CONFIG"),
        ({"config": {"folder_ids": ["a" * 100_000]}}, "BAD_CONFIG"),
        ({"config": None, "items": [_folder()], "include": ["x" * 100_000]}, "BAD_CONFIG"),
        ({"config": None, "items": [{**_folder(), "k" * 100_000: 1}]}, "BAD_CONFIG"),
        ({"config": {"folder_ids": [FOLDER], "k" * 100_000: 1}}, "BAD_CONFIG"),
        ({"kind": "k" * 100_000}, "BAD_KIND"),
    ],
)
def test_a_refused_value_is_quoted_in_part(fake: _Store, kw: dict[str, Any], code: str) -> None:
    with pytest.raises(ValidationError) as e:
        asyncio.run(app.handle_knowledge_source_add(_add(**kw)))
    # At most 40 characters of it, quoted, the cut marked.
    assert e.value.to_detail()["code"] == code and len(str(e.value)) < 200
    assert "…" in str(e.value)


def test_unknown_keys_name_the_first_and_count_the_rest(fake: _Store) -> None:
    item = {**_folder(), "aa": 1, "bb": 2}
    with pytest.raises(ValidationError) as e:
        asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=[item])))
    assert "unknown key 'aa' and 1 more" in str(e.value)


def test_a_value_that_fits_is_quoted_whole() -> None:
    assert domain.shown("a" * 40) == repr("a" * 40)
    assert domain.shown("a" * 41) == repr("a" * 40) + "…"


def test_an_unknown_source_name_is_quoted_in_part(fake: _Store) -> None:
    body = KnowledgeSourceUpdateInput(org=ORG, namespace=NS, name="n" * 100_000, include=["pdf"])
    with pytest.raises(NotFoundError) as e:
        asyncio.run(app.handle_knowledge_source_update(body))
    assert e.value.to_detail()["code"] == "SOURCE_NOT_FOUND" and len(str(e.value)) < 200
    assert "…" in str(e.value)


@pytest.mark.parametrize(
    ("confirm", "code"), [("n" * 100_000, "SOURCE_NOT_FOUND"), ("other", "CONFIRM_REQUIRED")]
)
def test_remove_quotes_a_long_name_in_part(fake: _Store, confirm: str, code: str) -> None:
    body = KnowledgeSourceRemoveInput(org=ORG, namespace=NS, name="n" * 100_000, confirm=confirm)
    with pytest.raises((NotFoundError, ValidationError)) as e:
        asyncio.run(app.handle_knowledge_source_remove(body))
    assert e.value.to_detail()["code"] == code and len(str(e.value)) < 200
    assert "…" in str(e.value)


def test_update_names_the_field_a_bad_item_came_in(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    with pytest.raises(ValidationError, match=r"add_items\[0\]\.id"):
        asyncio.run(
            app.handle_knowledge_source_update(_update(add_items=[{"type": "folder", "id": "x y"}]))
        )
    with pytest.raises(ValidationError, match=r"remove_items\[0\]\.id"):
        asyncio.run(
            app.handle_knowledge_source_update(
                _update(remove_items=[{"type": "folder", "id": "x y"}])
            )
        )


def test_bad_link_names_the_first_ten_and_counts_the_rest(fake: _Store) -> None:
    links = [f"https://example.com/{n}" for n in range(25)]
    with pytest.raises(ValidationError) as e:
        asyncio.run(app.handle_knowledge_source_add(_add(config=None, links=links)))
    message = str(e.value)
    assert "example.com/9" in message and "example.com/10" not in message
    assert message.endswith("and 15 more")


def test_update_counts_added_items_and_links_together(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    items = [{"type": "file", "id": f"{FILE}{n:03d}"} for n in range(200)]
    links = [f"https://drive.google.com/file/d/{FILE}{n:03d}x/view" for n in range(200)]
    with pytest.raises(ValidationError, match="add_items and add_links: at most 300"):
        asyncio.run(app.handle_knowledge_source_update(_update(add_items=items, add_links=links)))


def test_a_count_refusal_names_only_the_fields_sent(fake: _Store, fleet: Any) -> None:
    # A page store keeps 20 items, so 21 links pass the wire's bound and reach the count.
    links = [f"https://www.notion.so/Page-{n:032x}" for n in range(21)]
    body = _add(kind="notion", config=None, links=links)
    with pytest.raises(ValidationError, match=r"^links: at most 20"):
        asyncio.run(app.handle_knowledge_source_add(body))


def test_a_drive_added_for_a_held_folder_of_its_id_replaces_it(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive_id = "0AbCdEfGhIjKlMnOpQr"
    asyncio.run(
        app.handle_knowledge_source_add(_add(config=None, items=[_folder(drive_id), _folder()]))
    )

    async def _as_drive(*_: Any, **kw: Any) -> source_probes.Probe:
        # The probe finds the recursive folder is the drive's root.
        return source_probes.Probe(documents=1, items=[{"type": "shared_drive", "id": drive_id}])

    monkeypatch.setattr(source_probes, "probe", _as_drive)
    body = _update(add_items=[_folder(drive_id, recursive=True)])
    out = asyncio.run(app.handle_knowledge_source_update(body))
    assert out.items == [_folder(), {"type": "shared_drive", "id": drive_id}]
    # The folder the drive replaced is named as removed.
    assert out.removed == [_folder(drive_id)]


def test_a_drive_removed_and_added_back_as_its_root_folder_is_refused(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = {"type": "shared_drive", "id": "0AbCdEfGhIjKlMnOpQr"}
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=[drive, _folder()])))
    stored = _stored(fake)

    async def _as_drive(*_: Any, **kw: Any) -> source_probes.Probe:
        return source_probes.Probe(documents=1, items=[drive])

    monkeypatch.setattr(source_probes, "probe", _as_drive)
    # The probe names the folder as the drive, so the source re-read under its lease
    # refuses the change.
    body = _update(remove_items=[drive], add_items=[_folder(drive["id"], recursive=True)])
    assert _code(app.handle_knowledge_source_update(body)) == "BAD_CONFIG"
    assert _stored(fake) == stored


def test_the_fleet_gate_names_the_items_migration() -> None:
    migrations = Path(__file__).resolve().parents[2] / "schemas" / "migrations"
    found = migrations.glob(f"{app._ITEMS_MIGRATION:03d}_*.sql")
    assert [p.name for p in found] == [f"{app._ITEMS_MIGRATION:03d}_knowledge_source_items.sql"]


def test_an_include_update_over_an_unshared_kept_file_says_it_can_be_removed(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))

    async def _unshared(*_: Any, **__: Any) -> source_probes.Probe:
        message = f"file {FILE}: not found; share it with the service account"
        raise source_probes.SourceUnreachable(message, FILE)

    monkeypatch.setattr(source_probes, "probe", _unshared)
    with pytest.raises(ValidationError) as raised:
        asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))
    assert raised.value.to_detail()["code"] == "SOURCE_UNREACHABLE"
    assert raised.value.message.endswith(
        "share it with the service account, or remove it with remove_items"
    )


def test_an_include_update_the_store_does_not_answer_gives_no_remove_hint(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))

    async def _slow(*_: Any, **__: Any) -> source_probes.Probe:
        raise source_probes.SourceUnreachable("the source did not answer within 20 s")

    monkeypatch.setattr(source_probes, "probe", _slow)
    with pytest.raises(ValidationError) as raised:
        asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))
    assert "remove_items" not in raised.value.message


def test_only_an_unseen_removed_short_id_says_to_remove_by_id(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"project_ids": [P1, P2]}
    asyncio.run(app.handle_knowledge_source_add(_add(kind="linear", config=config)))
    _linear(monkeypatch, {})
    gone = [{"type": "project", "slug_id": "bbbbbbbbbbbb"}]
    with pytest.raises(ValidationError) as raised:
        asyncio.run(app.handle_knowledge_source_update(_update(remove_items=gone)))
    assert raised.value.message.endswith("; remove a source's own project by its id")
    with pytest.raises(ValidationError) as raised:
        asyncio.run(app.handle_knowledge_source_update(_update(add_items=gone)))
    assert raised.value.message.endswith("the key's account must see it")


def test_a_kept_file_of_a_dropped_kind_names_both_ways_out(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    _foreign_probe(monkeypatch, [FILE])
    with pytest.raises(ValidationError) as raised:
        asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))
    assert raised.value.message == (
        f"file {FILE}: not a kind the source's include names; name its kind in include, "
        "or remove it with remove_items"
    )


def test_a_file_of_a_type_the_sync_does_not_read_is_named_as_such(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _probe(kind: str, config: dict[str, Any], credential: str, **kw: Any) -> Any:
        return source_probes.Probe(documents=0, items=kw["items"], foreign={FILE: "image/png"})

    monkeypatch.setattr(source_probes, "probe", _probe)
    body = _add(config=None, items=[{"type": "file", "id": FILE}])
    with pytest.raises(ValidationError) as raised:
        asyncio.run(app.handle_knowledge_source_add(body))
    assert raised.value.message == f"file {FILE}: 'image/png' is a type the sync does not read"


def test_a_folder_given_as_a_file_item_says_to_add_it_as_a_folder(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = "application/vnd.google-apps.folder"

    async def _probe(kind: str, config: dict[str, Any], credential: str, **kw: Any) -> Any:
        return source_probes.Probe(documents=0, items=kw["items"], foreign={FILE: folder})

    monkeypatch.setattr(source_probes, "probe", _probe)
    body = _add(config=None, items=[{"type": "file", "id": FILE}])
    with pytest.raises(ValidationError) as raised:
        asyncio.run(app.handle_knowledge_source_add(body))
    assert raised.value.message == f"file {FILE}: a folder; add it as a folder item"


def _trashed_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _probe(kind: str, config: dict[str, Any], credential: str, **kw: Any) -> Any:
        return source_probes.Probe(documents=0, items=kw["items"], trashed=[FILE])

    monkeypatch.setattr(source_probes, "probe", _probe)


def test_add_refuses_a_trashed_file(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _trashed_probe(monkeypatch)
    items = [_folder(), {"type": "file", "id": FILE}]
    with pytest.raises(ValidationError) as raised:
        asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    assert raised.value.to_detail()["code"] == "SOURCE_UNREACHABLE"
    assert raised.value.message.endswith(f"file {FILE}: in the trash; restore it first")


def test_rotate_is_not_refused_over_a_trashed_file(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    _trashed_probe(monkeypatch)
    body = KnowledgeSourceRotateInput(org=ORG, namespace=NS, name="design-decks", credential=SECRET)
    assert asyncio.run(app.handle_knowledge_source_rotate(body)).name == "design-decks"


def test_an_include_update_over_a_trashed_kept_file_says_it_can_be_removed(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [_folder(), {"type": "file", "id": FILE}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    _trashed_probe(monkeypatch)
    with pytest.raises(ValidationError) as raised:
        asyncio.run(app.handle_knowledge_source_update(_update(include=["pdf"])))
    assert raised.value.message.endswith(
        "in the trash; restore it first, or remove it with remove_items"
    )


def test_update_adding_a_trashed_file_is_refused_and_check_is_not(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    _trashed_probe(monkeypatch)
    added = [{"type": "file", "id": FILE}]
    assert _code(app.handle_knowledge_source_update(_update(add_items=added))) == (
        "SOURCE_UNREACHABLE"
    )
    body = KnowledgeSourceCheckInput(org=ORG, namespace=NS, name="design-decks")
    assert asyncio.run(app.handle_knowledge_source_check(body)).reachable


def test_a_folder_link_to_a_held_drive_changes_nothing(fake: _Store, fleet: Any) -> None:
    drive_id = "0AbCdEfGhIjKlMnOpQr"
    items = [{"type": "shared_drive", "id": drive_id}, _folder()]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    stored = _stored(fake)
    fake.probes.clear()
    link = f"https://drive.google.com/drive/folders/{drive_id}"
    out = asyncio.run(app.handle_knowledge_source_update(_update(add_links=[link])))
    assert out.items == items and out.added == [] and out.removed == []
    assert _stored(fake) == stored and fake.probes == []


def test_an_update_adding_held_items_again_writes_and_probes_nothing(
    fake: _Store, fleet: Any
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    stored, states = _stored(fake), dict(fake.states)
    fake.probes.clear()
    out = asyncio.run(app.handle_knowledge_source_update(_update(add_items=[_folder()])))
    assert out.items == [_folder()] and out.added == []
    assert _stored(fake) == stored and fake.probes == []
    # No full listing is asked for either.
    assert fake.states == states


def test_a_folder_link_over_a_held_recursive_folder_changes_nothing(
    fake: _Store, fleet: Any
) -> None:
    items = [_folder(recursive=True)]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    stored, states = _stored(fake), dict(fake.states)
    out = asyncio.run(app.handle_knowledge_source_update(_update(add_links=[LINK])))
    # The link names the folder the source walks; only an item given narrows it.
    assert out.items == items and out.added == []
    assert _stored(fake) == stored and fake.states == states
    out = asyncio.run(app.handle_knowledge_source_update(_update(add_items=[_folder()])))
    assert out.items == [_folder()]


def test_an_item_given_wins_over_a_link_to_the_same_folder(fake: _Store, fleet: Any) -> None:
    out = asyncio.run(
        app.handle_knowledge_source_add(
            _add(config=None, items=[_folder(recursive=True)], links=[LINK])
        )
    )
    assert out.items == [_folder(recursive=True)]
    other = "2QrStUvWxYzAbCdEf"
    link = f"https://drive.google.com/drive/folders/{other}"
    body = _update(add_items=[_folder(other, recursive=True)], add_links=[link])
    out = asyncio.run(app.handle_knowledge_source_update(body))
    assert out.items == [_folder(recursive=True), _folder(other, recursive=True)]
    # A held recursive folder: the item given narrows it, the link beside it does not undo that.
    body = _update(add_items=[_folder()], add_links=[LINK])
    out = asyncio.run(app.handle_knowledge_source_update(body))
    assert _folder() in out.items and _folder(recursive=True) not in out.items


def test_a_folder_the_probe_names_as_a_held_drive_changes_nothing(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive_id = "0AbCdEfGhIjKlMnOpQr"
    items = [{"type": "shared_drive", "id": drive_id}, _folder()]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    stored, states = _stored(fake), dict(fake.states)

    async def _as_drive(*_: Any, **kw: Any) -> source_probes.Probe:
        return source_probes.Probe(documents=1, items=[{"type": "shared_drive", "id": drive_id}])

    monkeypatch.setattr(source_probes, "probe", _as_drive)
    body = _update(add_items=[_folder(drive_id, recursive=True)])
    out = asyncio.run(app.handle_knowledge_source_update(body))
    assert out.items == items and out.added == []
    assert _stored(fake) == stored and fake.states == states


def test_a_project_named_by_id_and_by_link_counts_once(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [f"{n:08x}-1111-4111-8111-111111111111" for n in range(20)]
    _linear(monkeypatch, {"aaaaaaaaaaaa": ids[0]})
    items = [{"type": "project", "id": i} for i in ids]
    link = "https://linear.app/acme/project/q3-planning-aaaaaaaaaaaa"
    body = _add(kind="linear", config=None, items=items, links=[link])
    # 21 entries, 20 projects: the bound counts the projects.
    assert len(asyncio.run(app.handle_knowledge_source_add(body)).items) == 20


def test_a_folder_entry_removes_the_shared_drive_of_its_id(fake: _Store, fleet: Any) -> None:
    drive_id = "0AbCdEfGhIjKlMnOpQr"
    items = [{"type": "shared_drive", "id": drive_id}, _folder()]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=items)))
    out = asyncio.run(app.handle_knowledge_source_update(_update(remove_items=[_folder(drive_id)])))
    assert out.items == [_folder()] and out.removed == [{"type": "shared_drive", "id": drive_id}]


def test_a_project_named_by_id_and_by_short_id_in_items_counts_once(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [f"{n:08x}-1111-4111-8111-111111111111" for n in range(20)]
    _linear(monkeypatch, {"aaaaaaaaaaaa": ids[0]})
    items = [{"type": "project", "id": i} for i in ids]
    items.append({"type": "project", "slug_id": "aaaaaaaaaaaa"})
    body = _add(kind="linear", config=None, items=items)
    assert len(asyncio.run(app.handle_knowledge_source_add(body)).items) == 20


def test_an_update_counts_a_project_named_by_id_and_by_link_once(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = [f"{n:08x}-1111-4111-8111-111111111111" for n in range(19)]
    asyncio.run(app.handle_knowledge_source_add(_add(kind="linear", config={"project_ids": held})))
    _linear(monkeypatch, {"bbbbbbbbbbbb": P3})
    link = "https://linear.app/acme/project/q3-planning-bbbbbbbbbbbb"
    body = _update(add_items=[{"type": "project", "id": P3}], add_links=[link])
    assert len(asyncio.run(app.handle_knowledge_source_update(body)).items) == 20


def test_a_call_too_large_is_refused_before_the_short_id_lookup(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _linear(monkeypatch, {})
    links = [f"https://linear.app/acme/project/p-{n:012x}" for n in range(21)]
    with pytest.raises(BadConfigError, match="^links: at most 20"):
        asyncio.run(app.handle_knowledge_source_add(_add(kind="linear", config=None, links=links)))
    assert asked == []


def test_an_update_too_large_is_refused_before_the_short_id_lookup(
    fake: _Store, fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add(kind="linear", config={"project_ids": [P1]})))
    asked = _linear(monkeypatch, {})
    links = [f"https://linear.app/acme/project/p-{n:012x}" for n in range(21)]
    with pytest.raises(BadConfigError, match="^add_links: at most 20"):
        asyncio.run(app.handle_knowledge_source_update(_update(add_links=links)))
    # Removals past what the source holds are refused before the lookup too.
    with pytest.raises(
        BadConfigError, match="^remove_links: names more items than the source holds"
    ):
        asyncio.run(app.handle_knowledge_source_update(_update(remove_links=links[:2])))
    assert asked == []


def test_an_unreadable_link_is_named_with_its_field(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    body = _update(remove_links=["https://example.com/a"])
    with pytest.raises(ValidationError, match=r"^unreadable remove_links: "):
        asyncio.run(app.handle_knowledge_source_update(body))


@pytest.mark.parametrize(
    "body", [{"include": ["docs", "slides", "pdf", "pptx"]}, {"add_items": [_folder()]}]
)
def test_an_update_that_changes_nothing_is_still_refused_during_an_erasure(
    fake: _Store, fleet: Any, body: dict[str, Any]
) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    fake.erasures.append({"scope_type": "namespace", "scope_value": NS})
    # However a no-op is phrased, it answers as every update does.
    with pytest.raises(ErasureRunningError):
        asyncio.run(app.handle_knowledge_source_update(_update(**body)))


def test_a_folder_entry_and_a_drive_entry_of_one_id_remove_the_drive_once(
    fake: _Store, fleet: Any
) -> None:
    drive_id = "0AbCdEfGhIjKlMnOpQr"
    drive = {"type": "shared_drive", "id": drive_id}
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=[drive, _folder()])))
    body = _update(remove_items=[_folder(drive_id), drive])
    out = asyncio.run(app.handle_knowledge_source_update(body))
    assert out.items == [_folder()] and out.removed == [drive]


def test_an_update_naming_nothing_says_what_to_name(fake: _Store, fleet: Any) -> None:
    asyncio.run(app.handle_knowledge_source_add(_add()))
    with pytest.raises(BadConfigError, match="^name an item, a link or include to change"):
        asyncio.run(app.handle_knowledge_source_update(_update()))


def test_redundant_folder_and_drive_entries_count_as_the_items_they_name(
    fake: _Store, fleet: Any
) -> None:
    x, y = "0AbCdEfGhIjKlMnOpQr", "0BbCdEfGhIjKlMnOpQr"
    drives = [{"type": "shared_drive", "id": x}, {"type": "shared_drive", "id": y}]
    asyncio.run(app.handle_knowledge_source_add(_add(config=None, items=[*drives, _folder()])))
    body = _update(remove_items=[_folder(x), drives[0], _folder(y), drives[1]])
    out = asyncio.run(app.handle_knowledge_source_update(body))
    assert out.items == [_folder()] and out.removed == drives
