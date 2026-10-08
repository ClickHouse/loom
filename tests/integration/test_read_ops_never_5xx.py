"""Every read-shaped op answers below 500 to a well-formed body.

A synthesised body can reach defects that answer 500 — an import error, a
not-found surfaced as 500, a UUID the column could not parse, an aggregate
alias shadowing a WHERE predicate, a correlated subquery in ORDER BY. The
leakage suite walks only
its ten op groups and as a *foreign* tenant, where a 403 hides a handler
that would fail for the owner. This test walks every op a viewer may call,
as the owner of a namespace that holds nothing (the purged case), with the
same synthesiser, and fails on any 5xx. A 4xx or an empty result is the
contract; a 422 on a synthesised body is the synthesiser's limit, not a
defect, and is reported but not gated.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.integration.test_cross_principal_leakage import (  # noqa: E402
    _SKIP_OPS,
    _post,
    _Seed,
    _synth_payload,
)

# Role-10 ops that still write on the caller's behalf (the erasure bench
# names the same set): a probe must not leave rows behind in CI.
_ROLE10_WRITERS = {
    "memory.cross_promote",
    "conflicts.propose_revision",
    "code.outcome",
    "memory.demonstrate",
    "namespace.validate",
    "knowledge.synthesize",
}
# Ops pinned to the answer each must give here. A 409 on the code op
# is the deployment's kill switch (code intelligence off), not a defect.
_FIXED = {
    "namespace.list_ontology": {200},
    "namespace.summary": {200},
    "namespace.list_categories": {200},
    "conversation.threads": {200},
    "procedure.locate": {404},
    "code.scip_status": {422, 409},
}


def read_shaped_ops() -> list[Any]:
    from server.operations.base import Role
    from server.operations.registry import ALL_OPERATIONS

    skip = _SKIP_OPS | _ROLE10_WRITERS
    return [
        op
        for op in ALL_OPERATIONS
        if int(op.required_role) <= int(Role.VIEWER) and op.name not in skip
    ]


def _phantom_seed(org: str, namespace: str) -> _Seed:
    """A tenant that stored nothing: every reference the synthesiser builds
    points at a row that does not exist, which is what a purged namespace
    looks like to its owner."""
    tok = uuid.uuid4().hex[:8]
    s = _Seed(org, namespace, namespace)
    s.keys = [f"phantom_{tok}_0", f"phantom_{tok}_1"]
    s.memory_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    s.entity = f"phantom{tok}"
    s.value_text = f"phantom {tok} text"
    return s


def test_every_read_shaped_op_answers_below_500_on_a_purged_namespace(
    control_plane_url: str, mint_test_token
) -> None:
    org = "read-ops-4495"
    ns = f"purged-{uuid.uuid4().hex[:8]}"
    seed = _phantom_seed(org, ns)
    token = mint_test_token(org=org, namespaces=[ns], roles=["viewer"], subject="probe")
    five_xx: dict[str, tuple[int, str]] = {}
    statuses: dict[str, int] = {}
    unbuildable: list[str] = []
    for op in read_shaped_ops():
        payload = _synth_payload(op.input_model, seed=seed, namespace=ns)
        if payload is None:
            unbuildable.append(op.name)
            continue
        r = _post(control_plane_url, token, op.name, payload)
        statuses[op.name] = r.status_code
        if r.status_code >= 500:
            five_xx[op.name] = (r.status_code, r.text[:200])
    assert not five_xx, f"read ops answering 5xx to a well-formed body: {five_xx}"
    wrong = {
        name: statuses.get(name) for name, ok in _FIXED.items() if statuses.get(name) not in ok
    }
    assert not wrong, (
        f"the 4450 ops answer outside their contract: {wrong}; unbuildable={unbuildable}"
    )
    assert len(statuses) >= 100, (
        f"walker exercised too few ops: {len(statuses)}; unbuildable={unbuildable}"
    )
