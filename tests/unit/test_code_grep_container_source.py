"""Container documentation and data keep physical source coordinates."""

import asyncio
import re
from dataclasses import asdict

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.app.grep import _line_coverage_fields, _matching_lines
from server.code.domain.extract.chunker import chunk_file
from server.code.domain.extract.lang_python import parse_python
from server.code.domain.symbol_body import retained_source_lines

SOURCE = '''class Handler:
    """Keep redirect requests on the declared route."""

    def run(self):
        return route_target()

    policy = "redirect"
'''


@pytest.mark.parametrize("pattern", ["redirect requests", 'policy = "redirect"'])
def test_class_source_outside_method_bodies_remains_greppable(pattern: str) -> None:
    parsed = parse_python("handler.py", SOURCE.encode())
    rows = [asdict(c) for c in chunk_file(parsed) if pattern in c.content]
    matches, capped = _matching_lines(rows, {}, CodeGrepInput(pattern=pattern), None, 20)
    expected = [
        (line_no, line.strip())
        for line_no, line in enumerate(SOURCE.splitlines(), 1)
        if pattern in line
    ]
    assert not capped
    assert [(hit["line"], hit["text"]) for hit in matches] == expected
    # Skeletons remain non-authoritative for source coordinates.
    assert _line_coverage_fields(rows, None)["partial"]


def test_function_body_keeps_its_existing_exact_source_coordinates() -> None:
    parsed = parse_python("handler.py", SOURCE.encode())
    rows = [asdict(c) for c in chunk_file(parsed) if "route_target" in c.content]
    matches, capped = _matching_lines(rows, {}, CodeGrepInput(pattern="route_target"), None, 20)
    assert not capped
    assert [(hit["line"], hit["text"]) for hit in matches] == [(5, "return route_target()")]


@pytest.mark.parametrize(
    "source_state", ["complete", "missing", "conflict", "changed", "failed", "capped"]
)
def test_grep_recovers_raw_source_before_disclosing_missing_coordinates(source_state):
    from server.code.app.grep_source import recover_source_chunks

    all_rows = [
        {**asdict(chunk), "embedding_model": "model"}
        for chunk in chunk_file(parse_python("handler.py", SOURCE.encode()))
    ]
    pattern = "redirect|route_target"
    line_re = re.compile(pattern)
    candidates = [row for row in all_rows if line_re.search(row["content"])]
    raw = [row for row in all_rows if retained_source_lines(row) is not None]
    calls = []

    class Repo:
        async def grep_source_chunks_for_span(self, org, ns, path, start, end, model):
            calls.append((org, ns, path, model))
            if source_state == "failed":
                raise RuntimeError("source read unavailable")
            if source_state == "missing":
                return []
            if source_state == "conflict":
                return [*raw, dict(raw[0], content=raw[0]["content"].replace("redirect", "reject"))]
            if source_state == "changed":
                return [
                    dict(row, content=row["content"].replace("redirect", "reject")) for row in raw
                ]
            return (raw * 401)[:401] if source_state == "capped" else raw

    recovered = asyncio.run(
        recover_source_chunks(Repo(), "org", "ns", candidates, max_chars=2_000_000)
    )
    matches, capped = _matching_lines(
        recovered, {}, CodeGrepInput(pattern=pattern, regex=True), line_re, 20
    )
    assert not capped
    assert calls == [("org", "ns", "handler.py", "model")]
    assert [(hit["line"], hit["text"]) for hit in matches] == [
        (2, '"""Keep redirect requests on the declared route."""'),
        (5, "return route_target()"),
        (7, 'policy = "redirect"'),
    ]
    assert bool(_line_coverage_fields(recovered, None).get("partial")) is (
        source_state != "complete"
    )
