"""A namespace's profile schema: what it accepts, how a model's answer is held
to it, and when a built profile is behind its user's memories."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.core.errors import ValidationError
from server.memory.domain import profile_schema as ps
from server.memory.domain import trust
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


def _schema(**props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props}


_TZ = {"type": "string", "description": "IANA time zone"}
_DIET = {"type": "string", "enum": ["vegan", "omnivore"], "description": "diet"}
_AGE = {"type": "integer", "minimum": 0, "maximum": 150, "description": "age in years"}
_LANGS = {
    "type": "array",
    "items": {"type": "string", "maxLength": 20},
    "maxItems": 3,
    "description": "languages spoken",
}


def test_a_schema_lists_its_properties_in_order() -> None:
    props = ps.parse(_schema(timezone=_TZ, diet=_DIET, age=_AGE, languages=_LANGS))
    assert [(p.name, p.type, p.item_type) for p in props] == [
        ("timezone", "string", ""),
        ("diet", "string", ""),
        ("age", "integer", ""),
        ("languages", "array", "string"),
    ]
    assert props[1].enum == ("vegan", "omnivore")
    assert dict(props[2].constraints) == {"min_value": 0, "max_value": 150}
    assert props[3].max_items == 3


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        ([], "JSON object"),
        ({"type": "array", "properties": {"a": _TZ}}, '"object"'),
        ({"type": "object", "properties": {}}, "non-empty"),
        ({"type": "object", "properties": {"a": _TZ}, "required": ["a"]}, "required"),
        ({"type": "object", "properties": {"a": _TZ}, "additionalProperties": True}, "false"),
        (_schema(a={"type": "string"}), "description"),
        (_schema(a={"type": "string", "description": " "}), "description"),
        (_schema(a={"type": "date", "description": "d"}), "type must be"),
        (_schema(a={"type": "integer", "enum": ["1"], "description": "d"}), "enum"),
        (_schema(a={"type": "string", "enum": [], "description": "d"}), "enum"),
        (_schema(a={"type": "string", "pattern": "(", "description": "d"}), "pattern"),
        (_schema(a={"type": "string", "minimum": 1, "description": "d"}), "min_value"),
        (_schema(a={"type": "string", "default": "x", "description": "d"}), "default"),
        (_schema(a={"type": "array", "description": "d"}), "items"),
        (_schema(a={"type": "array", "items": {"type": "array"}, "description": "d"}), "type"),
        (_schema(a={**_LANGS, "maxItems": 500}), "maxItems"),
        (_schema(**{"bad-name": _TZ}), "property name"),
        (_schema(a={**_TZ, "description": "x" * 501}), "description longer"),
    ],
)
def test_a_bad_schema_is_refused_with_its_reason(schema: Any, reason: str) -> None:
    with pytest.raises(ValidationError) as e:
        ps.parse(schema)
    assert reason in e.value.message


def test_the_property_and_size_caps() -> None:
    ps.parse(_schema(**{f"p{i}": _TZ for i in range(ps.MAX_PROPERTIES)}))
    with pytest.raises(ValidationError, match="at most 64"):
        ps.parse(_schema(**{f"p{i}": _TZ for i in range(ps.MAX_PROPERTIES + 1)}))
    long = {"type": "string", "description": "d" * 400}
    with pytest.raises(ValidationError, match="16384 bytes"):
        ps.parse(_schema(**{f"p{i}": long for i in range(45)}))


def test_a_schema_whose_full_answer_is_too_long_is_refused() -> None:
    """One value per property and maxItems per array: past the bound, the answer
    would not fit the worker's output cap."""
    tags = {"type": "array", "items": {"type": "string"}, "description": "tags"}
    arrays = ps.MAX_ANSWER_VALUES // ps.MAX_ITEMS
    fits = {f"a{i}": tags for i in range(arrays)}
    fits.update({f"s{i}": _TZ for i in range(ps.MAX_ANSWER_VALUES - arrays * ps.MAX_ITEMS)})
    assert ps.answer_values(ps.parse(_schema(**fits))) == ps.MAX_ANSWER_VALUES
    with pytest.raises(ValidationError, match="lower maxItems"):
        ps.parse(_schema(**fits, extra=_TZ))
    with pytest.raises(ValidationError, match="lower maxItems"):
        ps.parse(_schema(**{f"p{i}": tags for i in range(ps.MAX_PROPERTIES)}))


def test_a_schema_whose_full_answer_may_take_too_many_tokens_is_refused() -> None:
    """A maxLength sizes its value in the worker's output cap, so the estimate is
    bounded; every schema with no maxLength is within the bound."""
    tags = {"type": "array", "items": {"type": "string"}, "description": "tags"}
    arrays = ps.MAX_ANSWER_VALUES // ps.MAX_ITEMS
    widest = {f"a{i:0>63}": tags for i in range(arrays)}
    widest.update({f"s{i:0>63}": _TZ for i in range(ps.MAX_ANSWER_VALUES - arrays * ps.MAX_ITEMS)})
    assert ps.answer_tokens(ps.parse(_schema(**widest))) <= ps.MAX_ANSWER_TOKENS
    long = {"type": "string", "description": "d", "maxLength": 2 * ps.MAX_ANSWER_TOKENS}
    with pytest.raises(ValidationError, match="lower maxLength"):
        ps.parse(_schema(summary=long))


