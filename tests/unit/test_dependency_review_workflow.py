"""Exercise the exact inline dependency policy with synthetic API responses."""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github/workflows/dependency-review.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text())
RUN = WORKFLOW["jobs"]["dependencies"]["steps"][0]["run"]
SOURCE = RUN.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
POLICY = types.ModuleType("dependency_review_policy")
exec(compile(SOURCE, str(WORKFLOW_PATH), "exec"), POLICY.__dict__)

REPOSITORY = "owner/project"
REPOSITORY_ID = 1234
BASE = "a" * 40
HEAD = "b" * 40
ENDPOINT = f"https://api.github.com/repos/{REPOSITORY}/dependency-graph/compare/{BASE}...{HEAD}"
CANONICAL = (
    f"https://api.github.com/repositories/{REPOSITORY_ID}/dependency-graph/compare/{BASE}...{HEAD}"
)


def change(severity="high", kind="added"):
    return {
        "change_type": kind,
        "name": "fixture-package",
        "version": "1.0",
        "manifest": "package.json",
        "vulnerabilities": [{"severity": severity, "advisory_ghsa_id": "GHSA-abcd-1234-5678"}],
    }


class Response:
    def __init__(self, data, *, headers=None, status=200, raw=None, read_error=None):
        self.body = io.BytesIO(raw if raw is not None else json.dumps(data).encode())
        self.headers = headers or {}
        self.status = status
        self.read_error = read_error

    def read(self, size):
        if self.read_error is not None:
            raise self.read_error
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def review(*responses):
    return POLICY.review(REPOSITORY, BASE, HEAD, "test-token", Opener(*responses), REPOSITORY_ID)


