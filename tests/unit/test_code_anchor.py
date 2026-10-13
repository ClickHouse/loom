"""Code anchors: which names a memory's text mentions, which one definition each
resolves to, and where a link goes when the code moves, changes or goes away."""

from __future__ import annotations

from dataclasses import replace

from server.code.domain.anchor import (
    Definition,
    Link,
    fingerprint,
    local_name,
    mentions,
    recheck,
    resolve,
)


def _d(path: str, qn: str, body: str = "", line: int = 1) -> Definition:
    name = qn.replace("::", ".").split(".")[-1]
    return Definition(path=path, qualified_name=qn, name=name, start_line=line, body_hash=body)


def _names(text: str) -> list[str]:
    return [m.name for m in mentions(text)]


def test_mentions_keep_code_shapes_and_skip_prose() -> None:
    text = (
        "`Datapack.recall` scopes reads; MergeTreeReader::cancel leaks when "
        "read_rows_limit is hit. Call client.search(query) or flush() — the "
        "result was fine and the users were happy."
    )
    found = _names(text)
    assert found[:2] == ["Datapack.recall", "MergeTreeReader::cancel"]
    assert "read_rows_limit" in found and "client.search" in found and "flush" in found
    assert not {"result", "users", "fine", "happy"} & set(found)


def test_a_part_of_a_qualified_name_is_not_a_mention_of_its_own() -> None:
    assert _names("`Missing.read_rows` and Other::fetch_page") == [
        "Missing.read_rows",
        "Other::fetch_page",
    ]


def test_mentions_drop_short_names_and_repeat_once() -> None:
    assert _names("`get` and `getValue` and `getValue`") == ["getValue"]


def test_resolve_prefers_the_definition_in_the_memory_files() -> None:
    defs = [_d("src/a.py", "a::Reader::read"), _d("src/b.py", "b::Reader::read")]
    assert resolve("read", defs, ["src/b.py"]) == defs[1]
    assert resolve("Reader.read", defs, ["b.py"]) == defs[1]


def test_resolve_skips_a_homonym_it_cannot_tell_apart() -> None:
    defs = [_d("src/a.py", "a::Reader::read"), _d("src/b.py", "b::Reader::read")]
    assert resolve("read", defs, []) is None
    assert resolve("a::Reader::read", defs, []) == defs[0]


def test_resolve_takes_overloads_as_one_target() -> None:
    defs = [_d("src/io.cpp", "DB::write", line=30), _d("src/io.cpp", "DB::write", line=10)]
    assert resolve("DB::write", defs, []) == defs[1]


def test_recheck_follows_a_file_move_and_flags_a_changed_body() -> None:
    link = Link(path="src/old.py", qualified_name="src.old.Runner.run", body_hash="h1")
    here = [_d("src/new.py", "src.new.Runner.run", "h1")]
    moved = recheck(link, path_now="src/new.py", here=here, head="c2")
    assert (moved.path, moved.qualified_name, moved.state) == (
        "src/new.py",
        "src.new.Runner.run",
        "moved",
    )
    edited = [_d("src/old.py", "src.old.Runner.run", "h2")]
    changed = recheck(link, path_now="src/old.py", here=edited, head="c2")
    assert (changed.state, changed.changed_since, changed.body_hash) == ("changed", "c2", "h1")
    again = [_d("src/old.py", "src.old.Runner.run", "h3")]
    assert recheck(changed, path_now="src/old.py", here=again, head="c3").changed_since == "c2"
    reverted = [_d("src/old.py", "src.old.Runner.run", "h1")]
    back = recheck(changed, path_now="src/old.py", here=reverted, head="c4")
    assert (back.state, back.changed_since) == ("fresh", "")


def test_a_file_move_keeps_the_enclosing_scope() -> None:
    link = Link(path="src/old.py", qualified_name="src.old.Writer.run", body_hash="h1")
    here = [_d("src/new.py", "src.new.Reader.run", "h1")]
    assert recheck(link, path_now="src/new.py", here=here, head="c2").state == "missing"


def test_a_namesake_in_the_same_file_does_not_inherit_the_link() -> None:
    link = Link(path="src/io.py", qualified_name="src.io.Writer.read_rows", body_hash="h1")
    here = [_d("src/io.py", "src.io.Reader.read_rows", "h1")]
    after = recheck(link, path_now="src/io.py", here=here, head="c2")
    assert (after.state, after.qualified_name) == ("missing", "src.io.Writer.read_rows")


