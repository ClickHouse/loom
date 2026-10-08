"""scip_to_delta.py's command line over real, tiny SCIP indexes built with the vendored
bindings: a full conversion, an incremental window, a rename, a delete, an empty
window, a module below the repository root, and the exit codes the harness relies on.
The workers read the `--dry-run` file, so these tests pin its shape."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "scripts" / "scip_harness"
_CONVERTER = _HARNESS / "scip_to_delta.py"


def _pb():
    spec = importlib.util.spec_from_file_location("scip_pb2", _HARNESS / "vendor" / "scip_pb2.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("scip_pb2", mod)
    spec.loader.exec_module(mod)
    return sys.modules["scip_pb2"]


def _fn(name: str, disamb: str) -> str:
    return f"scip-clang cxx . . DB/{name}({disamb})."


def _add_doc(index, path: str, defs: list[tuple[str, int]], refs: list[tuple[str, int]]) -> None:
    """One document: a definition occurrence per (symbol, 0-based line), a reference
    occurrence per (symbol, 0-based line)."""
    pb = _pb()
    doc = index.documents.add()
    doc.relative_path = path
    for symbol, line in defs:
        occ = doc.occurrences.add()
        occ.symbol = symbol
        occ.symbol_roles = pb.SymbolRole.Definition
        occ.range.extend([line, 5, 6])
    for symbol, line in refs:
        occ = doc.occurrences.add()
        occ.symbol = symbol
        occ.range.extend([line, 4, 5])


def _index(tmp_path: Path, name: str = "index.scip") -> Path:
    """a.cpp defines f and g, g calls f; b.cpp defines h, which calls f."""
    pb = _pb()
    index = pb.Index()
    index.metadata.project_root = "file:///nonexistent-checkout"
    _add_doc(index, "src/b.cpp", [(_fn("h", "cc"), 0)], [(_fn("f", "aa"), 1)])
    _add_doc(index, "src/a.cpp", [(_fn("f", "aa"), 0), (_fn("g", "bb"), 2)], [(_fn("f", "aa"), 3)])
    out = tmp_path / name
    out.write_bytes(index.SerializeToString())
    return out


def _lines(tmp_path: Path, name: str, paths: list[str]) -> Path:
    p = tmp_path / name
    p.write_text("".join(f"{x}\n" for x in paths))
    return p


def _convert(tmp_path: Path, index: Path, *extra: str) -> tuple[dict, str]:
    out = tmp_path / "delta.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(_CONVERTER),
            "--index",
            str(index),
            "--commit",
            "h" * 40,
            "--source-root",
            "",
            *extra,
            "--dry-run",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text()), proc.stdout


def _names(delta: dict) -> dict[str, list[str]]:
    return {f["path"]: [s["qualified_name"] for s in f["symbols"]] for f in delta["files"]}


def _calls(delta: dict) -> set[tuple[str, str, str, int]]:
    return {
        (f["path"], e["from_qualified_name"], e["to_qualified_name"], e["line"])
        for f in delta["files"]
        for e in f["edges"]
        if e["edge_kind"] == "calls"
    }


def test_a_full_conversion_carries_every_document(tmp_path: Path) -> None:
    delta, stdout = _convert(tmp_path, _index(tmp_path), "--full")
    assert delta["delta_version"] == 1 and delta["commit"] == "h" * 40 and delta["base"] == ""
    assert _names(delta) == {"src/a.cpp": ["DB::f", "DB::g"], "src/b.cpp": ["DB::h"]}
    assert _calls(delta) == {("src/a.cpp", "DB::g", "DB::f", 4), ("src/b.cpp", "DB::h", "DB::f", 2)}
    assert delta["deleted_files"] == []
    # without a source root there is no constructor claim for the namespace
    assert "features" not in delta
    assert all(not f["deleted"] for f in delta["files"])
    assert stdout.splitlines()[0].startswith("delta: 2 files, 3 symbols, 2 edges")


def test_an_incremental_window_carries_only_the_changed_documents(tmp_path: Path) -> None:
    changed = _lines(tmp_path, "changed.txt", ["src/b.cpp"])
    delta, _ = _convert(
        tmp_path, _index(tmp_path), "--base", "b" * 40, "--changed-files", str(changed)
    )
    assert delta["base"] == "b" * 40
    assert _names(delta) == {"src/b.cpp": ["DB::h"]}
    assert _calls(delta) == {("src/b.cpp", "DB::h", "DB::f", 2)}


def test_a_rename_is_the_new_document_plus_the_old_path_deleted(tmp_path: Path) -> None:
    changed = _lines(tmp_path, "changed.txt", ["src/a.cpp"])
    deleted = _lines(tmp_path, "deleted.txt", ["src/old_a.cpp"])
    delta, _ = _convert(
        tmp_path,
        _index(tmp_path),
        "--base",
        "b" * 40,
        "--changed-files",
        str(changed),
        "--deleted-files",
        str(deleted),
    )
    assert _names(delta) == {"src/a.cpp": ["DB::f", "DB::g"]}
    assert delta["deleted_files"] == ["src/old_a.cpp"]


def test_a_delete_only_window_tombstones_the_path(tmp_path: Path) -> None:
    changed = _lines(tmp_path, "changed.txt", [])
    deleted = _lines(tmp_path, "deleted.txt", ["src/gone.cpp", ""])
    delta, stdout = _convert(
        tmp_path,
        _index(tmp_path),
        "--base",
        "b" * 40,
        "--changed-files",
        str(changed),
        "--deleted-files",
        str(deleted),
    )
    assert delta["files"] == [] and delta["deleted_files"] == ["src/gone.cpp"]
    assert stdout.startswith("delta: 0 files, 0 symbols, 0 edges")


def test_an_empty_window_is_an_empty_delta(tmp_path: Path) -> None:
    changed = _lines(tmp_path, "changed.txt", [])
    delta, _ = _convert(
        tmp_path, _index(tmp_path), "--base", "b" * 40, "--changed-files", str(changed)
    )
    assert delta["files"] == [] and delta["deleted_files"] == []


def test_an_empty_index_is_an_empty_delta(tmp_path: Path) -> None:
    empty = tmp_path / "empty.scip"
    empty.write_bytes(b"")
    delta, _ = _convert(tmp_path, empty, "--full")
    assert delta["files"] == [] and delta["deleted_files"] == []


def test_a_module_below_the_root_gets_repository_relative_paths(tmp_path: Path) -> None:
    changed = _lines(tmp_path, "changed.txt", ["svc/src/a.cpp", "elsewhere/x.cpp"])
    deleted = _lines(tmp_path, "deleted.txt", ["svc/src/gone.cpp"])
    delta, _ = _convert(
        tmp_path,
        _index(tmp_path),
        "--path-prefix",
        "svc/",
        "--changed-files",
        str(changed),
        "--deleted-files",
        str(deleted),
    )
    assert list(_names(delta)) == ["svc/src/a.cpp"]
    assert delta["deleted_files"] == ["svc/src/gone.cpp"]


def test_upload_mode_without_a_url_or_token_exits_2(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(_CONVERTER),
            "--index",
            str(_index(tmp_path)),
            "--commit",
            "h" * 40,
            "--source-root",
            "",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2 and "--loom-url and --token required" in proc.stderr


@pytest.mark.parametrize("flag", ["--index", "--commit"])
def test_a_missing_required_argument_is_a_usage_error(tmp_path: Path, flag: str) -> None:
    args = {"--index": str(_index(tmp_path)), "--commit": "h" * 40}
    args.pop(flag)
    proc = subprocess.run(
        [sys.executable, str(_CONVERTER), *[x for kv in args.items() for x in kv]],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2 and flag in proc.stderr


def test_the_delta_is_sorted_by_path_whatever_order_the_indexer_wrote(tmp_path: Path) -> None:
    """A parallel indexer writes documents in completion order; the delta does not
    follow it. Two indexes that differ only in document order give the same bytes."""
    pb = _pb()
    shuffled = pb.Index()
    shuffled.ParseFromString(_index(tmp_path).read_bytes())
    docs = list(shuffled.documents)
    shuffled.ClearField("documents")
    shuffled.documents.extend(reversed(docs))
    other = tmp_path / "reversed.scip"
    other.write_bytes(shuffled.SerializeToString())
    deleted = _lines(tmp_path, "deleted.txt", ["src/z.cpp", "src/y.cpp", "src/z.cpp"])

    first, _ = _convert(tmp_path, _index(tmp_path), "--full", "--deleted-files", str(deleted))
    first_bytes = (tmp_path / "delta.json").read_bytes()
    second, _ = _convert(tmp_path, other, "--full", "--deleted-files", str(deleted))
    assert [f["path"] for f in first["files"]] == ["src/a.cpp", "src/b.cpp"]
    assert first["deleted_files"] == ["src/y.cpp", "src/z.cpp"]
    assert (tmp_path / "delta.json").read_bytes() == first_bytes
    assert second == first


def test_a_failed_conversion_leaves_no_delta_file(tmp_path: Path) -> None:
    """The workers read "no file" as "nothing changed in the window": a run that fails
    part-way must not leave a truncated delta behind."""
    broken = tmp_path / "broken.scip"
    broken.write_bytes(_index(tmp_path).read_bytes()[:-7])
    out = tmp_path / "delta.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(_CONVERTER),
            "--index",
            str(broken),
            "--commit",
            "h" * 40,
            "--source-root",
            "",
            "--full",
            "--dry-run",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["broken.scip", "index.scip"]


_FACTORY_SRC = (
    "void use() {\n  auto p = std::make_shared<Foo>(1);\n}\nstruct Foo {\n  Foo(int);\n};\n"
)


def _factory_index(tmp_path: Path, scheme: str) -> Path:
    """One document whose line 2 constructs Foo through `make_shared<Foo>`, and whose
    type Foo has a method named Foo — a constructor's shape in any scheme."""
    pb = _pb()
    head = {"cxx": "cxx . . $", "scip-python": "scip-python python pkg 0.1"}.get(
        scheme, f"{scheme} gomod example.com/m v1"
    )
    index = pb.Index()
    doc = index.documents.add()
    doc.relative_path = "src/a.cpp"
    col = _FACTORY_SRC.splitlines()[1].index("Foo")
    for symbol, rng, role in (
        (f"{head} DB/use(aa).", [0, 5, 8], pb.SymbolRole.Definition),
        (f"{head} std/make_shared(bb).", [1, 16, 27], 0),
        (f"{head} DB/Foo#", [1, col, col + 3], 0),
        (f"{head} DB/Foo#", [3, 7, 10], pb.SymbolRole.Definition),
        (f"{head} DB/Foo#Foo(cc).", [4, 2, 5], pb.SymbolRole.Definition),
    ):
        occ = doc.occurrences.add()
        occ.symbol, occ.symbol_roles = symbol, role
        occ.range.extend(rng)
    out = tmp_path / f"{scheme}.scip"
    out.write_bytes(index.SerializeToString())
    return out


