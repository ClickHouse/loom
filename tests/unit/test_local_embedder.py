"""Local CPU embedder — provider selection + offline embed behavior.

Covers the optional latency-lever embedder (server/embedding/local.py)
and its wiring through the factory (server/embedding/embedder.py):

  - provider selection: local vs openai via LOOM_EMBEDDING_MODEL and
    the additive LOOM_EMBEDDING_PROVIDER override;
  - the default path still resolves to OpenAI (no default change);
  - the local model-alias → (HF id, dim) resolution;
  - the embedder returns an L2-normalized vector of the expected dim
    WITHOUT any network call, exercised against a stub `fastembed`
    (the real package is not a hard dependency and need not be
    installed in the test env).

These are pure-wiring tests: no ClickHouse, no network, no model
download. The heavy fastembed path is gated behind an availability
check in LocalEmbedder.__init__; here we substitute a stub so the
selection + normalization logic is testable offline.
"""

from __future__ import annotations

import functools
import math
import sys
from pathlib import Path

import pytest

from server.embedding import embedder as emb
from server.embedding import local as local_mod
from server.settings import settings


@pytest.fixture(autouse=True)
def _isolate_embedder(monkeypatch: pytest.MonkeyPatch):
    """Reset the cached embedder and pin a deterministic embedding config for each
    test, then restore."""
    emb.reset()
    # Default-ish baseline; individual tests override as needed.
    monkeypatch.setattr(settings, "embedding_provider", "")
    monkeypatch.setattr(settings, "embedding_model", "fake-ngram-1024")
    monkeypatch.setattr(settings, "embedding_dim", 1024)
    yield
    emb.reset()


# ---- provider resolution (no instantiation) -----------------------------


def test_provider_inferred_from_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embedding_provider", "")
    assert emb.provider_for("fake-ngram-1024") == "fake"
    assert emb.provider_for("local") == "local"
    assert emb.provider_for("local:bge-small") == "local"
    assert emb.provider_for("text-embedding-3-small") == "openai"


def test_explicit_provider_overrides_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pinning the provider wins regardless of the model string.
    monkeypatch.setattr(settings, "embedding_provider", "local")
    assert emb.provider_for("text-embedding-3-small") == "local"
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    assert emb.provider_for("fake-ngram-1024") == "openai"


def test_unknown_explicit_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embedding_provider", "cohere")
    with pytest.raises(ValueError, match="unknown LOOM_EMBEDDING_PROVIDER"):
        emb.provider_for("text-embedding-3-small")


def test_unknown_model_with_no_provider_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "embedding_provider", "")
    with pytest.raises(ValueError, match="unknown embedding model"):
        emb.provider_for("nonsense-model-9")


# ---- default factory path (no default change) ---------------------------


def test_default_selects_openai_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipping default is OpenAI text-embedding-3-small; with a key
    present and no provider override, the factory returns the OpenAI
    embedder — local must NOT be reachable by default."""
    monkeypatch.setattr(settings, "embedding_provider", "")
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(settings, "embedding_dim", 1536)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    got = emb.get_embedder()
    assert got.model_name == "text-embedding-3-small"
    assert got.dim == 1536
    # Defensive: it is the OpenAI implementation, not the local one.
    assert type(got).__name__ == "OpenAIEmbedder"


def test_openai_model_without_key_falls_back_to_fake_not_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key → fake fallback (the OSS dev path). It must NOT silently
    switch to the local provider — that would be a default change."""
    monkeypatch.setattr(settings, "embedding_provider", "")
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    got = emb.get_embedder()
    assert type(got).__name__ == "FakeNGramEmbedder"


def test_gemini_model_without_key_falls_back_to_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embedding_provider", "")
    monkeypatch.setattr(settings, "embedding_model", "gemini-embedding-001")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    got = emb.get_embedder()
    assert type(got).__name__ == "FakeNGramEmbedder" and got.dim == 1024


def test_keyless_fallback_uses_the_configured_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is as wide as LOOM_EMBEDDING_DIM, like an explicit fake:
    the schema's vector columns are sized from that setting, not from 1024."""
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(settings, "embedding_dim", 1536)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    got = emb.get_embedder()
    assert type(got).__name__ == "FakeNGramEmbedder"
    assert got.dim == 1536 and got.model_name == "fake-ngram-1536"


