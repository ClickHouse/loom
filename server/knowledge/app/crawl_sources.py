"""Crawl registration binds a current administrator to an exact delegated source."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from server.auth import machines, person_erasure, scim, source_registration
from server.auth.context import current_auth
from server.clients import gdrive
from server.clients import google_workspace as google
from server.clients._vendor import VendorError
from server.core import request_work
from server.core.ch import get_client, own_write_reads, set_query_tag
from server.core.diagnostics import private_work
from server.core.errors import OverloadedError, ValidationError
from server.core.secrets_at_rest import unseal
from server.knowledge.api.models_sources import (
    KnowledgeSourceAddInput,
    KnowledgeSourceAddOutput,
    KnowledgeSourceCheckOutput,
    KnowledgeSourceCrawlProof,
    KnowledgeSourceProbe,
    KnowledgeSourceRotateOutput,
    KnowledgeSourceUpdateInput,
    KnowledgeSourceUpdateOutput,
)
from server.knowledge.app import (
    crawl_authority,
    crawl_delegation,
    crawl_exclusions,
    crawl_expansion,
)
from server.knowledge.app import sources as ops
from server.knowledge.domain import sources as domain
from server.knowledge.domain.source_crawl import Options, Person, email
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import sources as store
from server.settings import settings

_CRAWL_MIGRATION = 114


def unavailable() -> ValidationError:
    return ValidationError("crawl source authority unavailable", code="CREDENTIAL_REFUSED")


@asynccontextmanager
async def private() -> AsyncIterator[None]:
    if not request_work.mark_mirrored():
        raise unavailable()
    with private_work():
        try:
            yield
        except (VendorError, source_registration.Refused, ValueError, TimeoutError):
            raise unavailable() from None


async def require_operator(source: KnowledgeSource) -> source_registration.AdminPermit:
    if source.credential_mode != "crawl" or not request_work.mark_mirrored():
        raise unavailable()
    return await source_registration.current_permit(crawl_authority.scope(source))


@dataclasses.dataclass
class _Draft:
    source: KnowledgeSource
    permit: source_registration.AdminPermit
    expected: KnowledgeSource | None
    deadline: float
    exclusion_version: str = ""
    members: int = 0

    async def validate(self) -> None:
        source = self.source
        if time.monotonic() >= self.deadline:
            raise TimeoutError("crawl registration deadline")
        await self.permit.validate(source.org)
        if not settings.knowledge_mirrored_sources_enabled or not settings.auth_actually_required():
            raise unavailable()
        await person_erasure.require_settled(ch=get_client())
        await ops._require_source_namespace(source.org, source.namespace)
        await ops._require_no_erasure(source.org, source.namespace)
        if self.expected is None:
            await ops._require_free(source.org, source.namespace, source.name)
        else:
            with own_write_reads():
                current = await store.latest_source(source.org, source.namespace, source.name)
            if current != self.expected:
                raise OverloadedError("the crawl source changed; retry", retry_after_s=1)
        if self.exclusion_version:
            index = await crawl_exclusions.Index.open(source.org, ch=get_client())
            if await index.version() != self.exclusion_version:
                raise unavailable()
        await self.permit.validate(source.org)


async def _draft(source: KnowledgeSource, expected: KnowledgeSource | None) -> _Draft:
    permit = await source_registration.current_permit(crawl_authority.scope(source))
    draft = _Draft(
        source, permit, expected, time.monotonic() + settings.knowledge_source_probe_timeout_s
    )
    await draft.validate()
    if not await machines.fleet_at(get_client(), _CRAWL_MIGRATION):
        raise OverloadedError("not every process enforces crawl authority yet", retry_after_s=30)
    await draft.validate()
    return draft


async def _approve(
    source: KnowledgeSource,
    credential: str,
    *,
    expected: KnowledgeSource | None,
    dry_run: bool = False,
    readmit: list[str] | None = None,
) -> tuple[KnowledgeSource, _Draft]:
    draft = await _draft(source, expected)
    async with asyncio.timeout_at(draft.deadline):
        ops._require_sealable(credential)
        account = gdrive.parse_service_account(credential)
        bounds = crawl_authority.configured_bounds()
        if dry_run:
            # A dry run never creates a refusal registry or modifies a pending fence.
            index = await crawl_exclusions.Index.open(source.org, ch=get_client())
        else:
            index = await crawl_exclusions.initialize(
                source.org,
                authority=draft.permit,
                ttl_s=bounds.lease_ttl_s,
                wait_s=bounds.lease_wait_s,
            )
        expansion = await crawl_expansion.expand(
            Options.parse(source.crawl_options),
            account,
            standing=draft,
            exclusions=index,
            limits=bounds.directory,
            deadline=draft.deadline,
        )
        await crawl_delegation.require_read_only(
            account,
            subject=Options.parse(source.crawl_options).directory_subject,
            standing=draft,
            deadline=draft.deadline,
            max_bytes=bounds.directory.body_bytes,
        )
        if readmit:
            index = await _readmit(index, draft, account, readmit, customer=expansion.customer)
            expansion = await crawl_expansion.expand(
                Options.parse(source.crawl_options),
                account,
                standing=draft,
                exclusions=index,
                limits=bounds.directory,
                deadline=draft.deadline,
            )
        draft.members = len(expansion.people)
        draft.exclusion_version = expansion.exclusion_version
        await draft.validate()
        source = dataclasses.replace(source, crawl_customer_id=expansion.customer)
        auth = current_auth()
        if auth is None:
            raise unavailable()
        subject, approval = await source_registration.capture(auth, crawl_authority.scope(source))
        source = dataclasses.replace(
            source,
            registrant_subject=subject,
            approval_enc=approval,
            secret_enc=ops._sealed(ops._SECRET_PURPOSE, credential),
            credential_enc="",
        )
        await draft.validate()
        if await index.version() != expansion.exclusion_version:
            raise unavailable()
        return source, draft


async def _commit(source: KnowledgeSource, draft: _Draft) -> None:
    bounds = crawl_authority.configured_bounds()
    async with crawl_exclusions.org_lease(
        source.org,
        ttl_s=bounds.lease_ttl_s,
        wait_s=bounds.lease_wait_s,
    ):
        async with ops.sources_lease(source.org, source.namespace):
            await draft.validate()
            # Recheck the approval and exclusion fence under the publication leases.
            await crawl_authority.validate_indexed(source)
            await store.write_state(
                source.org, source.namespace, source.name, state_value="", detail={}
            )
            await store.insert_source(source)
            await draft.permit.validate(source.org)
            await crawl_authority.validate_indexed(source)


def _options(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(Options.parse(value).stored()))


def _renew(source: KnowledgeSource, credential: str) -> KnowledgeSource:
    return dataclasses.replace(
        source,
        crawl_generation=uuid.uuid4().hex,
        crawl_credential_hash=hashlib.sha256(credential.encode()).hexdigest(),
        approval_enc="",
        registrant_subject="",
        crawl_customer_id="",
    )


async def add(
    input: KnowledgeSourceAddInput,
    *,
    name: str,
    kind: str,
    config: dict[str, Any],
    items: list[dict[str, Any]],
) -> KnowledgeSourceAddOutput:
    set_query_tag("knowledge.crawl.add")
    async with private():
        if kind != "gdrive" or input.crawl_options is None or not input.credential:
            raise unavailable()
        source = KnowledgeSource(
            org=input.org,
            namespace=input.namespace,
            name=name,
            kind=kind,
            source_id=str(uuid.uuid4()),
            config=config,
            items=items,
            access=domain.MIRRORED_ACCESS,
            credential_mode="crawl",
            crawl_options=_options(input.crawl_options.model_dump()),
        )
        source = _renew(source, input.credential)
        source, draft = await _approve(
            source, input.credential, expected=None, dry_run=input.dry_run
        )
        if not input.dry_run:
            await _commit(source, draft)
        return KnowledgeSourceAddOutput(
            name=name,
            kind=kind,
            namespace=source.namespace,
            items=source.items,
            include=ops._answered_include(source),
            probe=KnowledgeSourceProbe(),
            dry_run=input.dry_run,
            crawl_proof=_proof(source, draft),
        )


async def check(source: KnowledgeSource) -> KnowledgeSourceCheckOutput:
    set_query_tag("knowledge.crawl.check")
    async with private():
        permit = await require_operator(source)
        deadline = time.monotonic() + settings.knowledge_source_probe_timeout_s
        async with asyncio.timeout_at(deadline):
            authority = await crawl_authority.resolve(
                source,
                deadline=deadline,
                bounds=crawl_authority.configured_bounds(),
                presenter=permit,
            )
            await authority.prepare()
            await permit.validate(source.org)
        return KnowledgeSourceCheckOutput(
            name=source.name,
            summary="the delegated read-only authority was accepted",
            checked_at=ops._now_iso(),
        )


async def rotate(source: KnowledgeSource, credential: str) -> KnowledgeSourceRotateOutput:
    set_query_tag("knowledge.crawl.rotate")
    async with private():
        await require_operator(source)
        changed, draft = await _approve(_renew(source, credential), credential, expected=source)
        await _commit(changed, draft)
        return KnowledgeSourceRotateOutput(
            name=source.name, rotated_at=ops._now_iso(), crawl_proof=_proof(changed, draft)
        )


async def update(
    source: KnowledgeSource, input: KnowledgeSourceUpdateInput
) -> KnowledgeSourceUpdateOutput:
    set_query_tag("knowledge.crawl.update")
    async with private():
        await require_operator(source)
        added = ops._items_of("gdrive", input.add_items, input.add_links, "add_", held=source)
        removed = ops._as_held(
            source, ops._items_of("gdrive", input.remove_items, input.remove_links, "remove_")
        )
        change = ops._updated(source, added, removed, ops._include(input.include, "gdrive"))
        candidate = dataclasses.replace(
            change.source,
            crawl_options=(
                _options(input.crawl_options.model_dump())
                if input.crawl_options is not None
                else source.crawl_options
            ),
        )
        # Credential access follows current presenter and exact source standing.
        original = await _draft(source, source)
        await original.validate()
        await crawl_authority.validate_indexed(source)
        credential = unseal(ops._SECRET_PURPOSE, source.secret_enc, warn=False)
        if (
            not credential
            or hashlib.sha256(credential.encode()).hexdigest() != source.crawl_credential_hash
        ):
            raise unavailable()
        changed, draft = await _approve(
            _renew(candidate, credential), credential, expected=source, readmit=input.crawl_readmit
        )
        await _commit(changed, draft)
        return KnowledgeSourceUpdateOutput(
            name=source.name,
            items=changed.items,
            include=ops._answered_include(changed),
            added=change.added,
            removed=change.removed,
            crawl_proof=_proof(changed, draft),
        )


def _proof(source: KnowledgeSource, draft: _Draft) -> KnowledgeSourceCrawlProof:
    key = hmac.digest(settings.jwt_secret.encode(), b"crawl-audit-subjects", "sha256")
    options = Options.parse(source.crawl_options)
    return KnowledgeSourceCrawlProof(
        subject_hashes=[
            hmac.digest(key, (s.kind + ":" + s.email).encode(), "sha256").hex()
            for s in options.subjects
        ],
        read_scopes=[scope.value for scope in gdrive.ReadScope],
        write_denials=[scope.value for scope in gdrive.WriteScope],
        members=draft.members,
    )


async def _readmit(
    index: crawl_exclusions.Index,
    draft: _Draft,
    account: gdrive.ServiceAccount,
    requested: list[str],
    *,
    customer: str,
) -> crawl_exclusions.Index:
    addresses = [email(value) for value in requested]
    if len(addresses) != len(set(addresses)):
        raise unavailable()
    await draft.validate()
    maximum = settings.knowledge_crawl_directory_max_response_bytes
    token = await google.delegated_token(
        account,
        subject=Options.parse(draft.source.crawl_options).directory_subject,
        scope=gdrive.ReadScope.DIRECTORY_USERS,
        deadline=draft.deadline,
        max_bytes=maximum,
    )
    await draft.validate()
    principal = await google.directory_identity(token, max_bytes=maximum)
    await draft.validate()
    if principal.customer_id != customer:
        raise unavailable()
    people = []
    for address in addresses:
        found = await google.user(
            token, address, customer_id=principal.customer_id, max_bytes=maximum
        )
        await draft.validate()
        person = Person(found.customer_id, found.id, found.email, tuple(sorted(found.aliases)))
        for contact in person.addresses:
            if await scim.deactivated(contact):
                raise unavailable()
        await draft.validate()
        people.append(person)

    async def cleanup() -> None:
        await draft.validate()
        for person in people:
            for contact in person.addresses:
                if await scim.deactivated(contact):
                    raise unavailable()
        await draft.validate()

    bounds = crawl_authority.configured_bounds()
    return await index.change(
        tuple(people),
        exclude=False,
        ttl_s=bounds.lease_ttl_s,
        wait_s=bounds.lease_wait_s,
        authority=draft.permit,
        cleanup=cleanup,
    )
