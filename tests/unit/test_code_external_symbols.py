"""Symbols outside the repo are recognised by prefix (fan-in, graph stats and
code.symbol's neighbour block drop them)."""

from __future__ import annotations

from server.code.domain.external_symbols import is_external_symbol


def test_external_symbols_are_recognised() -> None:
    assert is_external_symbol("std::__1::shared_ptr::operator->")
    assert is_external_symbol("testing::Message::Message")
    assert is_external_symbol("")
    assert not is_external_symbol("DB::MergeTreeData::renameTempPartAndAdd")
