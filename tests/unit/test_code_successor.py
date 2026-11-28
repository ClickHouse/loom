"""What a linked definition became once its name is gone: a rename alone, a rename with
an edit, a method of a renamed class; uncertain when a new definition may be it and
none certainly is, and nothing when the code was removed."""

from __future__ import annotations

import pytest

from server.code.app.memory_links import file_definitions
from server.code.domain.anchor import Link, local_name, locate, recheck, unnumbered
from server.code.domain.successor import _MAX_LINES, Side, elsewhere, own_lines, successor
from tests.unit.step_count import steps as count


def _side(path: str, src: str) -> Side:
    return Side(path, file_definitions(path, src.encode()), src.splitlines())


def _after(before: tuple[str, str], after: tuple[str, str], name: str = "fetch_rows") -> Link:
    old, new = _side(*before), _side(*after)
    was = next(d for d in old.defs if d.name == name)
    link = Link(old.path, was.qualified_name, was.body_hash, checked_hash=was.body_hash)
    if locate(link, new.path, new.defs) is not None:
        return recheck(link, path_now=new.path, here=new.defs, head="c2")
    s = successor(was, old, new)
    return recheck(
        link,
        path_now=new.path,
        here=new.defs,
        renamed=s.definition,
        renamed_exactly=s.exact,
        uncertain=s.uncertain,
        carried=s.carried,
        head="c2",
    )


_FILES = {
    "reader.py": "def other():\n    return 0\n\n\ndef NAME():\n    return RV\n",
    "reader.c": "int other(void) { return 0; }\n\nint NAME(void) { return RV; }\n",
    "reader.cpp": "int other() { return 0; }\n\nint NAME() { return RV; }\n",
    "reader.ts": "export function other(): number { return 0; }\n\n"
    "export function NAME(): number { return RV; }\n",
    "reader.js": "export function other() { return 0; }\n\nexport function NAME() { return RV; }\n",
    "reader.go": "package reader\n\nfunc other() int { return 0 }\n\nfunc NAME() int { return RV }\n",
    "reader.rs": "pub fn other() -> u32 { 0 }\n\npub fn NAME() -> u32 { RV }\n",
    "Reader.java": "class Reader {\n  int other() { return 0; }\n  int NAME() { return RV; }\n}\n",
}


@pytest.mark.parametrize("path", sorted(_FILES))
def test_a_rename_with_an_edit_follows_the_successor_as_changed(path: str) -> None:
    src = _FILES[path]
    before = src.replace("NAME", "fetch_rows").replace("RV", "1")
    after = _after((path, before), (path, src.replace("NAME", "load_rows").replace("RV", "2")))
    assert after.state == "changed"
    assert after.qualified_name.replace("::", ".").endswith("load_rows")
    assert after.body_hash != after.checked_hash  # the baseline stays: the claim needs a look


def test_a_rename_that_moves_a_line_into_a_block_is_changed() -> None:
    before = "def fetch_rows(flag):\n    if flag:\n        return 3\n    return 4\n"
    after = "def load_rows(flag):\n    if flag:\n        return 3\n        return 4\n"
    assert _after(("reader.py", before), ("reader.py", after)).state == "changed"


@pytest.mark.parametrize(
    ("src", "a", "b"),
    [
        ("package reader\n\nfunc fetch_rows() int { return 1 }\n", "reader.go", "sub/reader.go"),
        ("package reader\n\nfunc fetch_rows() int { return 1 }\n", "sub/reader.go", "reader.go"),
        ("pub fn fetch_rows() -> u32 { 1 }\n", "reader.rs", "sub/reader.rs"),
        ("pub fn fetch_rows() -> u32 { 1 }\n", "sub/reader.rs", "reader.rs"),
    ],
)
def test_a_go_or_rust_file_moved_into_another_directory_is_moved(src: str, a: str, b: str) -> None:
    after = _after((a, src), (b, src))
    assert (after.state, after.path) == ("moved", b)


def test_a_method_follows_its_renamed_class() -> None:
    def program(cls: str) -> str:
        return (
            f"class {cls}:\n    def fetch_rows(self):\n        return 1\n\n"
            "    def close(self):\n        pass\n\n\nclass Writer:\n    def flush(self):\n"
            "        pass\n"
        )

    after = _after(("reader.py", program("Reader")), ("reader.py", program("Loader")))
    assert (after.state, after.qualified_name) == ("moved", "reader::Loader::fetch_rows")


def test_a_method_of_a_removed_class_never_moves_to_a_namesake_in_another() -> None:
    before = (
        "class Reader:\n    def fetch_rows(self):\n        return 1\n\n    def close(self):\n"
        "        pass\n\n\nclass Writer:\n    def flush(self):\n        pass\n"
    )
    after = "class Writer:\n    def flush(self):\n        pass\n\n    def fetch_rows(self):\n        return 1\n"
    assert _after(("reader.py", before), ("reader.py", after)).state == "missing"
    # nor by the search across scopes: a method this small is any class's
    assert _elsewhere({"reader.py": before}, {"reader.py": after}, "Reader.fetch_rows").state == (
        "missing"
    )


_KEPT = "def other():\n    return 0\n"
_GONE = _KEPT + "\n\ndef fetch_rows(data):\n    return sorted(data, key=len)[:10]\n"


@pytest.mark.parametrize(
    ("after", "state"),
    [
        # removed, nothing new in its place: the code is gone
        (_KEPT, "missing"),
        # removed beside an unrelated addition: no transfer, and not called gone either
        (_KEPT + "\n\ndef warm_up(host, port):\n    connect(host, port).ping()\n", "unresolved"),
        # two close candidates: none is named
        (
            _KEPT + "\n\ndef load_rows(data):\n    return sorted(data, key=len)[:20]\n\n\n"
            "def read_rows(data):\n    return sorted(data, key=len)[:30]\n",
            "unresolved",
        ),
    ],
)
def test_a_successor_is_named_only_on_corroborated_evidence(after: str, state: str) -> None:
    link = _after(("reader.py", _GONE), ("reader.py", after))
    assert (link.state, link.qualified_name) == (state, "reader::fetch_rows")


@pytest.mark.parametrize(
    ("after", "uncertain", "unmatched"),
    [
        # nothing new in its scope: removed, with no doubt
        (_KEPT, False, False),
        # new definitions there, none close to it: it is not among them
        (_KEPT + '\n\n__all__ = ["other"]\n', True, True),
        (_KEPT + "\n\ndef warm_up(host, port):\n    connect(host, port).ping()\n", True, True),
        # two close candidates: it may be either
        (
            _KEPT + "\n\ndef load_rows(data):\n    return sorted(data, key=len)[:20]\n\n\n"
            "def read_rows(data):\n    return sorted(data, key=len)[:30]\n",
            True,
            False,
        ),
        # a small definition with its text under an unrelated name: not it by the name
        # rule, but its shape is the same, so it may be
        (_KEPT + "\n\ndef warm_up(data):\n    return sorted(data, key=len)[:10]\n", True, False),
        # the same with an edit: its text is close, and an unrelated name rules nothing out
        (_KEPT + "\n\ndef warm_up(data):\n    return sorted(data, key=len)[:20]\n", True, False),
        # a new name declared twice, a stub before the close text: every declaration counts
        (
            _KEPT + "\n\ndef warm_up(data):\n    raise NotImplementedError\n\n\n"
            "def warm_up(data):\n    return sorted(data, key=len)[:20]\n",
            True,
            False,
        ),
    ],
)
def test_new_definitions_that_are_not_close_leave_the_old_one_unmatched(
    after: str, uncertain: bool, unmatched: bool
) -> None:
    old, new = _side("reader.py", _GONE), _side("reader.py", after)
    was = next(d for d in old.defs if d.name == "fetch_rows")
    found = successor(was, old, new)
    assert (found.definition, found.uncertain, found.unmatched) == (None, uncertain, unmatched)


