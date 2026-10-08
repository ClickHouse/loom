"""An imported module-level singleton is typed by its
annotation, and a class two vendored copies define is the nearer one's.

For `from airflow.configuration import conf` then `conf.getboolean("core", "x")`,
a repo-wide bind would pick a Protocol's `getboolean`; `conf: AirflowConfigParser =
initialize_config()` says what it is, and the parser tier records that
annotation as the constant's return type.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_python")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.py_receivers import PyReceiverTyper
from tests.unit.test_code_py_oracle_fixes import _edges

FILES = {
    "pkg/parser.py": (
        b"class AirflowConfigParser:\n"
        b"    def getboolean(self, section, key):\n        return True\n"
    ),
    "pkg/configuration.py": (
        b"from pkg.parser import AirflowConfigParser\n\n"
        b"def initialize_config():\n    return AirflowConfigParser()\n\n"
        b"conf: AirflowConfigParser = initialize_config()\n"
    ),
    "pkg/executors/base.py": (
        b"class ExecutorConf:\n    def getboolean(self, section, key):\n        return False\n"
    ),
    "pkg/app/settings.py": (
        b"from pkg.configuration import conf\n\n"
        b"def pooled():\n"
        b"    return conf.getboolean('database', 'pool')\n"  # line 4
    ),
    # a vendored twin of the class, farther from the caller than pkg.parser
    "other/vendor/parser.py": (
        b"class AirflowConfigParser:\n"
        b"    def getboolean(self, section, key):\n        return True\n"
    ),
}


def test_the_typer_asks_for_an_unbound_receivers_name() -> None:
    pf = parse_file("pkg/app/settings.py", FILES["pkg/app/settings.py"])
    typer = PyReceiverTyper(pf)
    assert "name:conf" in typer.callees()
    assert typer.type_of(4, "conf") == ""
    typer.bind_returns({"name:conf": "AirflowConfigParser"})
    assert typer.type_of(4, "conf") == "AirflowConfigParser"


def test_a_singleton_receiver_binds_its_annotated_class_nearest_copy_first() -> None:
    edges, misses = _edges(FILES, "pkg/app/settings.py")
    targets = {t for line, t, _c in edges if line == 4}
    assert targets == {"pkg.parser::AirflowConfigParser::getboolean"}, (edges, misses)
    assert {c for line, _t, c in edges if line == 4} == {0.9}


def test_a_locally_bound_name_still_wins_over_the_import() -> None:
    files = dict(FILES)
    files["pkg/app/settings.py"] = (
        b"from pkg.configuration import conf\n"
        b"from pkg.executors.base import ExecutorConf\n\n"
        b"def pooled():\n"
        b"    conf = ExecutorConf()\n"
        b"    return conf.getboolean('database', 'pool')\n"  # line 6
    )
    edges, _misses = _edges(files, "pkg/app/settings.py")
    assert {t for line, t, _c in edges if line == 6} == {
        "pkg.executors.base::ExecutorConf::getboolean"
    }
