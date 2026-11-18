"""The person a verified credential may read mirrored document lists as."""

from dataclasses import dataclass
from typing import Any

from server.auth.jwt import AuthInfo
from server.settings import settings


def reader_subject(auth: AuthInfo | None) -> str:
    """A primary session's subject or an explicit personal token's creator, else nobody.

    The caller resolves this spelling through the directory; token ownership and an
    email-shaped subject alone prove no verified email or hosted domain.
    """
    if (
        auth is None
        or not settings.auth_actually_required()
        or auth.impersonated_by
        or auth.agent_principal
        or auth.read_user_id
    ):
        return ""
    if auth.reads_as_creator:
        return auth.creator
    if auth.session_id and not auth.creator and not auth.is_service_account:
        return auth.subject
    return ""


@dataclass(frozen=True)
class ReaderIdentity:
    subject: str
    emails: frozenset[str]
    domains: frozenset[str]


async def resolve_reader(org: str, subject: str, *, ch: Any = None) -> ReaderIdentity | None:
    """Resolve actual verified contact rows uncached; an alias alone proves nothing."""
    from server.auth.aliases import canonical_subject
    from server.auth.sso_links import hosted_domain
    from server.core.ch import get_client, own_write_reads
    from server.core.grant_sql import VERIFIED_EMAIL_SQL

    if not org or not subject:
        return None
    ch = ch or get_client()
    with own_write_reads():
        canonical = await canonical_subject(org, subject, ch=ch)
        rows = await ch.query_all(
            f"""
            SELECT n.namespace, n.owner_email, n.contact_email, n.password_hash,
                   s.hosted_domain, s.issuer
            FROM (
                SELECT org, namespace, owner_email, contact_email, password_hash
                FROM loom.namespaces FINAL
                WHERE org = {{org:String}} AND active = 1 AND contact_email != ''
                  AND {VERIFIED_EMAIL_SQL}
                  AND (lower(owner_email) = lower({{owner:String}})
                       OR lower(contact_email) = lower({{sub:String}}))
                LIMIT 129
            ) AS n
            LEFT JOIN (
                SELECT org, namespace, email, hosted_domain, issuer
                FROM loom.sso_links FINAL WHERE org = {{org:String}} AND linked = 1
            ) AS s ON n.org = s.org AND n.namespace = s.namespace AND n.contact_email = s.email
            LIMIT 129
            """,
            params={"org": org, "sub": subject, "owner": canonical},
        )
    if len(rows) > 128:
        return None
    if not rows:
        return ReaderIdentity(canonical, frozenset(), frozenset())
    owners = {r["owner_email"].lower() for r in rows if r["owner_email"]}
    if owners != {canonical.lower()} or any(not r["owner_email"] for r in rows):
        return None
    emails = frozenset(r["contact_email"].lower() for r in rows)
    issuer = settings.oidc_issuer.rstrip("/") if settings.oauth_login_provider == "oidc" else ""
    domains = frozenset(
        domain
        for r in rows
        if not r["password_hash"] and issuer and str(r.get("issuer") or "").rstrip("/") == issuer
        if (domain := hosted_domain(r.get("hosted_domain")))
    )
    return ReaderIdentity(canonical, emails, domains)
