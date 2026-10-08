"""An empty impact says why (with the paths behind it), and
a fallback says why.

`code.impact` puts `paths_seen` ([{path, class}]) on every diff-shaped
response so `no_indexed_paths` is actionable (all `test` = a test-only
change; exact by construction); a request with no shape is a `no_match`
verdict, not a 404; the trace `query` holds the touched paths for a
diff-only call. `code.outcome` takes a closed `reason` on fell_back_grep /
ignored, stored in `source` the way a shunt_pass stores its pass reason
: never a 400, `unattributed` for older clients.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app.symbol import impact as _code_impact
from server.code.domain.index import verdicts as v
from server.settings import settings
from tests.unit.test_code_structural_verdicts import (  # noqa: F401 — the fixture pins org/ns
    _diff,
    _impact,
    _intel_on,
    _Repo,
)

_PREFIXES = ("src/", "base/", "programs/", "tests/", "ci/")
# the handler's real helper, captured before the autouse fixture below replaces it
_REAL_INDEXED_PREFIXES = _code_impact._indexed_prefixes


# ---- the pure classifier -------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("tests/queries/0_stateless/05182_x.sql", "test"),
        ("tests/queries/0_stateless/05182_x.reference", "test"),
        ("tests/performance/join.xml", "test"),
        ("tests/integration/test_prometheus/test_evaluation.py", "test"),
        ("src/Client/tests/gtest_terminal_markdown_renderer.cpp", "test"),
        ("docs/en/sql-reference/x.md", "unindexed"),  # outside the prefixes
        ("src/README.md", "doc"),
        ("src/Storages/StorageBuffer.h", "src"),
        ("src/Planner/PlannerJoinTree.cpp", "src"),
        ("ci/jobs/integration_test_job.py", "src"),
        ("programs/server/docs.html", "config"),
        ("src/CMakeLists.txt", "config"),
        ("utils/exclude-authors.txt", "unindexed"),
        (".claude/skills/review/SKILL.md", "unindexed"),
        ("./src/A.cpp", "src"),
        ("", "config"),
    ],
)
def test_classify_path_under_the_indexed_prefixes(path: str, expected: str) -> None:
    assert v.classify_path(path, prefixes=_PREFIXES) == expected


def test_classify_path_without_prefixes_and_with_the_unindexed_set() -> None:
    # no prefixes: the whole tree is indexed, so a doc is a doc
    assert v.classify_path("docs/en/x.md") == "doc"
    assert v.classify_path("utils/x.txt") == "config"
    # a graphed source path the index holds no row for is unindexed
    assert (
        v.classify_path("src/New.cpp", prefixes=_PREFIXES, unindexed={"src/New.cpp"}) == "unindexed"
    )
    # a test path stays a test even when it is outside the prefixes / unindexed
    assert (
        v.classify_path("spec/x_test.py", prefixes=("src/",), unindexed={"spec/x_test.py"})
        == "test"
    )
    assert set(v.PATH_CLASSES) == {"test", "doc", "config", "unindexed", "src"}


def test_paths_seen_keeps_request_order_dedups_and_caps() -> None:
    seen = v.paths_seen(["src/A.cpp", "tests/q/1.sql", "src/A.cpp", ""], prefixes=_PREFIXES)
    assert seen == [
        {"path": "src/A.cpp", "class": "src"},
        {"path": "tests/q/1.sql", "class": "test"},
    ]
    assert len(v.paths_seen([f"src/{i}.cpp" for i in range(300)], limit=200)) == 200


def test_verdict_is_exact_only_for_no_indexed_paths_without_a_src_path() -> None:
    tests_only = [{"path": "tests/q/1.sql", "class": "test"}, {"path": "src/x.md", "class": "doc"}]
    assert v.verdict_is_exact("no_indexed_paths", tests_only) is True
    assert v.verdict_is_exact("no_indexed_paths", []) is True
    assert v.verdict_is_exact("no_indexed_paths", [{"path": "src/C.h", "class": "src"}]) is False
    for verdict in ("", "no_seeds_in_hunks", "no_static_callers", "not_covered", "no_match"):
        assert v.verdict_is_exact(verdict, tests_only) is False


# ---- code.impact through the handler, one test per input shape --------------------


@pytest.fixture(autouse=True)
def _prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.symbol import impact as h

    monkeypatch.setattr(h, "_indexed_prefixes", lambda org, ns: _PREFIXES)


def test_files_only_test_paths_is_an_exact_no_indexed_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = [
        "tests/queries/0_stateless/05182_x.sql",
        "tests/queries/0_stateless/05182_x.reference",
        "tests/performance/join.xml",
    ]
    out, (c,) = _impact(monkeypatch, files=files)
    assert out.verdict == "no_indexed_paths" and out.epistemic == "exact" and out.causes == []
    assert out.paths_seen == [{"path": p, "class": "test"} for p in files]
    assert out.impacted_symbols == [] and out.touched_symbols == []
    assert c["abstain"] is False and c["verdict"] == "no_indexed_paths"
    assert c["route"] == "impact_files" and c["query"] == ",".join(files)


def test_diff_with_only_tests_is_an_exact_no_indexed_paths_and_the_trace_holds_the_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff = _diff("tests/queries/0_stateless/1.sql", 1) + _diff(
        "tests/queries/0_stateless/1.reference", 1
    )
    out, (c,) = _impact(monkeypatch, diff=diff)
    assert out.verdict == "no_indexed_paths" and out.epistemic == "exact" and out.causes == []
    assert [e["class"] for e in out.paths_seen] == ["test", "test"]
    assert c["abstain"] is False and c["route"] == "impact_diff"
    # a diff-only call traces its paths, so it is replayable by path
    assert c["query"] == "tests/queries/0_stateless/1.sql,tests/queries/0_stateless/1.reference"


def test_diff_touching_src_classifies_and_keeps_the_walks_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a hunk inside DB::A::g (nobody calls it) plus a test file
    diff = _diff("src/A.cpp", 32, "void g()") + _diff("tests/queries/0_stateless/1.sql", 1)
    out, (c,) = _impact(monkeypatch, diff=diff)
    assert out.verdict == "no_static_callers" and c["abstain"] is False
    assert out.paths_seen == [
        {"path": "src/A.cpp", "class": "src"},
        {"path": "tests/queries/0_stateless/1.sql", "class": "test"},
    ]
    assert [t["qualified_name"] for t in out.touched_symbols] == ["DB::A::g"]
    # rows reached: no verdict, paths still classified
    out, (c,) = _impact(monkeypatch, diff=_diff("src/A.cpp", 12, "void f()"))
    assert (
        out.verdict == ""
        and out.impacted_symbols
        and out.paths_seen
        == [
            {"path": "src/A.cpp", "class": "src"},
        ]
    )


def test_diff_touching_a_held_src_path_with_no_functions_is_not_forced_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source file the index holds but found no function in (a header of
    constants — or a parse it lost) keeps the envelope's own reading: the
    verdict is no_indexed_paths, the class says `src`, and nothing claims
    exactness on its behalf."""
    from server.code.api.models_symbol import CodeImpactInput
    from server.code.app.symbol import impact as h

    repo = _Repo()
    repo.files.add("src/Consts.h")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(h, "schedule_code_trace", lambda **k: calls.append(k))

    async def _no_declined(*a: Any, **k: Any) -> tuple[int, int]:
        return 0, 0

    monkeypatch.setattr(h, "_declined_safe", _no_declined)
    with patch.object(h, "CodeRepo", return_value=repo):
        out = asyncio.run(h.handle_code_impact(CodeImpactInput(files=["src/Consts.h"])))
    assert out.verdict == "no_indexed_paths" and calls[0]["abstain"] is False
    assert out.paths_seen == [{"path": "src/Consts.h", "class": "src"}]
    assert not v.verdict_is_exact(out.verdict, out.paths_seen)


