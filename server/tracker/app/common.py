"""What every tracker.* handler shares: the source seam, the tracker org
default, the code-namespace binding, and the linked trackers a search reads.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _with_default_org(input: Any) -> Any:
    """A QA input that names no org reads the deployment's QA org — the same default the
    transport authorized against (`plane_org_for`), so the handler never reads a different
    namespace than the one the gate admitted."""
    if str(getattr(input, "org", "") or ""):
        return input
    from server.code.app.common import plane_org_for

    org = plane_org_for("tracker.")
    return input.model_copy(update={"org": org}) if org else input


async def _bound_input(input: Any) -> Any:
    """A tracker op addressed to a code namespace answers the QA namespace bound to it —
    through the binding gate, so a reader of the repository reads the public QA facts with
    the token it holds, and a restricted binding keeps its own gate. A tracker namespace
    answers itself (the transport authorized it); anything else stays as addressed and
    answers `tracker_unavailable`, naming nothing. An input naming no org gets the tracker
    org first."""
    from server.tracker.app.binding import authorized_tracker_namespace
    from server.tracker.domain.namespaces import tracker_namespace

    input = _with_default_org(input)
    org, ns = str(getattr(input, "org", "") or ""), str(getattr(input, "namespace", "") or "")
    if not ns or tracker_namespace(org, ns) is not None:
        return input
    bound = await authorized_tracker_namespace(org, ns)
    if not bound or bound == ns:
        return input
    return input.model_copy(update={"namespace": bound})


async def linked_trackers(org: str, namespace: str) -> list[str]:
    """The trackers a search addressed to `namespace` reads — every tracker linked to a code
    namespace that the caller may read, primary first; a tracker namespace is itself;
    anything else is []."""
    from server.tracker.app.binding import authorized_tracker_namespaces
    from server.tracker.domain.namespaces import tracker_namespace

    if not namespace:
        return []
    if tracker_namespace(org, namespace) is not None:
        return [namespace]
    # The primary keeps marking through `_bound_input`; the rest Loom adds.
    return await authorized_tracker_namespaces(org, namespace, hides_answer=False)


def _unavailable(op: str, ns: str, e: Exception) -> None:
    logger.info("%s on %s: tracker_unavailable (%s)", op, ns, e)


def _s(v: Any) -> str:
    return "" if v is None else str(v)