def test_a_namespace_renamed_with_its_file_is_not_a_renamed_class() -> None:
    # namespaces own their members: sharing names proves nothing about an unchanged move
    def program(ns: str, limit: int) -> str:
        return f"namespace {ns} {{\nconst int limit = {limit};\nint fetch_rows() {{ return limit; }}\n}}\n"

    after = _after(("io.cpp", program("io", 3)), ("db.cpp", program("db", 4)))
    assert after.state != "moved"


def test_a_comparison_past_its_bound_is_unsure_not_a_match() -> None:
    body = "".join(f"    x = (x + {i % 3}) * (x - {i % 5})\n" for i in range(_MAX_LINES + 10))
    before = ("m.py", f"def other():\n    return 0\n\n\ndef fetch_rows(x):\n{body}    return x\n")
    after = ("m.py", f"def other():\n    return 0\n\n\ndef load_rows(x):\n{body}    return x + 1\n")
    assert _after(before, after).state == "unresolved"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "retained = []\ndef fetch_rows(payload):\n    buffer = bytearray(payload)\n"
            "    retained.append(buffer)\n    return len(buffer)\n",
            "retained = []\ndef load_rows(payload):\n    audit_count = 0\n"
            "    buffer = bytearray(payload)\n    retained.append(buffer)\n    return len(buffer)\n",
        ),
        (
            "def fetch_rows(lock, operation):\n    lock.acquire()\n    result = operation()\n"
            "    lock.release()\n    return result\n",
            "def load_rows(lock, operation):\n    audit_count = 0\n    lock.acquire()\n"
            "    result = operation()\n    lock.release()\n    return result\n",
        ),
    ],
)
def test_a_rename_with_an_added_statement_is_changed(before: str, after: str) -> None:
    link = _after(("reader.py", before), ("reader.py", after))
    assert (link.state, link.qualified_name) == ("changed", "reader::load_rows")


@pytest.mark.parametrize(
    ("path", "src", "old", "new"),
    [
        (
            "a.ts",
            "declare function fetch_rows(): number;\nfunction fetch_rows(): number { return 3; }\n",
            "return 3",
            "return 4",
        ),
        (
            "a.js",
            "function fetch_rows() { return 1; }\nfunction fetch_rows() { return 4; }\n",
            "return 4",
            "return 5",
        ),
    ],
)
def test_a_family_renamed_together_is_moved_only_when_no_member_changed(
    path: str, src: str, old: str, new: str
) -> None:
    renamed = src.replace("fetch_rows", "load_rows")
    assert _after((path, src), (path, renamed)).state == "moved"
    edited = _after((path, src), (path, renamed.replace(old, new)))
    assert (edited.state, edited.qualified_name) == ("changed", f"{path[0]}::load_rows")


def test_a_change_carried_by_a_class_rename_outlives_the_next_commit() -> None:
    def program(cls: str, limit: int, ping: int) -> tuple[str, str]:
        return "r.ts", (
            f"export class {cls} {{\n  limit = {limit};\n"
            "  fetch_rows(): number { return this.limit; }\n"
            f"  ping(): number {{ return {ping}; }}\n}}\n"
        )

    renamed = _after(program("Reader", 3, 1), program("Writer", 4, 1))
    assert (renamed.state, renamed.qualified_name) == ("changed", "r::Writer::fetch_rows")
    old, new = _side(*program("Writer", 4, 1)), _side(*program("Writer", 4, 2))
    # the method's text is the same as before the rename: the class around it is not
    again = recheck(renamed, path_now=new.path, here=new.defs, head="c3")
    assert again.state == "changed" and old.defs


_TINY = "def keep():\n    return 0\n\n\ndef NAME(n):\n    return n EXPR\n"


@pytest.mark.parametrize(
    ("old", "new", "state"),
    [
        ("fetch_rows", "load_rows", "changed"),  # keeps `rows`
        ("fetch_rows", "warm_up", "unresolved"),  # nothing in common
        ("get_rows", "get_user", "unresolved"),  # only the leading verb in common
        ("fetch_rows", "fetch_users", "unresolved"),
        ("list_rows", "list_users", "unresolved"),
        ("readRows", "loadRows", "changed"),  # camelCase keeps `rows` too
        ("fetch", "fetch_all", "changed"),  # grown
        ("count_rows", "count", "changed"),  # shrunk
        ("Reader", "ReaderV2", "changed"),
        ("total_rows", "rows_total", "changed"),  # reordered
        ("test_fetch_rows", "test_fetch_users", "unresolved"),  # only a common prefix
        ("get_user_name", "get_user_email", "unresolved"),
        ("fetch_rows_v1", "fetch_rows_v2", "changed"),  # a version bump
        ("ParseV1", "ParseV2", "changed"),
        ("load_rows_sync", "load_rows_async", "unresolved"),
        ("sha1", "sha256", "unresolved"),  # a bare number is a sibling, not a version
        ("sha256", "blake256", "unresolved"),  # nor is a shared one a kinship
        ("sha", "sha256", "unresolved"),  # nor is growing by a width alone
        ("hash", "hash_128", "unresolved"),
        ("load_rows", "load_rows2", "unresolved"),
        ("toInt32", "toInt64", "unresolved"),
        ("encode_base64", "encode_base32", "unresolved"),
    ],
)
def test_a_small_definition_renamed_and_edited_follows_only_a_related_name(
    old: str, new: str, state: str
) -> None:
    before = _TINY.replace("NAME", old).replace("EXPR", "+ 1")
    after = _TINY.replace("NAME", new).replace("EXPR", "* 2")
    link = _after(("m.py", before), ("m.py", after), name=old)
    assert link.state == state


def test_an_operator_replaced_by_another_is_not_its_successor() -> None:
    def program(op: str) -> str:
        return (
            "struct V {\n  int x;\n  int keep() const { return x; }\n"
            f"  V operator{op}(const V& o) const {{ return V{{x {op} o.x}}; }}\n}};\n"
        )

    link = _after(("v.cpp", program("+")), ("v.cpp", program("-")), name="operator+")
    assert link.state == "unresolved"


_LOOP = (
    "(source):\n    out = []\n    for row in source.rows():\n        if row.valid:\n"
    "            out.append(row.value)\n    return out\n"
)
_TS_LOOP = (
    "(source: any): any[] {\n  const out = [];\n  for (const row of source.rows()) {\n"
    "    if (row.valid) { out.push(row.value); }\n  }\n  return out;\n}\n"
)


