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
from pathlib import Path
from typing import Any

import pytest

from server.core.hosts import _resolve_in_loop as _REAL_RESOLVE_IN_LOOP
from server.memory.app import classify
from server.memory.domain.classify_endpoints import check_classify_endpoint, classify_refusal
from server.observability import degradations
from server.settings import settings
from tests.unit._hosts import resolves


def _no_thread(host: str) -> list[str]:
    raise AssertionError(f"{host} was looked up in a thread")


@pytest.fixture(autouse=True)
def _off_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.llm import customer

    async def _anyone() -> Any:
        return None

    monkeypatch.setattr(settings, "inference_gateway_required", False)
    monkeypatch.setattr(settings, "memory_classify_private_hosts", "")
    # An address literal resolves to itself; nothing here reaches a resolver.
    resolves(monkeypatch, lambda host: [host.strip("[]")])
    monkeypatch.setattr(customer, "require", _anyone)


class _Answer:
    def __init__(self, chunks: list[bytes], status: int = 200) -> None:
        self.chunks, self.status_code = chunks, status
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    async def aiter_raw(self) -> AsyncIterator[bytes]:
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
        classify.classify(
            org="acme",
            namespace="ns",
            endpoint=endpoint,
            prompt_template="",
            value="v",
            valid_types=[],
        )
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
    resolves(
        monkeypatch,
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
    # no identity headers off the gateway, and no compressed answer asked for
    assert seen["headers"] == {"Accept-Encoding": "identity", "Host": "classifier.example"}
    assert seen["extensions"] == {"sni_hostname": "classifier.example"}
    assert degradations.current() == ["classify: caller_type_kept (ConnectionRefusedError)"]


def test_a_listed_private_host_is_reached_by_name(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    monkeypatch.setattr(settings, "memory_classify_private_hosts", "classifier.local")
    resolves(monkeypatch, lambda host: ["10.0.0.9"])
    assert _classify("http://classifier.local:9000/c") == "note"
    (seen,) = client.seen
    assert seen["url"] == "http://classifier.local:9000/c"
    assert seen["headers"] == {"Accept-Encoding": "identity"} and seen["extensions"] == {}


def test_a_lookup_that_never_answers_is_cut_by_the_calls_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lookup runs on the event loop: the deadline ends it, and nothing of it is
    left running (no thread, no task) once the call has returned."""
    running: list[str] = []

    async def _hangs(host: str) -> list[str]:
        running.append(host)
        try:
            await asyncio.sleep(30)
        finally:
            running.remove(host)
        return ["93.184.215.14"]

    monkeypatch.setattr(settings, "memory_classify_timeout_s", 0.2)
    monkeypatch.setattr("server.core.hosts._resolve_in_loop", _hangs)
    monkeypatch.setattr("server.core.hosts._resolve", _no_thread)

    async def _timed() -> tuple[str, float, list[str]]:
        started = time.perf_counter()
        label = await classify.classify(
            org="acme",
            namespace="ns",
            endpoint="https://slow.example/c",
            prompt_template="",
            value="v",
            valid_types=[],
        )
        return label, time.perf_counter() - started, list(running)

    label, waited, left = asyncio.run(_timed())
    assert label == "" and waited < 1.5 and left == []


def test_an_answer_that_trickles_is_cut_by_the_calls_deadline(
    monkeypatch: pytest.MonkeyPatch, vetted: None, client: type[_Client]
) -> None:
    class _Trickle(_Answer):
        async def aiter_raw(self) -> AsyncIterator[bytes]:
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


@pytest.mark.parametrize("encoding", ["gzip", "gzip, gzip", "deflate"])
def test_a_compressed_answer_is_refused_before_it_inflates(
    monkeypatch: pytest.MonkeyPatch, vetted: None, encoding: str
) -> None:
    """Through httpx's own response handling: a compressed answer would inflate in one
    step past any cap, so it is refused unread, whatever it holds."""
    import gzip

    import httpx

    bomb = gzip.compress(gzip.compress(b" " * (8 * 1024 * 1024)))
    asked: list[str] = []

    class _Wire(httpx.AsyncByteStream):  # as a network answer arrives: unread until read
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield bomb

    def _answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers.get("accept-encoding", ""))
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=_Wire())

    real = httpx.AsyncClient
    monkeypatch.setattr(
        classify.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(_answer), follow_redirects=False),
    )
    degradations.begin()
    assert _classify("https://classifier.example/c") == ""
    assert asked == ["identity"]
    assert degradations.current() == ["classify: caller_type_kept (ValueError)"]


def test_a_plain_answer_through_httpx_is_read(
    monkeypatch: pytest.MonkeyPatch, vetted: None
) -> None:
    import httpx

    class _Wire(httpx.AsyncByteStream):  # as a network answer arrives
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"memory_type": "decision"}'

    def _answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["host"] == "classifier.example"
        return httpx.Response(200, stream=_Wire())

    real = httpx.AsyncClient
    monkeypatch.setattr(
        classify.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(_answer), follow_redirects=False),
    )
    assert _classify("https://classifier.example/c") == "decision"


@pytest.mark.parametrize("url", ["https:///c", "https://", "https://classifier.example:99999/c"])
def test_an_endpoint_the_call_could_never_reach_is_refused_at_registration(url: str) -> None:
    assert classify_refusal(url) != ""


def test_registration_stores_the_endpoint_as_the_call_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "memory_classify_private_hosts", "classifier.local")
    assert check_classify_endpoint(" http://classifier.local:9000/c ") == (
        "http://classifier.local:9000/c"
    )


def test_a_listed_gateway_host_is_called_as_a_model_call_though_not_required(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    """With gateway hosts listed but the gateway not required, a classifier on one of
    them still goes through the model client, with its identity headers, as before."""
    from server.clients import _http

    posted: list[str] = []

    class _ModelClient:
        async def post(self, url: str, **kw: Any) -> Any:
            posted.append(url)
            raise ConnectionRefusedError("down")

    from server.llm import customer

    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(_http, "get_model_client", lambda: _ModelClient())
    monkeypatch.setattr(customer, "headers", lambda who: {})
    assert _classify("https://gateway.example/classify") == ""
    assert posted == ["https://gateway.example/classify"] and client.seen == []


def test_a_redirect_is_not_followed(monkeypatch: pytest.MonkeyPatch, vetted: None) -> None:
    import httpx

    asked: list[str] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8123/"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        classify.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(_answer), **kw),
    )
    degradations.begin()
    assert _classify("https://classifier.example/c") == ""
    assert asked == ["https://203.0.113.7/c"]
    assert degradations.current() == ["classify: caller_type_kept (HTTPStatusError)"]


def test_a_listed_gateway_host_off_its_default_port_is_refused_though_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another port of the gateway's host is inward reach, whether or not the gateway is
    required, and listing the host as private does not open it."""
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "memory_classify_private_hosts", "gateway.example")
    assert classify_refusal("http://gateway.example:8080/c") != ""
    assert classify_refusal("https://gateway.example:8443/c") != ""
    assert classify_refusal("https://gateway.example/c") == ""
    # an explicit default port: urlsplit reads 443, httpx (which sends it) no port
    assert classify_refusal("https://gateway.example:443/c") == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@classifier.example/c",
        "https://token@classifier.example/c",
        "https://:secret@classifier.example/c",
    ],
)
def test_a_url_carrying_a_user_name_or_password_is_refused(url: str) -> None:
    """The call would drop them and namespace.get would hand the credential back."""
    assert classify_refusal(url) == "the URL may not carry a user name or password"


