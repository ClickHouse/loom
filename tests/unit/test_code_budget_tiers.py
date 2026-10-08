"""Budget-adaptive granularity: cards floor, escalation pointers, and the
route-aware default. Whole-file expansion is unit-tested at the helper."""

from __future__ import annotations

import asyncio

from server.code.domain.units import CodeUnit, budget_units


def _u(i: int, body_lines: int = 40) -> CodeUnit:
    return CodeUnit(
        unit_id=f"u{i}",
        path=f"src/F{i}.cpp",
        start_line=1,
        end_line=9,
        symbol=f"DB::F{i}::run",
        kind="function",
        signature="void run()",
        body="int x = 0;\n" * body_lines,
    )


def test_tight_budget_head_reservation_already_covers_all_slots() -> None:
    # The two-pass fill guarantees every requested slot at least a card, so an
    # extra "auto-cards floor" would only remove bodies that genuinely fit,
    # which is strictly worse. This pins that contract.
    units = [_u(i) for i in range(10)]
    out, used = budget_units(units, 600)
    assert len(out) == 10  # every slot represented
    assert all(u.granularity == "card" for u in out if u.elided)
    assert used <= 600


def test_cards_carry_an_escalation_pointer() -> None:
    out, _ = budget_units([_u(1)], 6000, bodies="none")
    assert out[0].next == "code.symbol name=DB::F1::run"
    chunk = CodeUnit(
        unit_id="c", path="src/X.cpp", start_line=5, end_line=9, kind="other", body="y\n" * 40
    )
    out2, _ = budget_units([chunk], 6000, bodies="none")
    assert out2[0].next.startswith("code.grep ")


def test_normal_budget_keeps_unit_granularity() -> None:
    out, _ = budget_units([_u(1)], 6000)
    assert out[0].granularity == "unit" and not out[0].elided


def test_whole_file_expansion_fits_and_discloses() -> None:
    from server.code.app.search.handler import _expand_top_to_file

    class _Repo:
        async def live_chunks_for_path(self, org, ns, path, model):
            return [
                {"start_line": 1, "end_line": 4, "content": "part one\n" + "a = 1\n" * 50},
                {"start_line": 5, "end_line": 9, "content": "part two\n" + "b = 2\n" * 50},
                {"start_line": 5, "end_line": 9, "content": "dup window"},
            ]

    units = [_u(1, body_lines=2)]
    spent = asyncio.run(_expand_top_to_file(_Repo(), "o", "n", units, 5000, None))
    assert spent > 0
    assert units[0].granularity == "file" and units[0].end_line == 9
    assert units[0].body.startswith("part one") and "part two" in units[0].body
    assert "dup window" not in units[0].body  # dup span deduped


def test_whole_file_expansion_refuses_when_too_big() -> None:
    from server.code.app.search.handler import _expand_top_to_file

    class _Repo:
        async def live_chunks_for_path(self, org, ns, path, model):
            return [
                {"start_line": 1, "end_line": 4, "content": "x" * 40000},
                {"start_line": 5, "end_line": 9, "content": "y" * 40000},
            ]

    units = [_u(1, body_lines=2)]
    spent = asyncio.run(_expand_top_to_file(_Repo(), "o", "n", units, 500, None))
    assert spent == 0 and units[0].granularity == "unit"


def test_code_ask_token_budget_defaults_to_setting_and_honours_caller():
    """code.ask grounds synthesis on a budgeted retrieval: the caller's
    token_budget when given, the deployment default otherwise. The judged
    bench sweeps this knob, so a silent fallback here would flatten the
    tokens-vs-quality curve into one point."""
    from unittest.mock import patch

    from server.code.api.models_search import CodeAskInput
    from server.code.app.ask import _ask_search_kwargs
    from server.settings import settings

    with patch.object(settings, "code_ask_token_budget", 4000):
        default_kw = _ask_search_kwargs(
            CodeAskInput(org="o", namespace="ns", question="how do merges pick parts?"),
            0.5,
            0.1,
        )
        caller_kw = _ask_search_kwargs(
            CodeAskInput(
                org="o",
                namespace="ns",
                question="how do merges pick parts?",
                token_budget=1500,
            ),
            0.5,
            0.1,
        )
    assert default_kw["token_budget"] == 4000
    assert caller_kw["token_budget"] == 1500
    assert default_kw["abstain_threshold"] == 0.5 and default_kw["noise_floor"] == 0.1
    # The response reports what synthesis saw, not what the caller asked for.
    from server.code.api.models_search import CodeAskOutput

    assert CodeAskOutput(source="synthesized").context_tokens == 0