@pytest.mark.parametrize(
    ("before", "after", "name"),
    [
        (
            ("reader.py", f"def fetch_rows{_LOOP}\n\ndef fetch_cols{_LOOP}"),
            ("loader.py", f"def fetch_cols{_LOOP}"),
            "fetch_rows",
        ),
        (
            (
                "src/reader.ts",
                f"export function fetchRows{_TS_LOOP}\nexport function fetchCols{_TS_LOOP}",
            ),
            ("src/loader.ts", f"export function fetchCols{_TS_LOOP}"),
            "fetchRows",
        ),
    ],
)
def test_a_definition_deleted_as_its_file_moves_is_not_handed_to_a_sibling(
    before: tuple[str, str], after: tuple[str, str], name: str
) -> None:
    # every name of a moved file carries a new module: only one new inside it is new
    assert _after(before, after, name=name).state == "missing"


def test_a_rename_with_an_edit_follows_across_a_file_move() -> None:
    before = f"def fetch_rows{_LOOP}\n\ndef fetch_cols{_LOOP}"
    edited = _LOOP.replace("row.value)", "row.value + 1)")
    after = f"def load_rows{edited}\n\ndef fetch_cols{_LOOP}"
    link = _after(("reader.py", before), ("loader.py", after))
    assert (link.state, link.qualified_name) == ("changed", "loader::load_rows")


@pytest.mark.parametrize(
    ("before", "after", "taken", "left"),
    [
        # `load_rows` is `count_rows` renamed: `fetch_rows`, removed beside it, gets nothing
        (
            "def fetch_rows():\n    return 1\n\n\ndef count_rows():\n    return 2\n",
            "def load_rows():\n    return 2\n",
            "count_rows",
            "fetch_rows",
        ),
        (
            "class R:\n    def fetch_rows(self):\n        return 1\n\n"
            "    def count_rows(self):\n        return 2\n\n    def keep(self):\n        return 0\n",
            "class R:\n    def load_rows(self):\n        return 2\n\n    def keep(self):\n        return 0\n",
            "count_rows",
            "fetch_rows",
        ),
        # a legacy copy deleted while the current one is renamed
        (
            "def fetch_rows_legacy(n):\n    return n + 1\n\n\ndef fetch_rows(n):\n    return n + 2\n",
            "def load_rows(n):\n    return n + 2\n",
            "fetch_rows",
            "fetch_rows_legacy",
        ),
    ],
)
def test_one_new_definition_is_the_successor_of_one_removed_definition(
    before: str, after: str, taken: str, left: str
) -> None:
    assert _after(("m.py", before), ("m.py", after), name=taken).state == "moved"
    # never handed over; and as the new one is close to it too, unsure rather than gone
    other = _after(("m.py", before), ("m.py", after), name=left)
    assert other.state == "unresolved" and other.qualified_name.endswith(f"::{left}")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # renamed beside a removed sibling with the same body: either may be `load_rows`
        (
            "def fetch_rows():\n    return None\n\n\ndef helper():\n    return None\n\n\n"
            "def keep():\n    return 0\n",
            "def load_rows():\n    return None\n\n\ndef keep():\n    return 0\n",
        ),
        (
            "def fetch_rows():\n    return 1\n\n\ndef fetch_cols():\n    return 1\n",
            "def load_rows():\n    return 1\n\n\ndef load_cols():\n    return 1\n",
        ),
        # renamed and edited into a removed sibling's body: either may be `load_rows`
        (
            "def fetch_rows():\n    return 1\n\n\ndef helper():\n    return 2\n\n\n"
            "def keep():\n    return 0\n",
            "def load_rows():\n    return 2\n\n\ndef keep():\n    return 0\n",
        ),
    ],
)
def test_two_removed_definitions_alike_leave_their_successor_unsure(
    before: str, after: str
) -> None:
    assert _after(("m.py", before), ("m.py", after)).state == "unresolved"


@pytest.mark.parametrize(
    ("before", "after", "state"),
    [
        ((1, 1), (1, 1), "moved"),  # the class renamed alone: its method moved with it
        ((1, 2), (2, 3), "changed"),  # edited to a sibling's old body
        ((1, 2), (2, 2), "changed"),
    ],
)
def test_a_renamed_class_carries_its_method_whatever_its_siblings_were(
    before: tuple[int, int], after: tuple[int, int], state: str
) -> None:
    def program(cls: str, rows: int, cols: int) -> str:
        return (
            f"class {cls}:\n    def fetch_rows(self):\n        return {rows}\n\n"
            f"    def fetch_cols(self):\n        return {cols}\n\n"
            "    def keep(self):\n        return 9\n"
        )

    link = _after(("m.py", program("R", *before)), ("m.py", program("S", *after)))
    assert (link.state, link.qualified_name) == (state, "m::S::fetch_rows")


def test_a_successor_search_is_linear_in_the_file() -> None:
    def work(n: int) -> int:
        def program(name: str, value: int) -> str:
            return "".join(
                f"def {name}_{i}(x):\n    return x + {value} * {i}\n\n\n" for i in range(n)
            )

        before, after = _side("m.py", program("fetch", 1)), _side("m.py", program("load", 2))
        was = next(d for d in before.defs if d.name == "fetch_0")
        return count(lambda: successor(was, before, after) is not None)

    assert work(800) / work(100) < 12


def test_a_method_renamed_as_its_class_is_renamed_is_followed() -> None:
    # `S.fetch_cols` is the sibling carried: it does not compete with `load_rows`
    before = (
        "class R:\n    def fetch_rows(self):\n        return 1\n\n"
        "    def fetch_cols(self):\n        return 1\n\n    def keep(self):\n        return 9\n"
    )
    after = before.replace("def fetch_rows", "def load_rows").replace("class R", "class S")
    link = _after(("m.py", before), ("m.py", after))
    assert (link.state, link.qualified_name) == ("changed", "m::S::load_rows")


def test_another_class_losing_a_namesake_does_not_block_a_renamed_class() -> None:
    before = (
        "class R:\n    def fetch_rows(self):\n        return 1\n\n    def keep(self):\n"
        "        return 9\n\n    def extra(self):\n        return 8\n\n\n"
        "class Q:\n    def load_rows(self):\n        return 5\n\n    def other(self):\n"
        "        return 6\n"
    )
    after = (
        before.replace("class R", "class S")
        .replace(
            "def fetch_rows(self):\n        return 1", "def load_rows(self):\n        return 1", 1
        )
        .replace("def load_rows(self):\n        return 5", "def read_all(self):\n        return 5")
    )
    link = _after(("m.py", before), ("m.py", after))
    assert (link.state, link.qualified_name) == ("changed", "m::S::load_rows")


def test_a_method_removed_as_its_class_is_renamed_is_missing() -> None:
    # `S.fetch_rows_all` is the sibling carried, not a candidate for `fetch_rows`
    before = (
        "class R:\n    def fetch_rows(self):\n        return 1\n\n"
        "    def fetch_rows_all(self):\n        return 2\n\n    def keep(self):\n        return 9\n"
    )
    after = (
        "class S:\n    def fetch_rows_all(self):\n        return 2\n\n"
        "    def keep(self):\n        return 9\n"
    )
    assert _after(("m.py", before), ("m.py", after)).state == "missing"


@pytest.mark.parametrize(
    ("path", "before", "after", "name"),
    [
        (
            "m.py",
            "class Conn:\n    def is_ready(self):\n        return True\n\n"
            "    def keep(self):\n        return 0\n",
            "class Conn:\n    def keep(self):\n        return 0\n\n"
            "    def supports_tls(self):\n        return True\n",
            "is_ready",
        ),
        (
            "m.c",
            "int keep(void) { return 0; }\nint is_ready() { return 1; }\n",
            "int keep(void) { return 0; }\nint max_retries() { return 1; }\n",
            "is_ready",
        ),
    ],
)
def test_a_small_definition_is_not_handed_to_an_unrelated_one_with_its_shape(
    path: str, before: str, after: str, name: str
) -> None:
    assert _after((path, before), (path, after), name=name).state == "unresolved"


