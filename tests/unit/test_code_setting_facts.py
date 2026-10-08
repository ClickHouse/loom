"""code.setting_facts: declaration, history with its
release, CI randomization and the test count from the indexed tree; a
negative only with the control green; one failing read never fails the
answer."""

from __future__ import annotations

from typing import Any

import pytest

import server.code.app.settings as h
from server.code.api.models_conventions import CodeSettingFactsInput
from server.code.app.ttl_cache import clear_all
from server.code.domain.index import setting_facts as sf
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

ORG, NS = "clickhouse", "code-clickhouse"

DECL_CHUNK = (
    '- 1 — Enabled.\n)", 0) \\\n'
    '    DECLARE(Bool, use_skip_indexes_if_final, true, R"(\n'
    "Controls whether skipping indexes are used when executing a query with the FINAL modifier.\n"
    "Second sentence.\n"
    ')", 0) \\\n'
    '    DECLARE(UInt64, max_parallel_replicas, 1, R"(\nThe maximum number of replicas.\n)", BETA) \\\n'
    '    DECLARE_WITH_ALIAS(Bool, allow_experimental_x, false, R"(\nExperimental X.\n)", EXPERIMENTAL, enable_x) \\\n'
)
HIST_CHUNKS = [
    {
        "start_line": 28,
        "content": '     addSettingsChanges(settings_changes_history, "26.9",\n        {\n'
        '            {"other", false, true, "New setting"},\n',
    },
    {
        "start_line": 60,
        "content": '            {"use_skip_indexes_if_final", 0, 1, "Change in default value of setting"},\n'
        '        });\n     addSettingsChanges(settings_changes_history, "25.3",\n        {\n',
    },
    {
        "start_line": 90,
        "content": '            {"use_skip_indexes_if_final", false, false, "New setting: skip \\"indexes\\" under FINAL"},\n',
    },
]


# ---- parsers ------------------------------------------------------------------


def test_declaration_parser_reads_type_default_sentence_and_tier() -> None:
    d = sf.parse_declaration(
        DECL_CHUNK, "use_skip_indexes_if_final", path="src/Core/Settings.cpp", chunk_start=1941
    )
    assert d is not None
    assert (d.type, d.default, d.tier, d.line) == ("Bool", "true", "production", 1943)
    assert (
        d.description
        == "Controls whether skipping indexes are used when executing a query with the FINAL modifier."
    )
    beta = sf.parse_declaration(DECL_CHUNK, "max_parallel_replicas", path="p", chunk_start=1)
    assert beta is not None and beta.tier == "beta" and beta.default == "1"
    exp = sf.parse_declaration(DECL_CHUNK, "allow_experimental_x", path="p", chunk_start=1)
    assert exp is not None and exp.tier == "experimental" and exp.alias == "enable_x"
    assert sf.parse_declaration(DECL_CHUNK, "use_skip_indexes", path="p", chunk_start=1) is None
    obs = sf.parse_obsolete(
        "    MAKE_OBSOLETE(M, Bool, s3_disable_checksum, false) \\\n",
        "s3_disable_checksum",
        path="src/Core/Settings.cpp",
        chunk_start=9401,
    )
    assert (
        obs is not None and obs.tier == "obsolete" and obs.default == "false" and obs.line == 9401
    )


def test_history_parser_attributes_entries_to_the_header_above_across_chunks() -> None:
    entries = sf.parse_history(HIST_CHUNKS, "use_skip_indexes_if_final")
    assert [(e.version, e.old, e.new) for e in entries] == [
        ("26.9", "0", "1"),
        ("25.3", "false", "false"),
    ]
    assert entries[0].reason == "Change in default value of setting" and entries[0].line == 60
    assert entries[1].reason == 'New setting: skip "indexes" under FINAL'
    assert sf.parse_history(HIST_CHUNKS, "nope") == []


def test_randomizer_parser_returns_sampler_and_class() -> None:
    rows = [
        {
            "start_line": 1836,
            "scope": "class SettingsRandomizer",
            "excerpt": '"use_query_condition_cache": lambda: random.randint(0, 1),\n        '
            '"use_skip_indexes_if_final": lambda: random.randint(0, 1),\n        "use_skip_indexes_on',
        }
    ]
    r = sf.parse_randomization(rows, "use_skip_indexes_if_final")
    assert r.randomized and r.sampler == "lambda: random.randint(0, 1)" and r.line == 1836
    assert r.randomizer == "SettingsRandomizer" and r.as_dict()["file"] == sf.RANDOMIZER_FILE
    mt = sf.parse_randomization(
        [
            {
                "start_line": 1990,
                "scope": "class MergeTreeSettingsRandomizer",
                "excerpt": '"ratio_of_defaults_for_sparse_serialization": threshold_generator(\n            0.3, 0.5, 0.0, 1.0\n        ),\n        "compute',
            }
        ],
        "ratio_of_defaults_for_sparse_serialization",
    )
    assert mt.randomized and mt.sampler == "threshold_generator( 0.3, 0.5, 0.0, 1.0 )"
    assert mt.randomizer == "MergeTreeSettingsRandomizer"
    assert sf.parse_randomization([], "x").as_dict() == {"randomized": False}


