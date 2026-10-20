"""profile_schema.set / get / clear and profile.structured.

An admin gives a namespace the shape of its users' structured profiles; the
structured_profile worker builds each user's profile in that shape from the
user's memories and stores it as the user's `structured_profile` profile row
(`write_structured_profile`). A read never calls a model: it returns the row
in the current schema's shape and says whether it is current.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from server.core.errors import ValidationError
from server.memory.api.models_crud import MemorySetInput
from server.memory.api.models_profile import (
    ProfileSchemaInput,
    ProfileSchemaOutput,
    ProfileSchemaSetInput,
    ProfileStructuredInput,
    ProfileStructuredOutput,
)
from server.memory.domain import profile_schema
from server.memory.store import profile_schemas as repo
from server.memory.store import profile_sources
from server.settings import settings

# A profile of 64 long properties is longer than a free-text section's default cap.
STRUCTURED_CHAR_LIMIT = 32768


def _caller() -> str:
    from server.auth.context import current_auth

    auth = current_auth()
    return (auth.subject if auth is not None else "") or "admin"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def handle_profile_schema_set(input: ProfileSchemaSetInput) -> ProfileSchemaOutput:
    """Set the namespace's profile schema. A changed schema leaves every
    profile built under the old one `stale` until the worker rebuilds it."""
    profile_schema.parse(input.json_schema)
    now = _now()
    row = {
        "org": input.org,
        "namespace": input.namespace,
        "schema_json": profile_schema.canonical(input.json_schema),
        "schema_sha256": profile_schema.schema_sha256(input.json_schema),
        "updated_by": _caller(),
        "updated_at": now,
        "tombstoned_at": None,
        "version": time.time_ns(),
    }
    await repo.insert_row(row)
    return _output(row)


async def handle_profile_schema_get(input: ProfileSchemaInput) -> ProfileSchemaOutput:
    from server.core.ch import own_write_reads

    with own_write_reads():  # an admin reads right after setting it, on any replica
        row = await repo.get(input.org, input.namespace)
    return _output(row) if row else ProfileSchemaOutput(found=False)


async def handle_profile_schema_clear(input: ProfileSchemaInput) -> ProfileSchemaOutput:
    """Clear the schema: no profile is built or rebuilt for the namespace, and
    reads answer `no_schema`. The profiles already built stay, as memory rows
    their users' erasure removes."""
    from server.core.ch import own_write_reads

    with own_write_reads():
        row = await repo.get(input.org, input.namespace)
    if row is None:
        return ProfileSchemaOutput(found=False)
    await repo.insert_row(
        {
            "org": input.org,
            "namespace": input.namespace,
            "schema_json": row["schema_json"],
            "schema_sha256": row["schema_sha256"],
            "updated_by": _caller(),
            "updated_at": _now(),
            "tombstoned_at": _now(),
            "version": time.time_ns(),
        }
    )
    return ProfileSchemaOutput(found=False)


def _output(row: dict[str, Any]) -> ProfileSchemaOutput:
    return ProfileSchemaOutput(
        found=True,
        json_schema=json.loads(row["schema_json"]),
        schema_sha256=str(row["schema_sha256"]),
        updated_by=str(row.get("updated_by") or ""),
        updated_at=row.get("updated_at"),
    )


def profile_tags(schema_sha256: str, sources_digest: str) -> list[str]:
    """The tags a built profile carries: the schema and the sources it was built from."""
    return [
        profile_sources.SCHEMA_TAG + schema_sha256[:16],
        profile_sources.SOURCES_TAG + sources_digest,
    ]


async def write_structured_profile(
    *,
    org: str,
    namespace: str,
    user_id: str,
    value: dict[str, Any],
    schema_sha256: str,
    sources_digest: str,
    memory_ids: list[str],
) -> None:
    """Write a built profile as a new version of the user's profile row. The trace
    names the build and the memories it read, so provenance classes it `derived`."""
    from server.memory.app.profile import _write_section

    label = settings.structured_profile_label
    await _write_section(
        MemorySetInput(
            org=org,
            namespace=namespace,
            agent="-",
            user_id=user_id,
            memory_key=label,
            memory_type="profile",
            value=value,
            tags=profile_tags(schema_sha256, sources_digest),
            char_limit=STRUCTURED_CHAR_LIMIT,
            source_trace=f"profile.fold:{label}:" + ",".join(memory_ids),
        )
    )


async def handle_profile_structured(input: ProfileStructuredInput) -> ProfileStructuredOutput:
    """The user's structured profile and whether it is current. No model call:
    a profile that is behind is served as built, marked `stale`."""
    from server.auth.context import current_auth
    from server.core.ch import own_write_reads
    from server.identity.domain.read_scope import resolve_read_filters

    rf = resolve_read_filters(
        requested=input.read_scope,
        user_id=input.user_id,
        session_id=input.session_id,
        auth=current_auth(),
    )
    uid = rf.user_id
    if not uid or uid in ("-", "*"):
        raise ValidationError("profile.structured reads one user's profile: name user_id")

    with own_write_reads():
        schema = await repo.get(input.org, input.namespace)
        if schema is None:
            return ProfileStructuredOutput(status="no_schema")
        props = profile_schema.parse(json.loads(schema["schema_json"]))
        sha = str(schema["schema_sha256"])
        row = await profile_sources.profile_row(
            org=input.org,
            namespace=input.namespace,
            user_id=uid,
            label=settings.structured_profile_label,
        )
        if row is None:
            return ProfileStructuredOutput(
                status="pending", profile=profile_schema.project(props, {}), schema_sha256=sha
            )
        digest = await profile_sources.sources_digest(
            org=input.org, namespace=input.namespace, user_id=uid
        )
    try:
        stored = json.loads(row["value"] or "{}")
    except ValueError:
        stored = {}
    current = set(profile_tags(sha, digest)) <= set(row.get("tags") or [])
    return ProfileStructuredOutput(
        status="ready" if current else "stale",
        profile=profile_schema.project(props, stored),
        built_at=row["updated_at"],
        memory_id=str(row["memory_id"]),
        version=int(row["version"]),
        schema_sha256=sha,
    )
