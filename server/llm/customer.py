"""The customer a model call is made for, as headers a metering gateway reads.

With `LOOM_INFERENCE_CUSTOMER_HEADER` set, every chat, embedding, rerank and
classify request names its customer, the org whose data it carries, and
(with `LOOM_INFERENCE_FEATURE_HEADER`) the feature that made it. A request
names the org it addresses, as authorized for its token, through the identity
the transports record for every op (`observability.genai`); a background job
or a CLI names the org of the rows it works on with `for_org`, never
inheriting a worker's identity; a call for the deployment itself (its startup
probe, the recalibration worker's fixed pairs, a provider check) runs under
`system()` and names `LOOM_INFERENCE_SYSTEM_CUSTOMER`. A call that names none
is refused (`Unattributed`), never sent to be billed to no one: one call never
carries two customers, so a job batching several orgs' rows splits them first.

The identity headers (`LOOM_INFERENCE_IDENTITY_HEADERS_JSON`, server/llm/identity.py)
name more of whom the call is for, from the same attribution: a request's token
subject, namespace and user_id; what a job's `for_org` names from the row it works
on. A job batching rows splits them by identity too (`groups`).

An org an admin switched off (`org.set_model_calls`, loom.org_model_calls)
gets no model call at all, header or not, and no cached vector or answer
either: the switch is a kill switch. `require()`, at the top of every
sender, checks both before the gateway token, the caches or provider health.
Each process tries to read the switched-off orgs at most every
`LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS` and refuses every call for an org
(`SwitchUnread`, a BackendUnavailableError) once its copy is older than
`LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS`: the switch fails closed. A call that
names an org is checked even as the deployment's (`system()`); only a
deployment call that names none is never switched off.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import quote

from server.core.errors import BackendUnavailableError, LoomServerError, ModelCallRefused
from server.core.polled_copy import PolledCopy
from server.llm.identity import Attribution as Attribution  # what a job's batch groups by
from server.llm.identity import IdentityUnread, names_agent, split_by, splits
from server.llm.identity import render as render_identity
from server.observability import genai
from server.settings import settings

# Whom a background job is working for; a request's comes from its identity.
_attribution: contextvars.ContextVar[Attribution | None] = contextvars.ContextVar(
    "loom_model_call_attribution", default=None
)
_system: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "loom_model_call_system", default=False
)
# Inside a `loom` command (server/cli/app.py), which names its namespace or none.
_cli: contextvars.ContextVar[bool] = contextvars.ContextVar("loom_model_call_cli", default=False)

# Not an org: the identity tuple's "none", and an org-wide token's wildcard.
_NO_ORG = ("", "-", "*")

log = logging.getLogger(__name__)
# A missed call path is logged once a minute per feature, not on every call.
_LOG_EVERY_S = 60.0  # not a setting: how often a log line repeats, not what Loom does
_logged_at: dict[str, float] = {}


class Unattributed(LoomServerError):
    """A model call with no customer to bill: a caller that set none (a job
    that did not name the org of its rows). Refused, never retried."""

    code = "MODEL_CALL_UNATTRIBUTED"
    status_code = 500


class ModelCallsDisabled(LoomServerError):
    """The org's admin switched its model calls off: nothing is sent for it."""

    code = "MODEL_CALLS_DISABLED"
    status_code = 403


class SwitchUnread(BackendUnavailableError):
    """This process's copy of the switch is too old to trust: no call is made
    for an org until it is read, and a job leaves its rows as they are."""


# What holds a call: a refusal (require()'s, or the gateway's for the call's
# identity), or a switch or an identity require() cannot read. None is a
# provider fault, so a fail-open caller (a reranker, classify) lets these
# through, and a job leaves its rows as they are to try again later; a queued
# extraction request naming no customer still fails, since it never clears.
HOLDS: tuple[type[LoomServerError], ...] = (
    Unattributed,
    ModelCallsDisabled,
    SwitchUnread,
    IdentityUnread,
    ModelCallRefused,
)
# What holds one group of a job's batch and not the others: its org switched off since
# the job picked it, its identity refused or asked to wait by the gateway, or what names
# that identity unread. The group's rows wait, no attempt spent, and the other groups'
# calls, and the vectors they bought, go on (`server.workers.holds`). An unread switch
# holds every call alike: the pass stops.
GROUP_HOLDS: tuple[type[LoomServerError], ...] = (
    ModelCallsDisabled,
    ModelCallRefused,
    IdentityUnread,
)


