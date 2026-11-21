"""Symbol fingerprints distinguish changed code from unrelated edits in its file."""

import copy
import unittest

from examples.software_factory.harbor.anchors import (
    SYMBOL_OPTIONS,
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
            for i, link in enumerate(self.links):
                value = ("e" if revision == "B" and i == 1 else "c") * 64
                self.cert["anchor_fingerprints"][revision][identity(link)] = {
                    **link,
                    "body_hash": value,
                    "receipt": {
                        "operation": "code.symbol",
                        "phase": revision,
                        "source_commit": self.cert["source_heads"][revision],
                        "request_id": revision + str(i),
                        "status_code": 200,
                        "physical_requests": 1,
                        "request": {
                            "org": "org",
                            "namespace": "code",
                            "name": link["symbol"],
                            **SYMBOL_OPTIONS,
                        },
                        "response": {"definitions": [{**link, "body_hash": value}]},
                    },
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

    def test_foreign_scope_wrong_revision_and_tampered_hash_refuse(self):
        key = identity(self.links[0])
        for field, value in (
            ("body_hash", "f" * 64),
            ("source_commit", "f" * 40),
            ("namespace", "foreign"),
        ):
            altered = copy.deepcopy(self.cert)
            row = altered["anchor_fingerprints"]["B"][key]
            if field == "body_hash":
                row[field] = value
            elif field == "source_commit":
                row["receipt"][field] = value
            else:
                row["receipt"]["request"][field] = value
            with self.subTest(field=field), self.assertRaises(PermissionError):
                validate(altered, self.fixture, self.binding)

    def test_file_links_compare_whole_file_content(self):
        link = {"path": "src/a.py", "symbol": ""}
        fingerprints = {
            rev: {identity(link): {**link, "body_hash": file_hash(files, link["path"])}}
            for rev, files in self.fixture["source_revisions"].items()
        }
        self.assertEqual(changes(fingerprints)[identity(link)], "changed")
