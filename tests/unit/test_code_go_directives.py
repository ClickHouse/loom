"""Go directives, struct tags and framework markers — the
`//go:embed` variable reads the files its patterns name, `//go:generate`,
struct field tags, cobra commands and kubebuilder markers are facet rows."""

from __future__ import annotations

from server.code.domain.extract.edges_treesitter import Edge, extract_edges_and_misses
from server.code.domain.extract.go_directives import (
    GO_FACET_KINDS,
    embed_edges,
    embed_targets,
    go_directives,
    has_go_facets,
)
from server.code.domain.extract.lang_go import parse_go
from server.code.domain.extract.treesitter import file_symbol_id

SRC = b"""package server

import (
    "embed"
    "github.com/spf13/cobra"
)

//go:embed templates/*.tmpl static
var assets embed.FS

//go:embed version.txt
var version string

//go:generate protoc --go_out=. foo.proto
//go:generate go run gen.go
//go:generate $GOPATH/bin/mockgen -source=store.go

// +kubebuilder:object:root=true
// +kubebuilder:rbac:groups=core,resources=pods,verbs=get;list
type Thing struct {
    ID    string `json:"id" yaml:"id,omitempty"`
    Name  string `json:"name" db:"name_col"`
    Skip  string `json:"-"`
    Wire  string `protobuf:"bytes,1,opt,name=wire" json:"wire,omitempty"`
    Inner struct{ X int `json:"x"` }
}

var serveCmd = &cobra.Command{
    Use:   "serve [flags]",
    Short: "Run the server",
}

func plain() {}
"""


def test_directives_and_facets() -> None:
    pf = parse_go("cmd/server/server.go", SRC)
    d = go_directives(pf)
    assert [(e.var_qualified_name, e.patterns) for e in d.embeds] == [
        ("cmd.server.server::assets", ["templates/*.tmpl", "static"]),
        ("cmd.server.server::version", ["version.txt"]),
    ]
    facets = {(f.kind, f.name) for f in d.facets}
    assert {("embed", "templates/*.tmpl"), ("embed", "static"), ("embed", "version.txt")} <= facets
    assert {("generate", "protoc"), ("generate", "go run"), ("generate", "mockgen")} <= facets
    assert {
        ("marker", "kubebuilder:object"),
        ("marker", "kubebuilder:rbac"),
        ("crd", "Thing"),
    } <= facets
    assert {
        ("field_tag", "json:id"),
        ("field_tag", "yaml:id"),
        ("field_tag", "json:name"),
        ("field_tag", "db:name_col"),
        ("field_tag", "json:wire"),
    } <= facets
    assert ("field_tag", "json:-") not in facets, "an omitted field names nothing"
    assert not [n for k, n in facets if k == "field_tag" and n.startswith("protobuf:")]
    assert ("command", "serve") in facets
    assert {f.kind for f in d.facets} <= GO_FACET_KINDS
    # one row per line: two json keys on one line stay distinct by name, the
    # ordinal is the line so the same key on two structs never collides
    assert all(f.ordinal == f.line for f in d.facets)
    assert has_go_facets(pf)
    assert not has_go_facets(parse_go("x.go", b"package x\nfunc f() {}\n"))


def test_embed_targets_follow_go_rules() -> None:
    live = [
        "cmd/server/templates/a.tmpl",
        "cmd/server/templates/b.txt",
        "cmd/server/templates/_partial.tmpl",
        "cmd/server/static/js/app.js",
        "cmd/server/static/.hidden",
        "cmd/server/server.go",
        "cmd/other/x.tmpl",
        "cmd/server/version.txt",
    ]
    # a glob names `_partial.tmpl` explicitly (Go skips `.`/`_` names only
    # when it walks a directory), a directory walk skips `.hidden`
    assert embed_targets("cmd/server", ["templates/*.tmpl"], live) == [
        "cmd/server/templates/_partial.tmpl",
        "cmd/server/templates/a.tmpl",
    ]
    assert embed_targets("cmd/server", ["static"], live) == ["cmd/server/static/js/app.js"]
    assert embed_targets("cmd/server", ["all:static"], live) == [
        "cmd/server/static/.hidden",
        "cmd/server/static/js/app.js",
    ]
    assert embed_targets("cmd/server", ["version.txt"], live) == ["cmd/server/version.txt"]
    assert embed_targets("", ["cmd/*/x.tmpl"], live) == ["cmd/other/x.tmpl"]


def test_reads_edges_from_the_variable_to_each_file() -> None:
    pf = parse_go("cmd/server/server.go", SRC)
    live = ["cmd/server/templates/a.tmpl", "cmd/server/static/js/app.js", "cmd/server/version.txt"]
    edges = embed_edges(pf, go_directives(pf), live, Edge)
    got = {(e.from_qualified_name, e.to_qualified_name, e.edge_kind, e.confidence) for e in edges}
    assert got == {
        ("cmd.server.server::assets", "cmd/server/templates/a.tmpl", "reads", 1.0),
        ("cmd.server.server::assets", "cmd/server/static/js/app.js", "reads", 1.0),
        ("cmd.server.server::version", "cmd/server/version.txt", "reads", 1.0),
    }
    assert {e.to_symbol_id for e in edges} == {file_symbol_id(p) for p in live}
    through, _m = extract_edges_and_misses(pf, {}, lane_context={"go_embed_paths": live})
    assert {e.to_qualified_name for e in through if e.edge_kind == "reads"} == set(live)
    assert not [e for e in extract_edges_and_misses(pf, {})[0] if e.edge_kind == "reads"]