def test_an_internationalized_host_is_posted_with_its_idna_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    hosts: list[str] = []

    class _Wire(httpx.AsyncByteStream):  # as a network answer arrives
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"memory_type": "note"}'

    def _answer(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        return httpx.Response(200, stream=_Wire())

    real = httpx.AsyncClient
    resolves(monkeypatch, lambda host: ["93.184.215.14"])
    monkeypatch.setattr(
        classify.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(_answer), follow_redirects=False),
    )
    assert _classify("https://bücher.example/c") == "note"
    assert hosts == ["xn--bcher-kva.example"]


def test_a_refused_host_is_logged_once_per_namespace(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client], caplog: pytest.LogCaptureFixture
) -> None:
    from server.memory.app.log_once import LogOnce

    monkeypatch.setattr(classify, "_REFUSED_LOGGED", LogOnce())

    async def _calls() -> list[str]:
        return [
            await classify.classify(
                org="acme",
                namespace=ns,
                endpoint="https://10.0.0.5/c",
                prompt_template="",
                value="v",
                valid_types=[],
            )
            for ns in ("a", "a", "a", "b")
        ]

    assert asyncio.run(_calls()) == ["", "", "", ""] and client.seen == []
    warned = [r.getMessage() for r in caplog.records if "host is refused" in r.getMessage()]
    assert len(warned) == 2
    assert warned[0].startswith("acme/a:") and warned[1].startswith("acme/b:")


