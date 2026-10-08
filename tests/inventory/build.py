"""One inventory row per tracked file under `tests/`.

Derived fields come from the path and a static scan of the file. Judgement
fields come from `annotations/*.toml`. Run
`python -m tests.inventory.build --json` for the full inventory and
`--summary` for counts by workflow, layer and status.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ANNOTATIONS_DIR = HERE / "annotations"

# The annotation files, one per area. A new area is a new file here and in
# `annotations/`.
AREAS: tuple[str, ...] = ("suite", "analyzer", "integration", "code", "memory", "platform")

LAYERS: tuple[str, ...] = (
    "unit",
    "contract",
    "analyzer",
    "fixture",
    "integration",
    "typing",
    "snapshot",
    "support",
)
STATUSES: tuple[str, ...] = (
    "unreviewed",
    "retain",
    "refactor",
    "consolidate",
    "generate",
    "archive",
    "deprecate",
    "remove",
    "investigate",
)
TAGS: tuple[str, ...] = (
    "positive",
    "negative",
    "boundary",
    "fault",
    "security",
    "concurrency",
    "recovery",
)
TIERS: tuple[str, ...] = (
    "unit",
    "integration",
    "slow",
    "destructive",
    "provider",
    "recovery",
    "manual",
)

_LANG_WORKFLOWS: tuple[str, ...] = (
    "cmake",
    "fbs",
    "go",
    "helm",
    "hosts",
    "java",
    "js",
    "md",
    "prisma",
    "proto",
    "python",
    "rust",
    "shell",
    "sql",
    "tla",
    "ts",
    "vue",
)

# Longest matching prefix wins. A path under a prefix nobody claims (a new
# fixture language, a new top-level directory) has no workflow and fails the
# gate, so the new directory gets a deliberate home.
# Directories whose workflow covers their own files but not their subdirectories.
_DIRECT_CHILDREN: tuple[tuple[str, str], ...] = (
    ("tests/", "suite"),
    ("tests/fixtures/", "fixtures"),
)
_WORKFLOW_PREFIXES: tuple[tuple[str, str], ...] = (
    ("tests/inventory/", "suite"),
    ("tests/analyzer/cases/", "analyzer-cases"),
    ("tests/analyzer/", "analyzer"),
    ("tests/code_lanes/", "code-lanes"),
    ("tests/fixtures/code_lanes/NOTICE.md", "code-lane-corpus"),
    ("tests/fixtures/code_lanes/expected.json", "code-lane-corpus"),
    *((f"tests/fixtures/code_lanes/{lang}/", f"fixtures-{lang}") for lang in _LANG_WORKFLOWS),
    ("tests/integration/", "integration"),
    ("tests/unit/fixtures/", "unit-fixtures"),
    ("tests/unit/sdk_typing/", "sdk-typing"),
    ("tests/unit/snapshots/", "snapshots"),
    ("tests/unit/", "unit"),
)

# A unit test whose name says it pins a published surface or a cross-module
# contract. An annotation may set `layer` for anything this misses.
_CONTRACT_NAME = re.compile(
    r"(contract|parity|surface|registry|snapshot|public_api|wire|openapi|generated|codegen)"
)

# Literal substrings, not regexes: the scan reads ~12 MB per gate run.
# Fixtures and names that mean a live service is needed.
_SERVICE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("loom-server", ("control_plane_url", "LOOM_BASE_URL")),
    ("clickhouse", ("ch_client", "clickhouse_connect", "LOOM_CH_HOST")),
    ("provider", ("require_real_embedder", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")),
    ("subprocess", ("subprocess.run(", "subprocess.Popen(", "check_output(", "check_call(")),
    ("git", ('"git"', "'git'")),
    ("docker", ('"docker"', "'docker'")),
)
_ISOLATION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("monkeypatch", ("monkeypatch",)),
    ("tmp_path", ("tmp_path",)),
    ("settings_override", ("settings_override",)),
    ("unique-ids", ("uuid4", "urandom", "token_hex", "secrets.token_")),
    ("finalizer", ("addfinalizer", "ExitStack", "finally:")),
)
_CLOCK_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("seeded-rng", ("random.seed(", "Random(", "default_rng(", "seed=")),
    ("wall-clock", ("time.time()", "datetime.now(", "utcnow()", "date.today(")),
    ("injected-clock", ("_utcnow", "freeze_time", "fake_clock", "FakeClock")),
)
_MARKER = re.compile(r"pytest\.mark\.([A-Za-z_]\w*)")
_IMPORT = re.compile(r"^\s*(?:from|import)\s+((?:server|loom|bench|scripts)(?:\.\w+)*)", re.M)

# Words in a file name that say which risk class the file exercises.
_NAME_TAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "security",
        re.compile(
            r"(auth|token|grant|scope|leak|secur|redact|isolation|refus|disclos|tenant|jwt|"
            r"permission|role|erasure|sensitive|blacklist|secret)"
        ),
    ),
    ("concurrency", re.compile(r"(concurren|race|lease|lock|parallel|thread|contention)")),
    ("fault", re.compile(r"(fault|fail|error|timeout|retry|unavailable|crash|poison|degrad)")),
    ("recovery", re.compile(r"(recover|resume|restart|rollback|repair|reconcile|drain)")),
    ("boundary", re.compile(r"(bound|limit|budget|cap|edge|overflow|trunc|size|floor|ceiling)")),
)


@dataclass(frozen=True)
class Annotation:
    """One `[[files]]` entry of an annotation file."""

    area: str
    match: str
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class Row:
    path: str
    workflow: str | None
    layer: str
    owner: str
    status: str
    tier: str
    targets: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    isolation: tuple[str, ...] = ()
    clock: tuple[str, ...] = ()
    covers: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    runtime: str = ""
    flake: str = ""
    evidence: str = ""
    notes: str = ""
    smells: tuple[str, ...] = field(default=())
    # False for a file that lists feature names without testing them (an
    # allow-list lint): its mentions do not count as coverage in the ledger.
    references: bool = True


_ANNOTATION_FIELDS: dict[str, type | tuple[type, ...]] = {
    "match": str,
    "owner": str,
    "status": str,
    "layer": str,
    "tier": str,
    "tags": list,
    "covers": list,
    "duplicates": list,
    "runtime": str,
    "flake": str,
    "evidence": str,
    "notes": str,
    "references": bool,
}
_LIST_FIELDS = frozenset({"tags", "covers", "duplicates"})


def tracked_files(root: Path = ROOT) -> list[str]:
    """Every file under `tests/` that git tracks, sorted."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "tests"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return sorted(p for p in out.decode().split("\0") if p)