def test_recheck_follows_a_rename_the_caller_found_by_shape() -> None:
    link = Link(
        path="src/a.py", qualified_name="src.a.fetch_rows", body_hash="h1", checked_hash="h1"
    )
    here = [_d("src/a.py", "src.a.load_rows", "h2"), _d("src/a.py", "src.a.other", "h9")]
    assert recheck(link, path_now="src/a.py", here=here, head="c2").state == "missing"
    after = recheck(link, path_now="src/a.py", here=here, renamed=here[0], head="c2")
    assert (after.qualified_name, after.state, after.body_hash) == (
        "src.a.load_rows",
        "moved",
        "h2",
    )
    # matched loosely (the name is used another way inside): followed, but as changed,
    # its baseline kept
    loose = recheck(
        link, path_now="src/a.py", here=here, renamed=here[0], renamed_exactly=False, head="c2"
    )
    assert (loose.qualified_name, loose.state, loose.body_hash) == (
        "src.a.load_rows",
        "changed",
        "h1",
    )


def test_a_fingerprint_covers_every_overload_in_order_and_every_character() -> None:
    assert fingerprint(["a", "b"]) != fingerprint(["a", "c"]) != fingerprint(["b", "a"])
    inside = "def f(x):\n    if x:\n        return 1\n        return 2"
    after = "def f(x):\n    if x:\n        return 1\n    return 2"
    assert fingerprint([inside]) != fingerprint([after])  # indentation is behaviour
    assert fingerprint(['def f():\n    return "a b"']) != fingerprint(
        ['def f():\n    return "a  b"']
    )
    assert fingerprint(["def f():\r\n    pass"]) == fingerprint(["def f():\n    pass"])
    assert fingerprint([]) == ""


def test_the_local_name_drops_only_what_the_path_makes() -> None:
    assert local_name("server.memory.links.Reader.run", "server/memory/links.py") == "Reader.run"
    assert local_name("svc.handler.Serve", "svc/handler/serve.go") == "Serve"
    assert local_name("DB::Reader::read", "src/Reader.cpp") == "DB.Reader.read"
    assert local_name("io::fetch_rows", "io.cpp") == "io.fetch_rows"
    assert local_name("io::fetch_rows", "io/reader.cpp") == "io.fetch_rows"
    assert local_name("src.Sample::Sample.fetch_rows", "src/Sample.java") == "Sample.fetch_rows"


def test_recheck_marks_removed_code_missing_and_finds_it_again() -> None:
    link = Link(path="src/a.py", qualified_name="src.a.fetch", body_hash="h1")
    gone = recheck(link, path_now="", head="c2")
    assert (gone.state, gone.path, gone.changed_since) == ("missing", "src/a.py", "c2")
    here = [_d("src/a.py", "src.a.fetch", "h1")]
    assert recheck(gone, path_now="src/a.py", here=here, head="c3").state == "fresh"


def test_recheck_finds_a_move_git_did_not_pair() -> None:
    link = Link(path="src/a.cpp", qualified_name="DB::Thing::run", body_hash="h1")
    there = [_d("src/b.cpp", "DB::Thing::run", "h1")]
    after = recheck(link, path_now="", elsewhere=there, head="c2")
    assert (after.path, after.state) == ("src/b.cpp", "moved")
    twice = [*there, _d("src/c.cpp", "DB::Thing::run", "h1")]
    assert recheck(link, path_now="", elsewhere=twice, head="c2").state == "missing"


def test_a_c_namesake_with_other_text_in_another_file_is_not_the_removed_one() -> None:
    # C and C++ names carry no path: two files' static helpers share one
    link = Link(path="src/a.cpp", qualified_name="fetch_rows", body_hash="h1")
    theirs = [_d("tools/b.cpp", "fetch_rows", "h9")]
    assert recheck(link, path_now="", elsewhere=theirs, head="c2").state == "missing"


def test_recheck_of_a_file_link() -> None:
    link = Link(path="docs/a.md")
    assert recheck(link, path_now="docs/b.md", head="c2").path == "docs/b.md"
    assert recheck(link, path_now="", head="c2").state == "missing"
    assert recheck(link, path_now="docs/a.md", head="c2").state == "fresh"


