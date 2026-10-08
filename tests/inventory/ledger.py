"""The feature-to-test ledger: product features from their registries, and
the tests that reference each one, by layer.

A feature is referenced by a test file when the file names it (an op name or
MCP tool name, a setting field or its `LOOM_` env name, a worker name, an SDK
export, a `loom.<table>`, a migration file stem, a CLI command, a route path,
a gate module) or when an annotation's `covers` pattern matches its id. It is
a static signal: a reference proves a test mentions the feature, not that
the test asserts it well. Tags (security, fault, concurrency, recovery) come
from the referencing files' inventory rows.

`python -m tests.inventory.ledger` prints counts per kind;
`--uncovered` lists features with no reference and no `gaps.toml` entry, as
the lines to add; `--prune-gaps` deletes the gap lines whose feature has a
test or no longer exists; `--json` prints every feature with its cells.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tests.inventory import build

ROOT = build.ROOT
GAPS = build.HERE / "gaps.toml"

# Where tests that count for the ledger live, and the layer each counts as.
# `tests/fixtures` and `tests/unit/fixtures` are inputs, not tests.
_LAYER_ROOTS: tuple[tuple[str, str], ...] = (
    ("tests/integration/", "integration"),
    ("tests/analyzer/", "analyzer"),
    ("tests/code_lanes/", "analyzer"),
    ("tests/unit/", "unit"),
    ("bench/", "bench"),
    ("sdk/typescript/", "ts"),
)
LAYERS: tuple[str, ...] = ("unit", "integration", "analyzer", "bench", "ts")
RISK_TAGS: tuple[str, ...] = ("security", "fault", "concurrency", "recovery")

_TOKEN = re.compile(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*")
_ROUTE = re.compile(
    r"""^\s*@(?:app|router)\.(get|post|put|delete|patch)\(\s*["']([^"']+)["']""", re.M
)
_TS_EXPORT = re.compile(r"^export\s+(?:type\s+)?\{([^}]*)\}", re.M)
_TS_EXPORT_DECL = re.compile(r"^export\s+(?:const|function|class|interface|type)\s+(\w+)", re.M)


@dataclass(frozen=True)
class Feature:
    id: str
    kind: str
    # Tokens that count as a reference. Dotted tokens match any run of
    # consecutive segments of a dotted name in the test (`a.memory.set`).
    tokens: frozenset[str]
    # Substrings that count as a reference (route paths).
    substrings: tuple[str, ...] = ()


@dataclass
class Coverage:
    feature: Feature
    files: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    tags: set[str] = field(default_factory=set)

    @property
    def covered(self) -> bool:
        return any(self.files.values())

    def cells(self) -> dict[str, int]:
        cells = {layer: len(self.files.get(layer, ())) for layer in LAYERS}
        cells.update({tag: int(tag in self.tags) for tag in RISK_TAGS})
        return cells


# ---------------------------------------------------------------- features


def _operations() -> Iterator[Feature]:
    from server.operations.registry import ALL_OPERATIONS

    for op in ALL_OPERATIONS:
        tokens = {op.name, op.mcp_tool_name}
        # A test that calls the handler directly names it, not the op.
        handler = getattr(op.handler, "__name__", "")
        if handler not in {"", "<lambda>", "wrapper", "inner"}:
            tokens.add(handler)
        yield Feature(f"op:{op.name}", "op", frozenset(tokens))


def _routes() -> Iterator[Feature]:
    seen: set[str] = set()
    for path in sorted((ROOT / "server").rglob("*.py")):
        for method, route in _ROUTE.findall(path.read_text()):
            fid = f"route:{method.upper()} {route}"
            if fid not in seen:
                seen.add(fid)
                yield Feature(fid, "route", frozenset(), substrings=(route,))


def _settings() -> Iterator[Feature]:
    from server.settings import Settings

    for name in Settings.model_fields:
        yield Feature(f"setting:{name}", "setting", frozenset({name, f"LOOM_{name.upper()}"}))


def _workers() -> Iterator[Feature]:
    from server.workers.registry import _WORKERS

    for row in _WORKERS:
        yield Feature(f"worker:{row.name}", "worker", frozenset({row.name}))


def _sdk_python() -> Iterator[Feature]:
    tree = ast.parse((ROOT / "sdk/python/loom/__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            for name in ast.literal_eval(node.value):
                yield Feature(f"sdk-py:{name}", "sdk-py", frozenset({name}))


def _sdk_typescript() -> Iterator[Feature]:
    text = (ROOT / "sdk/typescript/src/index.ts").read_text()
    names = set(_TS_EXPORT_DECL.findall(text))
    for group in _TS_EXPORT.findall(text):
        for part in group.split(","):
            part = part.strip()
            if part:
                names.add(part.split(" as ")[-1].strip())
    for name in sorted(names):
        yield Feature(f"sdk-ts:{name}", "sdk-ts", frozenset({name}))


def _ch_objects() -> Iterator[Feature]:
    for line in (ROOT / "schemas/schema.sql").read_text().splitlines():
        if line.startswith("-- loom."):
            name = line[len("-- loom.") :].strip()
            tokens = {f"loom.{name}"}
            # A one-word name (`memory`, `runs`) is too common a word to count bare.
            if "_" in name:
                tokens.add(name)
            yield Feature(f"ch:{name}", "ch", frozenset(tokens))


def _migrations() -> Iterator[Feature]:
    for path in sorted((ROOT / "schemas/migrations").glob("*.sql")):
        yield Feature(f"migration:{path.stem}", "migration", frozenset({path.stem, path.name}))


def _roles() -> Iterator[Feature]:
    from server.operations.base import Role

    for role in Role:
        yield Feature(
            f"role:{role.name.lower()}",
            "role",
            frozenset({f"Role.{role.name}", role.name.lower()}),
        )


def _cli_commands() -> Iterator[Feature]:
    import argparse as ap

    from server.cli import app

    parser = ap.ArgumentParser(prog="loom")
    sub = parser.add_subparsers(dest="cmd")
    for add in app._SUBCOMMAND_BUILDERS:
        add(sub)

    def walk(action: ap._SubParsersAction[ap.ArgumentParser], prefix: str) -> Iterator[str]:
        for name, child in action.choices.items():
            yield prefix + name
            for act in child._actions:
                if isinstance(act, ap._SubParsersAction):
                    yield from walk(act, f"{prefix}{name} ")

    for command in walk(sub, ""):
        leaf = command.rsplit(" ", 1)[-1]
        yield Feature(f"cli:{command}", "cli", frozenset({leaf}))


def _gates() -> Iterator[Feature]:
    for sub in ("gates", "codegen"):
        for path in sorted((ROOT / "scripts" / sub).glob("*.py")):
            if not path.name.startswith("_"):
                yield Feature(f"gate:{sub}/{path.stem}", "gate", frozenset({path.stem}))


FEATURE_SOURCES = {
    "op": _operations,
    "route": _routes,
    "setting": _settings,
    "worker": _workers,
    "sdk-py": _sdk_python,
    "sdk-ts": _sdk_typescript,
    "ch": _ch_objects,
    "migration": _migrations,
    "role": _roles,
    "cli": _cli_commands,
    "gate": _gates,
}


def features() -> list[Feature]:
    return [f for source in FEATURE_SOURCES.values() for f in source()]


# ---------------------------------------------------------------- references


def _layer_of(path: str) -> str | None:
    if path.startswith(("tests/fixtures/", "tests/unit/fixtures/", "tests/inventory/")):
        return None
    for prefix, layer in _LAYER_ROOTS:
        if path.startswith(prefix):
            name = path.rsplit("/", 1)[-1]
            if layer == "ts":
                return layer if name.endswith((".test.ts", ".test-d.ts")) else None
            if layer == "bench":
                # A harness naming a setting is not a test of it; bench's tests are.
                return layer if name.startswith("test_") and name.endswith(".py") else None
            return layer if name.endswith(".py") else None
    return None


def source_files() -> list[tuple[str, str]]:
    """(path, layer) for every tracked file that counts as a test."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "tests", "bench", "sdk/typescript/src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    pairs = []
    for path in sorted(p for p in out.decode().split("\0") if p):
        layer = _layer_of(path)
        if layer:
            pairs.append((path, layer))
    return pairs


def _tokens(text: str) -> set[str]:
    """Every word and every run of consecutive segments of a dotted name."""
    out: set[str] = set()
    for tok in _TOKEN.findall(text):
        parts = tok.split(".")
        if len(parts) == 1:
            out.add(tok)
            continue
        n = len(parts)
        for i in range(n):
            for j in range(i + 1, min(n, i + 4) + 1):
                out.add(".".join(parts[i:j]))
    return out


def index(sources: Sequence[tuple[str, str]]) -> dict[str, set[str]]:
    """token -> the test files that contain it."""
    idx: dict[str, set[str]] = defaultdict(set)
    for path, _layer in sources:
        for tok in _tokens(build.read_text(path)):
            idx[tok].add(path)
    return idx


def coverage(
    feats: Sequence[Feature] | None = None,
    sources: Sequence[tuple[str, str]] | None = None,
    rows: Iterable[build.Row] | None = None,
) -> list[Coverage]:
    feats = features() if feats is None else feats
    sources = source_files() if sources is None else sources
    layer_of = dict(sources)
    by_path = {r.path: r for r in (build.build_rows() if rows is None else rows)}
    idx = index(sources)
    covering = [r for r in by_path.values() if r.covers]
    listings = {r.path for r in by_path.values() if not r.references}
    out = []
    for feat in feats:
        cov = Coverage(feat)
        hits: set[str] = set()
        for tok in feat.tokens:
            hits |= idx.get(tok, set())
        for sub in feat.substrings:
            hits |= {p for p, _ in sources if sub in build.read_text(p)}
        for row in covering:
            if any(build.matches(pat, feat.id) for pat in row.covers):
                hits.add(row.path)
        for path in hits:
            layer = layer_of.get(path)
            if layer is None or path in listings:
                continue
            cov.files[layer].add(path)
            hit_row = by_path.get(path)
            if hit_row:
                cov.tags |= set(hit_row.tags) & set(RISK_TAGS)
        out.append(cov)
    return out


# ---------------------------------------------------------------- gaps


@dataclass(frozen=True)
class Gap:
    owner: str
    reason: str


def load_gaps(path: Path = GAPS) -> dict[str, Gap]:
    """`feature id -> Gap` from gaps.toml's `[gaps]` table."""
    data = tomllib.loads(path.read_text())
    extra = set(data) - {"gaps"}
    if extra:
        raise ValueError(f"{path.name}: unknown top-level keys {sorted(extra)}")
    gaps = {}
    for fid, entry in data.get("gaps", {}).items():
        if not isinstance(entry, dict) or set(entry) != {"owner", "reason"}:
            raise ValueError(f"{path.name}: {fid} must be {{ owner = ..., reason = ... }}")
        if entry["owner"] not in build.AREAS:
            raise ValueError(f"{path.name}: {fid} owner {entry['owner']!r} not in {build.AREAS}")
        if not str(entry["reason"]).strip():
            raise ValueError(f"{path.name}: {fid} needs a reason")
        gaps[fid] = Gap(owner=entry["owner"], reason=entry["reason"])
    return gaps


@dataclass(frozen=True)
class GapCheck:
    uncovered: list[str]  # no test and no gap entry
    stale: list[str]  # a gap entry whose feature now has a test
    unknown: list[str]  # a gap entry naming no current feature


def check_gaps(covs: Sequence[Coverage], gaps: Mapping[str, Gap]) -> GapCheck:
    ids = {c.feature.id for c in covs}
    covered = {c.feature.id for c in covs if c.covered}
    return GapCheck(
        uncovered=sorted(ids - covered - set(gaps)),
        stale=sorted(set(gaps) & covered),
        unknown=sorted(set(gaps) - ids),
    )


_GAP_REASONS = {
    "op": "No test calls this operation by name.",
    "route": "No test requests this route.",
    "setting": "No test names this setting or its LOOM_ env var.",
    "worker": "No test names this registry row.",
    "sdk-py": "No test uses this export.",
    "sdk-ts": "No test uses this export.",
    "ch": "No test names this object; only schema-check pins its DDL.",
    "migration": "No test reads this migration.",
    "role": "No test names this role.",
    "cli": "No test invokes this command.",
    "gate": "No test imports this generator.",
}
_CODE_WORDS = ("code", "scip", "symbol", "repo")
_MEMORY_WORDS = ("memory", "entity", "erasure", "profile", "ask", "search", "embed", "recall")


def suggest_owner(fid: str) -> str:
    name = fid.split(":", 1)[1]
    if any(w in name for w in _CODE_WORDS):
        return "code"
    if any(w in name for w in _MEMORY_WORDS):
        return "memory"
    return "platform"


def gap_line(fid: str) -> str:
    """The gaps.toml line for a feature with no test (check the owner)."""
    reason = _GAP_REASONS[fid.split(":", 1)[0]]
    return f'"{fid}" = {{ owner = "{suggest_owner(fid)}", reason = "{reason}" }}'


def prune_gaps(drop: Iterable[str], path: Path = GAPS) -> list[str]:
    """Delete the lines of `drop` from gaps.toml; returns the ids removed."""
    drop = set(drop)
    kept, removed = [], []
    for line in path.read_text().splitlines(keepends=True):
        key = line.split("=", 1)[0].strip().strip('"') if line.startswith('"') else None
        if key in drop:
            removed.append(key)
        else:
            kept.append(line)
    path.write_text("".join(kept))
    return removed


def _summary(covs: Sequence[Coverage]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for c in covs:
        k = out.setdefault(c.feature.kind, {"features": 0, "covered": 0} | dict.fromkeys(LAYERS, 0))
        k["features"] += 1
        k["covered"] += int(c.covered)
        for layer in LAYERS:
            k[layer] += int(bool(c.files.get(layer)))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="every feature with its cells")
    ap.add_argument("--uncovered", action="store_true", help="gap lines a new feature needs")
    ap.add_argument("--prune-gaps", action="store_true", help="delete gap lines no longer owed")
    ap.add_argument("--kind", action="append", default=[], help="only this feature kind")
    args = ap.parse_args(argv)
    covs = coverage()
    if args.kind:
        covs = [c for c in covs if c.feature.kind in args.kind]
    if args.prune_gaps:
        result = check_gaps(covs, load_gaps())
        for fid in prune_gaps([*result.stale, *result.unknown]):
            print(f"removed {fid}")
        return 0
    if args.uncovered:
        result = check_gaps(covs, load_gaps())
        sys.stdout.write("".join(gap_line(fid) + "\n" for fid in result.uncovered))
        return 0
    if args.json:
        json.dump(
            [{"id": c.feature.id, **c.cells()} for c in covs],
            sys.stdout,
            indent=1,
        )
    else:
        json.dump(_summary(covs), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