def workflow_of(path: str) -> str | None:
    best: tuple[int, str] | None = None
    for prefix, slug in _WORKFLOW_PREFIXES:
        if (path == prefix or path.startswith(prefix)) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), slug)
    if best:
        return best[1]
    for prefix, slug in _DIRECT_CHILDREN:
        if path.startswith(prefix) and "/" not in path[len(prefix) :]:
            return slug
    return None


def layer_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if path.startswith("tests/unit/snapshots/"):
        return "snapshot"
    if path.startswith("tests/unit/sdk_typing/"):
        return "typing"
    if path.startswith(("tests/fixtures/", "tests/unit/fixtures/")):
        return "fixture"
    if not (name.startswith("test_") and name.endswith(".py")):
        return "support"
    if path.startswith("tests/integration/"):
        return "integration"
    if path.startswith("tests/analyzer/"):
        return "analyzer"
    if _CONTRACT_NAME.search(name):
        return "contract"
    return "unit"


def default_tier(path: str, markers: Iterable[str]) -> str:
    marks = set(markers)
    for tier in ("destructive", "provider", "recovery", "slow"):
        if tier in marks:
            return tier
    if path.startswith(("tests/integration/", "tests/analyzer/")):
        return "integration"
    return "unit"


def load_annotations(directory: Path = ANNOTATIONS_DIR) -> list[Annotation]:
    """Every `[[files]]` entry of every area file, in file order.

    Raises ValueError naming the file and entry for an unknown area, an
    unknown field or a value of the wrong type or vocabulary.
    """
    out: list[Annotation] = []
    for path in sorted(directory.glob("*.toml")):
        area = path.stem
        if area not in AREAS:
            raise ValueError(f"{path.name}: unknown area {area!r} (known: {', '.join(AREAS)})")
        data = tomllib.loads(path.read_text())
        extra = set(data) - {"files"}
        if extra:
            raise ValueError(f"{path.name}: unknown top-level keys {sorted(extra)}")
        for i, entry in enumerate(data.get("files", [])):
            _validate_entry(path.name, i, entry)
            out.append(Annotation(area=area, match=entry["match"], fields=entry))
    return out


