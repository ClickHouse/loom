"""A namespace's classify_endpoint, off the inference gateway, reaches only https at a
public address it vetted.

A namespace admin chooses the URL (`namespace.register` makes the creator of a new
namespace its admin), so the server must not post memory content into its own
network, follow a redirect, wait past the classifier's deadline, read an answer of
any size, or report what answered there.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from server.memory.app import classify
from server.memory.domain.classify_endpoints import check_classify_endpoint, classify_refusal
from server.observability import degradations
from server.settings import settings


@pytest.fixture(autouse=True)
def _off_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.llm import customer

    async def _anyone() -> Any:
        return None

    monkeypatch.setattr(settings, "inference_gateway_required", False)
    monkeypatch.setattr(settings, "memory_classify_private_hosts", "")
    # An address literal resolves to itself; nothing here reaches a resolver.
    monkeypatch.setattr("server.core.hosts._resolve", lambda host: [host.strip("[]")])
    monkeypatch.setattr(customer, "require", _anyone)


class _Answer:
    def __init__(self, chunks: list[bytes], status: int = 200) -> None:
        self.chunks, self.status_code = chunks, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class _Client:
    """The off-gateway client: records each request; `answer` decides what it gets."""

    seen: list[dict[str, Any]] = []
    answer: Callable[[], _Answer] = staticmethod(lambda: _Answer([b'{"memory_type": "note"}']))

    def __init__(self, **kw: Any) -> None:
        self.follow_redirects = kw.get("follow_redirects")

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def stream(self, method: str, url: str, **kw: Any) -> _Stream:
        _Client.seen.append({"url": url, "follow_redirects": self.follow_redirects, **kw})
        return _Stream(_Client.answer())


class _Stream:
    def __init__(self, answer: _Answer) -> None:
        self.answer = answer

    async def __aenter__(self) -> _Answer:
        return self.answer

    async def __aexit__(self, *a: Any) -> None:
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> type[_Client]:
    _Client.seen = []
    _Client.answer = staticmethod(lambda: _Answer([b'{"memory_type": "note"}']))
    monkeypatch.setattr(classify.httpx, "AsyncClient", _Client)
    return _Client


@pytest.fixture
def vetted(monkeypatch: pytest.MonkeyPatch) -> None:
    """classifier.example resolves to a public address."""

    async def _vetted(url: str, **kw: Any) -> tuple[str, str, str, str]:
        return "", "https://203.0.113.7/c", "classifier.example", "classifier.example"

    monkeypatch.setattr("server.core.webhooks.pinned_target", _vetted)


def _classify(endpoint: str) -> str:
    return asyncio.run(
        classify.classify(endpoint=endpoint, prompt_template="", value="v", valid_types=[])
    )


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


def test_registration_refuses_plain_http_off_the_gateway_without_naming_the_gateway() -> None:
    with pytest.raises(ValueError) as e:
        check_classify_endpoint("http://classifier.example/c")
    assert "must be https" in str(e.value) and "gateway" not in str(e.value)


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
def test_an_inward_address_is_never_posted_to(url: str, client: type[_Client]) -> None:
    degradations.begin()
    assert _classify(url) == "" and client.seen == []
    assert degradations.current() == ["classify: caller_type_kept (endpoint host refused)"]


def test_a_public_looking_name_that_resolves_inward_is_never_posted_to(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    """The realistic attack: a name the admin controls resolves to a private address."""
    monkeypatch.setattr(
        "server.core.hosts._resolve",
        lambda host: ["10.1.2.3"] if host == "classifier.attacker.example" else [host],
    )
    degradations.begin()
    assert _classify("https://classifier.attacker.example/c") == "" and client.seen == []
    assert degradations.current() == ["classify: caller_type_kept (endpoint host refused)"]


def test_the_call_goes_to_the_vetted_address_and_reports_only_the_failures_kind(
    vetted: None, client: type[_Client]
) -> None:
    """The post goes to the address the host was vetted to, with the name as Host and
    TLS server name and no redirect followed; a failure is noted by its kind alone,
    never its text (a refused port or a status line would map what answers there)."""

    def _refused() -> _Answer:
        raise ConnectionRefusedError("All connection attempts failed to a host:8123")

    client.answer = staticmethod(_refused)
    degradations.begin()
    assert _classify("https://classifier.example/c") == ""
    (seen,) = client.seen
    assert seen["url"] == "https://203.0.113.7/c" and seen["follow_redirects"] is False
    assert seen["headers"] == {"Host": "classifier.example"}  # no identity headers off it
    assert seen["extensions"] == {"sni_hostname": "classifier.example"}
    assert degradations.current() == ["classify: caller_type_kept (ConnectionRefusedError)"]


def test_a_listed_private_host_is_reached_by_name(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    monkeypatch.setattr(settings, "memory_classify_private_hosts", "classifier.local")
    monkeypatch.setattr("server.core.hosts._resolve", lambda host: ["10.0.0.9"])
    assert _classify("http://classifier.local:9000/c") == "note"
    (seen,) = client.seen
    assert seen["url"] == "http://classifier.local:9000/c"
    assert seen["headers"] == {} and seen["extensions"] == {}


def test_a_lookup_that_never_answers_is_cut_by_the_calls_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_an_answer_that_trickles_is_cut_by_the_calls_deadline(
    monkeypatch: pytest.MonkeyPatch, vetted: None, client: type[_Client]
) -> None:
    class _Trickle(_Answer):
        async def aiter_bytes(self) -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(0.05)
                yield b" "

    monkeypatch.setattr(settings, "memory_classify_timeout_s", 0.3)
    client.answer = staticmethod(lambda: _Trickle([]))
    degradations.begin()
    started = time.perf_counter()
    assert _classify("https://classifier.example/c") == ""
    assert time.perf_counter() - started < 2.0
    assert degradations.current() == ["classify: caller_type_kept (TimeoutError)"]


def test_an_answer_over_the_cap_is_not_read_whole(vetted: None, client: type[_Client]) -> None:
    client.answer = staticmethod(lambda: _Answer([b" " * 32 * 1024] * 4))
    degradations.begin()
    assert _classify("https://classifier.example/c") == ""
    assert degradations.current() == ["classify: caller_type_kept (ValueError)"]
