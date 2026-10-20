"""A namespace's profile schema: what it accepts, how a model's answer is held
to it, and when a built profile is behind its user's memories."""

from __future__ import annotations

from typing import Any

import pytest

from server.core.errors import ValidationError
from server.memory.domain import profile_schema as ps


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


def test_one_schema_has_one_hash_whatever_its_key_order() -> None:
    a = {"type": "object", "properties": {"x": _TZ, "y": _AGE}}
    b = {"properties": {"y": dict(reversed(list(_AGE.items()))), "x": _TZ}, "type": "object"}
    assert ps.schema_sha256(a) == ps.schema_sha256(b)
    assert ps.schema_sha256(a) != ps.schema_sha256(_schema(x=_TZ))


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
    assert dropped == 2  # the diet and the 30-character item


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


def test_the_prompt_fences_the_facts_and_names_each_property() -> None:
    from server.memory.workers import structured_profile as w

    prompt = w.system_prompt(ps.parse(_schema(diet=_DIET, languages=_LANGS)))
    assert '- "diet" (string (one of "vegan", "omnivore")): diet' in prompt
    assert '- "languages" (array of string): languages spoken' in prompt
    assert "<<memory" in prompt  # the fence note


# --- staleness, on clickhouse-local against the real tables ------------------

from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run  # noqa: E402

_needs_ch = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


class _LocalCH:
    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = run(self.setup, sql, params)
        return rows[0] if rows else None


def _fact(mid: str, user: str, key: str, sha: str, version: int = 1, **cols: str) -> str:
    extra = "".join(f", {k}" for k in cols)
    vals = "".join(f", {v}" for v in cols.values())  # SQL literals
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        f"memory_type, value, memory_id, content_sha256, version{extra}) VALUES ('o', 'n', 'a', "
        f"'{user}', '-', '{key}', 'semantic', 'v', '{mid}', '{sha * 64}', {version}{vals})"
    )


def _profile(user: str, tags: list[str]) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        f"memory_type, value, version, tags, updated_at) VALUES ('o', 'n', '-', '{user}', '-', "
        f"'structured_profile', 'profile', '{{}}', 1, {tags!r}, now() - 3600)"
    )


_A, _B, _C = (f"00000000-0000-0000-0000-00000000000{i}" for i in "abc")


async def _behind(ch: _LocalCH, sha: str = "s" * 64, cool: int = 0) -> list[str]:
    from server.memory.store import profile_sources as src

    return await src.users_behind(
        org="o",
        namespace="n",
        label="structured_profile",
        schema_sha256=sha,
        cooldown_seconds=cool,
        limit=10,
        candidates=10,
        ch=ch,
    )


async def _digest(ch: _LocalCH, user: str) -> str:
    from server.memory.store import profile_sources as src

    return await src.sources_digest(org="o", namespace="n", user_id=user, ch=ch)


@_needs_ch
@pytest.mark.asyncio
async def test_a_profile_is_behind_its_users_memories_until_rebuilt() -> None:
    from server.memory.store.profile_sources import profile_tags

    facts = [ddl("memory"), _fact(_A, "u1", "tz", "a"), _fact(_B, "u2", "tz", "b")]
    # No profile yet: both users are behind.
    assert await _behind(_LocalCH(*facts)) == ["u1", "u2"]

    digest = await _digest(_LocalCH(*facts), "u1")
    assert len(digest) == 16
    built = [*facts, _profile("u1", profile_tags("s" * 64, digest))]
    assert await _behind(_LocalCH(*built)) == ["u2"]
    # A recall or a reinforcement writes a new version with the same content: still current.
    touched = [*built, _fact(_A, "u1", "tz", "a", version=2)]
    assert await _digest(_LocalCH(*touched), "u1") == digest
    assert await _behind(_LocalCH(*touched)) == ["u2"]
    # An edit, or a new fact, puts it behind (after the user with none); within
    # the cooldown it waits.
    edited = [*built, _fact(_A, "u1", "tz", "c", version=2)]
    assert await _behind(_LocalCH(*edited)) == ["u2", "u1"]
    assert await _behind(_LocalCH(*edited), cool=7200) == ["u2"]
    # A schema change puts it behind at once, cooldown or not.
    assert await _behind(_LocalCH(*built), sha="t" * 64, cool=7200) == ["u2", "u1"]


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

    # The fact's delete is a version written after the profile was built.
    deleted = _fact(_A, "u1", "tz", "a", tombstoned_at="now()")
    built = [ddl("memory"), deleted, _profile("u1", profile_tags("s" * 64, "0123456789abcdef"))]
    assert await _digest(_LocalCH(*built), "u1") == ""
    assert await _behind(_LocalCH(*built)) == ["u1"]
    emptied = [ddl("memory"), deleted, _profile("u1", profile_tags("s" * 64, ""))]
    assert await _behind(_LocalCH(*emptied)) == []


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


@_needs_ch
@pytest.mark.asyncio
async def test_a_fact_written_while_the_profile_was_built_puts_it_behind() -> None:
    from server.memory.store.profile_sources import profile_tags

    facts = [ddl("memory"), _fact(_A, "u1", "tz", "a", updated_at="now() - 7200")]
    digest = await _digest(_LocalCH(*facts), "u1")
    # The sources were read an hour ago and the row written now; the fact landed between.
    built = (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, version, tags, valid_at, updated_at) VALUES ('o', 'n', '-', 'u1', "
        f"'-', 'structured_profile', 'profile', '{{}}', 1, {profile_tags('s' * 64, digest)!r}, "
        "now() - 3600, now() - 600)"
    )
    late = _fact(_B, "u1", "lang", "b", updated_at="now() - 1800")
    assert await _behind(_LocalCH(*facts, built)) == []
    assert await _behind(_LocalCH(*facts, late, built)) == ["u1"]


@_needs_ch
@pytest.mark.asyncio
async def test_the_schema_listing_skips_deactivated_namespaces_and_resumes_after_a_cursor() -> None:
    from server.memory.store import profile_schemas

    def schema(org: str, ns: str) -> str:
        return (
            "INSERT INTO loom.profile_schemas (org, namespace, schema_json, schema_sha256, "
            f"updated_at, version) VALUES ('{org}', '{ns}', '{{}}', 'h', now(), 1)"
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
        return [(r["org"], r["namespace"]) for r in rows]

    everything = [("o1", "a"), ("o1", "b"), ("o2", "a"), ("o3", "a")]
    assert await names(exclude_orgs=[], after=None, limit=10) == everything
    assert await names(exclude_orgs=["o2"], after=None, limit=10) == [
        ("o1", "a"),
        ("o1", "b"),
        ("o3", "a"),
    ]
    assert await names(exclude_orgs=[], after=("o1", "b"), limit=1) == [("o2", "a")]
    assert await names(exclude_orgs=[], after=None, limit=10, only=("o2", "a")) == [("o2", "a")]
