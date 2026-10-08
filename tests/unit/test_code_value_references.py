"""An identifier used as a value is a `reads` edge to the
function, class or const it names."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.lang_typescript_imports import import_bindings
from server.code.domain.extract.lang_typescript_refs import (
    value_reference_edges,
    value_reference_sites,
)
from server.code.domain.extract.treesitter import file_symbol_id

TABLE = {
    "cors": [("c1", "src.mw::cors", "function")],
    "trpcErrorToast": [("t1", "src.toast::trpcErrorToast", "function")],
    "UsersService": [("u1", "src.users::UsersService", "class")],
    "env": [("e1", "src.env::env", "const")],
}


def test_arguments_pairs_jsx_arrays_and_shorthand_are_reads() -> None:
    pf = parse_file(
        "src/app.tsx",
        b"import { cors } from './mw'\nimport { trpcErrorToast } from './toast'\n"
        b"import { UsersService } from './users'\nimport { env } from './env'\n"
        b"import { z } from 'zod'\n"
        b"export function setup(app: any) {\n"
        b"  app.use(cors)\n"
        b"  const opts = { onError: trpcErrorToast, providers: [UsersService], env }\n"
        b"  return <Button onClick={trpcErrorToast} schema={z} />\n"
        b"}\n"
        b"app.listen(env.PORT)\n",
    )
    b = import_bindings(pf)
    sites = value_reference_sites(pf, set(b) | {s.name for s in pf.symbols})
    assert [(ln, n) for ln, n in sites] == [
        (7, "cors"),
        (8, "trpcErrorToast"),
        (8, "UsersService"),
        (8, "env"),
        (9, "trpcErrorToast"),
        (9, "z"),
    ]
    edges = value_reference_edges(pf, sites, TABLE, b)
    got = [
        (
            e.edge_kind,
            e.from_qualified_name.split("::")[-1],
            e.to_qualified_name,
            e.line,
            e.confidence,
        )
        for e in edges
    ]
    assert got == [
        ("reads", "setup", "src.mw::cors", 7, 1.0),
        ("reads", "setup", "src.toast::trpcErrorToast", 8, 1.0),
        ("reads", "setup", "src.users::UsersService", 8, 1.0),
        ("reads", "setup", "src.env::env", 8, 1.0),
        ("reads", "setup", "src.toast::trpcErrorToast", 9, 1.0),
    ]
    # env.PORT is a member read, not a value site
    assert file_symbol_id("src/app.tsx") not in {e.from_symbol_id for e in edges}


def test_a_cast_around_a_passed_value_is_still_a_site() -> None:
    pf = parse_file(
        "src/cast.ts",
        b"import { cors } from './mw'\nexport function setup(app: any) {\n"
        b"  app.use(cors as any)\n  app.use(cors satisfies Middleware)\n  app.use(cors!)\n}\n",
    )
    b = import_bindings(pf)
    sites = value_reference_sites(pf, set(b))
    assert sites == [(3, "cors"), (4, "cors"), (5, "cors")]
    assert [e.line for e in value_reference_edges(pf, sites, TABLE, b)] == [3, 4, 5]


def test_callees_declarations_and_same_file_functions() -> None:
    pf = parse_file(
        "src/local.ts",
        b"export function handler() {}\nexport function other() {}\n"
        b"export const routes = { handler }\nexport function run(app: any) {\n  app.get('/x', handler)\n  handler()\n  const h = other\n  return h\n}\n",
    )
    sites = value_reference_sites(pf, {s.name for s in pf.symbols})
    assert sites == [(3, "handler"), (5, "handler"), (7, "other")]
    edges = value_reference_edges(pf, sites, {}, {})
    got = [
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name.split("::")[-1], e.line)
        for e in edges
    ]
    assert got == [
        ("routes", "handler", 3),
        ("run", "handler", 5),
        ("run", "other", 7),
    ]
