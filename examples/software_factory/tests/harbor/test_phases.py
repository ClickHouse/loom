"""Failed setup phases preserve their location without exposing request data."""

import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.harbor.one_shot import Phases


class PhaseFailures(unittest.TestCase):
    def test_failure_records_location_without_payload_and_refuses_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            phases = Phases(output, "a" * 32)

            def refuse():
                raise PermissionError("CREDENTIAL_SENTINEL")

            with self.assertRaises(PermissionError):
                phases.run("runtime", refuse)
            text = phases.path.read_text()
            self.assertNotIn("CREDENTIAL_SENTINEL", text)
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertEqual([row["event"] for row in rows], ["started", "failed"])
            failure = rows[-1]
            self.assertEqual(failure["error_type"], "PermissionError")
            self.assertEqual(failure["frames"][-1]["file"], "test_phases.py")
            self.assertGreater(failure["frames"][-1]["line"], 0)
            with self.assertRaisesRegex(PermissionError, "interrupted phase"):
                Phases(output, "a" * 32).run("runtime", lambda: {})
