"""Token prices on code.* responses are measured, not len//4: per-kind rates
for the body text and the JSON envelope the agent actually ingests; empty
enrichment fields leave the wire."""

from __future__ import annotations

import json

from server.code.domain.units import (
    CHARS_PER_TOKEN,
    CodeUnit,
    budget_units,
    estimate_tokens,
    estimate_unit_tokens,
    kind_of_path,
)


def test_kind_of_path_and_rates() -> None:
    assert kind_of_path("tests/queries/0_stateless/01.sql") == "sql"
    assert kind_of_path("ci/run.sh") == "sh" and kind_of_path("x.bash") == "sh"
    assert kind_of_path("config.yml") == "yaml" and kind_of_path("a.xml") == "xml"
    assert kind_of_path("src/a.cpp") == "default" and kind_of_path("") == "default"
    # measured: kinds at or above 4 chars/token keep the safe 4.0
    assert (
        CHARS_PER_TOKEN["default"] == 4.0 and CHARS_PER_TOKEN["yaml"] < CHARS_PER_TOKEN["sql"] < 4.0
    )


def test_dense_kinds_cost_more_than_bytes_over_four() -> None:
    text = "SELECT a, b FROM t WHERE x = 1 ORDER BY a LIMIT 10;\n" * 20
    assert estimate_tokens(text, "q.sql") > len(text) // 4
    assert estimate_tokens(text, "q.cpp") == len(text) // 4
    assert estimate_tokens("", "q.yaml") == 0


def _unit(body: str = "", **kw) -> CodeUnit:
    base = dict(
        unit_id="u",
        path="src/a.cpp",
        start_line=1,
        end_line=9,
        symbol="DB::A::f",
        kind="function",
        signature="void f()",
        body=body,
    )
    base.update(kw)
    return CodeUnit(**base)


def test_unit_estimate_counts_the_envelope_and_escaping() -> None:
    body = 'x = "a"\n' * 100  # quotes and newlines escape to two chars each
    u = _unit(body)
    est = estimate_unit_tokens(u)
    # the JSON-escaped body alone exceeds len(body)//4; the card adds keys
    assert est > len(body) // 4
    assert est > estimate_unit_tokens(u, body="") > 0
    # the estimate tracks the serialized size within a few percent
    wire = len(json.dumps(u.model_dump(mode="json"), separators=(",", ":")))
    assert abs(est - wire / 4) / (wire / 4) < 0.05


def test_compact_serializer_drops_empty_enrichment_and_keeps_core() -> None:
    d = _unit().model_dump(mode="json")
    for core in (
        "unit_id",
        "path",
        "start_line",
        "end_line",
        "symbol",
        "kind",
        "signature",
        "body",
        "elided",
        "description",
        "tier",
        "score",
    ):
        assert core in d
    for opt in (
        "body_hash",
        "recent_prs",
        "examples",
        "lessons",
        "churn",
        "amp",
        "tested",
        "callees",
        "granularity",
        "next",
    ):
        assert opt not in d
    rich = _unit(
        churn=3,
        tested=True,
        granularity="card",
        next="code.symbol name=DB::A::f",
        callees=[{"n": "g"}],
    ).model_dump(mode="json")
    assert rich["churn"] == 3 and rich["tested"] is True and rich["granularity"] == "card"
    assert rich["next"].startswith("code.symbol") and rich["callees"] == [{"n": "g"}]
    assert "amp" not in rich and "lessons" not in rich
    # model_dump_json (the transport path) goes through the same serializer
    assert "recent_prs" not in json.loads(_unit().model_dump_json())


def test_budget_accounts_in_wire_tokens() -> None:
    units = [_unit("y" * 1200, unit_id=f"u{i}") for i in range(4)]
    out, used = budget_units(units, token_budget=800)
    assert len(out) == 4  # every slot at least a card
    assert used <= 800
    full = [u for u in out if not u.elided]
    assert full and all(len(u.body) == 1200 for u in full)
    # the reported cost is what the serialized units cost, not len(body)//4
    assert used >= sum(estimate_unit_tokens(u) for u in out) - len(out)
