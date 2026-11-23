"""The pure codecs do not qualify storage, secret rotation or source authorization."""

from __future__ import annotations

import json
import traceback
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from server.knowledge.domain.mcp_sources import (
    ConnectionEnvelope,
    CopyConsent,
    MCPSourceContractError,
    ResourceIdentity,
    SourceContract,
    bind_envelope,
    decode_envelope,
    decode_source,
    encode_envelope,
    encode_source,
    selection_sha256,
)

_TOKEN = "fixture-secret-bearer"
_SLOT = "mcp:" + "a" * 32
_GENERATION = "b" * 32 + "/1" + "c" * 31


def _envelope(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "org": "test-org",
        "owner": "person-one",
        "slot": _SLOT,
        "generation": _GENERATION,
        "profile_id": "documents",
        "profile_sha256": "d" * 64,
        "endpoint": "https://source.example/mcp",
        "audience": "https://source.example/mcp",
        "auth": "bearer",
        "bearer": _TOKEN,
        "label": "My documents",
        **changes,
    }


def _decode(**changes: Any) -> ConnectionEnvelope:
    return decode_envelope(json.dumps(_envelope(**changes)))


def _source(**changes: Any) -> SourceContract:
    return SourceContract.model_validate(
        {
            "version": 1,
            "slot": _SLOT,
            "generation": _GENERATION,
            "profile_id": "documents",
            "profile_sha256": "d" * 64,
            "mode": "snapshot",
            "reader_policy": "owner_only",
            "resources": (
                {"type": "resource", "id": "document:second"},
                {"type": "resource", "id": "document:first"},
            ),
            **changes,
        }
    )


def _binding(envelope: ConnectionEnvelope) -> dict[str, Any]:
    return envelope.model_dump(exclude={"version", "auth", "label"})


def _assert_public_error(call: Any) -> None:
    with pytest.raises(MCPSourceContractError) as caught:
        call()
    assert str(caught.value) == "MCP_SOURCE_CONTRACT_INVALID"
    assert _TOKEN not in repr(caught.value)
    assert _TOKEN not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


def test_envelope_roundtrip_keeps_secret_only_in_sealing_plaintext() -> None:
    envelope = _decode()
    assert envelope.bearer == SecretStr(_TOKEN)
    assert decode_envelope(encode_envelope(envelope)) == envelope
    assert json.loads(encode_envelope(envelope))["bearer"] == _TOKEN
    assert _TOKEN not in repr(envelope)
    assert _TOKEN not in str(dict(envelope))
    assert "bearer" not in envelope.model_dump()
    assert "bearer" not in envelope.model_dump(include={"bearer"})
    assert _TOKEN not in envelope.model_dump_json()
    # A general dump is not sealing plaintext; it must not restore a redacted token.
    _assert_public_error(lambda: decode_envelope(envelope.model_dump_json()))


def test_anonymous_auth_roundtrip_and_frozen_models() -> None:
    envelope = _decode(auth="none", bearer=None, audience=None)
    assert decode_envelope(encode_envelope(envelope)) == envelope
    with pytest.raises(ValidationError):
        envelope.owner = "another-person"  # type: ignore[misc]
    source = _source()
    with pytest.raises(ValidationError):
        source.resources[0].id = "another-document"  # type: ignore[misc]
    assert isinstance(source.resources, tuple)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": "1"},
        {"version": 1.0},
        {"version": 2},
        {"slot": "mcp:" + "A" * 32},
        {"slot": "mcp:" + "a" * 31},
        {"generation": "b" * 32 + "/2" + "c" * 31},
        {"generation": "b" * 32 + "/1" + "c" * 30},
        {"profile_sha256": "D" * 64},
        {"owner": 17},
        {"org": ""},
        {"profile_id": " "},
        {"owner": "person\nother"},
        {"owner": "person\x85other"},
        {"endpoint": "x" * 2049},
        {"endpoint": "é" * 1025},
        {"audience": False},
        {"label": "x" * 257},
        {"bearer": ""},
        {"bearer": "x" * 8193},
        {"bearer": _TOKEN + "\n"},
        {"bearer": "é"},
        {"bearer": 9},
        {"auth": "oauth"},
        {"auth": "none"},
        {"auth": "none", "audience": None},
        {"auth": "none", "bearer": None},
        {"bearer": None},
        {"audience": None},
        {"verified_email": "unproven@example.test"},
        {"refresh_token": _TOKEN},
    ],
)
def test_envelope_rejects_unknown_coercive_and_invalid_input(changes: dict[str, Any]) -> None:
    _assert_public_error(lambda: _decode(**changes))


@pytest.mark.parametrize("raw", ["", "{", "[]", "null", "42", '{"version":1}', _TOKEN])
def test_envelope_rejects_missing_or_truncated_input(raw: str) -> None:
    _assert_public_error(lambda: decode_envelope(raw))


def test_envelope_plaintext_bytes_are_bounded_before_decoding_and_encoding() -> None:
    raw = encode_envelope(_decode())
    boundary = raw + " " * (32768 - len(raw.encode()))
    assert decode_envelope(boundary).owner == "person-one"
    _assert_public_error(lambda: decode_envelope(boundary + " "))
    # Valid local field bounds can still exceed the wire bound after JSON escaping.
    envelope = ConnectionEnvelope.model_validate(
        _envelope(
            **dict.fromkeys(("org", "owner", "profile_id", "endpoint", "audience"), '"' * 2048),
            bearer="\\" * 8192,
        )
    )
    _assert_public_error(lambda: encode_envelope(envelope))


