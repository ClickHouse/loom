"""code.test_home and code.coverage_claim: the fingerprint is
normalised, homes rank by parts matched then the test map, verdicts follow
the positive control, and a claim of `uncovered` is never made when the
control failed."""

from __future__ import annotations

import pytest

import server.code.app.coverage as h
from server.code.api.models_testing import CodeCoverageClaimInput, CodeTestHomeInput
from server.code.domain.index import coverage_claim as cc

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

ST = cc.STATELESS
ORG, NS = "clickhouse", "code-clickhouse"


# ---- pure helpers -------------------------------------------------------------


def test_fingerprint_normalises_parts() -> None:
    fp = cc.normalize_fingerprint(
        ["src/Storages/MergeTree/MergeTreeData.cpp", "Settings", "bad stem!"],
        ["max_threads", "Bad Setting", "optimize_read_in_order"],
        "ReplicatedMergeTree('/x', 'r')",
        ["DB::IColumn::insert"],
    )
    assert fp.stems == ("MergeTreeData", "Settings")
    assert fp.settings == ("max_threads", "optimize_read_in_order")
    assert fp.engine == "ReplicatedMergeTree" and fp.symbols == ("DB::IColumn::insert",)
    assert cc.grep_pattern("engine", fp.engine) == "ENGINE = ReplicatedMergeTree"
    assert cc.grep_pattern("symbol", "DB::IColumn::insert") == "insert"


def test_rank_homes_prefers_files_matching_more_parts_then_map() -> None:
    parts = [("setting", "max_threads"), ("engine", "MergeTree")]
    hits = {
        ("setting", "max_threads"): [
            {"path": f"{ST}02994_a.sql", "chunks": 3},
            {"path": f"{ST}00001_b.sql", "chunks": 1},
            {"path": "src/not_a_test.cpp", "chunks": 9},
            {"path": f"{ST}02994_a.reference", "chunks": 1},
        ],
        ("engine", "MergeTree"): [
            {"path": f"{ST}02994_a.sql", "chunks": 3},
            {"path": f"{ST}00777_engine_only.sql", "chunks": 1},
        ],
    }
    tm = [{"test_path": f"{ST}00001_b.sql", "confidence": 0.8}]
    ranked = cc.rank_homes(parts, hits, tm, limit=5)
    assert [c.path for c in ranked] == [
        f"{ST}02994_a.sql",
        f"{ST}00001_b.sql",
        f"{ST}00777_engine_only.sql",
    ]
    assert ranked[0].matched == ["setting:max_threads", "engine:MergeTree"]
    assert cc.home_verdict(parts, ranked, control_ok=True) == cc.ADD_CASE_TO
    assert cc.home_verdict(parts, ranked[1:], control_ok=True) == cc.NEW_FILE_JUSTIFIED
    # an engine-only match never names a home
    assert cc.home_verdict(parts, ranked[2:], control_ok=True) == cc.NEW_FILE_JUSTIFIED
    assert cc.home_verdict(parts, ranked, control_ok=False) == cc.UNKNOWN
    assert cc.home_verdict([], [], control_ok=True) == cc.UNKNOWN


def test_stem_tokens_and_name_tiebreak() -> None:
    assert cc.stem_tokens(("MergeTreeIndexText",)) == {"text", "index"}
    toks = cc.stem_tokens(("MergeTreeIndexText", "SettingsQuirks"))
    assert "text" in toks and "quirks" in toks and "merge" not in toks
    parts = [("stem", "MergeTreeIndexText"), ("setting", "allow_experimental_full_text_index")]
    hits = {
        ("setting", "allow_experimental_full_text_index"): [
            {"path": f"{ST}03100_lwu_48_text_index.sql", "chunks": 2},
            {"path": f"{ST}02346_text_index_postprocessor_basic.sql", "chunks": 2},
            {"path": f"{ST}00900_unrelated.sql", "chunks": 2},
        ]
    }
    ranked = cc.rank_homes(parts, hits, [], limit=5)
    assert ranked[0].path == f"{ST}02346_text_index_postprocessor_basic.sql"
    assert ranked[-1].path == f"{ST}00900_unrelated.sql"
    # the stem matched no test: it does not raise the bar
    assert cc.home_verdict(parts, ranked, control_ok=True, live_parts=1) == cc.ADD_CASE_TO
    assert cc.home_verdict(parts, ranked, control_ok=True, live_parts=2) == cc.NEW_FILE_JUSTIFIED


def test_is_test_file() -> None:
    assert cc.is_test_file(f"{ST}02994_sanity_check_settings.sql")
    assert cc.is_test_file(f"{ST}03100_lwu_48_text_index.sh")
    assert cc.is_test_file(f"{cc.INTEGRATION}test_quota/test.py")
    assert not cc.is_test_file(f"{ST}02994_sanity_check_settings.reference")
    assert not cc.is_test_file(f"{cc.INTEGRATION}test_quota/configs/a.xml")
    assert not cc.is_test_file("src/Core/Settings.cpp")


