"""Generation content, prompt-version linkage, and the independent judge.

Every fact pinned here about the Langfuse OTLP contract was established by probing
the live API, and two of them silently DELETE the observation when violated —
which is the worst possible failure mode for telemetry, because the absence looks
like "nothing happened" rather than an error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.observability import genai, retrieval_judge

ROOT = Path(__file__).resolve().parents[2]


class _FakeSpan:
    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, k: str, v: Any) -> None:
        self.attrs[k] = v


def _cfg(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> None:
    # "all" so the content assertions below exercise the recording path itself.
    # Tiering is covered separately in test_capture_consent.py.
    defaults = {
        "langfuse_capture_io_level": "all",
        "langfuse_generation_io_max_chars": 2000,
    }
    defaults.update(kw)
    for k, v in defaults.items():
        monkeypatch.setattr(genai.settings, k, v, raising=False)


# --- Gap 1: content ---


def test_prompt_and_completion_land_as_structured_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`langfuse.observation.input` preserves a JSON object.

    Probed: it comes back as `{"system": ..., "user": ...}` rather than a
    concatenated string, so the system prompt and the query stay separable in the
    UI. `gen_ai.prompt`/`completion` and `input.value`/`output.value` also work but
    flatten to one string, which is why this convention was chosen.
    """
    _cfg(monkeypatch)
    span = _FakeSpan()
    genai._Generation(span, "gpt-4o-mini").record_content(
        system="SYSTEM TEXT", user="USER TEXT", output="MODEL REPLY"
    )
    parsed = json.loads(span.attrs["langfuse.observation.input"])
    assert parsed == {"system": "SYSTEM TEXT", "user": "USER TEXT"}
    assert span.attrs["langfuse.observation.output"] == "MODEL REPLY"


def test_content_is_truncated_with_an_explicit_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silently clipped prompt reads as the whole prompt.

    The missing tail is usually exactly where a malformed instruction is, so the
    bound has to announce itself.
    """
    _cfg(monkeypatch, langfuse_generation_io_max_chars=50)
    span = _FakeSpan()
    genai._Generation(span, "m").record_content(system="s" * 500, user="u" * 500, output="o" * 500)
    parsed = json.loads(span.attrs["langfuse.observation.input"])
    assert "truncated 450 chars" in parsed["system"]
    assert len(parsed["system"]) < 120
    assert "truncated 450 chars" in span.attrs["langfuse.observation.output"]


def test_content_capture_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that must not send user text keeps cost and tokens.

    Turning content off must NOT also disable the prompt-version link or the sha —
    those carry no user data and are what make a regression attributable.
    """
    _cfg(monkeypatch, langfuse_capture_io_level="off")
    span = _FakeSpan()
    genai._Generation(span, "m").record_content(
        system="secret system", user="what did alice earn", output="reply"
    )
    assert "langfuse.observation.input" not in span.attrs
    assert "langfuse.observation.output" not in span.attrs
    assert span.attrs["loom.prompt.sha"], "the prompt hash carries no user content"


def test_zero_max_chars_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _cfg(monkeypatch, langfuse_generation_io_max_chars=0)
    span = _FakeSpan()
    genai._Generation(span, "m").record_content(system="s", user="u", output="o")
    assert "langfuse.observation.input" not in span.attrs


def test_capture_defaults_to_off_and_payloads_stay_off() -> None:
    """No text capture until someone says yes.

    This defaulted to "query", on the reasoning that a search phrase is the
    cheapest useful thing to record and never exposes held data. True as far as
    it goes, but a search phrase is still something a person typed, and the
    default applied to every namespace that had never been asked — including
    other people's. Consent is given, not withdrawn: an operator who wants the
    diagnostic value sets "query" deliberately, and an individual can tick the
    box on their own namespace.

    The three levels still exist and still tier by payload; only the starting
    point moved.
    """
    from server.settings import Settings

    assert Settings.model_fields["langfuse_capture_io_level"].default == "off"


# --- Gap 3: prompt versioning ---