def test_a_rename_keeps_an_earlier_change_and_rebaselines_only_a_current_link() -> None:
    renamed = _d("src/a.py", "src.a.load_rows", "h3")
    changed = Link(
        "src/a.py", "src.a.fetch_rows", body_hash="h1", state="changed", checked_hash="h2"
    )
    after = recheck(changed, path_now="src/a.py", here=[renamed], renamed=renamed, head="c3")
    assert (after.qualified_name, after.state, after.body_hash, after.checked_hash) == (
        "src.a.load_rows",
        "changed",
        "h1",  # the claim was written against h1: a rename does not confirm it
        "h3",
    )
    fresh = Link("src/a.py", "src.a.fetch_rows", body_hash="h1", checked_hash="h1")
    after = recheck(fresh, path_now="src/a.py", here=[renamed], renamed=renamed, head="c3")
    assert (after.state, after.body_hash) == ("moved", "h3")


def test_an_edit_to_changed_code_moves_the_checked_fingerprint() -> None:
    link = Link("src/a.py", "src.a.f", body_hash="h1", state="changed", checked_hash="h2")
    after = recheck(link, path_now="src/a.py", here=[_d("src/a.py", "src.a.f", "h3")], head="c3")
    assert (after.state, after.checked_hash) == ("changed", "h3") and after != link


def test_an_explicitly_qualified_short_name_is_a_mention() -> None:
    assert _names("`Reader.get` returns None, and `DB::Pool::put` blocks") == [
        "Reader.get",
        "DB::Pool::put",
    ]
    assert _names("call get() then put()") == []


def test_a_go_or_rust_definition_keeps_its_local_name_across_a_directory_move() -> None:
    # Go: the package goes, directory and name (a move may rename it); Rust: the crate
    # and module path go
    for a, b in (("reader.go", "sub/reader.go"), ("sub/reader.go", "reader.go")):
        assert local_name(_go_name(a), a) == local_name(_go_name(b), b) == "fetch_rows"
    assert local_name("internal.rows.rows::R::Get", "internal/rows/r.go") == "R.Get"
    assert local_name("crate::reader::R::read", "reader.rs") == "R.read"
    assert local_name("sub::reader::R::read", "sub/reader.rs") == "R.read"
    assert local_name("crate::a::fetch_rows", "src/a/mod.rs") == "fetch_rows"


def _go_name(path: str) -> str:
    directory = path.rsplit("/", 1)[0] if "/" in path else "root"
    return f"{directory.replace('/', '.')}.reader::fetch_rows"


def test_code_not_found_where_a_successor_may_be_is_unresolved_not_missing() -> None:
    link = Link("src/a.py", "a::fetch_rows", "h1", checked_hash="h1")
    here = [_d("src/a.py", "a::load_rows", "h2")]
    assert recheck(link, path_now="src/a.py", here=here, uncertain=True, head="c2").state == (
        "unresolved"
    )
    assert recheck(link, path_now="src/a.py", here=here, head="c2").state == "missing"


def test_a_bare_word_of_the_prose_links_only_inside_the_memorys_files() -> None:
    text = "Merge PRs with the GitHub CLI; `TokenCache` and GitHub.open() are code."
    assert [(m.name, m.in_prose) for m in mentions(text)] == [
        ("TokenCache", False),
        ("GitHub.open", False),
        ("GitHub", True),
    ]
    defs = [_d("ci/github_helper.py", "ci.github_helper::GitHub")]
    assert resolve("GitHub", defs, [], in_prose=True) is None
    assert resolve("GitHub", defs, ["ci/github_helper.py"], in_prose=True) == defs[0]
    assert resolve("GitHub", defs, []) == defs[0]


def test_a_definition_moved_unchanged_into_another_module_is_found_elsewhere() -> None:
    link = Link("pkg/a.py", "pkg.a::fetch_rows", "h1", checked_hash="h1")
    moved = _d("pkg/b.py", "pkg.b::fetch_rows", "h1")
    after = recheck(link, path_now="pkg/a.py", here=[], elsewhere=[moved], head="c2")
    assert (after.state, after.path, after.qualified_name) == (
        "moved",
        "pkg/b.py",
        "pkg.b::fetch_rows",
    )
    # another text, or two candidates, is not it
    edited = _d("pkg/b.py", "pkg.b::fetch_rows", "h2")
    assert (
        recheck(link, path_now="pkg/a.py", here=[], elsewhere=[edited], head="c2").state
        == "missing"
    )
    twin = _d("pkg/c.py", "pkg.c::fetch_rows", "h1")
    assert (
        recheck(link, path_now="pkg/a.py", here=[], elsewhere=[moved, twin], head="c2").state
        == "missing"
    )


