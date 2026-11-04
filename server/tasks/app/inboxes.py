"""``inbox.*``: registering an inbox, listing inboxes and the principals
that may post, and the owner's policy rows."""

from __future__ import annotations

from typing import Any

from server.core.datetimes import utc_now as _now
from server.tasks.api.models_task import (
    InboxListInput,
    InboxListOutput,
    InboxPolicyListInput,
    InboxPolicyListOutput,
    InboxPolicySetInput,
    InboxPolicySetOutput,
    InboxPrincipalsInput,
    InboxPrincipalsOutput,
    InboxRegisterInput,
    InboxRegisterOutput,
    Principal,
)
from server.tasks.app.common import (
    _actor,
    _caller_subject,
    _conflict,
    _in_scope,
    _inbox_payload,
    _load_readable_inbox,
    _loads,
    _naive,
    _policy_payload,
    _require_admin_side,
)
from server.tasks.app.trust import (
    _poster_may_post,
)
from server.tasks.store import repo as repo
from server.tasks.store.rows import InboxRow, PolicyRow


async def fetch_inbox(org: str, inbox: str) -> dict[str, Any] | None:
    """The live inbox row as a plain dict, or None when it is not registered —
    the read another domain makes before it posts to an inbox."""
    return await repo.fetch_inbox(org, inbox)


def next_version() -> int:
    """The tasks plane's version stamp: microseconds since the epoch, strictly
    increasing inside one process. A domain writing rows that sit beside the
    tasks plane's (a coverage watch and the task it posts) stamps them here so
    the two order the same way."""
    return repo.next_version()


async def server_version() -> int:
    """A task event's ``seq`` from the server's clock, as every task event is
    stamped, so the task cursors order a domain's event across replicas."""
    return await repo.server_version()


async def handle_inbox_register(input: InboxRegisterInput) -> InboxRegisterOutput:
    """Admin side: ``namespace`` is the owner namespace. Re-registering an
    existing inbox writes a new version (a settings edit); ``if_match_version``
    guards a racing edit."""

    existing = await repo.fetch_inbox_row(input.org, input.inbox)
    if existing is not None and existing["owner_namespace"] != input.namespace:
        raise _conflict(
            "INBOX_OWNED_ELSEWHERE",
            f"inbox {input.inbox!r} is owned by namespace {existing['owner_namespace']!r}",
        )
    if (
        existing is not None
        and input.if_match_version is not None
        and int(existing["version"]) != input.if_match_version
    ):
        raise _conflict(
            "PRECONDITION_FAILED",
            "inbox changed since it was read",
            current_version=int(existing["version"]),
        )
    now = _naive(_now())
    row: InboxRow = {
        "org": input.org,
        "inbox": input.inbox,
        "owner_agent": input.owner_agent,
        "owner_namespace": input.namespace,
        "description": input.description,
        "posters": sorted(set(input.posters)),
        "accept_disclosure": sorted(set(input.accept_disclosure)),
        "default_lease_seconds": input.default_lease_seconds,
        "max_attempts": input.max_attempts,
        "max_defers": input.max_defers,
        "triage_escalate_after_seconds": input.triage_escalate_after_seconds,
        "webhook_url": input.webhook_url,
        "webhook_secret_ref": input.webhook_secret_ref,
        "created_by": (existing["created_by"] if existing else "")
        or _actor(f"process:{input.agent}"),
        "created_at": (existing["created_at"] if existing else None) or now,
        "updated_at": now,
        "version": repo.next_version(),
        "is_deleted": 0,
    }
    await repo.insert_inbox_version(row)
    return InboxRegisterOutput(inbox=_inbox_payload(row), created=existing is None)


async def handle_inbox_list(input: InboxListInput) -> InboxListOutput:
    if input.mine_everywhere:
        rows = await repo.list_inboxes(input.org, limit=input.limit)
        subject = _caller_subject()
        # The inboxes the caller may post to; a filter never marks the answer unverified.
        keep = []
        for r in rows:
            if r["owner_namespace"] == input.namespace or await _poster_may_post(
                r, subject, input.agent, hides_answer=False
            ):
                keep.append(r)
        rows = keep
    else:
        rows = await repo.list_inboxes(input.org, namespace=input.namespace, limit=input.limit)
    return InboxListOutput(inboxes=[_inbox_payload(r) for r in rows])


def _principal_kind(subject: str) -> str:
    if not subject or subject == "*":
        return "other"
    if subject.startswith("sa:"):
        return "service_account"
    return "human"