def _validate_entry(fname: str, i: int, entry: Mapping[str, Any]) -> None:
    where = f"{fname} files[{i}]"
    if "match" not in entry:
        raise ValueError(f"{where}: missing `match`")
    for key, value in entry.items():
        want = _ANNOTATION_FIELDS.get(key)
        if want is None:
            raise ValueError(f"{where}: unknown field {key!r}")
        if not isinstance(value, want):
            raise ValueError(f"{where}: {key} must be {want}")
    vocab = {"status": STATUSES, "layer": LAYERS, "tier": TIERS}
    for key, allowed in vocab.items():
        if key in entry and entry[key] not in allowed:
            raise ValueError(f"{where}: {key}={entry[key]!r} not in {allowed}")
    for tag in entry.get("tags", ()):
        if tag not in TAGS:
            raise ValueError(f"{where}: tag {tag!r} not in {TAGS}")
    if entry.get("status") == "remove" and not entry.get("evidence"):
        raise ValueError(f"{where}: status=remove needs `evidence`")


def matches(pattern: str, path: str) -> bool:
    """`fnmatch`, where `*` also crosses `/`: `tests/fixtures/go/*` covers descendants."""
    return fnmatch.fnmatchcase(path, pattern)


def merge_annotations(path: str, annotations: Sequence[Annotation]) -> dict[str, Any]:
    """The fields every matching entry sets for `path`. Lists union; two
    entries that set a scalar field to different values raise ValueError."""
    merged: dict[str, Any] = {}
    origin: dict[str, str] = {}
    for ann in annotations:
        if not matches(ann.match, path):
            continue
        for key, value in ann.fields.items():
            if key == "match":
                continue
            if key in _LIST_FIELDS:
                merged[key] = sorted({*merged.get(key, ()), *value})
            elif key in merged and merged[key] != value:
                raise ValueError(
                    f"{path}: {key} is {merged[key]!r} from {origin[key]} "
                    f"and {value!r} from {ann.area}:{ann.match}"
                )
            else:
                merged[key] = value
                origin[key] = f"{ann.area}:{ann.match}"
    return merged


@cache
def read_text(path: str) -> str:
    try:
        return (ROOT / path).read_text(errors="replace")
    except (FileNotFoundError, IsADirectoryError):
        return ""


def _hits(hints: Iterable[tuple[str, tuple[str, ...]]], text: str) -> tuple[str, ...]:
    return tuple(name for name, needles in hints if any(n in text for n in needles))