def test_a_change_survives_a_stop_in_unresolved_or_missing() -> None:
    # the memory said `fetch_rows` returns 1; it now returns 2: changed
    link = Link("src/m.py", "m::fetch_rows", "h1", checked_hash="h1")
    edited = recheck(
        link, path_now="src/m.py", here=[_d("src/m.py", "m::fetch_rows", "h2")], head="c1"
    )
    assert edited.state == "changed"
    # renamed beside a near-twin: not yet sure which is it
    unsure = recheck(edited, path_now="src/m.py", here=[], uncertain=True, head="c2")
    assert unsure.state == "unresolved"
    # the twin goes: an exact rename of the edited code is still a change to the claim
    renamed = _d("src/m.py", "m::load_rows", "h3")
    after = recheck(unsure, path_now="src/m.py", here=[renamed], renamed=renamed, head="c3")
    assert (after.state, after.qualified_name) == ("changed", "m::load_rows")
    # and gone, then back as it was edited: still changed
    gone = recheck(edited, path_now="src/m.py", here=[], head="c2")
    back = recheck(
        gone, path_now="src/m.py", here=[_d("src/m.py", "m::fetch_rows", "h2")], head="c3"
    )
    assert (gone.state, back.state) == ("missing", "changed")


def test_moved_is_where_the_code_is_against_where_the_memory_put_it() -> None:
    # the memory was written about a::f in a.py; the code now lives as b::f in b.py
    link = Link("b.py", "b::f", "h1", "moved", "", 1, 2, checked_hash="h1")
    link = replace(link, anchored_path="a.py", anchored_name="a::f")
    here = [_d("b.py", "b::f", "h1"), _d("b.py", "b::g", "x")]
    assert recheck(link, path_now="b.py", here=here, head="h3").state == "moved"
    # edited, then reverted: moved again, not fresh
    edited = recheck(link, path_now="b.py", here=[_d("b.py", "b::f", "h2")], head="h4")
    reverted = recheck(edited, path_now="b.py", here=here, head="h5")
    assert (edited.state, reverted.state) == ("changed", "moved")
    # removed, then restored there: moved again
    gone = recheck(link, path_now="b.py", here=[_d("b.py", "b::g", "x")], head="h4")
    back = recheck(gone, path_now="b.py", here=here, head="h5")
    assert (gone.state, back.state) == ("missing", "moved")
    # moved back to where the memory says it is: fresh
    home = recheck(link, path_now="a.py", here=[_d("a.py", "a::f", "h1")], head="h6")
    assert (home.state, home.path) == ("fresh", "a.py")
    moved_file = replace(Link("b.py", state="moved"), anchored_path="a.py")
    assert recheck(moved_file, path_now="b.py", head="h3").state == "moved"


def test_reading_mentions_is_linear_in_the_text() -> None:
    from tests.unit.step_count import steps

    def work(n: int) -> int:
        text = " ".join(f"read_rows_{i} and A::b{i}" for i in range(n))
        return steps(lambda: mentions(text, cap=n))

    assert work(1600) / work(200) < 12


def test_a_method_written_with_its_receiver_names_the_method() -> None:
    assert _names("calling self.read_rows() twice leaks; this.fetchRows() is hot") == [
        "read_rows",
        "fetchRows",
    ]
    defs = [_d("src/r.py", "src.r::Reader::read_rows")]
    assert resolve(_names("`cls.read_rows` is hot")[0], defs, ["src/r.py"]) == defs[0]


def test_a_go_package_renamed_in_place_keeps_the_link() -> None:
    link = Link(path="pkg/x.go", qualified_name="pkg.reader::B::Read", body_hash="h1")
    here = [_d("pkg/x.go", "pkg.rows::B::Read", "h2")]
    after = recheck(link, path_now="pkg/x.go", here=here, head="c2")
    assert (after.state, after.qualified_name) == ("changed", "pkg.rows::B::Read")
