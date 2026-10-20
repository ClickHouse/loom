"""Sensitivity tier: detection, the embed gate, and the
column riding every write path."""

from __future__ import annotations

import json
from typing import Any

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
def test_detector_text_lets_the_detector_read_what_the_value_holds(value: object) -> None:
    from server.memory.domain.sensitivity import detector_text, reads_high

    assert reads_high(detector_text(value))


def test_detector_text_reads_plain_values_as_written_and_never_recurses() -> None:
    from server.memory.domain.sensitivity import detector_text

    assert [detector_text(v) for v in ["null", "123", "plain words", None]] == [
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
    assert detector_text(deep) == "sk-" + "a" * 24


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
    from server.memory.domain.sensitivity import detect_sensitivity, detector_text

    assert detect_sensitivity(detector_text(value)) == ""


@pytest.mark.asyncio
async def test_memory_set_labels_and_tags_the_decoded_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # memory.set reads the decoded value for both: in the stored JSON a token or a protected
    # attribute after `\n` has no word boundary.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import blacklist_guard, write
    from server.memory.app.set_commit import _SetWrite
    from server.settings import settings

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", True)
    monkeypatch.setattr(settings, "memory_attribute_tagging", True)
    monkeypatch.setattr(blacklist_guard, "enforce_on_insert", _allowed)
    value = {"note": "status update:\nmarried last spring", "token": "id:\n" + _TOKEN}
    w = _SetWrite(
        input=MemorySetInput(org="o", namespace="n", agent="a", memory_key="k", value=value),
        version=1,
        retyped_from="",
        existing=None,
        expires_at=None,
    )
    await write._set_prepare_value(w, False)
    assert "sensitivity:high" in w.input.tags
    assert any(t.startswith("attribute:") for t in w.input.tags)


@pytest.mark.parametrize(
    ("value", "level"),
    [
        ({"name": "Ann", "email": "ann@example.com"}, "medium"),
        ({"phone": "415-555-0123", "n": 1}, "medium"),
        ({"phone": 4155550123}, "medium"),
    ],
)
def test_one_scalar_still_reads_as_what_it_holds(value: object, level: str) -> None:
    from server.memory.domain.sensitivity import detect_sensitivity, detector_text

    assert detect_sensitivity(detector_text(value)) == level


def test_a_chunk_of_an_object_value_is_read_with_its_escapes_undone() -> None:
    # memory.set writes an object's chunks as indented JSON; the raw-text check reads them.
    from server.memory.app.set_chunks import human_text
    from server.memory.domain.sensitivity import reads_high

    assert reads_high(human_text({"notes": "deploy steps:\n" + _TOKEN}))
    assert reads_high(human_text({"notes": "line\r" + _TOKEN}))  # a lone carriage return
    # an escaped backslash before `n` stays a backslash and an `n`: no line starts there
    assert not reads_high(human_text({"note": "x\\n" + _TOKEN}))
    assert not reads_high(human_text({"label": "Password:", "placeholder": "Enter one"}))


def test_a_protected_attribute_after_a_newline_is_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    # The write path tags attributes on the decoded value: in the stored JSON `\nmarried` has
    # no word boundary before the word.
    from server.memory.domain.sensitivity import attribute_tags, detector_text
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_attribute_tagging", True)
    stored = json.dumps({"note": "status update:\nmarried last spring"})
    assert attribute_tags(stored) == []
    assert attribute_tags(detector_text(stored)) != []


# Payment and identity numbers. 4111 1111 1111 1111 and 5500 0000 0000 0004 are the
# networks' published test cards; GB82 WEST ... and DE89 3704 ... are ISO 13616's examples.
@pytest.mark.parametrize(
    "text",
    [
        "card 4111 1111 1111 1111 exp 04/29",
        "charge it to 4111-1111-1111-1111",
        "mastercard 5500000000000004",
        "amex 3782 822463 10005",
        # pasted with its expiry or CVV, or after another number
        "card 4242 4242 4242 4242 08/27",
        "4111 1111 1111 1111 12/29",
        "4111 1111 1111 1111 123",
        "x2 4111 1111 1111 1111",
        "SSN 123-45-6789 on the form",
        "My employee badge PIN is 4471",
        "pin: 4471",
        "PIN code 0042",
        "CVV 123",
        "the cvc was 987",
        "PIN number 123456",
        "my PIN is 4471 of course",
        "passcode = 908712",
        "Security code: 55512",
    ],
)
def test_payment_and_identity_numbers_are_high(text: str) -> None:
    assert detect_sensitivity(text) == "high"


@pytest.mark.parametrize(
    "text",
    [
        "iban GB82 WEST 1234 5698 7654 32",
        "pay to DE89370400440532013000 by Friday",
        "ES91 2100 0418 4502 0005 1332 EUR",
        "ref XY12 GB82 WEST 1234 5698 7654 32",
        "ref XY12 LC55 HEMM 0001 0001 0012 0012 0002 3015",  # a 32-character IBAN
        # two phone numbers side by side are not read as one card number
        "phones: 415-555-0123 415-555-0199",
        # a phone number keeps its own tier: 13 digits after `+`, Luhn-valid or not
        "call +49 1512 3456789",
        "+1 415 555 0123",
    ],
)
def test_ibans_and_phone_numbers_are_medium(text: str) -> None:
    assert detect_sensitivity(text) == "medium"


@pytest.mark.parametrize(
    "text",
    [
        "card 4111 1111 1111 1112",  # fails the Luhn check
        "order 4111111111111112 shipped",
        "order 20240115103000 shipped",  # a yyyymmdd run: no card prefix
        "build 18446744073709551615",  # 20 digits: longer than any card
        "dev stack ports 3000 5432 6379 9200",  # Luhn-valid, but no network's prefix and length
        "ports 4000 4001 4002 4003 4004 4005 4006 4007",  # a table, not a card in it
        "IMEI 490154203237518",  # 15 digits, always Luhn-valid, no network issues 4 at 15
        "SSN 000-12-3456",  # never-issued area
        "SSN 666-12-3456",
        "SSN 912-34-5678",
        "SSN 123-00-4567",  # group 00
        "SSN 123-45-0000",  # serial 0000
        "released 2024-01-15",
        "PIN 3 of the header",
        "pin 100 of the connector",
        "pinned 4471 rows",
        "pin 128 rows before the import",
        "GPIO pin 115",
        "wire it to pin #115",
        "ship to MG Road, Bengaluru, PIN code 560001",  # a postal code
        "PIN code: 560001",
        "Bengaluru, Karnataka, PIN: 560001",
        "PIN is 560001",
        "passcode 100 of the batch",
        "the spinner took 1500 ms",
        "a pin is 2 mm wide",
        "iban GB82 WEST 1234 5698 7654 33",  # fails mod-97
        "ticket AB12 CDEF GHIJ KLMN",
        "fullwidth ＰＩＮ is ４４７１",  # ASCII only, as the sweep's RE2 filter is
    ],
)
def test_lookalike_numbers_stay_unset(text: str) -> None:
    assert detect_sensitivity(text) == ""


@pytest.mark.parametrize(
    ("value", "level"),
    [
        ({"card": "4111 1111 1111 1111"}, "high"),
        ({"card": 4111111111111111}, "high"),
        ({"note": "badge PIN is 4471"}, "high"),
        ({"iban": "GB82WEST12345698765432"}, "medium"),
        # each scalar is its own line: a list of digit groups is not a card number, and a
        # key is not a label for the next scalar (as with `password`)
        ({"groups": [4111, 1111, 1111, 1111]}, ""),
        ({"pin_count": 4471}, ""),
        ({"pin": "4471"}, ""),
    ],
)
def test_a_json_value_reads_by_its_scalars(value: object, level: str) -> None:
    from server.memory.domain.sensitivity import detector_text

    assert detect_sensitivity(detector_text(value)) == level


def test_a_long_digit_run_is_read_in_one_pass() -> None:
    from server.memory.domain.sensitivity import reads_high

    assert detect_sensitivity("7" * 200_000) == ""
    assert not reads_high("1 " * 100_000)
    assert reads_high("x " * 100_000 + "CVV 123")
    # whitespace after a label is read once, not once per split between two runs
    assert detect_sensitivity("pin" + " " * 200_000) == ""
    assert detect_sensitivity("PIN is" + "\n" * 200_000) == ""
    assert detect_sensitivity("cvv" + " " * 200_000 + "x") == ""


@pytest.mark.parametrize(
    "text",
    [
        "card 4111 1111 1111 1111",
        "amex 3782 822463 10005",
        "SSN 123-45-6789",
        "PIN is 4471",
        "cvv: 123",
        "cvv2 123",
        "passcode = 908712",
        "Security code: 55512",
        "iban GB82 WEST 1234 5698 7654 32",
    ],
)
def test_the_sweeps_coarse_filter_selects_every_new_shape(text: str) -> None:
    # The sweep narrows rows with RE2 `match()` before the detector runs, so the filter must
    # select whatever the detector labels; Python's `re` reads this RE2 subset the same way.
    import re

    from server.memory.app.sensitivity_sweep import COARSE_SHAPE_RE, _shapes

    assert re.search(COARSE_SHAPE_RE, text)
    level = detect_sensitivity(text)
    assert _shapes(text, level) == (("credential",) if level == "high" else ("other",))


def test_the_sweeps_coarse_filter_needs_a_value_after_a_pin_label() -> None:
    # A selected row the detector rejects stays unlabelled and is selected again on every
    # pass, so a bare word must not take a place under the pass's row limit.
    import re

    from server.memory.app.sensitivity_sweep import COARSE_SHAPE_RE

    assert not re.search(COARSE_SHAPE_RE, "pin numpy to 1.26 in the lockfile")
    assert not re.search(COARSE_SHAPE_RE, "the passcode screen and the CVV field")
    assert not re.search(COARSE_SHAPE_RE, "verify SHA256SUMS before the upload")
    assert not re.search(COARSE_SHAPE_RE, "pin the base image before the 2025 release")


@pytest.mark.parametrize(
    "value",
    [
        {"note": "door codes\nPIN: 4471"},
        {"note": "PIN:\n4471"},
        {"note": "PIN\nis 4471"},
        {"note": "bank:\nMT84 MALT 0110 0001 2345 MTLC AST0 01S"},
    ],
)
def test_the_sweeps_coarse_filter_reads_stored_json_text(value: object) -> None:
    # `match()` runs on the stored column, where an object value's line break is the two
    # characters `\n`; the detector reads the decoded text.
    import re

    from server.memory.app.sensitivity_sweep import COARSE_SHAPE_RE
    from server.memory.domain.sensitivity import detector_text

    stored = json.dumps(value)
    assert detect_sensitivity(detector_text(stored)) in ("high", "medium")
    assert re.search(COARSE_SHAPE_RE, stored)


def test_a_question_for_a_bank_account_is_contact_intent() -> None:
    from server.memory.domain.sensitivity import contact_intent

    assert contact_intent("which IBAN does my salary go to?")
    assert contact_intent("what is the bank account for rent")
    assert contact_intent("list my IBANs")
    assert contact_intent("account numbers for payroll")
    assert not contact_intent("what did we decide about the deploy schedule")
