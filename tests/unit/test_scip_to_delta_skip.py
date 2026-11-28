"""A document that fails to decode or convert is skipped, not fatal: it is named once on
stderr, listed in the delta's `skipped_files` and counted in its stats and summary line,
and the rest of the index converts. `--stop-on-failed-document` (the harness's
SCIP_STOP_ON_FAILED_DOCUMENT=1) fails the run on it instead. An index whose records
cannot be read to the end still fails (test_scip_to_delta_cli.py's
`test_a_failed_conversion_leaves_no_delta_file`)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_scip_to_delta_cli import _CONVERTER, _add_doc, _fn, _pb

_MARK = b"ZZBADUTF8ZZ"
_LIB = _CONVERTER.parent / "lib.sh"


def _index_with(tmp_path: Path, bad_symbol: str) -> Path:
    """src/a.cpp and src/bad.cpp, each defining one function."""
    pb = _pb()
    index = pb.Index()
    index.metadata.project_root = "file:///nonexistent-checkout"
    _add_doc(index, "src/a.cpp", [(_fn("f", "aa"), 0)], [])
    _add_doc(index, "src/bad.cpp", [(_fn(bad_symbol, "bb"), 0)], [])
    out = tmp_path / "index.scip"
    out.write_bytes(index.SerializeToString())
    return out


def _bad_utf8_index(tmp_path: Path) -> Path:
    index = _index_with(tmp_path, _MARK.decode())
    raw = index.read_bytes()
    assert raw.count(_MARK) == 1
    # same length, so every length prefix still holds; python-protobuf refuses the string
    index.write_bytes(raw.replace(_MARK, b"\xff\xfe" + _MARK[2:]))
    return index


def _run(index: Path, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_CONVERTER), "--index", str(index), "--commit", "h" * 40]
        + ["--source-root", "", "--full", "--dry-run", str(out), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_document_with_a_string_that_is_not_utf8_is_skipped_and_listed(
    tmp_path: Path,
) -> None:
    out = tmp_path / "delta.json"
    proc = _run(_bad_utf8_index(tmp_path), out)
    assert proc.returncode == 0, proc.stderr
    delta = json.loads(out.read_text())
    assert [f["path"] for f in delta["files"]] == ["src/a.cpp"]
    assert delta["skipped_files"] == ["src/bad.cpp"]
    assert delta["stats"]["skipped_failed"] == 1
    assert "1 failed documents skipped" in proc.stdout
    # named once, though several passes over the index read it
    assert proc.stderr.count("warning: skipped document 'src/bad.cpp'") == 1, proc.stderr


def test_the_switch_stops_the_run_on_a_failed_document(tmp_path: Path) -> None:
    out = tmp_path / "delta.json"
    proc = _run(_bad_utf8_index(tmp_path), out, "--stop-on-failed-document")
    assert proc.returncode != 0
    assert "DecodeError" in proc.stderr
    assert not out.exists()


@pytest.mark.parametrize(("stop", "fails"), [("1", True), ("0", False)])
def test_the_harness_passes_the_switch_to_the_converter(
    tmp_path: Path, stop: str, fails: bool
) -> None:
    """Every tier converts through lib.sh's `convert_delta`, which reads the switch the
    server sets (SCIP_STOP_ON_FAILED_DOCUMENT)."""
    index, out = _bad_utf8_index(tmp_path), tmp_path / "delta.json"
    env = {
        "PATH": os.environ["PATH"],
        "PYTHON": sys.executable,
        "DELTA_OUT": str(out),
        "SCIP_STOP_ON_FAILED_DOCUMENT": stop,
    }
    script = (
        f'source "{_LIB}"; convert_delta --index "{index}" --commit {"h" * 40} '
        '--source-root "" --full'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, check=False
    )
    assert (proc.returncode != 0) is fails, proc.stderr
    assert out.exists() is not fails


def _converter():
    spec = importlib.util.spec_from_file_location("scip_to_delta_skip", _CONVERTER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_server_logs_the_converters_warning_line() -> None:
    from server.code.app.index.compiler_tier import SKIPPED_WARNING

    assert _converter().SKIPPED_WARNING == SKIPPED_WARNING


def test_a_document_that_fails_to_convert_is_skipped_and_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conv = _converter()
    real = conv._convert_document

    def convert_document(doc, run):
        if doc.relative_path == "src/bad.cpp":
            raise KeyError("boom")
        return real(doc, run)

    monkeypatch.setattr(conv, "_convert_document", convert_document)
    index = str(_index_with(tmp_path, "g"))
    delta = conv.convert(index, None, [], "h" * 40, "", source_root="", full=True)
    assert [f["path"] for f in delta["files"]] == ["src/a.cpp"]
    assert delta["skipped_files"] == ["src/bad.cpp"]
    assert delta["stats"]["skipped_failed"] == 1
    assert "warning: skipped document 'src/bad.cpp': KeyError('boom')" in capsys.readouterr().err
    # a Go module's paths are prefixed with its directory, the skipped one too
    moduled = conv.convert(index, None, [], "h" * 40, "", source_root="", path_prefix="mod")
    assert moduled["skipped_files"] == ["mod/src/bad.cpp"]
    # each run keeps its own record: a second stream leaves the first one's as it was
    first = conv.DeltaStream(index, None, [], "h" * 40, "", source_root="", full=True)
    list(first.files)
    conv.DeltaStream(index, None, [], "h" * 40, "", source_root="", full=True)
    assert first.tail()["skipped_files"] == ["src/bad.cpp"]
    assert first.tail()["stats"]["skipped_failed"] == 1