@dataclass(frozen=True)
class Customer:
    """Whom one model call is for, as `require()` resolved it before anything is sent."""

    # The customer header's value (the org as the gateway names it, or the system
    # customer); "" sends none.
    name: str = ""
    # The identity headers, rendered.
    identity: tuple[tuple[str, str], ...] = ()


async def _read() -> frozenset[str]:
    from server.core.ch import get_client, own_write_reads

    # Every org switched off, one row each: never cut short, which would fail open.
    # Sequentially consistent: the read after `note_switch_change` must see the change
    # just written, and every process's read the newest change on any replica.
    with own_write_reads():
        rows = await get_client().query_all(
            "SELECT org FROM loom.org_model_calls FINAL WHERE enabled = 0"
        )
    return frozenset(str(r["org"]) for r in rows)


# The orgs switched off, as this process last read them.
_switch: PolledCopy[frozenset[str]] = PolledCopy("model-call switch", _read, log)


@contextlib.contextmanager
def for_org(
    org: str, *, namespace: str = "", subject: str = "", user_id: str = ""
) -> Iterator[None]:
    """Model calls made inside this block are for `org`: a job names the org of
    the row it works on, and its namespace, writer (`subject`) and user_id when
    it has them, which the identity headers carry."""
    with attributed(Attribution(org, namespace, subject, user_id)):
        yield


@contextlib.contextmanager
def attributed(a: Attribution) -> Iterator[None]:
    """Model calls made inside this block are for `a` (a group `groups` made)."""
    token = _attribution.set(a)
    try:
        yield
    finally:
        _attribution.reset(token)


@contextlib.contextmanager
def cli_command() -> Iterator[None]:
    """Model calls made inside this block are a `loom` command's: one that names its
    org but no namespace, where the identity headers name the agent, is refused
    (`require`) rather than going out as the gateway's default agent."""
    token = _cli.set(True)
    try:
        yield
    finally:
        _cli.reset(token)


@contextlib.contextmanager
def system() -> Iterator[None]:
    """Model calls made inside this block are the deployment's own."""
    token = _system.set(True)
    try:
        yield
    finally:
        _system.reset(token)


def current() -> str:
    """The org the current model call is for: `for_org`'s, else the
    request's (`genai.request_org`); "" when neither names one."""
    return attribution().org


def attribution() -> Attribution:
    """Whom the current model call is for: what `for_org` names, else the
    request's identity and its token's subject; empty when neither names an org."""
    named = _attribution.get()
    if named is not None and named.org not in _NO_ORG:
        return named
    org = genai.request_org()
    if not org:
        return Attribution()
    from server.auth.context import current_auth

    auth, ident = current_auth(), genai.request_identity()
    return Attribution(
        org=org,
        namespace=ident.get("namespace", ""),
        subject=(auth.subject or "") if auth is not None else "",
        user_id=ident.get("user_id", ""),
    )


def job_call() -> bool:
    """Whether the current model call is a job's or a CLI's (inside `for_org`),
    not a request's: only a job's calls wait out an identity the gateway holds
    (`server.llm.gateway.sending`)."""
    return _attribution.get() is not None


async def require() -> Customer:
    """Whom the current model call is for, checked before anything is sent,
    cached or recorded: no customer with no customer header configured, and the
    identity headers rendered (`identity.render`). Raises ModelCallsDisabled
    when the call's org is switched off (header or not), Unattributed, logged,
    when a header is configured and the call names no org, and IdentityUnread
    when what names its identity cannot be read."""
    who = attribution()
    org = who.org
    if org and await switched_off(org):
        raise ModelCallsDisabled(f"model calls are switched off for org {org!r}")
    if not org and not _system.get() and splits():
        # An identity header names whom a call is for: one that names no org would go
        # out as the gateway's default identity, billed to no one it names.
        _unattributed("names no org for its identity headers")
    if _cli.get() and not (who.namespace or who.subject) and names_agent():
        # A command run with --org and no --namespace knows no agent to bill: its call
        # would go out as the gateway's default agent.
        _unattributed("is a loom command naming no namespace: run it with --namespace")
    return Customer(name=_customer_named(org), identity=await render_identity(who))


def _customer_named(org: str) -> str:
    """The customer header's value for a call made for `org`; "" without the header."""
    if not settings.inference_customer_header:
        # Load refuses this pairing; a value set after load still never sends unmetered.
        if settings.inference_customer_required:
            raise Unattributed(
                "LOOM_INFERENCE_CUSTOMER_REQUIRED is set without LOOM_INFERENCE_CUSTOMER_HEADER"
            )
        return ""
    if org:
        # An org is named as the gateway knows it; the system customer as configured.
        return settings.inference_customer_format.replace("{org}", org)
    if _system.get():
        return settings.inference_system_customer
    _unattributed("names no customer")


