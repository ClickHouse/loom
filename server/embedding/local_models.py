"""The files of each local embedding model, pinned to one Hugging Face revision of the
export fastembed downloads for it and checked by sha256, so every install of a release
embeds with the same weights.

`model_dir` returns a model's checked directory, fetching what is missing at the
pinned revision. The image fetches the default model at build time
(`python -m server.embedding.local_models fetch bge-small`), so a server started
with no network still has it; a file whose hash does not match is refused, never
loaded.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.settings import settings
from server.settings.catalog import LOCAL_EMBEDDING_MODELS


@dataclass(frozen=True)
class PinnedModel:
    # The revision of the export fastembed downloads for the model (`_source`).
    revision: str
    # file name -> sha256
    files: dict[str, str]


# Each alias of LOOM_EMBEDDING_MODEL=local:<alias> (`LOCAL_EMBEDDING_MODELS`). The
# tokenizer files are what fastembed reads beside the ONNX model. A new revision is a
# new embedding space: it lands as a new alias, never as an edit to a pin here.
PINNED: dict[str, PinnedModel] = {
    "bge-small": PinnedModel(
        revision="aa8f8b060edb00e03bfdd08813a2949946c8ba55",
        files={
            "model_optimized.onnx": (
                "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"
            ),
            "config.json": "13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd",
            "special_tokens_map.json": (
                "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a"
            ),
            "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
            "tokenizer_config.json": (
                "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53"
            ),
        },
    ),
    "bge-base": PinnedModel(
        revision="199291fdd1aa89faf9c20b722dc72ad5e17aa0d0",
        files={
            "model_optimized.onnx": (
                "4e556722bc4f65716c544c8a931f1e90fb3f866e5741fd93a96f051d673339c7"
            ),
            "config.json": "86f84a5285de7f1ee673f712387219ef1e261ec27dcd870e793a80f9da1aaa3b",
            "special_tokens_map.json": (
                "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a"
            ),
            "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
            "tokenizer_config.json": (
                "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53"
            ),
        },
    ),
    "minilm": PinnedModel(
        revision="d13954661f83248295ba75c1ed411eef3b7b936e",
        files={
            "model.onnx": "bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5",
            "config.json": "1b4d8e2a3988377ed8b519a31d8d31025a25f1c5f8606998e8014111438efcd7",
            "special_tokens_map.json": (
                "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a"
            ),
            "tokenizer.json": "851ca67100d372ca3ae031a6abd168f53489eebfd7d89523f35c5c9b4d372c3c",
            "tokenizer_config.json": (
                "abda01c8c14c5151ae498aceb30db406d6b91242c394fd88d3b6fd5a63a101e6"
            ),
        },
    ),
}

_DOWNLOAD_TIMEOUT_S = 120  # not a setting: bounds one read of a stalled download


class ModelFileMismatch(RuntimeError):
    """A model file is not the pinned one, or is missing from the pins."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _described(alias: str) -> dict[str, Any]:
    """fastembed's description of the alias's model: its source and its files."""
    from fastembed import TextEmbedding

    hf_id = LOCAL_EMBEDDING_MODELS[alias][0]
    models: list[dict[str, Any]] = TextEmbedding.list_supported_models()
    for described in models:
        if described["model"] == hf_id:
            return described
    raise LookupError(f"fastembed does not list {hf_id} (local:{alias})")


def _source(alias: str) -> str:
    """The Hugging Face repository fastembed downloads the alias's model from."""
    return str(_described(alias)["sources"]["hf"])


def check_listing() -> None:
    """Every alias's pins name the model files fastembed's description lists for it (its
    ONNX file and the files beside it), so an upgrade that changes those fails the image
    build. The tokenizer files are checked by `fetch` loading the model."""
    for alias, model in PINNED.items():
        described = _described(alias)
        needed = {described["model_file"], *described["additional_files"]}
        if not needed <= set(model.files):
            raise ModelFileMismatch(f"local:{alias} pins no {sorted(needed - set(model.files))}")


def _fetch(repo: str, model: PinnedModel, name: str, dest: Path) -> None:
    """Download one file at the pinned revision; it lands only once its hash matches."""
    url = f"https://huggingface.co/{repo}/resolve/{model.revision}/{name}"
    digest = hashlib.sha256()
    # Named so a fetch killed mid-download leaves a file plainly not the model.
    with tempfile.NamedTemporaryFile(
        dir=dest.parent, prefix=f".{name}.", suffix=".partial", delete=False
    ) as tmp:
        partial = Path(tmp.name)
        try:
            with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
                for block in iter(lambda: resp.read(1 << 20), b""):
                    digest.update(block)
                    tmp.write(block)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    if digest.hexdigest() != model.files[name]:
        partial.unlink()
        raise ModelFileMismatch(f"{url} is not the pinned file (sha256 differs)")
    partial.chmod(0o644)  # a temp file is the owner's alone
    partial.replace(dest)


def model_dir(alias: str) -> Path:
    """The checked directory of `alias`'s files, fetching any that are missing.
    Raises ModelFileMismatch for a file, on disk or fetched, that is not the pinned one."""
    model = PINNED[alias]
    where = Path(settings.local_embedding_model_dir).expanduser() / alias / model.revision
    where.mkdir(parents=True, exist_ok=True)
    for name, sha in model.files.items():
        path = where / name
        if not path.exists():
            _fetch(_source(alias), model, name, path)
        elif _sha256(path) != sha:
            raise ModelFileMismatch(f"{path} is not the pinned file of {alias} (sha256 differs)")
    return where


def _embeds(alias: str, where: Path) -> None:
    """Load the files in `where` as the server does and embed one sentence."""
    from fastembed import TextEmbedding

    hf_id, dim = LOCAL_EMBEDDING_MODELS[alias]
    model = TextEmbedding(model_name=hf_id, specific_model_path=str(where))
    (vector,) = list(model.embed(["The image carries this model."]))
    if len(vector) != dim:
        raise ModelFileMismatch(f"local:{alias} embeds {len(vector)} dimensions, not {dim}")


def main(argv: list[str]) -> int:
    """`fetch <alias>...`: check every alias's pins against fastembed, then fetch, check
    and load each model named and embed one sentence with it, as the image build does."""
    if len(argv) < 2 or argv[0] != "fetch":
        print("usage: python -m server.embedding.local_models fetch <alias>...", file=sys.stderr)
        return 2
    check_listing()
    for alias in argv[1:]:
        where = model_dir(alias)
        _embeds(alias, where)
        print(where)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
