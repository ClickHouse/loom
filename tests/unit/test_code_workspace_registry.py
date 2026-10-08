"""Workspace members by their manifests; a.d.ts
twin never ties with the implementation."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import (
    extract_edges_and_misses,
    prefer_implementation,
)
from server.code.domain.extract.ts_workspace import (
    member_dirs,
    package_name_of,
    registry,
    workspace_globs,
)


def test_workspace_globs_read_pnpm_yaml_then_npm_workspaces() -> None:
    yaml = "packages:\n  # comment\n  - \"web\"\n  - 'packages/**'\n  - sv-addons/*\n  - '!**/test/**'\ncatalog:\n  zod: ^3\n"
    assert workspace_globs(yaml, "") == ["web", "packages/**", "sv-addons/*"]
    assert workspace_globs("", '{"workspaces": ["./packages/*", "./tests/runner"]}') == [
        "packages/*",
        "tests/runner",
    ]
    assert workspace_globs("", '{"workspaces": {"packages": ["apps/*"]}}') == ["apps/*"]
    assert workspace_globs("", "not json") == []


def test_member_dirs_expand_globs_against_the_live_paths() -> None:
    present = {
        "package.json",
        "docs/package.json",
        "packages/cli/package.json",
        "packages/cli/src/index.ts",
        "packages/deep/nested/package.json",
        "sv-addons/sv/package.json",
        "node_modules/x/package.json",
        "packages/cli/node_modules/y/package.json",
    }
    assert member_dirs(["docs", "packages/*", "sv-addons/*"], present) == [
        "docs",
        "packages/cli",
        "sv-addons/sv",
    ]
    assert member_dirs(["packages/**"], present) == ["packages/cli", "packages/deep/nested"]
    assert registry(
        {"packages/cli": '{"name": "shadcn-svelte"}', "docs": '{"name": "docs"}', "x": "junk"}
    ) == {
        "shadcn-svelte": "packages/cli",
        "docs": "docs",
    }
    assert package_name_of("shadcn-svelte/registry") == "shadcn-svelte"
    assert package_name_of("@shadcn-svelte/sv/utils") == "@shadcn-svelte/sv"
    assert (
        package_name_of("./x") == ""
        and package_name_of("$lib/x") == ""
        and package_name_of("@/x") == ""
    )


def test_the_registry_resolves_a_member_named_unlike_its_directory() -> None:
    import asyncio

    from server.code.app.index import ingest_resolve

    class _Repo:
        texts = {
            "pnpm-workspace.yaml": "packages:\n  - packages/*\n  - docs\n",
            "package.json": '{"name": "root", "private": true}',
            "packages/cli/package.json": '{"name": "shadcn-svelte", "exports": {".": "./src/index.ts", "./registry": "./src/registry/index.ts"}}',
            "docs/package.json": '{"name": "docs"}',
        }
        paths = set(texts) | {
            "packages/cli/src/index.ts",
            "packages/cli/src/registry/index.ts",
            "docs/src/lib/x.ts",
        }

        async def live_path_set(self, org, ns):
            return set(self.paths)

        async def live_text_for_path(self, org, ns, path):
            return self.texts.get(path, "")

    for cache in (
        ingest_resolve._LIVE_PATHS_CACHE,
        ingest_resolve._WORKSPACE_CACHE,
        ingest_resolve._TSCONFIG_CACHE,
    ):
        cache.clear()
    pf = parse_file(
        "docs/src/lib/x.ts",
        b"import { getRegistry } from 'shadcn-svelte/registry';\nimport { cli } from 'shadcn-svelte';\n",
    )
    asyncio.run(ingest_resolve._resolve_lane_includes(_Repo(), "o", "n", pf))  # type: ignore[arg-type]
    assert pf.includes == ["packages/cli/src/registry/index.ts", "packages/cli/src/index.ts"]
    ingest_resolve._LIVE_PATHS_CACHE.clear()
    ingest_resolve._WORKSPACE_CACHE.clear()


def test_a_declaration_twin_yields_to_the_implementation() -> None:
    impl = ("i1", "lib.log-controller::LogController", "class")
    twin = ("t1", "types.logger.d::LogController", "class")
    assert prefer_implementation([impl, twin]) == [impl]
    assert prefer_implementation([twin]) == [twin]  # types-only: kept
    assert prefer_implementation([impl, ("i2", "other.mod::LogController", "class")]) == [
        impl,
        ("i2", "other.mod::LogController", "class"),
    ]
    # heritage: with the require binding the import rung binds the .js class at 1.0
    # without it the twin rule alone leaves the implementation
    table = {"LogController": [impl, twin]}
    bound = parse_file(
        "test/x.test.js",
        b"const { LogController } = require('../lib/log-controller')\n"
        b"class Quiet extends LogController {}\n",
    )
    bound.includes = ["lib/log-controller.js"]
    edges, _ = extract_edges_and_misses(bound, table, None)
    assert [(e.to_qualified_name, e.confidence) for e in edges if e.edge_kind == "inherits"] == [
        ("lib.log-controller::LogController", 1.0),
    ]
    bare = parse_file("test/y.test.js", b"class Quiet extends LogController {}\n")
    edges, _ = extract_edges_and_misses(bare, table, None)
    assert [
        (e.to_qualified_name, round(e.confidence, 1)) for e in edges if e.edge_kind == "inherits"
    ] == [
        ("lib.log-controller::LogController", 0.9),
    ]


def test_lerna_rush_and_nx_manifests_name_members_too() -> None:
    from server.code.domain.extract.ts_workspace import extra_manifest_globs

    lerna = '{"packages": ["packages/*", "tools/cli"], "version": "independent"}'
    rush = '/** rush */ {\n  "projects": [ { "packageName": "@x/a", "projectFolder": "apps/a", }, ],\n}'
    nx = '{"workspaceLayout": {"appsDir": "apps", "libsDir": "libs"}}'
    assert extra_manifest_globs(lerna, rush, nx) == [
        "packages/*",
        "tools/cli",
        "apps/a",
        "apps/*",
        "libs/*",
    ]
    assert extra_manifest_globs("", "not json", "") == []
