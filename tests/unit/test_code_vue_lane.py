"""A Vue single-file component is a JavaScript / TypeScript
lane file — the script block parsed offset-preservingly with the grammar
its `lang` attribute names, the component a `function` unit over the
template, template elements and handlers bound as `calls` edges, `.vue`
imports resolved through the `@/` alias ladder."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_typescript")
pytest.importorskip("tree_sitter_javascript")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.chunker import chunk_file
from server.code.domain.extract.edges_treesitter import call_sites, extract_edges_and_misses
from server.code.domain.extract.lang_detect import detect_by_extension
from server.code.domain.extract.lang_typescript_imports import (
    import_bindings,
    import_candidates,
    resolve_imports,
)
from server.code.domain.extract.lang_vue import (
    component_name,
    parse_vue,
    script_grammar,
    sfc_blocks,
    synthetic_script,
    template_edges,
    template_sites,
)

_CHILD = b"""<template>
    <!-- A bordered panel. -->
    <div class="dv" @click="toggle">
        <slot />
    </div>
</template>

<script setup>
import { ref } from 'vue'

const open = ref(true)

const toggle = () => {
    open.value = !open.value
}
</script>

<style scoped>
.dv { border: 1px solid grey; }
</style>
"""

_PAGE = b"""<template>
    <div class="page">
        <DataView :loading="loading">
            <template #header>
                <Button label="Refresh" @click="reload" />
                <Button label="Clear" v-on:click="clearFilters()" />
            </template>
            <li v-for="row in rows" :key="row.id">{{ formatRow(row) }}</li>
            <Radix.Arrow />
        </DataView>
        <data-view v-if="empty" />
        <router-link :to="{ name: 'x' }" @click="router.push({})">New</router-link>
        <span @click="() => reload()">{{ missing(1) }}</span>
    </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { Button } from 'primevue'
import * as Radix from '@radix-ui/react-tooltip'

import DataView from '@/components/common/DataView.vue'
import { useRows } from '@/composables/useRows'

const { rows, loading, load } = useRows()

const empty = computed(() => rows.value.length === 0)

const reload = async () => {
    await load({ limit: 50 })
}

const clearFilters = () => {
    reload()
}

const formatRow = (row) => `${row.name}`

onMounted(reload)
</script>
"""

_TYPED = b"""<template>
    <span>{{ fmt(props.n) }}</span>
</template>

<script setup lang="ts">
interface Props {
    n: number
}

const props = defineProps<Props>()

