"""The vendored SCIP bindings import, decode an index, match the vendored schema, and
match the checksums the vendor README records."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parents[2] / "scripts" / "scip_harness" / "vendor"


def _scip_pb2():
    spec = importlib.util.spec_from_file_location("scip_pb2", _VENDOR / "scip_pb2.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("scip_pb2", mod)
    spec.loader.exec_module(mod)
    return sys.modules["scip_pb2"]


def test_an_index_round_trips_through_the_bindings() -> None:
    pb = _scip_pb2()
    index = pb.Index()
    index.metadata.project_root = "file:///repo"
    doc = index.documents.add()
    doc.relative_path = "src/a.cpp"
    occ = doc.occurrences.add()
    occ.symbol = "scip-clang cxx . . DB/f(ab)."
    occ.symbol_roles = pb.SymbolRole.Definition
    occ.range.extend([3, 4, 5])
    info = doc.symbols.add()
    info.symbol = occ.symbol
    info.kind = pb.SymbolInformation.Kind.Function

    decoded = pb.Index()
    decoded.ParseFromString(index.SerializeToString())
    assert decoded.metadata.project_root == "file:///repo"
    (d,) = decoded.documents
    assert d.relative_path == "src/a.cpp"
    assert list(d.occurrences[0].range) == [3, 4, 5]
    assert d.symbols[0].kind == pb.SymbolInformation.Kind.Function


def test_the_bindings_carry_every_top_level_type_of_the_schema() -> None:
    pb = _scip_pb2()
    proto = (_VENDOR / "scip.proto").read_text()
    messages = set(re.findall(r"(?m)^message\s+(\w+)", proto))
    enums = set(re.findall(r"(?m)^enum\s+(\w+)", proto))
    assert messages and enums
    assert messages == set(pb.DESCRIPTOR.message_types_by_name)
    assert enums == set(pb.DESCRIPTOR.enum_types_by_name)


def test_the_vendor_readme_records_the_files_checksums() -> None:
    readme = (_VENDOR / "README.md").read_text()
    for name in ("scip.proto", "scip_pb2.py"):
        digest = hashlib.sha256((_VENDOR / name).read_bytes()).hexdigest()
        row = next(line for line in readme.splitlines() if line.startswith(f"| `{name}`"))
        assert digest in row, f"{name}: README records a different sha256 than {digest}"
