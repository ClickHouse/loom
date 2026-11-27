import base64
import unittest

from examples.software_factory.evals.flask.grading import validate_candidate, validate_fixtures
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.index import MUTABLE_STATE_KEYS, TABLES, fingerprint


class HarborContracts(unittest.TestCase):
    def test_trusted_fixtures_preserve_bytes_without_expanding_candidate_authority(self):
        fixtures = {"tests/app/.env": base64.b64encode(b"VALUE=fixture\n").decode()}
        self.assertEqual(
            validate_fixtures(fixtures, {}, digest(fixtures)),
            {"tests/app/.env": b"VALUE=fixture\n"},
        )
        for name in ("../outside", ".git/config", "src/flask/app.py"):
            invalid = {name: base64.b64encode(b"fixture").decode()}
            with self.assertRaises(PermissionError):
                validate_fixtures(invalid, {}, digest(invalid))
        with self.assertRaises(PermissionError):
            validate_fixtures(fixtures, {"tests/app/.env": "base"}, digest(fixtures))
        with self.assertRaises(PermissionError):
            validate_fixtures(fixtures, {}, "0" * 64)

    def test_only_named_mutable_state_is_excluded_from_source_fingerprint(self):
        source = {t: [] for t in TABLES}
        row = {"org": "org", "namespace": "flask", "state_key": "git_head", "value": "base"}
        source["code_index_state"] = [row]
        expected = fingerprint(source)
        for key in MUTABLE_STATE_KEYS:
            source["code_index_state"].append({**row, "state_key": key, "value": "cached"})
        self.assertEqual(fingerprint(source), expected)
        source["code_index_state"].append({**row, "state_key": "unknown_future_state"})
        self.assertNotEqual(fingerprint(source), expected)
        source["code_index_state"] = [{**row, "value": "changed-base"}]
        self.assertNotEqual(fingerprint(source), expected)
        source["code_index_state"] = [row]
        source["code_symbols"] = [{"org": "org", "namespace": "flask", "name": "changed"}]
        self.assertNotEqual(fingerprint(source), expected)

    def test_native_candidate_allows_source_changes_and_rejects_evaluator_injection(self):
        base = {"src/flask/app.py": "base", "tests/test_app.py": "private"}
        expected = digest({"src/flask/app.py": "base"})
        changed = {"src/flask/added.py": "candidate"}
        self.assertEqual(validate_candidate(changed, base, expected), changed)
        for files in ({"tests/test_app.py": "pass"}, {"src/flask/../test.py": "pass"}):
            with self.assertRaises(PermissionError):
                validate_candidate(files, base, expected)
        with self.assertRaises(PermissionError):
            validate_candidate(changed, base, "wrong-base")