function fmt(n: number): string {
    return `${n}`
}
</script>
"""

_CHILD_PATH = "app/src/components/common/DataView.vue"
_PAGE_PATH = "app/src/views/Page.vue"


def _table(*pfs):
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in pfs:
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    return table


def test_blocks_are_column_zero_and_nested_slot_templates_do_not_close_them() -> None:
    blocks = sfc_blocks(_PAGE.decode())
    assert [(b.kind, b.start_line, b.end_line) for b in blocks] == [
        ("template", 1, 15),
        ("script", 17, 40),
    ]
    child = sfc_blocks(_CHILD.decode())
    assert [(b.kind, b.start_line, b.end_line) for b in child] == [
        ("template", 1, 6),
        ("script", 8, 16),
        ("style", 18, 20),
    ]
    assert child[1].body_start == 9 and child[1].body_end == 15
    one_line = sfc_blocks(
        '<script setup>import x from "y"</script>\n<template>\n<p/>\n</template>\n'
    )
    assert [(b.kind, b.start_line, b.end_line) for b in one_line] == [
        ("script", 1, 1),
        ("template", 2, 4),
    ]


def test_synthetic_script_preserves_lines_and_byte_offsets() -> None:
    blocks = sfc_blocks(_CHILD.decode())
    synth = synthetic_script(_CHILD, blocks)
    assert len(synth) == len(_CHILD)
    assert synth.count(b"\n") == _CHILD.count(b"\n")
    rows = synth.split(b"\n")
    assert rows[0].strip() == b"" and rows[8] == b"import { ref } from 'vue'"
    assert rows[18].strip() == b""  # the style block is blank too


def test_script_grammar_follows_the_lang_attribute() -> None:
    assert script_grammar(sfc_blocks(_PAGE.decode())) == ("javascript", "javascript")
    assert script_grammar(sfc_blocks(_TYPED.decode())) == ("typescript", "typescript")
    assert script_grammar(sfc_blocks('<script setup lang="tsx">\n</script>\n')) == (
        "typescript",
        "tsx",
    )


def test_component_symbol_spans_the_template_and_script_symbols_keep_their_lines() -> None:
    pf = parse_vue(_CHILD_PATH, _CHILD)
    assert pf.lang == "javascript" and pf.source == _CHILD
    comp = pf.symbols[0]
    assert (comp.name, comp.kind, comp.signature, comp.start_line, comp.end_line) == (
        "DataView",
        "function",
        "component",
        1,
        6,
    )
    assert comp.qualified_name == "app.src.components.common.DataView::DataView"
    assert comp.doc_comment == "A bordered panel."
    by_name = {s.name: s for s in pf.symbols}
    assert (by_name["toggle"].kind, by_name["toggle"].start_line, by_name["toggle"].end_line) == (
        "function",
        13,
        15,
    )
    assert "open" not in by_name  # a plain `const x = ref()` is no unit, as in a `.js` module
    assert pf.includes == ["vue"]
    # the tree's offsets are the original's: the walker reads real names
    assert [n for _l, n in call_sites(parse_vue(_PAGE_PATH, _PAGE))] == [
        "useRows",
        "computed",
        "load",
        "reload",
        "onMounted",
    ]


def test_component_name_from_stem_and_index_directory() -> None:
    assert component_name("a/b/CampaignList.vue") == "CampaignList"
    assert component_name("a/Widget/index.vue") == "Widget"


def test_template_sites_tags_members_bound_kebab_and_handlers() -> None:
    pf = parse_vue(_PAGE_PATH, _PAGE)
    sites = template_sites(pf)
    assert sites == [
        (3, "DataView", "jsx"),
        (5, "Button", "jsx"),
        (5, "reload", "handler"),
        (6, "Button", "jsx"),
        (6, "clearFilters", "handler"),
        (8, "formatRow", "handler"),
        (9, "Arrow", "jsx:Radix"),
        (11, "DataView", "jsx"),  # `<data-view>`: the imported binding's kebab form
        (13, "missing", "handler"),
    ]
    # `<router-link>` is not imported → no site; `router.push({})` and
    # `() => reload()` are not a function name → no handler site


def test_template_edges_bind_imports_same_file_handlers_and_nothing_else() -> None:
    child = parse_vue(_CHILD_PATH, _CHILD)
    page = parse_vue(_PAGE_PATH, _PAGE)
    # a repository-unique `Button` and `Arrow` that must NOT be bound
    from server.code.domain.extract.lang_typescript import parse_typescript

    lib = parse_typescript(
        "app/src/ui/Button.tsx",
        b"export function Button() { return null }\nexport function Arrow() { return null }\n",
    )
    table = _table(child, page, lib)
    bindings = import_bindings(page)
    edges = template_edges(page, template_sites(page, bindings), table, bindings)
    got = sorted((e.to_qualified_name.rsplit("::", 1)[-1], e.line, e.confidence) for e in edges)
    assert got == [
        ("DataView", 3, 1.0),
        ("DataView", 11, 1.0),
        ("clearFilters", 6, 1.0),
        ("formatRow", 8, 1.0),
        ("reload", 5, 1.0),
    ]
    assert {e.edge_kind for e in edges} == {"calls"}
    assert {e.from_qualified_name for e in edges} == {"app.src.views.Page::Page"}
    # the child's own handler binds to its own function
    child_edges = template_edges(child, template_sites(child), _table(child))
    assert [(e.to_qualified_name.rsplit("::", 1)[-1], e.line) for e in child_edges] == [
        ("toggle", 3)
    ]


def test_typescript_script_block_and_interpolation_handler() -> None:
    pf = parse_vue("app/src/components/Typed.vue", _TYPED)
    assert pf.lang == "typescript"
    kinds = {s.name: s.kind for s in pf.symbols}
    assert kinds == {
        "Typed": "function",
        "Props": "interface",
        "fmt": "function",
        "Typed.props": "interface",
    }  # The macro block
    edges = template_edges(pf, template_sites(pf), _table(pf))
    assert [(e.to_qualified_name.rsplit("::", 1)[-1], e.line) for e in edges] == [("fmt", 2)]


def test_vue_imports_resolve_through_the_root_alias() -> None:
    cands = import_candidates(_PAGE_PATH, "@/components/common/DataView.vue")
    assert "app/src/components/common/DataView.vue" in cands
    present = {_CHILD_PATH, "app/src/composables/useRows.js"}
    assert resolve_imports(
        _PAGE_PATH, ["@/components/common/DataView.vue", "@/composables/useRows", "vue"], present
    ) == [_CHILD_PATH, "app/src/composables/useRows.js"]
    # `./x.vue` relative and `index.vue` directory forms
    assert "a/b/Row.vue" in import_candidates("a/b/Table.vue", "./Row.vue")
    assert "a/Widget/index.vue" in import_candidates("a/Page.vue", "./Widget")


def test_ingest_detects_and_parses_a_vue_file_as_its_script_language() -> None:
    assert detect_by_extension("x/y/Foo.vue") == "vue"
    pf = parse_file(_PAGE_PATH, _PAGE)
    assert pf is not None and pf.lang == "javascript"
    assert pf.symbols[0].signature == "component"
    edges, misses = extract_edges_and_misses(pf, _table(pf), None)
    assert [
        (e.from_qualified_name.rsplit("::", 1)[-1], e.to_qualified_name.rsplit("::", 1)[-1])
        for e in edges
        if e.edge_kind == "calls"
    ] == [("clearFilters", "reload")]
    assert {m.callee_name for m in misses} <= {"useRows", "computed", "load", "onMounted"}


def test_chunks_keep_the_original_text_template_as_one_unit_functions_by_span() -> None:
    pf = parse_vue(_PAGE_PATH, _PAGE)
    chunks = chunk_file(pf)
    comp = [c for c in chunks if c.kind == "function" and c.scope.endswith("::Page")]
    assert len(comp) == 1 and comp[0].start_line == 1 and comp[0].end_line == 15
    assert comp[0].content.startswith("<template>") and "<DataView" in comp[0].content
    fns = {
        c.scope.rsplit("::", 1)[-1]: (c.start_line, c.end_line)
        for c in chunks
        if c.kind == "function" and not c.scope.endswith("::Page")
    }
    assert fns["reload"] == (29, 31) and fns["formatRow"] == (37, 37) and fns["empty"] == (27, 27)
    assert all("<template>" not in c.content for c in chunks if not c.scope.endswith("::Page"))
    # an oversized template still chunks (line-cut pieces, one scope)
    rows = b"".join(
        b'    <p class="row">line %d of the panel body text here</p>\n' % i for i in range(400)
    )
    big = b"<template>\n" + rows + b"</template>\n<script setup>\nconst x = 1\n</script>\n"
    pieces = [
        c
        for c in chunk_file(parse_vue("a/Big.vue", big), max_chars=3000)
        if c.scope.endswith("::Big")
    ]
    assert len(pieces) > 1 and all(len(c.content) <= 3000 for c in pieces)
    assert pieces[0].start_line == 1 and pieces[-1].end_line == 402


def test_macro_blocks_are_interface_symbols_and_bound_function_props_are_sites() -> None:
    """`defineProps` / `defineEmits` become `<Comp>.props` /
    `<Comp>.emits` interface symbols carrying the member names; a bound prop
    whose value names a same-file function is a handler site, a bound ref
    is not."""
    from server.code.domain.extract.lang_vue import parse_vue, template_sites

    src = (
        "<template>\n"
        '  <Grid :formatter="fmtRow" :items="rows" @select="pick" />\n'
        "</template>\n"
        "<script setup>\n"
        "import Grid from '@/components/Grid.vue'\n"
        "const props = defineProps({ rows: { type: Array }, dense: Boolean })\n"
        "const emit = defineEmits(['select', 'close'])\n"
        "const fmtRow = (r) => r\n"
        "const pick = () => emit('select')\n"
        "</script>\n"
    )
    pf = parse_vue("ui/src/Widget.vue", src.encode())
    by_name = {s.name: s for s in pf.symbols}
    assert by_name["Widget.props"].kind == "interface"
    assert by_name["Widget.props"].signature == "props: rows, dense"
    assert by_name["Widget.emits"].signature == "emits: select, close"
    assert by_name["Widget.props"].qualified_name == "ui.src.Widget::Widget.props"
    sites = template_sites(pf)
    assert (2, "Grid", "jsx") in sites
    assert (2, "fmtRow", "handler") in sites and (2, "pick", "handler") in sites
    assert not any(name == "rows" for _l, name, _k in sites)  # a bound ref is data, not a call
