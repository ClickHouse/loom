import json
from pathlib import Path


def test_algorithm_gate_fixtures_are_valid() -> None:
    extraction = json.loads(Path("bench/fixtures/extraction_regressions.json").read_text())
    graph = json.loads(Path("bench/fixtures/graph_regressions.json").read_text())

    assert extraction and graph
    assert all(case.get("messages") and case.get("expected") for case in extraction)
    assert all(case.get("messages") and case.get("expected_triples") for case in graph)
