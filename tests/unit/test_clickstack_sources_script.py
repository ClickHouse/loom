"""deploy/clickstack_sources.sh against a stub HyperDX API: a second run
updates the three sources instead of creating three more, both correlation
directions are set, no credential reaches the output or a command line, and
an unsafe target is refused before any request."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "clickstack_sources.sh"
COOKIE = "connect.sid=s%3Asecret-cookie-value"
PASSWORD = 'pa"ss\\word-secret'

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "curl", "python3")),
    reason="needs bash, curl and python3",
)


class _HyperDX(BaseHTTPRequestHandler):
    sources: list[dict[str, Any]]
    logins: list[dict[str, Any]]
    cookies: list[str]

    def log_message(self, *_: object) -> None:
        pass

    def _send(self, body: object, status: int = 200) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> Any:
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def do_GET(self) -> None:
        self.cookies.append(self.headers.get("Cookie", ""))
        if self.path == "/api/me":
            self._send({"email": "ops@example.com"})
        elif self.path == "/api/connections":
            self._send([{"id": "conn1", "name": "ch"}])
        elif self.path == "/api/sources":
            self._send(self.sources)
        else:
            self._send({}, 404)

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/api/login/password":
            self.logins.append(body)
            self._send({})
            return
        body["id"] = f"src{len(self.sources) + 1}"
        self.sources.append(body)
        self._send(body)

    def do_PUT(self) -> None:
        sid = self.path.rsplit("/", 1)[1]
        body = self._body()
        for i, s in enumerate(self.sources):
            if s["id"] == sid:
                self.sources[i] = body
        self._send(body)


@pytest.fixture
def hyperdx():
    handler = type("H", (_HyperDX,), {"sources": [], "logins": [], "cookies": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield handler, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_rerun_updates_in_place_and_wires_both_directions(hyperdx) -> None:
    handler, base = hyperdx
    for _ in range(2):
        res = _run({"HDX_BASE": base + "/", "HDX_COOKIE": COOKIE})
        assert res.returncode == 0, res.stderr
    by_name = {s["name"]: s for s in handler.sources}
    assert sorted(by_name) == ["loom-logs", "loom-metrics", "loom-traces"]
    ids = {k: v["id"] for k, v in by_name.items()}
    assert by_name["loom-logs"]["traceSourceId"] == ids["loom-traces"]
    assert by_name["loom-logs"]["metricSourceId"] == ids["loom-metrics"]
    assert by_name["loom-traces"]["logSourceId"] == ids["loom-logs"]
    assert by_name["loom-traces"]["durationPrecision"] == 9
    assert all(c == COOKIE for c in handler.cookies)
    assert "secret-cookie-value" not in res.stdout + res.stderr


def test_password_login_sends_valid_json_and_prints_no_password(hyperdx) -> None:
    handler, base = hyperdx
    res = _run({"HDX_BASE": base, "HDX_EMAIL": "ops@example.com", "HDX_PASSWORD": PASSWORD})
    assert res.returncode == 0, res.stderr
    assert handler.logins == [{"email": "ops@example.com", "password": PASSWORD}]
    assert "secret" not in res.stdout + res.stderr


@pytest.mark.parametrize(
    ("base", "why"),
    [
        ("http://hyperdx.example.com", "plain http"),
        ("https://hyperdx.example.com/api", "no path"),
        ("hyperdx.example.com", "no path"),
    ],
)
def test_unsafe_targets_are_refused_before_any_request(base: str, why: str) -> None:
    res = _run({"HDX_BASE": base, "HDX_COOKIE": COOKIE})
    assert res.returncode == 2
    assert why in res.stderr


def test_prefix_is_validated(hyperdx) -> None:
    _, base = hyperdx
    res = _run({"HDX_BASE": base, "HDX_COOKIE": COOKIE, "HDX_PREFIX": 'x","kind":"y'})
    assert res.returncode == 2 and "HDX_PREFIX" in res.stderr
