"""profile_schema.set / get / clear and profile.structured.

An admin gives a namespace the shape of its users' structured profiles; the
structured_profile worker builds each user's profile in that shape from the
user's memories and stores it as the user's `structured_profile` profile row
(`write_structured_profile`). A read never calls a model: it returns the row
in the current schema's shape and says whether it is current.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from server.core.ch import own_write_reads
from server.core.datetimes import naive_utc_now
from server.core.errors import AuthInsufficientError, ValidationError
from server.identity.domain.read_scope import effective_read_scope, resolve_read_filters
from server.memory.api.models_crud import MemorySetInput
from server.memory.api.models_profile import (
    ProfileSchemaInput,
    ProfileSchemaOutput,
    ProfileSchemaSetInput,
    ProfileStructuredInput,
    ProfileStructuredOutput,
)
from server.memory.app.profile import write_section
from server.memory.app.provenance import writer_actor
from server.memory.domain import profile_schema
from server.memory.store import profile_schemas as repo
from server.memory.store import profile_sources
from server.memory.store.rows import next_version
from server.settings import settings
from server.settings.budgets import llm_max_tokens

# A profile of 64 long properties is longer than a free-text section's default cap.
STRUCTURED_CHAR_LIMIT = 32768  # not a setting: row bound at the default output cap
# Characters one output token can turn into once stored as JSON, so the row bound
# grows with a raised `structured_profile` cap instead of refusing its answers.
CHARS_PER_OUTPUT_TOKEN = 8  # not a setting: JSON characters per output token, an upper bound


async def handle_profile_schema_set(input: ProfileSchemaSetInput) -> ProfileSchemaOutput:
    """Set the namespace's profile schema. A changed schema leaves every
    profile built under the old one `stale` until the worker rebuilds it."""
    props = profile_schema.parse(input.json_schema)
    now = naive_utc_now()
    row = {
        "org": input.org,
        "namespace": input.namespace,
        "schema_json": profile_schema.stored_form(input.json_schema),
        "schema_sha256": profile_schema.schema_sha256(props),
        "updated_by": writer_actor(),
        "updated_at": now,
        "tombstoned_at": None,
        "version": next_version(),
    }
    await repo.insert_row(row)
    return _output(row)


async def handle_profile_schema_get(input: ProfileSchemaInput) -> ProfileSchemaOutput:
    """The namespace's profile schema, its hash, and who set it when."""
    with own_write_reads():  # an admin reads right after setting it, on any replica
        row = await repo.get(input.org, input.namespace)
    return _output(row) if row else ProfileSchemaOutput(found=False)


async def handle_profile_schema_clear(input: ProfileSchemaInput) -> ProfileSchemaOutput:
    """Clear the schema: no profile is built or rebuilt for the namespace, and
    reads answer `no_schema`. The profiles already built stay, as memory rows
    their users' erasure removes. Answers the schema it cleared (`found` true), or
    `found` false when there was none."""
    with own_write_reads():
        row = await repo.get(input.org, input.namespace)
    if row is None:
        return ProfileSchemaOutput(found=False)
    now = naive_utc_now()
    cleared = {
        "org": input.org,
        "namespace": input.namespace,
        "schema_json": row["schema_json"],
        "schema_sha256": row["schema_sha256"],
        "updated_by": writer_actor(),
        "updated_at": now,
        "tombstoned_at": now,
        "version": next_version(),
    }
    await repo.insert_row(cleared)
    return _output(cleared)


def _output(row: dict[str, Any]) -> ProfileSchemaOutput:
    return ProfileSchemaOutput(
        found=True,
        json_schema=json.loads(row["schema_json"]),
        schema_sha256=str(row["schema_sha256"]),
        updated_by=str(row.get("updated_by") or ""),
        updated_at=row.get("updated_at"),
    )


async def write_structured_profile(
    *,
    org: str,
    namespace: str,
    user_id: str,
    label: str,
    value: dict[str, Any],
    schema_sha256: str,
    sources_digest: str,
    source_count: int,
    sources_read_at: datetime,
    memory_ids: list[str],
) -> None:
    """Write a built profile as a new version of the user's profile row. The trace
    names the build and the memories it read, so provenance classes it `derived`;
    `valid_at` is when those memories were read, the point the profile is true as of."""
    await write_section(
        MemorySetInput(
            org=org,
            namespace=namespace,
            agent="-",
            user_id=user_id,
            memory_key=label,
            memory_type="profile",
            value=value,
            tags=profile_sources.profile_tags(schema_sha256, sources_digest, source_count),
            valid_at=sources_read_at,
            char_limit=max(
                STRUCTURED_CHAR_LIMIT,
                CHARS_PER_OUTPUT_TOKEN * llm_max_tokens("structured_profile"),
            ),
            source_trace=f"profile.fold:{label}:" + ",".join(memory_ids),
        )
    )


async def handle_profile_structured(input: ProfileStructuredInput) -> ProfileStructuredOutput:
    """The user's structured profile and whether it is current. No model call:
    a profile that is behind is served as built, marked `stale`."""
    # Lazy: server.auth.context pulls in authz -> operations -> the handlers, this one too.
    from server.auth.context import current_auth

    auth = current_auth()
    # own_session resolves once a session_id is named; a profile spans sessions, so refuse it here.
    if effective_read_scope(input.read_scope, auth) == "own_session":
        raise AuthInsufficientError(
            "profile.structured reads a user's profile, which spans their sessions; "
            "a read bounded to one session cannot take it"
        )
    rf = resolve_read_filters(
        requested=input.read_scope,
        user_id=input.user_id,
        session_id=input.session_id,
        auth=auth,
    )
    uid = rf.user_id
    if not uid or uid in ("-", "*"):
        raise ValidationError("profile.structured reads one user's profile: name user_id")

    with own_write_reads():
        schema = await repo.get(input.org, input.namespace)
        if schema is None:
            return ProfileStructuredOutput(status="no_schema")
        try:
            props = profile_schema.parse(json.loads(schema["schema_json"]))
        except (ValueError, ValidationError):
            # A stored schema that no longer parses is the namespace's to fix, never
            # the reader's: the worker builds nothing under it either.
            return ProfileStructuredOutput(status="no_schema")
        sha = str(schema["schema_sha256"])
        row = await profile_sources.profile_row(
            org=input.org,
            namespace=input.namespace,
            user_id=uid,
            label=settings.structured_profile_label,
            scope=rf,
        )
        if row is None:
            return ProfileStructuredOutput(
                status="pending", profile=profile_schema.project(props, {}), schema_sha256=sha
            )
        digest, _ = await profile_sources.sources_digest(
            org=input.org, namespace=input.namespace, user_id=uid, scope=rf
        )
    current = profile_sources.is_current(list(row.get("tags") or []), sha, digest)
    return ProfileStructuredOutput(
        status="ready" if current else "stale",
        # memory.set stores canonical JSON; a value that is no object projects to all null.
        profile=profile_schema.project(props, json.loads(row["value"])),
        built_at=row["updated_at"],
        memory_id=str(row["memory_id"]),
        version=int(row["version"]),
        version_text=str(row["version"]),
        schema_sha256=sha,
    )
