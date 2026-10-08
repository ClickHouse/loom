"""A bare Python call is never the enclosing class's method.

Python has no implicit `self`: `merge(...)` inside `DataFrame.join` is the
`merge` the file imported (or a module-level def), not `DataFrame.merge`.
Binding it to the method at 0.8 would rank a wrong edge above the imported
function one rung below.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_python")

from tests.unit.test_code_py_oracle_fixes import _edges

FILES = {
    "pkg/reshape.py": b"def merge(left, right):\n    return left\n",
    "pkg/frame.py": (
        b"from pkg.reshape import merge\n\n"
        b"class Frame:\n"
        b"    def merge(self, other):\n"
        b"        return merge(self, other)\n"  # line 5: the imported function
        b"\n"
        b"    def join(self, other):\n"
        b"        joined = merge(self, other)\n"  # line 8: the imported function
        b"        return self.merge(joined)\n"  # line 9: the method, through self
    ),
}


def test_a_bare_call_binds_the_import_not_the_enclosing_classs_namesake() -> None:
    edges, misses = _edges(FILES, "pkg/frame.py")
    by_line = {line: (target, conf) for line, target, conf in edges}
    assert by_line[5] == ("pkg.reshape::merge", 1.0)
    assert by_line[8] == ("pkg.reshape::merge", 1.0)
    assert by_line[9] == ("pkg.frame::Frame::merge", 0.8)
    assert not [m for m in misses if m.callee_name == "merge"]


def test_without_the_import_a_bare_call_names_no_method() -> None:
    """No import, no module-level `merge`: the class's method is not a
    candidate for a bare call, so the site abstains (repo-wide it is the
    one `merge` — but a method is never a bare call's target)."""
    files = {"pkg/frame.py": FILES["pkg/frame.py"].replace(b"from pkg.reshape import merge\n", b"")}
    edges, _misses = _edges(files, "pkg/frame.py")
    assert {t for line, t, _c in edges if line in (4, 7)} == set()
    assert {t for line, t, _c in edges if line == 8} == {"pkg.frame::Frame::merge"}