def test_the_hash_follows_what_a_profile_depends_on() -> None:
    def sha(schema: dict[str, Any]) -> str:
        return ps.schema_sha256(ps.parse(schema))

    a = {"type": "object", "properties": {"x": _TZ, "y": _AGE}}
    # Key order, property order and titles change no profile.
    b = {"properties": {"y": dict(reversed(list(_AGE.items()))), "x": _TZ}, "type": "object"}
    titled = {**a, "title": "P", "properties": {"x": {**_TZ, "title": "Zone"}, "y": _AGE}}
    assert sha(a) == sha(b) == sha(titled)
    # A description, a type or a property does.
    assert sha(a) != sha(_schema(x=_TZ))
    assert sha(a) != sha(_schema(x={**_TZ, "description": "the zone"}, y=_AGE))


def test_the_stored_schema_keeps_the_admins_order() -> None:
    schema = _schema(zone=_TZ, age=_AGE)
    assert list(json.loads(ps.stored_form(schema))["properties"]) == ["zone", "age"]


def test_an_answer_is_held_to_the_schema() -> None:
    props = ps.parse(_schema(timezone=_TZ, diet=_DIET, age=_AGE, languages=_LANGS))
    profile, dropped = ps.conform(
        props,
        {
            "timezone": "Europe/Berlin",
            "diet": "carnivore",  # not in the enum
            "age": "41",  # coerced
            "languages": ["en", "de", "en", "x" * 30, "fr", "it"],
            "salary": 100,  # not declared
        },
    )
    assert profile == {
        "timezone": "Europe/Berlin",
        "diet": None,
        "age": 41,
        "languages": ["en", "de", "fr"],
    }
    assert dropped == 3  # the diet, the 30-character item, and `it` past maxItems


def test_a_value_out_of_bounds_or_of_the_wrong_shape_reads_null() -> None:
    props = ps.parse(
        _schema(age=_AGE, ok={"type": "boolean", "description": "d"}, tz=_TZ, langs=_LANGS)
    )
    profile, dropped = ps.conform(props, {"age": 200, "ok": "maybe", "tz": {"a": 1}, "langs": []})
    assert profile == {"age": None, "ok": None, "tz": None, "langs": None}
    assert dropped == 3
    assert ps.conform(props, "not an object") == (dict.fromkeys(profile), 0)


def test_a_stored_profile_takes_the_current_schema_shape() -> None:
    props = ps.parse(_schema(timezone=_TZ, pronouns={"type": "string", "description": "d"}))
    assert ps.project(props, {"timezone": "UTC", "diet": "vegan"}) == {
        "timezone": "UTC",
        "pronouns": None,
    }


def test_the_fake_model_fills_the_prompt_properties() -> None:
    import asyncio

    from server.llm import customer
    from server.llm.fake import FakeLLMClient
    from server.memory.workers import structured_profile as w

    props = ps.parse(_schema(timezone=_TZ, age=_AGE, languages=_LANGS))
    facts = w.render_facts(
        [
            {"value": "My timezone is UTC.", "since": "2026-01-01"},
            {"value": "My timezone is Europe/Berlin now.", "since": "2026-02-01"},
            {"value": "I speak these languages: en.", "since": "2026-01-03"},
        ]
    )

    async def ask() -> Any:
        with customer.for_org("o"):
            out = await FakeLLMClient().complete(system=w.system_prompt(props), user=facts)
        return w.parse_answer(out.text)

    assert asyncio.run(ask()) == {
        "timezone": "My timezone is Europe/Berlin now",
        "age": None,
        "languages": ["I speak these languages: en"],
    }


@pytest.mark.parametrize("text", ["[1]", "3", '"x"', "null", "not json"])
def test_an_answer_that_is_not_a_json_object_parses_to_none(text: str) -> None:
    from server.memory.workers import structured_profile as w

    assert w.parse_answer(text) is None


def test_the_prompt_fences_the_facts_and_names_each_property() -> None:
    from server.memory.workers import structured_profile as w

    prompt = w.system_prompt(ps.parse(_schema(diet=_DIET, languages=_LANGS)))
    assert '- "diet" (string, one of "vegan", "omnivore"): diet' in prompt
    assert '- "languages" (array of string, at most 20 characters): languages spoken' in prompt
    assert "<<memory" in prompt  # the fence note


def test_the_prompt_names_each_property_s_constraints() -> None:
    from server.memory.workers import structured_profile as w

    country = {"type": "string", "pattern": "^[A-Z]{2}$", "maxLength": 2, "description": "Country"}
    age = {"type": "integer", "minimum": 0, "maximum": 130, "description": "Age"}
    prompt = w.system_prompt(ps.parse(_schema(country=country, age=age)))
    assert '- "country" (string, pattern ^[A-Z]{2}$, at most 2 characters): Country' in prompt
    assert '- "age" (integer, 0 to 130): Age' in prompt


# --- staleness, on clickhouse-local against the real tables ------------------


_needs_ch = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


class _LocalCH:
    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = run(self.setup, sql, params)
        return rows[0] if rows else None


# Fixed times: each check replays its setup, so a `now()` there would move between runs.
_T0 = "toDateTime('2026-01-01 00:00:00')"
_JAN2 = "toDateTime('2026-01-02 00:00:00')"
_JAN3 = "toDateTime('2026-01-03 00:00:00')"
_JAN4 = "toDateTime('2026-01-04 00:00:00')"