def test_coverage_verdict_and_epistemic() -> None:
    assert cc.coverage_verdict([], control_ok=True) == cc.UNCOVERED
    assert cc.coverage_verdict([], control_ok=False) == cc.UNKNOWN
    assert cc.coverage_verdict([{"test_path": "t"}], control_ok=False) == cc.UNKNOWN
    assert cc.epistemic_of([{"method": "pr_cooccurrence"}]) == "lower_bound"
    assert cc.epistemic_of([{"method": "content_reference"}]) == "exact"
    assert cc.epistemic_of([{"method": "grep"}]) == "exact"


# ---- handlers over a stub repository ------------------------------------------


class _Repo:
    def __init__(self, *, control_hit: bool = True) -> None:
        self.control_hit = control_hit
        self.greps: list[tuple[str, str]] = []

    async def facets_present(self, org, ns):
        return False  # a namespace without statement records: every part greps

    async def tests_for_facets(self, org, ns, facets, *, limit=50):
        return [], 0

    async def grep_files(
        self, org, ns, pattern, *, regex, case_sensitive, path_prefix, limit, scope=None
    ):
        self.greps.append((pattern, path_prefix))
        if pattern == "arrayJoin":
            hit = [{"path": f"{path_prefix}00000_control.sql", "chunks": 1, "first_line": 1}]
            return (hit if self.control_hit else []), (1 if self.control_hit else 0)
        if pattern == "max_threads" and path_prefix == ST:
            return [
                {"path": f"{ST}02994_sanity_check_settings.sql", "chunks": 4, "first_line": 3}
            ], 1
        if pattern == "ENGINE = MergeTree" and path_prefix == ST:
            # thousands of files upstream: the list is TRUNCATED (total > rows),
            # and the home file is not in the returned page
            return [
                {"path": f"{ST}00100_other.sql", "chunks": 1, "first_line": 1},
                {"path": f"{ST}00101_other.sql", "chunks": 1, "first_line": 1},
            ], 4000
        if pattern == "ENGINE = MergeTree" and path_prefix == cc.INTEGRATION:
            return [
                {"path": f"{cc.INTEGRATION}test_x/configs/a.xml", "chunks": 1, "first_line": 1}
            ], 1
        if pattern == "parseQuotaKey":
            return [{"path": f"{ST}01234_quota.sql", "chunks": 1, "first_line": 7}], 1
        return [], 0

    async def paths_containing(self, org, ns, paths, pattern):
        self.greps.append((f"member:{pattern}", ",".join(sorted(paths))))
        if pattern == "ENGINE = MergeTree":
            return [p for p in paths if p.endswith("02994_sanity_check_settings.sql")]
        return []

    async def paths_by_stem(self, org, ns, stems, *, limit=40):
        return (
            [{"path": "src/Core/Settings.cpp", "stem": "Settings"}] if "Settings" in stems else []
        )

    async def max_stateless_test_number(self, org, ns):
        return 4100

    async def test_map_for(self, org, ns, paths, *, limit=30):
        return (
            [
                {
                    "src_path": "src/Core/Settings.cpp",
                    "test_path": f"{ST}02994_sanity_check_settings.sql",
                    "confidence": 0.7,
                }
            ]
            if paths
            else []
        )

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        return (
            [{"path": "src/Access/QuotaCache.cpp", "qualified_name": f"DB::{name}"}]
            if name == "parseQuotaKey"
            else []
        )

    async def coverage_rows_for(self, org, ns, paths, *, limit=2000):
        return []

    async def coverage_files_for(self, org, ns, paths):
        return {}

    async def test_map_detail(self, org, ns, paths, *, limit=60):
        if "src/Access/QuotaCache.cpp" in paths:
            return [
                {
                    "src_path": "src/Access/QuotaCache.cpp",
                    "test_path": f"{ST}01234_quota.sql",
                    "method": "content_reference",
                    "confidence": 0.6,
                    "evidence": "parseQuotaKey",
                },
                {
                    "src_path": "src/Access/QuotaCache.cpp",
                    "test_path": "tests/integration/test_quota/test.py",
                    "method": "pr_cooccurrence",
                    "confidence": 0.3,
                    "evidence": "",
                },
            ]
        return []


@pytest.fixture
def plane(monkeypatch):
    h.reset_control_cache()  # The control is kept per namespace
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    repo = _Repo()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    return repo


