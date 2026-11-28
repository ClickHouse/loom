import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from server.knowledge.domain.mcp_worker_source import (
    WORKER_REGISTRATION,
    WorkerSourceRefused,
    decode_worker_source,
    encode_worker_source,
)
from server.knowledge.domain.sources import MIRRORED_ACCESS, KnowledgeSource
from server.knowledge.store.sources import get_source, insert_source


class TestWorkerSource(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        connection = str(uuid4())
        self.config = {
            "version": 2,
            "worker_id": str(uuid4()),
            "profile_digest": "a" * 64,
            "owner_connection_id": connection,
            "owner_connection_generation": 3,
            "approval_generation": 2,
            "selection_generation": 4,
            "mode": "selected_refresh",
            "reader_policy": "current_team_access",
            "interval_seconds": 300,
        }
        self.source = KnowledgeSource(
            org="org",
            namespace="team",
            name="source",
            kind="mcp",
            source_id=str(uuid4()),
            access=MIRRORED_ACCESS,
            credential_mode="worker",
            owner_subject="alice",
            connection_id=connection,
            config={WORKER_REGISTRATION: json.dumps(self.config)},
            items=[{"type": "resource", "id": "urn:resource:Résumé"}],
        )

    def test_preserves_exact_selection_and_opaque_worker_connection(self) -> None:
        config, [document] = decode_worker_source(self.source)
        self.assertEqual(document.id, "urn:resource:Résumé")
        self.assertEqual(str(config.owner_connection_id), self.source.connection_id)
        self.assertEqual(config.selection_generation, 4)

    def test_registration_roundtrip_preserves_identity_without_credential_fields(self) -> None:
        config, selected = decode_worker_source(self.source)
        row = encode_worker_source(
            org=self.source.org,
            namespace=self.source.namespace,
            name=self.source.name,
            source_id=UUID(self.source.source_id),
            owner=self.source.owner_subject,
            config=config,
            selection=selected,
        )
        self.assertEqual(decode_worker_source(row), (config, selected))
        self.assertEqual(row.source_id, self.source.source_id)
        self.assertEqual(row.credential_enc, "")
        self.assertEqual(row.secret_enc, "")
        self.assertEqual(row.created_by, row.owner_subject)

    def test_never_interprets_older_or_inactive_authority(self) -> None:
        cases = (
            {"config": {WORKER_REGISTRATION: json.dumps({**self.config, "version": 1})}},
            {"kind": "linear"},
            {"credential_mode": "connected"},
            {"credential_enc": "legacy"},
            {"access": "namespace"},
            {"connection_id": str(uuid4())},
            {"active": False},
            {"pending_delete": True},
        )
        for fields in cases:
            with self.subTest(fields=tuple(fields)), self.assertRaises(WorkerSourceRefused):
                decode_worker_source(replace(self.source, **fields))

    def test_rejects_credential_data_ambiguous_modes_and_duplicate_selection(self) -> None:
        cases = ({"credential": "value"}, {"mode": "live"}, {"owner_connection_generation": "3"})
        for fields in cases:
            config = {**self.config, **fields}
            with self.subTest(fields=tuple(fields)), self.assertRaises(WorkerSourceRefused):
                decode_worker_source(
                    replace(self.source, config={WORKER_REGISTRATION: json.dumps(config)})
                )
        repeated = self.source.items * 2
        with self.assertRaises(WorkerSourceRefused):
            decode_worker_source(replace(self.source, items=repeated))

    async def test_existing_store_preserves_worker_rows_but_does_not_advertise_legacy_kind(
        self,
    ) -> None:
        client = AsyncMock()
        await insert_source(self.source, ch=client)
        stored = client.insert.await_args.args[1][0]
        assert stored["kind"] == ""
        assert stored["source_kind"] == "mcp"
        assert stored["credential_enc"] == stored["secret_enc"] == ""
        client.query_one.return_value = stored
        source = await get_source(
            self.source.org, self.source.namespace, self.source.name, ch=client
        )
        assert source is not None
        assert decode_worker_source(source) == decode_worker_source(self.source)
        assert client.query_one.await_args.kwargs["params"] == {
            "org": self.source.org,
            "ns": self.source.namespace,
            "name": self.source.name,
        }
