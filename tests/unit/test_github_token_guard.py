"""Check offline recognition and masking with unissued credential shapes."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import string
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "github_token_guard_under_test", ROOT / "scripts/gates/check_no_secrets.py"
)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


def body(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(
        alphabet[value % len(alphabet)] for value in hashlib.shake_256(b"unissued").digest(length)
    )


def encoded(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


class GitHubTokenShapes(unittest.TestCase):
    def scan(self, text: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.txt"
            path.write_text(text)
            return GUARD._scan(str(path))

    def one(self, token: str):
        findings = self.scan(f'CUSTOM_CREDENTIAL="{token}"\n')
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].value, token)
        return findings[0]

    def test_classic_personal_access_shape(self):
        self.one("ghp_" + body(36))

    def test_fine_grained_personal_access_shape(self):
        self.one("github_pat_" + body(22) + "_" + body(59))

    def test_stateless_installation_shape_is_not_truncated(self):
        header = encoded({"alg": "RS256", "typ": "JWT"})
        payload = encoded({"sub": "synthetic", "exp": 0, "padding": body(500)})
        signature = (
            base64.urlsafe_b64encode(hashlib.sha256(b"unissued").digest()).decode().rstrip("=")
        )
        token = "ghs_0_" + ".".join([header, payload, signature])
        self.assertGreater(len(token), 520)
        self.one(token)

    def test_legacy_installation_shape(self):
        self.one("ghs_" + body(36))

    def test_references_and_short_placeholders_are_not_credentials(self):
        values = [
            "${GH_TOKEN}",
            "ghp_<token>",
            "github_pat_<token>",
            "ghs_example",
            "ghp_" + body(35),
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(self.scan(f'VALUE="{value}"\n'))

    def test_personal_access_boundaries_do_not_match_longer_identifiers(self):
        values = [
            "prefixghp_" + body(36),
            "ghp_" + body(37),
            "github_pat_" + body(81),
            "github_pat_" + body(83),
            "GHP_" + body(36),
        ]
        for value in values:
            with self.subTest(length=len(value)):
                self.assertFalse(self.scan(f'VALUE="{value}"\n'))

    def test_existing_provider_and_scoped_password_rules_remain(self):
        provider = "sk-" + body(20) + "T3BlbkFJ" + body(20)
        findings = self.scan(f'OPENAI_API_KEY="{provider}"\nLOOM_JWT_SECRET="{body(48)}"\n')
        self.assertEqual(
            [row.label for row in findings], ["OpenAI API key", "loom secret assignment"]
        )

    def test_cli_finding_text_masks_the_full_value(self):
        token = "ghp_" + body(36)
        finding = self.one(token)
        self.assertNotIn(token, str(finding))
        self.assertIn("…", str(finding))

    def test_annotation_has_the_actual_location_and_static_rule(self):
        finding = self.one("ghp_" + body(36))
        annotation = finding.annotation()
        self.assertTrue(annotation.startswith("::error file="))
        self.assertIn(
            ",line=1,title=GitHub personal access token::Possible credential.", annotation
        )
        self.assertNotIn(finding.value, annotation)
        self.assertNotIn(finding.value[:4], annotation)

    def test_annotation_escapes_workflow_command_properties(self):
        finding = GUARD.Finding(
            "notes%,:\r\n::error file=injected.py", 23, "Synthetic,\r\nRule%", "unused"
        )
        annotation = finding.annotation()
        self.assertTrue(
            annotation.startswith("::error file=notes%25%2C%3A%0D%0A%3A%3Aerror file=injected.py,")
        )
        self.assertIn(",line=23,title=Synthetic%2C%0D%0ARule%25::", annotation)
        self.assertEqual(len(annotation.splitlines()), 1)
        self.assertEqual(annotation.count("::error "), 1)

    def test_annotation_never_formats_matched_value_fragments(self):
        value = "SENSITIVE_VALUE_NOT_FOR_OUTPUT_END"
        finding = GUARD.Finding("data.txt", 4, "Synthetic rule", value)
        annotation = finding.annotation()
        self.assertEqual(
            annotation, "::error file=data.txt,line=4,title=Synthetic rule::Possible credential."
        )
        for fragment in [value, value[:4], value[-2:]]:
            self.assertNotIn(fragment, annotation)

    def test_token_in_a_url_is_recognized_without_a_named_variable(self):
        token = "ghp_" + body(36)
        findings = self.scan(f'URL="https://{token}@example.invalid/repo"\n')
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].value, token)


if __name__ == "__main__":
    unittest.main()