@pytest.mark.asyncio
async def test_test_home_names_the_existing_suite_file(plane) -> None:
    out = await h.handle_code_test_home(
        CodeTestHomeInput(
            org=ORG,
            namespace=NS,
            file_stems=["Settings"],
            settings=["max_threads"],
            engine="MergeTree",
        )
    )
    assert out.verdict == cc.ADD_CASE_TO
    assert out.home == f"{ST}02994_sanity_check_settings.sql"
    assert out.candidates[0]["matched"] == ["setting:max_threads", "engine:MergeTree"]
    assert out.candidates[0]["map_confidence"] == 0.7
    assert out.next_free_stateless_number == 4101 and out.control == {
        "symbol": "arrayJoin",
        "ok": True,
    }
    # both test trees were searched for every part, and the truncated engine
    # part was resolved by membership over the rare part's candidates
    assert (("max_threads", ST) in plane.greps) and (("max_threads", cc.INTEGRATION) in plane.greps)
    assert any(g[0] == "member:ENGINE = MergeTree" for g in plane.greps)
    # a config file under integration is never a candidate
    assert not any("configs/" in c["path"] for c in out.candidates)


@pytest.mark.asyncio
async def test_test_home_is_unknown_when_the_control_fails(plane, monkeypatch) -> None:
    plane.control_hit = False
    out = await h.handle_code_test_home(
        CodeTestHomeInput(org=ORG, namespace=NS, settings=["max_threads"])
    )
    assert out.verdict == cc.UNKNOWN and out.home == "" and out.control["ok"] is False
    assert out.candidates  # the evidence is still returned, only the verdict abstains


@pytest.mark.asyncio
async def test_coverage_claim_merges_map_and_grep_evidence(plane) -> None:
    out = await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, symbol="parseQuotaKey")
    )
    assert out.verdict == cc.COVERED_BY and out.epistemic == "exact"
    assert out.src_paths == ["src/Access/QuotaCache.cpp"]
    first = out.tests[0]
    assert first["test_path"] == f"{ST}01234_quota.sql"
    assert set(first["methods"]) == {"content_reference", "grep"} and first["confidence"] == 0.95
    assert any(t["test_path"].startswith("tests/integration/") for t in out.tests)


@pytest.mark.asyncio
async def test_coverage_claim_uncovered_only_with_a_passing_control(plane) -> None:
    out = await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, setting="nonexistent_setting_x")
    )
    assert out.verdict == cc.UNCOVERED and out.tests == []
    plane.control_hit = False
    h.reset_control_cache()  # The control is kept per namespace; the stub's answer changed
    out = await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, setting="nonexistent_setting_x")
    )
    assert out.verdict == cc.UNKNOWN


@pytest.mark.asyncio
async def test_coverage_claim_needs_exactly_one_subject_in_the_handler_after_the_gate(
    plane,
) -> None:
    """The "exactly one of symbol, path, setting" rule lives in the handler,
    after the namespace gate, so a refused principal never
    learns the op's argument shape from a model-level 422; the model itself
    accepts the argument-less call."""
    from server.core.errors import ValidationError

    assert CodeCoverageClaimInput(org=ORG, namespace=NS).symbol == ""
    with pytest.raises(ValidationError, match="exactly one of symbol, path, setting"):
        await h.handle_code_coverage_claim(CodeCoverageClaimInput(org=ORG, namespace=NS))
    with pytest.raises(ValidationError):
        await h.handle_code_coverage_claim(
            CodeCoverageClaimInput(org=ORG, namespace=NS, symbol="a", path="b")
        )


class _FacetRepo(_Repo):
    """A repo whose namespace holds facet rows for `dialect`."""

    def __init__(self, *, present: bool = True) -> None:
        super().__init__()
        self.present = present
        self.facet_reads: list[list[tuple[str, str]]] = []

    async def facets_present(self, org, ns):
        return self.present

    async def tests_for_facets(self, org, ns, facets, *, limit=50):
        self.facet_reads.append(list(facets))
        if facets == [("setting", "dialect")]:
            return (
                [
                    {
                        "path": "tests/queries/0_stateless/04057_dialect.sql",
                        "ordinals": [3, 9],
                        "lines": [4, 12],
                        "confidence": 1.0,
                        "same_statement": True,
                    }
                ],
                1,
            )
        return [], 0


@pytest.mark.asyncio
async def test_test_home_reads_facets_before_grepping(monkeypatch) -> None:
    h.reset_control_cache()  # The control is kept per namespace
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    repo = _FacetRepo()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    out = await h.handle_code_test_home(
        CodeTestHomeInput(org=ORG, namespace=NS, settings=["dialect"], file_stems=["dialect_x"])
    )
    assert repo.facet_reads == [[("setting", "dialect")]]
    grepped = {p for p, _ in repo.greps}
    assert "dialect" not in grepped, "the setting part came from the facets"
    assert "arrayJoin" in grepped, "the positive control still greps"
    assert any(c["path"].endswith("04057_dialect.sql") for c in out.candidates)


@pytest.mark.asyncio
async def test_test_home_greps_every_part_without_facet_rows(monkeypatch) -> None:
    h.reset_control_cache()  # The control is kept per namespace
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    repo = _FacetRepo(present=False)
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    await h.handle_code_test_home(CodeTestHomeInput(org=ORG, namespace=NS, settings=["dialect"]))
    assert repo.facet_reads == []
    assert "dialect" in {p for p, _ in repo.greps}
