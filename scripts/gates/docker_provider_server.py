"""Deterministic embeddings fixture, reachable only inside the smoke network.

This is not a real provider and does not validate paid credentials or LLM quality.
The control endpoint deliberately has no authentication: never deploy this fixture.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMENSIONS = {"docker-smoke-8": 8, "docker-smoke-16": 16}
KEYS = {"docker-smoke-embedding-key", "docker-smoke-rotated-key"}


def embedding(model: str, text: str) -> list[float]:
    digest = hashlib.sha256(f"{model}:{text}".encode()).digest()
    values = [float(digest[index] + 1) for index in range(DIMENSIONS[model])]
    norm = sum(value * value for value in values) ** 0.5
    return [value / norm for value in values]


class ProviderServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int]):
        super().__init__(address, Handler)
        self.failed_model = ""
        self.requests_by_model: dict[str, int] = {}
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: ProviderServer

    def log_message(self, *_args: object) -> None:
        pass  # Do not log submitted headers, keys or source content.

    def reply(self, status: int, payload: dict) -> None:
        content = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        self.reply(200 if self.path == "/health" else 404, {"ok": self.path == "/health"})

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 1048576:
            self.reply(413, {"error": "invalid request size"})
            return
        payload = json.loads(self.rfile.read(size))
        if self.path == "/control":
            with self.server.lock:
                self.server.failed_model = str(payload.get("failed_model", ""))
            self.reply(200, {"ok": True})
            return
        if self.path == "/v1/chat/completions":
            if self.headers.get("Authorization") != "Bearer docker-smoke-llm-key":
                self.reply(401, {"error": "invalid fixture LLM key"})
                return
            self.reply(
                200,
                {
                    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
            return
        model = payload.get("model")
        if self.path != "/v1/embeddings" or model not in DIMENSIONS:
            self.reply(404, {"error": "unknown fixture model"})
            return
        if self.headers.get("Authorization", "").removeprefix("Bearer ") not in KEYS:
            self.reply(401, {"error": "invalid fixture key"})
            return
        inputs = payload["input"]
        texts = [inputs] if isinstance(inputs, str) else inputs
        with self.server.lock:
            # Validation succeeds; the injected failure exercises an actual
            # rebuild page rather than being rejected before configuration save.
            failed = model == self.server.failed_model and any(
                text != "Loom provider connection check." for text in texts
            )
            self.server.requests_by_model[model] = self.server.requests_by_model.get(model, 0) + 1
        if failed:
            self.reply(400, {"error": "injected non-retryable provider failure"})
            return
        self.reply(
            200,
            {
                "data": [
                    {"index": index, "embedding": embedding(model, text)}
                    for index, text in enumerate(texts)
                ]
            },
        )


if __name__ == "__main__":
    ProviderServer(("0.0.0.0", 8080)).serve_forever()
