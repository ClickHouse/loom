"""The tier harnesses in the workers' output-file mode, over a real git window, a stub
indexer and the real converter: a modified file is re-read, a deleted or renamed-away
file is tombstoned, and a window with nothing for the tier writes no delta, and neither
it nor a window with only deletions runs the indexer. The C/C++ harness runs against a
stub `cmake`: the tests check when it configures or reconfigures the compile database,
and that a failed configure fails the run."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from server.code.domain.index.tier_health import classify

_HARNESS = Path(__file__).resolve().parents[2] / "scripts" / "scip_harness"

# the stub indexer logs its call and writes the index the test built
_FAKE_INDEXER = """#!/usr/bin/env bash
echo "indexer $*" >>"$CALLS"
while [[ $# -gt 0 ]]; do [[ "$1" == "--output" ]] && cp "$FAKE_INDEX" "$2"; shift; done
"""

_TIERS = {
    "python": ("run_python_delta.sh", "SCIP_PYTHON", "py"),
    "typescript": ("run_typescript_delta.sh", "SCIP_TYPESCRIPT", "ts"),
    "go": ("run_go_monorepo_delta.sh", "SCIP_GO", "go"),
}


def _pb():
    spec = importlib.util.spec_from_file_location("scip_pb2", _HARNESS / "vendor" / "scip_pb2.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("scip_pb2", mod)
    spec.loader.exec_module(mod)
    return sys.modules["scip_pb2"]


def _write_index(path: Path, docs: list[str]) -> None:
    pb = _pb()
    index = pb.Index()
    for i, rel in enumerate(docs):
        doc = index.documents.add()
        doc.relative_path = rel
        occ = doc.occurrences.add()
        occ.symbol = f"scip-clang cxx . . m/f{i}(0)."
        occ.symbol_roles = pb.SymbolRole.Definition
        occ.range.extend([0, 4, 6])
    path.write_bytes(index.SerializeToString())


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _commit_rename(repo: Path, old: str, new: str) -> str:
    _git(repo, "mv", old, new)
    return _commit(repo, f"rename {old} to {new}")


class _Window:
    def __init__(self, tmp_path: Path, tier: str) -> None:
        script, indexer_env, ext = _TIERS[tier]
        self.tmp, self.script, self.ext = tmp_path, _HARNESS / script, ext
        self.repo = tmp_path / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "tsconfig.json").write_text("{}")
        (self.repo / "go.mod").write_text("module example.com/m\n")
        for name in ("a", "b"):
            (self.repo / "src" / f"{name}.{ext}").write_text(f"x_{name} = 1\n")
        _git(tmp_path, "init", "-q", str(self.repo))
        self.base = _commit(self.repo, "init")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in (("indexer", _FAKE_INDEXER), ("go", "#!/usr/bin/env bash\n")):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.calls = tmp_path / "calls"
        self.index = tmp_path / "fake.scip"
        # the Go harness writes one delta per module; the root module's is `.json`
        out = {"DELTA_OUT_DIR": str(tmp_path / "out")} if tier == "go" else {}
        self.delta = tmp_path / "out" / ".json" if tier == "go" else tmp_path / "delta.json"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALLS": str(self.calls),
            "FAKE_INDEX": str(self.index),
            "DELTA_OUT": str(self.delta),
            **out,
            "PYTHON": sys.executable,
            indexer_env: str(bin_dir / "indexer"),
            "SCIP_TS_PATHS": "0",
            "WORKSPACES": "",
        }

    def path(self, name: str) -> str:
        return f"src/{name}.{self.ext}"

    def run(self, head: str, index_docs: list[str]) -> dict | None:
        _write_index(self.index, index_docs)
        subprocess.run(
            ["bash", str(self.script), str(self.repo), self.base, head],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(self.delta.read_text()) if self.delta.exists() else None

    @property
    def indexed(self) -> bool:
        return self.calls.exists() and "indexer" in self.calls.read_text()


@pytest.mark.parametrize("tier", sorted(_TIERS))
def test_a_modified_file_is_read_from_the_index(tmp_path: Path, tier: str) -> None:
    w = _Window(tmp_path, tier)
    (w.repo / w.path("a")).write_text("x_a = 2\n")
    head = _commit(w.repo, "edit a")
    delta = w.run(head, [w.path("a"), w.path("b")])
    assert delta is not None and w.indexed
    assert [f["path"] for f in delta["files"]] == [w.path("a")]
    assert delta["deleted_files"] == [] and delta["base"] == w.base


@pytest.mark.parametrize("tier", sorted(_TIERS))
def test_a_rename_tombstones_the_old_path(tmp_path: Path, tier: str) -> None:
    w = _Window(tmp_path, tier)
    _git(w.repo, "mv", w.path("a"), w.path("c"))
    head = _commit(w.repo, "rename a to c")
    delta = w.run(head, [w.path("b"), w.path("c")])
    assert delta is not None
    assert [f["path"] for f in delta["files"]] == [w.path("c")]
    assert delta["deleted_files"] == [w.path("a")]


@pytest.mark.parametrize("tier", sorted(_TIERS))
def test_a_non_ascii_name_is_indexed_and_retired_under_its_own_name(
    tmp_path: Path, tier: str
) -> None:
    # git C-quotes such a name by default ("src/caf\303\251.py"), which names
    # no file: the window must list it as the tree does.
    w = _Window(tmp_path, tier)
    new = f"src/café.{w.ext}"
    _git(w.repo, "mv", w.path("a"), new)
    head = _commit(w.repo, "rename a to café")
    delta = w.run(head, [w.path("b"), new])
    assert delta is not None and w.indexed
    assert [f["path"] for f in delta["files"]] == [new]
    assert delta["deleted_files"] == [w.path("a")]
    back = _commit_rename(w.repo, new, w.path("a"))
    w.base = head
    delta = w.run(back, [w.path("a"), w.path("b")])
    assert delta is not None and delta["deleted_files"] == [new]


@pytest.mark.parametrize("tier", sorted(_TIERS))
def test_a_delete_only_window_tombstones_without_indexing(tmp_path: Path, tier: str) -> None:
    w = _Window(tmp_path, tier)
    _git(w.repo, "rm", "-q", w.path("b"))
    head = _commit(w.repo, "delete b")
    delta = w.run(head, [w.path("a")])
    assert delta is not None and not w.indexed
    assert delta["files"] == [] and delta["deleted_files"] == [w.path("b")]


@pytest.mark.parametrize("tier", sorted(_TIERS))
def test_a_window_without_the_tiers_files_writes_no_delta(tmp_path: Path, tier: str) -> None:
    w = _Window(tmp_path, tier)
    (w.repo / "README.md").write_text("docs\n")
    head = _commit(w.repo, "docs only")
    assert w.run(head, [w.path("a")]) is None
    assert not w.indexed


_FAKE_SCIP_CLANG = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    --compdb-path) echo "compdb $(cat "$2")" >>"$CALLS" ;;
    --index-output-path) cp "$FAKE_INDEX" "$2" ;;
  esac
  shift
done
"""

# stub cmake: logs its call, fails under CMAKE_FAIL, else lists every src/*.cpp, .cc and .cxx
# in the compile database, as a configure over a glob does
_FAKE_CMAKE = """#!/usr/bin/env bash
echo "cmake $*" >>"$CMAKE_CALLS"
[[ -n "${CMAKE_FAIL:-}" ]] && { echo "CMake Error: a package is missing" >&2; exit 1; }
"$PYTHON" - <<'PYEOF'
import json, pathlib
root = pathlib.Path.cwd()
compdb = [{"directory": str(root), "file": str(p), "command": "c++"} for p in sorted(root.glob("src/*.c[cpx]*"))]
(root / "build" / "compile_commands.json").write_text(json.dumps(compdb))
PYEOF
"""


class _CppWindow:
    """run_delta.sh over a checkout with a compile_commands.json already configured:
    src/a.cpp and src/b.cpp, b.cpp includes src/h.h, and CMakeLists.txt builds them."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.cpp").write_text("int a() { return 1; }\n")
        (self.repo / "src" / "b.cpp").write_text('#include "h.h"\nint b() { return 2; }\n')
        (self.repo / "src" / "h.h").write_text("int h();\n")
        (self.repo / "CMakeLists.txt").write_text("project(x)\n")
        (self.repo / ".gitignore").write_text("build/\n")
        (self.repo / "build").mkdir()
        compdb = [
            {"directory": str(self.repo), "file": str(self.repo / "src" / n), "command": "c++"}
            for n in ("a.cpp", "b.cpp")
        ]
        (self.repo / "build" / "compile_commands.json").write_text(json.dumps(compdb))
        # a cache whose compilers the host has: the reconfigure tests keep it
        (self.repo / "build" / "CMakeCache.txt").write_text(
            "CMAKE_GENERATOR:INTERNAL=Ninja\n"
            "CMAKE_C_COMPILER:FILEPATH=/bin/sh\nCMAKE_CXX_COMPILER:FILEPATH=/bin/sh\n"
        )
        _git(tmp_path, "init", "-q", str(self.repo))
        self.base = _commit(self.repo, "init")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "scip-clang").write_text(_FAKE_SCIP_CLANG)
        (bin_dir / "scip-clang").chmod(0o755)
        (bin_dir / "cmake").write_text(_FAKE_CMAKE)
        (bin_dir / "cmake").chmod(0o755)
        self.calls = tmp_path / "calls"
        self.cmake_calls = tmp_path / "cmake_calls"
        self.index = tmp_path / "fake.scip"
        self.delta = tmp_path / "delta.json"
        self.env = {
            **os.environ,
            "CALLS": str(self.calls),
            "FAKE_INDEX": str(self.index),
            "DELTA_OUT": str(self.delta),
            "PYTHON": sys.executable,
            "SCIP_CLANG": str(bin_dir / "scip-clang"),
            "JOBS": "1",
            "CMAKE_CALLS": str(self.cmake_calls),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        }

    def run(self, head: str, index_docs: list[str]) -> dict | None:
        proc = self.run_unchecked(head, index_docs)
        assert proc.returncode == 0, proc.stdout
        return json.loads(self.delta.read_text()) if self.delta.exists() else None

    def run_unchecked(
        self, head: str = "", index_docs: list[str] | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        """One run, its output merged; no `head` is a full index."""
        _write_index(self.index, index_docs or [])
        window = [self.base, head] if head else ["--full", ""]
        return subprocess.run(
            ["bash", str(_HARNESS / "run_delta.sh"), str(self.repo), *window, "-"],
            env={**self.env, **env},
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    @property
    def cmake_runs(self) -> list[str]:
        return self.cmake_calls.read_text().splitlines() if self.cmake_calls.exists() else []

    @property
    def compdbs(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [
            sorted(Path(e["file"]).name for e in json.loads(line[len("compdb ") :]))
            for line in self.calls.read_text().splitlines()
        ]


def test_a_changed_header_indexes_the_translation_units_that_include_it(tmp_path: Path) -> None:
    w = _CppWindow(tmp_path)
    (w.repo / "src" / "h.h").write_text("int h(int);\n")
    head = _commit(w.repo, "edit header")
    delta = w.run(head, ["src/b.cpp", "src/h.h"])
    assert w.compdbs == [["b.cpp"]]
    # the includer re-read for the header is in the delta too, so its edges refresh
    assert delta is not None and [f["path"] for f in delta["files"]] == ["src/b.cpp", "src/h.h"]


def test_a_window_that_only_edits_a_translation_unit_keeps_the_compile_database(
    tmp_path: Path,
) -> None:
    w = _CppWindow(tmp_path)
    (w.repo / "src" / "a.cpp").write_text("int a() { return 3; }\n")
    head = _commit(w.repo, "edit a")
    w.run(head, ["src/a.cpp"])
    assert w.cmake_runs == [] and w.compdbs == [["a.cpp"]]


@pytest.mark.parametrize("name", ["c.cpp", "c.cc", "c.cxx"])
def test_a_translation_unit_added_after_the_configure_is_indexed(tmp_path: Path, name: str) -> None:
    """The compile database was configured before the file existed: the window that
    adds it reconfigures on the existing cache, so it is indexed, not dropped."""
    w = _CppWindow(tmp_path)
    (w.repo / "src" / name).write_text("int c() { return 3; }\n")
    head = _commit(w.repo, "add c")
    delta = w.run(head, [f"src/{name}"])
    assert w.cmake_runs == ["cmake -B build -S ."]
    assert w.compdbs == [[name]]
    assert delta is not None and [f["path"] for f in delta["files"]] == [f"src/{name}"]


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("CMakeLists.txt", "project(y)\n"),
        ("CMakeLists.txt", None),
        ("src/CMakeLists.txt", "set(X 1)\n"),
        ("cmake/flags.cmake", "set(X 1)\n"),
    ],
    ids=["change CMakeLists.txt", "delete CMakeLists.txt", "add src/CMakeLists.txt", "add .cmake"],
)
def test_a_window_that_edits_the_build_reconfigures(
    tmp_path: Path, path: str, content: str | None
) -> None:
    """`content` None deletes the file."""
    w = _CppWindow(tmp_path)
    if content is None:
        _git(w.repo, "rm", "-q", path)
    else:
        (w.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (w.repo / path).write_text(content)
    w.run(_commit(w.repo, f"edit {path}"), [])
    assert w.cmake_runs == ["cmake -B build -S ."]


def test_a_failed_reconfigure_fails_the_run_with_cmakes_last_lines(tmp_path: Path) -> None:
    """A build edit the host cannot configure fails the run, as a failed first configure
    does: no delta, CMake's last lines in the output, classified `cmake`."""
    w = _CppWindow(tmp_path)
    (w.repo / "src" / "c.cpp").write_text("int c() { return 3; }\n")
    proc = w.run_unchecked(_commit(w.repo, "add c"), CMAKE_FAIL="1")
    assert proc.returncode == 1 and not w.delta.exists() and w.compdbs == []
    assert "CMake Error: a package is missing" in proc.stdout
    assert classify(f"harness exit 1: {proc.stdout[-2000:]}") == "cmake"


def test_a_window_whose_configure_failed_indexes_what_it_adds_once_it_configures(
    tmp_path: Path,
) -> None:
    """The failed run moved no watermark, so the retried window still adds c.cpp."""
    w = _CppWindow(tmp_path)
    (w.repo / "src" / "c.cpp").write_text("int c() { return 3; }\n")
    head = _commit(w.repo, "add c")
    assert w.run_unchecked(head, CMAKE_FAIL="1").returncode == 1
    delta = w.run(head, ["src/c.cpp"])
    assert w.compdbs == [["c.cpp"]]
    assert delta is not None and [f["path"] for f in delta["files"]] == ["src/c.cpp"]


def test_a_full_index_whose_reconfigure_fails_fails(tmp_path: Path) -> None:
    w = _CppWindow(tmp_path)
    proc = w.run_unchecked(CMAKE_FAIL="1")
    assert proc.returncode == 1 and w.compdbs == []
    assert "cmake configure failed" in proc.stdout


@pytest.mark.parametrize(
    "cache",
    [
        None,
        "CMAKE_C_COMPILER:FILEPATH=/bin/sh\nCMAKE_CXX_COMPILER:FILEPATH=/nonexistent/clang++-18\n",
        "CMAKE_C_COMPILER:FILEPATH=/nonexistent/clang-18\nCMAKE_CXX_COMPILER:FILEPATH=/bin/sh\n",
    ],
    ids=["no cache", "missing C++ compiler", "missing C compiler"],
)
def test_a_cache_that_cannot_be_reconfigured_is_configured_from_scratch(
    tmp_path: Path, cache: str | None
) -> None:
    """No cache, or one naming a compiler the host no longer has: the pinned first
    configure, not a reconfigure that would fail on every run."""
    w = _CppWindow(tmp_path)
    if cache is None:
        (w.repo / "build" / "CMakeCache.txt").unlink()
    else:
        (w.repo / "build" / "CMakeCache.txt").write_text(cache)
    assert (
        w.run_unchecked(index_docs=["src/a.cpp", "src/b.cpp"], CC="cc", CXX="c++").returncode == 0
    )
    assert len(w.cmake_runs) == 1 and "-G Ninja" in w.cmake_runs[0]


def test_a_first_configure_without_clang_on_path_fails_classified_cmake(tmp_path: Path) -> None:
    """No CC/CXX and no clang on PATH: the run fails with the configure marker rather than
    letting CMake fall back to another compiler."""
    w = _CppWindow(tmp_path)
    (w.repo / "build" / "CMakeCache.txt").unlink()
    tools = tmp_path / "tools"
    tools.mkdir()
    for d in ("/usr/bin", "/bin"):
        for exe in Path(d).iterdir():
            if not exe.name.startswith("clang") and not (tools / exe.name).exists():
                (tools / exe.name).symlink_to(exe)
    env = {k: v for k, v in w.env.items() if k not in ("CC", "CXX")}
    env["PATH"] = f"{w.env['PATH'].split(os.pathsep)[0]}{os.pathsep}{tools}"
    w.env = env
    proc = w.run_unchecked()
    assert proc.returncode == 1 and w.cmake_runs == []
    assert "no clang on PATH" in proc.stdout
    assert classify(f"harness exit 1: {proc.stdout[-2000:]}") == "cmake"


def test_a_first_configure_is_not_followed_by_a_reconfigure(tmp_path: Path) -> None:
    w = _CppWindow(tmp_path)
    (w.repo / "build" / "compile_commands.json").unlink()
    assert (
        w.run_unchecked(index_docs=["src/a.cpp", "src/b.cpp"], CC="cc", CXX="c++").returncode == 0
    )
    assert len(w.cmake_runs) == 1 and "-G Ninja" in w.cmake_runs[0]
    assert w.compdbs == [["a.cpp", "b.cpp"]]


def test_a_full_index_reconfigures_before_it_reads_the_compile_database(tmp_path: Path) -> None:
    w = _CppWindow(tmp_path)
    (w.repo / "src" / "c.cpp").write_text("int c() { return 3; }\n")
    _commit(w.repo, "add c")
    assert w.run_unchecked(index_docs=["src/a.cpp", "src/b.cpp", "src/c.cpp"]).returncode == 0
    assert w.cmake_runs == ["cmake -B build -S ."]
    assert w.compdbs == [["a.cpp", "b.cpp", "c.cpp"]]


def test_a_cpp_delete_only_window_tombstones_without_indexing(tmp_path: Path) -> None:
    w = _CppWindow(tmp_path)
    _git(w.repo, "rm", "-q", "src/a.cpp")
    head = _commit(w.repo, "delete a")
    delta = w.run(head, ["src/b.cpp"])
    assert w.compdbs == []
    assert delta is not None
    assert delta["files"] == [] and delta["deleted_files"] == ["src/a.cpp"]


def test_a_cpp_rename_tombstones_the_old_path(tmp_path: Path) -> None:
    w = _CppWindow(tmp_path)
    _git(w.repo, "mv", "src/b.cpp", "src/c.cpp")
    head = _commit(w.repo, "rename b to c")
    # the rename adds c.cpp, so the window reconfigures and indexes it
    delta = w.run(head, ["src/c.cpp"])
    assert w.cmake_runs == ["cmake -B build -S ."] and w.compdbs == [["c.cpp"]]
    assert delta is not None and delta["deleted_files"] == ["src/b.cpp"]


def test_a_cpp_file_with_a_non_ascii_name_is_retired_under_its_own_name(tmp_path: Path) -> None:
    w = _CppWindow(tmp_path)
    (w.repo / "src" / "größe.cpp").write_text("int g() { return 3; }\n")
    w.base = _commit(w.repo, "add größe.cpp")
    _git(w.repo, "rm", "-q", "src/größe.cpp")
    head = _commit(w.repo, "delete größe.cpp")
    delta = w.run(head, [])
    assert delta is not None and delta["deleted_files"] == ["src/größe.cpp"]


def test_a_window_without_cpp_changes_writes_no_delta(tmp_path: Path) -> None:
    w = _CppWindow(tmp_path)
    (w.repo / "README.md").write_text("docs\n")
    head = _commit(w.repo, "docs only")
    assert w.run(head, []) is None and w.compdbs == []


_UNKNOWN_BASE = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("tier", [*sorted(_TIERS), "cpp"])
def test_an_unknown_base_exits_3_and_writes_no_delta(tmp_path: Path, tier: str) -> None:
    """A base the checkout does not have (rewritten history, a pruned mirror) is not an
    empty window: the harness exits EXIT_BASE_NOT_FOUND, indexes nothing, writes no
    delta, and says to rerun as a full index."""
    w = _CppWindow(tmp_path) if tier == "cpp" else _Window(tmp_path, tier)
    script = _HARNESS / "run_delta.sh" if tier == "cpp" else w.script
    head = _git(w.repo, "rev-parse", "HEAD")
    extra = ["-"] if tier == "cpp" else []
    proc = subprocess.run(
        ["bash", str(script), str(w.repo), _UNKNOWN_BASE, head, *extra],
        env=w.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 3, proc.stderr
    assert f"base commit {_UNKNOWN_BASE} not found" in proc.stderr
    assert "rerun as a full index" in proc.stderr
    assert not w.delta.exists() and not w.calls.exists()
