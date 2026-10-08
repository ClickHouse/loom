"""What a read-replica (`LOOM_READ_REPLICA=true`) does not serve.

A read-replica shares the main Loom's ClickHouse, its signing keys and so its
tokens, and serves every op the main Loom serves, writes included: what it
writes lands in the shared service and the main Loom's workers process it.
The exception is the work tied to the main Loom's host: each op below runs an
ingest pass against the host's git mirror, clones or removes one, or changes
which repositories and tracker sources the deployment indexes. A replica has
no mirrors and runs no workers, so those ops answer with where to send them.

The ops that only read full git history (blame, a citation's commit checks)
are forwarded instead when `LOOM_READ_REPLICA_MAIN_URL` names the main Loom:
the replica sends the op's own input there with the caller's headers, and the
main Loom authorizes and answers it as if the caller had called it directly.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from server.settings import settings

HOST_WORK_OPS = frozenset(
    {
        "code.refresh",
        "code.reindex",
        "code.scip_ingest",
        "code.calibrate",
        "code.repo.add",
        "code.repo.add_branch",
        "code.repo.remove",
        "code.repo.set_access",
        "code.repo.set_source",
        "code.repo.remote_branches",
    }
)


# The subject of the service token a read-replica mints for its own calls to
# the main Loom (`loom-replica:<process id>`); `embedding.embed` serves only it.
REPLICA_SUBJECT_PREFIX = "loom-replica:"

# Ops that read the main Loom's git mirror and nothing a replica cannot read
# itself: forwarded to the main Loom when this replica knows its URL.
FORWARDED_OPS = frozenset({"code.blame_range", "code.verify_citations"})

# The request headers a forwarded op carries: the caller's credential, a
# trusted caller's impersonation, and the checkout's code scope that resolves
# a namespace-less code.* call.
FORWARD_HEADERS = ("authorization", "x-loom-on-behalf-of", "x-loom-code-scope")

_caller_headers: ContextVar[dict[str, str] | None] = ContextVar(
    "loom_replica_caller_headers", default=None
)


# The `reason` a refusal carries on both transports, beside the role refusal's
# `code`: the caller holds the access, this Loom just does not run the op.
REASON = "READ_REPLICA"


class ReplicaRefusal(PermissionError):
    """An op this read-replica does not run. A PermissionError so the gates
    refuse it like a role failure (403), and its own type so the transports
    say "send it to the main Loom", never "request access"."""


def op_refusal(op_name: str) -> str | None:
    """Why this process does not serve `op_name`, or None when it does (always
    None on the main Loom). The sentence is the refusal the caller reads."""
    if not settings.read_replica or op_name not in HOST_WORK_OPS:
        return None
    return (
        f"{op_name} runs on the main Loom only: it works on the git mirrors or "
        "changes what the deployment indexes, and this Loom is a read-replica"
    )


def refuse_host_work(op_name: str) -> None:
    """Raise ReplicaRefusal when this read-replica does not run `op_name`."""
    refusal = op_refusal(op_name)
    if refusal is not None:
        raise ReplicaRefusal(refusal)


def remember_caller_headers(headers: Any) -> None:
    """Keep this request's forwardable headers for `forward`. A no-op on the
    main Loom, which never forwards."""
    if not settings.read_replica:
        return
    kept = {h: str(headers.get(h)) for h in FORWARD_HEADERS if headers.get(h)}
    _caller_headers.set(kept)


def forwards(op_name: str) -> bool:
    """Whether this process sends `op_name` to the main Loom."""
    return (
        bool(settings.read_replica)
        and bool(str(settings.read_replica_main_url).strip())
        and op_name in FORWARDED_OPS
    )


def caller_headers() -> dict[str, str]:
    """The headers `remember_caller_headers` kept for this request."""
    return dict(_caller_headers.get() or {})


__all__ = [
    "FORWARDED_OPS",
    "FORWARD_HEADERS",
    "HOST_WORK_OPS",
    "REASON",
    "REPLICA_SUBJECT_PREFIX",
    "ReplicaRefusal",
    "caller_headers",
    "forwards",
    "op_refusal",
    "refuse_host_work",
    "remember_caller_headers",
]