def test_unindexed_and_unheld_paths_are_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    # outside the indexed prefixes: an answer, exact, class unindexed
    out, (c,) = _impact(monkeypatch, files=["utils/exclude-authors.txt"])
    assert out.verdict == "no_indexed_paths" and out.epistemic == "exact"
    assert out.paths_seen == [{"path": "utils/exclude-authors.txt", "class": "unindexed"}]
    # a graphed source path under the prefixes the index does not hold: abstain, class unindexed
    out, (c,) = _impact(monkeypatch, diff=_diff("src/New.cpp", 3))
    assert out.verdict == "not_covered" and c["abstain"] is True
    assert out.paths_seen == [{"path": "src/New.cpp", "class": "unindexed"}]
    assert "external_calls" in out.causes


def test_empty_request_is_a_no_match_verdict_not_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    for kw in ({}, {"diff": ""}, {"files": []}, {"files": [], "diff": ""}):
        out, (c,) = _impact(monkeypatch, **kw)
        assert out.verdict == "no_match" and c["abstain"] is True
        assert c["route"] == "impact_diff" and c["query"] == "diff"
        assert out.paths_seen == [] and out.impacted_symbols == [] and out.touched_symbols == []


def test_symbol_shape_is_unchanged_but_for_the_empty_paths_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, (c,) = _impact(monkeypatch, name="DB::A::f")
    assert out.impacted_symbols and out.verdict == "" and out.paths_seen == []
    assert c["query"] == "DB::A::f" and c["route"] == "impact_symbol"
    # the new field is additive: every field the deployment served is still there
    dumped = out.model_dump(mode="json")
    for k in (
        "impacted_symbols",
        "impacted_tests",
        "co_change_files",
        "blast_radius_score",
        "touched_symbols",
        "co_change_missing",
        "owners",
        "reverts",
        "counts_floor",
        "unresolved_calls",
        "ambiguous_calls",
        "epistemic",
        "causes",
        "by_hop_counts",
        "truncated",
        "verdict",
        "trace_run_id",
    ):
        assert k in dumped


