"""Run a Loom code-link check on the playground's sample definition."""

import json
import re
from http.server import BaseHTTPRequestHandler

from api.playground._loom_anchor_state import definition_state, fingerprint

SAVED_STATUS = 503
QUERY = re.compile(r"status=([1-5][0-9]{2})")


def _source(status: int) -> str:
    return f"def should_retry(status: int) -> bool:\n    return status == {status}"


class handler(BaseHTTPRequestHandler):
    """Vercel discovers the handler by name."""

    def do_GET(self) -> None:
        match = QUERY.fullmatch(self.path.partition("?")[2])
        if match is None:
            self._reply(400, {"error": "Choose an HTTP status from 100 to 599."})
            return
        status = int(match[1])
        source = _source(status)
        saved = fingerprint([_source(SAVED_STATUS)])
        current = fingerprint([source])
        self._reply(
            200,
            {
                "definition": "retry.py::should_retry",
                "source": source,
                "saved_fingerprint": saved,
                "checked_fingerprint": current,
                "code_state": definition_state(saved, current),
            },
        )

    def _reply(self, status: int, body: dict[str, str]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
