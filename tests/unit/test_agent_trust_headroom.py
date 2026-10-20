"""How far each agent trust is towards its --max-agents: counted as the cap
counts (every namespace of the trust's org carrying its mark, released ones
too), for active agent trusts only, and exported per trust."""

from __future__ import annotations

import pytest

from server.auth import agents
from server.observability import metrics_gauges, otel_probes
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run
from tests.unit.test_code_state_gauges import _Meter


def _trust(
    name: str,
    *,
    org: str = "acme",
    agent_claim: str = "agent_id",
    active: int = 1,
    max_agents: int = 4,
) -> str:
    return (
        "INSERT INTO loom.machine_identities (name, org, ns_roles, public_keys, issuer, "
        "audience, claims, active, updated_by, updated_at, version, agent_claim, max_agents) "
        f"VALUES ('{name}', '{org}', [], '', 'https://issuer.example', 'loom', "
        f"{{'tenant_id': 't1'}}, {active}, 'op', now64(3), 1, '{agent_claim}', {max_agents})"
    )


def _namespace(ns: str, tags: list[str], *, org: str = "acme", active: str = "true") -> str:
    return (
        "INSERT INTO loom.namespaces (org, namespace, owner_email, tags, active) "
        f"VALUES ('{org}', '{ns}', 'sa:x', {tags!r}, {active})"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_the_census_counts_what_the_cap_counts() -> None:
    rows = run(
        [
            ddl("machine_identities"),
            ddl("namespaces"),
            _trust("fleet", max_agents=4),
            _trust("idle", max_agents=10),
            _trust("bot", agent_claim=""),  # one bot, no agents
            _trust("gone", active=0),
            _namespace("fleet-a1", ["machine:fleet"]),
            _namespace("fleet-a2", ["machine:fleet", "x"]),
            _namespace("fleet-a3", ["machine:fleet"], active="false"),  # released: still counted
            _namespace("fleet-a9", ["machine:fleet"], org="other"),  # another org's
            _namespace("fleet-x", ["machine:fleetwood"]),
            _namespace("team", ["x"]),
        ],
        otel_probes._AGENT_TRUSTS_SQL + " ORDER BY trust",
    )
    assert rows == [
        {"org": "acme", "trust": "fleet", "max_agents": 4, "made": 3},
        {"org": "acme", "trust": "idle", "max_agents": 10, "made": 0},
    ]


def test_the_census_reads_the_mark_agents_gives_a_namespace() -> None:
    assert agents.tag("fleet") == "machine:fleet"
    assert f"'{agents.TAG_PREFIX}'" in otel_probes._AGENT_TRUSTS_SQL


def test_each_trust_reports_its_agents_and_its_share_of_the_cap() -> None:
    series = otel_probes._agent_trust_series_from_rows(
        [("acme", "fleet", 4, 3), ("acme", "idle", 10, 0)]
    )
    fleet = {"loom.org": "acme", "loom.trust": "fleet"}
    idle = {"loom.org": "acme", "loom.trust": "idle"}
    assert series == {
        "agent_trust_agents": [(3.0, fleet), (0.0, idle)],
        "agent_trust_used_ratio": [(0.75, fleet), (0.0, idle)],
    }


def test_the_gauges_are_registered_only_with_a_probe() -> None:
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict)
    assert not any(n.startswith("loom.auth.trust_agents") for n in meter.gauges)
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict, agent_trusts_probe=dict)
    assert {"loom.auth.trust_agents", "loom.auth.trust_agents_used_ratio"} <= set(meter.gauges)
    assert "--max-agents" in meter.descriptions["loom.auth.trust_agents_used_ratio"]