async def handle_inbox_principals(input: InboxPrincipalsInput) -> InboxPrincipalsOutput:
    """Admin side. The poster picker's source: every identity the org already
    knows — service accounts, grant holders (people under their login email,
    bots under ``sa:`` subjects) and subjects seen in the ledger — with what
    they hold and when they were last seen. Nobody is typed in by hand."""

    # An inbox being registered has no row yet: the picker then reads the
    # directory against the caller's own namespace (the transport already
    # authorized the caller as its admin) with nothing listed. An inbox whose owner
    # namespace the caller cannot read answers the same way, so the name confirms nothing.
    inbox = await repo.fetch_inbox_row(input.org, input.inbox)
    if inbox is not None and not await _in_scope(input.org, inbox["owner_namespace"], write=False):
        inbox = None
    if inbox is None:
        owner_ns = input.namespace
        listed: set[str] = set()
    else:
        owner_ns = inbox["owner_namespace"]
        listed = set(inbox.get("posters") or [])
    await _require_admin_side(input.org, owner_ns)

    book: dict[str, dict[str, Any]] = {}

    def entry(subject: str) -> dict[str, Any]:
        return book.setdefault(
            subject,
            {
                "subject": subject,
                "label": subject,
                "kind": _principal_kind(subject),
                "namespaces": set(),
                "roles": set(),
                "last_seen": None,
                "can_already_post": False,
            },
        )

    for sa in await repo.list_service_accounts(input.org):
        subject = str(sa.get("sa_subject") or "")
        if not subject:
            continue
        e = entry(subject)
        e["label"] = str(sa.get("name") or subject)
        e["kind"] = "service_account"
        ns_roles = _loads(sa.get("ns_roles"), {})
        for ns in list(sa.get("namespaces") or []) + list(ns_roles.keys()):
            e["namespaces"].add(str(ns))
        for r in list(sa.get("roles") or []) + list(ns_roles.values()):
            e["roles"].add(str(r))
        role_here = ns_roles.get(owner_ns) or (
            next(iter(sa.get("roles") or []), "")
            if owner_ns in (sa.get("namespaces") or [])
            else ""
        )
        if role_here in ("writer", "admin"):
            e["can_already_post"] = True
    for g in await repo.list_grant_subjects(input.org):
        subject = str(g["subject"])
        e = entry(subject)
        e["namespaces"].add(str(g["namespace"]))
        e["roles"].add(str(g["role"]))
        if g["namespace"] == owner_ns and g["role"] in ("writer", "admin"):
            e["can_already_post"] = True
    for subject, seen in (await repo.subjects_last_seen(input.org)).items():
        if subject and subject != "*":
            entry(subject)["last_seen"] = seen
    for subject in listed:  # a listed poster the directory has forgotten still shows
        entry(subject)

    q = input.query.strip().lower()
    rows = [
        e for e in book.values() if not q or q in e["subject"].lower() or q in e["label"].lower()
    ]
    order = {"human": 0, "service_account": 1, "other": 2}
    rows.sort(key=lambda e: (order[e["kind"]], e["label"].lower(), e["subject"]))
    truncated = len(rows) > input.limit
    out = [
        Principal(
            subject=e["subject"],
            label=e["label"],
            kind=e["kind"],
            namespaces=sorted(e["namespaces"]),
            roles=sorted(e["roles"]),
            last_seen=e["last_seen"],
            can_already_post=e["can_already_post"],
            listed=e["subject"] in listed,
        )
        for e in rows[: input.limit]
    ]
    return InboxPrincipalsOutput(principals=out, truncated=truncated)


async def handle_inbox_policy_set(input: InboxPolicySetInput) -> InboxPolicySetOutput:
    """Set the trust mode an inbox owner chose for a poster and task kind (Loom stores it, never
    changes it)."""

    inbox = await _load_readable_inbox(input.org, input.inbox)
    await _require_admin_side(input.org, inbox["owner_namespace"])
    # The table's automatic-trust columns (daily_cap, promote_after, require_human, streak)
    # stay at their zero values: loom stores the mode the owner set and never moves it itself.
    row: PolicyRow = {
        "org": input.org,
        "inbox": input.inbox,
        "poster": input.poster,
        "kind": input.kind,
        "mode": input.mode,
        "daily_cap": 0,
        "require_evidence": list(input.require_evidence),
        "promote_after": 0,
        "require_human": 0,
        "streak": 0,
        "set_by": _actor(f"process:{input.agent}"),
        "set_at": _naive(_now()),
        "version": repo.next_version(),
        "is_deleted": 1 if input.remove else 0,
    }
    await repo.insert_policy_version(row)
    if input.remove:
        return InboxPolicySetOutput(policy=None, removed=True)
    return InboxPolicySetOutput(policy=_policy_payload(row), removed=False)


async def handle_inbox_policy_list(input: InboxPolicyListInput) -> InboxPolicyListOutput:
    """List an inbox's trust policies."""

    await _load_readable_inbox(input.org, input.inbox)
    rows = await repo.fetch_policies(input.org, input.inbox)
    return InboxPolicyListOutput(policies=[_policy_payload(r) for r in rows])
