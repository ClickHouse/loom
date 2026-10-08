# Python stubs for scip-python

`run_python_delta.sh` puts this directory on `PYTHONPATH` while scip-python
indexes, so pyright resolves these imports. Each entry is a package the indexed
code imports by wildcard that no indexing environment installs: left
unresolved, pyright's wildcard-import flow analysis asserts and scip-python
abandons the whole index.

| stub     | imported by                                                                         | as                                  |
| -------- | ----------------------------------------------------------------------------------- | ----------------------------------- |
| `delta/` | ClickHouse `tests/integration/test_storage_delta/*`, `utils/data-lakes-importer.py` | `from delta import *` (delta-spark) |

A stub declares a module-level `__getattr__` returning `Any`, so every name
resolves and nothing is checked against it.
