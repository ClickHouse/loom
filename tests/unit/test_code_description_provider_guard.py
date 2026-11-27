"""Fake-served descriptions never reach generated context or maintenance writes."""

from __future__ import annotations

import argparse
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from server.cli import code_maintenance
from server.code.app.index import describe, round_models


def _client(provider: str, served_provider: str = "fake") -> SimpleNamespace:
    return SimpleNamespace(
        provider=provider,
        complete=AsyncMock(
            return_value=SimpleNamespace(provider=served_provider, text="A generated description.")
        ),
    )


class DescriptionProviderGuardTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, path: str, client: SimpleNamespace) -> str:
        prompt = ("system", "user", 60)
        if path == "round":
            return await round_models._describe_call(client, prompt)
        with patch.object(describe, "describe_prompt", return_value=prompt):
            return await describe._describe_chunk(client, SimpleNamespace(scope="sample"))

    async def test_exposed_fake_clients_do_not_complete(self) -> None:
        for path in ("chunk", "round"):
            with self.subTest(path=path):
                client = _client("fake")
                self.assertEqual(await self._call(path, client), "")
                client.complete.assert_not_awaited()

    async def test_fake_served_fallback_results_are_discarded(self) -> None:
        for path in ("chunk", "round"):
            with self.subTest(path=path):
                client = _client("primary", "fake")
                self.assertEqual(await self._call(path, client), "")
                client.complete.assert_awaited_once()

    async def test_real_served_results_are_kept(self) -> None:
        for path in ("chunk", "round"):
            with self.subTest(path=path):
                client = _client("primary", "secondary")
                self.assertEqual(await self._call(path, client), "A generated description.")
                client.complete.assert_awaited_once()

    async def test_fake_client_cannot_reuse_a_description_hint(self) -> None:
        prompt = ("system", "user", 60)
        client = _client("fake")
        with patch.object(describe, "describe_prompt", return_value=prompt):
            value = await describe._describe_chunk(
                client,
                SimpleNamespace(scope="sample"),
                hints={describe.describe_key(prompt): "A prefetched description."},
            )
        self.assertEqual(value, "")
        client.complete.assert_not_awaited()

    async def test_ingest_counts_only_accepted_descriptions(self) -> None:
        for provider, served, expected in (
            ("fake", "fake", {}),
            ("primary", "fake", {}),
            ("primary", "secondary", {0: "A generated description."}),
        ):
            with self.subTest(provider=provider, served=served):
                stats = SimpleNamespace(described=0)
                chunk = SimpleNamespace(kind="function", scope="sample")
                with (
                    patch.object(describe, "describe_concurrency", return_value=1),
                    patch.object(describe, "describe_prompt", return_value=("system", "user", 60)),
                ):
                    got = await describe._describe_new_chunks(
                        _client(provider, served), [chunk], stats
                    )
                self.assertEqual(got, expected)
                self.assertEqual(stats.described, len(expected))

    async def test_disabled_description_client_stays_empty(self) -> None:
        stats = SimpleNamespace(described=0)
        with patch.object(describe, "_describe_chunk", new_callable=AsyncMock) as call:
            self.assertEqual(await describe._describe_new_chunks(None, [], stats), {})
        call.assert_not_awaited()
        self.assertEqual(stats.described, 0)

    async def test_round_prefetch_keeps_fake_text_out_of_plans_and_hints(self) -> None:
        for provider in ("fake", "primary"):
            with self.subTest(provider=provider):
                reads = SimpleNamespace(prefetch_cards=AsyncMock(), describe_hints={})
                plan = SimpleNamespace(
                    path="sample.py",
                    to_insert=[SimpleNamespace(kind="function")],
                    descriptions={},
                )
                with (
                    patch.object(describe, "_card_for", new_callable=AsyncMock, return_value=""),
                    patch.object(describe, "describe_prompt", return_value=("system", "user", 60)),
                    patch.object(round_models, "model_concurrency", return_value=1),
                ):
                    await round_models._describe(
                        reads, "example-org", "example-ns", [plan], _client(provider), "model"
                    )
                self.assertEqual(plan.descriptions, {})
                self.assertEqual(list(reads.describe_hints.values()), [""])

    async def test_cli_rejects_fake_descriptions_before_embedding_or_insert(self) -> None:
        for provider in ("fake", "primary"):
            with self.subTest(provider=provider):
                client = _client(provider)
                embedder = SimpleNamespace(
                    model_name="constant-model", embed=AsyncMock(return_value=[0.0])
                )
                repo = SimpleNamespace(
                    top_summaries=AsyncMock(return_value=[{"scope_path": "sample.py"}]),
                    live_chunks_for_path=AsyncMock(
                        return_value=[{"kind": "function", "scope": "sample", "content": "pass"}]
                    ),
                    insert_rows=AsyncMock(),
                )
                args = argparse.Namespace(top_pct=100, suffix="", concurrency=1, limit=0)
                with (
                    patch.object(code_maintenance, "connect_code_plane", new_callable=AsyncMock),
                    patch.object(
                        code_maintenance, "_identity", return_value=("example-org", "example-ns")
                    ),
                    patch.object(code_maintenance, "_compact_after_bulk", new_callable=AsyncMock),
                    patch.object(code_maintenance, "_note", new_callable=Mock),
                    patch("server.code.store.index.CodeRepo", return_value=repo),
                    patch("server.core.ch.get_client", return_value=object()),
                    patch("server.embedding.embedder.get_embedder", return_value=embedder),
                    patch("server.llm.client.get_llm_client", return_value=client),
                    patch.object(describe, "_card_for", new_callable=AsyncMock, return_value=""),
                    patch.object(describe, "describe_prompt", return_value=("system", "user", 60)),
                    redirect_stdout(StringIO()),
                ):
                    self.assertEqual(await code_maintenance._describe(args), 0)
                embedder.embed.assert_not_awaited()
                repo.insert_rows.assert_not_awaited()
                if provider == "fake":
                    client.complete.assert_not_awaited()
                else:
                    client.complete.assert_awaited_once()