def _fact(mid: str, user: str, key: str, sha: str, version: int = 1, **cols: str) -> str:
    """One version of a fact; `cols` are SQL literals."""
    # Written at _T0 unless `updated_at` says otherwise: a later version with the same
    # `updated_at` is a recall or a backfill, one with `now()` a restatement.
    cols = {"memory_type": "'semantic'", "updated_at": _T0, **cols}
    extra = "".join(f", {k}" for k in cols)
    vals = "".join(f", {v}" for v in cols.values())
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        f"value, memory_id, content_sha256, version{extra}) VALUES ('o', 'n', 'a', "
        f"'{user}', '-', '{key}', 'v', '{mid}', '{sha * 64}', {version}{vals})"
    )


def _profile(user: str, tags: list[str], **cols: str) -> str:
    """A profile the worker built an hour ago; `cols` are SQL literals."""
    from server.memory.store.profile_sources import WORKER_ACTOR

    cols = {"updated_at": "now() - 3600", "actor": f"'{WORKER_ACTOR}'", **cols}
    extra = "".join(f", {k}" for k in cols)
    vals = "".join(f", {v}" for v in cols.values())
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        f"memory_type, value, version, tags{extra}) VALUES ('o', 'n', '-', '{user}', '-', "
        f"'structured_profile', 'profile', '{{}}', 1, {tags!r}{vals})"
    )


_A, _B, _C = (f"00000000-0000-0000-0000-00000000000{i}" for i in "abc")
_SHA = "s" * 64


async def _behind(
    ch: _LocalCH,
    sha: str = _SHA,
    cool: int = 0,
    limit: int = 10,
    refused: dict[str, tuple[str, int]] | None = None,
) -> list[str]:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import profile_sources as src

    behind = await src.users_behind(
        org="o",
        namespace="n",
        label="structured_profile",
        schema_sha256=sha,
        cooldown_seconds=cool,
        limit=limit,
        refused=refused,
        timeout_seconds=60,
        max_memory_bytes=4 * 1024**3,
        scope=WHOLE_NAMESPACE,
        ch=ch,
    )
    return [b.user_id for b in behind]


async def _digest(ch: _LocalCH, user: str) -> tuple[str, int]:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import profile_sources as src

    return await src.sources_digest(
        org="o", namespace="n", user_id=user, scope=WHOLE_NAMESPACE, ch=ch
    )


async def _profiles_for(setup: list[str], users: list[str], **cols: str) -> list[str]:
    """Each of `users`' profile, built from `setup` and current."""
    from server.memory.store.profile_sources import profile_tags

    out = []
    for user in users:
        digest, count = await _digest(_LocalCH(*setup), user)
        out.append(_profile(user, profile_tags(_SHA, digest, count), **cols))
    return out


async def _built(*facts: str, user: str = "u1", **cols: str) -> list[str]:
    """The setup with `user`'s profile built from `facts`, current."""
    setup = [ddl("memory"), *facts]
    return [*setup, *await _profiles_for(setup, [user], **cols)]


@_needs_ch
@pytest.mark.asyncio
async def test_a_profile_is_behind_its_users_memories_until_rebuilt() -> None:
    facts = [_fact(_A, "u1", "tz", "a"), _fact(_B, "u2", "tz", "b")]
    # No profile yet: both users are behind.
    assert await _behind(_LocalCH(ddl("memory"), *facts)) == ["u1", "u2"]

    built = await _built(*facts)
    digest, count = await _digest(_LocalCH(*built), "u1")
    assert len(digest) == 16 and count == 1
    assert await _behind(_LocalCH(*built)) == ["u2"]
    # A recall or a backfill writes a new version that keeps `updated_at`: still current.
    touched = [*built, _fact(_A, "u1", "tz", "a", version=2)]
    assert await _behind(_LocalCH(*touched)) == ["u2"]
    # A restatement moves the fact to the newest place the model reads: one rebuild.
    restated = [*built, _fact(_A, "u1", "tz", "a", version=2, updated_at="now()")]
    assert await _behind(_LocalCH(*restated)) == ["u2", "u1"]
    # An edit puts it behind, after the user with no profile; within the cooldown it waits.
    edited = [*built, _fact(_A, "u1", "tz", "c", version=2)]
    assert await _behind(_LocalCH(*edited)) == ["u2", "u1"]
    assert await _behind(_LocalCH(*edited), cool=7200) == ["u2"]
    # A deletion (fewer sources than the build read) does not wait.
    deleted = [*built, _fact(_A, "u1", "tz", "a", version=2, tombstoned_at="now()")]
    assert await _behind(_LocalCH(*deleted), cool=7200) == ["u2", "u1"]
    # A schema change puts every profile behind at once, cooldown or not.
    assert await _behind(_LocalCH(*built), sha="t" * 64, cool=7200) == ["u2", "u1"]


@_needs_ch
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"sensitivity": "'high'"},
        {"memory_type": "'episodic'"},
        {"tags": "['about:other']"},
        {"tombstoned_at": "now()"},
        {"expires_at": "now() - 60"},
        {"valid_to": "now() - 60"},
        {"status": "'superseded'"},
        {"status": "'duplicate'"},
    ],
)
async def test_a_source_that_stops_being_one_brings_the_user_back(change: dict[str, str]) -> None:
    built = await _built(_fact(_A, "u1", "tz", "a"))
    assert await _behind(_LocalCH(*built)) == []
    assert await _behind(_LocalCH(*built, _fact(_A, "u1", "tz", "a", version=2, **change))) == [
        "u1"
    ]