def _unattributed(why: str) -> NoReturn:
    """Refuse a call that names no one, logged once a minute by what made it."""
    what = feature()
    # Logged here: a fail-open caller (a reranker, a degraded leg) may swallow
    # the error, and the missed path must still show by its code.
    now = time.monotonic()
    if now - _logged_at.get(what, -_LOG_EVERY_S) >= _LOG_EVERY_S:
        _logged_at[what] = now
        log.error("MODEL_CALL_UNATTRIBUTED: a model call (%s) %s", what, why)
    raise Unattributed(
        f"a model call ({what}) {why}: run it for an org "
        "(server.llm.customer.for_org) or as the deployment's (customer.system)"
    )


def names_one() -> bool:
    """Whether the current model call names a customer, as `require` checks,
    for a job that records a row naming none instead of calling. Logs nothing:
    the row is bad data, not a missed call path."""
    if not settings.inference_customer_header:
        if settings.inference_customer_required:
            return False
        return not splits() or bool(current() or _system.get())
    return bool(current() or _system.get())


# The error a job records on a row that names no customer: retrying cannot
# help, since a row's org never changes, so the row is given up on at once.
NO_CUSTOMER = "MODEL_CALL_UNATTRIBUTED: the row names no org to bill"


def headers(who: Customer) -> dict[str, str]:
    """The customer, feature and identity headers for one model request, as
    `require()` resolved them; {} when it resolved none."""
    out: dict[str, str] = {}
    if who.name:
        # Header values are latin-1 with no line breaks: quote whatever the org adds.
        out[settings.inference_customer_header] = quote(who.name, safe="-._~:@")
        if settings.inference_feature_header:
            out[settings.inference_feature_header] = quote(gateway_feature(), safe="-._~:@")
    out.update(who.identity)
    return out


def gateway_feature() -> str:
    """The feature as the gateway names it (`LOOM_INFERENCE_FEATURE_MAP_JSON`),
    else Loom's own name."""
    mine = feature()
    names = settings.feature_map()  # decoded once per value
    return names.get(mine) or names.get("*") or mine


def feature() -> str:
    """What made the call: the cascade stage when one is set (hyde,
    extract_typed, ...), else the op or worker (memory.search,
    worker.embedding)."""
    stage = genai.current_stage()
    if stage != "other":
        return stage
    return genai.current_operation() or "other"


async def groups(rows: Iterable[Attribution]) -> dict[Attribution, list[int]]:
    """The positions of `rows` grouped by whom a call for them is, in first-seen
    order: one model call per group (`attributed`), since a call names a single
    customer and identity. With no customer or identity header nothing reads
    whom a call is for, so every position is one group, unless an org among
    them is switched off: then each org is its own group, so the switch refuses
    that org's call and the others go on."""
    rows = list(rows)
    if not rows:
        return {}
    if (
        not settings.inference_customer_header
        and not splits()
        and {r.org for r in rows}.isdisjoint(await switched_off_orgs())
    ):
        return {Attribution(): list(range(len(rows)))}
    # One group per rendered identity: rows of two namespaces one agent owns share a
    # call. An identity that cannot be read is its own group, held where it is called.
    keys: dict[Attribution, tuple[str, object]] = {}
    out: dict[tuple[str, object], tuple[Attribution, list[int]]] = {}
    for i, r in enumerate(rows):
        a = split_by(r)
        if a not in keys:
            try:
                keys[a] = (a.org, await render_identity(a))
            except IdentityUnread:
                keys[a] = (a.org, a)
        out.setdefault(keys[a], (a, []))[1].append(i)
    return dict(out.values())


async def switched_off(org: str) -> bool:
    """Whether `org`'s model calls are switched off. Raises SwitchUnread
    when this process's copy of the switch is too old to trust."""
    return org in await switched_off_orgs()


async def switched_off_orgs() -> frozenset[str]:
    """The orgs whose model calls are switched off, for a job to pass over;
    raises SwitchUnread when this process's copy is too old."""
    off = await _switch.get(
        refresh_s=settings.model_calls_policy_refresh_seconds,
        max_stale_s=settings.model_calls_policy_max_stale_seconds,
    )
    if off is None:
        raise SwitchUnread("the model-call switch could not be read; model calls wait until it is")
    return off


def note_switch_change() -> None:
    """Make the next call read the switch again, for a change just made here."""
    _switch.note_change()
