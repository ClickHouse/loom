"""A decorator's calls attribute to the member it decorates;
a static member assigned onto a function is a unit."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.lang_typescript_refs import reference_edges, reference_sites


def test_a_decorators_call_attributes_to_the_decorated_member() -> None:
    src = (
        b"import { InfinityPaginationResponse } from '../utils/dto';\n@Controller('users')\n"
        b"export class UsersController {\n  @ApiOkResponse({\n    type: InfinityPaginationResponse(User),\n  })\n"
        b"  @Get()\n  async findAll() { return 1; }\n\n  plain() {}\n}\n"
    )
    pf = parse_file("src/users/users.controller.ts", src)
    spans = {s.name: (s.start_line, s.end_line) for s in pf.symbols if s.kind == "function"}
    assert spans["findAll"] == (4, 8) and spans["plain"] == (10, 10)  # the decorators join the span
    pf.includes = ["src/utils/dto.ts"]
    edges, _ = extract_edges_and_misses(
        pf,
        {
            "InfinityPaginationResponse": [
                ("i1", "src.utils.dto::InfinityPaginationResponse", "function")
            ]
        },
        None,
    )
    assert {
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name.split("::")[-1], e.confidence)
        for e in edges
        if e.edge_kind == "calls"
    } == {("findAll", "InfinityPaginationResponse", 1.0)}


def test_a_static_member_on_a_function_is_a_unit_that_a_member_element_binds_to() -> None:
    lib = parse_file(
        "components/post-item.tsx",
        b"export function PostItem() { return <div/> }\n"
        b"PostItem.Skeleton = function PostItemSkeleton() { return <span/> }\n"
        b"PostItem.helper = 3\n",
    )
    assert [(s.kind, s.qualified_name) for s in lib.symbols] == [
        ("function", "components.post-item::PostItem"),
        ("function", "components.post-item::PostItem::Skeleton"),
    ]
    page = parse_file(
        "app/(dashboard)/loading.tsx",
        b"import { PostItem } from '@/components/post-item'\n"
        b"export default function Loading() { return <PostItem.Skeleton /> }\n",
    )
    page.includes = ["components/post-item.tsx"]
    sites = reference_sites(page)
    assert (2, "Skeleton", "jsx:PostItem") in sites
    table = {"Skeleton": [("k1", "components.post-item::PostItem::Skeleton", "function")]}
    edges = reference_edges(page, sites, table, {"PostItem": "@/components/post-item"})
    assert [
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name.split("::")[-1], e.confidence)
        for e in edges
    ] == [
        ("Loading", "Skeleton", 1.0),
    ]