@_needs_ch
@pytest.mark.asyncio
async def test_a_revert_after_a_rebuild_brings_the_user_back() -> None:
    # A -> B, rebuilt under B, then back to A.
    a = _fact(_A, "u1", "tz", "a")
    b = _fact(_A, "u1", "tz", "b", version=2)
    built = await _built(a, b)
    assert await _behind(_LocalCH(*built)) == []
    assert await _behind(_LocalCH(*built, _fact(_A, "u1", "tz", "a", version=3))) == ["u1"]


@_needs_ch
@pytest.mark.asyncio
async def test_a_restored_fact_brings_the_user_back() -> None:
    live = _fact(_A, "u1", "tz", "a")
    deleted = _fact(_A, "u1", "tz", "a", version=2, tombstoned_at="now() - 7200")
    built = await _built(live, deleted, _fact(_B, "u1", "lang", "b"))
    assert await _behind(_LocalCH(*built)) == []
    restored = _fact(_A, "u1", "tz", "a", version=3)
    assert await _behind(_LocalCH(*built, restored)) == ["u1"]


@_needs_ch
@pytest.mark.asyncio
async def test_writes_that_are_no_source_never_crowd_out_an_edit() -> None:
    users = [f"r{i:02d}" for i in range(15)]
    mids = [f"00000000-0000-0000-0001-0000000000{i:02d}" for i in range(15)]
    facts = [_fact(m, u, "tz", "a") for m, u in zip(mids, users, strict=True)]
    setup = [ddl("memory"), *facts, _fact(_A, "u1", "tz", "b")]
    profiles = await _profiles_for(setup, [*users, "u1"])
    # Since the build: episodic rows, a profile section, a status change on a
    # non-source row, a restatement; and one real edit by u1.
    noise = []
    for i, (m, u) in enumerate(zip(mids, users, strict=True)):
        noise.append(
            _fact(
                f"00000000-0000-0000-0002-0000000000{i:02d}", u, "ep", "e", memory_type="'episodic'"
            )
        )
        noise.append(
            _fact(
                f"00000000-0000-0000-0003-0000000000{i:02d}",
                u,
                "persona",
                "p",
                memory_type="'profile'",
            )
        )
        noise.append(_fact(m, u, "tz", "a", version=2))  # a recall
    edited = _fact(_A, "u1", "tz", "c", version=2)
    ch = _LocalCH(*setup, *profiles, *noise, edited)
    assert await _behind(ch, limit=5) == ["u1"]


@_needs_ch
@pytest.mark.asyncio
async def test_a_refused_user_sorts_last_and_waits_until_their_digest_changes() -> None:
    facts = [_fact(_A, "u1", "tz", "a"), _fact(_B, "u2", "tz", "b")]
    built = [ddl("memory"), *facts, *await _profiles_for([ddl("memory"), *facts], ["u1", "u2"])]
    edited = [*built, _fact(_A, "u1", "tz", "c", version=2), _fact(_B, "u2", "tz", "d", version=2)]
    digest, _ = await _digest(_LocalCH(*edited), "u1")
    at = {"u1": (digest, 1)}  # the digest and the profile row version the refusal saw
    assert await _behind(_LocalCH(*edited), limit=1, refused=at) == ["u2"]
    assert await _behind(_LocalCH(*edited), refused=at) == ["u2"]
    again = [*edited, _fact(_A, "u1", "tz", "e", version=3)]
    assert await _behind(_LocalCH(*again), refused=at) == ["u2", "u1"]


@_needs_ch
@pytest.mark.asyncio
async def test_an_unpinned_or_unlocked_profile_is_rebuilt() -> None:
    from server.memory.store.profile_sources import profile_tags

    setup = [ddl("memory"), _fact(_A, "u1", "tz", "a")]
    pinned = await _profiles_for(setup, ["u1"], pinned="1")
    edited = [*setup, *pinned, _fact(_A, "u1", "tz", "c", version=2)]
    digest, _ = await _digest(_LocalCH(*edited), "u1")
    at = {"u1": (digest, 1)}
    assert await _behind(_LocalCH(*edited), refused=at) == []
    # The unpin (or unlock) writes a new version of the profile row: the refusal no
    # longer holds, though the digest is the same.
    old_digest, count = await _digest(_LocalCH(*setup), "u1")
    unpinned = _profile("u1", profile_tags(_SHA, old_digest, count)).replace(
        "'profile', '{}', 1,", "'profile', '{}', 2,"
    )
    assert await _behind(_LocalCH(*edited, unpinned), refused=at) == ["u1"]


@_needs_ch
@pytest.mark.asyncio
async def test_an_unpin_of_a_redacted_profile_ends_its_refusal() -> None:
    from server.memory.store.profile_sources import profile_tags

    # A redact scrubbed the pinned head: it reads as no profile, and the build is
    # refused at the head's version.
    setup = [ddl("memory"), _fact(_A, "u1", "tz", "a")]
    gone = {"pinned": "1", "redacted_at": "now()"}
    redacted = await _profiles_for(setup, ["u1"], **gone)
    digest, count = await _digest(_LocalCH(*setup), "u1")
    at = {"u1": (digest, 1)}
    assert await _behind(_LocalCH(*setup, *redacted), refused=at) == []
    # The unpin copies the redaction forward but moves the head to version 2.
    unpinned = _profile("u1", profile_tags(_SHA, digest, count), redacted_at="now()").replace(
        "'profile', '{}', 1,", "'profile', '{}', 2,"
    )
    assert await _behind(_LocalCH(*setup, *redacted, unpinned), refused=at) == ["u1"]