def test_verdict_needs_the_control_before_a_negative() -> None:
    facts = sf.Facts(name="zz_nope")
    assert sf.verdict_of(facts, control_ok=False) == ("unknown", ["control_failed"])
    assert sf.verdict_of(facts, control_ok=True) == ("not_a_setting", ["no_declaration"])
    facts.declaration = sf.Declaration(file="f", type="Bool")
    assert sf.verdict_of(facts, control_ok=True) == ("known", [])


# ---- handler -----------------------------------------------------------------


class _Repo:
    def __init__(self, *, history_fails: bool = False) -> None:
        self.history_fails = history_fails
        self.calls: list[tuple[str, Any]] = []

    async def grep_chunks(
        self, org, ns, pattern, *, regex=False, path_prefix="", limit=60, scope=None
    ):
        self.calls.append(("grep_chunks", pattern))
        if "max_threads" in pattern or "use_skip_indexes_if_final" in pattern:
            if pattern.startswith("MAKE_OBSOLETE"):
                return []
            name = "max_threads" if "max_threads" in pattern else "use_skip_indexes_if_final"
            return [
                {
                    "path": "src/Core/Settings.cpp",
                    "start_line": 1941,
                    "content": DECL_CHUNK.replace("use_skip_indexes_if_final", name),
                }
            ]
        return []

    async def chunks_of_path(self, org, ns, path, *, limit=400):
        self.calls.append(("chunks_of_path", path))
        if self.history_fails:
            raise RuntimeError("boom")
        return HIST_CHUNKS

    async def chunk_contains(self, org, ns, path, literal, *, limit=10):
        self.calls.append(("chunk_contains", literal))
        if "use_skip_indexes_if_final" in literal:
            return [
                {
                    "start_line": 1836,
                    "scope": "class SettingsRandomizer",
                    "excerpt": '"use_skip_indexes_if_final": lambda: random.randint(0, 1),\n "x',
                }
            ]
        return []

    async def grep_files(
        self,
        org,
        ns,
        pattern,
        *,
        regex=False,
        case_sensitive=True,
        path_prefix="",
        limit=200,
        scope=None,
    ):
        self.calls.append(("grep_files", pattern))
        return [], 21 if "use_skip" in pattern else 0


@pytest.fixture
def wired(monkeypatch):
    holder: dict[str, Any] = {}
    monkeypatch.setattr(h, "CodeRepo", lambda: holder["repo"])
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    clear_all()

    def _use(repo: _Repo) -> _Repo:
        holder["repo"] = repo
        return repo

    yield _use
    clear_all()