def test_prompt_version_is_an_int_and_never_sent_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ways to silently delete the observation, both probed against the API.

    `langfuse.prompt.version` as a STRING → Langfuse drops the whole observation.
    `langfuse.prompt.name` with no version → also dropped. So they are set
    together, and the version is an int, or neither is set at all.
    """
    _cfg(monkeypatch)
    lock = {"deadbeefcafe": {"name": "loom-test", "version": 4}}
    monkeypatch.setattr(genai, "_prompt_lockfile", lambda: lock)
    monkeypatch.setattr(genai, "_prompt_identity", lambda system: ("deadbeefcafe", "loom-test", 4))
    span = _FakeSpan()
    genai._Generation(span, "m").record_content(system="anything", user="u", output="o")
    assert span.attrs["langfuse.prompt.name"] == "loom-test"
    version = span.attrs["langfuse.prompt.version"]
    assert isinstance(version, int) and not isinstance(version, bool), (
        "a string version makes Langfuse drop the entire observation"
    )
    assert version == 4
    # The tag prompts.evaluate finds the version's traces by.
    assert "prompt:loom-test:4" in span.attrs["langfuse.trace.tags"]


def test_unregistered_prompt_gets_no_link_but_keeps_the_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edited-but-unpushed prompt must not be labelled with a stale version.

    A generation tagged with the wrong prompt version sends you to the wrong diff,
    which is worse than no tag. The sha still lands, so prompt-version grouping
    keeps working in ClickStack for prompts Langfuse has never seen.
    """
    _cfg(monkeypatch)
    monkeypatch.setattr(genai, "_prompt_lockfile", lambda: {})
    span = _FakeSpan()
    genai._Generation(span, "m").record_content(
        system="a prompt nobody pushed", user="u", output="o"
    )
    assert "langfuse.prompt.name" not in span.attrs
    assert "langfuse.prompt.version" not in span.attrs
    assert len(span.attrs["loom.prompt.sha"]) == 12


def test_prompt_identity_is_derived_from_the_text_actually_sent() -> None:
    """Identity comes from content, not from a stage→constant table.

    A hand-maintained table drifts the moment someone edits a prompt; a content
    hash cannot, and it also covers prompts assembled inline at call time.
    """
    a, _, _ = genai._prompt_identity("one prompt")
    b, _, _ = genai._prompt_identity("one prompt")
    c, _, _ = genai._prompt_identity("one prompt ")
    assert a == b, "the same text must hash identically"
    assert a != c, "a one-character edit must produce a new version"


def test_lockfile_covers_the_push_registry() -> None:
    """Every registered prompt must be in the lockfile, at its current content.

    This is the gate that keeps the link honest: edit a prompt without re-running
    `scripts/codegen/push_langfuse_prompts.py --write` and its generations silently lose
    their Langfuse prompt link. Failing here is the reminder.
    """
    lock_path = ROOT / "server" / "observability" / "langfuse_prompts.lock.json"
    assert lock_path.exists(), "run scripts/codegen/push_langfuse_prompts.py --write"
    lock = json.loads(lock_path.read_text())["prompts"]

    from scripts.codegen.push_langfuse_prompts import registry, sha_of

    missing = []
    for name, text in registry().items():
        sha = sha_of(text)
        entry = lock.get(sha)
        if entry is None or entry.get("name") != name:
            missing.append(f"{name} (sha {sha})")
    assert not missing, (
        "these prompts changed without being re-pushed, so their generations "
        f"will carry no prompt-version link: {missing}. Run "
        "`scripts/codegen/push_langfuse_prompts.py --write`."
    )


def test_lockfile_versions_are_all_ints() -> None:
    """A string version in the lockfile would delete every affected observation."""
    lock = json.loads(
        (ROOT / "server" / "observability" / "langfuse_prompts.lock.json").read_text()
    )["prompts"]
    bad = [
        sha
        for sha, e in lock.items()
        if not isinstance(e.get("version"), int) or isinstance(e.get("version"), bool)
    ]
    assert not bad, f"non-int prompt versions would drop observations: {bad}"