def test_indexed_prefixes_come_from_the_repo_entry_and_never_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.domain.index import repos

    monkeypatch.setattr(settings, "code_repos_json", "")
    monkeypatch.setattr(settings, "code_repo", "acme/widgets")  # a scalar entry, org o / ns n
    monkeypatch.setattr(settings, "code_path_prefixes", "src/,base/")
    assert _REAL_INDEXED_PREFIXES("o", "n") == ("src/", "base/")  # the scalar entry, org o / ns n
    assert _REAL_INDEXED_PREFIXES("o", "missing") == ()

    def _boom(org: str, ns: str) -> Any:
        raise RuntimeError("config")

    monkeypatch.setattr(repos, "repo_for_namespace", _boom)
    assert _REAL_INDEXED_PREFIXES("o", "n") == ()


def test_capabilities_catalogue_lists_paths_seen_and_reason() -> None:
    from server.operations.capabilities import capability_snapshot

    ops = capability_snapshot()["ops"]
    assert "paths_seen" in ops["code.impact"]["response_fields"]
    assert "reason" in ops["code.outcome"]["request_fields"]


# ---- code.outcome: a fallback says why --------------------------------------------


def test_outcome_reason_vocabulary_is_closed_and_shared_with_the_plugin() -> None:
    from server.code.api.models_outcome import (
        OUTCOME_REASON_ACTIONS,
        OUTCOME_REASONS,
        CodeOutcomeInput,
    )

    assert OUTCOME_REASONS == {
        "abstained",
        "empty",
        "too_slow",
        "wrong_shape",
        "not_indexed",
        "stale",
        "preference",
        "unattributed",
    }
    assert OUTCOME_REASON_ACTIONS == {"fell_back_grep", "ignored"}
    # the plugin's capture hook only ever sends values from this set
    hook = (
        Path(__file__).resolve().parents[2]
        / "integrations"
        / "claude-code-plugin"
        / "hooks"
        / "loom-capture.mjs"
    ).read_text()
    body = hook.split("export function fallbackReason(row) {", 1)[1].split("\n}\n", 1)[0]
    sent = set(re.findall(r'return "([a-z_]+)"', body))
    assert sent and sent <= OUTCOME_REASONS
    # the SDK reference client sends the field under the same name
    sdk = (
        Path(__file__).resolve().parents[2] / "sdk" / "python" / "loom" / "reference_client.py"
    ).read_text()
    assert 'body["reason"] = reason' in sdk
    for reason in sorted(OUTCOME_REASONS):
        row = CodeOutcomeInput(
            action="fell_back_grep", trace_run_id="t", reason=reason, source="clickgap"
        )
        assert row.outcome_reason == reason and row.stored_source == reason


