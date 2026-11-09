"""With LOOM_CH_LOG_COMMENT_STATEMENT_DIGEST the `log_comment`
of every SELECT / command ends with `sql:<shape12> <first 200 chars>`, so
system.query_log can be grouped per statement shape where the 100 KB `query`
column is cut inside a vector literal. Off by default: the tag stays exactly
"<op> <request_id>"."""

from __future__ import annotations

import pytest

from server.core import ch as _ch
from server.core.ch import query_tag_var, statement_settings
from server.core.ch_statement_text import statement_digest, statement_shape

_A = "SELECT memory_key, cosineDistance(embedding, [0.1, -0.2, 0.3]) AS d FROM loom.memory_embeddings WHERE org = 'o' AND namespace = 'ns' ORDER BY d LIMIT 10"
_B = "SELECT memory_key, cosineDistance(embedding, [0.9, 0.8, -0.7]) AS d FROM loom.memory_embeddings WHERE org = 'other' AND namespace = 'x' ORDER BY d LIMIT 40"
_C = "SELECT memory_key, session_id FROM loom.memory_embeddings WHERE org = 'o' AND namespace = 'ns' ORDER BY d LIMIT 10"


def test_literals_do_not_change_the_shape_but_the_select_list_does() -> None:
    sa, sb, sc = (statement_digest(s).split(" ", 1)[0] for s in (_A, _B, _C))
    assert sa == sb
    assert sa != sc
    assert sa.startswith("sql:") and len(sa) == len("sql:") + 12


def test_the_head_is_the_first_200_characters_with_whitespace_collapsed() -> None:
    long = "SELECT\n    a,\n    b\n  FROM t WHERE " + " AND ".join(
        f"c{i} = {i}" for i in range(200)
    )
    shape, head = statement_digest(long).split(" ", 1)
    assert head.startswith("SELECT a, b FROM t WHERE c0 = 0 AND c1 = 1")
    assert len(head) == 200
    assert "\n" not in head


def test_a_secret_or_an_analyst_statement_logs_its_shape_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_ch.settings, "ch_log_comment_statement_digest", True)
    secret = "CREATE USER x IDENTIFIED WITH sha256_password BY 'Aa1-not-in-the-log'"
    with _ch.uncaptured():
        lc = statement_settings(secret)["log_comment"]
    assert lc.endswith(statement_shape(secret)) and "not-in-the-log" not in lc
    caller = "SELECT * FROM (SELECT 'a caller literal') LIMIT 51"
    with _ch.as_analyst("loom_analyst", "pw", {}):
        lc = statement_settings(caller)["log_comment"]
    assert lc.endswith(statement_shape(caller)) and "caller literal" not in lc


def test_off_by_default_the_tag_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ch.settings, "ch_log_comment_statement_digest", False)
    token = query_tag_var.set("memory.search rid-1")
    try:
        out = statement_settings(_A)
        out.pop("max_execution_time", None)  # per-class statement deadline
        assert out == {"log_comment": "memory.search rid-1"}
    finally:
        query_tag_var.reset(token)


def test_on_the_digest_follows_the_tag_as_trailing_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ch.settings, "ch_log_comment_statement_digest", True)
    token = query_tag_var.set("memory.search rid-1")
    try:
        lc = statement_settings(_A)["log_comment"]
    finally:
        query_tag_var.reset(token)
    parts = lc.split(" ")
    assert parts[:2] == ["memory.search", "rid-1"], "the 0096 tag is still the first two tokens"
    assert parts[2] == statement_digest(_A).split(" ", 1)[0]
    assert lc.endswith(statement_digest(_A))


def test_on_without_a_tag_the_digest_stands_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ch.settings, "ch_log_comment_statement_digest", True)
    token = query_tag_var.set("")
    try:
        assert statement_settings(_A)["log_comment"] == statement_digest(_A)
    finally:
        query_tag_var.reset(token)


def test_cost_by_op_readers_ignore_the_digest_tokens() -> None:
    from bench.ch_cost_by_op import op_of, request_of

    tagged = "memory.search rid-1 " + statement_digest(_A)
    assert op_of(tagged) == "memory.search"
    assert request_of(tagged) == "rid-1"
    worker = "worker.usage_fold " + statement_digest(_A)
    assert op_of(worker) == "worker.usage_fold"
    assert request_of(worker) == ""