# --- Gap 2: the independent judge ---


def _judge_cfg(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> None:
    defaults = {
        "retrieval_judge_sample_rate": 1.0,
        "langfuse_url": "https://us.cloud.langfuse.com",
        "langfuse_admin_token": "pk-lf-a:sk-lf-b",
    }
    defaults.update(kw)
    for k, v in defaults.items():
        monkeypatch.setattr(retrieval_judge.settings, k, v, raising=False)


def test_judge_is_off_by_default() -> None:
    """It spends real money per judged search, so it must be switched on."""
    from server.settings import Settings

    assert Settings.model_fields["retrieval_judge_sample_rate"].default == 0.0


def test_judge_does_not_run_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _judge_cfg(monkeypatch, retrieval_judge_sample_rate=0.0)
    assert retrieval_judge.enabled() is False
    assert (
        retrieval_judge.submit(
            trace_id="a" * 32, query="q", hits=[{"value": "v"}], org="o", namespace="n"
        )
        is False
    )


def test_judge_sampling_uses_a_different_bit_range_than_span_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judged searches must not be a correlated subset of exported ones.

    Both gates keyed on the low 64 bits would make the judged set a strict subset
    of the exported set, biasing the sample toward exactly the traces already kept.
    The judge measures the whole population, so it takes an independent slice.
    """
    from server.observability import otel_langfuse

    monkeypatch.setattr(otel_langfuse.settings, "langfuse_sample_rate", 0.5, raising=False)
    monkeypatch.setattr(otel_langfuse.settings, "langfuse_span_denylist", "", raising=False)
    _judge_cfg(monkeypatch, retrieval_judge_sample_rate=0.5)
    span_filter = otel_langfuse._LangfuseSpanFilter()

    class _Ctx:
        def __init__(self, t: int) -> None:
            self.trace_id = t

    class _Span:
        name = "memory.search"
        parent = "p"

        def __init__(self, t: int) -> None:
            self._c = _Ctx(t)

        def get_span_context(self) -> Any:
            return self._c

    disagreements = 0
    for i in range(400):
        tid = (i * 0x9E3779B97F4A7C15) & ((1 << 128) - 1)
        if span_filter.keep(_Span(tid)) != retrieval_judge._sampled(f"{tid:032x}"):
            disagreements += 1
    assert disagreements > 50, (
        "the two sampling gates agree too often — they are probably keyed on the "
        "same bits, which correlates the judged sample with the exported one"
    )


def test_judge_parses_a_well_formed_verdict() -> None:
    got = retrieval_judge._parse(
        'noise {"on_target": true, "useful": [1, 3], "reason": "needs inference"} tail',
        5,
    )
    # answered, 2 of 5 excerpts load-bearing -> 0.80 + 0.20 * 0.4
    assert got == (0.88, "answers", "needs inference")


def test_judge_derives_a_real_distribution() -> None:
    """A score derived from `useful / total` spreads. An anchored 0.0-1.0 prompt
    collapses to two values, so the metric has no variance and the
    quality-collapse alert can never fire."""
    seen = set()
    for answered in (True, False):
        for useful in range(0, 6):
            seen.add(retrieval_judge._relevance_from(answered, useful, 5))
    assert len(seen) >= 10, f"only {len(seen)} distinct values: {sorted(seen)}"
    # A perfectly answered single-hit query must not be dragged down by top_k:
    # this is the bias the first 14 live judgements exposed (it scored 0.68).
    assert retrieval_judge._relevance_from(True, 1, 5) >= 0.80

    # The two bands must not overlap, or a threshold cannot mean "not answerable".
    answered_band = {retrieval_judge._relevance_from(True, u, 5) for u in range(6)}
    partial_band = {retrieval_judge._relevance_from(False, u, 5) for u in range(1, 6)}
    assert min(answered_band) > max(partial_band)


def test_judge_rejects_unusable_output() -> None:
    """No score is better than a fabricated one. `on_target` must be a real
    bool: defaulting it is how the judgements collapsed silently."""
    assert retrieval_judge._parse("not json at all", 5) is None
    assert retrieval_judge._parse('{"useful": [1]}', 5) is None
    assert retrieval_judge._parse('{"on_target": "yes", "useful": []}', 5) is None
    assert retrieval_judge._parse('{"on_target": true}', 5) is None
    assert retrieval_judge._parse('{"on_target": true, "useful": "1,2"}', 5) is None
    # The prompt asks for `on_target`; a reply naming another field is not a verdict.
    assert retrieval_judge._parse('{"answered": true, "useful": [1]}', 5) is None


def test_judge_ignores_invented_and_repeated_excerpt_numbers() -> None:
    """A model that repeats an index or cites excerpt 9 of 5 must not inflate
    precision."""
    rel, verdict, _ = retrieval_judge._parse(
        '{"on_target": false, "useful": [2, 2, 2, 9, 0, -1]}', 5
    ) or (None, None, None)
    # One distinct in-range index of 5 -> 0.10 + 0.40 * 0.2
    assert rel == 0.18
    assert verdict == "partial"


def test_judge_reports_zero_when_nothing_was_useful() -> None:
    rel, verdict, _ = retrieval_judge._parse('{"on_target": false, "useful": []}', 5) or (
        None,
        None,
        None,
    )
    assert rel == 0.0
    assert verdict == "off_topic"


def test_judge_excerpts_are_bounded() -> None:
    """Paying to judge 50 excerpts would cost more than the search did."""
    hits = [{"value": "x" * 5000} for _ in range(50)]
    ex = retrieval_judge._excerpts(hits, 5)
    assert len(ex) == 5
    assert all(len(e) <= 600 for e in ex)


def test_judge_is_independent_of_loom_confidence() -> None:
    """Structural guard: the judge must not read loom's own quality signals.

    If it consumed `retrieval_confidence` or the similarity scores, it would
    inherit the circularity it exists to break.
    """
    src = (ROOT / "server" / "observability" / "retrieval_judge.py").read_text()
    for forbidden in (
        "retrieval_confidence",
        "top_similarity",
        "distance",
        "abstain",
    ):
        assert forbidden not in src.split('"""', 2)[2], (
            f"the judge reads {forbidden!r}, which reintroduces the circularity it exists to break"
        )


