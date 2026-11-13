"""Open-source neutrality lint.

The repository describes techniques, not products, people or its own history:

1. Public copy (README, quickstart, memory) names no adjacent product, even as a
   category example.
2. The whole source tree names no competitor memory or vector-store product, no
   person, contributor handle, personal address, internal host, repository,
   project or namespace, and not the tracker source's product name; it cites no
   paper and no design-decision record (the PR and the docs carry the why).
3. Server code names no public benchmark dataset as the reason for a choice: it
   states the behaviour and the measured effect. The adapters under bench/ ARE
   those benchmarks and keep their names; a lowercase namespace or agent prefix
   (`locomo-`) is an identifier, not a citation.

The names are kept as salted digests, so this lint names none of them. Excluded, with the reason on each: the migrations (a
deployed ledger checksums every file, so their text is frozen history), vendored
third-party text, and the few test files whose subject is text that carries such a
reference (a parser for pointers in a session transcript, say).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from tests.unit.tracked_files import tracked

ROOT = Path(__file__).resolve().parents[2]

PUBLIC_COPY_FILES = (
    "README.md",
    "i18n/zh-CN/README.md",
    "docs/getting-started/quickstart.md",
    "docs/concepts/memory.md",
)

# The names the repository must not carry: adjacent products, people, contributor
# handles, internal hosts, repositories, projects and namespaces of one deployment, and
# the tracker source's product name. Kept as salted digests so this lint names none of
# them either. A name's digest is its lowercase form, separators as written:
#   python -c 'import hashlib,sys; print(hashlib.sha256(b"loom-public-copy:" + sys.argv[1].encode()).hexdigest()[:16])' <name>
BANNED_DIGESTS = frozenset(
    {
        "019223553f0b5e07",
        "01f3a416c45e252a",
        "0c52ce0ffa591ba8",
        "1321e95ab8255216",
        "17e257a2797cd452",
        "1c350715de618495",
        "283989ba567ec51a",
        "2ec40d25094e9c00",
        "3320129ca98837c9",
        "38957521bc93f876",
        "395d4bcaf29ed263",
        "425e9c6d24d1cfc0",
        "444b8c3e0920d233",
        "467874561477f904",
        "47f364d8b603dcf8",
        "4a767e4fea639659",
        "4aa3248d2b761fe2",
        "5191061343599a0e",
        "541993920ecc1db5",
        "559fc465edeee205",
        "5865a4b052a5fc95",
        "5a80da1f842bb160",
        "5acdd40876802776",
        "5c5d5912049c463b",
        "5dd01599340bdf08",
        "5e83c7a75d7380f4",
        "621f3b25c4fdc3b2",
        "62698bd32fbc0530",
        "6ca571903658b54a",
        "711988538eb118ed",
        "72cfcb785d9cfe9f",
        "741ea518ee031a43",
        "75633bf4de788dea",
        "7a90b18924dc90e4",
        "7e94ed7913da6bb3",
        "89c5fe8ade9d289b",
        "89de11ac38c5805b",
        "8e546cf9c96c49fa",
        "92acaee9c4397a15",
        "9348e28964bfc5d4",
        "96bf17001b2589f1",
        "98711209ae005677",
        "a4e9fea03670fe7c",
        "a9103d27ec81eb6c",
        "aafb017cd8bc8fde",
        "aeb831d00afaf3c4",
        "af96b890924d1287",
        "b7025b291e831af2",
        "bc25c8cf48ee0eed",
        "c6314bdacc696a4a",
        "d0d3283b834e228d",
        "d24f515a1740d751",
        "d4742e066957f8d2",
        "d5f83d20dcfb2736",
        "e0b34c970e71df8c",
        "e80f51916307a1ed",
        "e8cdb5c54d2d5256",
        "ea202b8d8b85457e",
        "eca3f6d456c7109a",
        "f19bdeaa038c8e67",
        "f31b2a7473c2d401",
        "f9c0b22693a6fa06",
        "fa7f0e6f380cf621",
    }
)
_SALT = b"loom-public-copy:"
# A run of words joined by "-", "_", "." or one space; its windows of up to three words
# are checked with the separators the line has, so `name_name` (an identifier),
# `name-name` (a name) and `name name` (prose) are told apart.
_RUN = re.compile(r"[a-z0-9]+(?:[-_. ][a-z0-9]+)*")
_WINDOW = 3
_seen: dict[str, bool] = {}


def _is_banned(window: str) -> bool:
    hit = _seen.get(window)
    if hit is None:
        hit = hashlib.sha256(_SALT + window.encode()).hexdigest()[:16] in BANNED_DIGESTS
        _seen[window] = hit
    return hit


def _banned_in(line: str) -> str:
    """The first banned name in `line`, as the words it was found as, or ""."""
    for run in _RUN.findall(line.lower()):
        parts = re.split(r"([-_. ])", run)
        words, seps = parts[0::2], parts[1::2]
        for i in range(len(words)):
            window = words[i]
            for j in range(i, min(i + _WINDOW, len(words))):
                if j > i:
                    window += seps[j - 1] + words[j]
                if _is_banned(window):
                    return window
    return ""


PATTERNS = {
    "paper citation": re.compile(r"arxiv|\bet al\.", re.IGNORECASE),
    "decision-record citation": re.compile(r"\b[Dd]ecisions? \d{3,5}\b|docs/decisions/\d"),
    "internal host": re.compile(r"\bubuntu-[0-9a-z]{2}(?![\w.])|\.ts\.net\b"),
    # A personal address at the project's domain; the policy inboxes SECURITY.md and
    # CODE_OF_CONDUCT.md name are public.
    "personal address": re.compile(
        r"\b(?!(?:security|conduct)@)[A-Za-z0-9._%+-]+@clickhouse\.com\b", re.IGNORECASE
    ),
}

# Prose spellings of public benchmark datasets, matched case-sensitively under
# server/ only so identifiers (a `locomo-` bench-namespace prefix) are not caught.
BENCHMARK_NAMES = re.compile(r"LoCoMo|LOCOMO|Locomo|LongMemEval|MemoryAgentBench|MemBench")
BENCHMARK_SCOPE = "server/"

SCAN_DIRS = (
    ".claude",
    "server",
    "sdk",
    "docs",
    "bench",
    "frontend/src",
    "frontend/e2e",
    "tests",
    "schemas",
    "integrations",
    "scripts",
    "i18n",
    "dev",
    "deploy",
    "skills",
    "examples",
    ".github",
    ".claude-plugin",
    ".agents/plugins",
)
ROOT_FILES = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CLAUDE.md",
    "AGENTS.md",
    "Dockerfile",
    "Makefile",
    "pyproject.toml",
    "redeploy.sh",
    ".env.example",
    "server.json",
    "context7.json",
    "CITATION.cff",
)
SCAN_SUFFIXES = {
    ".py",
    ".md",
    ".mjs",
    ".js",
    ".ts",
    ".tsx",
    ".sql",
    ".toml",
    ".yml",
    ".yaml",
    ".sh",
    ".css",
    ".html",
    ".json",
    ".jsonl",
}
# Build artifacts, vendored deps and large gitignored datasets are not source.
SKIP_DIR_PARTS = {"node_modules", "dist", "__pycache__", "datasets", ".venv"}
# A deployed ledger checksums every migration, so their text is frozen; the
# schema dump is generated from them.
SKIP_PREFIXES = ("schemas/migrations/", "schemas/schema.sql")

# This lint holds the digests.
SELF = "tests/unit/test_public_copy.py"

# Files whose subject is text carrying a decision reference: parsers and
# classifiers for pointers an agent's transcript or a repository's own ADR
# folder contains (Loom indexes any repository's `docs/decisions/`).
DECISION_REFERENCE_DATA = {
    "tests/unit/test_session_brief.py",
    "tests/unit/test_session_echoes.py",
    "tests/unit/test_task_context_pack.py",
    "tests/unit/test_live_capture_junk.py",
    "tests/unit/test_activity_facts.py",
    "tests/unit/test_knowledge_uri_prefix_input.py",
}


def _iter_scan_files():
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        # `.claude/` also holds worktrees and ignored skills a contributor installed.
        files = (ROOT / p for p in tracked(d)) if d == ".claude" else base.rglob("*")
        for path in sorted(files):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            if SKIP_DIR_PARTS.intersection(path.parts):
                continue
            yield path
    for name in ROOT_FILES:
        if (ROOT / name).is_file():
            yield ROOT / name


def _scanned() -> list[tuple[str, list[str]]]:
    out = []
    for path in _iter_scan_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == SELF or rel.startswith(SKIP_PREFIXES):
            continue
        try:
            out.append((rel, path.read_text(errors="ignore").splitlines()))
        except OSError:
            continue
    return out


def test_public_copy_uses_categories_not_competitor_names() -> None:
    for rel in PUBLIC_COPY_FILES:
        for i, line in enumerate((ROOT / rel).read_text().splitlines(), start=1):
            assert not _banned_in(line), f"{rel}:{i} names a product; use a category instead"


def test_source_tree_names_no_product_person_or_host() -> None:
    violations = [
        f"{rel}:{i}: {line.strip()[:120]!r}"
        for rel, lines in _scanned()
        for i, line in enumerate(lines, start=1)
        if _banned_in(line)
    ]
    assert not violations, (
        "a product, person or internal name; describe the technique or use a neutral "
        "stand-in:\n" + "\n".join(violations)
    )


def test_source_tree_cites_no_paper_record_or_host() -> None:
    violations = []
    for rel, lines in _scanned():
        for kind, pattern in PATTERNS.items():
            if kind == "decision-record citation" and rel in DECISION_REFERENCE_DATA:
                continue
            violations += [
                f"{rel}:{i}: {kind}: {line.strip()[:120]!r}"
                for i, line in enumerate(lines, start=1)
                if pattern.search(line)
            ]
    assert not violations, (
        "say what the code does, not where the idea or the decision came from:\n"
        + "\n".join(violations)
    )


def test_server_code_names_no_benchmark_dataset() -> None:
    violations = [
        f"{rel}:{i}: {m.group(0)!r} in {line.strip()[:120]!r}"
        for rel, lines in _scanned()
        if rel.startswith(BENCHMARK_SCOPE)
        for i, line in enumerate(lines, start=1)
        if (m := BENCHMARK_NAMES.search(line))
    ]
    assert not violations, (
        "a benchmark dataset named in server code; state the behaviour and the measured "
        "effect (bench/ keeps the names):\n" + "\n".join(violations)
    )


def test_a_digest_catches_its_name_in_every_shape_and_nothing_else(monkeypatch) -> None:
    """With a made-up name's digest: prose, identifiers, compounds and hosts carry it;
    a longer word, an underscore form of a hyphenated name, and a spaced form do not."""
    import hashlib as _h
    import sys

    def digest(name: str) -> str:
        return _h.sha256(_SALT + name.encode()).hexdigest()[:16]

    monkeypatch.setattr(
        sys.modules[__name__], "BANNED_DIGESTS", frozenset({digest("acmedb"), digest("acme-cloud")})
    )
    _seen.clear()
    assert _banned_in("we moved off AcmeDB last year") == "acmedb"
    assert _banned_in("the acmedb_memories table") == "acmedb"
    assert _banned_in("see https://acme-cloud.example/docs") == "acme-cloud"
    assert _banned_in("acmedbx and acmedbs") == ""
    assert _banned_in("acme_cloud is an identifier") == ""
    assert _banned_in("an acme cloud deployment") == ""
    _seen.clear()
