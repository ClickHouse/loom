"""A file whose compiler-tier document a delta skipped reads as stale, although the
tier's watermark moved past the file's last change, until a later delta applies it. The
marks stay out of the whole-namespace state reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.code.app.index.scip_delta import ScipDelta, ScipFile, apply_delta
from server.code.store.index import CodeRepo
from server.core.ch import get_client
from tests.integration._resources import unique_org


@pytest.mark.asyncio
async def test_a_skipped_document_is_stale_until_a_delta_applies_it(ch_client: Any) -> None:
    org, ns = unique_org("scip-stale-mark"), "code-stale-mark"
    paths = ["src/a.cpp", "src/bad.cpp"]
    changed = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(hours=1)
    ch_client.insert(
        "loom.code_files",
        [[org, ns, p, changed] for p in paths],
        column_names=["org", "namespace", "path", "created_at"],
    )
    await get_client().connect()
    repo = CodeRepo()
    await repo.set_state(org, ns, "scip_commit", "a" * 40)  # the tier ran after the change
    assert await repo.scip_stale_paths(org, ns, paths) == set()

    skip = ScipDelta(commit="b" * 40, skipped_files=["src/bad.cpp"])
    await apply_delta(repo, org, ns, skip, tier="clang")
    await repo.set_state(org, ns, "scip_commit", "b" * 40)
    assert await repo.scip_stale_paths(org, ns, paths) == {"src/bad.cpp"}
    assert await repo.scip_stale_marks(org, ns, "clang") == {"src/bad.cpp"}
    assert await repo.scip_stale_marks(org, ns, "python") == set()
    assert not any(k.startswith("scip_stale:") for k in await repo.all_state(org, ns))
    many = await repo.all_state_many([(org, ns)])
    assert not any(k.startswith("scip_stale:") for k in many[(org, ns)])

    await apply_delta(
        repo,
        org,
        ns,
        ScipDelta(commit="c" * 40, files=[ScipFile(path="src/bad.cpp")]),
        tier="clang",
    )
    assert await repo.scip_stale_paths(org, ns, paths) == set()
    assert await repo.scip_stale_marks(org, ns, "clang") == set()
