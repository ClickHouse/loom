"""Whom a model call is for, as headers an inference gateway reads.

`LOOM_INFERENCE_IDENTITY_HEADERS_JSON` maps header names to templates; every
chat, embedding, rerank and classify request carries them, rendered from the
call's attribution (`server.llm.customer.attribution`):

- a request's: the org, namespace and user_id the transport recorded, and the
  subject of its token;
- a job's or a CLI's: what `customer.for_org` names around the call, from the
  row it works on, with the row's writer as the subject when the row records one.

From the subject come `{agent}`, the agent of a workload trust's agent token
(`sa:<trust>:<agent>`, server/auth/agents.py); `{tenant}`, the value that
trust requires of its `LOOM_INFERENCE_TENANT_CLAIM` claim, when it requires
one exactly; and `{email}`, a person's address. A call that knows no subject
(a job's row that records no writer) takes the agent whose own namespace the
row is in, if any. `{user_id}` is the call's user_id, else its subject, as the
traces name the end user. A header whose template renders empty is not sent,
and a value is percent-encoded only where a header could not carry it.

A trust's claims and a namespace's owner are read at most once a minute; a read
that fails serves the copy in hand, and with none the call waits
(`IdentityUnread`), never sent under another identity.
"""

from __future__ import annotations

import logging
import string
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from server.core.errors import BackendUnavailableError
from server.settings import settings
from server.settings.catalog import IDENTITY_PLACEHOLDER

log = logging.getLogger(__name__)

# How long a trust's claims and a namespace's owner are kept before they are read again.
_KEEP_S = 60.0  # not a setting: both change only when an operator re-registers them
_KEPT_MAX = 10_000  # not a setting: bounds memory; a dropped entry costs one read
_kept: dict[tuple[str, str, str], tuple[float, str]] = {}

# What a header value carries as it is: printable ASCII but the space and '%'.
_AS_IS = "".join(c for c in string.punctuation if c != "%")

# The identity tuple's "none", and an org-wide token's wildcard: not a value to name.
_NONE = ("-", "*")
# The placeholders that read the call's subject.
_BY_SUBJECT = frozenset({"agent", "tenant", "subject", "email", "user_id"})
# The placeholders that name the agent: their headers are sent together or not at all.
_AGENT = frozenset({"agent", "tenant"})


class IdentityUnread(BackendUnavailableError):
    """What names this call's identity (a trust's claims, a namespace's owner)
    could not be read and no copy is in hand: the call waits rather than go
    out under another identity."""


@dataclass(frozen=True)
class Attribution:
    """Whom a model call is for: its org, and what else the caller knows."""

    org: str = ""
    namespace: str = ""
    # The token's subject, or the writer of the row a job works on.
    subject: str = ""
    user_id: str = ""


def _used() -> frozenset[str]:
    return frozenset(
        m.group(1)
        for _, template in settings.identity_headers()
        for m in IDENTITY_PLACEHOLDER.finditer(template)
    )


def split_by(a: Attribution) -> Attribution:
    """`a` cut to what the identity headers read (the org always): a job batching
    rows into one call groups them by it, since one call names one identity."""
    used = _used()
    by_subject = bool(used & _BY_SUBJECT)
    return Attribution(
        org=a.org,
        # A call with no subject takes its namespace's agent.
        namespace=a.namespace if by_subject or "namespace" in used else "",
        subject=a.subject if by_subject else "",
        user_id=a.user_id if "user_id" in used else "",
    )


def splits() -> bool:
    """Whether any identity header is configured, so a job's batch splits by identity."""
    return bool(settings.identity_headers())


def reads_writer() -> bool:
    """Whether the identity headers name more than a call's org and namespace (its
    subject), so a job finds each row's writer before calling for it."""
    return bool(_used() & _BY_SUBJECT)