def test_rusts_impl_block_is_not_a_member_two_structs_share() -> None:
    before = "pub struct MySql;\nimpl MySql {\n    pub fn connect() -> u32 { 1 }\n}\n"
    after = "pub struct Postgres;\nimpl Postgres {\n    pub fn connect() -> u32 { 2 }\n}\n"
    link = _after(("src/db.rs", before), ("src/db.rs", after), name="connect")
    assert (link.state, link.qualified_name) == ("missing", "crate::db::MySql::connect")


def test_a_class_rename_search_is_linear_in_the_number_of_classes() -> None:
    def work(n: int) -> int:
        def program(prefix: str) -> str:
            return "".join(
                f"class {prefix}{i}:\n    def fetch_rows(self):\n        return {i}\n\n"
                f"    def close(self):\n        pass\n\n\n"
                for i in range(n)
            )

        before, after = _side("m.py", program("Old")), _side("m.py", program("New"))
        was = next(d for d in before.defs if d.qualified_name == "m::Old0::fetch_rows")
        return count(lambda: successor(was, before, after))

    assert work(400) / work(50) < 12


def test_a_rival_too_long_to_compare_may_be_the_one_so_no_successor_is_certain() -> None:
    body = "".join(f"    x{i} = compute(x, {i})\n" for i in range(300))
    extra = "".join(f"    y{i} = other(x, {i})\n" for i in range(101))
    keep = "\n\ndef keep():\n    return 0\n"
    before = f"def fetch_rows(x):\n{body}    return x\n\n\ndef big_rows(x):\n{body}{extra}    return x\n{keep}"
    after = (
        f"def load_rows(x):\n{body.replace('compute(x, 7)', 'compute(x, 8)')}    return x\n{keep}"
    )
    old, new = _side("m.py", before), _side("m.py", after)
    was = next(d for d in old.defs if d.name == "fetch_rows")
    found = successor(was, old, new)
    # not compared, so not ruled out either
    assert (found.definition, found.uncertain, found.unmatched) == (None, True, False)


def test_a_class_renamed_in_a_file_of_out_of_line_methods_is_unsure() -> None:
    # its class is in the header: this file cannot show it is a class, nor a namespace
    before = (
        "namespace DB {\nint FunctionMatchPhrase::getReturnTypeImpl() const { return 1; }\n"
        "void FunctionMatchPhrase::checkArguments() const { check(); }\n"
        "void FunctionMatchPhrase::buildImpl() const { build(); }\n}\n"
    )
    after = before.replace("FunctionMatchPhrase", "FunctionHasPhrase")
    link = _after(("src/F.cpp", before), ("src/F.cpp", after), name="getReturnTypeImpl")
    assert link.state == "unresolved"


def test_a_rust_impl_renamed_without_its_struct_carries_its_methods() -> None:
    before = "impl Reader {\n    pub fn get(&self) -> u32 { 1 }\n    pub fn put(&self) {}\n}\n"
    link = _after(
        ("src/r.rs", before), ("src/r.rs", before.replace("Reader", "Loader")), name="get"
    )
    assert (link.state, link.qualified_name) == ("changed", "crate::r::Loader::get")


@pytest.mark.parametrize(
    "program",
    [
        "class {0}:\n    def clone(self) -> {0}:\n        return {0}()\n\n"
        "    def size(self):\n        return 1\n",
        "export class {0} {{\n  clone(): {0} {{ return new {0}(); }}\n"
        "  size(): number {{ return 1; }}\n}}\n",
    ],
)
def test_a_class_naming_its_own_type_renamed_alone_moves_its_methods(program: str) -> None:
    path = "m.py" if program.startswith("class") else "m.ts"
    before, after = program.format("Reader"), program.format("RowReader")
    link = _after((path, before), (path, after), name="size")
    assert (link.state, link.qualified_name.rsplit("::", 2)[-2:]) == (
        "moved",
        ["RowReader", "size"],
    )


@pytest.mark.parametrize(
    ("path", "program"),
    [
        (
            "r.rs",
            "pub struct {0} {{ n: u32 }}\n\nimpl {0} {{\n    pub fn read(&self) -> u32 {{ self.n + {1} }}\n"
            "    pub fn size(&self) -> u32 {{ 1 }}\n    pub fn close(&self) {{}}\n}}\n",
        ),
        (
            "r.cpp",
            "struct {0} {{ int n; int read(); int size(); void close(); }};\n"
            "int {0}::read() {{ return n + {1}; }}\nint {0}::size() {{ return 1; }}\n"
            "void {0}::close() {{}}\n",
        ),
    ],
)
def test_a_method_edited_outside_its_renamed_class_reads_changed(path: str, program: str) -> None:
    link = _after(
        (path, program.format("Reader", 1)), (path, program.format("Writer", 2)), name="read"
    )
    assert (link.state, link.qualified_name.rsplit("::", 2)[-2:]) == ("changed", ["Writer", "read"])


@pytest.mark.parametrize(
    ("path", "program"),
    [
        ("r.py", "class {0}:\n    def read(self):\n        return 1\n"),
        ("r.rs", "pub struct {0};\n\nimpl {0} {{\n    pub fn read(&self) -> u32 {{ 1 }}\n}}\n"),
        ("r.ts", "export class {0} {{\n  read(): number {{ return 1; }}\n}}\n"),
    ],
)
def test_a_class_of_one_method_renamed_alone_leaves_it_unsure(path: str, program: str) -> None:
    # one member is too little to follow the class by: the link is served, never hidden
    link = _after((path, program.format("Reader")), (path, program.format("Writer")), name="read")
    assert link.state == "unresolved"


def test_a_rename_with_a_comment_added_in_its_body_is_moved() -> None:
    before = "def fetch_rows(a):\n    return a * 2 + 1\n"
    after = "def load_rows(a):\n    # note\n    return a * 2 + 1\n"
    got = _after(("reader.py", before), ("reader.py", after))
    assert (got.state, got.qualified_name) == ("moved", "reader::load_rows")


@pytest.mark.parametrize("name", ["fetch_rows", "close"])
def test_a_class_renamed_with_a_comment_added_above_a_method_moves_its_methods(
    name: str,
) -> None:
    def program(cls: str, comment: str = "") -> str:
        return (
            f"class {cls}:\n    def fetch_rows(self):\n        return 1\n\n{comment}"
            "    def close(self):\n        pass\n\n\nclass Writer:\n    def flush(self):\n"
            "        pass\n"
        )

    before, after = program("Reader"), program("Loader", "    # closes it\n")
    got = _after(("reader.py", before), ("reader.py", after), name)
    assert (got.state, got.qualified_name) == ("moved", f"reader::Loader::{name}")


# ---- renamed as it moved to another file or scope -----------------------------------


