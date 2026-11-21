"""Before contact deletion, prove Google identity through the existing exact source authority.

This path can only capture Directory identity. It cannot read Drive content, serve
results, substitute a request identity or admit a crawl while deletion is pending.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass

from server.auth import person_erasure, source_registration
from server.clients import gdrive
from server.clients import google_workspace as google
from server.clients._vendor import CredentialRefused
from server.core.ch import get_client, set_query_tag
from server.core.secrets_at_rest import unseal
from server.knowledge.app import crawl_authority
from server.knowledge.app import sources as source_ops
from server.knowledge.domain.source_crawl import Options
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import sources as store
from server.settings import settings

_now = time.monotonic


@dataclass(frozen=True, repr=False)
class _Standing:
    pending: person_erasure.Pending
    source: KnowledgeSource
    approval: source_registration.Approval
    deadline: float

    async def validate(self) -> None:
        if _now() >= self.deadline:
            raise TimeoutError("crawl identity capture deadline")
        await self.pending.validate(self.source.org, before_delete=True)
        crawl_authority._shape(self.source)
        await self.approval.validate()
        await source_ops._require_source_namespace(self.source.org, self.source.namespace)
        await source_ops._require_no_erasure(self.source.org, self.source.namespace)
        current = await store.latest_source(
            self.source.org, self.source.namespace, self.source.name, ch=get_client()
        )
        if (
            current is None
            or not current.active
            or crawl_authority.scope(current) != crawl_authority.scope(self.source)
            or current.approval_enc != self.source.approval_enc
            or current.secret_enc != self.source.secret_enc
            or current.registrant_subject != self.source.registrant_subject
            or current.owner_subject != self.source.owner_subject
            or current.connection_id != self.source.connection_id
            or current.credential_enc != self.source.credential_enc
        ):
            raise CredentialRefused("crawl identity authority changed")
        crawl_authority._shape(current)
        await self.pending.validate(self.source.org, before_delete=True)


async def _token(
    pending: person_erasure.Pending, source: KnowledgeSource, deadline: float
) -> tuple[_Standing, google.DelegatedToken]:
    approval = source_registration.load(
        source.approval_enc, crawl_authority.scope(source), registrant=source.registrant_subject
    )
    standing = _Standing(pending, source, approval, deadline)
    await standing.validate()
    plain = unseal(source_ops._SECRET_PURPOSE, source.secret_enc, warn=False)
    if not plain or not secrets.compare_digest(
        hashlib.sha256(plain.encode()).hexdigest(), source.crawl_credential_hash
    ):
        raise CredentialRefused("crawl identity credential unavailable")
    account = gdrive.parse_service_account(plain)
    await standing.validate()
    options = Options.parse(source.crawl_options)
    token = await google.delegated_token(
        account,
        subject=options.directory_subject,
        scope=gdrive.ReadScope.DIRECTORY_USERS,
        deadline=deadline,
        max_bytes=settings.knowledge_crawl_directory_max_response_bytes,
    )
    await standing.validate()
    principal = await google.directory_identity(
        token, max_bytes=settings.knowledge_crawl_directory_max_response_bytes
    )
    await standing.validate()
    if principal.customer_id != source.crawl_customer_id:
        raise CredentialRefused("crawl identity workspace changed")
    return standing, token


async def _for_workspace(
    pending: person_erasure.Pending,
    person: person_erasure.Person,
    source: KnowledgeSource,
    deadline: float,
) -> tuple[person_erasure.Workspace, ...]:
    standing, token = await _token(pending, source, deadline)
    found: dict[str, person_erasure.Workspace] = {}
    for address in person.emails:
        await standing.validate()
        user = await google.user(
            token,
            address,
            customer_id=source.crawl_customer_id,
            max_bytes=settings.knowledge_crawl_directory_max_response_bytes,
        )
        await standing.validate()
        observed = person_erasure.Workspace(
            person.org,
            person.subject,
            user.customer_id,
            user.id,
            tuple(sorted({user.email.lower(), *(a.lower() for a in user.aliases)})),
        )
        if address not in observed.emails or user.id in found and found[user.id] != observed:
            raise CredentialRefused("crawl identity changed during capture")
        found[user.id] = observed
    return tuple(found[key] for key in sorted(found))


async def _current_sources(org: str) -> list[KnowledgeSource]:
    history = await store.crawl_history(
        org, maximum=settings.knowledge_crawl_registry_scan_limit, ch=get_client()
    )
    customers = {source.crawl_customer_id for source in history}
    if "" in customers:
        raise CredentialRefused("crawl workspace history unavailable")
    current: dict[str, KnowledgeSource] = {}
    for namespace, name in sorted({(s.namespace, s.name) for s in history}):
        source = await store.latest_source(org, namespace, name, ch=get_client())
        if source is not None and source.active and source.credential_mode == "crawl":
            current.setdefault(source.crawl_customer_id, source)
    if set(current) != customers:
        # A lost/expired authority cannot be replaced by guessing a current user's id.
        raise CredentialRefused("historical crawl identity requires verified repair")
    return [current[key] for key in sorted(current)]


async def prepare(pending: person_erasure.Pending) -> tuple[person_erasure.Workspace, ...]:
    """Capture only existing crawl workspaces; absence never invents provider configuration."""
    set_query_tag("knowledge.crawl.person_capture")
    deadline = _now() + settings.knowledge_crawl_identity_capture_timeout_s
    result: list[person_erasure.Workspace] = []
    async with asyncio.timeout_at(deadline):
        for person in pending.captured.people:
            await pending.validate(person.org, before_delete=True)
            for source in await _current_sources(person.org):
                await pending.validate(person.org, before_delete=True)
                result.extend(await _for_workspace(pending, person, source, deadline))
        await pending.validate(before_delete=True)
    return tuple(sorted(result, key=lambda w: (w.org, w.subject, w.customer, w.user)))
