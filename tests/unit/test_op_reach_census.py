"""The op reach census: every op declares a maturity, derives
a reach set, and has a reach path or says why not; the committed manifest
matches the tree; the `all` tier stays under its ceiling; the agent-controlled
standing-memory and forgetting verbs are on the core tier; the catalogue
carries the two additive fields."""

from __future__ import annotations

import asyncio
import json

import pytest

from scripts.codegen.op_reach_census import derive_static_reach, render_manifest
from server.core.paths import REPO_ROOT
from server.operations.base import MATURITIES
from server.operations.reach import (
    REACH_KINDS,
    STATIC_KINDS,
    catalogue_facets,
    has_reach,
    mcp_reach,
    op_reach,
)
from server.operations.reach_manifest import STATIC_REACH
from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME
from server.operations.toolsets import (
    _MCP_ATOMIC_OPS,
    _MCP_CODE_CORE_OPS,
    _MCP_CORE_OPS,
    _MCP_HIDDEN_OPS,
    MCP_ASSISTANT_OPS,
    SYNTHETIC_TOOL_OPS,
    listed_under_all,
    op_toolsets,
)
from server.settings import settings
from server.transport import mcp_tools as mt

ALL_TIER_CEILING = 120


def _listing(toolset: str, code: bool) -> list[dict]:
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "mcp_toolset", toolset)
    monkey.setattr(settings, "mcp_code_tools", code)
    monkey.setattr(settings, "mcp_schema_profile", "standard")
    try:
        return asyncio.run(mt._tools_descriptor())
    finally:
        monkey.undo()


# ------------------------------------------------------------------ declarations


def test_every_op_declares_a_known_maturity():
    assert set(MATURITIES) == {"stable", "experimental"}
    bad = {op.name: op.maturity for op in ALL_OPERATIONS if op.maturity not in MATURITIES}
    assert not bad, bad


def test_every_op_reach_is_a_subset_of_the_kinds_and_http_only_is_exclusive():
    for op in ALL_OPERATIONS:
        reach = op.reach
        assert reach and reach <= REACH_KINDS, (op.name, reach)
        assert ("http_only" in reach) == (reach == {"http_only"}), (op.name, reach)


def test_census_gate_no_reach_path_means_not_stable():
    """The CI gate: an op only a raw /v1 POST reaches must say so with its
    maturity — declared, described and tested code with no caller is dark."""
    dark = sorted(
        op.name for op in ALL_OPERATIONS if not has_reach(op.name) and op.maturity == "stable"
    )
    assert not dark, f"ops with no reach path declared stable: {dark}"


def test_mcp_reach_derives_from_the_tier_frozensets():
    for name in _MCP_CORE_OPS | _MCP_CODE_CORE_OPS:
        assert {"mcp_core", "mcp_all"} <= op_reach(name), name
    for name in _MCP_ATOMIC_OPS:
        assert {"mcp_atomic", "mcp_all"} <= op_reach(name), name
    for name in _MCP_HIDDEN_OPS:
        assert not (op_reach(name) & {"mcp_core", "mcp_atomic", "mcp_all"}), name
    # the shape the eval found: the hidden operator sweep has no SDK wrapper either
    assert op_reach("blacklist.sweep") == {"http_only"}
    assert OPERATIONS_BY_NAME["blacklist.sweep"].maturity == "experimental"


def test_manifest_matches_the_tree():
    """SDK methods, examples/dev/scripts and integrations are scanned at test
    time; the committed table must equal the scan (regenerate with
    scripts/codegen/op_reach_census.py --write-manifest)."""
    derived = derive_static_reach(REPO_ROOT)
    assert derived == STATIC_REACH, {
        "missing": sorted(set(derived) - set(STATIC_REACH)),
        "stale": sorted(set(STATIC_REACH) - set(derived)),
        "changed": sorted(
            k for k in derived if k in STATIC_REACH and derived[k] != STATIC_REACH[k]
        ),
    }
    for kinds in STATIC_REACH.values():
        assert kinds and all(k in STATIC_KINDS for k in kinds)
        assert list(kinds) == [k for k in STATIC_KINDS if k in kinds], "manifest order is canonical"
    text = render_manifest(derived)
    assert "GENERATED" in text and text.count('": (') == len(derived)


