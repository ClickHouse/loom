"""The local embedding models' files: pinned by revision and sha256, fetched at the
pin, and refused when what is on disk is not the pinned file."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from server.embedding import local_models
from server.embedding.local_models import PINNED, ModelFileMismatch, PinnedModel
from server.settings import settings
from server.settings.catalog import LOCAL_EMBEDDING_MODELS

_BODY = {"model.onnx": b"weights", "tokenizer.json": b"{}"}
_TOY = PinnedModel(
    revision="r1",
    files={name: hashlib.sha256(body).hexdigest() for name, body in _BODY.items()},
)


@pytest.fixture
def toy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """A pinned toy model under tmp_path; returns the URLs fetched."""
    fetched: list[str] = []

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        fetched.append(url)
        return io.BytesIO(_BODY[url.rsplit("/", 1)[1]])

    monkeypatch.setitem(PINNED, "toy", _TOY)
    monkeypatch.setattr(local_models, "_source", lambda alias: "org/toy")
    monkeypatch.setattr(settings, "local_embedding_model_dir", str(tmp_path))
    monkeypatch.setattr(local_models.urllib.request, "urlopen", urlopen)
    return fetched


def test_every_local_alias_is_pinned_to_a_commit_and_file_hashes() -> None:
    assert set(PINNED) == set(LOCAL_EMBEDDING_MODELS)
    for model in PINNED.values():
        assert len(model.revision) == 40 and all(len(sha) == 64 for sha in model.files.values())


def test_fastembed_knows_each_alias_source_and_files() -> None:
    """Also checked by the image build (`fetch`), where fastembed is installed."""
    pytest.importorskip("fastembed")
    local_models.check_listing()
    assert all("/" in local_models._source(alias) for alias in PINNED)


def test_a_file_fastembed_loads_but_the_pins_lack_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = {"model_file": "model.onnx", "additional_files": ["model.onnx_data"]}
    monkeypatch.setattr(local_models, "PINNED", {"toy": _TOY})
    monkeypatch.setattr(local_models, "_described", lambda alias: listing)
    with pytest.raises(ModelFileMismatch, match="model.onnx_data"):
        local_models.check_listing()


def test_missing_files_are_fetched_at_the_pinned_revision(toy: list[str], tmp_path: Path) -> None:
    where = local_models.model_dir("toy")
    assert where == tmp_path / "toy" / "r1"
    assert sorted(toy) == [
        "https://huggingface.co/org/toy/resolve/r1/model.onnx",
        "https://huggingface.co/org/toy/resolve/r1/tokenizer.json",
    ]
    assert (where / "model.onnx").read_bytes() == b"weights"
    local_models.model_dir("toy")
    assert len(toy) == 2  # checked on disk, not fetched again


def test_a_fetched_file_that_is_not_the_pinned_one_never_lands(
    toy: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(_BODY, "model.onnx", b"other weights")
    with pytest.raises(ModelFileMismatch):
        local_models.model_dir("toy")
    assert list((tmp_path / "toy" / "r1").iterdir()) == []  # nor its temp file


def test_a_download_cut_off_leaves_nothing_behind(
    toy: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class CutOff(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise ConnectionResetError("cut off")

    monkeypatch.setattr(local_models.urllib.request, "urlopen", lambda url, timeout: CutOff())
    with pytest.raises(ConnectionResetError):
        local_models.model_dir("toy")
    assert list((tmp_path / "toy" / "r1").iterdir()) == []


def test_a_changed_file_on_disk_is_refused(toy: list[str], tmp_path: Path) -> None:
    where = local_models.model_dir("toy")
    (where / "model.onnx").write_bytes(b"tampered")
    with pytest.raises(ModelFileMismatch, match="model.onnx"):
        local_models.model_dir("toy")