@_needs_ch
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gone",
    [{"expires_at": "now() - 60"}, {"redacted_at": "now()"}, {"sensitivity": "'high'"}],
)
async def test_an_expired_redacted_or_high_profile_is_no_profile(gone: dict[str, str]) -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import profile_sources as src

    built = await _built(_fact(_A, "u1", "tz", "a"), **gone)
    ch = _LocalCH(*built)
    key = {
        "org": "o",
        "namespace": "n",
        "user_id": "u1",
        "label": "structured_profile",
        "scope": WHOLE_NAMESPACE,
        "ch": ch,
    }
    assert await src.profile_row(**key) is None
    assert await _behind(ch) == ["u1"]
    # The head still carries its label, so the worker holds a high one instead of
    # writing over it (test_a_high_profile_keeps_its_label).
    head = await src.profile_head_hold(**key)
    assert head is not None and (head["sensitivity"] == "high") == ("sensitivity" in gone)


@_needs_ch
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor",
    [
        trust.actor_for("alice", is_service_account=False),
        # A service account named after the worker still writes as a `process` actor.
        trust.actor_for("structured-profile-worker", is_service_account=True),
        trust.actor_for("structured-profile", is_service_account=True),
    ],
)
async def test_a_profile_row_a_caller_wrote_is_no_profile(actor: str) -> None:
    """A writer can put the worker's tags on its own row of the profile key; only a
    row the worker wrote reads as the user's profile."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import profile_sources as src

    built = await _built(_fact(_A, "u1", "tz", "a"), actor=f"'{actor}'")
    ch = _LocalCH(*built)
    key = {
        "org": "o",
        "namespace": "n",
        "user_id": "u1",
        "label": "structured_profile",
        "scope": WHOLE_NAMESPACE,
        "ch": ch,
    }
    assert await src.profile_row(**key) is None
    assert await _behind(ch) == ["u1"]


@_needs_ch
@pytest.mark.asyncio
async def test_a_high_row_or_a_fact_about_someone_else_is_no_source() -> None:
    base = [ddl("memory"), _fact(_A, "u1", "tz", "a")]
    digest = await _digest(_LocalCH(*base), "u1")
    high = [*base, _fact(_B, "u1", "door", "b", sensitivity="'high'")]
    other = [*base, _fact(_C, "u1", "boss_tz", "c", tags="['about:other']")]
    assert await _digest(_LocalCH(*high), "u1") == digest
    assert await _digest(_LocalCH(*other), "u1") == digest


@_needs_ch
@pytest.mark.asyncio
async def test_a_user_whose_memories_are_gone_is_rebuilt_empty() -> None:
    from server.memory.store.profile_sources import profile_tags

    deleted = _fact(_A, "u1", "tz", "a", tombstoned_at="now()")
    built = [ddl("memory"), deleted, _profile("u1", profile_tags(_SHA, "0123456789abcdef", 1))]
    assert await _digest(_LocalCH(*built), "u1") == ("", 0)
    assert await _behind(_LocalCH(*built), cool=7200) == ["u1"]
    emptied = [ddl("memory"), deleted, _profile("u1", profile_tags(_SHA, "", 0))]
    assert await _behind(_LocalCH(*emptied)) == []


@_needs_ch
@pytest.mark.asyncio
async def test_a_profile_built_short_is_behind_with_sources_or_none() -> None:
    # A build that read fewer facts than it counted carries SHORT, which no digest takes:
    # not the live facts', nor the empty one of a user whose facts are all gone.
    from server.memory.store.profile_sources import SHORT, profile_tags

    live = _fact(_A, "u1", "tz", "a")
    deleted = _fact(_A, "u1", "tz", "a", tombstoned_at="now()")
    # As many facts as it counted (a fact behind its delivery): behind, after the cooldown.
    short = _profile("u1", profile_tags(_SHA, SHORT, 1))
    assert await _behind(_LocalCH(ddl("memory"), live, short)) == ["u1"]
    assert await _behind(_LocalCH(ddl("memory"), live, short), cool=7200) == []
    # Every fact gone since: behind at once, as any profile left with fewer sources.
    assert await _behind(_LocalCH(ddl("memory"), deleted, short), cool=7200) == ["u1"]


def test_a_number_that_is_not_finite_or_exact_reads_null() -> None:
    props = ps.parse(
        _schema(n={"type": "number", "description": "d"}, i={"type": "integer", "description": "d"})
    )
    for bad in ("NaN", "inf", float("inf"), float("nan"), "1e999", True):
        assert ps.conform(props, {"n": bad, "i": bad}) == ({"n": None, "i": None}, 2)
    assert ps.conform(props, {"n": 1.5, "i": 2**60}) == ({"n": 1.5, "i": None}, 1)
    assert ps.conform(props, {"n": "-2", "i": "7"}) == ({"n": -2.0, "i": 7}, 0)


def test_a_title_is_accepted_and_ignored_at_every_level() -> None:
    props = ps.parse(
        {
            "type": "object",
            "title": "Profile",
            "properties": {
                "tz": {**_TZ, "title": "Time zone"},
                "langs": {
                    **_LANGS,
                    "title": "Languages",
                    "items": {"type": "string", "title": "L"},
                },
            },
        }
    )
    assert [p.name for p in props] == ["tz", "langs"]


def test_facts_of_one_day_keep_their_order_by_time() -> None:
    from server.memory.domain.profile_facts import render_fold_input

    rows = [
        {"value": "Prefers tea now.", "since": "2026-03-01 18:00:00"},
        {"value": "Prefers coffee.", "since": "2026-03-01 09:00:00"},
    ]
    assert render_fold_input(rows).splitlines() == [
        "- [2026-03-01] Prefers coffee.",
        "- [2026-03-01] Prefers tea now.",
    ]


@_needs_ch
@pytest.mark.asyncio
async def test_the_schema_listing_skips_deactivated_namespaces_and_wraps_after_a_cursor() -> None:
    from server.memory.store import profile_schemas

    def schema(org: str, ns: str) -> str:
        return (
            "INSERT INTO loom.profile_schemas (org, namespace, schema_json, schema_sha256, "
            f"updated_at, version) VALUES ('{org}', '{ns}', '{ps.stored_form(_schema(tz=_TZ))}', "
            "'h', now(), 1)"
        )

    ch = _LocalCH(
        ddl("profile_schemas"),
        ddl("namespaces"),
        schema("o1", "a"),
        schema("o1", "b"),
        schema("o1", "off"),
        schema("o2", "a"),
        schema("o3", "a"),
        "INSERT INTO loom.namespaces (org, namespace, owner_email, active) "
        "VALUES ('o1', 'off', 'x', false)",
    )

    async def names(**kw: Any) -> list[tuple[str, str]]:
        rows = await profile_schemas.list_active(ch=ch, **kw)
        assert all(ps.parse(json.loads(r["schema_json"]))[0].name == "tz" for r in rows)
        return [(r["org"], r["namespace"]) for r in rows]

    everything = [("o1", "a"), ("o1", "b"), ("o2", "a"), ("o3", "a")]
    assert await names(exclude_orgs=[], after=None, limit=10) == everything
    assert await names(exclude_orgs=["o2"], after=None, limit=10) == [
        ("o1", "a"),
        ("o1", "b"),
        ("o3", "a"),
    ]
    assert await names(exclude_orgs=[], after=("o1", "b"), limit=3) == [
        ("o2", "a"),
        ("o3", "a"),
        ("o1", "a"),
    ]
    assert await names(exclude_orgs=[], after=None, limit=10, only=("o2", "a")) == [("o2", "a")]


@pytest.mark.asyncio
async def test_set_and_clear_write_every_column_of_profile_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewrite under FINAL serves the newest row whole: each writer carries every column."""
    from server.memory.api.models_profile import ProfileSchemaInput, ProfileSchemaSetInput
    from server.memory.app import profile_schema as app
    from server.memory.store import profile_schemas
    from tests.unit.schema_ddl import column_names

    written: list[dict[str, Any]] = []

    async def insert_row(row: dict[str, Any], **kw: Any) -> None:
        written.append(row)

    async def get(org: str, namespace: str, **kw: Any) -> dict[str, Any]:
        return written[-1]

    monkeypatch.setattr(profile_schemas, "insert_row", insert_row)
    monkeypatch.setattr(profile_schemas, "get", get)
    await app.handle_profile_schema_set(
        ProfileSchemaSetInput(org="o", namespace="n", json_schema=_schema(tz=_TZ))
    )
    cleared = await app.handle_profile_schema_clear(ProfileSchemaInput(org="o", namespace="n"))
    assert cleared.found and cleared.json_schema == _schema(tz=_TZ)
    assert written[1]["tombstoned_at"] == written[1]["updated_at"]
    for row in written:
        assert sorted(row) == sorted(column_names("profile_schemas"))


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["{not json", json.dumps({"type": "array"})])
async def test_a_stored_schema_that_no_longer_parses_reads_as_no_schema(
    monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    from server.memory.api.models_profile import ProfileStructuredInput
    from server.memory.app import profile_schema as app
    from server.memory.store import profile_schemas

    async def get(org: str, namespace: str, **kw: Any) -> dict[str, Any]:
        return {"schema_json": stored, "schema_sha256": "h"}

    monkeypatch.setattr(profile_schemas, "get", get)
    out = await app.handle_profile_structured(
        ProfileStructuredInput(org="o", namespace="n", agent="a", user_id="u1")
    )
    assert out.status == "no_schema" and out.profile == {}


@_needs_ch
@pytest.mark.asyncio
async def test_every_digest_is_16_hex_characters() -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import profile_sources as src

    users = [f"u{i:02d}" for i in range(64)]
    facts = [
        _fact(f"00000000-0000-0000-0004-0000000000{i:02d}", u, "tz", "a")
        for i, u in enumerate(users)
    ]
    behind = await src.users_behind(
        org="o",
        namespace="n",
        label="structured_profile",
        schema_sha256="0" * 64,
        cooldown_seconds=0,
        limit=len(users),
        timeout_seconds=60,
        max_memory_bytes=4 * 1024**3,
        scope=WHOLE_NAMESPACE,
        ch=_LocalCH(ddl("memory"), *facts),
    )
    digests = [b.digest for b in behind]
    assert len(digests) == 64 and all(len(d) == 16 and int(d, 16) >= 0 for d in digests)
    # `hex` drops leading zeros: the padding is what keeps these 16 long.
    assert any(d.startswith("0") for d in digests)


def _dated(
    mid: str, value: str, created: str, updated: str, version: int = 1, sensitivity: str = ""
) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, memory_id, content_sha256, version, created_at, updated_at, "
        f"sensitivity) VALUES ('o', 'n', 'a', 'u1', '-', 'k{mid[-1]}', 'semantic', "
        f"'\"{value}\"', '{mid}', '{value[0] * 64}', {version}, {created}, {updated}, "
        f"'{sensitivity}')"
    )


