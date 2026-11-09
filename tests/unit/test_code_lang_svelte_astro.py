"""Svelte and Astro components index like Vue ones."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.lang_sfc import markup_sites, sfc_component_name
from server.code.domain.extract.lang_vue import template_edges

SVELTE = b"""<script lang="ts">
  import Button from '$lib/components/Button.svelte';
  import { Dialog } from './dialog';
  import { fmt } from '../utils/format';
  export let rows: string[] = [];
  function save() { return persist(rows); }
  function persist(r: string[]) { return r.length; }
  function label(n: number) { return fmt(n); }
</script>

<!-- The rows table -->
<Dialog.Root>
  <Button on:click={save} formatter={label}>{label(rows.length)}</Button>
  {#if persist(rows)}<p>{rows.length}</p>{/if}
</Dialog.Root>

<style>
  p { color: red; }
</style>
"""

ASTRO = b"""---
import Layout from '../layouts/Layout.astro';
import Card from '../components/Card.astro';
import { getPosts } from '../lib/posts';
const posts = await getPosts();
function title(p: { t: string }) { return p.t; }
---
<Layout>
  {posts.map((p) => <Card heading={title(p)} onClick={title} />)}
</Layout>
"""


def _table(*rows):
    out: dict[str, list[tuple[str, str, str]]] = {}
    for sid, qn, kind in rows:
        out.setdefault(qn.split("::")[-1], []).append((sid, qn, kind))
    return out


def test_a_svelte_component_is_a_unit_over_its_markup_with_its_script_functions() -> None:
    pf = parse_file("src/lib/Table.svelte", SVELTE)
    assert pf.lang == "typescript"
    syms = [(s.kind, s.name, s.start_line, s.end_line) for s in pf.symbols]
    assert (
        syms[0] == ("function", "Table", 11, 15) and pf.symbols[0].doc_comment == "The rows table"
    )
    assert [s.name for s in pf.symbols[1:] if s.kind == "function"] == ["save", "persist", "label"]
    assert ("const", "rows") in [
        (s.kind, s.name) for s in pf.symbols
    ]  # `export let` is an exported binding
    assert pf.includes == ["$lib/components/Button.svelte", "./dialog", "../utils/format"]
    sites = markup_sites(pf)
    assert (12, "Root", "jsx:Dialog") in sites and (13, "Button", "jsx") in sites
    assert (13, "save", "handler") in sites and (13, "label", "handler") in sites
    assert (14, "persist", "handler") in sites


def test_svelte_edges_bind_an_imported_component_and_same_file_handlers() -> None:
    pf = parse_file("src/lib/Table.svelte", SVELTE)
    pf.includes = ["src/lib/components/Button.svelte", "src/lib/dialog.ts", "src/utils/format.ts"]
    table = _table(
        ("b1", "src.lib.components.Button::Button", "function"),
        ("d1", "src.lib.dialog::Dialog", "const"),
    )
    edges = template_edges(pf, markup_sites(pf), table)
    got = {
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name.split("::")[-1], e.confidence)
        for e in edges
    }
    assert ("Table", "Button", 1.0) in got
    assert (
        ("Table", "save", 1.0) in got
        and ("Table", "label", 1.0) in got
        and ("Table", "persist", 1.0) in got
    )


def test_an_astro_component_reads_its_frontmatter_and_markup() -> None:
    pf = parse_file("src/pages/index.astro", ASTRO)
    assert pf.lang == "typescript"
    # `index.astro` takes its directory, as `index.vue` does
    assert [(s.kind, s.name) for s in pf.symbols][:2] == [
        ("function", "pages"),
        ("function", "title"),
    ]
    assert pf.symbols[0].start_line == 8 and pf.symbols[0].end_line == 10
    assert pf.includes == ["../layouts/Layout.astro", "../components/Card.astro", "../lib/posts"]
    sites = markup_sites(pf)
    assert (
        (8, "Layout", "jsx") in sites
        and (9, "Card", "jsx") in sites
        and (9, "title", "handler") in sites
    )


def test_sveltekit_route_files_take_their_directory() -> None:
    assert sfc_component_name("src/routes/blog/[slug]/+page.svelte") == "_slug__page"
    assert sfc_component_name("src/routes/+layout.svelte") == "routes_layout"
    assert sfc_component_name("src/lib/Button.svelte") == "Button"


# ---------------------------------------------------------------- names the importer chooses


def test_route_segments_are_not_native_marks() -> None:
    from server.code.domain.extract.name_scope import gate_name_table

    table = {
        "EditPost": [
            ("e1", "src.pages.posts.[...slug]._components.EditPost::EditPost", "function")
        ],
        "Docs": [("d1", "docs.src.routes.(app).docs.+page::app_page", "function")],
        "Visitor": [("v1", "DB::$anonymous_namespace_src/IO/Foo.cpp::Visitor", "class")],
    }
    gated = gate_name_table(table, "typescript")
    assert set(gated) == {"EditPost", "Docs"}  # the C++ shape still carries `/`


def test_a_default_import_binds_the_components_unit_whatever_the_local_name() -> None:
    import asyncio

    from server.code.domain.extract.lang_sfc import import_target_edges, import_targets

    pf = parse_file(
        "docs/src/lib/components/block-viewer.svelte",
        b"<script lang=\"ts\">\n  import BlockViewerCode from './block-viewer-code.svelte';\n"
        b"  import { Button } from '$lib/components/ui/button/index.js';\n</script>\n"
        b"<BlockViewerCode />\n<Button>go</Button>\n",
    )
    present = {
        "docs/src/lib/components/block-viewer-code.svelte",
        "src/lib/components/ui/button/index.js",
        "src/lib/components/ui/button/button.svelte",
    }
    barrel = 'import Root, { type ButtonProps } from "./button.svelte";\nexport { Root, type ButtonProps as Props, Root as Button };\n'

    async def read(path):
        return barrel if path.endswith("index.js") else ""

    targets = asyncio.run(import_targets(pf, present, read))
    assert targets == {
        "BlockViewerCode": "docs.src.lib.components.block-viewer-code::block-viewer-code",
        "Button": "src.lib.components.ui.button.button::button",
    }
    table = {
        "block-viewer-code": [
            ("b1", "docs.src.lib.components.block-viewer-code::block-viewer-code", "function")
        ],
        "button": [("b2", "src.lib.components.ui.button.button::button", "function")],
    }
    edges = import_target_edges(pf, markup_sites(pf), targets, table)
    assert {
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name.split("::")[-1], e.confidence)
        for e in edges
    } == {
        ("block-viewer", "block-viewer-code", 1.0),
        ("block-viewer", "button", 1.0),
    }


def test_barrel_alias_map_reads_the_shadcn_idiom_and_direct_reexports() -> None:
    from server.code.domain.extract.lang_sfc import barrel_alias_map

    text = (
        'import Root, {\n\ttype ButtonProps,\n\tbuttonVariants,\n} from "./button.svelte";\n'
        "export {\n\tRoot,\n\ttype ButtonProps as Props,\n\t//\n\tRoot as Button,\n\tbuttonVariants,\n};\n"
        'export { default as Dialog, Content as DialogContent } from "./dialog.svelte";\n'
    )
    m = barrel_alias_map(text)
    assert m["Root"] == ("./button.svelte", "default") and m["Button"] == (
        "./button.svelte",
        "default",
    )
    assert m["buttonVariants"] == ("./button.svelte", "buttonVariants")
    assert m["Dialog"] == ("./dialog.svelte", "default") and m["DialogContent"] == (
        "./dialog.svelte",
        "Content",
    )
    assert "Props" not in m  # a type re-export is not a component


def test_an_event_with_modifiers_names_its_handler() -> None:
    src = b"<form on:submit|preventDefault={save}></form>\n<a on:click|preventDefault|once={go}></a>\n"
    sites = markup_sites(parse_file("src/F.svelte", src), {})
    assert [(line, name) for line, name, _kind in sites] == [(1, "save"), (2, "go")]
