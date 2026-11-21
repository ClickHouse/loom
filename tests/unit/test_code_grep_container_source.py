"""Container documentation and data keep physical source coordinates."""

from dataclasses import asdict

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.app.grep import _line_coverage_fields, _matching_lines
from server.code.domain.extract.chunker import chunk_file
from server.code.domain.extract.lang_python import parse_python

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