def test_scan_of_a_checkout_under_a_tests_directory_still_finds_reach(tmp_path):
    """Only `tests` directories inside the scanned tree are skipped; the
    checkout's own location does not matter."""
    root = tmp_path / "tests" / "loom"
    sdk = root / "sdk" / "python" / "loom"
    sdk.mkdir(parents=True)
    (sdk / "memory.py").write_text('PATH = "/v1/memory.pin"\n')
    (sdk / "tests").mkdir()
    (sdk / "tests" / "test_x.py").write_text('PATH = "/v1/memory.get"\n')
    derived = derive_static_reach(root)
    assert derived.get("memory.pin") == ("sdk_method",)
    assert "memory.get" not in derived


def test_a_test_beside_its_source_is_not_reach(tmp_path):
    """A dashboard test names ops in its fixtures; only the page source is reach."""
    src = tmp_path / "frontend" / "src"
    src.mkdir(parents=True)
    (src / "Page.tsx").write_text('post("/v1/memory.pin");\n')
    (src / "Page.test.tsx").write_text('const OP = "memory.unpin";\n')
    derived = derive_static_reach(tmp_path)
    assert derived.get("memory.pin") == ("dashboard",)
    assert "memory.unpin" not in derived


def test_manifest_finds_the_known_reach_paths():
    # SDK wrapper: sdk/python/loom/memory.py posts /v1/memory.pin
    assert "sdk_method" in op_reach("memory.pin")
    # example: examples/claude_agent_loop.py calls agent.memory.set(...)
    assert "sdk_example" in op_reach("memory.set")
    # plugin hook: loom-working-memory.mjs posts working_memory.append
    assert "plugin_hook" in op_reach("working_memory.append")
    # dashboard: frontend/src/api/admin.ts posts ops.health
    assert "dashboard" in op_reach("ops.health") and "plugin_hook" not in op_reach("ops.health")


# ------------------------------------------------------------------ the exposed verbs


def test_agent_controlled_verbs_are_on_the_core_tier():
    """The standing-memory and forgetting verbs an agent should control
    (standing memory, a hot tier): pin, refuse, scratchpad."""
    for name in ("memory.pin", "blacklist.add", "working_memory.append"):
        assert name in _MCP_CORE_OPS and name not in _MCP_HIDDEN_OPS, name
        assert "mcp_core" in op_reach(name)
    names = {t["name"] for t in _listing("core", False)}
    assert {"loom_memory_pin", "loom_blacklist_add", "loom_working_memory_append"} <= names
    pin = next(t for t in _listing("core", False) if t["name"] == "loom_memory_pin")
    # a curated description that names memory.set as the default save
    assert "memory.set" in pin["description"] and "WHEN NOT" in pin["description"]
    assert len(pin["description"]) // 4 <= 480


# ------------------------------------------------------------------ the all tier


def test_all_tier_lists_stable_plus_curated_and_stays_under_the_ceiling():
    for code in (False, True):
        listed = _listing("all", code)
        names = {t["name"] for t in listed}
        assert len(listed) <= ALL_TIER_CEILING, len(listed)
        for op in ALL_OPERATIONS:
            if op.name in _MCP_HIDDEN_OPS or (op.name.startswith("code.") and not code):
                assert op.mcp_tool_name not in names, op.name
                continue
            expected = listed_under_all(op)
            assert (op.mcp_tool_name in names) == expected, (op.name, op.maturity)
    # core ⊆ all regardless of maturity
    core = {t["name"] for t in _listing("core", True)}
    assert core <= {t["name"] for t in _listing("all", True)}


def test_experimental_ops_are_callable_by_name_not_listed():
    exp = [
        op
        for op in ALL_OPERATIONS
        if op.maturity == "experimental" and op.name not in _MCP_HIDDEN_OPS
    ]
    assert exp, "the census marked ops experimental"
    names = {t["name"] for t in _listing("all", True)}
    for op in exp:
        curated = bool(mcp_reach(op.name) & {"mcp_core", "mcp_atomic"})
        assert (op.mcp_tool_name in names) == curated, op.name
    # listing is visibility only: the transport's call path reads the registry, not the roster
    assert all(op.name in OPERATIONS_BY_NAME for op in exp)


def test_every_catalogue_says_all_exactly_when_the_all_listing_lists_the_op():
    """`ops.capabilities` / `code.index_status` toolsets, the reach kinds and
    the `all` listing (code tools on) agree op by op."""
    listed = {t["name"] for t in _listing("all", True)}
    for op in ALL_OPERATIONS:
        on_all = op.mcp_tool_name in listed or (
            op.name in SYNTHETIC_TOOL_OPS and op.name not in _MCP_HIDDEN_OPS
        )
        assert ("all" in op_toolsets(op.name)) == on_all, op.name
        assert ("mcp_all" in mcp_reach(op.name)) == on_all, op.name
        assert listed_under_all(op) == on_all, op.name


