"""What a linked definition became once its name is gone: a rename alone, a rename with
an edit, a method of a renamed class; uncertain when a new definition may be it and
none certainly is, and nothing when the code was removed."""

from __future__ import annotations

import pytest

from server.code.app.memory_links import _definitions
from server.code.domain.anchor import Link, locate, recheck
from server.code.domain.successor import _MAX_LINES, Side, successor
from tests.unit.step_count import steps as count


def _side(path: str, src: str) -> Side:
    return Side(path, _definitions(path, src.encode()), src.splitlines())


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
    assert (found.definition, found.uncertain) == (None, True)


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
