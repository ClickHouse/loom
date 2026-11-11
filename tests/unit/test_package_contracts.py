"""Domain packages keep their import contract.

server/ is organised by domain (server/<domain>/) and, inside a domain, by
layer:

  api      the operations' wire contract (Operation declarations, models)
  app      handlers and orchestration
  domain   pure logic and typed values: no I/O
  store    ClickHouse repositories and row builders
  workers  background loops

This test reads every module under server/ with ast (function-level imports
included), classifies both ends of each server import by path, and checks the
rules below. Imports that break a rule today are listed in
package_contract_allowlist.txt; the test fails on a new violation and on an
allow-list line that no longer occurs, so the list only shrinks.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
ALLOWLIST = Path(__file__).with_name("package_contract_allowlist.txt")

DOMAINS = frozenset(
    {
        "memory",
        "code",
        "tracker",
        "erasure",
        "tasks",
        "knowledge",
        "identity",
        "insights",
        "assistant",
        "help",
    }
)
LAYERS = ("api", "app", "domain", "store", "workers")
# Platform packages: shared services every domain may use.
PLATFORM = frozenset(
    {
        "auth",
        "clients",
        "embedding",
        "llm",
        "observability",
        "settings",
        "storage",
        "transport",
        "cli",
        "operations",
        "workers",
        "router",
    }
)
# core modules that do I/O: domain code must not import them.
CORE_IO = (
    "server.core.ch",
    "server.core.schema",
    "server.core.idempotency",
    "server.core.ratelimit_shared",
    "server.core.namespace_write_marks",
    "server.core.hosts",
    "server.core.webhooks",
    "server.core.leases",
    "server.core.object_store",
)


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def classify(module: str) -> tuple[str, str]:
    """(package, layer) for a server module; layer is '' outside a domain."""
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != "server":
        return ("external", "")
    top = parts[1]
    if top in DOMAINS:
        layer = parts[2] if len(parts) > 2 and parts[2] in LAYERS else ""
        return (top, layer)
    if top == "core":
        return ("core", "")
    if top in PLATFORM:
        return (top, "")
    return ("root", "")  # server.main, server/__init__


def _is_wire_models(module: str) -> bool:
    """A domain's wire models module: `server.<domain>.api.models` or `.models_<group>`."""
    parts = module.split(".")
    return len(parts) >= 4 and parts[2] == "api" and parts[3].startswith("models")


def allowed(importer: str, imported: str) -> bool:
    ipkg, ilayer = classify(importer)
    tpkg, tlayer = classify(imported)
    if tpkg == "external" or ipkg == "root":
        return True  # server.main is the composition root: it wires every package
    if ipkg in DOMAINS:
        same = tpkg == ipkg
        if tpkg in DOMAINS:
            rules = {
                "domain": {"domain"},
                "store": {"domain"},
                "api": {"domain", "api"} | ({"app"} if same else set()),
                "app": ({"api", "app", "domain", "store"} if same else {"app", "domain"}),
                "workers": ({"app", "domain", "store"} if same else {"app", "domain"}),
                "": set(LAYERS),
            }
            if not same and ilayer in {"app", "workers"} and _is_wire_models(imported):
                # Calling another domain's handler takes its input model: the wire
                # models are data, never its Operation declarations (api.ops*).
                return True
            return (tlayer or "") in rules.get(ilayer, set()) or (same and ilayer == tlayer)
        if tpkg == "core":
            return not (ilayer == "domain" and imported.startswith(CORE_IO))
        if ilayer == "domain":
            return tpkg == "settings"
        if ilayer == "store":
            return tpkg in {"settings", "observability", "storage"}
        if ilayer == "api":
            return tpkg in {"operations", "settings"}
        return tpkg not in {"transport", "cli", "root"}
    # Platform and core packages never reach into a domain package.
    if tpkg in DOMAINS:
        # The registry reads a domain's api (its operations); the worker registry
        # names its loops. The CLI and the transports are outer adapters: the
        # transports bind a request to a domain's handlers and policies, so they
        # read its api, app and domain, never its store or workers directly.
        if ipkg == "cli":
            return True
        if ipkg == "transport":
            return tlayer in {"api", "app", "domain"}
        if ipkg == "operations":
            return tlayer == "api"
        if ipkg == "workers":
            return tlayer == "workers"
        return False
    if ipkg == "core":
        return tpkg == "core" or tpkg == "settings"
    if ipkg == "settings":
        return tpkg in {"core", "settings"}
    return True


def _imports(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    pkg = module if path.name == "__init__.py" else module.rpartition(".")[0]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = pkg.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            out.add(base)
            out.update(f"{base}.{a.name}" for a in node.names)
    return {m for m in out if m.startswith("server.")}


def _known(modules: set[str], name: str) -> str | None:
    while name and name not in modules:
        name = name.rpartition(".")[0]
    return name or None


def violations() -> set[str]:
    paths = {_module_name(p): p for p in SERVER.rglob("*.py") if "__pycache__" not in p.parts}
    modules = set(paths)
    found: set[str] = set()
    for module, path in paths.items():
        for name in _imports(path, module):
            target = _known(modules, name)
            if target and target != module and not allowed(module, target):
                found.add(f"{module} -> {target}")
    return found


def _allowlist() -> set[str]:
    return {
        line.strip()
        for line in ALLOWLIST.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_no_new_contract_violation() -> None:
    new = sorted(violations() - _allowlist())
    assert not new, "imports that break the package contract:\n" + "\n".join(new)


def test_the_allowlist_only_shrinks() -> None:
    stale = sorted(_allowlist() - violations())
    assert not stale, (
        "allow-listed imports no longer occur; delete these lines from "
        f"{ALLOWLIST.name}:\n" + "\n".join(stale)
    )


def test_classify_reads_the_layout() -> None:
    assert classify("server.erasure.store.census") == ("erasure", "store")
    assert classify("server.core.ch") == ("core", "")
    assert classify("server.operations.base") == ("operations", "")
    assert classify("server.workers.registry") == ("workers", "")
    assert not allowed("server.erasure.domain.x", "server.core.ch")
    assert allowed("server.erasure.app.saga", "server.erasure.store.census")
    assert not allowed("server.tasks.app.transitions", "server.erasure.store.census")
    assert not allowed("server.core.budgets", "server.tasks.app.transitions")
    assert allowed("server.transport.gates", "server.identity.app.policy_gate")
    assert not allowed("server.transport.http", "server.memory.store.rows")
    assert not allowed("server.operations.registry", "server.memory.app.search")
    assert allowed("server.memory.app.fanout", "server.knowledge.api.models_knowledge")
    assert not allowed("server.memory.app.fanout", "server.knowledge.api.ops_knowledge")
    assert not allowed("server.memory.domain.x", "server.knowledge.api.models_knowledge")


def _all_imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            out.add(node.module)
    return out


def _importers(root: Path, banned: tuple[str, ...]) -> list[str]:
    return sorted(
        f"{p.relative_to(ROOT)} imports {name}"
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
        for name in _all_imports(p)
        if name in banned or name.startswith(tuple(f"{b}." for b in banned))
    )


def test_the_server_parses_no_original_and_the_extractor_reads_no_server_code() -> None:
    """Every original is parsed in an extractor service (extractor/loom_extractor/);
    the server reads only the artifact it returns. The knowledge plane also uses
    no parser library of its own."""
    assert not _importers(SERVER, ("loom_extractor", "sqlglot", "pypdf"))
    assert not _importers(SERVER / "knowledge", ("tomllib", "html.parser", "zipfile", "xml"))
    assert not _importers(ROOT / "extractor", ("server",))