@pytest.mark.parametrize(
    "url",
    [
        "https://gate\tway.example:8443/c",  # urlsplit drops the tab; httpx refuses it
        "https://gateway.example:8443/c#\x01",
        "https://gateway.example:8443/c#" + "a" * 70000,  # past httpx's URL length
        "https://ｇａｔｅｗａｙ.example:8443/c",  # fullwidth: IDNA maps it to the gateway's host
    ],
)
def test_a_url_two_parsers_read_apart_is_refused_and_never_posted(
    monkeypatch: pytest.MonkeyPatch, url: str, client: type[_Client]
) -> None:
    """httpx (the gateway check) and urlsplit (the call) must read one host: each of
    these reads as off the gateway to one and reaches the gateway's host by the other."""
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    resolves(monkeypatch, lambda host: ["93.184.215.14"])
    with pytest.raises(ValueError, match="classify_endpoint"):
        check_classify_endpoint(url)
    assert _classify(url) == "" and client.seen == []


def test_a_gateway_url_carrying_a_user_name_or_password_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    assert classify_refusal("https://gateway.example/c") == ""
    assert classify_refusal("https://user:pw@gateway.example/c") == (
        "the URL may not carry a user name or password"
    )


@pytest.mark.parametrize(
    ("listed", "url"),
    [
        ("xn--bcher-kva.local", "http://bücher.local:9000/c"),
        ("bücher.local", "http://xn--bcher-kva.local:9000/c"),
    ],
)
def test_a_private_host_matches_in_either_spelling(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client], listed: str, url: str
) -> None:
    monkeypatch.setattr(settings, "memory_classify_private_hosts", listed)
    resolves(monkeypatch, lambda host: ["10.0.0.9"])
    assert classify_refusal(url) == ""
    assert _classify(url) == "note"
    (seen,) = client.seen
    assert seen["url"] == "http://xn--bcher-kva.local:9000/c"


def test_a_null_answer_is_noted_as_malformed_not_as_a_refused_host(
    vetted: None, client: type[_Client]
) -> None:
    client.answer = staticmethod(lambda: _Answer([b"null"]))
    degradations.begin()
    assert _classify("https://classifier.example/c") == ""
    assert len(client.seen) == 1
    assert degradations.current() == ["classify: caller_type_kept (malformed payload)"]


@pytest.mark.parametrize(
    "url",
    [
        "https://[fe80::1%25eth0]/c",  # a zone id
        "https://gate%77ay.example/c",  # a percent-encoded name
        "https://ｇａｔｅｗａｙ.example/c",  # fullwidth, at the default port
    ],
)
def test_a_host_that_is_not_a_bare_name_or_address_is_refused_and_never_posted(
    monkeypatch: pytest.MonkeyPatch, url: str, client: type[_Client]
) -> None:
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    resolves(monkeypatch, lambda host: ["93.184.215.14"])
    assert classify_refusal(url) == "it is not a valid URL"
    assert _classify(url) == "" and client.seen == []


