"""A private closer cannot publish positive evidence through a later recall-credit cache hit."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager

import pytest

from server.core import diagnostics, request_work
from server.memory.app import recall_credit


@contextmanager
def privacy(kind: str):
    if kind == "private":
        with diagnostics.private_work():
            yield
    elif kind == "http_owner":
        with request_work.http_owner({"type": "http"}):
            request_work.begin()
            assert request_work.mark_mirrored()
            request_work.begin()
            assert not request_work.mirror_marked() and request_work.http_mirror_marked()
            yield
    else:
        yield


@pytest.mark.parametrize("kind", ["ordinary", "private", "http_owner", "unavailable"])
@pytest.mark.parametrize("cached", [False, True])
def test_only_ordinary_closers_change_cached_recall_evidence(monkeypatch, kind, cached) -> None:
    key = ("org", "client")
    before = {key: (False, float("inf"))} if cached else {}
    monkeypatch.setattr(recall_credit, "_CLOSER_CACHE", dict(before))

    if kind == "unavailable":

        def unavailable():
            raise RuntimeError("classification unavailable")

        monkeypatch.setattr(request_work, "http_mirror_marked", unavailable)

    def exercise():
        with privacy(kind):
            recall_credit.note_closer(*key)
        if kind == "ordinary":
            assert recall_credit._CLOSER_CACHE[key][0] is True
        else:
            assert recall_credit._CLOSER_CACHE == before

    contextvars.copy_context().run(exercise)
