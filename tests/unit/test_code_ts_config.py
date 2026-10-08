"""Tsconfig / jsconfig `paths`, `baseUrl` and `extends` give
alias and bare specifiers exact repository candidates."""

from __future__ import annotations

from server.code.domain.extract.ts_config import (
    config_dir_for,
    extends_candidates,
    parse_jsonc,
    paths_candidates,
    resolve_config,
)


def _pkgs(spec: str) -> list[str]:
    name = spec.split("/")[1] if spec.startswith("@") else spec.split("/")[0]
    return [f"packages/{name}", name]


def test_jsonc_with_comments_and_trailing_commas_parses_and_garbage_is_empty() -> None:
    assert parse_jsonc('{ // c\n "a": [1,], /* b */ "b": {"c": 2,}, }') == {"a": [1], "b": {"c": 2}}
    assert parse_jsonc("{ not json") == {} and parse_jsonc("") == {} and parse_jsonc("[1]") == {}


def test_the_nearest_config_governs_a_file() -> None:
    present = {"tsconfig.json", "web/tsconfig.json", "packages/shared/jsconfig.json"}
    assert config_dir_for("web/src/a/b.tsx", present) == "web"
    assert config_dir_for("worker/src/x.ts", present) == ""
    assert config_dir_for("packages/shared/src/x.ts", present) == "packages/shared"
    assert config_dir_for("web/src/a.ts", {"packages/x/tsconfig.json"}) is None


def test_paths_match_longest_prefix_substitute_star_and_try_fallbacks_in_order() -> None:
    texts = {
        "web/tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["./*"], "@/ui/*": ["./src/components/ui/*", "./vendor/ui/*"], "utils": ["./src/lib/utils"]}}}'
    }
    cfg = resolve_config("web/tsconfig.json", texts, _pkgs, set(texts))
    assert paths_candidates("@/src/features/x", cfg) == ["web/src/features/x"]
    assert paths_candidates("@/ui/button", cfg) == [
        "web/src/components/ui/button",
        "web/vendor/ui/button",
    ]
    assert paths_candidates("utils", cfg) == ["web/src/lib/utils"]
    assert paths_candidates("./rel", cfg) == [] and paths_candidates("zod", cfg) == []


def test_base_url_resolves_bare_specifiers_and_paths_targets() -> None:
    texts = {
        "tsconfig.json": '{"compilerOptions": {"baseUrl": "./src", "paths": {"~/*": ["./app/*"]}}}'
    }
    cfg = resolve_config("tsconfig.json", texts, _pkgs, set(texts))
    assert cfg.base_url == "src"
    assert paths_candidates("~/store/index", cfg) == ["src/app/store/index"]
    assert paths_candidates("lib/util", cfg) == ["src/lib/util"]  # baseUrl before node_modules


def test_extends_composes_relative_and_workspace_package_configs() -> None:
    texts = {
        "web/tsconfig.json": '{"extends": "@repo/typescript-config/nextjs.json", "compilerOptions": {"paths": {"@/*": ["./*"]}}}',
        "packages/typescript-config/nextjs.json": '{"extends": "./base", "compilerOptions": {"paths": {"#shared/*": ["../shared/src/*"]}}}',
        "packages/typescript-config/base.json": '{"compilerOptions": {"strict": true}}',
    }
    cfg = resolve_config("web/tsconfig.json", texts, _pkgs, set(texts))
    assert [t.base for t in cfg.tables] == ["web", "packages/typescript-config"]
    # the child's table answers `@/`; the parent's table, relative to ITS file, answers `#shared/`
    assert paths_candidates("@/src/x", cfg) == ["web/src/x"]
    assert paths_candidates("#shared/db", cfg) == ["packages/shared/src/db"]
    assert extends_candidates("web/tsconfig.json", "../tsconfig.base", []) == [
        "tsconfig.base.json",
        "tsconfig.base/tsconfig.json",
    ]
    # a chain that loops or names a missing config ends quietly
    loop = {"a/tsconfig.json": '{"extends": "./tsconfig.json"}'}
    assert resolve_config("a/tsconfig.json", loop, _pkgs, set(loop)).tables == []


# ---------------------------------------------------------------- through the ingest


def test_the_governing_tsconfig_binds_an_alias_the_guessing_ladder_cannot() -> None:
    import asyncio

    from server.code.app.index import ingest_resolve
    from server.code.app.index.ingest_parse import parse_file

    class _Repo:
        texts = {
            "tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"~/*": ["./app/*"]}}}'
        }
        paths = {"tsconfig.json", "app/store/index.ts", "lib/util.ts", "src/pages/home.tsx"}

        async def live_path_set(self, org, ns):
            return set(self.paths)

        async def live_text_for_path(self, org, ns, path):
            return self.texts.get(path, "")

    ingest_resolve._LIVE_PATHS_CACHE.clear()
    ingest_resolve._TSCONFIG_CACHE.clear()
    pf = parse_file(
        "src/pages/home.tsx",
        b"import { store } from '~/store';\nimport { util } from 'lib/util';\nimport z from 'zod';\n",
    )
    asyncio.run(ingest_resolve._resolve_lane_includes(_Repo(), "o", "n", pf))  # type: ignore[arg-type]
    # `~/store` -> app/store/index.ts by paths; `lib/util` -> lib/util.ts by baseUrl; zod is a library
    assert pf.includes == ["app/store/index.ts", "lib/util.ts"]
    ingest_resolve._LIVE_PATHS_CACHE.clear()
    ingest_resolve._TSCONFIG_CACHE.clear()


def test_destructured_require_binds_each_name_to_its_module() -> None:
    from server.code.app.index.ingest_parse import parse_file
    from server.code.domain.extract.lang_typescript_imports import import_bindings

    pf = parse_file(
        "test/logger.test.js",
        b"const { LogController, other: renamed } = require('../lib/log-controller')\n"
        b"const fastify = require('..')\n",
    )
    assert import_bindings(pf) == {
        "LogController": "../lib/log-controller",
        "renamed": "../lib/log-controller",
        "fastify": "..",
    }
