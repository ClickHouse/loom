"""Namespace-list erasures another domain starts.

A domain that retires namespaces (a removed repository index, throwaway bench
namespaces) calls these instead of building an `erasure.request` input
itself. Both run the same `namespace_list` saga the op runs, with its
checkpoints, fail-closed backend steps and residue census; they differ only in
whether the caller waits for the saga to finish.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from server.erasure.api.erasure import ErasureRequestInput, ErasureRequestOutput


def _namespace_list_request(
    org: str,
    namespaces: Sequence[str],
    *,
    requester: str,
    purge: bool,
    external: bool,
) -> ErasureRequestInput:
    return ErasureRequestInput(
        org=org,
        scope_type="namespace_list",
        scope_value=json.dumps(
            {"namespaces": list(namespaces), "purge": purge, "external": external}
        ),
        requester=requester,
        purge=purge,
        external=external,
    )


async def erase_namespaces(
    org: str,
    namespaces: Sequence[str],
    *,
    requester: str,
    purge: bool,
    external: bool,
) -> ErasureRequestOutput:
    """Run a namespace-list erasure to its end and return the terminal request
    (status, erasure_id, residue), exactly as `erasure.request` does."""
    from server.erasure.app import handlers

    return await handlers.handle_erasure_request(
        _namespace_list_request(
            org, namespaces, requester=requester, purge=purge, external=external
        )
    )


async def erase_namespaces_detached(
    org: str,
    namespaces: Sequence[str],
    *,
    requester: str,
    purge: bool,
    external: bool,
) -> str:
    """Start a namespace-list erasure in the background and return its
    erasure_id; erasure_recovery resumes it if the process dies."""
    from server.erasure.app import saga

    return await saga.run_erasure_detached(
        _namespace_list_request(
            org, namespaces, requester=requester, purge=purge, external=external
        )
    )
