"""HTTP routes ride as `http_route` facets at the handler."""

from __future__ import annotations

import pytest

from server.code.app.index.ingest_chunks import _chunks_for
from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.ts_routes import route_facets
from tests.unit.code_fixtures import grows_linearly


def test_four_route_spellings_become_facets() -> None:
    nest = parse_file(
        "src/users/users.controller.ts",
        b"@Controller('users')\nexport class UsersController {\n  @Get(':id')\n  findOne() {}\n  @Post()\n  create() {}\n}\n",
    )
    assert [(f.kind, f.name, f.line, f.confidence) for f in route_facets(nest)] == [
        ("http_route", "GET /users/:id", 3, 1.0),
        ("http_route", "POST /users", 5, 1.0),
    ]
    app = parse_file(
        "app/(dashboard)/api/posts/[postId]/route.ts",
        b"export async function GET() {}\nexport const DELETE = async () => {}\n",
    )
    assert [f.name for f in route_facets(app)] == [
        "GET /api/posts/:postId",
        "DELETE /api/posts/:postId",
    ]
    page = parse_file(
        "app/(marketing)/pricing/page.tsx",
        b"export default function PricingPage() { return null }\n",
    )
    assert [f.name for f in route_facets(page)] == ["PAGE /pricing"]
    pages = parse_file(
        "web/src/pages/api/public/traces/[traceId].ts",
        b"export default async function handler() {}\n",
    )
    assert [(f.name, f.confidence) for f in route_facets(pages)] == [
        ("ANY /api/public/traces/:traceId", 0.9)
    ]
    express = parse_file(
        "lib/routes.js",
        b"app.get('/health', h)\nrouter.post('/users/:id', h)\nfastify.route({ method: 'PUT', url: '/items', handler })\n"
        b"fastify.route({ method: 'GET', // the method v1 used\n url: '/a' })\n",
    )
    assert [f.name for f in route_facets(express)] == [
        "GET /health",
        "POST /users/:id",
        "PUT /items",
        "GET /a",
    ]
    plain = parse_file("lib/util.ts", b"export function add(a: number) { return a }\n")
    assert route_facets(plain) == []


def test_the_chunk_step_carries_route_facets_for_typescript() -> None:
    pf = parse_file(
        "src/users/users.controller.ts",
        b"@Controller('users')\nexport class UsersController {\n  @Get()\n  list() { return [] }\n}\n",
    )
    chunks, prefixes, facets = _chunks_for(pf, pf.path, pf.source, 6000)
    assert chunks and [(f.kind, f.name) for f in facets] == [("http_route", "GET /users")]


def test_a_typescript_file_with_routes_writes_its_facets_and_one_without_does_not() -> None:
    from server.code.app.index.ingest_parse import _facets_ride_symbols

    ctl = parse_file(
        "src/users/users.controller.ts",
        b"@Controller('users')\nexport class C {\n  @Get()\n  list() {}\n}\n",
    )
    assert _facets_ride_symbols(ctl, ctl.path, False, route_facets(ctl)) is True
    util = parse_file("src/util.ts", b"export const x = 1\n")
    assert _facets_ride_symbols(util, util.path, False, route_facets(util)) is False


def test_nest_object_form_controller_prefixes_its_routes() -> None:
    pf = parse_file(
        "src/users/users.controller.ts",
        b"@Controller({\n  path: 'users',\n  version: '1',\n})\nexport class UsersController {\n  @Get(':id')\n  one() {}\n}\n",
    )
    assert [f.name for f in route_facets(pf)] == ["GET /users/:id"]


@pytest.mark.parametrize(
    "unit",
    [
        "@Controller(){",
        "@Controller({",
        "@Controller({path:''",
        "app.get('/a')\n",
        ".route({",
        ".route({method:'GET',",
    ],
)
def test_route_facets_over_a_run_of_controllers_or_routes_cost_linear_time(unit: str) -> None:
    from server.code.domain.extract.treesitter import ParsedFile

    def run(n: int) -> None:
        route_facets(
            ParsedFile(path="a.ts", lang="typescript", tree=None, source=(unit * n).encode())
        )

    grows_linearly(run, 5_000, 20_000)


def test_route_decorators_in_one_unclosed_controller_cost_linear_time() -> None:
    from server.code.domain.extract.treesitter import ParsedFile

    def run(n: int) -> None:
        source = ("@Controller() class A {" + "@Get(" * n).encode()
        route_facets(ParsedFile(path="a.ts", lang="typescript", tree=None, source=source))

    grows_linearly(run, 5_000, 20_000)
