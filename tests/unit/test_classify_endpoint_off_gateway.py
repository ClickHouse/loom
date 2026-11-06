"""A namespace's classify_endpoint, off the inference gateway, reaches only https at a
public address it vetted.

A namespace admin chooses the URL (`namespace.register` makes the creator of a new
namespace its admin), so the server must not post memory content into its own
network, follow a redirect, or report what answered there.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.memory.app import classify
from server.memory.domain.classify_endpoints import classify_refusal
from server.observability import degradations
from server.settings import settings


@pytest.fixture(autouse=True)
def _off_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.llm import customer

    async def _anyone() -> Any:
        return None

    monkeypatch.setattr(settings, "inference_gateway_required", False)
    # An address literal resolves to itself; nothing here reaches a resolver.
    monkeypatch.setattr("server.core.hosts._resolve", lambda host: [host.strip("[]")])
    monkeypatch.setattr(settings, "memory_classify_private_hosts", "")
    monkeypatch.setattr(customer, "require", _anyone)


@pytest.mark.parametrize(
    ("url", "refused"),
    [
        ("https://classifier.example/c", False),
        ("http://classifier.example/c", True),
        ("http://127.0.0.1:8123/?query=SELECT%201", True),
        ("ftp://classifier.example/c", True),
    ],
)
def test_a_classify_endpoint_off_the_gateway_must_be_https(url: str, refused: bool) -> None:
    assert bool(classify_refusal(url)) is refused


def test_an_operators_private_host_may_be_plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_classify_private_hosts", "Classifier.Local")
    assert classify_refusal("http://classifier.local:9000/c") == ""
    assert classify_refusal("http://other.local/c") != ""


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8123/",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/c",
        "https://[::1]/c",
        "https://[::ffff:127.0.0.1]/c",
    ],
)
def test_an_inward_address_is_never_posted_to(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[str] = []

    class _NeverClient:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _NeverClient:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> Any:
            posted.append(url)
            raise AssertionError("posted inward")

    monkeypatch.setattr(classify.httpx, "AsyncClient", _NeverClient)
    label = asyncio.run(
        classify.classify(endpoint=url, prompt_template="", value="v", valid_types=[])
    )
    assert label == "" and posted == []


def test_the_call_goes_to_the_vetted_address_and_reports_only_the_failures_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post goes to the address the host was vetted to, with the name as Host and
    TLS server name, no redirect followed; a failure is noted by its kind alone, never
    its text (a refused port or a status line would map what answers there)."""
    seen: dict[str, Any] = {}

    async def _vetted(url: str, **kw: Any) -> tuple[str, str, str, str]:
        return "", "https://203.0.113.7/c", "classifier.example", "classifier.example"

    class _Client:
        def __init__(self, **kw: Any) -> None:
            seen["follow_redirects"] = kw.get("follow_redirects")

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> Any:
            seen.update(url=url, headers=kw["headers"], extensions=kw["extensions"])
            raise ConnectionRefusedError("All connection attempts failed to 10.1.2.3:8123")

    monkeypatch.setattr("server.core.webhooks.pinned_target", _vetted)
    monkeypatch.setattr(classify.httpx, "AsyncClient", _Client)
    degradations.begin()
    label = asyncio.run(
        classify.classify(
            endpoint="https://classifier.example/c", prompt_template="", value="v", valid_types=[]
        )
    )
    assert label == ""
    assert seen["url"] == "https://203.0.113.7/c" and seen["follow_redirects"] is False
    assert seen["headers"] == {"Host": "classifier.example"}  # no identity headers off it
    assert seen["extensions"] == {"sni_hostname": "classifier.example"}
    notes = degradations.current()
    assert notes == ["classify: caller_type_kept (ConnectionRefusedError)"], notes


def test_a_public_looking_name_that_resolves_inward_is_never_posted_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The realistic attack: a name the admin controls resolves to a private address."""
    posted: list[str] = []

    class _NeverClient:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _NeverClient:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> Any:
            posted.append(url)
            raise AssertionError("posted inward")

    monkeypatch.setattr(
        "server.core.hosts._resolve",
        lambda host: ["10.1.2.3"] if host == "classifier.attacker.example" else [host],
    )
    monkeypatch.setattr(classify.httpx, "AsyncClient", _NeverClient)
    label = asyncio.run(
        classify.classify(
            endpoint="https://classifier.attacker.example/c",
            prompt_template="",
            value="v",
            valid_types=[],
        )
    )
    assert label == "" and posted == []


def test_a_listed_private_host_is_reached_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _Client:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> Any:
            seen.update(url=url, headers=kw["headers"], extensions=kw["extensions"])
            raise ConnectionRefusedError("down")

    monkeypatch.setattr(settings, "memory_classify_private_hosts", "classifier.local")
    monkeypatch.setattr("server.core.hosts._resolve", lambda host: ["10.0.0.9"])
    monkeypatch.setattr(classify.httpx, "AsyncClient", _Client)
    asyncio.run(
        classify.classify(
            endpoint="http://classifier.local:9000/c", prompt_template="", value="v", valid_types=[]
        )
    )
    assert seen == {"url": "http://classifier.local:9000/c", "headers": {}, "extensions": {}}


def test_a_lookup_that_never_answers_is_cut_by_the_calls_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    def _hangs(host: str) -> list[str]:
        time.sleep(2)
        return ["203.0.113.7"]

    monkeypatch.setattr(settings, "memory_classify_timeout_s", 0.2)
    monkeypatch.setattr("server.core.hosts._resolve", _hangs)
    async def _timed() -> tuple[str, float]:
        started = time.perf_counter()
        label = await classify.classify(
            endpoint="https://slow.example/c", prompt_template="", value="v", valid_types=[]
        )
        return label, time.perf_counter() - started

    # timed inside the loop: the resolver's thread finishes on its own after the deadline
    label, waited = asyncio.run(_timed())
    assert label == "" and waited < 1.5


def test_registration_refuses_plain_http_off_the_gateway_without_naming_the_gateway() -> None:
    from server.memory.domain.classify_endpoints import check_classify_endpoint

    with pytest.raises(ValueError) as e:
        check_classify_endpoint("http://classifier.example/c")
    assert "must be https" in str(e.value) and "gateway" not in str(e.value)
