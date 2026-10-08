"""Every model a benchmark server ran with, as one JSON record beside the results.

A benchmark number is only true with its instrument: the embedder, the
extraction model, each memory.ask tier and every other per-job model the read
and write paths call. This reads them the way the server does — the settings
loaded from the env file it was started with, plus overrides in the process
environment — and writes the `server/settings/_models.py` fields with their
resolved values (an empty one reads "(off)" or "(llm_model: …)" by what the field
means), so the record cannot
drift from what ran.

    LOOM_ENV_FILE=/path/to/.env python -m bench.models_used --out bench/results/models.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Fields whose empty value turns the job off rather than handing it to llm_model
# (see their comments in server/settings/_models.py).
OFF_WHEN_EMPTY = frozenset(
    {
        "llm_fallback_model",
        "memory_ask_synth_model_cheap",
        "memory_ask_mid_model",
        "memory_extract_verify_models",
        "code_search_hyde_model",
        "code_search_concept_expand_model",
    }
)


def snapshot() -> dict[str, str]:
    from server.settings import settings
    from server.settings._models import ModelSettings

    out: dict[str, str] = {
        "llm_provider": str(settings.llm_provider),
        "embedding_dim": str(settings.embedding_dim),
    }
    for name in ModelSettings.model_fields:
        value = str(getattr(settings, name) or "")
        if not value:
            value = "(off)" if name in OFF_WHEN_EMPTY else f"(llm_model: {settings.llm_model})"
        out[name] = value
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default="", help="write the record here (default: stdout only)")
    args = ap.parse_args()
    record = snapshot()
    text = json.dumps(record, indent=1, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