def test_a_private_host_with_an_underscore_is_accepted(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    """A Compose service name: both parsers read the `_` alike."""
    monkeypatch.setattr(settings, "memory_classify_private_hosts", "my_classifier")
    resolves(monkeypatch, lambda host: ["172.18.0.4"])
    assert classify_refusal("http://my_classifier:8080/c") == ""
    assert _classify("http://my_classifier:8080/c") == "note"
    assert client.seen[0]["url"] == "http://my_classifier:8080/c"


def test_the_gateways_explicit_default_port_is_called_as_a_model_call(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    from server.clients import _http
    from server.llm import customer

    posted: list[str] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"memory_type": "note"}

    class _ModelClient:
        async def post(self, url: str, **kw: Any) -> _Resp:
            posted.append(url)
            return _Resp()

    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(_http, "get_model_client", lambda: _ModelClient())
    monkeypatch.setattr(customer, "headers", lambda who: {})
    assert _classify("https://gateway.example:443/c") == "note"
    assert posted == ["https://gateway.example:443/c"] and client.seen == []


def test_namespaces_whose_names_never_answer_leave_another_namespaces_call_alone(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    """Two namespaces burst at names that never answer: each call ends at its deadline
    with nothing of its lookup left running, and a third namespace's classifier
    answers meanwhile: no lookup holds a thread another namespace needs."""
    running: list[str] = []

    async def _lookup(host: str) -> list[str]:
        if host.startswith("never"):
            running.append(host)
            try:
                await asyncio.sleep(30)
            finally:
                running.remove(host)
        return ["93.184.215.14"]

    monkeypatch.setattr(settings, "memory_classify_timeout_s", 0.3)
    monkeypatch.setattr("server.core.hosts._resolve_in_loop", _lookup)
    monkeypatch.setattr("server.core.hosts._resolve", _no_thread)

    async def _call(namespace: str, host: str) -> str:
        return await classify.classify(
            org="acme",
            namespace=namespace,
            endpoint=f"https://{host}/c",
            prompt_template="",
            value="v",
            valid_types=[],
        )

    async def _all() -> tuple[list[str], list[str]]:
        hogs = [_call(ns, f"never{i}.example") for ns in ("hog-a", "hog-b") for i in range(8)]
        labels = await asyncio.gather(*hogs, _call("other", "classifier.example"))
        return labels, list(running)

    labels, left = asyncio.run(_all())
    *hogs, other = labels
    assert other == "note" and hogs == [""] * 16
    assert left == [] and [s["url"] for s in client.seen] == ["https://93.184.215.14/c"]


def test_the_in_loop_lookup_reads_a_and_aaaa_and_fails_only_when_both_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dns.resolver

    from server.core import hosts

    # the real _resolve_in_loop (the autouse fixture stubs it), over a fake nameserver
    monkeypatch.setattr(hosts, "_resolve_in_loop", _REAL_RESOLVE_IN_LOOP)

    class _Record:
        def __init__(self, text: str) -> None:
            self.text = text

        def to_text(self) -> str:
            return self.text

    class _Resolver:
        """As dnspython answers: a name it knows with no record of a kind is an empty
        answer (NOERROR, no data: NoAnswer, not raised), one it does not is NXDOMAIN."""

        def __init__(self, zone: dict[tuple[str, str], list[str]]) -> None:
            self.zone = zone

        async def resolve(self, host: str, kind: str, *, raise_on_no_answer: bool) -> Any:
            if (host, kind) == ("slow6.example", "AAAA"):
                raise dns.resolver.LifetimeTimeout(timeout=1.0, errors=[])
            if not any(name == host for name, _ in self.zone):
                raise dns.resolver.NXDOMAIN
            records = [_Record(a) for a in self.zone.get((host, kind), [])]
            if not records and raise_on_no_answer:
                raise dns.resolver.NoAnswer
            return records

    zone = {
        ("both.example", "A"): ["93.184.215.14"],
        ("both.example", "AAAA"): ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
        ("v4.example", "A"): ["93.184.215.14"],
        ("v6.example", "AAAA"): ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
        ("slow6.example", "A"): ["93.184.215.14"],
        ("slow6.example", "AAAA"): [],  # this kind's query fails: see _Resolver.resolve
    }
    monkeypatch.setattr(hosts, "_async_resolver", lambda: _Resolver(zone))
    assert asyncio.run(hosts._resolve_in_loop("both.example")) == [
        "93.184.215.14",
        "2606:2800:21f:cb07:6820:80da:af6b:8b2c",
    ]
    assert asyncio.run(hosts._resolve_in_loop("v4.example")) == ["93.184.215.14"]
    assert asyncio.run(hosts._resolve_in_loop("v6.example")) == [
        "2606:2800:21f:cb07:6820:80da:af6b:8b2c"
    ]
    # one kind's query failing (a timeout, a SERVFAIL) leaves the other's answer
    assert asyncio.run(hosts._resolve_in_loop("slow6.example")) == ["93.184.215.14"]
    assert asyncio.run(hosts.vet_host("slow6.example")) == ("", "93.184.215.14")
    assert asyncio.run(hosts._resolve_in_loop("10.0.0.5")) == ["10.0.0.5"]  # a literal
    with pytest.raises(dns.resolver.NXDOMAIN):
        asyncio.run(hosts._resolve_in_loop("none.example"))
    refusal, _ = asyncio.run(hosts.vet_host("none.example"))
    assert refusal == "none.example does not resolve (NXDOMAIN)"


class _Nameserver(asyncio.DatagramProtocol):
    """A real nameserver on a local UDP socket: v4.example has an A record and no
    AAAA (NOERROR, no data), a name under never.example is never answered, any
    other is NXDOMAIN."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        import dns.message
        import dns.rcode
        import dns.rdatatype
        import dns.rrset

        query = dns.message.from_wire(data)
        question = query.question[0]
        name = question.name.to_text()
        self.asked.append(name)
        if name.endswith("never.example."):
            return
        answer = dns.message.make_response(query)
        if name == "v4.example.":
            if question.rdtype == dns.rdatatype.A:
                answer.answer.append(dns.rrset.from_text(name, 60, "IN", "A", "93.184.215.14"))
        else:
            answer.set_rcode(dns.rcode.NXDOMAIN)
        assert self.transport is not None
        self.transport.sendto(answer.to_wire(), addr)


async def _with_nameserver(
    monkeypatch: pytest.MonkeyPatch, lifetime: float, body: Callable[[], Any]
) -> tuple[Any, _Nameserver, set[str], set[str]]:
    """Run `body` with the in-loop lookup asking a real resolver at a local nameserver;
    also the names of the threads before and after it."""
    import threading

    import dns.asyncresolver

    from server.core import hosts

    loop = asyncio.get_running_loop()
    transport, server = await loop.create_datagram_endpoint(
        _Nameserver, local_addr=("127.0.0.1", 0)
    )
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = ["127.0.0.1"]
    resolver.port = transport.get_extra_info("sockname")[1]
    resolver.lifetime = lifetime
    monkeypatch.setattr(hosts, "_resolve_in_loop", _REAL_RESOLVE_IN_LOOP)
    monkeypatch.setattr(hosts, "_async_resolver", lambda: resolver)
    monkeypatch.setattr(hosts, "_resolve", _no_thread)
    before = {t.name for t in threading.enumerate()}
    try:
        result = await body()
    finally:
        transport.close()
    return result, server, before, {t.name for t in threading.enumerate()}


def test_a_real_lookup_reads_an_answer_with_no_aaaa_and_an_nxdomain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import hosts

    async def _both() -> tuple[tuple[str, str], tuple[str, str]]:
        return await hosts.vet_host("v4.example"), await hosts.vet_host("none.example")

    (found, missing), server, _, _ = asyncio.run(_with_nameserver(monkeypatch, 5.0, _both))
    assert found == ("", "93.184.215.14")
    assert missing == ("none.example does not resolve (NXDOMAIN)", "")
    assert sorted(set(server.asked)) == ["none.example.", "v4.example."]


def test_a_real_lookup_that_never_answers_ends_at_the_deadline_without_a_thread(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    """dnspython waits on the event loop: the classifier's deadline, shorter than the
    resolver's own lifetime, ends the lookup, and no thread was used or is left."""
    monkeypatch.setattr(settings, "memory_classify_timeout_s", 0.3)

    async def _timed() -> tuple[str, float]:
        started = time.perf_counter()
        label = await _call_never()
        return label, time.perf_counter() - started

    (label, waited), server, before, after = asyncio.run(
        _with_nameserver(monkeypatch, 30.0, _timed)
    )
    assert label == "" and waited < 1.5 and client.seen == []
    assert "never.example." in server.asked and after == before


def test_the_resolvers_own_lifetime_ends_a_lookup_before_a_longer_deadline(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    monkeypatch.setattr(settings, "memory_classify_timeout_s", 5.0)

    async def _timed() -> tuple[str, float, list[str]]:
        degradations.begin()
        started = time.perf_counter()
        label = await _call_never()
        return label, time.perf_counter() - started, degradations.current()

    (label, waited, noted), server, before, after = asyncio.run(
        _with_nameserver(monkeypatch, 0.3, _timed)
    )
    assert label == "" and waited < 3.0 and client.seen == []
    assert noted == ["classify: caller_type_kept (endpoint host refused)"]
    assert "never.example." in server.asked and after == before


async def _call_never() -> str:
    return await classify.classify(
        org="acme",
        namespace="ns",
        endpoint="https://never.example/c",
        prompt_template="",
        value="v",
        valid_types=[],
    )


def test_a_listed_private_host_is_looked_up_by_the_system_resolver(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    """/etc/hosts included: the operator listed it, so it is not a tenant's name."""

    async def _never(host: str) -> list[str]:
        raise AssertionError("a listed host is not looked up in the loop")

    monkeypatch.setattr(settings, "memory_classify_private_hosts", "classifier.local")
    monkeypatch.setattr("server.core.hosts._resolve", lambda host: ["10.0.0.9"])
    monkeypatch.setattr("server.core.hosts._resolve_in_loop", _never)
    assert _classify("http://classifier.local:9000/c") == "note"


def test_a_lookup_cancelled_from_outside_leaves_nothing_and_the_namespace_classifies_again(
    monkeypatch: pytest.MonkeyPatch, client: type[_Client]
) -> None:
    """The write is cancelled (its request dropped) mid-lookup: the lookup ends with it,
    and the namespace's next classification runs as before."""
    running: list[str] = []
    started = asyncio.Event()

    async def _lookup(host: str) -> list[str]:
        if host == "never.example":
            running.append(host)
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                running.remove(host)
        return ["93.184.215.14"]

    monkeypatch.setattr("server.core.hosts._resolve_in_loop", _lookup)
    monkeypatch.setattr("server.core.hosts._resolve", _no_thread)

    async def _call(host: str) -> str:
        return await classify.classify(
            org="acme",
            namespace="ns",
            endpoint=f"https://{host}/c",
            prompt_template="",
            value="v",
            valid_types=[],
        )

    async def _cancel_then_again() -> tuple[list[str], str]:
        task = asyncio.create_task(_call("never.example"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return list(running), await _call("classifier.example")

    left, again = asyncio.run(_cancel_then_again())
    assert left == [] and again == "note"


def test_the_in_loop_resolver_is_read_again_when_resolv_conf_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A container's DNS server can move without a restart: the resolver follows the
    file, and without one the lookup fails closed."""
    import os

    import dns.resolver

    from server.core import hosts

    conf = tmp_path / "resolv.conf"
    conf.write_text("nameserver 192.0.2.1\n")
    monkeypatch.setattr(hosts, "_RESOLV_CONF", str(conf))
    monkeypatch.setattr(hosts, "_resolver", None)
    first = hosts._async_resolver()
    assert first.nameservers == ["192.0.2.1"] and hosts._async_resolver() is first
    conf.write_text("nameserver 192.0.2.2\n")
    later = os.stat(conf).st_mtime_ns + 1_000_000_000
    os.utime(conf, ns=(later, later))
    assert hosts._async_resolver().nameservers == ["192.0.2.2"]
    conf.unlink()
    with pytest.raises(dns.resolver.NoResolverConfiguration):
        hosts._async_resolver()


def test_the_unit_network_guard_refuses_a_lookup_at_the_systems_nameserver() -> None:
    """dnspython at port 53 is the system's resolver, which may be a loopback stub that
    forwards: refused and recorded, `localhost` too (dnspython reads no /etc/hosts)."""
    import dns.asyncresolver

    from tests.unit import conftest

    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = ["127.0.0.53"]
    for name in ("example.com", "localhost"):
        with pytest.raises(OSError, match=f"unit tests may not resolve {name}"):
            asyncio.run(resolver.resolve(name, "A"))
    recorded = [a for a in conftest._network_attempts if a.startswith("resolve ")]
    assert recorded == ["resolve example.com", "resolve localhost"]
    conftest._network_attempts.clear()  # recorded on purpose: not this test's failure
