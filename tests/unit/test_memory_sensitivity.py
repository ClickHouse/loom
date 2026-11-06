"""Sensitivity tier: detection, the embed gate, and the
column riding every write path."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from server.memory.app.sensitivity_sweep import (
    _shapes,
    coarse_shape_re,
    scan_text,
)
from server.memory.app.set_chunks import human_text
from server.memory.domain.memory_types import is_embedded, is_searchable_type
from server.memory.domain.sensitivity import (
    contact_intent,
    detect_sensitivity,
    detector_text,
    high_spans,
    reads_high,
    resolve_sensitivity,
    value_reads_high,
)
from server.memory.store.rows import (
    _NON_NULLABLE_STRING_COLS,
    MEMORY_PERSISTED_COLS,
    MemoryRow,
)
from server.memory.workers.observe_extract import _sendable
from tests.unit.clickhouse_local import CLICKHOUSE, run

# The coarse filter of the `credential` shape alone: the scheduled pass's filter
# (`credential,email`) selects a high row through it.
_HIGH_SHAPE_RE = coarse_shape_re({"credential"})

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


@pytest.mark.parametrize("text", _FAKE_CREDENTIALS)
def test_the_scheduled_sweep_selects_every_credential_shape(text: str) -> None:
    # The sweep labels only what its coarse filter selects: as written, and as the JSON text
    # a scalar value is stored as.
    assert re.search(_HIGH_SHAPE_RE, text)
    assert re.search(_HIGH_SHAPE_RE, json.dumps(text))


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
    from server.memory.domain.sensitivity import sensitivity_tags

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
    assert reads_high(detector_text(value))


def test_detector_text_reads_plain_values_as_written_and_never_recurses() -> None:
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
        {"PWD": "/workspace1"},  # an environment's working directory, not a password
        {"pwd": "build/out1"},
        {"env": {"PWD": "/opt/app/server"}},
        {"password_hint": "first pet"},
        {"token_count": 123456789},
        {"secret_santa": "alice to bob"},
        # two scalars never read as one: a form label then the next key, or a number list
        {"label": "Password:", "placeholder": "Enter your password"},
        {"label": "Secret:", "required": True},
        {"api_key": "YOUR_API_KEY"},
        {"password": "not_set_yet"},
        {"client_secret": "<redacted>"},
        {"password": "[REDACTED]"},
        {"password": "${DB_PASSWORD}"},
        {"password": "vault:kv/db/password1"},
        {"password": "/etc/key1x"},
        {"client_secret": "/q8Zt/R3mKp9x"},  # two /-segments read as a path: a known miss
        {"password": "../secrets/db1"},
        {"password": "$DB_PASS1"},
        {"batch_sizes": [256, 512, 1024]},
    ],
)
def test_an_ordinary_object_never_reads_as_sensitive(value: object) -> None:
    # Each key and scalar is its own line, joined by a separator no pattern crosses: a config
    # object reads as the stored JSON always did, neither high nor medium.
    assert detect_sensitivity(detector_text(value)) == ""


@pytest.mark.parametrize(
    "value",
    [
        {"password": "hunter2xyz123"},
        {"db": {"password": "Tr0ub4dor&3"}},
        {"dbPassword": "hunter2xyz123"},
        {"client_secret": "9f8e7d6c5b4a3210ffee"},
        {"api.key": "abcd1234efgh"},
        {"password": "admin:Passw0rd"},
        {"password": "$ecr3tPass"},
        {"password": "ab\\cd1234"},
        {"password": "abc\bdef12"},
        {"client_secret": "/q8ZtR3mKp9x"},  # base64 may open with `/`; a path has two segments
        {"client_secret": "//q8ZtR3mKp9x"},  # no segment between the slashes: not a path
        {"password": "Abc;12345678"},
        {"db_password": "p@ss,w0rd99"},
        {"password": 'ab"cd12345'},
        {"password": "config/db/pass1"},  # a relative path is not a reference
        {"db_pwd": "hunter2xyz9"},
        {"db_password": "postgres://app:s3cretPw@db/main"},
        {"webhook_secret": "https://hooks.example/services/T000/B000/XXXXabc123"},
        # a URL that only names where the secret lives reads high too: a known false positive
        {"db_password": "https://vault.example/v1/secret/db1"},
        # an ARN's `secret:` part reads as an assignment: a known false positive
        {"client_secret": "arn:aws:secretsmanager:eu-west-1:123456789012:secret:prod/db-AbCd12"},
    ],
)
def test_a_credential_under_a_credential_key_reads_high(value: object) -> None:
    stored = json.dumps(value)
    assert value_reads_high(value)
    assert reads_high(stored)  # a raw chunk of the stored object
    assert high_spans(stored)


def test_a_working_directory_as_free_text_still_reads_high() -> None:
    # a known false positive: the assignment pattern takes `PWD=` as a password, though the
    # same line under a JSON `PWD` key is a working directory and stays unlabelled
    assert detect_sensitivity("PWD=/workspace1") == "high"
    assert detect_sensitivity(detector_text({"PWD": "/workspace1"})) == ""


@pytest.mark.parametrize(
    ("raw", "high"),
    [(r'{"password": "\/etc\/key1x"}', False), (r'{"password": "ab\/cd1234"}', True)],
)
def test_a_raw_pair_reads_its_value_decoded(raw: str, high: bool) -> None:
    # JSON may escape a slash: the raw text and the decoded value agree on a path.
    assert reads_high(raw) is high
    assert value_reads_high(json.loads(raw)) is high


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
    await write._set_prepare_value(w)
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
    assert detect_sensitivity(detector_text(value)) == level


def test_a_chunk_of_an_object_value_is_read_with_its_escapes_undone() -> None:
    # memory.set writes an object's chunks as indented JSON; the raw-text check reads them.
    assert reads_high(human_text({"notes": "deploy steps:\n" + _TOKEN}))
    assert reads_high(human_text({"notes": "line\r" + _TOKEN}))  # a lone carriage return
    assert reads_high(human_text({"note": "PIN is\v4471"}))  # written as `\u000b`
    # a key naming a secret, as `detector_text` reads it
    assert reads_high(human_text({"badge_pin": "4471"}))
    assert reads_high(human_text({"ssn": 123456789}))
    assert not reads_high(human_text({"pin_count": 4471}))
    assert not reads_high(human_text({"pin": "560001"}))
    # the value ends where the number does: not read as a shorter number
    assert not reads_high(human_text({"pin": "4471abc"}))
    assert not reads_high(human_text({"pin": 4471.5}))
    assert not reads_high(human_text({"pin": "4" * 20}))
    assert not reads_high(human_text({"pin": "4471 abc"}))  # the quote closes after the digits
    assert not reads_high(human_text({"p_in": 1013}))
    # a key holding an escaped quote or backslash
    assert reads_high(human_text({'pin"': "4471"}))
    assert reads_high(human_text({"door\\pin": "4471"}))
    # an escaped backslash before `n` stays a backslash and an `n`: no line starts there
    assert not reads_high(human_text({"note": "x\\n" + _TOKEN}))
    assert not reads_high(human_text({"label": "Password:", "placeholder": "Enter one"}))


def test_a_protected_attribute_after_a_newline_is_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    # The write path tags attributes on the decoded value: in the stored JSON `\nmarried` has
    # no word boundary before the word.
    from server.memory.domain.sensitivity import attribute_tags
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
        "amex 378282246310005",  # ungrouped, 15 digits
        "visa 4000 0000 0000 0000 006",  # 19 digits: the first 16 fail Luhn
        "mastercard 2221 0000 0000 0009",
        "diners 3056 930902 5904",
        "discover 6011111111111117",
        # pasted with its expiry or CVV, or after another number
        "card 4242 4242 4242 4242 08/27",
        "4111 1111 1111 1111 12/29",
        "4111 1111 1111 1111 123",
        "x2 4111 1111 1111 1111",
        "4111 1111 1111 1111 0429 123",  # card, MMYY, CVV
        "2024-01-15 4111 1111 1111 1111",
        "SSN 123-45-6789 on the form",
        "My employee badge PIN is 4471",
        "pin: 4471",
        "PIN code 0042",
        "CVV 123",
        "the cvc was 987",
        "PIN number 123456",
        "my PIN is 4471 of course",
        "My PIN is 4471.",
        # a label after a prefix, and two-word labels joined by `_`
        "BADGE_PIN=4471",
        "atm_pin: 4471",
        "PIN_CODE=4471",
        "CARD_CVV=123",
        "security_code: 5512",
        # compound labels
        "cvv code: 123",
        "CVV number is 123",
        "cvc code 123",
        "pass code: 4471",
        # a bounded qualifier
        "PIN for the front door is 4471",
        "passcode for the garage: 908712",
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
        "whatsapp 4915123456787",  # the same number without `+`: 13 digits, Luhn-valid
        "ts 1760000000000",  # epoch milliseconds: the phone shape's 13 digits, never a card
        "+1 415 555 0123",
    ],
)
def test_ibans_and_phone_numbers_are_medium(text: str) -> None:
    assert detect_sensitivity(text) == "medium"


def test_an_ibans_printed_groups_are_not_read_as_a_card() -> None:
    # A valid German IBAN whose groups 4005 0150 0000 0005 read as a Visa number that
    # passes Luhn: the IBAN is medium, and a card beside it is still high.
    iban = "DE24 4005 0150 0000 0005 00"
    assert detect_sensitivity("card 4005 0150 0000 0005") == "high"
    assert detect_sensitivity(f"salary goes to {iban}") == "medium"
    assert detect_sensitivity(f"iban {iban}, card 4111 1111 1111 1111") == "high"


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
        "hash 6000000000000000004",  # a 19-digit id with no network's prefix
        "hash 4000000000000000006",  # Visa prefix, Luhn-valid: only a printed grouping is 19
        "SSN 000-12-3456",  # never-issued area
        "SSN 666-12-3456",
        "SSN 912-34-5678",
        "SSN 123-00-4567",  # group 00
        "SSN 123-45-0000",  # serial 0000
        "released 2024-01-15",
        "PIN 3 of the header",
        "pin 100 of the connector",
        "PIN code 100 of the batch",
        "pinned 4471 rows",
        "pin 128 rows before the import",
        "GPIO pin 115",
        "wire it to pin #115",
        "spin: 4471",  # a word that ends in a label
        "PIN for the front door 4471",  # a bare pin takes no direct value
        "PIN for one two three four is 4471",  # more than three words
        # a version or a date is not a PIN
        "the tzdata pin is 2024.1",
        "pytz pin: 2023.3",
        "base image pin is 2024-01-15",
        "PIN for certifi is 2024.2.2",
        "tzdata pin: 2024a",
        "base image pin is 2024/01/15",
        "the pin is 1234abc",
        "ports 3000 5432 6379 9200 4000 4001 4002 4003 4004",  # nine groups: a table
        "ship to MG Road, Bengaluru, PIN code 560001",  # a postal code
        "PIN code: 560001",
        "Bengaluru, Karnataka, PIN: 560001",
        "PIN is 560001",
        "passcode 100 of the batch",
        "the spinner took 1500 ms",
        "a pin is 2 mm wide",
        "iban GB82 WEST 1234 5698 7654 33",  # fails mod-97
        "iban XY54 WEST 1234 5698 7654 32",  # passes mod-97; XY is no registered country
        "iban DE86 3704 0044 0532 0130",  # passes mod-97; 20 characters, a German IBAN is 22
        "ticket AB12 CDEF GHIJ KLMN",
        "fullwidth ＰＩＮ is ４４７１",  # ASCII only, as the sweep's RE2 filter is
    ],
)
def test_lookalike_numbers_stay_unset(text: str) -> None:
    assert detect_sensitivity(text) == ""


def test_a_connector_pin_written_as_an_assignment_reads_as_a_pin() -> None:
    # The price of reading "PIN: 4471" as a PIN; documented in docs/architecture/security.md.
    assert detect_sensitivity("GPIO pin: 115") == "high"
    assert detect_sensitivity(detector_text({"led": {"pin": 115}})) == "high"
    # and so does one written with `number`: the price of reading "PIN number 4471"
    assert detect_sensitivity("GPIO pin number 115") == "high"


@pytest.mark.parametrize(
    "value",
    [{"pin": " 4471"}, {"pin": "\t4471"}, {"pin": "4471 "}, {"pin": "\u00a04471"}],
)
def test_a_keyed_value_reads_the_same_on_every_path(value: object) -> None:
    # The stored value and a raw chunk of it give one answer, and the sweep's prefilter
    # selects what reads as high; the whitespace stripped is the ASCII whitespace the
    # patterns read.
    assert value_reads_high(value) == reads_high(human_text(value))
    if value_reads_high(value):  # the prefilter is a superset: what reads as high is selected
        assert re.search(_HIGH_SHAPE_RE, json.dumps(value))


def test_a_value_reading_high_is_left_out_of_a_model_prompt_whatever_its_label() -> None:
    # A caller's explicit level sets the stored label, but the readers that send a value to a
    # model check the value too: fail closed.
    assert resolve_sensitivity("low", "badge PIN is 4471") == "low"
    assert not _sendable("low", "badge PIN is 4471")
    assert not _sendable("", {"badge_pin": "4471"})
    assert _sendable("low", "the badge reader is by the door")


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
        ({"pin_count": 4}, ""),
        # a key naming a PIN, SSN or card number labels a value of that shape
        ({"pin": "4471"}, "high"),
        ({"badge_pin": "4471"}, "high"),
        ({"badgePin": 9087}, "high"),
        ({"pin_number": "4471"}, "high"),
        ({"pinNumber": "908712"}, "high"),
        ({"PIN": "4471"}, "high"),
        ({"CVV2": "123"}, "high"),
        ({"door": {"security-code": "5512"}}, "high"),
        ({"ssn": "123-45-6789"}, "high"),
        ({"ssn": "123456789"}, "high"),
        ({"card_number": "4111111111111111"}, "high"),  # a card under any key
        ({"pin": "560001"}, ""),  # a 6-digit pin is a postal code, as in free text
        ({"zip_pin": "560001"}, ""),
        ({"pin_code": "4471"}, "high"),
        ({"pin__code": "4471"}, "high"),
        ({"pin/code": "4471"}, "high"),
        ({"pincode": "4471"}, "high"),
        ({"pass_code": "4471"}, "high"),
        ({"cvv_2": "123"}, "high"),
        ({"cvv_code": "123"}, "high"),
        ({"cvcNumber": "987"}, "high"),
        ({"ssn_number": "123456789"}, "high"),
        ({"ssn_no": "123-45-6789"}, "high"),
        ({"social_security_number": "123-45-6789"}, "high"),
        ({"address": {"pincode": "560001"}}, ""),  # a 6-digit pin code is a postal code
        ({"pin": "A1"}, ""),
        ({"pin": "12"}, ""),
        ({"pin": True}, ""),
        ({"spin": "4471"}, ""),
        ({"p_in": 1013}, ""),  # `p` and `in` are not the word `pin`
        ({"passcode": "4471"}, "high"),
        ({"cvv2": "123"}, "high"),
        # a label's words written as one word
        ({"cvvcode": "123"}, "high"),
        ({"cvvnumber": "123"}, "high"),
        ({"cvccode": "123"}, "high"),
        ({"cvcnumber": "123"}, "high"),
        ({"ssnnumber": "123456789"}, "high"),
        ({"ssnno": "123456789"}, "high"),
        ({"SOCIALSECURITYNUMBER": "123-45-6789"}, "high"),
        # a run of capitals before a lowercase word
        ({"PINcode": "4471"}, "high"),
        ({"CVVcode": "123"}, "high"),
        ({"SSNno": "123-45-6789"}, "high"),
        ({"ATMPin": "4471"}, "high"),
        ({"badgePin": "4471"}, "high"),
        ({"Spin": 1200}, ""),  # one word
        ({"ATMPIN": "4471"}, ""),  # one word
        ({"securitycode": "5512"}, "high"),
        ({"ssn": "000-12-3456"}, ""),
        ({"pan": "4111111111111112"}, ""),
        ({"pan": "4471"}, ""),
    ],
)
def test_a_json_value_reads_by_its_scalars(value: object, level: str) -> None:
    assert detect_sensitivity(detector_text(value)) == level


def _read_long_runs(n: int) -> None:
    assert detect_sensitivity("7" * n) == ""
    assert not reads_high("1 " * (n // 2))
    assert reads_high("x " * (n // 2) + "CVV 123")
    # whitespace after a label is read once, not once per split between two runs
    assert detect_sensitivity("pin" + " " * n) == ""
    assert detect_sensitivity("PIN is" + "\n" * n) == ""
    assert detect_sensitivity("cvv" + " " * n + "x") == ""
    assert not reads_high('"pin":' + " " * n + "x")
    assert not reads_high('"pin": "' + " " * n + "x")
    assert detect_sensitivity("PIN for" + " " * n + "x") == ""
    assert detect_sensitivity("PIN for the" + " " * n + "door") == ""
    assert detect_sensitivity("GB82WEST12345698765432 " * (n // 10)) == "medium"
    # a run of local-part or token characters with no `@` / `.` is scanned once, not once
    # per word boundary inside it (the email and JWT shapes)
    assert detect_sensitivity("a-" * (n // 2)) == ""
    assert detect_sensitivity("a@" + "b." * (n // 2)) == ""
    assert detect_sensitivity("eyJ-" * (n // 4)) == ""


def _read_escaped_quotes(n: int) -> None:
    # each escaped quote is a possible key start: a key reads at most 200 characters
    assert not reads_high('\\"' * n)


def _fastest_seconds(read: Callable[[int], None], n: int) -> float:
    """The fastest of three reads, so one stall on a busy runner does not count."""
    times = []
    for _ in range(3):
        started = time.perf_counter()
        read(n)
        times.append(time.perf_counter() - started)
    return min(times)


@pytest.mark.parametrize(("read", "n"), [(_read_long_runs, 50_000), (_read_escaped_quotes, 5_000)])
def test_long_runs_are_read_in_linear_time(read: Callable[[int], None], n: int) -> None:
    # From n to 4n, a linear read takes about 4 times as long and a quadratic one 16 times.
    assert _fastest_seconds(read, 4 * n) < 8 * _fastest_seconds(read, n)


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
    # select whatever the detector labels, and the scheduled pass's filter every high row.
    level = detect_sensitivity(text)
    assert re.search(_HIGH_SHAPE_RE if level == "high" else coarse_shape_re(set()), text)
    assert _shapes(text, level) == (("credential",) if level == "high" else ("other",))


def test_the_sweeps_coarse_filter_needs_a_value_after_a_pin_label() -> None:
    # A selected row the detector rejects stays unlabelled and is selected again on every
    # pass, so a bare word must not take a place under the pass's row limit.
    assert not re.search(coarse_shape_re(set()), "pin numpy to 1.26 in the lockfile")
    assert not re.search(coarse_shape_re(set()), "the passcode screen and the CVV field")
    assert not re.search(coarse_shape_re(set()), "verify SHA256SUMS before the upload")
    assert not re.search(coarse_shape_re(set()), "pin the base image before the 2025 release")
    assert not re.search(coarse_shape_re(set()), "pin 128 rows before the import")
    assert not re.search(coarse_shape_re(set()), "GPIO pin 115")
    # `pin` and `pin code` take 3-5 digits, so a postal code is not selected
    assert not re.search(coarse_shape_re(set()), "Bengaluru, Karnataka, PIN: 560001")
    assert not re.search(coarse_shape_re(set()), "ship to MG Road, Bengaluru, PIN code 560001")
    assert not re.search(coarse_shape_re(set()), "PIN is 560001")
    # while every other label takes up to 8, so a 6-digit `PIN number` is
    assert re.search(coarse_shape_re(set()), "PIN number 560001")
    # a backspace is not whitespace to the detector, so the filter does not take it as one
    stored = json.dumps({"note": "PIN:\b4471"})
    assert detect_sensitivity(detector_text(stored)) == ""
    assert not re.search(coarse_shape_re(set()), stored)


@pytest.mark.parametrize(
    "value",
    [
        {"note": "door codes\nPIN: 4471"},
        {"note": "PIN:\n4471"},
        {"note": "PIN\nis 4471"},
        {"note": "bank:\nMT84 MALT 0110 0001 2345 MTLC AST0 01S"},
        {"note": "\u201cPIN is 4471\u201d"},  # stored as `\u201cPIN`
        {"note": "PIN:\f4471"},  # stored as `PIN:\f4471`
        {"note": "PIN is\v4471"},  # stored as `PIN is\u000b4471`
        {"note": "\bPIN is 4471"},  # stored as `\bPIN`
        {"badge_pin": "4471"},
        {"ssn": 123456789},
    ],
)
def test_the_sweeps_coarse_filter_reads_stored_json_text(value: object) -> None:
    # `match()` runs on the stored column, where an object value's line break is the two
    # characters `\n`; the detector reads the decoded text.
    stored = json.dumps(value)
    level = detect_sensitivity(detector_text(stored))
    assert level in ("high", "medium")
    assert re.search(_HIGH_SHAPE_RE if level == "high" else coarse_shape_re(set()), stored)


def test_a_question_for_a_bank_account_is_contact_intent() -> None:
    assert contact_intent("which IBAN does my salary go to?")
    assert contact_intent("what is the bank account for rent")
    assert contact_intent("list my IBANs")
    assert contact_intent("bank account numbers for payroll")
    assert contact_intent("which bank account numbers do we pay into")
    assert not contact_intent("what did we decide about the deploy schedule")
    assert not contact_intent("what is the AWS account number for staging")


_PREFILTER_OBJECTS = [
    {"pin": "4471"},
    {"password": "Abc;12345678"},
    {"db_password": "p@ss,w0rd99"},
    {"password": 'ab"cd12345'},
    {"password": "config/db/pass1"},
    {"badge_pin": " 4471"},
    {"badgePin": 9087},
    {"pin_number": "908712"},
    {"CVV2": "123"},
    {"pass_code": "4471"},
    {"cvv_2": "123"},
    {"pin_code": "4471"},
    {"pin__code": "4471"},
    {"pin/code": "4471"},  # written `pin\/code`
    {"pincode": "4471"},
    {"pin": "560001"},  # a postal code
    {"pin": "\t4471"},  # written `\t4471`
    {"note": "deploy:\n" + "sk-" + "proj-" + "Ab3" * 12},  # written `\nsk-`
    {"pin": " 4471"},
    {"CVV:": "123"},
    {"pin ": "4471"},
    {"SSN:": "123456789"},
    {"Security Code (CVV)": "123"},
    {"pin\n": "4471"},  # written `pin\n`
    {'pin"': "4471"},  # written `pin\"`
    {"door\\pin": "4471"},  # written `door\\pin`
    {"p_in": 1013},
    {"spin": "4471"},  # a key word ending in a label
    {"unpin": "1234"},
    {"cvvcode": "123"},
    {"ssnno": "123456789"},
    {"securitycode": "5512"},
    {"PINcode": "4471"},
    {"ATMPin": "4471"},
    {"Spin": 1200},
    {"ATMPIN": "4471"},
    {"cvv_code": "123"},
    {"ssn_no": "123456789"},
    {"social_security_number": "123-45-6789"},
    {"note": "BADGE_PIN=4471"},
    {"note": "PIN for the front door is 4471"},
    {"address": {"pincode": "560001"}},
    {"door": {"security-code": "5512"}},
    {"badge.pin": "4471"},  # nested in value_json: `{"badge":{"pin":...}}`
    {"security.code": "5512"},
    {"ssn": 123456789},
    {"note": "PIN is\v4471"},  # written `\u000B`
    {"note": "“PIN is 4471”"},  # kept raw
    {"note": "see https://x.example/a PIN: 4471"},  # `/` written `\/`
    {"note": "door codes\nPIN: 4471"},
    {"note": "bank:\nMT84 MALT 0110 0001 2345 MTLC AST0 01S"},
    {"note": "visa 4000 0000 0000 0000 006"},
    {"pin_count": 4},
    {"pin": "A1"},
    {"note": "pin 128 rows"},
    {"note": "deploy:\npassword=hunter2222"},  # written `\npassword`
    {"note": "api_key = abcdefgh12345678"},
    {"password": "hunter2xyz123"},
    {"db": {"password": "Tr0ub4dor&3"}},
    {"dbPassword": "hunter2xyz123"},
    {"api.key": "abcd1234efgh"},
    {"password": "required"},  # a form field, not a credential
    {"password": "ab\\cd1234"},  # written `ab\\cd1234`
    {"password": "p\u00e4ss1234"},  # written `p\u00e4ss1234`
    {"password": "abc\bdef12"},  # written `abc\bdef12`
    {"card": 4111111111111111},  # a lone card number
    {"note": "call 415\n555\n0123"},  # written `415\n555\n0123`
    {"note": "415\u00a0555\u00a00123"},  # written `415\u00a0555\u00a00123`
]
_PREFILTER_TEXTS = [
    "PIN is 4471",
    "CARD_CVV=123",
    "atm_pin: 4471",
    "cvv number is 123",
    "cvv2 123",
    "card 4111 1111 1111 1111",
    "SSN 123-45-6789",
    "iban GB82 WEST 1234 5698 7654 32",
    "pin 128 rows",
    "GPIO pin 115",
    "verify SHA256SUMS before the upload",
    # a key prefix the detector reads only at a word boundary
    "deploy key " + "sk-" + "proj-" + "Ab3" * 12,
    "eyJ" + "a1" * 8 + ".eyJ" + "b2" * 8 + "." + "Sf9" * 8,
    "task-1234 and disk-usage are done",
    "db password: " + "Tr0ub4dor&3xyz",
    "client_secret = " + "9f8e7d6c5b4a3210ffee",
    "mastercard 5500000000000004",
    "amex 378282246310005",
    # after a non-ASCII character, whose `\uXXXX` escape in `value` ends in a digit
    "番号4111111111111111",
    "番号4111 1111 1111 1111",
    "\u00a04111111111111111",
    "\u30004111111111111111",
]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_a_dotted_security_code_key_is_selected_and_labelled_by_the_sweep() -> None:
    # ClickHouse nests `security.code` in value_json, as `code` under `security`; the
    # writer's bytes in `value` keep the key whole, so the sweep labels the row as the
    # write path does.
    rows = run(
        [],
        "SELECT v AS value, toString(CAST(v AS JSON)) AS object_text, match(v, {r:String}) AS hit"
        " FROM (SELECT {v:String} AS v)",
        {"r": coarse_shape_re(set()), "v": json.dumps({"security.code": "5512"})},
    )
    assert rows[0]["object_text"] == '{"security":{"code":"5512"}}' and rows[0]["hit"] == 1
    assert detect_sensitivity(scan_text(rows[0])) == "high"
    assert value_reads_high({"security.code": "5512"})


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_the_sweeps_prefilter_selects_on_clickhouse_what_the_detector_labels() -> None:
    # RE2 `match()` on the texts the scan reads: the `json.dumps` text in `value`, and
    # `toString(value_json)` for an object value. The prefilter is a superset: every case
    # the detector labels is selected (a high one by the scheduled pass's filter too), and
    # the only unlabelled cases it selects are keys whose last word ends in a label
    # (documented in docs/architecture/security.md).
    sql = (
        "SELECT j AS value, toString(CAST(j AS JSON)) AS object_text,"
        " match(value, {r:String}) OR match(object_text, {r:String}) AS hit,"
        " match(value, {h:String}) OR match(object_text, {h:String}) AS high_hit"
        " FROM (SELECT arrayJoin({js:Array(String)}) AS j)"
        " UNION ALL SELECT j AS value, '{}' AS object_text, match(j, {r:String}) AS hit,"
        " match(j, {h:String}) AS high_hit"
        " FROM (SELECT arrayJoin({ts:Array(String)}) AS j)"
    )
    params = {
        "r": coarse_shape_re(set()),
        "h": _HIGH_SHAPE_RE,
        "js": [json.dumps(v) for v in _PREFILTER_OBJECTS],
        "ts": [json.dumps(t) for t in _PREFILTER_TEXTS],
    }
    rows = run([], sql, params)
    assert len(rows) == len(_PREFILTER_OBJECTS) + len(_PREFILTER_TEXTS)
    over_selected = set()
    for row in rows:
        level = detect_sensitivity(scan_text(row))
        if level == "high":
            assert int(row["high_hit"]), row
        if level:
            assert int(row["hit"]), row
        elif int(row["hit"]):
            over_selected.add(row["value"])
    assert over_selected == {
        '{"spin": "4471"}',
        '{"unpin": "1234"}',
        '{"Spin": 1200}',
        '{"ATMPIN": "4471"}',
        '{"password": "required"}',
    }


def test_an_email_and_a_jwt_still_read_as_before() -> None:
    assert detect_sensitivity("write to bob.smith+loom@mail.example.com today") == "medium"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib2IifQ.c2lnbmF0dXJlLXZhbHVl"
    assert detect_sensitivity(f"token {jwt}") == "high"
    assert detect_sensitivity(f"Authorization: Bearer {jwt}") == "high"