def _turnover(
    before: dict[str, str], after: dict[str, str]
) -> tuple[list[Side], list[Side], list[Side]]:
    """What the files gained, lost and kept by the name inside their module, as the
    recheck reads a range: each file's new definitions, its removed ones, and those it
    still has under a name it had."""

    def local(side: Side) -> dict[str, list]:
        names: dict[str, list] = {}
        for d in side.defs:
            names.setdefault(unnumbered(local_name(d.qualified_name, side.path)), []).append(d)
        return names

    new, gone, kept = [], [], []
    for path in sorted(before.keys() | after.keys()):
        old = _side(path, before.get(path, ""))
        now = _side(path, after.get(path, ""))
        was, here = local(old), local(now)
        new.append(Side(path, [d for n in here.keys() - was.keys() for d in here[n]], now.lines))
        kept.append(Side(path, [d for n in here.keys() & was.keys() for d in here[n]], now.lines))
        gone.append(Side(path, [d for n in was.keys() - here.keys() for d in was[n]], old.lines))
    return new, gone, kept


def _elsewhere(before: dict[str, str], after: dict[str, str], name: str = "fetch_rows") -> Link:
    """The link on `name` in a.py (or the only file of `before`) once the files are as
    `after`, where its own file no longer defines it."""
    path = "a.py" if "a.py" in before else next(iter(before))
    old = _side(path, before[path])
    was = next(d for d in old.defs if local_name(d.qualified_name, path) == name or d.name == name)
    link = Link(path, was.qualified_name, was.body_hash, checked_hash=was.body_hash)
    here = _side(path, after[path]).defs if path in after else []
    s = elsewhere(was, old, *_turnover(before, after))
    return recheck(
        link,
        path_now=path if path in after else "",
        here=here,
        renamed=s.definition,
        renamed_exactly=s.exact,
        uncertain=s.uncertain,
        carried=s.carried,
        head="c2",
    )


_KEEP = "def keep():\n    return 0\n"
_SMALL_ROWS = "def fetch_rows(n):\n    return list(range(n))\n"
_ROWS = (
    "def fetch_rows(n, start=0):\n    out = []\n    for i in range(start, n):\n"
    "        if i % 2:\n            out.append(i)\n        else:\n            out.append(-i)\n"
    "    return sorted(out)\n"
)
_METHOD = (
    "    def fetch_rows(self, n, start=0):\n        for i in range(start, n):\n"
    "            if i % 2:\n                self.rows.append(i)\n            else:\n"
    "                self.rows.append(-i)\n        return sorted(self.rows)\n"
)
_READER_CLOSES = (
    "class Reader:\n    def __init__(self):\n        self.rows = []\n\n"
    "    def close(self):\n        pass\n"
)
_READER = _READER_CLOSES + "\n" + _METHOD
_OTHER_ROWS = "class Other:\n" + _METHOD


@pytest.mark.parametrize("gone", [_KEEP, None], ids=["file-kept", "file-deleted"])
def test_a_function_renamed_as_it_moved_to_another_file_is_moved(gone: str | None) -> None:
    after = {"b.py": _ROWS.replace("fetch_rows", "warm_up")} | ({"a.py": gone} if gone else {})
    got = _elsewhere({"a.py": _KEEP + "\n\n" + _ROWS}, after)
    assert (got.state, got.path, got.qualified_name) == ("moved", "b.py", "b::warm_up")
    assert got.body_hash == got.checked_hash  # only its name changed: the new baseline


@pytest.mark.parametrize(
    ("name", "state"),
    [
        ("_fetch_rows", "moved"),
        ("fetch_rows_v2", "moved"),
        # its words with more before or after them: what it is called beside a namesake
        ("bulk_fetch_rows", "moved"),
        ("fetch_rows_once", "moved"),
        ("fetch_rows2", "moved"),
        ("fetch", "moved"),
        ("load_rows", "missing"),
        ("fetch_bulk_rows", "missing"),
        ("warm_up", "missing"),
    ],
)
def test_a_small_function_renamed_as_it_moved_keeps_the_words_of_its_name(
    name: str, state: str
) -> None:
    # under thirty tokens two definitions look alike by their shape alone, and among every
    # changed file one shared word is no kinship
    after = {"a.py": _KEEP, "b.py": _SMALL_ROWS.replace("fetch_rows", name)}
    assert _elsewhere({"a.py": _KEEP + "\n\n" + _SMALL_ROWS}, after).state == state


def test_a_name_that_holds_its_words_is_its_own_only_in_another_file() -> None:
    # beside where it was the search of its own file decides, and a width alone is a
    # sibling there (`sha`, `sha256`)
    after = {"a.py": _KEEP + "\n\n" + _SMALL_ROWS.replace("fetch_rows", "fetch_rows2")}
    assert _elsewhere({"a.py": _KEEP + "\n\n" + _SMALL_ROWS}, after).state == "missing"


@pytest.mark.parametrize(
    ("theirs", "state"),
    [
        # a name that holds its words is told by its tokens, never by a text that is close
        ("def bulk_fetch_rows(n):\n    return list(range(n + 1))\n", "missing"),
        # its own name with another text, in a class: not close, so another definition
        ("class Rows:\n    def fetch_rows(self):\n        return self.rows\n", "missing"),
        ("class Rows:\n    def fetch_rows(self, n):\n        return list(range(n))\n", "changed"),
    ],
    ids=["grown-and-edited", "unrelated-member", "close-member"],
)
def test_a_small_function_is_told_under_another_name_only_by_its_tokens(
    theirs: str, state: str
) -> None:
    after = {"a.py": _KEEP, "b.py": theirs}
    assert _elsewhere({"a.py": _KEEP + "\n\n" + _SMALL_ROWS}, after).state == state


_GUARDED = "if OLD_SSL:\n    class NAME(ValueError):\n        pass\n"


@pytest.mark.parametrize(
    ("name", "stayed", "state"),
    [
        # under a condition it did not have: its tokens, not its shape, and what
        # surrounds it changed
        ("SSLCertError", "", "changed"),
        ("RetryError", "", "missing"),
        # a stub that stayed has those tokens too: any of them may be what it became
        ("SSLCertError", "class KeyMissing(ValueError):\n    pass\n", "unresolved"),
    ],
    ids=["its-words", "other-words", "a-stub-that-stayed"],
)
def test_a_small_definition_moved_under_a_condition_is_told_token_for_token(
    name: str, stayed: str, state: str
) -> None:
    stub = "class CertError(ValueError):\n    pass\n"
    before = {"a.py": _KEEP + "\n\n" + stub, "b.py": "X = 1\n" + stayed}
    after = {"a.py": _KEEP, "b.py": "X = 1\n" + stayed + _GUARDED.replace("NAME", name)}
    got = _elsewhere(before, after, "CertError")
    assert got.state == state
    assert (got.qualified_name == f"b::{name}") == (state == "changed")


def test_a_removed_stub_is_not_handed_to_a_new_stub_sharing_a_word_of_its_name() -> None:
    # two exception stubs have one shape whatever they are for
    stub = "class ReadError(Exception):\n    pass\n"
    before = {"errors.py": _KEEP + "\n\n" + stub}
    after = {"errors.py": _KEEP, "retry.py": stub.replace("ReadError", "RetryError")}
    got = _elsewhere(before, after, "ReadError")
    assert (got.state, got.path) == ("missing", "errors.py")
    # the same stub made private as it moved is still the one the memory names
    after = {"errors.py": _KEEP, "retry.py": stub.replace("ReadError", "_ReadError")}
    got = _elsewhere(before, after, "ReadError")
    assert (got.state, got.qualified_name) == ("moved", "retry::_ReadError")


