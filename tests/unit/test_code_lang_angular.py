"""Angular components carry their selector; templates are
their component's unit and bind by selector and to the class's methods."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.lang_angular import (
    component_meta,
    inline_template_sites,
    owner_class,
    template_edges,
    template_sites,
)
from server.code.domain.extract.lang_detect import detect_by_extension

COMPONENT = b"""import { Component } from '@angular/core';
import { HeaderComponent } from './core/layout/header.component';

@Component({
  selector: 'app-root',
  templateUrl: './app.component.html',
  imports: [HeaderComponent],
})
export class AppComponent {
  save() { return 1; }
  fmt(n: number) { return String(n); }
}
"""
TEMPLATE = b"""<app-layout-header />
<router-outlet></router-outlet>
<button (click)="save()">{{ fmt(count) }}</button>
<li *ngFor="let a of articles()"></li>
"""


def test_a_component_class_carries_its_selector_in_the_signature() -> None:
    pf = parse_file("src/app/app.component.ts", COMPONENT)
    cls = next(s for s in pf.symbols if s.kind == "class")
    assert cls.signature == "selector:app-root"
    assert component_meta(COMPONENT.decode())[0]["template_url"] == "./app.component.html"


def test_an_external_template_is_its_modules_unit_and_binds_by_selector_and_method() -> None:
    assert detect_by_extension("src/app/app.component.html") == "angular_template"
    pf = parse_file("src/app/app.component.html", TEMPLATE)
    assert [(s.kind, s.qualified_name, s.start_line, s.end_line) for s in pf.symbols] == [
        ("function", "src.app.app.component::template", 1, 4),
    ]
    assert (
        owner_class("src/app/app.component.html", COMPONENT.decode())
        == "src.app.app.component::AppComponent"
    )
    sites = template_sites(TEMPLATE.decode())
    assert (1, "app-layout-header", "selector") in sites and (
        2,
        "router-outlet",
        "selector",
    ) in sites
    assert (
        (3, "save", "handler") in sites
        and (3, "fmt", "handler") in sites
        and (4, "articles", "handler") in sites
    )
    assert [n for _, n, _ in template_sites('{{ $any(x) }}<a *ngIf="a.$emit(x)">')] == [
        "$any",
        "$emit",
    ]
    selectors = {
        "app-layout-header": ("h1", "src.app.core.layout.header.component::HeaderComponent")
    }
    methods = {
        "save": [("m1", "src.app.app.component::AppComponent::save")],
        "fmt": [("m2", "src.app.app.component::AppComponent::fmt")],
        "articles": [],
    }
    edges = template_edges(pf, sites, pf.symbols[0], selectors, methods)
    got = {(e.to_qualified_name.split("::")[-1], e.line, e.confidence) for e in edges}
    assert got == {
        ("HeaderComponent", 1, 1.0),
        ("save", 3, 1.0),
        ("fmt", 3, 1.0),
    }  # router-outlet: no declared selector


def test_an_inline_template_attributes_to_its_class() -> None:
    src = (
        b"import { Component } from '@angular/core';\n@Component({\n  selector: 'app-tag',\n"
        b'  template: `<span (click)="pick()"><app-icon /></span>`,\n})\nexport class TagComponent { pick() {} }\n'
    )
    pf = parse_file("src/app/tag.component.ts", src)
    sites = inline_template_sites(pf)
    assert (4, "pick", "handler", "src.app.tag.component::TagComponent") in sites
    assert (4, "app-icon", "selector", "src.app.tag.component::TagComponent") in sites


def test_an_empty_manifest_read_is_not_cached() -> None:
    import asyncio

    from server.code.app.index import ingest_resolve

    class _Repo:
        texts: dict[str, str] = {}

        async def live_text_for_path(self, org, ns, path):
            return self.texts.get(path, "")

    repo = _Repo()
    key = ("o", "n", "src/app/app.component.ts", ingest_resolve.PACKAGE_JSON_MAX_BYTES)
    ingest_resolve._PACKAGE_JSON_CACHE.pop(key)
    assert (
        asyncio.run(ingest_resolve._package_json_text(repo, "o", "n", "src/app/app.component.ts"))
        == ""
    )  # type: ignore[arg-type]
    repo.texts["src/app/app.component.ts"] = (
        "@Component({selector: 'app-root'}) export class AppComponent {}"
    )
    assert asyncio.run(
        ingest_resolve._package_json_text(repo, "o", "n", "src/app/app.component.ts")
    ).startswith("@Component")  # type: ignore[arg-type]


def test_an_empty_workspace_registry_is_not_cached() -> None:
    import asyncio

    from server.code.app.index import ingest_resolve

    class _Repo:
        texts: dict[str, str] = {}

        async def live_text_for_path(self, org, ns, path):
            return self.texts.get(path, "")

    repo = _Repo()
    ingest_resolve._LIVE_PATHS_CACHE[("o", "w")] = (
        float("inf"),
        {"pnpm-workspace.yaml", "packages/cli/package.json"},
    )
    ingest_resolve._WORKSPACE_CACHE.pop(("o", "w"))
    present = ingest_resolve._LIVE_PATHS_CACHE[("o", "w")][1]
    assert asyncio.run(ingest_resolve._workspace_registry(repo, "o", "w", present)) == {}  # type: ignore[arg-type]
    repo.texts.update(
        {
            "pnpm-workspace.yaml": "packages:\n  - packages/*\n",
            "packages/cli/package.json": '{"name": "cli"}',
        }
    )
    ingest_resolve._PACKAGE_JSON_CACHE.clear()
    assert asyncio.run(ingest_resolve._workspace_registry(repo, "o", "w", present)) == {
        "cli": "packages/cli"
    }  # type: ignore[arg-type]
    ingest_resolve._LIVE_PATHS_CACHE.pop(("o", "w"), None)
    ingest_resolve._WORKSPACE_CACHE.pop(("o", "w"))


def test_a_template_named_outside_the_cli_convention_finds_its_owner_by_template_url() -> None:
    from server.code.domain.extract.lang_angular import owner_class_from

    src = "@Component({ selector: 'app-list', templateUrl: './list-view.html' })\nexport class ListComponent {}\n"
    assert (
        owner_class_from("src/app/list/list-view.html", "src/app/list/list.ts", src)
        == "src.app.list.list::ListComponent"
    )
    assert owner_class_from("src/app/list/other.html", "src/app/list/list.ts", src) is None