async def render(a: Attribution) -> tuple[tuple[str, str], ...]:
    """The identity headers of a call made for `a`, in their configured order. The
    headers that name the agent (`{agent}`, `{tenant}`) go together or not at all:
    when any placeholder of theirs renders empty, none of them is sent, since a
    gateway takes a tenant and an agent as one identity. Any other header whose
    template renders empty is left out."""
    templates = settings.identity_headers()
    if not templates:
        return ()
    values = await _values(a, _used())
    agent_headers = [t for _, t in templates if _names(t) & _AGENT]
    whole = all(values[p] for t in agent_headers for p in _names(t))
    out = []
    for name, template in templates:
        if _names(template) & _AGENT and not whole:
            continue
        value = IDENTITY_PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
        if value:
            out.append((name, value))
    return tuple(out)


def _names(template: str) -> frozenset[str]:
    return frozenset(m.group(1) for m in IDENTITY_PLACEHOLDER.finditer(template))


async def _values(a: Attribution, used: frozenset[str]) -> dict[str, str]:
    """Each placeholder's value for `a`, percent-encoded where a header needs it;
    only those `used` are read."""
    org, namespace, subject = _named(a.org), _named(a.namespace), _named(a.subject)
    user_id = _named(a.user_id)
    wanted = used & _BY_SUBJECT if not user_id else used & (_BY_SUBJECT - {"user_id"})
    if wanted and not subject and org and namespace:
        subject = await _namespace_agent(org, namespace)
    from server.auth import agents

    found = agents.parse(subject)
    trust = subject.removeprefix("sa:").partition(":")[0] if subject.startswith("sa:") else ""
    raw = {
        "org": org,
        "namespace": namespace,
        "subject": subject,
        "agent": found[1] if found else "",
        "user_id": user_id or subject,
        "tenant": await _tenant(trust) if "tenant" in used and trust else "",
        "email": await _email(org, subject) if "email" in used else "",
    }
    return {k: _quote(v) for k, v in raw.items()}


def _named(value: str) -> str:
    value = (value or "").strip()
    return "" if value in _NONE else value


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe=_AS_IS)


async def _tenant(trust: str) -> str:
    """The value `trust` requires of its tenant claim, when it requires one exactly."""
    claim = settings.inference_tenant_claim
    want = await _kept_read(("tenant", trust, claim), _claim(trust), f"trust {trust!r}")
    return "" if want.endswith("*") else want  # a prefix condition names no one tenant


def _claim(trust: str) -> Callable[[], Awaitable[str]]:
    async def read() -> str:
        from server.auth import machines

        m = await machines.get(trust, active_only=False)
        return m["claims"].get(settings.inference_tenant_claim, "") if m is not None else ""

    return read


async def _namespace_agent(org: str, namespace: str) -> str:
    """The subject of the agent whose own namespace this is, '' for any other."""

    async def read() -> str:
        from server.auth import agents
        from server.auth.personal_namespace import namespace_owner
        from server.core.ch import get_client

        owner = await namespace_owner(get_client(), org, namespace) or ""
        return owner if agents.parse(owner) else ""

    return await _kept_read(("owner", org, namespace), read, f"namespace {org}/{namespace}")


async def _email(org: str, subject: str) -> str:
    """A person's address: the subject itself, or the spelling the directory ties
    to it (`server.auth.aliases`); '' for a service account."""
    if not subject or subject.startswith("sa:"):
        return ""
    if "@" in subject:
        return subject
    from server.auth.aliases import subject_aliases

    return next((s for s in await subject_aliases(org, subject) if "@" in s), "")


async def _kept_read(
    key: tuple[str, str, str], read: Callable[[], Awaitable[str]], what: str
) -> str:
    now = time.monotonic()
    hit = _kept.get(key)
    if hit is not None and now - hit[0] < _KEEP_S:
        return hit[1]
    try:
        value = await read()
    except Exception as e:  # any failed read: the copy in hand serves; with none the call waits
        if hit is None:
            raise IdentityUnread(f"the identity headers could not read {what}") from e
        log.warning("identity headers: re-reading %s failed, its copy serves: %s", what, e)
        value = hit[1]
    if len(_kept) >= _KEPT_MAX:
        _kept.clear()
    _kept[key] = (now, value)
    return value


def _reset_for_tests() -> None:
    _kept.clear()