def test_op_is_registered_experimental_viewer_read() -> None:
    op = OPERATIONS_BY_NAME["code.setting_facts"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent


@pytest.mark.asyncio
async def test_handler_gathers_the_four_facts(wired) -> None:
    repo = wired(_Repo())
    out = await h.handle_code_setting_facts(
        CodeSettingFactsInput(org=ORG, namespace=NS, name="use_skip_indexes_if_final")
    )
    assert out.verdict == "known" and out.causes == [] and out.tier == "production"
    assert out.declaration["file"] == "src/Core/Settings.cpp" and out.declaration["line"] == 1943
    assert [(x["version"], x["old"], x["new"]) for x in out.history] == [
        ("26.9", "0", "1"),
        ("25.3", "false", "false"),
    ]
    assert (
        out.randomized_in_ci["randomized"]
        and out.randomized_in_ci["sampler"] == "lambda: random.randint(0, 1)"
    )
    assert out.tests_total == 21 and out.control == {"setting": "max_threads", "ok": True}
    # the history file is read once per process, then cached
    await h.handle_code_setting_facts(
        CodeSettingFactsInput(org=ORG, namespace=NS, name="use_skip_indexes_if_final")
    )
    assert sum(1 for c in repo.calls if c[0] == "chunks_of_path") == 1


@pytest.mark.asyncio
async def test_unknown_name_is_not_a_setting_only_with_the_control_green(wired) -> None:
    wired(_Repo())
    out = await h.handle_code_setting_facts(
        CodeSettingFactsInput(org=ORG, namespace=NS, name="zz_definitely_missing")
    )
    assert out.verdict == "not_a_setting" and out.causes == ["no_declaration"] and out.control["ok"]
    assert out.declaration == {} and out.history == [] and not out.randomized_in_ci["randomized"]
    out = await h.handle_code_setting_facts(
        CodeSettingFactsInput(org=ORG, namespace=NS, name="bad name!")
    )
    assert out.verdict == "not_a_setting" and out.causes == ["invalid_name"]


@pytest.mark.asyncio
async def test_one_failing_read_is_named_not_fatal(wired) -> None:
    wired(_Repo(history_fails=True))
    out = await h.handle_code_setting_facts(
        CodeSettingFactsInput(org=ORG, namespace=NS, name="use_skip_indexes_if_final")
    )
    assert out.verdict == "known" and out.causes == ["history_unread"] and out.history == []
    assert out.randomized_in_ci["randomized"]


def test_plain_string_descriptions_and_alias_declarations_are_found() -> None:
    """Shapes from the 248 CI-randomized settings: three declared with a
    plain C string instead of R"(...)", one reachable only as the alias of a
    DECLARE_WITH_ALIAS whose head sits in the previous chunk."""
    plain = (
        '    DECLARE(Bool, query_plan_read_in_order_through_join, true, "Keep reading in order from the left '
        'table in JOIN operations, which can be utilized by subsequent steps.", 0) \\\n'
    )
    d = sf.parse_declaration(
        plain, "query_plan_read_in_order_through_join", path="p", chunk_start=6923
    )
    assert d is not None and d.type == "Bool" and d.default == "true" and d.tier == "production"
    assert d.description.startswith("Keep reading in order from the left table")
    tail = (
        '_for_remote_reading, 2 * DBMS_DEFAULT_BUFFER_SIZE, R"(\nMin bytes to read per task.\n)", 0, '
        'filesystem_prefetch_min_bytes_for_single_read_task) \\\n    DECLARE(Bool, merge_tree_x, true, R"(\nX.\n)", 0) \\\n'
    )
    a = sf.parse_declaration(
        tail, "filesystem_prefetch_min_bytes_for_single_read_task", path="p", chunk_start=7253
    )
    assert a is not None and a.is_alias and a.tier == "production" and a.type == ""
    assert a.description == "Min bytes to read per task." and a.as_dict()["is_alias"] is True
    full = (
        "    DECLARE_WITH_ALIAS(UInt64, filesystem_prefetch_min_bytes_for_remote_reading, 2 * DBMS_DEFAULT_BUFFER_SIZE, "
        'R"(\nMin bytes to read per task.\n)", 0, filesystem_prefetch_min_bytes_for_single_read_task) \\\n'
    )
    b = sf.parse_declaration(
        full, "filesystem_prefetch_min_bytes_for_single_read_task", path="p", chunk_start=1
    )
    assert (
        b is not None
        and b.is_alias
        and b.alias == "filesystem_prefetch_min_bytes_for_remote_reading"
    )
    assert b.type == "UInt64" and b.default == "2 * DBMS_DEFAULT_BUFFER_SIZE"
    # the primary still resolves as itself and names its alias
    c = sf.parse_declaration(
        full, "filesystem_prefetch_min_bytes_for_remote_reading", path="p", chunk_start=1
    )
    assert (
        c is not None
        and not c.is_alias
        and c.alias == "filesystem_prefetch_min_bytes_for_single_read_task"
    )

    quoted = (
        '    DECLARE(String, auto_statistics_types, "basic, uniq_v2", R"(\nComma-separated list of statistics '
        'types to calculate automatically on all suitable columns.\nSupported.\n)", 0) \\\n'
    )
    q = sf.parse_declaration(quoted, "auto_statistics_types", path="p", chunk_start=2136)
    assert q is not None and q.default == '"basic, uniq_v2"' and q.type == "String"
    assert q.description.startswith("Comma-separated list of statistics types")

    # the chunk ends inside the description (the audit's real max_threads chunk):
    # type, default and the visible description are still read; the tier is
    # the default (production) because the flags word is out of reach
    cut = (
        '    DECLARE(UInt64, max_threads_for_indexes, 0, R"(\nThe maximum number of threads process indices.\n)", 0) \\\n'
        '    DECLARE(MaxThreads, max_threads, 0, R"(\nThe maximum number of query processing threads, excluding threads'
    )
    t = sf.parse_declaration(cut, "max_threads", path="p", chunk_start=362)
    assert t is not None and t.type == "MaxThreads" and t.default == "0" and t.tier == "production"
    assert t.description.startswith("The maximum number of query processing threads")