@pytest.mark.parametrize(
    ("scheme", "claims"), [("cxx", True), ("scip-python", False), ("scip-go", False)]
)
def test_only_a_scip_clang_index_claims_constructor_calls(
    tmp_path: Path, scheme: str, claims: bool
) -> None:
    """`ctor_calls` says every constructor call through a C++ factory is in the delta.
    Only scip-clang indexes carry that form: another scheme computes no factory edge,
    even for a line shaped like one, and claims nothing for the namespace."""
    root = tmp_path / "checkout"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.cpp").write_text(_FACTORY_SRC)
    out = tmp_path / "delta.json"
    subprocess.run(
        [
            sys.executable,
            str(_CONVERTER),
            "--index",
            str(_factory_index(tmp_path, scheme)),
            "--commit",
            "h" * 40,
            "--full",
            "--source-root",
            str(root),
            "--dry-run",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    delta = json.loads(out.read_text())
    factory = [
        e
        for f in delta["files"]
        for e in f["edges"]
        if e["edge_kind"] == "calls" and e["to_qualified_name"].endswith("Foo::Foo")
    ]
    assert (delta.get("features") == ["ctor_calls"]) is claims
    assert (delta["stats"]["factory_ctor_edges"] == 1) is claims
    assert bool(factory) is claims
