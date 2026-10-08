"""A const named in a later export clause is an exported unit."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.lang_typescript import export_clause_names


def test_export_clause_names_read_locals_not_aliases_and_skip_reexports_and_types() -> None:
    text = (
        "const A = 1; const B = 2; const cfg = { x: 1 };\n"
        "export { A, B as Bee, type T };\nexport { C } from './c';\nexport default cfg;\n"
    )
    assert export_clause_names(text) == {"A", "B", "cfg"}


def test_the_component_kit_idiom_indexes_its_wrappers() -> None:
    pf = parse_file(
        "web/src/components/ui/collapsible.tsx",
        b'"use client";\nimport * as CollapsiblePrimitive from "@radix-ui/react-collapsible";\n\n'
        b"const Collapsible = CollapsiblePrimitive.Root;\n\n"
        b"const CollapsibleTrigger = CollapsiblePrimitive.CollapsibleTrigger;\n\n"
        b"const helper = 1;\n\n"
        b"export { Collapsible, CollapsibleTrigger as Trigger };\n",
    )
    names = [(s.kind, s.name) for s in pf.symbols]
    assert ("const", "Collapsible") in names and ("const", "CollapsibleTrigger") in names
    assert ("const", "helper") not in names  # not exported, one line: no unit, as before


def test_export_default_of_a_const_indexes_it() -> None:
    pf = parse_file("web/src/config.ts", b"const cfg = { a: 1 };\nexport default cfg;\n")
    # The literal-valued key `a` is a field under the const
    assert [(s.kind, s.name) for s in pf.symbols] == [("const", "cfg"), ("field", "a")]
