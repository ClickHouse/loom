"""A definition's declaration as its parser sees it: where it starts once decorators,
attributes and template heads are counted, and its rename shapes. An exact match is a
rename alone; a loose one (the name is also used another way inside) may have changed
what a call means and is relinked as changed; anything else is not a rename."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.domain.anchor import NO_SHAPE, Shape
from server.code.domain.declaration import Declarations
from tests.unit.step_count import steps


def _start(path: str, src: str, line: int) -> int:
    decls = Declarations.parse(path, src.encode())
    assert decls is not None
    return decls.start(line)


def _shape(decls: Declarations, line: int, name: str) -> Shape:
    m = decls.member(line, name)
    return NO_SHAPE if m is None else m.shape


def _text(decls: Declarations, line: int, name: str) -> str | None:
    m = decls.member(line, name, shapes=False)
    return None if m is None else m.shown


def _shapes(path: str, src: str, name: str) -> tuple[str, tuple[str, str]]:
    decls = Declarations.parse(path, src.encode())
    assert decls is not None
    line = 1 + next(
        i
        for i, s in enumerate(src.splitlines())
        if name in s and not s.lstrip().startswith(("@", "#[", "//"))
    )
    exact, calls, reads = _shape(decls, line, name)
    return exact, (calls, reads)


def _match(path: str, before: str, after: str) -> str | None:
    """'exact', 'loose' or None: how `after` (`load_rows`) matches `before` (`fetch_rows`)."""
    (e1, l1), (e2, l2) = _shapes(path, before, "fetch_rows"), _shapes(path, after, "load_rows")
    if e1 and e1 == e2:
        return "exact"
    return "loose" if any(x and x == y for x, y in zip(l1, l2, strict=True)) else None


@pytest.mark.parametrize(
    ("src", "line", "start"),
    [
        ('@deco(")")\ndef f():\n    return 1\n', 2, 1),  # a bracket inside a string
        ("@deco\n# why it is cached\ndef f():\n    return 1\n", 3, 1),  # a comment between
        (
            "@deco(\n" + "".join(f"    a{i}=1,\n" for i in range(40)) + ")\ndef f():\n    pass\n",
            43,
            1,
        ),
        ("class C:\n    @property\n    def g(self):\n        return 1\n", 3, 2),
        ("x = [1]\ndef g():\n    return 2\n", 2, 2),
    ],
)
def test_python_decorators_start_the_definition(src: str, line: int, start: int) -> None:
    assert _start("m.py", src, line) == start


def test_template_heads_and_typescript_decorators_start_the_definition() -> None:
    cpp = "namespace a {\ntemplate <int N = 1>\nint f() { return N; }\nint g() { return 0; }\n}\n"
    assert (_start("m.cpp", cpp, 3), _start("m.cpp", cpp, 4)) == (2, 4)
    ts = "class Box {\n  @Input()\n  size(): number { return 1; }\n}\n"
    assert _start("m.ts", ts, 3) == 2


def test_a_language_without_a_grammar_has_no_declaration() -> None:
    assert Declarations.parse("notes.txt", b"fetch_rows") is None


# (path, the program with NAME for the declaration and every name renamed with it, the
# match expected); `fetch_rows` left in the program stays as it is
_RENAMES = [
    # a rename alone: the recursive calls and reads follow the declaration
    ("m.py", "def NAME(n):\n    return NAME(n - 1)\n", "exact"),
    ("m.cpp", "int NAME(int n) { return NAME(n - 1); }", "exact"),
    ("m.ts", "function NAME(n) { return NAME(n - 1); }", "exact"),
    ("m.py", "class C:\n    def NAME(self):\n        return 1\n", "exact"),  # its only member
    ("m.ts", "export function NAME(n: number): number { return NAME(n - 1); }", "exact"),
    ("m.py", "def NAME(n, d):\n    d[NAME] = 2\n    return NAME(n - 1, d)\n", "exact"),
    (
        "m.py",
        "def NAME(data, n):\n    data.fetch_rows = 1\n    return NAME(data, n - 1)\n",
        "exact",
    ),
    ("m.py", "def NAME(n):\n    from fetch_rows import DEFAULT\n    return NAME(n - 1)\n", "exact"),
    (
        "m.py",
        "def NAME(n):\n    from backend import fetch_rows as handler\n    return NAME(n - 1)\n",
        "exact",
    ),
    ("m.py", "@constant(fetch_rows)\ndef NAME():\n    return 0\n", "exact"),
    (
        "m.py",
        "def NAME(n):\n    def helper(f=NAME):\n        return f\n    return NAME(n - 1) if n else helper()\n",
        "exact",
    ),
    (
        "m.java",
        "class Sample {\n    static int NAME(int n) {\n        int fetch_rows = 3;\n"
        "        return n > 0 ? NAME(n - 1) : fetch_rows;\n    }\n}\n",
        "exact",
    ),
    (
        "m.rs",
        "fn NAME(n: u32) -> u32 {\n    if n > 0 { return NAME(n - 1); }\n    fetch_rows!()\n}\n",
        "exact",
    ),
    # the name is also bound or used another way inside: a call may mean something else
    # now, so the link follows the rename as changed
    (
        "m.py",
        "def NAME(n):\n    def helper():\n        fetch_rows = 1\n        return fetch_rows\n"
        "    return NAME(n - 1) if n else helper()\n",
        "loose",
    ),
    (
        "m.py",
        "def NAME(n):\n    def helper(fetch_rows):\n        return fetch_rows\n"
        "    return NAME(n - 1) if n else helper(1)\n",
        "loose",
    ),
    (
        "m.py",
        "def NAME(n):\n    values = [fetch_rows for fetch_rows in range(3)]\n"
        "    return NAME(n - 1) if n else len(values)\n",
        "loose",
    ),
    (
        "m.py",
        "def NAME(n):\n    return [fetch_rows for fetch_rows in NAME(n - 1)] if n else []\n",
        "loose",
    ),
    (
        "m.py",
        "def NAME(n):\n    class Box:\n        fetch_rows = 3\n    return NAME(n - 1) if n else Box.fetch_rows\n",
        "loose",
    ),
    (
        "m.py",
        "def NAME(n, callback=fetch_rows):\n    return NAME(n - 1) if n else callback\n",
        "loose",
    ),
    (
        "m.ts",
        "function NAME(n: number): number {\n  if (n > 0) { const fetch_rows = 1; }\n"
        "  return n ? NAME(n - 1) : 3;\n}\n",
        "loose",
    ),
    (
        "m.js",
        "function NAME(n) {\n  const g = fetch_rows => fetch_rows;\n  return n ? NAME(n - 1) : g(3);\n}\n",
        "loose",
    ),
    (
        "m.cpp",
        "int NAME(int n) {\n  if (!n) return 3;\n  int result = NAME(n - 1);\n"
        "  int fetch_rows = 5;\n  return result;\n}\n",
        "loose",
    ),
    (
        "m.cpp",
        "int NAME(int n) {\n  if (!n) return 3;\n  auto helper = [fetch_rows = NAME(n - 1)]() {\n"
        "    return fetch_rows;\n  };\n  return helper();\n}\n",
        "loose",
    ),
    (
        "m.go",
        "package main\n\nfunc NAME(n int) int {\n\tif n == 0 { return 3 }\n\tvar (\n"
        "\t\tfetch_rows = NAME(n - 1)\n\t\tresult = fetch_rows\n\t)\n\treturn result\n}\n",
        "loose",
    ),
    (
        "m.rs",
        "fn NAME(n: u32) -> u32 {\n    if n == 0 { return 0; }\n"
        "    let fetch_rows = NAME(n - 1);\n    fetch_rows\n}\n",
        "loose",
    ),
    # what a name means may have changed: followed, but as changed
    ("m.py", "def NAME():\n    [(fetch_rows := 3) for _ in [0]]\n    return NAME\n", "loose"),
    ("m.cpp", "int NAME(int n) {\n  int fetch_rows = 1;\n  return NAME;\n}\n", "loose"),
    ("m.go", "package m\n\nfunc NAME() int {\n\tfetch_rows := 3\n\treturn NAME\n}\n", "loose"),
    (
        "m.ts",
        "function NAME(n: number): number {\n  function helper(fetch_rows = (m: number) => 3, cb = NAME) {\n"
        "    return cb(0);\n  }\n  return n ? helper() : 4;\n}\n",
        "loose",
    ),
    (
        "m.py",
        "def NAME(n):\n    def helper(cb=NAME):\n        fetch_rows = lambda: 9\n        return cb(0)\n"
        "    return helper() if n else 3\n",
        "loose",
    ),
    # a nested class: a member of it (or what it inherits) may be what the name means
    (
        "m.java",
        "class Sample {\n    static int NAME(int n) {\n        if (n == 0) return 4;\n"
        "        class Box {\n            int fetch_rows(int m) { return 3; }\n"
        "            int apply() { return NAME(0); }\n        }\n"
        "        return new Box().apply();\n    }\n}\n",
        "loose",
    ),
    # a qualified import binds the bare name; an aliased one or a qualified call does not
    (
        "m.cpp",
        "namespace DB {\nint NAME(int n = 0) {\n    if (!n) return 4;\n"
        "    using backend::fetch_rows;\n    return NAME(0);\n}\n}\n",
        "loose",
    ),
    (
        "m.cpp",
        "namespace DB {\nint NAME(int n = 0) {\n    if (!n) return backend::fetch_rows(0);\n"
        "    return NAME(0);\n}\n}\n",
        "exact",
    ),
    (
        "m.rs",
        "fn NAME(n: u32) -> u32 {\n    use crate::other::fetch_rows;\n"
        "    if n > 0 { return NAME(n - 1); }\n    3\n}\n",
        "loose",
    ),
    (
        "m.rs",
        "fn NAME(n: u32) -> u32 {\n    use crate::other::fetch_rows as other_rows;\n"
        "    if n > 0 { return NAME(n - 1); }\n    3\n}\n",
        "exact",
    ),
    # a function bound to a variable
    ("m.ts", "const NAME = (): number => 3;\n", "exact"),
    ("m.ts", "export const NAME = (): number => 3;\n", "exact"),
    ("m.ts", "const NAME = function (): number { return 3; };\n", "exact"),
    ("m.ts", "const NAME = (n: number): number => (n ? NAME(n - 1) : 3);\n", "exact"),
    # the callable forms the index declares: wrapped, asserted, generators, wrapper calls
    ("m.ts", "const NAME = (() => 3);\n", "exact"),
    ("m.ts", "const NAME = (() => 3) as () => number;\n", "exact"),
    ("m.ts", "const NAME = (() => 3)!;\n", "exact"),
    ("m.ts", "const NAME = (() => 3) satisfies () => number;\n", "exact"),
    ("m.ts", "const NAME = (function () { return 3; });\n", "exact"),
    ("m.ts", "const NAME = function* () { yield 3; };\n", "exact"),
    ("m.ts", "const NAME = async function* () { yield 3; };\n", "exact"),
    ("m.ts", "const NAME = memo(() => 3);\n", "exact"),
    # a type declared with the name (a C++ alias or local struct, a TypeScript class)
    (
        "m.cpp",
        "struct Box { operator int() { return 3; } };\n\nint NAME(int n = 0) {\n"
        "    if (!n) return 4;\n    using fetch_rows = Box;\n    return NAME();\n}\n",
        "loose",
    ),
    (
        "m.cpp",
        "int NAME(int n = 0) {\n    if (!n) return 4;\n"
        "    struct fetch_rows { operator int() { return 3; } };\n    return NAME();\n}\n",
        "loose",
    ),
    (
        "m.ts",
        "function NAME(n: number = 0): number {\n  if (!n) return 4;\n"
        "  class fetch_rows { get v(): number { return 3; } }\n  return (NAME.prototype as any).v;\n}\n",
        "loose",
    ),
    (
        "m.java",
        "class Sample {\n    static int NAME(int n) {\n        if (n == 0) return 4;\n"
        "        enum Box {\n            INSTANCE;\n            int fetch_rows(int m) { return 3; }\n"
        "            int apply() { return NAME(0); }\n        }\n"
        "        return Box.INSTANCE.apply();\n    }\n}\n",
        "loose",
    ),
    (
        "m.java",
        "class Sample {\n    static int NAME(int n) {\n        if (n == 0) return 4;\n"
        "        interface Box {\n            default int fetch_rows(int m) { return 3; }\n"
        "            default int apply() { return NAME(0); }\n        }\n"
        "        return new Box() {}.apply();\n    }\n}\n",
        "loose",
    ),
    (
        "m.cpp",
        "int NAME(int n) {\n  struct Box : Base {\n    int apply() { return NAME(0); }\n  };\n"
        "  return n ? Box().apply() : 4;\n}\n",
        "loose",
    ),
    # what the code does changed: not a rename
    ("m.py", 'def NAME():\n    return "NAME"\n', None),
    ("m.py", "def NAME(data):\n    return data.NAME\n", None),
    ("m.cpp", 'const char* NAME() { return R"(a " NAME " b)"; }', None),
    ("m.py", "def NAME():\n    return client.load(NAME=1)\n", None),
    ("m.py", "@constant(NAME)\ndef NAME():\n    return 0\n", None),
    ("m.py", "def NAME():\n    from backend import NAME\n    return NAME()\n", None),
    (
        "m.ts",
        "function NAME(n: number): number {\n  const fetch_rows = (m: number): number => m ? NAME(0) : 3;\n"
        "  return n ? fetch_rows(1) : 4;\n}\n",
        None,
    ),
    # a shorthand pattern binds the name; a shorthand value reads it
    (
        "m.js",
        "function NAME(n) {\n  if (n) { const { fetch_rows } = lib; return NAME(n - 1); }\n"
        "  return 3;\n}\n",
        "loose",
    ),
    (
        "m.ts",
        "function NAME(n: number, { fetch_rows }: any): number {\n"
        "  return n ? NAME(n - 1, lib) : 3;\n}\n",
        "loose",
    ),
    (
        "m.rs",
        "fn NAME(n: u32, s: S) -> u32 {\n    let S { fetch_rows } = s;\n"
        "    if n > 0 { return NAME(n - 1, s); }\n    3\n}\n",
        "loose",
    ),
    ("m.js", "function NAME(n) {\n  return n ? { fetch_rows } : NAME(1);\n}\n", "loose"),
    ("m.js", "function NAME(n) {\n  return n ? { NAME } : NAME(1);\n}\n", "exact"),
    ("m.rs", "fn NAME() -> S {\n    S { NAME }\n}\n", "exact"),
]


@pytest.mark.parametrize(("path", "program", "expected"), _RENAMES)
def test_a_rename_matches_only_as_far_as_the_names_are_known(
    path: str, program: str, expected: str | None
) -> None:
    before = program.replace("NAME", "fetch_rows")
    after = program.replace("NAME", "load_rows")
    assert _match(path, before, after) == expected


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        # a nested function or a local of that name: the calls meant it before
        (
            "m.py",
            "def fetch_rows(n):\n    def fetch_rows(m):\n        return m\n    return fetch_rows(n)\n",
            "def load_rows(n):\n    def fetch_rows(m):\n        return m\n    return load_rows(n)\n",
        ),
        (
            "m.ts",
            "function fetch_rows(n: number): number {\n  if (n) { var fetch_rows = 1; }\n  return fetch_rows(n - 1);\n}\n",
            "function load_rows(n: number): number {\n  if (n) { var fetch_rows = 1; }\n  return load_rows(n - 1);\n}\n",
        ),
        # a method's bare call is not the method in Python
        (
            "m.py",
            "class C:\n    def fetch_rows(self):\n        return fetch_rows()\n",
            "class C:\n    def load_rows(self):\n        return load_rows()\n",
        ),
    ],
)
def test_a_call_the_name_may_rebind_is_never_an_exact_rename(
    path: str, before: str, after: str
) -> None:
    assert _match(path, before, after) == "loose"


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("m.cpp", "int anchor() { return 1; } int NAME() { return R; }\n"),
        ("m.ts", "function anchor() { return 1; } function NAME() { return R; }\n"),
        ("m.go", "package m\nfunc anchor() int { return 1 }; func NAME() int { return R }\n"),
        ("m.go", "package m\ntype ( Anchor = int; NAME = [R]int; Tail = int )\n"),
        ("m.java", "public @interface Anchor {} @interface NAME { int value() default R; }\n"),
    ],
)
def test_a_shape_is_the_declaration_that_names_the_symbol(path: str, line: str) -> None:
    def program(name: str, value: int) -> str:
        return line.replace("NAME", name).replace("R", str(value))

    assert _match(path, program("fetch_rows", 3), program("load_rows", 3)) == "exact"
    assert _match(path, program("fetch_rows", 3), program("load_rows", 4)) is None


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        # the last line indented into the `if`: False now returns None
        (
            "m.py",
            "def fetch_rows(flag):\n    if flag:\n        return 3\n    return 4\n",
            "def load_rows(flag):\n    if flag:\n        return 3\n        return 4\n",
        ),
        # a newline after `return` ends the statement: undefined
        (
            "m.ts",
            "function fetch_rows() {\n  return 3;\n}\n",
            "function load_rows() {\n  return\n  3;\n}\n",
        ),
        (
            "m.js",
            "function fetch_rows() {\n  return 3;\n}\n",
            "function load_rows() {\n  return\n  3;\n}\n",
        ),
    ],
)
def test_the_same_tokens_nested_another_way_are_not_a_rename(
    path: str, before: str, after: str
) -> None:
    assert _match(path, before, after) is None


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "m.py",
            "def fetch_rows(flag):\n    if flag:\n        return 3\n    return 4\n",
            "def load_rows(flag):\n  if flag:\n    return 3\n  return 4\n",
        ),
        (
            "m.py",
            "def fetch_rows(n):\n    return (1 +\n            2)\n",
            "def load_rows(n):\n    return (1 + 2)\n",
        ),
        (
            "m.ts",
            "function fetch_rows() {\n  return 3;\n}\n",
            "function load_rows() { return 3; }\n",
        ),
        ("m.cpp", "int fetch_rows() {\n  return 3;\n}\n", "int load_rows() { return 3; }\n"),
        (
            "m.go",
            "package m\nfunc fetch_rows() int {\n\treturn 3\n}\n",
            "package m\nfunc load_rows() int { return 3 }\n",
        ),
    ],
)
def test_a_rename_reformatted_is_still_exact(path: str, before: str, after: str) -> None:
    assert _match(path, before, after) == "exact"


@pytest.mark.parametrize(
    "src",
    [
        "int fetch_rows(int n) { return 1; } int fetch_rows(double n) { return 3; }\n",
        "namespace A { int fetch_rows() { return 1; } } namespace B { int fetch_rows() { return 3; } }\n",
    ],
)
def test_namesakes_on_one_line_have_no_shape(src: str) -> None:
    # a line and a name cannot tell overloads or two scopes apart: never a rename
    decls = Declarations.parse("m.cpp", src.encode())
    assert decls is not None
    assert _shape(decls, 1, "fetch_rows") == NO_SHAPE


def test_a_qualified_call_on_the_line_is_not_a_namesake() -> None:
    line = "int unrelated() { return backend::fetch_rows(); } int NAME() { return 3; }\n"
    before, after = line.replace("NAME", "fetch_rows"), line.replace("NAME", "load_rows")
    decls = Declarations.parse("m.cpp", before.encode())
    assert decls is not None
    one = _shape(decls, 1, "fetch_rows")
    assert one[0]
    after_decls = Declarations.parse("m.cpp", after.encode())
    assert after_decls is not None
    assert _shape(after_decls, 1, "load_rows")[0] == one[0]


def test_namesakes_on_one_line_are_told_apart_once() -> None:
    # every one of a line's namesakes asks; the answer is read once, not once each
    def asked(n: int) -> int:
        src = "".join(f"namespace N{i} {{ int fetch_rows() {{ return {i}; }} }} " for i in range(n))
        decls = Declarations.parse("m.cpp", src.encode())
        assert decls is not None
        return steps(lambda: all(_shape(decls, 1, "fetch_rows") == NO_SHAPE for _ in range(n)))

    assert asked(800) / asked(100) < 12


def test_overloads_on_their_own_lines_each_have_their_shape() -> None:
    src = "int fetch_rows(int n) { return 1; }\nint fetch_rows(double n) { return 3; }\n"
    decls = Declarations.parse("m.cpp", src.encode())
    assert decls is not None
    one, two = _shape(decls, 1, "fetch_rows"), _shape(decls, 2, "fetch_rows")
    assert one[0] and two[0] and one != two


def test_a_function_named_inside_its_binding_is_one_declaration() -> None:
    src = "export const Icon = memo(function Icon(): number { return 3; });\n"
    decls = Declarations.parse("m.tsx", src.encode())
    assert decls is not None
    assert all(_shape(decls, 1, "Icon")[1:])


def _growth(program: Any, path: str = "m.py") -> float:
    """How much more work a shape takes for 8x the input: about 8 when linear, 64 when
    quadratic."""

    def work(n: int) -> int:
        decls = Declarations.parse(path, program(n).encode())
        assert decls is not None
        return steps(lambda: all(_shape(decls, 1, "fetch_rows")[1:]))

    return work(4000) / work(500)


@pytest.mark.parametrize(
    "line",
    [
        "    x{i} = fetch_rows(n - {i})\n",  # self references
        "    fetch_rows{i} = 1\n    def fetch_rows(m{i}):\n        return m{i}\n",  # bindings
    ],
)
def test_a_shape_is_linear_in_the_definition(line: str) -> None:
    def program(n: int) -> str:
        body = "".join(line.format(i=i) for i in range(n))
        return f"def fetch_rows(n):\n    if n < 1:\n        return 0\n{body}    return 0\n"

    assert _growth(program) < 12


def test_a_shape_with_many_aliased_imports_is_linear() -> None:
    def program(n: int) -> str:
        aliases = ", ".join(f"fetch_rows as f{i}" for i in range(n))
        return f"def fetch_rows(n):\n    from backend import {aliases}\n    return n\n"

    assert _growth(program) < 12


@pytest.mark.parametrize(
    ("path", "program"),
    [
        ("m.py", lambda n: "def fetch_rows(data):\n    return data" + ".fetch_rows" * n + "\n"),
        ("m.cpp", lambda n: "int fetch_rows() {\n    return a" + "::fetch_rows" * n + ";\n}\n"),
        # the whole definition on one line
        ("m.py", lambda n: "def fetch_rows(data): return data" + ".fetch_rows" * n + "\n"),
        ("m.cpp", lambda n: "int fetch_rows() { return a" + "::fetch_rows" * n + "; }\n"),
    ],
)
def test_a_qualified_member_chain_is_linear(path: str, program: Any) -> None:
    assert _growth(program, path) < 12


@pytest.mark.parametrize(
    ("path", "src", "line", "text"),
    [
        (
            "m.cpp",
            "int a() { return 1; } int fetch_rows() { return 3; }\n",
            1,
            "int fetch_rows() { return 3; }",
        ),
        (
            "m.py",
            "@cache\ndef fetch_rows():\n    return 3\nx = 1\n",
            2,
            "@cache\ndef fetch_rows():\n    return 3",
        ),
        (
            "m.cpp",
            "int fetch_rows(int) { return 1; } int fetch_rows(double) { return 3; }\n",
            1,
            None,
        ),
    ],
)
def test_a_declarations_text_is_its_own_bytes(
    path: str, src: str, line: int, text: str | None
) -> None:
    decls = Declarations.parse(path, src.encode())
    assert decls is not None
    assert _text(decls, line, "fetch_rows") == text


@pytest.mark.parametrize("path", ["m.js", "m.ts"])
@pytest.mark.parametrize(
    "line",
    [
        "const NAME = () => 3, unrelated = () => R;\n",
        "export const unrelated = () => R, NAME = () => 3;\n",
    ],
)
def test_a_sibling_binding_is_not_part_of_the_declaration(path: str, line: str) -> None:
    def program(name: str, value: int) -> str:
        return line.replace("NAME", name).replace("R", str(value))

    one, two = (Declarations.parse(path, program("fetch_rows", v).encode()) for v in (3, 4))
    assert one is not None and two is not None
    assert _text(one, 1, "fetch_rows") == _text(two, 1, "fetch_rows")
    assert "unrelated" not in (_text(one, 1, "fetch_rows") or "unrelated")
    assert _match(path, program("fetch_rows", 3), program("load_rows", 4)) == "exact"


@pytest.mark.parametrize("path", ["m.js", "m.ts"])
def test_a_binding_on_a_later_line_keeps_its_keyword_and_body(path: str) -> None:
    src = "export const unrelated = () => 3,\n  fetch_rows = (n) => (n ? fetch_rows(n - 1) : 3);\n"
    decls = Declarations.parse(path, src.encode())
    assert decls is not None
    assert _text(decls, 2, "fetch_rows") == (
        "export const\n  fetch_rows = (n) => (n ? fetch_rows(n - 1) : 3);"
    )
    assert decls.start(2) == 1
    assert _match(path, src, src.replace("fetch_rows", "load_rows")) == "exact"
    let = Declarations.parse(path, src.replace("const", "let").encode())
    assert let is not None and _text(let, 2, "fetch_rows") != _text(decls, 2, "fetch_rows")


@pytest.mark.parametrize("path", ["m.js", "m.ts"])
@pytest.mark.parametrize(
    "line",
    [
        "const NAME = () => 3,\n  unrelated = () => R /* unrelated returns R */;\n",
        "const NAME = () => 3, /* unrelated returns R */ unrelated = () => R;\n",
    ],
)
def test_a_siblings_comments_go_with_it(path: str, line: str) -> None:
    def program(name: str, value: int) -> str:
        return line.replace("NAME", name).replace("R", str(value))

    one, two = (Declarations.parse(path, program("fetch_rows", v).encode()) for v in (3, 4))
    assert one is not None and two is not None
    assert (
        _text(one, 1, "fetch_rows") == _text(two, 1, "fetch_rows") == "const fetch_rows = () => 3;"
    )
    assert _match(path, program("fetch_rows", 3), program("load_rows", 4)) == "exact"


def test_a_bindings_own_comment_is_part_of_it() -> None:
    src = "const unrelated = () => 3, /* rows */ fetch_rows = () => 3;\n"
    decls = Declarations.parse("m.ts", src.encode())
    assert decls is not None
    assert _text(decls, 1, "fetch_rows") == "const /* rows */ fetch_rows = () => 3;"


@pytest.mark.parametrize(
    "line",
    [
        "int & Reader::NAME() { return x; }",
        "T && Reader::NAME() { return x; }",
        "const T & Reader::NAME() const { return x; }",
        "int * Reader::NAME() { return x; }",
    ],
)
def test_a_cpp_definition_returning_a_reference_has_a_declaration(line: str) -> None:
    src = "namespace DB {\n" + line + "\n}\n"
    decls = Declarations.parse("m.cpp", src.replace("NAME", "fetchRows").encode())
    assert decls is not None and _text(decls, 2, "fetchRows")
    assert (
        _match("m.cpp", src.replace("NAME", "fetch_rows"), src.replace("NAME", "load_rows"))
        == "exact"
    )


@pytest.mark.parametrize("name", ["~Reader", "operator()"])
def test_a_destructor_or_operator_has_a_declaration(name: str) -> None:
    src = "struct Reader {\n  ~Reader() { close(); }\n  int operator()(int a) { return a; }\n};\n"
    decls = Declarations.parse("m.cpp", src.encode())
    assert decls is not None
    line = 2 if name.startswith("~") else 3
    assert _text(decls, line, name) and _shape(decls, line, name).exact


@pytest.mark.parametrize(
    ("path", "grammar"),
    [
        ("m.pyw", "python"),
        ("m.ipynb", "python"),
        ("m.c", "cpp"),
        ("m.tsx", "tsx"),
        ("m.mjs", "javascript"),
        ("m.hh", None),  # the index reads it as text
        ("m.vue", None),
    ],
)
def test_a_grammar_follows_the_language_the_index_parses_a_path_as(
    path: str, grammar: str | None
) -> None:
    from server.code.domain.declaration import _grammar

    assert _grammar(path) == grammar