def test_invalid_unicode_and_nontext_plaintext_have_public_errors() -> None:
    _assert_public_error(lambda: decode_envelope("\ud800"))
    _assert_public_error(lambda: decode_envelope(b"{}"))  # type: ignore[arg-type]
    _assert_public_error(lambda: _decode(owner="\ud800"))


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("org", "other-org"),
        ("owner", "person-two"),
        ("slot", "mcp:" + "e" * 32),
        ("generation", "b" * 32 + "/0" + "c" * 31),
        ("profile_id", "other-profile"),
        ("profile_sha256", "e" * 64),
        ("endpoint", "https://source.example/other"),
        ("audience", "https://source.example/other"),
        ("audience", None),
    ],
)
def test_binding_requires_exact_row_and_current_profile(field: str, other: Any) -> None:
    envelope = _decode()
    bind_envelope(envelope, **_binding(envelope))
    _assert_public_error(lambda: bind_envelope(envelope, **{**_binding(envelope), field: other}))


def test_multiple_slots_do_not_change_each_others_binding() -> None:
    first = _decode()
    second = _decode(slot="mcp:" + "e" * 32, generation="f" * 32 + "/1" + "a" * 31)
    bind_envelope(first, **_binding(first))
    bind_envelope(second, **_binding(second))
    _assert_public_error(lambda: bind_envelope(first, **_binding(second)))
    assert decode_envelope(encode_envelope(first)) == first


def test_source_splits_columns_without_duplicating_selection_and_preserves_order() -> None:
    source = _source()
    config, items = encode_source(source)
    assert "resources" not in json.loads(config)
    assert "document:" not in config
    assert json.loads(items) == [
        {"type": "resource", "id": "document:second"},
        {"type": "resource", "id": "document:first"},
    ]
    assert decode_source(config, items) == source
    assert source.resources[0].id == "document:second"


@pytest.mark.parametrize("mode", ["live", "snapshot", "selected_refresh"])
@pytest.mark.parametrize("policy", ["owner_only", "collection_copy"])
def test_policy_and_mode_are_declarations_not_authority(mode: str, policy: str) -> None:
    if policy == "collection_copy":
        with pytest.raises(ValidationError):
            _source(mode=mode, reader_policy=policy)
        return
    source = _source(mode=mode, reader_policy=policy)
    assert decode_source(*encode_source(source)) == source
    assert source.reader_policy == policy
    assert not hasattr(source, "authorized")


@pytest.mark.parametrize("mode", ["snapshot", "selected_refresh"])
def test_copy_consent_is_scoped_metadata_not_runtime_authorization(mode: str) -> None:
    consent = CopyConsent(
        version=1,
        publisher="person-one",
        namespace="col-team",
        resources_sha256=selection_sha256(_source().resources),
    )
    source = _source(mode=mode, reader_policy="collection_copy", copy_consent=consent)
    assert decode_source(*encode_source(source)) == source
    assert not hasattr(source, "authorized")


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": "1"},
        {"version": 2},
        {"mode": "sync_everything"},
        {"reader_policy": "upstream_acl"},
        {"recursive": True},
        {"slot": "gdrive"},
        {"generation": "not-a-generation"},
        {"profile_sha256": "not-a-digest"},
        {"profile_id": "\n"},
    ],
)
def test_source_rejects_unknown_or_coercive_metadata(changes: dict[str, Any]) -> None:
    config, items = encode_source(_source())
    metadata = {**json.loads(config), **changes}
    _assert_public_error(lambda: decode_source(json.dumps(metadata), items))


@pytest.mark.parametrize(
    "items",
    [
        [],
        {},
        [{"type": "resource", "id": ""}],
        [{"type": "resource", "id": 4}],
        [{"type": "resource", "id": "resource\x7f"}],
        [{"type": "resource", "id": "x" * 2049}],
        [{"type": "resource", "id": "é" * 1025}],
        [{"type": "folder", "id": "folder-id"}],
        [{"type": "resource", "id": "one", "recursive": True}],
        [{"type": "resource", "id": "one"}] * 2,
        [{"type": "resource", "id": str(i)} for i in range(1001)],
    ],
)
def test_source_rejects_invalid_selection(items: Any) -> None:
    config, _ = encode_source(_source())
    _assert_public_error(lambda: decode_source(config, json.dumps(items)))


def test_source_allows_1000_distinct_opaque_ids_without_native_parsing() -> None:
    resources = tuple(ResourceIdentity(type="resource", id=f"opaque:{i}") for i in range(1000))
    source = _source(resources=resources)
    assert decode_source(*encode_source(source)) == source


@pytest.mark.parametrize(
    ("config", "items"), [("{", "[]"), ("[]", "[]"), ("{}", "["), ("{}", "null")]
)
def test_source_refuses_malformed_columns(config: str, items: str) -> None:
    _assert_public_error(lambda: decode_source(config, items))


def test_source_refuses_a_second_editable_selection_in_config() -> None:
    config, items = encode_source(_source())
    metadata = {**json.loads(config), "resources": []}
    _assert_public_error(lambda: decode_source(json.dumps(metadata), items))
