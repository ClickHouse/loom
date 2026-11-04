"""A user's erasure spares the originals another writer still needs, on a real
ClickHouse: two users of one namespace share content-addressed originals (the
same file uploaded twice, the same artifact extracted from it), and erasing the
first leaves the bytes the second's live document, unexpired upload and open
ingest job hold.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


def _own(ch_client: Any, org: str, uid: str) -> set[str]:
    from server.erasure.store.steps_memory import _USERS_OWN_ORIGINALS

    rows = ch_client.query(_USERS_OWN_ORIGINALS, parameters={"org": org, "uid": uid}).result_rows
    return {str(sha) for _ns, sha in rows}


def test_an_original_another_user_still_holds_is_not_the_erased_users(ch_client: Any) -> None:
    org, ns = unique_org("erase-shared"), "docs"
    shared_doc, shared_upload, shared_job, alone = (f"{i:064x}" for i in (1, 2, 3, 4))
    now = datetime.now(UTC).replace(tzinfo=None)
    # The erased user uploaded every one of the four originals.
    ch_client.insert(
        "loom.blob_uploads",
        [[org, ns, sha, "a", now] for sha in (shared_doc, shared_upload, shared_job, alone)],
        column_names=["org", "namespace", "sha256", "user_id", "created_at"],
    )
    # Another user's live document names the first as its artifact, their unexpired
    # upload holds the second and their open job the third.
    ch_client.insert(
        "loom.documents",
        [[org, ns, "repo://b", str(uuid.uuid4()), "b", shared_doc, 1, now]],
        column_names=[
            "org",
            "namespace",
            "source_uri",
            "doc_id",
            "user_id",
            "artifact_sha256",
            "version",
            "ingested_at",
        ],
    )
    ch_client.insert(
        "loom.knowledge_uploads",
        [[org, ns, uuid.uuid4(), shared_upload, 1, "b", now + timedelta(hours=1)]],
        column_names=["org", "namespace", "upload_id", "sha256", "size", "user_id", "expires_at"],
    )
    ch_client.insert(
        "loom.knowledge_ingestions",
        [[org, ns, uuid.uuid4(), "repo://c", uuid.uuid4(), shared_job, "processing", "b", 1]],
        column_names=[
            "org",
            "namespace",
            "ingestion_id",
            "source_uri",
            "upload_id",
            "sha256",
            "state",
            "user_id",
            "version",
        ],
    )
    assert _own(ch_client, org, "a") == {alone}