def scan(path: str) -> dict[str, tuple[str, ...]]:
    """Static facts about a Python test file; empty for anything else."""
    if not path.endswith(".py") or layer_of(path) == "fixture":
        return {}
    text = read_text(path)
    name = path.rsplit("/", 1)[-1].lower()
    tags = {t for t, rx in _NAME_TAGS if rx.search(name)}
    if "pytest.raises" in text or "status_code == 4" in text:
        tags.add("negative")
    if "def test_" in text:
        tags.add("positive")
    smells = []
    if "async def test_" in text and "time.sleep(" in text:
        smells.append("blocking-sleep-in-async-file")
    if "pytest.mark.flaky" in text:
        smells.append("reruns")
    return {
        "targets": tuple(sorted(set(_IMPORT.findall(text)))),
        "markers": tuple(sorted(set(_MARKER.findall(text)) - {"asyncio"})),
        "tags": tuple(sorted(tags)),
        "services": _hits(_SERVICE_HINTS, text),
        "isolation": _hits(_ISOLATION_HINTS, text),
        "clock": _hits(_CLOCK_HINTS, text),
        "smells": tuple(smells),
    }


def _owner(path: str, targets: Sequence[str]) -> str:
    """The server package most imported, else the workflow."""
    pkgs = Counter(
        ".".join(t.split(".")[:3]) for t in targets if t.startswith("server.") and t.count(".") >= 1
    )
    if pkgs:
        return pkgs.most_common(1)[0][0]
    if any(t == "loom" or t.startswith("loom.") for t in targets):
        return "sdk.python"
    return workflow_of(path) or "unowned"


def build_rows(
    files: Sequence[str] | None = None, annotations: Sequence[Annotation] | None = None
) -> list[Row]:
    files = tracked_files() if files is None else files
    annotations = load_annotations() if annotations is None else annotations
    rows = []
    for path in files:
        facts = scan(path)
        ann = merge_annotations(path, annotations)
        targets = facts.get("targets", ())
        markers = facts.get("markers", ())
        rows.append(
            Row(
                path=path,
                workflow=workflow_of(path),
                layer=ann.get("layer", layer_of(path)),
                owner=ann.get("owner", _owner(path, targets)),
                status=ann.get("status", "unreviewed"),
                tier=ann.get("tier", default_tier(path, markers)),
                targets=targets,
                markers=markers,
                tags=tuple(sorted({*facts.get("tags", ()), *ann.get("tags", ())})),
                services=facts.get("services", ()),
                isolation=facts.get("isolation", ()),
                clock=facts.get("clock", ()),
                covers=tuple(ann.get("covers", ())),
                duplicates=tuple(ann.get("duplicates", ())),
                runtime=ann.get("runtime", ""),
                flake=ann.get("flake", ""),
                evidence=ann.get("evidence", ""),
                notes=ann.get("notes", ""),
                smells=facts.get("smells", ()),
                references=ann.get("references", True),
            )
        )
    return rows


def unmatched_annotations(files: Sequence[str], annotations: Sequence[Annotation]) -> list[str]:
    """`area:match` for every entry that matches no tracked file."""
    return [
        f"{a.area}:{a.match}" for a in annotations if not any(matches(a.match, f) for f in files)
    ]


def summary(rows: Iterable[Row]) -> dict[str, dict[str, int]]:
    rows = list(rows)
    return {
        key: dict(sorted(Counter(str(getattr(r, key)) for r in rows).items()))
        for key in ("workflow", "layer", "status", "tier")
    } | {"smells": dict(Counter(s for r in rows for s in r.smells))}


def _iter_json(rows: Iterable[Row]) -> Iterator[dict[str, Any]]:
    for r in rows:
        yield {k: v for k, v in asdict(r).items() if v not in ("", ())}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="every row as a JSON list")
    ap.add_argument("--summary", action="store_true", help="counts by workflow/layer/status")
    ap.add_argument("--path", action="append", default=[], help="only rows matching this glob")
    args = ap.parse_args(argv)
    rows = build_rows()
    if args.path:
        rows = [r for r in rows if any(matches(p, r.path) for p in args.path)]
    if args.summary or not args.json:
        json.dump(summary(rows), sys.stdout, indent=2)
    else:
        json.dump(list(_iter_json(rows)), sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