# ------------------------------------------------------------------ catalogue


def test_catalogue_facets_are_additive_and_json_shaped():
    for op in ALL_OPERATIONS:
        f = catalogue_facets(op)
        assert set(f) == {"maturity", "reach", "toolsets"}
        assert f["maturity"] in MATURITIES and f["reach"] == sorted(op.reach)
        # The toolsets facet is the listing membership, sorted;
        # a hidden op is on none but the assistant roster; `all` only when the all
        # listing carries it
        assert f["toolsets"] == sorted(f["toolsets"])
        # `tasks` is the task inbox plane's own roster
        assert set(f["toolsets"]) <= {
            "all",
            "assistant",
            "atomic",
            "coding",
            "core",
            "tracker",
            "tasks",
        }
        if op.name in _MCP_HIDDEN_OPS:
            assert f["toolsets"] == (["assistant"] if op.name in MCP_ASSISTANT_OPS else [])
        assert ("all" in f["toolsets"]) == listed_under_all(op)
        assert ("core" in f["toolsets"]) == ("mcp_core" in op.reach)
        assert ("atomic" in f["toolsets"]) == ("mcp_atomic" in op.reach)
        json.dumps(f)


def test_census_script_check_passes_offline():
    from scripts.codegen import op_reach_census as mod

    assert mod.violations(ALL_OPERATIONS) == []
    table = mod.rows(ALL_OPERATIONS, None, None)
    assert len(table) == len(ALL_OPERATIONS) and all(r["calls_30d"] == "n/a" for r in table)
    assert mod.summary(table)["ops"] == len(ALL_OPERATIONS)
    assert "| op | reach |" in mod.render_table(table[:2], markdown=True)


def test_write_manifest_writes_the_committed_bytes_and_nothing_else(tmp_path, monkeypatch):
    """`--write-manifest` reproduces the ruff-formatted committed file byte for
    byte and returns without building the census (no ClickHouse read)."""
    import dataclasses

    from scripts.codegen import op_reach_census as mod

    out = tmp_path / "reach_manifest.py"
    monkeypatch.setattr(mod, "MANIFEST", dataclasses.replace(mod.MANIFEST, path=out))

    def no_counts(*_a, **_k):
        raise AssertionError("--write-manifest must not read call counts")

    monkeypatch.setattr(mod, "call_counts", no_counts)
    assert mod.main(["--write-manifest"]) == 0
    committed = REPO_ROOT / "server" / "operations" / "reach_manifest.py"
    assert out.read_bytes() == committed.read_bytes()
    with pytest.raises(SystemExit):
        mod.main(["--check", "--write-manifest"])


def test_check_compares_the_manifest_bytes(tmp_path, monkeypatch):
    """An unformatted manifest parses to the same table but is not what
    `--write-manifest` writes, so `--check` names it stale."""
    import dataclasses

    from scripts.codegen import op_reach_census as mod

    out = tmp_path / "reach_manifest.py"
    out.write_text(render_manifest(derive_static_reach(REPO_ROOT)))
    monkeypatch.setattr(mod, "MANIFEST", dataclasses.replace(mod.MANIFEST, path=out))
    assert mod.violations(ALL_OPERATIONS) == [
        f"{out} is stale — run python scripts/codegen/op_reach_census.py --write-manifest"
    ]
    assert mod.main(["--check"]) == 1


def test_call_counts_only_with_the_flag(monkeypatch, capsys):
    """No flag: no connection, calls are n/a. `--call-counts` reads through the
    server CLI's client and maps the rows."""
    import server.cli._ch as cli_ch
    from scripts.codegen import op_reach_census as mod

    seen: list[tuple[str, dict]] = []

    class FakeCH:
        async def query_all(self, sql, params):
            seen.append((sql, params))
            return [{"operation": "memory.set", "calls": 7, "last_ts": "2000-01-01", "agents": 2}]

    def fake_run_connected(run):
        import asyncio

        return asyncio.run(run(FakeCH()))

    monkeypatch.setattr(cli_ch, "run_connected", fake_run_connected)
    assert mod.main(["--json"]) == 0
    assert seen == []
    assert json.loads(capsys.readouterr().out)["summary"]["uncalled_30d"] == "n/a"

    assert mod.call_counts(9) == {"memory.set": {"calls": 7, "last_ts": "2000-01-01", "agents": 2}}
    assert seen == [(mod.CALLS_SQL, {"days": 9})]
