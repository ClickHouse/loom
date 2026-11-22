"""A reviewed output-equivalence claim requires exact current evidence."""

import hashlib
import unittest

from tests.mutation.run_identity import _mark_reviewed_equivalents


class ReviewedEquivalence(unittest.TestCase):
    def setUp(self) -> None:
        self.source = hashlib.sha256(b"fixed source").hexdigest()
        self.rows = [{"name": "fixed", "status": "survived", "diff": "exact diff"}]
        self.evidence = {
            "source_sha256": self.source,
            "mutmut_version": "pinned",
            "equivalents": [
                {
                    "name": "fixed",
                    "diff_sha256": hashlib.sha256(b"exact diff").hexdigest(),
                    "reason": "The changed split bound retains the same final suffix.",
                }
            ],
        }

    def test_exact_evidence_retains_the_raw_survivor_and_adds_its_reason(self) -> None:
        _mark_reviewed_equivalents(self.rows, self.source, "pinned", self.evidence)
        self.assertEqual(self.rows[0]["status"], "survived")
        self.assertEqual(self.rows[0]["disposition"], "reviewed-output-equivalent")
        self.assertTrue(self.rows[0]["reason"])

    def test_changed_source_or_tool_has_no_reviewed_classification(self) -> None:
        for source, tool in [("changed", "pinned"), (self.source, "changed")]:
            with self.subTest(source=source, tool=tool):
                _mark_reviewed_equivalents(self.rows, source, tool, self.evidence)
                self.assertNotIn("disposition", self.rows[0])

    def test_changed_diff_and_unknown_name_remain_unreviewed(self) -> None:
        for name, diff in [("fixed", "changed"), ("unknown", "exact diff")]:
            with self.subTest(name=name, diff=diff):
                rows = [{"name": name, "status": "survived", "diff": diff}]
                _mark_reviewed_equivalents(rows, self.source, "pinned", self.evidence)
                self.assertNotIn("disposition", rows[0])

    def test_an_empty_reason_refuses_instead_of_hiding_the_outcome(self) -> None:
        self.evidence["equivalents"][0]["reason"] = ""
        with self.assertRaisesRegex(ValueError, "reviewed reason"):
            _mark_reviewed_equivalents(self.rows, self.source, "pinned", self.evidence)


if __name__ == "__main__":
    unittest.main()
