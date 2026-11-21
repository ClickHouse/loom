"""Exercise Dify's import order and overlapping calls in a fresh interpreter."""

import dify_plugin  # noqa: F401 — patch threading before importing the SDK and executor

# isort: split

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gevent import get_hub
from gevent.event import Event
from gevent.pool import Pool
from gevent.pywsgi import WSGIServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider.loom import LoomProvider
from tools.recall import RecallTool
from tools.remember import RememberTool
from tools.search_knowledge import SearchKnowledgeTool

SECOND = sys.argv[1]
PATHS = {
    "recall": "/v1/memory.search",
    "remember": "/v1/memory.set",
    "knowledge": "/v1/knowledge.search",
    "credentials": "/v1/memory.list",
}
assert SECOND in PATHS
calls = []
first_entered, both_entered, release = Event(), Event(), Event()
hold = False


def marker(body, path):
    return f"{body['org']}:{body.get('user_id', '-')}:{path}"


def application(environ, start_response):
    path = environ["PATH_INFO"]
    body = json.loads(environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"])))
    calls.append((path, body, environ.get("HTTP_AUTHORIZATION")))
    if hold:
        first_entered.set()
        if len(calls) == 2:
            both_entered.set()
        assert release.wait(timeout=15), "Held HTTP response was not released"
    refused = body.get("user_id") == "refused"
    reply = (
        {"detail": "first-native-test-token"}
        if refused
        else {"stored": True, "marker": marker(body, path)}
        if path == "/v1/memory.set"
        else {"entries": [], "truncated": False, "marker": marker(body, path)}
        if path == "/v1/memory.list"
        else {"results": [], "marker": marker(body, path)}
    )
    start_response("403 Forbidden" if refused else "200 OK", [("Content-Type", "application/json")])
    return [json.dumps(reply).encode()]


def invoke(kind, credentials, user_id):
    if kind == "credentials":
        LoomProvider().validate_credentials(credentials)
        return None
    cls = {"recall": RecallTool, "remember": RememberTool, "knowledge": SearchKnowledgeTool}[kind]
    parameters = (
        {"user_id": user_id, "memory_key": "seat", "value": "aisle"}
        if kind == "remember"
        else {"user_id": user_id, "query": "seat", "top_k": 3}
    )
    messages = list(cls.from_credentials(credentials).invoke(parameters))
    assert len(messages) == 1
    return messages[0].message.json_object


server = WSGIServer(("127.0.0.1", 0), application, log=None, spawn=Pool(4))
pool = None
server.start()
first = {
    "base_url": f"http://127.0.0.1:{server.server_port}",
    "api_key": "first-native-test-token",
    "org": "first-org",
    "namespace": "first-namespace",
    "agent": "first-agent",
}
second = dict(
    first,
    api_key="second-native-test-token",
    org="second-org",
    namespace="second-namespace",
    agent="second-agent",
)
try:
    assert invoke("recall", first, "serial") == {
        "results": [],
        "marker": "first-org:serial:/v1/memory.search",
    }
    print("SERIAL_NATIVE_IMPORT_PASS", flush=True)
    calls.clear()
    hold = True
    pool = ThreadPoolExecutor(max_workers=2)
    futures = [pool.submit(invoke, "recall", first, "alice")]
    entered = first_entered.wait(timeout=5)
    overlap = False
    try:
        if entered:
            futures.append(pool.submit(invoke, SECOND, second, "bob"))
            overlap = both_entered.wait(timeout=3)
    finally:
        release.set()
    results, errors = [], []
    for future in futures:
        try:
            results.append(future.result(timeout=25))
        except Exception as exc:  # noqa: BLE001 — report every invocation before asserting
            errors.append({"type": type(exc).__name__, "message": str(exc)})
    print(
        json.dumps(
            {
                "case": SECOND,
                "entered": entered,
                "overlap": overlap,
                "calls": calls,
                "results": results,
                "errors": errors,
            }
        ),
        flush=True,
    )
    assert entered and overlap and not errors and len(calls) == len(results) == 2
    assert [path for path, _, _ in calls] == ["/v1/memory.search", PATHS[SECOND]]
    for (path, body, authorization), credentials, user_id in zip(
        calls, [first, second], ["alice", "-" if SECOND == "credentials" else "bob"], strict=True
    ):
        assert authorization == "Bearer " + credentials["api_key"]
        assert body["org"] == credentials["org"]
        assert body["namespace"] == credentials["namespace"]
        assert body["user_id"] == user_id
        assert body["agent"] == ("*" if path == "/v1/memory.search" else credentials["agent"])
        if path.endswith("search"):
            assert body["namespace_scope"] == "this" and body["top_k"] == 3
        if path == "/v1/memory.search":
            assert body["bodies"] == "full"
        elif path == "/v1/memory.list":
            assert body["limit"] == 1
    expected_second = (
        None
        if SECOND == "credentials"
        else {"stored": True, "marker": "second-org:bob:" + PATHS[SECOND]}
        if SECOND == "remember"
        else {"results": [], "marker": "second-org:bob:" + PATHS[SECOND]}
    )
    assert results == [
        {"results": [], "marker": "first-org:alice:/v1/memory.search"},
        expected_second,
    ]
    hold = False
    calls.clear()
    try:
        invoke("recall", first, "refused")
    except ValueError as exc:
        assert "Loom request failed" in str(exc) and first["api_key"] not in str(exc)
    else:
        raise AssertionError("A server refusal must fail the invocation")
    assert invoke("recall", first, "after-failure") == {
        "results": [],
        "marker": "first-org:after-failure:/v1/memory.search",
    }
    assert [body["user_id"] for _, body, _ in calls] == ["refused", "after-failure"]
finally:
    release.set()
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)
    server.stop(timeout=1)
    # This isolated child owns its hub; drain SDK work before ending the process.
    native_pool = get_hub().threadpool
    native_pool.join()
    native_pool.kill()

print("NATIVE_CONCURRENCY_PASS " + SECOND, flush=True)