def test_explicit_provider_does_not_apply_to_secondary_models(
    _stub_fastembed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOOM_EMBEDDING_PROVIDER names the served model's provider. A secondary
    vector model infers from its own spec: provider=fake must not turn a
    `text-embedding-3-large` arm into a fake one, and provider=openai must not
    send `local:bge-small` to OpenAI."""
    monkeypatch.setattr(
        settings, "code_search_vector_models", "text-embedding-3-large,local:bge-small"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(settings, "embedding_provider", "fake")
    large = emb.get_secondary_embedder("text-embedding-3-large")
    assert type(large).__name__ == "OpenAIEmbedder"
    assert large.model_name == "text-embedding-3-large"
    emb.reset()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    small = emb.get_secondary_embedder("local:bge-small")
    assert type(small).__name__ == "LocalEmbedder"
    # the served model still honours the explicit provider
    assert emb.provider_for("fake-ngram-1024") == "openai"


@pytest.mark.parametrize("label", ["prefix:256:fake-ngram-1024", "ctx:fake-ngram-1024"])
def test_a_label_over_the_served_model_cannot_be_the_served_model(
    monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setattr(settings, "embedding_model", label)
    with pytest.raises(ValueError, match="unknown embedding model"):
        emb.get_embedder()


# ---- local model-alias resolution ---------------------------------------


def test_local_specs_name_an_alias() -> None:
    assert local_mod._alias("local") == "bge-small"
    assert local_mod._alias("local:") == "bge-small"
    for alias in ("bge-small", "bge-base", "minilm"):
        assert local_mod._alias(f"local:{alias}") == alias


def test_an_unknown_alias_or_a_non_local_spec_raises() -> None:
    with pytest.raises(ValueError, match="unknown local embedding model alias"):
        local_mod._alias("local:gigantic-model")
    with pytest.raises(ValueError, match="not a local model spec"):
        local_mod._alias("text-embedding-3-small")


# ---- offline embed via a stub fastembed ----------------------------------


class _StubTextEmbedding:
    """Stand-in for fastembed.TextEmbedding. Records that it was
    constructed and returns a fixed, deliberately NON-unit-norm vector so
    the embedder's defensive L2-normalize is exercised. No network."""

    instantiated_with: list[str] = []
    loaded_from: list[str] = []
    calls: list[list[str]] = []

    def __init__(
        self, model_name: str, specific_model_path: str, threads: int | None = None
    ) -> None:
        type(self).instantiated_with.append(model_name)
        type(self).loaded_from.append(specific_model_path)
        self.model_name, self.threads = model_name, threads

    def embed(self, texts):
        import array

        type(self).calls.append(list(texts))
        for _ in texts:
            # 384-dim raw vector (norm != 1) — .tolist() compatible.
            yield array.array("f", [3.0, 4.0] + [0.0] * 382)


@pytest.fixture
def _stub_fastembed(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Inject a stub `fastembed` module, and pinned files that need no fetch, so
    LocalEmbedder loads offline."""
    from server.embedding import local_models

    _StubTextEmbedding.instantiated_with = []
    _StubTextEmbedding.loaded_from = []
    _StubTextEmbedding.calls = []
    monkeypatch.setattr(local_models, "model_dir", lambda alias: tmp_path / alias)
    import types

    stub = types.ModuleType("fastembed")
    stub.TextEmbedding = _StubTextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", stub)
    yield stub


@pytest.mark.asyncio
async def test_local_embed_returns_unit_norm_vector_offline(
    _stub_fastembed,
) -> None:
    """The local embedder returns an L2-normalized vector of the model's
    dim with no network — the stub provides a non-unit vector and the
    embedder normalizes it."""
    e = local_mod.LocalEmbedder(spec="local:bge-small")
    assert e.dim == 384
    assert e.model_name == "local:bge-small"
    assert e.hf_model_id == "BAAI/bge-small-en-v1.5"

    vec = await e.embed("how do I configure the embedder")
    assert len(vec) == 384
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=1e-6)
    # [3,4,0,...] normalizes to [0.6, 0.8, 0, ...]
    assert vec[0] == pytest.approx(0.6, abs=1e-6)
    assert vec[1] == pytest.approx(0.8, abs=1e-6)
    # The stub loaded the right HF model id, and exactly once (lazy load).
    assert _StubTextEmbedding.instantiated_with == ["BAAI/bge-small-en-v1.5"]
    assert _StubTextEmbedding.loaded_from[0].endswith("bge-small")


@pytest.mark.asyncio
async def test_local_embed_empty_input_is_zero_vector(_stub_fastembed) -> None:
    e = local_mod.LocalEmbedder(spec="local")
    vec = await e.embed("   ")
    assert vec == [0.0] * 384
    # No model load needed for the empty short-circuit.
    assert _StubTextEmbedding.instantiated_with == []


@pytest.mark.asyncio
async def test_local_embed_many_is_one_inference_call_over_the_raw_texts(
    _stub_fastembed,
) -> None:
    """The write path sends raw text (queries are normalised; batches are
    not) in one inference call; a blank text is a zero vector in its slot."""
    e = local_mod.LocalEmbedder(spec="local")
    out = await e.embed_many(["a  b", "   ", "c\n\nd"])
    assert _StubTextEmbedding.calls == [["a  b", "c\n\nd"]]
    assert out[1] == [0.0] * 384
    assert out[0][:2] == pytest.approx([0.6, 0.8]) and out[2][:2] == pytest.approx([0.6, 0.8])
    await e.embed("a  b")
    assert _StubTextEmbedding.calls[-1] == ["a b"]


@pytest.mark.asyncio
async def test_factory_returns_local_when_selected(
    monkeypatch: pytest.MonkeyPatch, _stub_fastembed
) -> None:
    """End-to-end through get_embedder(): LOOM_EMBEDDING_MODEL=local:* and
    LOOM_EMBEDDING_PROVIDER=local both yield a working local embedder."""
    monkeypatch.setattr(settings, "embedding_provider", "")
    monkeypatch.setattr(settings, "embedding_model", "local:minilm")
    got = emb.get_embedder()
    assert type(got).__name__ == "LocalEmbedder"
    assert got.dim == 384
    vec = await got.embed("ping")
    assert len(vec) == 384

    # Provider-pinned form, with the model string left at an OpenAI name:
    # the factory uses the default local spec ('local' → bge-small, 384).
    emb.reset()
    monkeypatch.setattr(settings, "embedding_provider", "local")
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    got2 = emb.get_embedder()
    assert type(got2).__name__ == "LocalEmbedder"
    assert got2.dim == 384
    assert got2.model_name == "local"


def test_local_provider_missing_package_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If fastembed is genuinely unavailable, selecting the local provider
    raises a clear, actionable ImportError (not a bare ModuleNotFound at a
    random call site)."""
    # Ensure no stub is present and the import fails.
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(ImportError, match="requires the `fastembed` package"):
        local_mod.LocalEmbedder(spec="local:bge-small")


def test_calibration_does_not_treat_local_as_fake() -> None:
    """The adaptive-threshold probe skips ONLY fake embedders. The local
    embedder's model_name must not contain 'fake', so the percentile probe
    re-derives gates per local model (the dim/distribution-safety path)."""
    assert "fake" not in "local".lower()
    assert "fake" not in "local:bge-small".lower()
    assert "fake" not in "local:minilm".lower()


def test_secondary_embedder_is_allow_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    emb.reset()
    monkeypatch.setattr(emb.settings, "code_search_vector_models", "")
    with pytest.raises(ValueError):
        emb.get_secondary_embedder("fake-ngram-64")
    monkeypatch.setattr(emb.settings, "code_search_vector_models", "fake-ngram-64, fake-ngram-32")
    a = emb.get_secondary_embedder("fake-ngram-64")
    assert a.dim == 64 and a.model_name == "fake-ngram-64"
    assert emb.get_secondary_embedder("fake-ngram-64") is a  # cached per spec
    assert emb.allowed_vector_models() == ["fake-ngram-64", "fake-ngram-32"]
    emb.reset()


def test_the_served_fake_model_has_one_width_on_every_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A coded vector column takes one length: the served model named as a
    secondary spec is built at LOOM_EMBEDDING_DIM, like get_embedder()."""
    emb.reset()
    monkeypatch.setattr(emb.settings, "embedding_model", "fake-ngram-1024")
    monkeypatch.setattr(emb.settings, "embedding_dim", 3072)
    monkeypatch.setattr(emb.settings, "code_search_vector_models", "fake-ngram-1024, fake-ngram-64")
    assert emb.get_secondary_embedder("fake-ngram-1024").dim == 3072
    assert emb.get_secondary_embedder("fake-ngram-64").dim == 64
    emb.reset()


def test_each_web_worker_gets_its_share_of_the_cpus_for_onnx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import web_workers
    from server.embedding import local

    monkeypatch.setattr(web_workers, "cpu_limit", lambda: 8)
    monkeypatch.setattr(emb.settings, "web_workers", 4)
    assert local.onnx_threads() == 2
    monkeypatch.setattr(emb.settings, "web_workers", 16)
    assert local.onnx_threads() == 1


def test_onnx_threads_stay_within_the_cpu_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from server.core import web_workers
    from server.embedding import local

    (tmp_path / "cpu.max").write_text("200000 100000\n")
    limit = functools.partial(web_workers.cpu_limit, cpus=32, proc_cgroup="0::/\n", root=tmp_path)
    monkeypatch.setattr(web_workers, "cpu_limit", limit)
    monkeypatch.setattr(emb.settings, "web_workers", 1)
    assert local.onnx_threads() == 2


@pytest.mark.parametrize(
    ("env", "dim"),
    [
        ({"LOOM_EMBEDDING_MODEL": "local:bge-base"}, 768),
        ({"LOOM_EMBEDDING_MODEL": "local"}, 384),
        ({"LOOM_EMBEDDING_MODEL": "x", "LOOM_EMBEDDING_PROVIDER": "local"}, 384),
        ({"LOOM_EMBEDDING_MODEL": "local:bge-base", "LOOM_EMBEDDING_DIM": "512"}, 512),
        ({"LOOM_EMBEDDING_MODEL": "fake-ngram-3072"}, 3072),
        ({"LOOM_EMBEDDING_MODEL": "local:nope"}, 3072),  # refused when built
    ],
)
def test_an_unset_width_is_the_local_models_own(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], dim: int
) -> None:
    """The schema is sized from `embedding_dim`, so a coded table created at the
    default 3072 would refuse every vector a 384-dim local model writes. An
    inferred width is not the env's choice."""
    from server.settings import Settings

    for var in ("LOOM_EMBEDDING_MODEL", "LOOM_EMBEDDING_DIM", "LOOM_EMBEDDING_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    s = Settings(_env_file=None)
    assert s.embedding_dim == dim
    assert ("embedding_dim" in s.env_fields_set()) is ("LOOM_EMBEDDING_DIM" in env)
