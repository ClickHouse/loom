"""loom.namespaces is ReplacingMergeTree(updated_at): a directory row written
without `classification` resets a restricted namespace to standard on the
next FINAL. Every writer carries the column."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _schema_columns() -> set[str]:
    schema = (ROOT / "schemas" / "schema.sql").read_text()
    block = schema.split("CREATE TABLE IF NOT EXISTS loom.namespaces\n", 1)[1].split("\nENGINE", 1)[
        0
    ]
    return set(re.findall(r"^\s+`([a-z_]+)`", block, re.M))


def test_schema_has_the_column() -> None:
    assert "classification" in _schema_columns()
    assert "join_policy" in _schema_columns()  # same carry-forward rule


def test_registry_row_carries_classification_forward() -> None:
    import server.operations  # noqa: F401 — wires the handler modules
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    now = datetime(2026, 9, 15, 12, 0, 0)
    inp = NamespaceRegisterInput(
        org="clickhouse", namespace="tracker-ops-restricted", owner_email="o@x"
    )
    fresh = registry_row(inp, None, now)
    assert fresh["classification"] == "standard"
    kept = registry_row(
        inp, {"classification": "restricted", "conversation_logging_enabled": False}, now
    )
    assert kept["classification"] == "restricted"
    assert fresh["title"] == ""
    titled = {"title": "Q3 Plans", "conversation_logging_enabled": False}
    assert registry_row(inp, titled, now)["title"] == "Q3 Plans"
    assert fresh["join_policy"] == "inherit"
    assert (
        registry_row(inp, {"join_policy": "closed", "conversation_logging_enabled": False}, now)[
            "join_policy"
        ]
        == "closed"
    )
    # the form has no field that could set it
    assert not hasattr(inp, "classification")
    assert set(fresh) <= _schema_columns()


def test_register_handler_reads_the_whole_existing_row() -> None:
    src = (ROOT / "server" / "identity" / "app" / "namespace.py").read_text()
    select = src.split("async def handle_namespace_register", 1)[1].split(
        "if existing is not None", 1
    )[0]
    assert "namespaces_repo.fetch_row(" in select, (
        "handle_namespace_register must read every column (classification, join_policy, …) "
        "to carry it forward"
    )


def test_self_signup_row_names_the_column() -> None:
    src = (ROOT / "server" / "auth" / "self_signup.py").read_text()
    block = src.split('"loom.namespaces",', 1)[1].split("await ", 1)[0]
    assert '"classification"' in block
    assert '"join_policy"' in block


def test_every_literal_namespaces_insert_is_a_known_writer() -> None:
    """A new `ch.insert("loom.namespaces", …)` site must either build its row
    through `registry_row`, copy an existing row in full (`SELECT *`), or be
    listed here with its own classification handling."""
    known = {
        "server/identity/app/namespace.py",  # registry_row + SELECT * soft delete
        "server/identity/app/namespace_snapshot.py",  # restore: SELECT * reactivation + new row
        "server/identity/app/namespace_autoregister.py",  # registry_row
        "server/cli/hygiene.py",  # registry_row
        "server/auth/self_signup.py",  # literal row, names the column
        "server/code/app/index/registry.py",  # literal row + SELECT * narrowing
        "server/tracker/app/registry.py",  # literal row + SELECT * narrowing
        "server/code/app/repo_admin.py",  # SELECT * new version: set_access
        "server/identity/app/conversation_logging.py",  # SELECT * new version: set_logging
        "server/identity/app/claim_release.py",  # SELECT * new version: release
        "server/identity/app/collections.py",  # registry_row + full-row copy (title)
    }
    found = set()
    for path in (ROOT / "server").rglob("*.py"):
        if 'insert("loom.namespaces"' in path.read_text():
            found.add(str(path.relative_to(ROOT)))
    assert found <= known, f"unlisted loom.namespaces writer(s): {sorted(found - known)}"
