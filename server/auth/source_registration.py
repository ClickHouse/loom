"""A source retains checked claim metadata, never a bearer or a request identity."""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from server.auth import aliases, authz, revocation, scim, tenancy
from server.auth.context import current_auth
from server.auth.jwt import AuthInfo, AuthorizationClaims, SigningAuthority, _check_not_revoked
from server.auth.reader_identity import resolve_reader
from server.core import cell, grant_cache
from server.core.ch import own_write_reads
from server.core.secrets_at_rest import seal, unseal
from server.operations.base import Role
from server.settings import settings

PURPOSE = "knowledge_crawl.approval"
_now = time.time
_CLAIMS = TypeAdapter(AuthorizationClaims)
_CLAIM_FIELDS = frozenset(field.name for field in dataclasses.fields(AuthorizationClaims))
_SIGNING_FIELDS = frozenset(field.name for field in dataclasses.fields(SigningAuthority))


class Refused(PermissionError):
    def __init__(self) -> None:
        super().__init__("source registration authority unavailable")


@dataclass(frozen=True)
class Scope:
    org: str
    namespace: str
    source_id: str
    generation: str
    resource_hash: str

    def check(self) -> None:
        if any(not isinstance(v, str) or not v or "\0" in v for v in dataclasses.astuple(self)):
            raise Refused()
        if len(self.resource_hash) != 64 or any(
            c not in "0123456789abcdef" for c in self.resource_hash
        ):
            raise Refused()


@dataclass(frozen=True, repr=False)
class AdminPermit:
    """A current presenter, distinct from the source registrant's retained approval."""

    _auth: AuthInfo
    _approval: Approval

    async def validate(self, org: str) -> None:
        if org != self._approval.scope.org or current_auth() is not self._auth:
            raise Refused()
        await self._approval.validate()
        if current_auth() is not self._auth:
            raise Refused()


async def current_permit(scope: Scope) -> AdminPermit:
    scope.check()
    auth = current_auth()
    if (
        not isinstance(auth, AuthInfo)
        or not auth.has_role(Role.ORG_ADMIN)
        or not authz.may_act_in_org(auth, scope.org)
    ):
        raise Refused()
    registrant = await aliases.canonical_subject(scope.org, auth.subject)
    metadata = _CLAIMS.validate_json(json.dumps(dataclasses.asdict(auth)), strict=True)
    permit = AdminPermit(auth, Approval(scope, registrant, metadata))
    await permit.validate(scope.org)
    return permit


async def capture(auth: AuthInfo, scope: Scope) -> tuple[str, str]:
    """Seal the current caller's exact authorization for this one source generation."""
    scope.check()
    if not isinstance(auth, AuthInfo) or current_auth() is not auth:
        raise Refused()
    if auth.signing_authority is None or not auth.signing_authority.current():
        raise Refused()
    await authz.check_authorized(auth, Role.ADMIN, scope.org, scope.namespace)
    if not auth.has_role(Role.ORG_ADMIN) or current_auth() is not auth:
        raise Refused()
    registrant = await aliases.canonical_subject(scope.org, auth.subject)
    frame = {
        "format": 1,
        "scope": dataclasses.asdict(scope),
        "registrant": registrant,
        "claims": dataclasses.asdict(auth),
    }
    encoded = seal(PURPOSE, json.dumps(frame, sort_keys=True, separators=(",", ":")))
    approval = load(encoded, scope, registrant=registrant)
    await approval.validate()
    if current_auth() is not auth:
        raise Refused()
    return registrant, encoded


def load(encoded: str, scope: Scope, *, registrant: str) -> Approval:
    scope.check()
    try:
        value = json.loads(unseal(PURPOSE, encoded, warn=False))
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "scope", "registrant", "claims"}
            or type(value["format"]) is not int
            or value["format"] != 1
            or value["scope"] != dataclasses.asdict(scope)
            or not registrant
            or value["registrant"] != registrant
            or not isinstance(value["claims"], dict)
            or set(value["claims"]) != _CLAIM_FIELDS
        ):
            raise Refused()
        signing = value["claims"].get("signing_authority")
        if signing is not None and (
            not isinstance(signing, dict) or set(signing) != _SIGNING_FIELDS
        ):
            raise Refused()
        claims = _CLAIMS.validate_json(json.dumps(value["claims"]), strict=True)
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise Refused() from None
    if len(claims.parent_subjects) != len(claims.parent_iats):
        raise Refused()
    return Approval(scope, registrant, claims)