@pytest.mark.parametrize("name", ["Reader", "Reader.fetch_rows", "Reader.close"])
def test_a_class_renamed_as_it_moved_takes_its_members(name: str) -> None:
    after = {"a.py": _KEEP, "b.py": _READER.replace("Reader", "Loader")}
    got = _elsewhere({"a.py": _KEEP + "\n\n" + _READER}, after, name)
    assert (got.state, got.qualified_name) == (
        "moved",
        "b::" + name.replace("Reader", "Loader").replace(".", "::"),
    )


def test_a_method_that_left_its_class_for_another_file_reads_changed() -> None:
    # the same text in another class: what surrounds it is not what the memory saw
    got = _elsewhere(
        {"a.py": _READER}, {"a.py": _READER_CLOSES, "b.py": _OTHER_ROWS}, "Reader.fetch_rows"
    )
    assert (got.state, got.qualified_name) == ("changed", "b::Other::fetch_rows")
    assert got.carried


def test_a_method_that_became_a_function_reads_changed() -> None:
    function = "\n".join(line[4:] for line in _METHOD.splitlines()) + "\n"
    got = _elsewhere(
        {"a.py": _READER}, {"a.py": _READER_CLOSES + "\n\n" + function}, "Reader.fetch_rows"
    )
    assert (got.state, got.qualified_name) == ("changed", "a::fetch_rows")


@pytest.mark.parametrize(
    ("theirs", "state"),
    [
        (_OTHER_ROWS.replace("append(-i)", "append(-i * 2)"), "changed"),  # close
        ("class Other:\n    def fetch_rows(self):\n        return {'unrelated': 1}\n", "missing"),
    ],
    ids=["close", "not-close"],
)
def test_its_own_name_in_another_class_elsewhere_is_it_only_with_a_close_text(
    theirs: str, state: str
) -> None:
    got = _elsewhere(
        {"a.py": _READER}, {"a.py": _READER_CLOSES, "b.py": theirs}, "Reader.fetch_rows"
    )
    assert got.state == state
    assert (got.path == "b.py") == (state == "changed")


_DICT = (
    "class {0}:\n    def __init__(self):\n        self.data = {{}}\n\n"
    "    def __iter__(self):\n        return {1}\n\n"
    "    def __len__(self):\n        return len(self.data)\n"
)


_FOLDED = _DICT.format("Folded", "iter(self.data.keys())")


@pytest.mark.parametrize(
    ("kept", "theirs", "state"),
    [
        # the class came with it, edited: two of its three members, and this one
        (_KEEP, _FOLDED, "changed"),
        # another class's: one member name in common says nothing
        (
            _KEEP,
            "class Folded:\n    def __iter__(self):\n        return iter(self.data)\n",
            "missing",
        ),
        # its own class is still there, less this method: the new class is another one
        (
            _DICT.format("Multi", "0").replace("    def __iter__(self):\n        return 0\n\n", ""),
            _FOLDED,
            "missing",
        ),
    ],
    ids=["its-class-came-along", "unrelated-class", "its-class-stayed"],
)
def test_a_small_method_goes_to_its_name_in_another_class_only_with_its_class(
    kept: str, theirs: str, state: str
) -> None:
    # `__iter__` is any class's, and a text this short is mostly its signature
    before = {"a.py": _KEEP + "\n\n" + _DICT.format("Multi", "iter(self.data)")}
    got = _elsewhere(before, {"a.py": kept, "b.py": theirs}, "Multi.__iter__")
    assert got.state == state


_CLEAR = "    def clear(self):\n        self.data.clear()\n"
_OWN = (
    "    def get(self, key):\n        return self.data[key]\n\n"
    "    def put(self, key, value):\n        self.data[key] = value\n\n"
    "    def pop(self, key):\n        return self.data.pop(key)\n\n"
    "    def keys(self):\n        return list(self.data)\n\n"
) + _CLEAR


@pytest.mark.parametrize(
    ("own", "state"),
    [
        # five members of its own beside the four it took in: mostly another class
        (_OWN, "missing"),
        # one of its own: it keeps the removed class's
        (_CLEAR, "changed"),
    ],
    ids=["mostly-its-own", "mostly-the-removed-class"],
)
def test_a_class_that_was_already_there_is_weighed_with_the_members_it_had(
    own: str, state: str
) -> None:
    # Store was in b.py and takes Multi's members in as Multi goes
    multi = _KEEP + "\n\n" + _DICT.format("Multi", "iter(self.data)")
    after = {"a.py": _KEEP, "b.py": _DICT.format("Store", "iter(self.data)") + "\n" + own}
    before = {"a.py": multi, "b.py": "class Store:\n" + own}
    assert _elsewhere(before, after, "Multi.__iter__").state == state
    # as the same class is when the range brings it in whole
    assert _elsewhere({"a.py": multi}, after, "Multi.__iter__").state == state


def test_a_small_method_follows_its_class_renamed_with_an_alias_left_behind() -> None:
    # the old name stays as an alias of the class: no member is left under it
    before = {"a.py": _DICT.format("Multi", "iter(self.data)")}
    after = {"a.py": _DICT.format("Folded", "iter(self.data)") + "\n\nMulti = Folded\n"}
    got = _elsewhere(before, after, "Multi.__iter__")
    assert (got.state, got.qualified_name) == ("moved", "a::Folded::__iter__")


def test_a_small_method_never_becomes_a_function_of_its_name() -> None:
    before = {"a.py": _DICT.format("Multi", "iter(self.data)")}
    after = {"a.py": _KEEP, "b.py": "def __iter__(self):\n    return iter(self.data)\n"}
    assert _elsewhere(before, after, "Multi.__iter__").state == "missing"


def test_a_close_namesake_that_stayed_leaves_an_edited_move_unresolved() -> None:
    # sibling classes share the method. Client keeps its name and takes the text of the
    # removed Sync, while Client's old text arrives as Async: the new one is not the
    # only close one, and the one that stayed never inherits a link
    sync, other = _METHOD, _METHOD.replace("sorted(self.rows)", "list(self.rows)")
    before = {"a.py": "class Sync:\n" + sync, "b.py": "class Client:\n" + other}
    after = {"b.py": "class Client:\n" + sync + "\n\nclass Async:\n" + other}
    got = _elsewhere(before, after, "Sync.fetch_rows")
    assert (got.state, got.path) == ("unresolved", "a.py")
    # with no namesake kept beside it, the new one is followed
    alone = {"b.py": "class Async:\n" + other}
    got = _elsewhere({"a.py": "class Sync:\n" + sync}, alone, "Sync.fetch_rows")
    assert (got.state, got.qualified_name) == ("changed", "b::Async::fetch_rows")


def test_a_namesake_that_stayed_never_makes_removed_code_unresolved() -> None:
    # nothing new is close to it: the code is gone, however alike its sibling is
    before = {"a.py": "class Sync:\n" + _METHOD, "b.py": "class Client:\n" + _METHOD}
    after = {"a.py": _KEEP, "b.py": "class Client:\n" + _METHOD + "\n\nX = 1\n"}
    assert _elsewhere(before, after, "Sync.fetch_rows").state == "missing"


