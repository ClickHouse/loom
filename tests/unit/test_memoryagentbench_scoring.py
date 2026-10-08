def test_citation_markers_are_not_answer_words() -> None:
    from bench.memoryagentbench import score

    cited = "Debbie wore a green dress. [debbie_dress][chunk:chunk-00001:2]"
    assert score("AR", "qa", cited, ["Debbie wore a green dress"]) == {"sub_em": 1.0, "f1": 1.0}
