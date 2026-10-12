"""The writing principal a memory row is stamped with: the request's token,
else the enclosing pipeline stage's actor. It reads the request's auth
context, so it lives in app; the source-class rules and the pipeline actor
are server.memory.domain.provenance."""

from __future__ import annotations

from server.memory.domain import trust
from server.memory.domain.provenance import pipeline_actor_var


def writer_actor(fallback: str = "") -> str:
    """The writing principal as `kind:subject`, kind from the token; then the
    pipeline actor of the enclosing stage (`pipeline_actor`); then `fallback`."""
    # Lazy: server.auth.context pulls in authz → operations → handlers, and
    # the handlers import this module at load time.
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is None:
        return pipeline_actor_var.get() or fallback or ""
    subject = str(getattr(auth, "subject", "") or "")
    if not subject:
        return pipeline_actor_var.get() or fallback or ""
    return trust.actor_for(subject, is_service_account=bool(auth.is_service_account))
