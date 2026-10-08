"""A function in an anonymous namespace is one the compiler tier spells
`DB::$anonymous_namespace_<path>::f`, a form nobody writes. The natural
spellings bind it: `DB::f` through the tail rule (even when no parser row
spells it `DB::f`), and a bare `f` as any bare name does. A refusal suggests
the natural spelling, not the path form."""

from __future__ import annotations

from server.code.app.index import symbol_forms as sf

SCIP = "DB::$anonymous_namespace_src/Interpreters/ActionsDAG.cpp::buildFilterActionsDAGImpl"


def _row(qn: str) -> dict[str, str]:
    return {"qualified_name": qn, "path": "src/Interpreters/ActionsDAG.cpp"}


def test_the_natural_name_drops_anonymous_segments_of_either_spelling() -> None:
    assert sf.natural_name(SCIP) == "DB::buildFilterActionsDAGImpl"
    assert sf.natural_name("DB::(anonymous namespace)::Foo::bar") == "DB::Foo::bar"
    assert sf.natural_name("DB::ActionsDAG::build") == "DB::ActionsDAG::build"


def test_db_f_binds_the_compiler_tiers_anonymous_row_when_it_is_the_only_one() -> None:
    rows = [_row(SCIP)]
    assert sf.tail_bind("DB::buildFilterActionsDAGImpl", rows, class_names=set()) == rows


def test_an_exact_parser_row_still_does_not_bind_by_tail() -> None:
    # the exact lookup owns `DB::f` when the table spells it so
    assert sf.tail_bind("DB::f", [_row("DB::f")], class_names=set()) == []


def test_two_anonymous_definitions_of_one_name_stay_ambiguous() -> None:
    rows = [
        _row("DB::$anonymous_namespace_src/A.cpp::f"),
        _row("DB::$anonymous_namespace_src/B.cpp::f"),
    ]
    assert sf.tail_bind("DB::f", rows, class_names=set()) == []