class DependencyReviewTests(unittest.TestCase):
    def test_added_high_and_critical_fail_but_lower_and_removed_pass(self):
        result = review(
            Response([change("low"), change("moderate"), change("critical", "removed")])
        )
        self.assertEqual(result["blocked_advisories"], 0)
        result = review(Response([change("high"), change("critical")]))
        self.assertEqual(result["blocked_advisories"], 2)
        self.assertEqual([row["severity"] for row in result["findings"]], ["high", "critical"])

    def test_empty_diff_is_complete(self):
        self.assertEqual(
            review(Response([])), {"changes": 0, "blocked_advisories": 0, "findings": []}
        )

    def test_later_page_findings_and_query_order(self):
        opener = Opener(
            Response([], headers={"Link": f'<{ENDPOINT}?page=2&per_page=100>; rel="next"'}),
            Response([change()]),
        )
        result = POLICY.review(REPOSITORY, BASE, HEAD, "test-token", opener, REPOSITORY_ID)
        self.assertEqual(result["blocked_advisories"], 1)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.requests[1][0].full_url, ENDPOINT + "?per_page=100&page=2")
        self.assertEqual(opener.requests[0][0].get_header("Authorization"), "Bearer test-token")
        self.assertEqual(opener.requests[0][1], 20)

    def test_canonical_repository_id_links_are_accepted(self):
        opener = Opener(
            Response([], headers={"Link": f'<{CANONICAL}?per_page=100&page=2>; rel="next"'}),
            Response([change()]),
        )
        result = POLICY.review(REPOSITORY, BASE, HEAD, "test-token", opener, REPOSITORY_ID)
        self.assertEqual(result["blocked_advisories"], 1)
        self.assertEqual(opener.requests[1][0].full_url, ENDPOINT + "?per_page=100&page=2")

    def test_invalid_numeric_repository_identity_fails(self):
        for repository_id in (None, True, "1234", -1, 2**63):
            with self.subTest(repository_id=repository_id), self.assertRaises(POLICY.ReviewError):
                POLICY.review(REPOSITORY, BASE, HEAD, "token", Opener(), repository_id)

    def test_empty_snapshot_header_and_encoded_empty_list_pass(self):
        for header in ("", base64.b64encode(b"[]").decode()):
            with self.subTest(header=header):
                self.assertEqual(
                    review(
                        Response(
                            [], headers={"x-github-dependency-graph-snapshot-warnings": header}
                        )
                    )["blocked_advisories"],
                    0,
                )

    def test_incomplete_or_malformed_snapshots_fail(self):
        for raw in (b'["missing snapshot"]', b"{}", b"null", b"not JSON"):
            with self.subTest(raw=raw), self.assertRaises(POLICY.ReviewError):
                review(
                    Response(
                        [],
                        headers={
                            "x-github-dependency-graph-snapshot-warnings": base64.b64encode(
                                raw
                            ).decode()
                        },
                    )
                )
        with self.assertRaises(POLICY.ReviewError):
            review(Response([], headers={"x-github-dependency-graph-snapshot-warnings": "%%%"}))

    def test_http_and_timeout_fail_without_retry(self):
        for code in (403, 404, 429, 503):
            opener = Opener(urllib.error.HTTPError(ENDPOINT, code, "failure", {}, None))
            with self.subTest(code=code), self.assertRaises(POLICY.ReviewError):
                POLICY.review(REPOSITORY, BASE, HEAD, "test-token", opener, REPOSITORY_ID)
            self.assertEqual(len(opener.requests), 1)
        with self.assertRaises(POLICY.ReviewError):
            review(TimeoutError("fixture timeout"))

    def test_redirects_are_not_followed(self):
        self.assertIsNone(
            POLICY.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid")
        )
        with self.assertRaises(POLICY.ReviewError):
            review(Response([], status=302))

    def test_unknown_severity_and_malformed_schema_fail(self):
        malformed = [
            None,
            [],
            {},
            {**change(), "vulnerabilities": None},
            {**change(), "name": None},
            {**change(), "change_type": "changed"},
            change("unknown"),
            {**change(), "vulnerabilities": [{"severity": "high"}]},
        ]
        for row in malformed:
            with self.subTest(row=row), self.assertRaises(POLICY.ReviewError):
                review(Response([row]))
        for raw in (b"{", b"{}", b"null", b'"not a list"'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                review(Response([], raw=raw))

    def test_response_record_and_vulnerability_bounds_fail_closed(self):
        responses = [
            Response([], raw=b" " * (POLICY.MAX_BYTES + 1)),
            Response([change()] * (POLICY.PAGE_SIZE + 1)),
            Response([{**change(), "vulnerabilities": change()["vulnerabilities"] * 101}]),
            Response([{**change(), "name": "x" * 4097}]),
        ]
        for response in responses:
            with (
                self.subTest(size=len(response.body.getvalue())),
                self.assertRaises(POLICY.ReviewError),
            ):
                review(response)

    def test_unexpected_or_malformed_next_link_fails_before_request(self):
        links = [
            '<https://example.invalid/>; rel="next"',
            f'<https://api.github.com/repositories/9999/dependency-graph/compare/{BASE}...{HEAD}?per_page=100&page=2>; rel="next"',
            f'<{ENDPOINT}?per_page=100&page=1>; rel="next"',
            f'<{ENDPOINT}?per_page=100&page=3>; rel="next"',
            f'<{ENDPOINT}?per_page=100&page=2&extra=1>; rel="next"',
            f'<{ENDPOINT}?per_page=100&page=2#fragment>; rel="next"',
            f'<{ENDPOINT}?per_page=100&page=2>; rel="next", <{ENDPOINT}>; rel="next"',
            "malformed",
        ]
        for link in links:
            opener = Opener(Response([], headers={"Link": link}))
            with self.subTest(link=link), self.assertRaises(POLICY.ReviewError):
                POLICY.review(REPOSITORY, BASE, HEAD, "test-token", opener, REPOSITORY_ID)
            self.assertEqual(len(opener.requests), 1)

    def test_page_limit_fails_instead_of_truncating_successfully(self):
        opener = Opener(
            *(
                Response(
                    [], headers={"Link": f'<{ENDPOINT}?per_page=100&page={page + 1}>; rel="next"'}
                )
                for page in range(1, POLICY.MAX_PAGES + 1)
            )
        )
        with self.assertRaisesRegex(POLICY.ReviewError, "page limit"):
            POLICY.review(REPOSITORY, BASE, HEAD, "test-token", opener, REPOSITORY_ID)
        self.assertEqual(len(opener.requests), POLICY.MAX_PAGES)

    def test_report_is_bounded_without_losing_failure_count(self):
        row = {**change(), "name": "x\n::error::fixture" * 30}
        result = review(Response([row] * POLICY.PAGE_SIZE))
        self.assertEqual(result["blocked_advisories"], POLICY.PAGE_SIZE)
        self.assertEqual(len(result["findings"]), POLICY.MAX_FINDINGS)
        self.assertEqual(len(result["findings"][0]["name"]), 200)
        self.assertNotIn("\n", json.dumps(result))

    def test_invalid_request_inputs_fail_before_network(self):
        for args in (
            ("https://example.invalid", BASE, HEAD, "token"),
            (REPOSITORY, "main", HEAD, "token"),
            (REPOSITORY, BASE, HEAD, ""),
        ):
            opener = Opener()
            with self.subTest(args=args), self.assertRaises(POLICY.ReviewError):
                POLICY.review(*args, opener, REPOSITORY_ID)
            self.assertEqual(opener.requests, [])

    def test_main_exit_codes_and_no_raw_failure_output(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(
                json.dumps(
                    {
                        "repository": {"id": REPOSITORY_ID},
                        "pull_request": {"base": {"sha": BASE}, "head": {"sha": HEAD}},
                    }
                )
            )
            env = {
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_TOKEN": "test-token",
            }
            for response, expected in (
                (Response([]), 0),
                (Response([change()]), 1),
                (TimeoutError("private diagnostic fixture"), 2),
                (http.client.BadStatusLine("private diagnostic fixture"), 2),
                (
                    Response(
                        [], read_error=http.client.IncompleteRead(b"private diagnostic fixture", 5)
                    ),
                    2,
                ),
            ):
                output = io.StringIO()
                with (
                    patch.dict(os.environ, env),
                    patch.object(
                        POLICY.urllib.request, "build_opener", return_value=Opener(response)
                    ),
                    redirect_stdout(output),
                ):
                    self.assertEqual(POLICY.main(), expected)
                self.assertNotIn("test-token", output.getvalue())
                self.assertNotIn("private diagnostic fixture", output.getvalue())

    def test_workflow_uses_only_public_compute_and_readonly_api(self):
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        job = WORKFLOW["jobs"]["dependencies"]
        self.assertEqual(job["if"], "github.event.repository.private == false")
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["timeout-minutes"], 5)
        self.assertEqual(len(job["steps"]), 1)
        self.assertNotIn("uses", job["steps"][0])


if __name__ == "__main__":
    unittest.main()
