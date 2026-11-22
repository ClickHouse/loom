"""Symbol fingerprints distinguish changed code from unrelated edits in its file."""

import copy
import unittest

from examples.software_factory.harbor.anchors import (
    KIND,
    changes,
    file_hash,
    identity,
    matches,
    validate,
)


class AnchorEvidence(unittest.TestCase):
    def setUp(self):
        self.binding = {"org": "org", "code_namespace": "code"}
        self.fixture = {
            "source_revisions": {"A": {"src/a.py": "old\n"}, "B": {"src/a.py": "new\n"}}
        }
        self.links = [{"path": "src/a.py", "symbol": name} for name in ("a.stable", "a.edited")]
        self.cert = {
            "source_heads": {"A": "a" * 40, "B": "b" * 40},
            "events": [{"phase": "A", "operation": "memory.get", "response": {"code": self.links}}],
            "anchor_fingerprints": {"A": {}, "B": {}},
        }
        for revision in ("A", "B"):
            self.cert["anchor_fingerprints"][revision] = {
                "kind": KIND,
                "org": "org",
                "namespace": "code",
                "commit": self.cert["source_heads"][revision],
                "fingerprint_version": "fixture-v1",
                "anchors": {},
            }
            for i, link in enumerate(self.links):
                value = ("e" if revision == "B" and i == 1 else "c") * 64
                self.cert["anchor_fingerprints"][revision]["anchors"][identity(link)] = {
                    **link,
                    "body_hash": value,
                    "file_sha256": file_hash(
                        self.fixture["source_revisions"][revision], link["path"]
                    ),
                }

    def test_unchanged_symbol_stays_fresh_inside_a_changed_file(self):
        result = validate(self.cert, self.fixture, self.binding)
        stable, edited = self.links
        self.assertEqual(result[identity(stable)], "fresh")
        self.assertTrue(matches({**stable, "state": "fresh"}, result, "b" * 40))
        self.assertFalse(
            matches({**stable, "state": "changed", "changed_since": "b" * 40}, result, "b" * 40)
        )
        self.assertEqual(result[identity(edited)], "changed")
        self.assertFalse(matches({**edited, "state": "fresh"}, result, "b" * 40))
        self.assertTrue(
            matches({**edited, "state": "changed", "changed_since": "b" * 40}, result, "b" * 40)
        )

    def test_foreign_scope_wrong_revision_and_malformed_hash_refuse(self):
        key = identity(self.links[0])
        for field, value in (
            ("body_hash", "invalid"),
            ("commit", "f" * 40),
            ("namespace", "foreign"),
            ("kind", "retrieval-chunk"),
            ("fingerprint_version", "other-rules"),
        ):
            altered = copy.deepcopy(self.cert)
            snapshot = altered["anchor_fingerprints"]["B"]
            if field == "body_hash":
                snapshot["anchors"][key][field] = value
            else:
                snapshot[field] = value
            with self.subTest(field=field), self.assertRaises(PermissionError):
                validate(altered, self.fixture, self.binding)

    def test_native_file_links_track_existence_rather_than_declaration_edits(self):
        link = {"path": "src/a.py", "symbol": ""}
        fingerprints = {
            rev: {"anchors": {identity(link): {**link, "body_hash": ""}}}
            for rev, files in self.fixture["source_revisions"].items()
        }
        self.assertEqual(changes(fingerprints)[identity(link)], "fresh")
        fingerprints["B"]["anchors"][identity(link)]["body_hash"] = None
        self.assertEqual(changes(fingerprints)[identity(link)], "missing")
