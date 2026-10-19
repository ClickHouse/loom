"""Sensitivity tier: detection, the embed gate, and the
column riding every write path."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from server.memory.domain.memory_types import is_embedded, is_searchable_type
from server.memory.domain.sensitivity import detect_sensitivity, resolve_sensitivity
from server.memory.store.rows import (
    _NON_NULLABLE_STRING_COLS,
    MEMORY_PERSISTED_COLS,
    MemoryRow,
)

# Credential SHAPES are assembled at runtime so no tracked file carries a
# string the repo's secret guard (tests/unit/test_no_secrets_guard.py) would
# flag; the detector only ever sees the joined value.
_FAKE_CREDENTIALS = [
    "OPENAI_API_KEY=" + "sk-" + "proj-" + "Ab3" * 12,
    "the anthropic key is " + "sk-" + "ant-" + "api03-" + "Zq9" * 12,
    "aws access key " + "AKIA" + "IOSFODNN7EXAMPLE" + " for the staging account",
    "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow...",
    "session jwt " + ".".join("eyJ" + "a1" * 8 for _ in range(2)) + "." + "Sf9" * 8,
    "db password: " + "Tr0ub4dor&3xyz",
    "client_secret = " + "9f8e7d6c5b4a3210ffee",
    "github token " + "ghp_" + "Ab1" * 12,
    "slack " + "xoxb-" + "1234567890-" + "abcdefghij",
]


@pytest.mark.parametrize("text", _FAKE_CREDENTIALS)
def test_credential_shapes_are_high(text: str) -> None:
    assert detect_sensitivity(text) == "high"


@pytest.mark.parametrize(
    "text",
    [
        "Reach Melanie at melanie@example.com about the picnic",
        "Caroline's number is +1 415 555 0123",
        "call (415) 555-0123 after 6",
    ],
)
def test_contact_details_are_medium(text: str) -> None:
    assert detect_sensitivity(text) == "medium"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "deploys are Tue/Thu only",
        "Caroline moved from her hometown, Sweden, 4 years ago.",
        "the release was 26.8 and the build took 2023 seconds",
        "user prefers dark mode; the token budget is 4096",
        "password reset flow lives in auth/reset.py",
    ],
)
def test_plain_facts_stay_unset(text: str) -> None:
    assert detect_sensitivity(text) == ""


def test_a_line_with_both_is_high() -> None:
    assert detect_sensitivity("ops@example.com api_key=abcdefgh12345678") == "high"


def test_resolve_prefers_the_callers_level(monkeypatch: pytest.MonkeyPatch) -> None:
    from server import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "memory_sensitivity_autodetect", True)
    assert resolve_sensitivity("low", "password=supersecret123") == "low"
    assert resolve_sensitivity("", "password=supersecret123") == "high"
    assert resolve_sensitivity(None, "melanie@example.com") == "medium"
    assert resolve_sensitivity("bogus", "plain") == ""
    monkeypatch.setattr(settings_mod.settings, "memory_sensitivity_autodetect", False)
    assert resolve_sensitivity("", "password=supersecret123") == ""
    assert resolve_sensitivity("high", "plain") == "high"


def test_high_is_never_embedded_whatever_the_type() -> None:
    assert is_searchable_type("semantic")
    assert is_embedded("semantic", "")
    assert is_embedded("semantic", "medium")
    assert not is_embedded("semantic", "high")
    assert not is_embedded("preference", "high")
    # Non-searchable types stay non-embedded regardless.
    assert not is_embedded("episodic", "")
    assert not is_embedded("profile", "low")


def test_sensitivity_rides_the_persisted_column_set() -> None:
    """Every version-row writer mirrors MEMORY_PERSISTED_COLS; omitting the
    column would zero it under FINAL for every reader."""
    assert "sensitivity" in MEMORY_PERSISTED_COLS
    assert "sensitivity" in _NON_NULLABLE_STRING_COLS
    assert MemoryRow.model_fields["sensitivity"].default == ""


def test_consolidation_canonical_row_carries_the_column() -> None:
    import inspect

    from server.memory.app import consolidate as _memory_consolidate

    src = inspect.getsource(_memory_consolidate)
    # The column rides every canonical row — and it is the inherited label,
    # not a hardcoded '' (a 'medium' source is embedded and
    # can enter a cluster; the merged row must not come out unlabelled).
    assert '"sensitivity": inherited_sensitivity(' in src


def test_inherited_sensitivity_never_lowers_the_label() -> None:
    """A row synthesised from other rows (dense topic memory, merged canonical
    row) keeps the highest label among its sources, and the summary text is
    re-detected so a kept email address is still 'medium'."""
    from server.memory.domain.sensitivity import inherited_sensitivity, max_sensitivity

    assert max_sensitivity(["", None, "low"]) == "low"
    assert max_sensitivity(["medium", "", "low"]) == "medium"
    assert max_sensitivity(["high", "medium"]) == "high"
    assert max_sensitivity(["bogus", ""]) == ""
    assert inherited_sensitivity(["", ""], "User prefers dark mode.") == ""
    assert inherited_sensitivity(["medium", ""], "User prefers dark mode.") == "medium"
    # unlabelled sources, but the summary itself carries a contact detail
    assert inherited_sensitivity(["", ""], "Reach the owner at ops@example.com") == "medium"
    assert inherited_sensitivity(["low"], "x") == "low"


def test_sensitivity_tag_rides_labelled_rows_and_contact_intent_is_recognised() -> None:
    """The tier travels as a `sensitivity:<level>` tag so the
    embeddings table (no sensitivity column) can be ranked on it; a question
    that asks how to reach someone is contact intent and lifts the demotion."""
    from server.memory.domain.sensitivity import contact_intent, sensitivity_tags

    assert sensitivity_tags("medium") == ["sensitivity:medium"]
    assert sensitivity_tags("high", ["sensitivity:high"]) == []  # already carried
    assert sensitivity_tags("") == [] and sensitivity_tags("low") == []
    assert contact_intent("Who owns the nightly build and how do I reach them?")
    assert contact_intent("what's Grace's phone number")
    assert contact_intent("send an email to the on-call owner")
    assert not contact_intent("Who reviewed the on-call rota recently?")
    assert not contact_intent("what did Grace decide about the release train")


def test_sensitivity_penalty_is_a_per_request_field_on_search_and_ask() -> None:
    """A caller can zero (or raise) the demotion per request;
    memory.ask forwards it to memory.search unchanged, and the handler helper
    lets the explicit value beat both the default and the contact-intent zero."""
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.knobs import _sensitivity_penalty

    s = MemorySearchInput(org="o", namespace="n", query="who reviewed the rota?")
    assert s.sensitivity_penalty is None
    a = MemoryAskInput(
        org="o", namespace="n", query="how do I reach Priya?", sensitivity_penalty=0.25
    )
    assert a.sensitivity_penalty == 0.25
    # explicit wins over contact intent (which would otherwise zero it)
    assert _sensitivity_penalty(a) == 0.25
    # contact intent zeroes the default
    assert (
        _sensitivity_penalty(
            MemorySearchInput(org="o", namespace="n", query="how do I reach Priya?")
        )
        == 0.0
    )
    with pytest.raises(ValidationError):
        MemorySearchInput(org="o", namespace="n", query="q", sensitivity_penalty=1.5)


def test_recent_recall_admits_medium_only_on_contact_intent() -> None:
    """The stack's recent_recall replays lately-recalled rows into every pack
    regardless of query; a contact detail rides it only when the seeding
    question asked for one."""
    from server.memory.app.stack import recent_recall_admits_medium

    assert recent_recall_admits_medium("how do I reach the owner of the nightly build?")
    assert recent_recall_admits_medium("who should I call about the rota")
    assert not recent_recall_admits_medium("who reviewed the on-call rota recently?")
    assert not recent_recall_admits_medium("")


@pytest.mark.asyncio
async def test_memory_stack_recent_recall_sql_carries_the_medium_guard(monkeypatch) -> None:
    from types import SimpleNamespace

    from server.memory.app import handlers as mem
    from server.memory.app import restore as _restore_mod
    from server.memory.app import stack as _stack_mod
    from server.memory.app import stack as stack_mod

    seen: list[tuple[str, dict]] = []

    class _CH:
        async def query_all(self, sql, params=None, **kw):
            seen.append((sql, dict(params or {})))
            return []

        async def query_one(self, *a, **kw):
            return None

    monkeypatch.setattr(mem, "get_client", lambda: _CH())
    monkeypatch.setattr(_stack_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(_restore_mod, "get_client", lambda: _CH())
    monkeypatch.setattr("server.memory.app.write.get_client", mem.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem.get_client)

    async def _no_profile(*a, **kw):
        raise RuntimeError("no profile in test")

    monkeypatch.setattr(
        "server.memory.app.profile.handle_profile_render", _no_profile, raising=False
    )
    inp = SimpleNamespace(
        org="o",
        namespace="n",
        agent="a",
        user_id="",
        session_id="",
        run_id="",
        token_budget=2000,
        pinned_limit=5,
        recent_limit=20,
        include_knowledge=False,
        knowledge_top_k=0,
        seed_query="who reviewed the rota?",
        procedure_limit=0,
        include_procedures=False,
        sections=None,
    )
    try:
        await stack_mod.handle_memory_stack(inp)
    except Exception:  # noqa: BLE001 — must not raise: later sections may need unstubbed fields
        pass  # sections after recent_recall may need fields this stub lacks
    recent = [(sql, p) for sql, p in seen if "recall_count > 0" in sql]
    assert recent, "recent_recall query not issued"
    sql, params = recent[0]
    assert "sensitivity != 'medium' OR {medium_ok:UInt8} = 1" in sql
    assert params["medium_ok"] == 0
    # both stack reads serve only live rows: not expired, not retired (superseded,
    # duplicate, proposed, rejected)
    pinned = [q for q, _ in seen if "pinned = 1" in q]
    assert pinned
    for q in (sql, pinned[0]):
        assert "(expires_at IS NULL OR expires_at > now())" in q
        assert "status IN ('active', 'conflicted')" in q


_TOKEN = "ghp_" + "a" * 36


@pytest.mark.parametrize(
    "value",
    [
        json.dumps("deploy token:\n" + _TOKEN),  # a string, as memory.set stores it
        {"note": "deploy token:\n" + _TOKEN},
        {_TOKEN: "ci token"},  # the secret is the key
        json.dumps({"narrative": 'config: password: "hunter2secret"'}),
    ],
    ids=["json-string", "own-line", "in-a-key", "quoted"],
)
def test_value_text_lets_the_detector_read_what_the_value_holds(value: object) -> None:
    from server.memory.domain.sensitivity import reads_high, value_text

    assert reads_high(value_text(value))


def test_value_text_reads_plain_values_as_written_and_never_recurses() -> None:
    from server.memory.domain.sensitivity import value_text

    assert [value_text(v) for v in ["null", "123", "plain words", None]] == [
        "",
        "123",
        "plain words",
        "",
    ]
    deep: list[object] = []
    inner = deep
    for _ in range(5000):
        nxt: list[object] = []
        inner.append(nxt)
        inner = nxt
    inner.append("sk-" + "a" * 24)
    assert value_text(deep) == "sk-" + "a" * 24


def test_an_update_labels_a_token_on_its_own_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # The write path detects on the decoded value, not the stored JSON.
    from types import SimpleNamespace

    from server.memory.app.update import _update_sensitivity
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", True)
    level, _tags = _update_sensitivity(
        SimpleNamespace(sensitivity=""), json.dumps({"note": "token:\n" + _TOKEN}), []
    )
    assert level == "high"


@pytest.mark.parametrize(
    "value",
    [
        {"password": {"min_length": 12}},
        {"password": None, "username": "alice"},
        {"secret": {"provider": "vault"}},
        {"fields": {"email": "required", "password": "required"}},
        {"password": "********"},
        {"api_key": "env:OPENAI_API_KEY"},
        {"pwd": "/home/alice/repo"},
        {"env": {"PWD": "/opt/app/server"}},
        {"password_hint": "first pet"},
        {"token_count": 123456789},
        {"secret_santa": "alice to bob"},
        # two scalars never read as one: a form label then the next key, or a number list
        {"label": "Password:", "placeholder": "Enter your password"},
        {"label": "Secret:", "required": True},
        {"batch_sizes": [256, 512, 1024]},
    ],
)
def test_an_ordinary_object_never_reads_as_sensitive(value: object) -> None:
    # Each key and scalar is its own line, joined by a separator no pattern crosses: a config
    # object reads as the stored JSON always did, neither high nor medium.
    from server.memory.domain.sensitivity import detect_sensitivity, value_text

    assert detect_sensitivity(value_text(value)) == ""


def test_a_protected_attribute_after_a_newline_is_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    # The write path tags attributes on the decoded value: in the stored JSON `\nmarried` has
    # no word boundary before the word.
    from server.memory.domain.sensitivity import attribute_tags, value_text
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_attribute_tagging", True)
    stored = json.dumps({"note": "status update:\nmarried last spring"})
    assert attribute_tags(stored) == []
    assert attribute_tags(value_text(stored)) != []