@_needs_ch
@pytest.mark.asyncio
async def test_a_fact_edited_last_is_read_last() -> None:
    """Berlin, then Tokyo, then the Berlin fact edited to Paris: the model must read
    Paris last, though its row was created first."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import profile_sources as src
    from server.memory.workers.structured_profile import render_facts

    ch = _LocalCH(
        ddl("memory"),
        _dated(_A, "My timezone is Berlin.", _JAN2, _JAN2),
        _dated(_B, "My timezone is Tokyo.", _JAN3, _JAN3),
        _dated(_A, "My timezone is Paris.", _JAN2, _JAN4, version=2),
    )
    rows = await src.user_facts(
        org="o",
        namespace="n",
        user_id="u1",
        types=["semantic"],
        limit=10,
        scope=WHOLE_NAMESPACE,
        ch=ch,
    )
    assert [r["value"] for r in rows] == ['"My timezone is Paris."', '"My timezone is Tokyo."']
    assert render_facts(rows).splitlines()[-1].endswith("My timezone is Paris.")


@pytest.fixture
def local_store(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the profile stores at a clickhouse-local setup the test passes in."""
    from server.memory.store import profile_sources as src

    def use(*setup: str) -> _LocalCH:
        ch = _LocalCH(*setup)
        monkeypatch.setattr(src, "_default_client", lambda: ch)
        return ch

    return use