def names_person(
    encoded: str, *, org: str, namespace: str, source_id: str, spellings: tuple[str, ...]
) -> bool:
    """Inspect sealed historical PII, without treating expired metadata as live authority."""
    try:
        value = json.loads(unseal(PURPOSE, encoded, warn=False))
        if not isinstance(value, dict) or not isinstance(value.get("scope"), dict):
            raise Refused()
        scope = Scope(**value["scope"])
        if (scope.org, scope.namespace, scope.source_id) != (org, namespace, source_id):
            raise Refused()
        registrant = value["registrant"]
        if not isinstance(registrant, str) or not registrant:
            raise Refused()
        approval = load(encoded, scope, registrant=registrant)
        subjects = {
            approval.registrant,
            approval.claims.subject,
            approval.claims.creator,
            approval.claims.impersonated_by,
            *approval.claims.parent_subjects,
        }
        return bool(subjects.intersection(spellings))
    except (ValueError, TypeError, KeyError, RecursionError):
        raise Refused() from None


@dataclass(frozen=True, repr=False)
class Approval:
    scope: Scope
    registrant: str
    claims: AuthorizationClaims

    def __repr__(self) -> str:
        return "Approval(<redacted>)"

    async def _revocations(self) -> None:
        claims = self.claims
        _check_not_revoked(
            {
                "sub": claims.subject,
                "iat": claims.issued_at,
                "jti": claims.token_id,
                "sid": claims.session_id,
                "parents": list(claims.parents),
                "creator": claims.creator,
                "parent_subjects": list(claims.parent_subjects),
                "parent_iats": list(claims.parent_iats),
            }
        )
        # The worker checks current storage, not only the request verifier's bounded snapshot.
        ids = [v for v in (claims.token_id, claims.session_id, *claims.parents) if v]
        oldest = (
            min([claims.issued_at, *claims.parent_iats]) if claims.issued_at is not None else None
        )
        subjects = [
            (claims.subject, claims.issued_at),
            *zip(claims.parent_subjects, claims.parent_iats, strict=True),
        ]
        if claims.creator:
            subjects.append((claims.creator, oldest))
        if claims.impersonated_by:
            subjects.append((claims.impersonated_by, claims.issued_at))
        cutoffs = await revocation.cutoffs([*ids, *(s for s, _ in subjects)])
        if any(key in cutoffs for key in ids) or any(
            subject in cutoffs and (issued is None or revocation.kills(cutoffs[subject], issued))
            for subject, issued in subjects
        ):
            raise Refused()

    async def validate(self) -> None:
        scope, claims = self.scope, self.claims
        scope.check()
        if (
            not settings.auth_actually_required()
            or cell.gate_reason()
            or claims.signing_authority is None
            or not claims.signing_authority.current()
            or claims.issued_at is None
            or claims.issued_at > _now() + settings.jwt_leeway_seconds
            or not claims.has_role(Role.ORG_ADMIN)
            or not authz.may_act_in_org(claims, scope.org)
            or claims.expires_at is not None
            and _now() > claims.expires_at + settings.jwt_leeway_seconds
            or not await tenancy.is_held(scope.org)
        ):
            raise Refused()
        await self._revocations()
        identity = await resolve_reader(scope.org, claims.subject)
        if identity is None or identity.subject != self.registrant:
            raise Refused()
        for address in identity.emails:
            if await scim.deactivated(address):
                raise Refused()
        # Same metadata and role law as a real request, with fresh target grants and lineage.
        spellings = await aliases.verified_spellings(scope.org, claims.subject)
        grant_cache.invalidate(scope.org, [*spellings, claims.creator, *claims.parent_subjects])
        grant_cache.invalidate(scope.org, kind=grant_cache.GROUPS)
        grant_cache.invalidate(scope.org, kind=grant_cache.COLLECTIONS)
        with own_write_reads():
            await authz.check_acted_for(claims, scope.org)
            await authz._check_authorized_claims(claims, Role.ADMIN, scope.org, scope.namespace)
