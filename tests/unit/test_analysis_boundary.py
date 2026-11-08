"""The analysis boundary as written: grants, policies and profile come from
the one allowlist, and every allowlisted table is one erasure reaches."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re

import pytest

from server.core.sql_literal import sql_string
from server.erasure.store import census
from server.insights.domain import analysis as boundary
from server.insights.store import analysis as store
from tests.unit import clickhouse_local

CAPS = boundary.Caps(
    max_execution_s=10,
    max_rows_to_read=1000,
    max_memory_bytes=10_000,
    max_result_rows=50,
    max_result_bytes=100_000,
    max_concurrent=4,
    max_threads=2,
)
KEY = bytes(range(32))
SQL = boundary.entity_sql("`loom`", "pw-current", KEY, CAPS)


def _grants() -> dict[str, list[str]]:
    out = {}
    for stmt in SQL:
        m = re.match(r"GRANT SELECT\((.*)\) ON `loom`\.(\w+) TO `loom_analyst`$", stmt)
        if m:
            out[m.group(2)] = m.group(1).split(", ")
    return out


def _policies(name: str) -> dict[str, str]:
    out = {}
    for stmt in SQL:
        m = re.match(rf"CREATE ROW POLICY OR REPLACE `{name}` ON `loom`\.(\w+) (.*)$", stmt)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_grants_are_the_allowlist_and_the_canary() -> None:
    expected = {t.name: [c.name for c in t.columns] for t in boundary.TABLES}
    expected[boundary.CANARY_TABLE] = ["id"]
    assert _grants() == expected


def test_a_version_names_one_set_of_grants_and_policies() -> None:
    # Changing a grant, a policy, a live predicate or a retired table changes
    # this digest: bump ENTITY_VERSION and record the new digest here.
    assert {boundary.ENTITY_VERSION: boundary.structure_digest()} == {
        2: "54f17f7b202ff38e8d89640dc320b263"
    }


def test_the_canary_policy_is_the_last_policy() -> None:
    policies = [s for s in SQL if s.startswith("CREATE ROW POLICY")]
    assert f".{boundary.CANARY_TABLE} " in policies[-1]
    assert "USING org = toString(getSetting('SQL_loom_org'))" in policies[-1]


def test_policies_come_before_grants_and_revokes_after() -> None:
    kinds = [s.split(" ", 1)[0] for s in SQL]
    table_policies = [
        i
        for i, s in enumerate(SQL)
        if s.startswith("CREATE ROW POLICY") and f".{boundary.CANARY_TABLE} " not in s
    ]
    first_grant = kinds.index("GRANT")
    assert max(table_policies) < first_grant
    assert not any(s.startswith("REVOKE ALL") for s in SQL)
    assert not any(s.startswith("DROP ROW POLICY") for s in SQL)
    assert all(s.startswith("GRANT SELECT(") for s in SQL if s.startswith("GRANT"))


def test_every_grant_and_role_outside_the_allowlist_is_revoked_before_the_grants() -> None:
    def grant(access: str, database: str, table: str, column: str, home: bool) -> boundary.Grant:
        return boundary.Grant(access, database, table, column, home)

    held = frozenset(
        {
            grant("SELECT", "loom", "memory", "org", True),
            grant("SELECT", "loom", "memory", "value", True),
            # A whole allowlisted table: its revoke also takes the column grants.
            grant("SELECT", "loom", "memory", "", True),
            grant("SELECT", "loom", "signing_keys", "", True),
            grant("SELECT", "other", "memory", "org", False),
            grant("SHOW TABLES", "loom", "", "", True),
            grant("SELECT", "", "", "", False),
        }
    )
    sql = boundary.entity_sql("`loom`", "pw", KEY, CAPS, held, frozenset({"bi_reader"}))
    revokes = [s for s in sql if s.startswith("REVOKE")]
    assert sorted(revokes) == sorted(
        [
            "REVOKE SELECT(`value`) ON `loom`.`memory` FROM `loom_analyst`",
            "REVOKE SELECT ON `loom`.`memory` FROM `loom_analyst`",
            "REVOKE SELECT ON `loom`.`signing_keys` FROM `loom_analyst`",
            "REVOKE SELECT(`org`) ON `other`.`memory` FROM `loom_analyst`",
            "REVOKE SHOW TABLES ON `loom`.* FROM `loom_analyst`",
            "REVOKE SELECT ON *.* FROM `loom_analyst`",
            "REVOKE `bi_reader` FROM `loom_analyst`",
        ]
    )
    grants = [i for i, s in enumerate(sql) if s.startswith("GRANT")]
    assert max(sql.index(r) for r in revokes) < min(grants)
    policies = [i for i, s in enumerate(sql) if s.startswith("CREATE ROW POLICY")]
    assert max(sql.index(r) for r in revokes) > max(policies[:-1])


def test_every_other_users_policy_is_written_before_the_analysts() -> None:
    for t in boundary.TABLES:
        others = next(
            i
            for i, s in enumerate(SQL)
            if f"`{boundary.OTHERS_POLICY_NAME}` ON `loom`.{t.name} " in s
        )
        scoped = next(
            i for i, s in enumerate(SQL) if f"`{boundary.POLICY_NAME}` ON `loom`.{t.name} " in s
        )
        assert others < scoped, t.name


def test_the_caps_profile_carries_its_own_digest() -> None:
    caps_sql = boundary.caps_sql(CAPS)
    assert all(boundary.caps_digest(CAPS) in s for s in caps_sql)
    other = boundary.Caps(**{**CAPS.__dict__, "max_threads": 3})
    assert boundary.caps_digest(other) != boundary.caps_digest(CAPS)


def test_the_payload_counts_bytes_as_clickhouse_length_does() -> None:
    assert boundary.scope_payload("é", "a", 1, "q") == "2:é1:a1:1q"


def test_a_retired_table_keeps_a_policy_that_reads_nothing(monkeypatch) -> None:
    monkeypatch.setattr(boundary, "RETIRED_TABLES", ("old_table",))
    sql = boundary.entity_sql("`loom`", "pw", KEY, CAPS)
    assert (
        "CREATE ROW POLICY OR REPLACE `loom_analyst_scope` ON `loom`.old_table "
        "AS permissive FOR SELECT USING 0 TO `loom_analyst`"
    ) in sql
    assert not any("ON `loom`.old_table TO" in s for s in sql if s.startswith("GRANT"))


def test_every_table_policy_carries_scope_signature_and_live_predicate() -> None:
    scoped = _policies(boundary.POLICY_NAME)
    for t in boundary.TABLES:
        policy = scoped[t.name]
        assert policy.startswith(f"AS permissive FOR SELECT USING ({t.live}) AND org = ")
        assert "has(splitByChar(',', toString(getSetting('SQL_loom_ns'))), namespace)" in policy
        assert policy.endswith("TO `loom_analyst`")


def test_other_users_keep_reading_every_table() -> None:
    others = _policies(boundary.OTHERS_POLICY_NAME)
    assert set(others) == {t.name for t in boundary.TABLES}
    assert set(others.values()) == {"AS permissive FOR SELECT USING 1 TO ALL EXCEPT `loom_analyst`"}


def test_tombstoning_tables_are_read_live() -> None:
    # A table the saga tombstones keeps its rows; its policy must hide them.
    live = {t.name: t.live for t in boundary.TABLES}
    counted = {s.table: s.live for s in census._CENSUS}
    assert live["memory"] == counted["loom.memory"] == "tombstoned_at IS NULL"
    assert live["memory_record_conflicts"] == counted["loom.memory_record_conflicts"]
    # Stricter than the census, which also counts a replaced version whose
    # original is still in the bucket: the analyst reads live versions only.
    assert counted["loom.documents"].startswith("(superseded_at IS NULL OR ")
    assert live["documents"] == "superseded_at IS NULL"


def test_every_allowlisted_table_is_reached_by_erasure() -> None:
    classified = census.classified_tables()
    for t in boundary.TABLES:
        assert f"loom.{t.name}" in classified, t.name
    assert classified["loom.analysis_secrets"] == "no_memory_content"


def test_no_entity_statement_changes_what_cloud_pins() -> None:
    # ClickHouse Cloud pins how its users' queries are logged and refuses a
    # profile or a statement that changes it; the text is cut from the log instead.
    pinned = re.compile(r"\blog_(queries|query_settings|queries_min_type)\s*=")
    assert not any(pinned.search(stmt) for stmt in SQL)


def test_entity_statements_are_sent_with_their_text_cut_from_the_log() -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    class _Client:
        async def execute(self, sql: str, settings: dict[str, object], **_: object) -> None:
            sent.append((sql, settings))

    asyncio.run(store.write_entities(SQL, ch=_Client()))  # type: ignore[arg-type]
    assert [sql for sql, _ in sent] == SQL
    assert all(settings == {"log_queries_cut_to_length": 1} for _, settings in sent)


def test_profile_pins_what_a_statement_could_widen() -> None:
    # The pins live in the main profile, the caps in the caps profile it carries.
    profile = " ".join(s for s in SQL if "SETTINGS PROFILE" in s)
    assert "INHERIT 'loom_analyst'" in profile
    for pinned in (
        "readonly = 2 CONST",
        "final = 1 CONST",
        "apply_row_policy_after_final = 1 CONST",
        "log_queries_cut_to_length = 1 CONST",
        "use_query_cache = 0 CONST",
        "read_overflow_mode = 'throw' CONST",
        "result_overflow_mode = 'throw' CONST",
        "max_execution_time = 10 MIN 0.1 MAX 10",
        "max_rows_to_read = 1000 MIN 1 MAX 1000",
        "max_result_rows = 51 MIN 1 MAX 51",
        "max_threads = 2 MIN 1 MAX 2",
        "send_logs_level = 'none' CONST",
        "use_query_condition_cache = 0 CONST",
    ):
        assert pinned in profile, pinned


def test_one_password_and_one_key() -> None:
    user = next(s for s in SQL if s.startswith("ALTER USER"))
    assert "IDENTIFIED WITH sha256_password BY 'pw-current' SETTINGS" in user
    policy = _policies(boundary.POLICY_NAME)["memory"]
    assert policy.count("hex(SHA256(concat(decrypt(") == 1


def test_the_digest_leaves_out_the_key_which_never_changes() -> None:
    other = boundary.entity_sql("`loom`", "pw-other", bytes(32), CAPS)
    assert boundary.entity_digest(other) == boundary.entity_digest(SQL)
    assert KEY.hex() not in SQL[-1]


def test_needs_rewrite() -> None:
    digest = boundary.entity_digest(SQL)
    v = boundary.ENTITY_VERSION
    assert boundary.needs_rewrite(None, digest)
    assert boundary.needs_rewrite(boundary.Stored(v - 1, digest), digest)
    assert not boundary.needs_rewrite(boundary.Stored(v + 1, "other"), digest)
    assert boundary.needs_rewrite(boundary.Stored(v, "other"), digest)
    assert not boundary.needs_rewrite(boundary.Stored(v, digest), digest)


def test_signature_is_hmac_sha256_of_the_length_prefixed_scope() -> None:
    key = KEY
    settings = boundary.scope_settings(key, "acme", ["b", "a", "a"], 1700000000, "qid-1")
    assert settings["SQL_loom_ns"] == "a,b"
    payload = b"4:acme3:a,b10:1700000000qid-1"
    assert settings["SQL_loom_sig"] == hmac.new(key, payload, hashlib.sha256).hexdigest().upper()
    assert settings["query_id"] == "qid-1"
    # Moving a character between the org and the namespaces changes the payload.
    assert boundary.scope_payload("acm", "ea,b", 1, "q") != boundary.scope_payload(
        "acme", "a,b", 1, "q"
    )
    # So does moving one between the expiry and the query id.
    assert boundary.scope_payload("o", "n", 12, "3q") != boundary.scope_payload("o", "n", 123, "q")


def test_the_policy_signs_the_query_id_clickhouse_gives_the_statement() -> None:
    # A signature read inside one statement and sent with another reads nothing.
    assert "initialQueryID()" in boundary.signature_filter(KEY)


def test_wrapper_drops_one_semicolon_ends_a_comment_and_asks_one_row_more() -> None:
    assert boundary.wrap_statement("SELECT 1 -- note;", 10) == (
        "SELECT * FROM (\nSELECT 1 -- note\n) LIMIT 11"
    )


def _local_error(sql: str) -> str:
    with pytest.raises(AssertionError) as raised:
        clickhouse_local.run([], sql)
    return str(raised.value)


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_the_policy_computes_the_signature_loom_signs() -> None:
    payload = boundary.scope_payload("acme", "a,b", 123, "qid-1")
    rows = clickhouse_local.run([], f"SELECT {boundary.hmac_sql(KEY, sql_string(payload))} AS s")
    assert rows == [{"s": boundary.sign_scope(KEY, "acme", "a,b", 123, "qid-1")}]


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_an_error_that_quotes_the_policy_quotes_no_key() -> None:
    # The probe's unsigned read is refused with an error quoting the policy,
    # and ClickHouse logs that error in system.query_log.
    rows = "SELECT 'acme' AS org, 'a' AS namespace"
    error = _local_error(f"SELECT 1 FROM ({rows}) WHERE {boundary.policy_filter(KEY)}")
    assert "UNKNOWN_SETTING" in error
    assert "[HIDDEN]" in error
    padded = KEY.ljust(64, b"\0")
    for secret in (KEY, bytes(b ^ 0x36 for b in padded), bytes(b ^ 0x5C for b in padded)):
        assert secret.hex() not in error.lower()
    assert "unhex(" not in error
    # The pads end in the zero-padded key's 0x36 and 0x5C bytes, printable as they are.
    assert "6" * 32 not in error and "\\" * 32 not in error


def test_policy_reads_every_setting_as_a_string() -> None:
    # A typed override (SQL_loom_ns = 5) would make the policy throw, and the
    # error would quote the policy, key included.
    policy = _policies(boundary.POLICY_NAME)["memory"]
    assert policy.count("getSetting(") == policy.count("toString(getSetting(")


def test_the_stamp_is_written_last() -> None:
    assert SQL[-1].startswith("ALTER SETTINGS PROFILE `loom_analyst` SETTINGS ")
    assert boundary.DIGEST_SETTING in SQL[-1]
    assert all(boundary.DIGEST_SETTING not in s for s in SQL[:-1])


def test_revokes_for_skips_a_line_that_is_not_a_grant() -> None:
    assert boundary.revokes_for(["not a grant", "GRANT r TO loom_analyst"]) == [
        "REVOKE r FROM loom_analyst"
    ]
