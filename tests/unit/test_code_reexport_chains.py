"""A name a barrel re-exports binds to its declaration at 1.0."""

from __future__ import annotations

import asyncio

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.ts_reexports import declared_name, reexport_steps, resolve_export

SHARED = {
    "packages/shared/src/index.ts": 'export * from "./constants";\nexport { stringify as toText } from "./utils/stringify";\n',
    "packages/shared/src/constants.ts": "export function isTable(x: string) { return true }\n",
    "packages/shared/src/utils/stringify.ts": 'export function stringify(v: unknown) { return "" }\n',
    "docs/src/lib/ui/button/index.ts": 'import Root from "./button.svelte";\nexport { Root, Root as Button };\n',
}


def test_chains_follow_star_named_and_component_reexports() -> None:
    present = set(SHARED) | {"docs/src/lib/ui/button/button.svelte"}

    async def rt(p):
        return SHARED.get(p, "")

    run = lambda m, n: asyncio.run(resolve_export(m, n, present, rt))  # noqa: E731
    assert run("packages/shared/src/index.ts", "isTable") == (
        "packages/shared/src/constants.ts",
        "isTable",
    )
    assert run("packages/shared/src/index.ts", "toText") == (
        "packages/shared/src/utils/stringify.ts",
        "stringify",
    )
    assert run("packages/shared/src/index.ts", "nothing") is None
    assert run("docs/src/lib/ui/button/index.ts", "Button") == (
        "docs/src/lib/ui/button/button.svelte",
        "button",
    )
    assert reexport_steps(SHARED["docs/src/lib/ui/button/index.ts"], "Button") == [
        ("./button.svelte", "default")
    ]
    assert declared_name("export default function Foo() {}", "default") == "Foo"


def test_an_ambiguous_name_through_a_barrel_binds_to_the_declaring_module() -> None:
    pf = parse_file(
        "web/src/x.ts",
        b"import { isTable } from '@langfuse/shared';\nexport function use() { return isTable('a'); }\n",
    )
    table = {
        "isTable": [
            ("a", "packages.shared.src.constants::isTable", "function"),
            ("b", "web.src.legacy::isTable", "function"),
        ]
    }
    edges, misses = extract_edges_and_misses(pf, table, None)
    assert not [e for e in edges if e.edge_kind == "calls"]  # ambiguous without the chain
    chains = {"isTable": ("packages.shared.src.constants", "isTable")}
    edges, misses = extract_edges_and_misses(pf, table, None, None, None, chains)
    assert [(e.to_qualified_name, e.confidence) for e in edges if e.edge_kind == "calls"] == [
        ("packages.shared.src.constants::isTable", 1.0),
    ]


def test_the_ingest_builds_chains_from_the_live_texts() -> None:
    from server.code.app.index import ingest_edges, ingest_resolve

    class _Repo:
        async def live_path_set(self, org, ns):
            return set(SHARED) | {"web/src/x.ts", "packages/shared/package.json"}

        async def live_text_for_path(self, org, ns, path):
            return (
                SHARED.get(path, "")
                if path != "packages/shared/package.json"
                else '{"name": "@langfuse/shared", "exports": {".": "./src/index.ts"}}'
            )

    ingest_resolve._LIVE_PATHS_CACHE.clear()
    ingest_resolve._WORKSPACE_CACHE.clear()
    pf = parse_file(
        "web/src/x.ts",
        b"import { isTable, toText } from '@langfuse/shared';\nexport function use() { return isTable(toText(1)); }\n",
    )
    asyncio.run(ingest_resolve._resolve_lane_includes(_Repo(), "o", "n", pf))  # type: ignore[arg-type]
    chains = asyncio.run(ingest_edges._reexport_chains(_Repo(), "o", "n", pf))  # type: ignore[arg-type]
    assert chains == {
        "isTable": ("packages.shared.src.constants", "isTable"),
        "toText": ("packages.shared.src.utils.stringify", "stringify"),
    }
    ingest_resolve._LIVE_PATHS_CACHE.clear()
    ingest_resolve._WORKSPACE_CACHE.clear()
