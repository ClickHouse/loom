"""Every LLM stage literal in the server is a registered stage.

`genai.llm_stage` maps an unknown name to "other" so the metric label set
stays bounded — which also means an unregistered helper disappears from
cost and latency attribution. This test makes the
registry the source of truth: a new `llm_stage("x")`, `@staged("x")` or
`stage="x"` literal must be added to `_KNOWN_STAGES` to ship.
"""

from __future__ import annotations

import re
from pathlib import Path

from server.observability import genai

ROOT = Path(__file__).resolve().parents[2] / "server"
_PATTERNS = (
    re.compile(r'llm_stage\(\s*"([A-Za-z0-9_.]+)"\s*\)'),
    re.compile(r'@staged\(\s*"([A-Za-z0-9_.]+)"\s*\)'),
    re.compile(r'\bstage\s*(?::\s*str\s*)?=\s*"([A-Za-z0-9_.]+)"'),
)


def stage_literals() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pat in _PATTERNS:
            for m in pat.finditer(text):
                found.setdefault(m.group(1), []).append(str(path.relative_to(ROOT.parent)))
    return found


def test_every_stage_literal_is_registered() -> None:
    found = stage_literals()
    assert found, "no stage literals found — the scan is broken"
    unknown = {name: files for name, files in found.items() if name not in genai._KNOWN_STAGES}
    assert not unknown, f"stage literals not in genai._KNOWN_STAGES: {unknown}"


# Stage names built at runtime rather than written as a literal.
_BUILT = {"ask_synthesize_cheap", "ask_synthesize_mid"}  # f"ask_synthesize_{tier}" (cascade.py)
_CONSTANTS = {"bulk_contextualize"}  # chunk_context.STAGE


def test_every_registered_stage_has_a_caller() -> None:
    """A stage whose caller was retired is a dead label; drop it with the caller."""
    found = set(stage_literals())
    dead = set(genai._KNOWN_STAGES) - found - _BUILT - _CONSTANTS - {"other"}
    assert not dead, f"registered stages no server code sets: {sorted(dead)}"


def test_unknown_stage_still_collapses_to_other() -> None:
    with genai.llm_stage("not_a_registered_stage"):
        assert genai.current_stage() == "other"
    with genai.llm_stage("ask_decompose"):
        assert genai.current_stage() == "ask_decompose"


def test_query_only_classification_covers_the_decomposition_leg() -> None:
    assert "ask_decompose" in genai._QUERY_ONLY_STAGES
    # Legs that paste stored memories into the prompt stay content-class.
    for name in ("ask_deep_select", "ask_consistency", "ask_premise"):
        assert name not in genai._QUERY_ONLY_STAGES
