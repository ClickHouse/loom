"""Source-bound notification setup and authenticated sync hints."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import json
import logging
import secrets
import time
import uuid
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from server.auth import machines
from server.clients import gdrive, gdrive_push, linear, notion
from server.core import leases
from server.core.ch import get_client, sequential_reads
from server.core.errors import BadConfigError, OverloadedError, ValidationError
from server.core.lease_writes import fenced
from server.core.secrets_at_rest import resealed, seal, unseal
from server.knowledge.api.models_source_push import (
    PushConfigureInput,
    PushConfirmInput,
    PushSourceInput,
    PushStatusInput,
    PushStatusOutput,
)
from server.knowledge.app import sources
from server.knowledge.domain import source_push as domain
from server.knowledge.domain.source_push import DeliveryRefused, PushRegistration
from server.knowledge.domain.sources import KnowledgeSource, json_object
from server.knowledge.store import source_push as store
from server.knowledge.store import sources as source_store
from server.memory.app.provenance import writer_actor
from server.settings import settings

log = logging.getLogger(__name__)
PUSH_MIGRATION = 109


def _require_enabled() -> None:
    if not settings.knowledge_source_push_enabled:
        raise BadConfigError("an operator must enable source push on this deployment")


async def require_ready(*, ch: Any = None) -> None:
    if not await machines.fleet_at(ch or get_client(), PUSH_MIGRATION):
        raise BadConfigError("push setup requires every live build to support its erasure")


def _fingerprint(src: KnowledgeSource) -> str:
    credential = sources.credential_of(src)
    if not credential:
        raise DeliveryRefused("source credential unavailable")
    return domain.fingerprint(credential=credential, items=src.items, config=src.config)


def _secrets(reg: PushRegistration) -> dict[str, Any]:
    raw = unseal(domain.SECRET_PURPOSE, reg.secret_enc)
    if not raw:
        raise DeliveryRefused("push secret unavailable")
    return domain.payload(raw.encode())


def _sealed(data: dict[str, Any]) -> str:
    value = seal(domain.SECRET_PURPOSE, json.dumps(data, sort_keys=True))
    if not value:
        raise BadConfigError("a signing root is required to seal push setup")
    return value


def callback_url(reg: PushRegistration) -> str:
    base = str(settings.public_base_url).strip().rstrip("/")
    try:
        parsed = urlsplit(base)
    except ValueError as exc:
        raise BadConfigError(
            "push setup requires LOOM_PUBLIC_BASE_URL with an HTTPS origin"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise BadConfigError("push setup requires LOOM_PUBLIC_BASE_URL with an HTTPS origin")
    return (
        f"{base}/hooks/knowledge/{quote(reg.namespace, safe='')}/{reg.source_id}"
        f"/{reg.generation}?{urlencode({'org': reg.org})}"
    )


async def _source(input: PushSourceInput) -> KnowledgeSource:
    src = await sources._source_or_404(input.org, input.namespace, input.name)
    sources._require_served(src)
    await sources._require_source_namespace(src.org, src.namespace)
    return src


async def _fresh(reg: PushRegistration) -> KnowledgeSource:
    """Called under sources_lease, with current strong reads."""
    fresh = await source_store.get_source(reg.org, reg.namespace, reg.name)
    current = await store.get(reg.org, reg.namespace, reg.source_id)
    if (
        fresh is None
        or current is None
        or current.generation != reg.generation
        or current.status == "retired"
        or fresh.source_id != reg.source_id
        or fresh.kind != reg.provider
    ):
        raise DeliveryRefused("push setup no longer current")
    sources._require_served(fresh)
    await sources._require_source_namespace(reg.org, reg.namespace)
    await sources._require_no_erasure(reg.org, reg.namespace)
    if _fingerprint(fresh) != reg.fingerprint:
        raise DeliveryRefused("source setup changed")
    return fresh


async def _put(reg: PushRegistration, *, retiring: bool = False, ch: Any = None) -> None:
    async with fenced(domain.writer_lease_name(reg.org, reg.namespace), ch=ch) as uncertain:
        if not retiring:
            await require_ready(ch=ch)
        uncertain(True)
        await store.put(dataclasses.replace(reg, actor=writer_actor("process:source-push")), ch=ch)
        uncertain(False)


async def retire(src: KnowledgeSource) -> None:
    """Caller holds the source lease; clearing setup cannot enable an old build."""
    current = await store.get(src.org, src.namespace, src.source_id)
    if current and current.status != "retired":
        await _put(dataclasses.replace(current, status="retired", secret_enc=""), retiring=True)


async def reseal_registry(*, dry_run: bool, ch: Any = None) -> tuple[int, list[str]]:
    """Re-read under the lifecycle lease; a scanned row cannot recreate an erased one."""
    after = ("", "", "")
    count = 0
    lost: list[str] = []
    while page := await store.sealed_page(after, ch=ch):
        for observed in page:
            async with sources.sources_lease(observed.org, observed.namespace, ch=ch):
                with sequential_reads():
                    await sources._require_no_erasure(observed.org, observed.namespace, ch=ch)
                    current = await store.get(
                        observed.org, observed.namespace, observed.source_id, ch=ch
                    )
                    if current is None or not current.secret_enc:
                        continue
                    fresh = await source_store.get_source(
                        current.org, current.namespace, current.name, ch=ch
                    )
                    if fresh is None or fresh.source_id != current.source_id:
                        if not dry_run:
                            await _put(
                                dataclasses.replace(current, status="retired", secret_enc=""),
                                retiring=True,
                                ch=ch,
                            )
                        continue
                    new = resealed(domain.SECRET_PURPOSE, current.secret_enc)
                    if new is None:
                        if not unseal(domain.SECRET_PURPOSE, current.secret_enc, warn=False):
                            lost.append(f"{current.org}/{current.namespace}/{current.source_id}")
                        continue
                    count += 1
                    if not dry_run:
                        await _put(dataclasses.replace(current, secret_enc=new), ch=ch)
        after = (page[-1].org, page[-1].namespace, page[-1].source_id)
    return count, lost


async def handle_configure(input: PushConfigureInput) -> PushStatusOutput:
    src = await _source(input)
    if not input.enabled:
        async with sources.sources_lease(src.org, src.namespace):
            with sequential_reads():
                fresh = await _source(input)
                if fresh.source_id != src.source_id:
                    raise OverloadedError("source changed; retry")
                await retire(fresh)
        return PushStatusOutput(
            provider=src.kind, enabled=bool(settings.knowledge_source_push_enabled)
        )
    _require_enabled()
    await require_ready()
    if src.kind not in {"gdrive", "notion", "linear"}:
        raise ValidationError("push supports gdrive, notion and linear sources")
    reg = PushRegistration(
        org=src.org,
        namespace=src.namespace,
        name=src.name,
        source_id=src.source_id,
        provider=src.kind,
        generation=str(uuid.uuid4()),
        fingerprint=_fingerprint(src),
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        actor=writer_actor("process:source-push"),
    )
    callback_url(reg)
    credential = sources.credential_of(src)
    async with asyncio.timeout(settings.knowledge_source_push_provider_timeout_s):
        if src.kind == "notion":
            reg = dataclasses.replace(reg, provider_id=await notion.workspace_id(credential))
        elif src.kind == "linear":
            reg = dataclasses.replace(reg, provider_id=await linear.organization_id(credential))
    reg = dataclasses.replace(reg, secret_enc=_sealed({}))
    async with sources.sources_lease(src.org, src.namespace):
        with sequential_reads():
            fresh = await _source(input)
            await sources._require_no_erasure(src.org, src.namespace)
            if fresh.source_id != src.source_id or _fingerprint(fresh) != reg.fingerprint:
                raise OverloadedError("source changed during setup; retry")
            await _put(reg)
    if src.kind == "gdrive":
        await renew_drive(reg, initial=True)
    return await handle_status(PushStatusInput(org=src.org, namespace=src.namespace, name=src.name))


async def handle_status(input: PushStatusInput) -> PushStatusOutput:
    src = await _source(input)
    with sequential_reads():
        reg = await store.get(src.org, src.namespace, src.source_id)
    if reg is None or reg.status == "retired":
        return PushStatusOutput(
            provider=src.kind, enabled=bool(settings.knowledge_source_push_enabled)
        )
    if _fingerprint(src) != reg.fingerprint:
        return PushStatusOutput(
            status="obsolete",
            provider=src.kind,
            enabled=bool(settings.knowledge_source_push_enabled),
        )
    data = _secrets(reg)
    challenge = str(data.get("challenge") or "") if reg.status == "pending" else ""
    channels = data.get("channels") or []
    expiration = min((float(c["expiration"]) for c in channels), default=0)
    status = "expired" if channels and expiration <= time.time() else reg.status
    try:
        address = callback_url(reg)
    except BadConfigError:
        # A paused deployment may have removed its public origin; admins must
        # still be able to inspect and retire existing setup.
        address = ""
    return PushStatusOutput(
        enabled=bool(settings.knowledge_source_push_enabled),
        status=status,
        provider=reg.provider,
        generation=reg.generation,
        callback_url=address,
        provider_id=reg.provider_id,
        challenge_fingerprint=domain.challenge_fingerprint(challenge) if challenge else "",
        verification_token=challenge if input.reveal_challenge else "",
        channels=len(channels),
        expires_at=expiration,
    )


async def handle_confirm(input: PushConfirmInput) -> PushStatusOutput:
    _require_enabled()
    src = await _source(input)
    async with sources.sources_lease(src.org, src.namespace):
        with sequential_reads():
            reg = await store.get(src.org, src.namespace, src.source_id)
            if (
                reg is None
                or reg.provider not in {"notion", "linear"}
                or reg.status != "pending"
                or reg.generation != input.generation
            ):
                raise ValidationError("the pending setup changed")
            await _fresh(reg)
            if reg.provider == "linear":
                if not input.signing_secret.strip() or input.challenge_fingerprint:
                    raise ValidationError("the Linear webhook signing secret is required")
                reg = dataclasses.replace(
                    reg, secret_enc=_sealed({"signing_secret": input.signing_secret})
                )
            else:
                challenge = str(_secrets(reg).get("challenge") or "")
                if (
                    input.signing_secret
                    or not challenge
                    or not hmac.compare_digest(
                        domain.challenge_fingerprint(challenge), input.challenge_fingerprint
                    )
                ):
                    raise ValidationError("the pending challenge changed")
            await _put(dataclasses.replace(reg, status="active"))
    return await handle_status(PushStatusInput(org=src.org, namespace=src.namespace, name=src.name))


async def receive(
    org: str, namespace: str, source_id: str, generation: str, raw: bytes, headers: dict[str, str]
) -> None:
    if not settings.knowledge_source_push_enabled:
        raise DeliveryRefused("push is disabled")
    with sequential_reads():
        reg = await store.get(org, namespace, source_id)
    if reg is None or reg.generation != generation or reg.status not in {"pending", "active"}:
        raise DeliveryRefused("unverifiable delivery")
    async with sources.sources_lease(reg.org, reg.namespace):
        with sequential_reads():
            fresh = await _fresh(reg)
            current = await store.get(org, namespace, source_id)
            if current is None:
                raise DeliveryRefused("unverifiable delivery")
            data = _secrets(current)
            if current.provider == "notion" and current.status == "pending":
                body = domain.payload(raw)
                challenge = body.get("verification_token")
                if (
                    not isinstance(challenge, str)
                    or not 16 <= len(challenge) <= 512
                    or set(body) != {"verification_token"}
                ):
                    raise DeliveryRefused("invalid verification challenge")
                existing = data.get("challenge")
                if existing and existing != challenge:
                    raise DeliveryRefused("verification challenge already captured")
                if not existing:
                    await _put(
                        dataclasses.replace(
                            current, secret_enc=_sealed({**data, "challenge": challenge})
                        )
                    )
                return
            if current.provider == "notion":
                domain.verify_notion(
                    raw,
                    headers.get("x-notion-signature", ""),
                    str(data.get("challenge") or ""),
                    current.provider_id,
                )
            elif current.provider == "linear":
                domain.verify_linear(
                    raw,
                    headers.get("linear-signature", ""),
                    str(data.get("signing_secret") or ""),
                    current.provider_id,
                    now=time.time(),
                )
            elif current.provider == "gdrive":
                pending = data.get("pending_channels") or []
                if headers.get("x-goog-resource-state") == "sync" and any(
                    c["id"] == headers.get("x-goog-channel-id")
                    and hmac.compare_digest(
                        str(c["token"]).encode(), headers.get("x-goog-channel-token", "").encode()
                    )
                    for c in pending
                ):
                    return
                if not domain.verify_drive(headers, data.get("channels") or [], now=time.time()):
                    return
            else:
                raise DeliveryRefused("unverifiable delivery")
            state = await sources._read_state(fresh)
            detail = json_object(state.get("detail", ""))
            if not detail.get("requested_at"):
                async with fenced(domain.writer_lease_name(reg.org, reg.namespace)) as uncertain:
                    uncertain(True)
                    await sources._update_state(
                        fresh, state, {"requested_at": sources._now_iso(), "full": False}
                    )
                    uncertain(False)


async def _stop_channels(token: str, channels: list[dict[str, Any]]) -> None:
    for channel in channels:
        try:
            async with asyncio.timeout(settings.knowledge_source_push_stop_timeout_s):
                await gdrive_push.stop(token, channel)
        except Exception:  # noqa: BLE001 — expiry and polling recover failed channel cleanup
            log.warning("source push channel cleanup failed")


async def renew_drive(reg: PushRegistration, *, initial: bool = False) -> None:
    _require_enabled()
    async with leases.holding(
        f"knowledge-push:{reg.org}:{reg.source_id}", ttl_s=120, wait_s=0
    ) as held:
        if not held:
            raise OverloadedError("source push renewal already running")
        await _renew_drive(reg, initial=initial)


async def _renew_drive(reg: PushRegistration, *, initial: bool) -> None:
    created: list[dict[str, Any]] = []
    publication_attempted = False
    token = ""
    async with asyncio.timeout(settings.knowledge_source_push_provider_timeout_s):
        async with sources.sources_lease(reg.org, reg.namespace):
            with sequential_reads():
                src = await _fresh(reg)
                current = await store.get(reg.org, reg.namespace, reg.source_id)
                if current is None:
                    raise DeliveryRefused("setup missing")
                data = _secrets(current)
                old = data.get("channels") or []
                if (
                    not initial
                    and old
                    and min(float(c["expiration"]) for c in old) > time.time() + 86400
                ):
                    return
        token = await gdrive.access_token(gdrive.parse_service_account(sources.credential_of(src)))
        drives = await gdrive_push.member_drives(token)
        corpora = ["", *sorted(drives)]
        pending = [
            {"id": str(uuid.uuid4()), "token": secrets.token_urlsafe(32), "drive_id": d}
            for d in corpora
        ]
        async with sources.sources_lease(reg.org, reg.namespace):
            with sequential_reads():
                await _fresh(reg)
                await _put(
                    dataclasses.replace(
                        current, secret_enc=_sealed({**data, "pending_channels": pending})
                    )
                )
        try:
            for channel in pending:
                created.append(
                    await gdrive_push.watch(
                        token,
                        channel_id=channel["id"],
                        channel_token=channel["token"],
                        address=callback_url(reg),
                        drive_id=channel["drive_id"],
                    )
                )
            async with sources.sources_lease(reg.org, reg.namespace):
                with sequential_reads():
                    await _fresh(reg)
                    publication_attempted = True
                    await _put(
                        dataclasses.replace(
                            current, status="active", secret_enc=_sealed({"channels": created})
                        )
                    )
        except BaseException:
            # Cancellation can follow a successful provider request; stop every
            # known channel without publishing a response to a retired generation.
            if token and created and not publication_attempted:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(settings.knowledge_source_push_cleanup_timeout_s):
                        await _stop_channels(token, created)
            raise
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(settings.knowledge_source_push_cleanup_timeout_s):
            await _stop_channels(token, old)


def audit_subject(input: Any, response: Any) -> dict[str, str]:
    return {"org": str(input.org), "namespace": str(input.namespace), "name": str(input.name)}