def test_lockfile_ships_inside_the_python_package() -> None:
    """It must live under `server/`, not `deploy/`.

    The Dockerfile copies `server` and `sdk` only. A lockfile in `deploy/` is
    absent from the image, so every prompt link silently disappears in the
    deployed build (`promptName=None` on every generation) while working on a
    laptop. This pins the location.
    """
    assert (ROOT / "server" / "observability" / "langfuse_prompts.lock.json").exists()
    assert not (ROOT / "deploy" / "langfuse_prompts.lock.json").exists(), (
        "a stale copy in deploy/ will be the one someone edits"
    )


def test_judge_model_is_pinned_away_from_the_cascade() -> None:
    """A judge drawn from the model it grades measures agreement, not quality.

    Empty would fall through to `llm_model` — gpt-4o-mini in every current
    deployment, which is also what does the reranking and the iterative judging. It
    would inherit the same blind spots, so the scores read reassuring for exactly
    the failures they exist to catch. Independence from retrieval state (embedder,
    thresholds, calibration) is necessary but not sufficient.

    Pinned to the offline benches' judge so a live `retrieval_relevance` and a
    bench judge score are the same instrument, per bench/README.md's rule that a
    cross-instrument comparison is not a comparison.
    """
    from server.settings import Settings

    pinned = Settings.model_fields["retrieval_judge_model"].default
    assert pinned, "an empty judge model falls back to the cascade's own model"
    assert pinned != Settings.model_fields["llm_model"].default

    # And it must be priced, or every judge generation reports cost_unknown.
    from server.observability.cost_accumulator import is_priced

    assert is_priced(pinned), (
        f"{pinned} is not in PRICE_PER_1K_TOKENS, so the judge's own spend would "
        f"be invisible — the thing this whole effort exists to prevent"
    )