@_needs_ch
@pytest.mark.asyncio
@pytest.mark.parametrize(("cooldown", "rebuilt"), [(7200, False), (0, True)])
async def test_an_edit_waits_out_the_configured_cooldown(
    cooldown: int, rebuilt: bool, local_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.store import profile_schemas
    from server.memory.workers import structured_profile as w
    from server.settings import settings

    built = await _built(_fact(_A, "u1", "tz", "a"))
    local_store(*built, _fact(_A, "u1", "tz", "c", version=2))
    monkeypatch.setattr(settings, "structured_profile_cooldown_seconds", cooldown)

    async def list_active(**kw: Any) -> list[dict[str, Any]]:
        schema = ps.stored_form(_schema(tz=_TZ))
        return [{"org": "o", "namespace": "n", "schema_json": schema, "schema_sha256": _SHA}]

    async def no_switch() -> set[str]:
        return set()

    built_users: list[str] = []

    async def build(schema: Any, user_id: str) -> int:
        built_users.append(user_id)
        return 0

    monkeypatch.setattr(profile_schemas, "list_active", list_active)
    monkeypatch.setattr(w.customer, "switched_off_orgs", no_switch)
    monkeypatch.setattr(w, "build_profile", build)
    w._scans.clear()
    await w.run_pass()
    w._scans.clear()
    assert built_users == (["u1"] if rebuilt else [])


@_needs_ch
@pytest.mark.asyncio
async def test_a_build_reads_at_most_the_configured_number_of_facts(
    local_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import profile_schema as schema_app
    from server.memory.workers import structured_profile as w
    from server.settings import settings

    local_store(
        ddl("memory"),
        _dated(_A, "My timezone is Berlin.", _JAN2, _JAN2),
        _dated(_B, "My timezone is Tokyo.", _JAN3, _JAN3),
    )
    monkeypatch.setattr(settings, "structured_profile_memory_limit", 1)
    read: list[str] = []

    async def ask(org: str, ns: str, props: Any, facts: str) -> dict[str, Any]:
        read.append(facts)
        return {}

    async def write(**kw: Any) -> None:
        return None

    monkeypatch.setattr(w, "_ask", ask)
    monkeypatch.setattr(schema_app, "write_structured_profile", write)
    schema = w.ActiveSchema("o", "n", ps.parse(_schema(tz=_TZ)), _SHA)
    assert await w.build_profile(schema, "u1") == 0
    assert read == ["- [2026-01-03] My timezone is Tokyo."]  # _JAN3


@_needs_ch
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label",
    # an import leaves a row unlabelled until the sweep; a writer's explicit `low` sets the
    # stored label, but the build still checks the value
    ["", "low"],
    ids=["unlabelled-pin", "low-labelled-pin"],
)
async def test_a_fact_reading_as_high_never_reaches_the_profile_model(
    local_store: Any, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    from server.memory.app import profile_schema as schema_app
    from server.memory.workers import structured_profile as w

    local_store(
        ddl("memory"),
        _dated(_A, "My timezone is Berlin.", _JAN2, _JAN2),
        _dated(_B, "badge PIN is 4471", _JAN3, _JAN3, sensitivity=label),
    )
    read: list[str] = []

    async def ask(org: str, ns: str, props: Any, facts: str) -> dict[str, Any]:
        read.append(facts)
        return {}

    async def write(**kw: Any) -> None:
        return None

    monkeypatch.setattr(w, "_ask", ask)
    monkeypatch.setattr(schema_app, "write_structured_profile", write)
    schema = w.ActiveSchema("o", "n", ps.parse(_schema(tz=_TZ)), _SHA)
    assert await w.build_profile(schema, "u1") == 0
    assert read == ["- [2026-01-02] My timezone is Berlin."]


@_needs_ch
@pytest.mark.asyncio
async def test_restating_an_older_fact_makes_it_the_latest_the_build_reads() -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import profile_sources as src
    from server.memory.workers.structured_profile import render_facts

    berlin = _dated(_A, "My timezone is Berlin.", _JAN2, _JAN2)
    tokyo = _dated(_B, "My timezone is Tokyo.", _JAN3, _JAN3)
    built = [*await _built(berlin, tokyo)]
    assert await _behind(_LocalCH(*built)) == []
    restated = _dated(_A, "My timezone is Berlin.", _JAN2, "now()", version=2)
    ch = _LocalCH(*built, restated)
    assert await _behind(ch) == ["u1"]
    rows = await src.user_facts(
        org="o",
        namespace="n",
        user_id="u1",
        types=["semantic"],
        limit=10,
        scope=WHOLE_NAMESPACE,
        ch=ch,
    )
    assert render_facts(rows).splitlines()[-1].endswith("My timezone is Berlin.")


def _active() -> Any:
    from server.memory.workers import structured_profile as w

    return w.ActiveSchema("o", "n", ps.parse(_schema(tz=_TZ)), _SHA)


@_needs_ch
@pytest.mark.asyncio
async def test_the_scan_runs_under_the_configured_memory_cap(
    local_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.workers import structured_profile as w
    from server.settings import settings

    local_store(*await _built(_fact(_A, "u1", "tz", "a")))
    monkeypatch.setattr(settings, "structured_profile_scan_max_memory_bytes", 1024)
    w._namespace_backoff.clear()
    result = w.StructuredProfileResult()
    assert await w._scan(_active(), result, 1) is None
    # Past the cap the scan fails, is counted, and the namespace backs off.
    assert result.errors == 1 and ("o", "n", _SHA) in w.namespaces_waiting()
    w._namespace_backoff.clear()


@pytest.mark.asyncio
async def test_the_scan_runs_under_the_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import ch as core_ch
    from server.memory.store import profile_sources as src
    from server.memory.workers import structured_profile as w
    from server.settings import settings

    seen: list[float | None] = []

    class _Timed:
        async def query_all(self, sql: str, params: Any = None) -> list[Any]:
            seen.append(core_ch.statement_timeout_var.get())
            return []

    monkeypatch.setattr(src, "_default_client", lambda: _Timed())
    monkeypatch.setattr(settings, "structured_profile_scan_timeout_seconds", 7)
    w._scans.pop(("o", "n"), None)
    await w._scan(_active(), w.StructuredProfileResult(), 1)
    w._scans.pop(("o", "n"), None)
    assert seen == [7]


@pytest.mark.parametrize(
    "below_floor",
    [
        {"structured_profile_interval_seconds": 0},
        {"structured_profile_idle_interval_seconds": 0},
        {"structured_profile_scan_timeout_seconds": 0},
        {"structured_profile_scan_max_memory_bytes": 1000},
        {"structured_profile_max_users_per_pass": 0},
        {"structured_profile_memory_limit": 0},
        {"structured_profile_cooldown_seconds": -1},
        {"structured_profile_backoff_base_seconds": 0},
        {"structured_profile_backoff_cap_seconds": 0},
        {"structured_profile_refusal_retry_seconds": 0},
        {"structured_profile_scan_max_age_seconds": 0},
    ],
)
def test_a_structured_profile_setting_below_its_floor_is_refused(
    below_floor: dict[str, int],
) -> None:
    from pydantic import ValidationError as PydanticError

    from server.settings.catalog import MemorySettings

    with pytest.raises(PydanticError):
        MemorySettings(**below_floor)


@_needs_ch
@pytest.mark.asyncio
async def test_the_listing_leaves_out_waiting_namespaces_before_its_limit() -> None:
    from server.memory.store import profile_schemas

    def schema(org: str, ns: str, sha: str = "h") -> str:
        return (
            "INSERT INTO loom.profile_schemas (org, namespace, schema_json, schema_sha256, "
            f"updated_at, version) VALUES ('{org}', '{ns}', '{{}}', '{sha}', now(), 1)"
        )

    ch = _LocalCH(ddl("profile_schemas"), ddl("namespaces"), schema("o1", "a"), schema("o1", "b"))
    rows = await profile_schemas.list_active(
        exclude_orgs=[], after=None, limit=1, waiting=[("o1", "a", "h")], ch=ch
    )
    assert [(r["org"], r["namespace"]) for r in rows] == [("o1", "b")]
    # A wait recorded under another schema hash no longer holds.
    rows = await profile_schemas.list_active(
        exclude_orgs=[], after=None, limit=1, waiting=[("o1", "a", "old")], ch=ch
    )
    assert [(r["org"], r["namespace"]) for r in rows] == [("o1", "a")]


@pytest.mark.asyncio
@pytest.mark.parametrize("narrowed", [False, True])
async def test_a_narrowed_token_reads_the_schema_but_not_who_set_it(
    monkeypatch: pytest.MonkeyPatch, narrowed: bool
) -> None:
    from server.memory.api.models_profile import ProfileSchemaInput
    from server.memory.app import profile_schema as schema_app

    stored = {
        "schema_json": json.dumps(_schema(tz=_TZ)),
        "schema_sha256": _SHA,
        "updated_by": "human:admin",
        "updated_at": None,
    }

    async def get(org: str, namespace: str) -> dict[str, Any]:
        return stored

    monkeypatch.setattr(schema_app.repo, "get", get)
    monkeypatch.setattr(schema_app, "request_narrowed", lambda: narrowed)
    out = await schema_app.handle_profile_schema_get(ProfileSchemaInput(org="o", namespace="n"))
    assert out.found and out.schema_sha256 == _SHA
    assert out.updated_by == ("" if narrowed else "human:admin")
