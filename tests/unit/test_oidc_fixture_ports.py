"""OIDC fixture port allocation excludes the already listening IdP."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.integration import test_oidc_flow as flow


@pytest.mark.parametrize("loom_port", [None, 41003])
def test_oidc_stack_excludes_idp_port(monkeypatch, loom_port):
    bound = set()

    def free_port():
        return next(port for port in (41001, 41002) if port not in bound)

    class FakeIdP:
        def __init__(self, email):
            self.port = free_port()
            self.url = f"http://127.0.0.1:{self.port}"

        @contextmanager
        def running(self):
            bound.add(self.port)
            try:
                yield self
            finally:
                bound.remove(self.port)

    @contextmanager
    def loom_server(label, env, *, port):
        assert port not in bound
        assert bound == {41001}
        assert port == (loom_port or 41002)
        assert env["LOOM_OIDC_ISSUER"] == "http://127.0.0.1:41001"
        assert env["LOOM_OAUTH_ISSUER"] == f"http://127.0.0.1:{port}"
        assert env["LOOM_OIDC_JIT_PROVISION"] == "true"
        yield env["LOOM_OAUTH_ISSUER"]

    monkeypatch.delenv("LOOM_CH_SECURE", raising=False)
    monkeypatch.setattr(flow, "free_port", free_port)
    monkeypatch.setattr(flow, "FakeIdP", FakeIdP)
    monkeypatch.setattr(flow, "loom_server", loom_server)
    request = SimpleNamespace(param={"seed": False, "jit": True, "loom_port": loom_port})
    stack = flow.oidc_stack.__wrapped__(None, request)
    _, namespace, base = next(stack)
    assert namespace is None
    assert base == f"http://127.0.0.1:{loom_port or 41002}"
    with pytest.raises(StopIteration):
        next(stack)
    assert not bound
