"""Historical knowledge is independently dated and cannot encode the observed failing test."""

import copy
import json
import unittest

from examples.software_factory.evals.flask.memory import historical


class HistoricalCorpus(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads(historical.CATALOG.read_text())
        self.cases = json.loads(historical.CASES.read_text())["cases"]
        self.paths = {path for row in self.catalog["records"] for path in row["files"]}

    def test_same_dated_corpus_is_eligible_for_each_unchanged_historical_task(self):
        self.assertEqual(len(self.cases), 3)
        self.assertEqual(len(self.catalog["records"]), 24)
        self.assertEqual(sum(bool(row["files"]) for row in self.catalog["records"]), 10)
        for case in self.cases:
            historical.validate_catalog(self.catalog, case, self.paths)

    def test_observed_failure_and_target_solutions_are_not_memory_content(self):
        text = json.dumps([row["value"] for row in self.catalog["records"]])
        for forbidden in (
            "ValueError",
            "AssertionError",
            "test_route_decorator_custom_endpoint_with_dots",
            "51 of 52",
            "6/9",
            "9/9",
            "hidden test",
            "raise ",
        ):
            self.assertNotIn(forbidden, text)

    def test_later_edits_target_discussions_and_unscoped_paths_refuse(self):
        mutations = [
            lambda d: d["sources"]["pr-2450"].update(last_edited_at="2025-01-01T00:00:00Z"),
            lambda d: d["sources"]["pr-2450"].update(number=4045),
            lambda d: d["records"][0].update(files=["tests/private.py"]),
            lambda d: d["records"][0].update(source_ids=["unverified"]),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                corpus = copy.deepcopy(self.catalog)
                mutate(corpus)
                with self.assertRaises(PermissionError):
                    historical.validate_catalog(corpus, self.cases[0], self.paths)