def test_two_new_definitions_of_its_shape_leave_it_unresolved() -> None:
    after = {
        "a.py": _KEEP,
        "b.py": _ROWS.replace("fetch_rows", "load_rows"),
        "c.py": _ROWS.replace("fetch_rows", "read_rows"),
    }
    got = _elsewhere({"a.py": _KEEP + "\n\n" + _ROWS}, after)
    assert (got.state, got.path) == ("unresolved", "a.py")


def test_two_close_definitions_of_its_name_leave_it_unresolved() -> None:
    edited = _OTHER_ROWS.replace("append(-i)", "append(-i * 2)")
    after = {"a.py": _READER_CLOSES, "b.py": edited, "c.py": edited.replace("Other", "Third")}
    assert _elsewhere({"a.py": _READER}, after, "Reader.fetch_rows").state == "unresolved"


def test_a_new_definition_another_removed_one_may_have_become_is_not_followed() -> None:
    # c.py lost a definition of the same shape: b.py's may be either's
    before = {"a.py": _KEEP + "\n\n" + _ROWS, "c.py": _ROWS.replace("fetch_rows", "scan_rows")}
    after = {"a.py": _KEEP, "c.py": _KEEP, "b.py": _ROWS.replace("fetch_rows", "load_rows")}
    assert _elsewhere(before, after).state == "unresolved"


def test_removed_twins_each_follow_the_new_definition_of_their_own_name() -> None:
    # two classes with the same methods move at once: each method goes with its name
    twins = _READER + "\n\n" + _READER.replace("Reader", "Writer").replace("fetch_rows", "put_rows")
    moved = twins.replace("Reader", "Loader").replace("Writer", "Saver")
    before, after = {"a.py": _KEEP + "\n\n" + twins}, {"a.py": _KEEP, "b.py": moved}
    got = _elsewhere(before, after, "Writer.put_rows")
    assert (got.state, got.qualified_name) == ("moved", "b::Saver::put_rows")
    # the same `close` in both: which class's it is cannot be told by the method alone
    assert _elsewhere(before, after, "Writer.close").state == "unresolved"


def test_a_removed_definition_with_nothing_new_like_it_is_missing() -> None:
    after = {"a.py": _KEEP, "b.py": "def unrelated(n):\n    return {'n': n}\n"}
    assert _elsewhere({"a.py": _KEEP + "\n\n" + _ROWS}, after).state == "missing"


# ---- its text nearly unchanged under another name -------------------------------------

# `_ROWS` with a parameter added and a callee renamed with it: a tenth of its tokens
_ROWS_EDITED = _ROWS.replace("(n, start=0)", "(n, start=0, step=1)").replace("sorted", "_sorted")


@pytest.mark.parametrize(
    ("theirs", "state"),
    [
        (_ROWS_EDITED, "changed"),
        # rewritten past a tenth of its tokens: nothing but its name would tell it
        (_ROWS_EDITED.replace("out.append(-i)", "out.extend([i, -i, i * i])"), "missing"),
    ],
    ids=["nearly-its-text", "rewritten"],
)
def test_a_function_renamed_and_edited_as_it_moved_is_told_by_a_near_text(
    theirs: str, state: str
) -> None:
    after = {"a.py": _KEEP, "b.py": theirs.replace("fetch_rows", "warm_up")}
    got = _elsewhere({"a.py": _KEEP + "\n\n" + _ROWS}, after)
    assert (got.state, got.path) == (state, "b.py" if state == "changed" else "a.py")


def test_a_near_text_is_no_answer_without_the_edited_rules() -> None:
    # the search made first, for its tokens alone, leaves it to the later ones
    before = {"a.py": _KEEP + "\n\n" + _ROWS}
    after = {"a.py": _KEEP, "b.py": _ROWS_EDITED.replace("fetch_rows", "warm_up")}
    old = _side("a.py", before["a.py"])
    was = next(d for d in old.defs if d.name == "fetch_rows")
    turnover = _turnover(before, after)
    assert elsewhere(was, old, *turnover, edited=False).definition is None
    assert elsewhere(was, old, *turnover).definition.qualified_name == "b::warm_up"


def test_a_small_definition_is_never_told_by_a_near_text() -> None:
    # two dozen tokens with one changed are any accessor's
    after = {"a.py": _KEEP, "b.py": _SMALL_ROWS.replace("(n)", "(n, m)").replace("fetch", "_fetch")}
    assert _elsewhere({"a.py": _KEEP + "\n\n" + _SMALL_ROWS}, after).state == "missing"


def test_a_near_text_goes_to_the_removed_definition_nearest_to_it() -> None:
    # c.py lost the very text b.py gains under another name: b.py's is that one's, and
    # a.py's, a parameter and a callee away, is not handed a second predecessor's heir
    twin = _ROWS_EDITED.replace("fetch", "scan")
    after = {"a.py": _KEEP, "c.py": _KEEP, "b.py": _ROWS_EDITED.replace("fetch_rows", "warm_up")}
    assert _elsewhere({"a.py": _KEEP + "\n\n" + _ROWS, "c.py": twin}, after).state == "unresolved"
    got = _elsewhere({"a.py": twin, "c.py": _KEEP + "\n\n" + _ROWS}, after, "scan_rows")
    assert (got.state, got.qualified_name) == ("moved", "b::warm_up")


def test_a_text_that_stayed_as_near_under_a_name_it_had_leaves_it_unresolved() -> None:
    # Client keeps its name and takes Sync's text; Client's old text, nearly the same,
    # arrives as Async.load_rows: Sync's may have gone under the name that stayed
    sync, other = _METHOD, _METHOD.replace("sorted(self.rows)", "list(self.rows)")
    theirs = other.replace("fetch_rows", "load_rows")
    before = {"a.py": "class Sync:\n" + sync, "b.py": "class Client:\n" + theirs}
    after = {"b.py": "class Client:\n" + sync.replace("fetch_rows", "load_rows")}
    after["b.py"] += "\n\nclass Async:\n" + theirs
    got = _elsewhere(before, after, "Sync.fetch_rows")
    assert (got.state, got.path) == ("unresolved", "a.py")
    # with no such name kept beside it, the near one is it
    alone = {"b.py": "class Async:\n" + theirs}
    got = _elsewhere({"a.py": "class Sync:\n" + sync}, alone, "Sync.fetch_rows")
    assert (got.state, got.qualified_name) == ("changed", "b::Async::load_rows")


_INNER = (
    "    def collect(n, start=0):\n        out, seen = [], set()\n"
    "        for i in range(start, n):\n            if i in seen:\n                continue\n"
    "            seen.add(i)\n            if i % 2:\n                out.append(i)\n"
    "            else:\n                out.append(-i)\n"
    "        for j in sorted(seen, reverse=True):\n            out.append(j * j)\n"
    "        return sorted(out)\n"
)


def test_a_near_text_inside_a_new_wrapper_is_it_not_the_wrapper() -> None:
    # the wrapper is as near as the function it holds, by holding it
    inner = _INNER.replace("collect", "gather").replace("(n, start=0)", "(n, start=0, step=1)")
    function = "\n".join(line[4:] for line in _INNER.splitlines()) + "\n"
    before = {"a.py": _KEEP + "\n\n" + function}
    after = {"a.py": _KEEP, "b.py": "def make():\n" + inner + "\n    return gather\n"}
    got = _elsewhere(before, after, "collect")
    assert (got.state, got.qualified_name) == ("changed", "b::make::gather")