def test_outcome_reason_never_rejects_and_a_missing_reason_reads_as_unattributed() -> None:
    from server.code.api.models_outcome import CodeOutcomeInput

    # no reason: the client's own source is kept on the row, reads as unattributed
    bare = CodeOutcomeInput(action="fell_back_grep", trace_run_id="t", source="review-bot")
    assert bare.outcome_reason == "unattributed" and bare.stored_source == "review-bot"
    # unknown reason: recorded, as unattributed
    odd = CodeOutcomeInput(
        action="ignored", trace_run_id="t", source="review-bot", reason="because"
    )
    assert odd.outcome_reason == "unattributed" and odd.stored_source == "unattributed"
    # the reason travels in `reason` only; a reason word in `source` is just a source
    in_source = CodeOutcomeInput(action="ignored", trace_run_id="t", source="too_slow")
    assert in_source.outcome_reason == "unattributed" and in_source.stored_source == "too_slow"
    # case / whitespace / non-string never 400
    assert (
        CodeOutcomeInput(action="ignored", trace_run_id="t", reason=" Stale ").outcome_reason
        == "stale"
    )
    assert (
        CodeOutcomeInput(action="ignored", trace_run_id="t", reason=42).outcome_reason
        == "unattributed"
    )  # type: ignore[arg-type]
    assert CodeOutcomeInput(action="ignored", trace_run_id="t", reason={"x": 1}).reason == ""  # type: ignore[arg-type]
    assert CodeOutcomeInput(action="ignored", trace_run_id="t", reason="x" * 500).reason == "x" * 32
    # positive and shunt actions carry no outcome reason and keep their source
    for action, extra in (
        ("opened_file", {"trace_run_id": "t"}),
        ("used_in_answer", {"trace_run_id": "t"}),
        ("shunt_pass", {}),
        ("shunt_hit", {}),
    ):
        row = CodeOutcomeInput(action=action, source="plugin_hook", reason="stale", **extra)  # type: ignore[arg-type]
        assert row.outcome_reason == "" and row.stored_source == "plugin_hook"


def test_outcome_reason_rides_source_on_the_row_and_labels_the_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_outcome import CodeOutcomeInput
    from server.code.app import outcome

    monkeypatch.setattr(settings, "code_trace_log_enabled", True)
    inserted: list[dict[str, Any]] = []
    counted: list[dict[str, Any]] = []
    attrs: list[dict[str, Any]] = []

    class _Client:
        class client:
            @staticmethod
            async def insert(table, rows, *, column_names, settings):
                inserted.append(dict(zip(column_names, rows[0], strict=True)))

    async def _no_fold(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(outcome, "_fold_outcome_signal", _no_fold)
    monkeypatch.setattr(outcome, "record_code_outcome", lambda **kw: counted.append(kw))
    monkeypatch.setattr(outcome, "record_code_shunt", lambda **kw: counted.append({"shunt": kw}))
    monkeypatch.setattr(outcome, "set_code_span_attributes", lambda **kw: attrs.append(kw))
    with patch("server.core.ch.get_client", return_value=_Client()):
        asyncio.run(
            outcome.handle_code_outcome(
                CodeOutcomeInput(
                    action="fell_back_grep",
                    trace_run_id="t1",
                    targets=[],
                    source="clickgap",
                    reason="abstained",
                )
            )
        )
        asyncio.run(
            outcome.handle_code_outcome(
                CodeOutcomeInput(
                    action="fell_back_grep",
                    trace_run_id="t2",
                    targets=[],
                    source="clickgap",
                )
            )
        )
        asyncio.run(
            outcome.handle_code_outcome(
                CodeOutcomeInput(
                    action="opened_file",
                    trace_run_id="t3",
                    targets=["src/a.cpp"],
                    source="plugin_hook",
                )
            )
        )
    assert [r["source"] for r in inserted] == ["abstained", "clickgap", "plugin_hook"]
    assert [r["action"] for r in inserted] == ["fell_back_grep", "fell_back_grep", "opened_file"]
    assert counted == [
        {"action": "fell_back_grep", "reason": "abstained", "namespace": "n"},
        {"action": "fell_back_grep", "reason": "unattributed", "namespace": "n"},
        {"action": "opened_file", "reason": "", "namespace": "n"},
    ]
    assert [a["source"] for a in attrs] == ["abstained", "unattributed", "plugin_hook"]


def test_outcome_counter_labels_are_bounded_and_never_raise() -> None:
    from server.code.app import common as cc

    cc.reset_shunt_counters_for_tests()
    cc.record_code_outcome(action="ignored", reason="stale", namespace="n")
    cc.record_code_outcome(action="x" * 100, reason="y" * 100, namespace="")
    cc.reset_shunt_counters_for_tests()
