"""Transport correlation must retain ids without recording credentials."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from examples.software_factory.harbor.live_support import TransportAudit


class TransportCorrelationContracts(unittest.TestCase):
    def test_missing_or_malformed_ids_stay_unknown(self):
        audit = TransportAudit()
        request = SimpleNamespace(url=SimpleNamespace(path="/v1/code.symbol"))
        for value in (None, "", "x" * 65, "bad\nid", "non-ascii-λ"):
            response = SimpleNamespace(
                request=request, status_code=200, headers={"x-request-id": value}
            )
            asyncio.run(audit.response(response))
            self.assertIsNone(audit.rows[-1].get("request_id"))

    def test_http_request_and_response_ids_survive_without_other_headers(self):
        audit = TransportAudit()
        request = SimpleNamespace(
            url=SimpleNamespace(path="/v1/code.symbol"),
            headers={"x-request-id": "client-123", "authorization": "secret-fixture"},
        )
        response = SimpleNamespace(
            request=request,
            status_code=200,
            headers={"x-request-id": "server-456", "set-cookie": "secret-cookie"},
        )
        asyncio.run(audit.request(request))
        asyncio.run(audit.response(response))
        self.assertEqual(audit.rows[0].get("request_id"), "client-123")
        self.assertEqual(audit.rows[1].get("request_id"), "server-456")
        self.assertNotIn("secret", json.dumps(audit.rows))
        self.assertEqual(audit.count(), 1)

    def test_bedrock_parsed_request_id_survives_without_response_content(self):
        audit = TransportAudit()
        audit.before_send()
        audit.after_call(
            http_response=SimpleNamespace(status_code=200),
            parsed={
                "ResponseMetadata": {"RequestId": "aws-789", "HTTPHeaders": {"secret": "value"}},
                "output": {"message": "private-content"},
            },
        )
        self.assertEqual(audit.rows[1].get("request_id"), "aws-789")
        self.assertNotIn("private-content", json.dumps(audit.rows))
        self.assertNotIn("HTTPHeaders", json.dumps(audit.rows))
        self.assertEqual(audit.count(), 1)


if __name__ == "__main__":
    unittest.main()