def test_a_function_nested_in_it_is_the_one_a_new_function_alike_became() -> None:
    # fetch_rows is little more than the function it holds. b.py's `run` keeps its name
    # and takes that one in as `gather`: `gather` is `collect`, and fetch_rows, as near
    # to it by what it holds, is not followed into a part of itself
    outer = "def fetch_rows(n):\n" + _INNER + "\n    return collect(n)\n"
    host = (
        "def run(n):\n" + _INNER.replace("collect", "gather") + "\n    return gather(n) or None\n"
    )
    before = {"a.py": _KEEP + "\n\n" + outer, "b.py": "def run(n):\n    return None\n"}
    after = {"a.py": _KEEP, "b.py": host}
    assert _elsewhere(before, after).state == "unresolved"
    got = _elsewhere(before, after, "fetch_rows.collect")
    assert (got.state, got.qualified_name) == ("changed", "b::run::gather")


# ---- a small definition goes with its scope -------------------------------------------

_STUB = "class Row:\n    x = field()\n    y = field()\n"
_FIXTURE = "def make_row():\n    class NAME:\n        x = field()\n        y = field()\n\n"


@pytest.mark.parametrize(
    ("name", "state"),
    # a name that only holds its words (`BigRow`) is its own at module level alone
    [("Row", "changed"), ("_Row", "changed"), ("Pair", "missing"), ("BigRow", "missing")],
)
def test_a_small_module_level_definition_goes_into_a_scope_under_its_own_words(
    name: str, state: str
) -> None:
    # at module level its name is its own: nested in a new function, it is still it
    after = {"a.py": _KEEP, "b.py": _FIXTURE.replace("NAME", name) + f"    return {name}\n"}
    got = _elsewhere({"a.py": _KEEP + "\n\n" + _STUB}, after, "Row")
    assert got.state == state
    if state == "changed":  # what surrounds it is new
        assert (got.qualified_name, got.carried) == (f"b::make_row::{name}", True)


_SUITE = (
    "class TestRows:\n    def test_reads(self):\n        assert read(1) == [1]\n\n"
    "    def NAME(self):\n        assert read(2) != read(3)\n"
)


@pytest.mark.parametrize(
    ("name", "state"),
    [("test_distinct", "changed"), ("check_other", "missing")],
    ids=["kin", "another-name"],
)
def test_a_small_method_renamed_as_its_class_moved_goes_with_the_class(
    name: str, state: str
) -> None:
    # the class is the same but for this name: inside it, a name its own file's search
    # would take is enough, and one of other words is another method
    before = {"a.py": _KEEP + "\n\n" + _SUITE.replace("NAME", "distinct")}
    after = {"a.py": _KEEP, "b.py": _SUITE.replace("NAME", name)}
    got = _elsewhere(before, after, "TestRows.distinct")
    assert got.state == state
    assert (got.qualified_name == f"b::TestRows::{name}") == (state == "changed")


_WIDE = (
    "    def __iter__(self):\n        return iter(self.data)\n\n"
    "    def read(self, key, default=None):\n        if key in self.data:\n"
    "            return self.data[key]\n        self.misses += 1\n        return default\n\n"
    "    def write(self, key, value):\n        if key in self.data:\n"
    "            self.hits += 1\n        self.data[key] = value\n        return self\n"
)
_NARROW = "    def __len__(self):\n        return len(self.data)\n"


@pytest.mark.parametrize(
    ("left", "state"),
    [
        # `read` and `write`, each told by its own text, left with it for Base
        ("", "changed"),
        # only `read` did: one member says nothing of the class
        ("write", "missing"),
    ],
    ids=["two-came-along", "one-came-along"],
)
def test_a_small_method_goes_to_the_class_its_larger_siblings_left_for(
    left: str, state: str
) -> None:
    # Multi is split: it stays in a.py with `__len__`, and Base takes the rest
    stays = _NARROW
    theirs = _WIDE
    if left:
        kept = _WIDE[_WIDE.index("    def write") :]
        stays, theirs = _NARROW + "\n" + kept, _WIDE[: _WIDE.index("    def write")]
    before = {"a.py": "class Multi:\n" + _NARROW + "\n" + _WIDE}
    after = {"a.py": "class Multi(Base):\n" + stays, "b.py": "class Base:\n" + theirs}
    got = _elsewhere(before, after, "Multi.__iter__")
    assert got.state == state
    assert (got.path == "b.py") == (state == "changed")


_JAR = (
    "class NAME:\n"
    "    def __getitem__(self, name):\n        return self._find(name)\n\n"
    "    def get(self, name, default=None):\n        try:\n            return self._find(name)\n"
    "        except KeyError:\n            self.misses += 1\n            return default\n\n"
    "    def set(self, name, value):\n        self.data[name] = VALUE\n"
)
_JAR_OLD = "    def update(self, other):\n        self.data.update(other)\n"
_JAR_NEW = (
    "    def copy(self):\n        return type(self)(self.data)\n\n"
    "    def clear(self):\n        self.data.clear()\n\n"
    "    def keys(self):\n        return list(self.data)\n"
)


@pytest.mark.parametrize(
    ("get", "state"),
    [("self._find(name)", "changed"), ("self._find(name.lower())", "missing")],
    ids=["a-larger-sibling-came-along", "none-with-its-tokens"],
)
def test_a_small_method_goes_with_a_class_keeping_two_names_and_a_larger_siblings_text(
    get: str, state: str
) -> None:
    # CookieJar left a.py whole and RequestsJar keeps three of its four names among its
    # six, under half of all seven: `get`, told by its own text, vouches for the class
    old = _JAR.replace("NAME", "CookieJar").replace("VALUE", "value") + "\n" + _JAR_OLD
    new = _JAR.replace("NAME", "RequestsJar").replace("VALUE", "str(value)") + "\n" + _JAR_NEW
    after = {
        "a.py": _KEEP,
        "b.py": new.replace(
            "return self._find(name)\n        except", f"return {get}\n        except"
        ),
    }
    got = _elsewhere({"a.py": _KEEP + "\n\n" + old}, after, "CookieJar.__getitem__")
    assert got.state == state
    assert (got.qualified_name == "b::RequestsJar::__getitem__") == (state == "changed")


def test_small_siblings_do_not_vouch_for_a_small_method() -> None:
    # any dict-like class has these three: Store is not shown to be Multi
    before = {"a.py": _KEEP + "\n\n" + _DICT.format("Multi", "iter(self.data)")}
    theirs = _DICT.format("Store", "iter(self.data)") + "\n" + _OWN
    assert _elsewhere(before, {"a.py": _KEEP, "b.py": theirs}, "Multi.__iter__").state == "missing"


# ---- the lines a wide range is searched for -------------------------------------------


def test_a_definitions_own_lines_are_its_longest_without_its_name() -> None:
    old = _side("a.py", "class Reader:\n" + _METHOD.replace("sorted(self.rows)", "fetch_rows"))
    was = next(d for d in old.defs if d.name == "fetch_rows")
    # without indentation, a move changes it; never a line a rename rewrites
    assert own_lines(was, old) == [
        "for i in range(start, n):",
        "self.rows.append(-i)",
        "self.rows.append(i)",
    ]
    stub = _side("a.py", "def fetch_rows():\n    pass\n")
    assert own_lines(stub.defs[0], stub) == []
