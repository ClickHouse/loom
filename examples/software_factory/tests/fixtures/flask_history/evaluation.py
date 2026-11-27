"""Trusted independent checks; never include this payload in public/native history."""

from __future__ import annotations

import ast
import difflib

from examples.software_factory.factory.model import digest

from .fixtures import _case

HEADER_CHECKS = """from flask import Flask
from werkzeug.datastructures import Headers, MultiDict


def test_incoming_multivalue_headers_replace_old_values_without_loss():
    app = Flask(__name__)
    for incoming in (Headers, MultiDict):
        response = app.response_class("payload", status=202)
        response.headers.add("Set-Cookie", "old=0")
        headers = incoming([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("X-Trace", "ok")])
        with app.app_context():
            result = app.make_response((response, 207, headers))
        assert result.headers.getlist("Set-Cookie") == ["a=1", "b=2"]
        assert result.headers["X-Trace"] == "ok"
        assert result.get_data() == b"payload"
        assert result.status_code == 207
        assert result is response


def test_header_compatibility_and_streaming_are_preserved():
    app = Flask(__name__)
    class CustomResponse(app.response_class):
        pass
    for headers in ({"X-Only": "one"}, [("X-Only", "one")], Headers([("X-Only", "one")])):
        response = CustomResponse(iter(["part1", "part2"]), status=206)
        with app.app_context():
            result = app.make_response((response, headers))
        assert result is response
        assert result.is_streamed
        assert result.status_code == 206
        assert result.headers["X-Only"] == "one"
        assert result.get_data() == b"part1part2"
"""

CACHE_CHECKS = """from datetime import timedelta
from flask import Blueprint, Flask


def test_zero_none_positive_and_timedelta_cache_ages():
    app = Flask(__name__)
    blueprint = Blueprint("cache_policy", __name__)
    cases = ((0, 0), (None, None), (27, 27), (timedelta(0), 0), (timedelta(seconds=91), 91))
    for configured, expected in cases:
        app.config["SEND_FILE_MAX_AGE_DEFAULT"] = configured
        with app.app_context():
            assert app.get_send_file_max_age("asset.txt") == expected
            assert blueprint.get_send_file_max_age("asset.txt") == expected


def test_cache_age_is_resolved_from_each_current_application():
    first, second = Flask("first_cache"), Flask("second_cache")
    first.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    second.config["SEND_FILE_MAX_AGE_DEFAULT"] = 77
    for app, expected in ((first, 0), (second, 77), (first, 0)):
        with app.app_context():
            assert app.get_send_file_max_age(None) == expected
"""

JSON_CHECKS = """from flask import Flask, json, jsonify


def test_json_helper_uses_each_current_application_options():
    payload = {"b": "é", "a": 1}
    apps = []
    for i, (sort, ascii_only) in enumerate(((False, False), (True, True), (False, True))):
        app = Flask("json_context_" + str(i))
        app.json.sort_keys = sort
        app.json.ensure_ascii = ascii_only
        apps.append((app, sort, ascii_only))
    for app, sort, ascii_only in apps + apps[:1]:
        with app.app_context():
            helper = json.dumps(payload)
            direct = app.json.dumps(payload)
            assert helper == direct
            assert (helper.index('"a"') < helper.index('"b"')) is sort
            assert ("\\\\u00e9" in helper) is ascii_only
            assert json.loads(jsonify(payload).get_data(as_text=True)) == payload


def test_explicit_json_options_and_no_context_fallback():
    payload = {"b": "é", "a": 1}
    app = Flask("json_explicit")
    app.json.sort_keys = True
    app.json.ensure_ascii = True
    with app.app_context():
        explicit = json.dumps(payload, sort_keys=False, ensure_ascii=False)
        assert explicit.index('"b"') < explicit.index('"a"')
        assert "é" in explicit
    outside = json.dumps(payload)
    assert outside.index('"b"') < outside.index('"a"')
    assert "\\\\u00e9" in outside
"""

CHECKS = {
    "flask-memory-response-headers": HEADER_CHECKS,
    "flask-memory-cache-policy": CACHE_CHECKS,
    "flask-memory-json-context": JSON_CHECKS,
}


def evaluator_spec(case_id: str) -> dict:
    _case(case_id)
    source = CHECKS[case_id]
    path = "tests/test_simulated_memory_contract.py"
    selectors = [
        path + "::" + node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
    ]
    patch = "".join(
        difflib.unified_diff(
            [], source.splitlines(keepends=True), fromfile="/dev/null", tofile="b/" + path
        )
    )
    return dict(
        test_path=path,
        test_source=source,
        test_patch=patch,
        hidden_selectors=selectors,
        evaluator_sha256=digest(dict(source=source, selectors=selectors)),
        visibility="evaluator-private",
    )


def reference_sources(case_id: str, original_base_source_files: dict[str, str]) -> dict[str, str]:
    manifest, _ = _case(case_id)
    if digest(original_base_source_files) != manifest["base_source_sha256"]:
        raise PermissionError("Reference control requires the original pinned public source")
    return dict(original_base_source_files)
