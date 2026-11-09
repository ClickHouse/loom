"""insights.query stays inside the token's org and namespace, whatever the SQL.

Two orgs each write a canary memory and a canary ledger row. Every statement
below then runs under org A's viewer token on namespace A, and each answer is
a refusal or rows without org B's canary, namespace B's canary, a deleted or
an erased key. A positive control reads A's own canary first, so an empty
answer cannot pass by accident.

The boundary is ClickHouse's (docs/architecture/assistant.md): the analyst
user's grants, settings profile and signed row policies. Needs a server that
enforces auth (an auth-off server ignores tokens); skips otherwise.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass

import httpx
import pytest

from server.insights.domain import analysis as boundary
from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org

_REFUSED = {403, 422}


@dataclass
class _Seed:
    org_a: str
    org_b: str
    ns_a: str
    ns_b: str
    nonce: str
    viewer_a: str
    writer_a: str
    writer_b: str


def _call(
    base: str, token: str, op: str, body: dict, headers: dict[str, str] | None = None
) -> httpx.Response:
    """One call, waiting out a rate limit: insights.query has a small bucket."""
    sent = {"Authorization": f"Bearer {token}", **(headers or {})}
    for _ in range(10):
        r = httpx.post(f"{base}/v1/{op}", json=body, headers=sent, timeout=60)
        if r.status_code != 429 or "analysis statements as it may at once" in r.text:
            return r
        time.sleep(float(r.headers.get("Retry-After") or 1))
    return r


@pytest.fixture(scope="module")
def seed(control_plane_url: str) -> _Seed:
    # Minted directly, with the server's secret as mint_test_token does: the
    # seed is module-scoped, which a function-scoped fixture cannot serve.
    from server.auth.jwt import mint_token

    if (
        httpx.post(f"{control_plane_url}/v1/insights.catalog", json={}, timeout=30).status_code
        != 401
    ):
        pytest.skip("needs a server that enforces auth (LOOM_AUTH_REQUIRED=true)")
    nonce = uuid.uuid4().hex[:12]
    org_a, org_b = unique_org("analysis-a"), unique_org("analysis-b")
    ns_a, ns_b = f"ns-a-{nonce}", f"ns-b-{nonce}"

    def token(org: str, namespaces: list[str], role: str) -> str:
        return mint_token(
            subject=f"analysis-{role}-{nonce}",
            org=org,
            namespaces=namespaces,
            roles=[role],
            ttl_seconds=900,
            is_service_account=True,
        )

    s = _Seed(
        org_a=org_a,
        org_b=org_b,
        ns_a=ns_a,
        ns_b=ns_b,
        nonce=nonce,
        viewer_a=token(org_a, [ns_a], "viewer"),
        writer_a=token(org_a, [ns_a, ns_b], "writer"),
        writer_b=token(org_b, [ns_a], "writer"),
    )
    writes = [
        (s.writer_a, org_a, ns_a, f"own/{nonce}"),
        (s.writer_a, org_a, ns_a, f"deleted/{nonce}"),
        (s.writer_a, org_a, ns_b, f"other-ns/{nonce}"),
        # Org B uses namespace A's name too: only the org tells them apart.
        (s.writer_b, org_b, ns_a, f"other-org/{nonce}"),
    ]
    for tok, org, ns, key in writes:
        r = _call(
            control_plane_url,
            tok,
            "memory.set",
            {"org": org, "namespace": ns, "agent": "canary", "memory_key": key, "value": nonce},
        )
        assert r.status_code == 200, r.text
    r = _call(
        control_plane_url,
        s.writer_a,
        "memory.delete",
        {"org": org_a, "namespace": ns_a, "agent": "canary", "memory_key": f"deleted/{nonce}"},
    )
    assert r.status_code == 200, r.text
    return s


def _query(
    base: str, s: _Seed, sql: str, token: str | None = None, request_id: str | None = None
) -> httpx.Response:
    return _call(
        base,
        token or s.viewer_a,
        "insights.query",
        {"org": s.org_a, "namespace": s.ns_a, "sql": sql},
        {"X-Request-Id": request_id} if request_id else None,
    )


def _assert_contained(r: httpx.Response, s: _Seed) -> None:
    """A refusal, or rows that hold none of the foreign canaries. Only the rows
    are searched: a refusal and a column name quote the statement's text."""
    assert r.status_code in {200, *_REFUSED}, f"{r.status_code}: {r.text[:300]}"
    # The row policy's text holds the scope key; no answer may quote it.
    assert "unhex(" not in r.text and "getSetting(" not in r.text, r.text[:300]
    if r.status_code != 200:
        return
    rows = str(r.json()["rows"])
    for leaked in ("other-ns/", "other-org/", "deleted/", s.org_b, s.ns_b):
        assert leaked not in rows, f"{leaked} in {rows[:300]}"


def test_positive_control_reads_own_canary(control_plane_url: str, seed: _Seed) -> None:
    r = _query(control_plane_url, seed, "SELECT memory_key, org, namespace FROM loom.memory")
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [[f"own/{seed.nonce}", seed.org_a, seed.ns_a]]


_ESCAPES = [
    # Other orgs and namespaces, by filter, join, subquery, union and table functions.
    "SELECT memory_key FROM loom.memory WHERE org != '{org_a}'",
    "SELECT memory_key FROM loom.memory WHERE namespace = '{ns_b}'",
    "SELECT b.memory_key FROM loom.memory a CROSS JOIN loom.memory b",
    "SELECT memory_key FROM (SELECT * FROM loom.memory WHERE org = '{org_b}')",
    "SELECT memory_key FROM loom.memory UNION ALL SELECT memory_key FROM loom.memory WHERE 1",
    "SELECT * FROM view(SELECT memory_key, org FROM loom.memory)",
    "SELECT * FROM merge('loom', '^memory$')",
    "SELECT throwIf(org = '{org_b}') FROM loom.memory",
    "SELECT sleepEachRow(0.001) FROM loom.memory WHERE org = '{org_b}'",
    # Tables and columns outside the allowlist.
    "SELECT * FROM system.query_log",
    "SELECT * FROM system.processes",
    "SELECT * FROM system.row_policies",
    "SELECT * FROM system.users",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM loom.signing_keys",
    "SELECT * FROM loom.revoked_tokens",
    "SELECT * FROM loom.analysis_secrets",
    "SELECT * FROM loom.analysis_canary",
    "SELECT value FROM loom.memory",
    "SHOW CREATE TABLE loom.memory",
    "DESCRIBE loom.memory",
    "KILL QUERY WHERE 1 TEST",
    # Plans, indexes and the outside world.
    "EXPLAIN ESTIMATE SELECT * FROM loom.memory",
    "SELECT 1) UNION ALL (EXPLAIN ESTIMATE SELECT * FROM loom.memory",
    "SELECT 1 -- ) UNION ALL (EXPLAIN SELECT 1",
    "SELECT * FROM mergeTreeIndex('loom', 'memory')",
    "SELECT * FROM url('http://127.0.0.1:1/', 'CSV', 'a String')",
    "SELECT * FROM file('/etc/passwd', 'LineAsString')",
    "SELECT * FROM s3('http://127.0.0.1:1/x', 'CSV', 'a String')",
    "SELECT * FROM remote('127.0.0.1', 'loom', 'memory')",
    "SELECT * FROM cluster('default', 'loom', 'memory')",
    "SELECT * FROM executable('cat /etc/passwd', 'LineAsString', 'a String')",
    "SELECT * FROM input('a String')",
    "SELECT aiGenerate(memory_key, map('credentials', 'default')) FROM loom.memory",
    # Settings that would change the scope, the caps or the live view.
    "SELECT memory_key FROM loom.memory SETTINGS SQL_loom_org = '{org_b}'",
    "SELECT memory_key FROM loom.memory SETTINGS SQL_loom_ns = '{ns_a},{ns_b}'",
    "SELECT memory_key FROM loom.memory SETTINGS SQL_loom_exp = '9999999999'",
    "SELECT memory_key FROM loom.memory SETTINGS readonly = 0",
    "SELECT memory_key FROM loom.memory SETTINGS max_execution_time = 100000",
    "SELECT memory_key FROM loom.memory SETTINGS final = 0",
    "SELECT memory_key FROM loom.memory SETTINGS apply_row_policy_after_final = 0",
    "SELECT memory_key FROM loom.memory SETTINGS use_query_cache = 1",
    "SELECT memory_key FROM loom.memory SETTINGS send_logs_level = 'trace'",
    "SELECT memory_key FROM loom.memory SETTINGS use_query_condition_cache = 1",
    "SELECT memory_key FROM loom.memory SETTINGS max_threads = 1000",
    # A scope signed with another key, and one past its expiry.
    "SELECT memory_key FROM loom.memory SETTINGS SQL_loom_sig = '" + "AB" * 32 + "'",
    "SELECT memory_key FROM loom.memory SETTINGS SQL_loom_exp = '1'",
    # Typed and malformed scope values: the policy must neither throw nor be quoted.
    "SELECT count() FROM loom.memory SETTINGS SQL_loom_org = 5",
    "SELECT count() FROM loom.memory SETTINGS SQL_loom_ns = 5",
    "SELECT count() FROM loom.memory SETTINGS SQL_loom_exp = 5",
    "SELECT count() FROM loom.memory SETTINGS SQL_loom_sig = 5",
    "SELECT count() FROM loom.memory SETTINGS SQL_loom_exp = 'abc'",
    # Not a SELECT.
    "INSERT INTO loom.memory (org) VALUES ('x')",
    "ALTER TABLE loom.memory DELETE WHERE 1",
    "CREATE TEMPORARY TABLE t (a String)",
]


# Expressions that throw on a row's value, so the error quotes it: the policy
# must drop another scope's rows before any of them is evaluated.
_QUOTING_ERRORS = [
    "SELECT toUInt64(memory_key) FROM loom.memory",
    "SELECT 1 FROM loom.memory WHERE toUInt64(memory_key) = 1",
    "SELECT 1 FROM loom.memory PREWHERE toUInt64(memory_key) = 1",
    "SELECT 1 FROM loom.memory WHERE toUInt64(memory_key) = 1 "
    "SETTINGS short_circuit_function_evaluation = 'disable'",
    "SELECT 1 FROM loom.memory WHERE toUInt64(memory_key) = 1 "
    "SETTINGS optimize_move_to_prewhere = 1, optimize_move_to_prewhere_if_final = 1, "
    "short_circuit_function_evaluation = 'disable'",
    "SELECT 1 FROM loom.memory WHERE toUInt64(memory_key) = 1 "
    "SETTINGS query_plan_merge_filters = 1, short_circuit_function_evaluation = 'disable'",
    "SELECT toUInt64(memory_key) FROM loom.memory "
    "SETTINGS format_display_secrets_in_show_and_select = 1",
]


def _key_spellings(ch_client) -> list[str]:
    """The policy's `decrypt` arguments, and the pads they evaluate to, as the
    log could spell them: no logged text or error may hold one."""
    secrets = re.findall(r"unhex\('([0-9a-f]+)'\)", _scope_filter(ch_client, "memory"))
    key = next((h for h in secrets if len(h) == 64), None)
    assert key, "no 64-character key literal in the memory policy"
    return [*secrets, *boundary.key_spellings(bytes.fromhex(key))]


@pytest.mark.parametrize("sql", _QUOTING_ERRORS)
def test_an_error_quotes_no_value_from_another_scope(
    control_plane_url: str, seed: _Seed, ch_client, sql: str
) -> None:
    request_id = f"quote-{uuid.uuid4().hex[:12]}"
    r = _query(control_plane_url, seed, sql, request_id=request_id)
    assert r.status_code in _REFUSED, r.text
    for leaked in ("other-ns/", "other-org/", "deleted/"):
        assert leaked not in r.text, r.text[:300]
    ch_client.command("SYSTEM FLUSH LOGS")
    logged = ch_client.query(
        "SELECT groupArray(exception) FROM system.query_log "
        "WHERE startsWith(query_id, {rid:String}) AND exception != ''",
        parameters={"rid": f"{request_id}:"},
    ).result_rows[0][0]
    # The caller's own row is the one that threw.
    assert any(f"own/{seed.nonce}" in e for e in logged), logged
    for leaked in ("other-ns/", "other-org/", "deleted/", *_key_spellings(ch_client)):
        assert not any(leaked.lower() in e.lower() for e in logged), logged


@pytest.mark.parametrize("template", _ESCAPES)
def test_escape_is_refused_or_reads_nothing_foreign(
    control_plane_url: str, seed: _Seed, template: str
) -> None:
    sql = template.format(org_a=seed.org_a, org_b=seed.org_b, ns_a=seed.ns_a, ns_b=seed.ns_b)
    _assert_contained(_query(control_plane_url, seed, sql), seed)


def test_a_scope_read_inside_one_statement_reads_nothing_from_another(
    control_plane_url: str, seed: _Seed
) -> None:
    """A statement can read its own signed scope with getSetting. Sent by
    another org's token in SETTINGS, it must read none of the first org's
    rows: the signature covers the statement's own query id."""
    r = _query(
        control_plane_url,
        seed,
        "SELECT getSetting('SQL_loom_org'), getSetting('SQL_loom_ns'), "
        "getSetting('SQL_loom_exp'), getSetting('SQL_loom_sig')",
    )
    assert r.status_code == 200, r.text
    org, ns, exp, sig = r.json()["rows"][0]
    replay = (
        f"SELECT memory_key FROM loom.memory SETTINGS SQL_loom_org = '{org}', "
        f"SQL_loom_ns = '{ns}', SQL_loom_exp = '{exp}', SQL_loom_sig = '{sig}'"
    )
    for token, org_called, ns_called in (
        (seed.writer_b, seed.org_b, seed.ns_a),
        (seed.viewer_a, seed.org_a, seed.ns_a),
    ):
        r = _call(
            control_plane_url,
            token,
            "insights.query",
            {"org": org_called, "namespace": ns_called, "sql": replay},
        )
        # The same org's replay shows the query-id binding alone: its org matches.
        assert r.status_code in {200, *_REFUSED}, r.text
        if r.status_code == 200:
            assert f"own/{seed.nonce}" not in str(r.json()["rows"]), r.text


def test_ledger_rows_stay_in_scope(control_plane_url: str, seed: _Seed) -> None:
    r = _query(control_plane_url, seed, "SELECT DISTINCT org, namespace FROM loom.agent_events")
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [[seed.org_a, seed.ns_a]]


def test_query_cache_keeps_orgs_apart(control_plane_url: str, seed: _Seed) -> None:
    from server.auth.jwt import mint_token

    viewer_b = mint_token(
        subject=f"analysis-viewer-b-{seed.nonce}",
        org=seed.org_b,
        namespaces=[seed.ns_a],
        roles=["viewer"],
        ttl_seconds=900,
        is_service_account=True,
    )
    sql = "SELECT memory_key FROM loom.memory"
    first = _query(control_plane_url, seed, sql)
    second = _call(
        control_plane_url,
        viewer_b,
        "insights.query",
        {"org": seed.org_b, "namespace": seed.ns_a, "sql": sql},
    )
    assert first.json()["rows"] == [[f"own/{seed.nonce}"]]
    assert second.json()["rows"] == [[f"other-org/{seed.nonce}"]]


def test_scope_outside_the_token_is_refused(control_plane_url: str, seed: _Seed) -> None:
    for org, ns in ((seed.org_b, seed.ns_a), (seed.org_a, seed.ns_b)):
        r = _call(
            control_plane_url,
            seed.viewer_a,
            "insights.query",
            {"org": org, "namespace": ns, "sql": "SELECT memory_key FROM loom.memory"},
        )
        assert r.status_code in {403, 404}, r.text


def _platform_admin(org: str, nonce: str) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=f"analysis-admin-{nonce}",
        org=org,
        namespaces=["*"],
        roles=["platform_admin"],
        ttl_seconds=900,
        is_service_account=True,
    )


def test_a_revoked_token_stops_new_calls(control_plane_url: str, seed: _Seed) -> None:
    from server.auth.jwt import mint_token

    subject = f"sa:analysis-revoked-{seed.nonce}"
    tok = mint_token(
        subject=subject,
        org=seed.org_a,
        namespaces=[seed.ns_a],
        roles=["viewer"],
        ttl_seconds=900,
        is_service_account=True,
    )
    assert _query(control_plane_url, seed, "SELECT 1", tok).status_code == 200
    r = httpx.post(
        f"{control_plane_url}/v1/admin/revoke_subject",
        json={"org": seed.org_a, "subject": subject, "reason": "analysis isolation test"},
        headers={"Authorization": f"Bearer {_platform_admin(seed.org_a, seed.nonce)}"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    wait_until(
        lambda: _query(control_plane_url, seed, "SELECT 1", tok).status_code == 401,
        max_wait=30,
        description="the revoked token to be refused",
    )


def test_an_erased_user_is_gone_from_the_next_statement(
    control_plane_url: str, seed: _Seed
) -> None:
    user = f"erased-{seed.nonce}"
    key = f"erased/{seed.nonce}"
    r = _call(
        control_plane_url,
        seed.writer_a,
        "memory.set",
        {
            "org": seed.org_a,
            "namespace": seed.ns_a,
            "agent": "canary",
            "user_id": user,
            "memory_key": key,
            # Its own value: the same text as another key would be merged into it.
            "value": f"what {user} said",
        },
    )
    assert r.status_code == 200, r.text
    sql = f"SELECT memory_key, user_id FROM loom.memory WHERE user_id = '{user}'"
    wait_until(
        lambda: _query(control_plane_url, seed, sql).json()["rows"] == [[key, user]],
        max_wait=30,
        description="the erased user's memory to be readable before the erasure",
    )
    r = _call(
        control_plane_url,
        _platform_admin(seed.org_a, seed.nonce),
        "erasure.request",
        {"org": seed.org_a, "scope_type": "user_id", "scope_value": user, "requester": "ops"},
    )
    assert r.status_code == 200, r.text
    for after in (sql, "SELECT memory_key FROM loom.memory", "SELECT count() FROM loom.memory"):
        r = _query(control_plane_url, seed, after)
        assert r.status_code == 200, r.text
        assert key not in str(r.json()["rows"]) and user not in str(r.json()["rows"])


def test_a_statement_during_a_policy_rewrite_reads_the_right_rows(
    control_plane_url: str, seed: _Seed, ch_client
) -> None:
    """Every write replaces each policy in place under the same key, so a
    statement that runs while the policies are written again still reads its
    own rows and no other org's."""
    import threading

    policies = [_policy(ch_client, t.name) for t in boundary.TABLES]
    done = threading.Event()
    rewrites: list[int] = []
    failures: list[BaseException] = []

    def rewrite() -> None:
        try:
            while not done.is_set():
                for stmt in policies:
                    ch_client.command(stmt, settings=boundary.LOG_TEXT_CUT)
                rewrites.append(1)
        except BaseException as e:  # noqa: BLE001 — reported to the test below
            failures.append(e)

    writer = threading.Thread(target=rewrite)
    writer.start()
    try:
        for _ in range(20):
            r = _query(control_plane_url, seed, "SELECT memory_key, org FROM loom.memory")
            assert r.status_code == 200, r.text
            assert r.json()["rows"] == [[f"own/{seed.nonce}", seed.org_a]]
    finally:
        done.set()
        writer.join()
    assert not failures, failures
    assert rewrites, "the policies were never written again during the statements"


def test_both_orgs_at_once_read_their_own_rows(control_plane_url: str, seed: _Seed) -> None:
    from concurrent.futures import ThreadPoolExecutor

    sql = "SELECT memory_key, org FROM loom.memory"
    own = {
        seed.org_a: [[f"own/{seed.nonce}", seed.org_a]],
        seed.org_b: [[f"other-org/{seed.nonce}", seed.org_b]],
    }

    def ask(org: str) -> tuple[str, httpx.Response]:
        token = seed.viewer_a if org == seed.org_a else seed.writer_b
        body = {"org": org, "namespace": seed.ns_a, "sql": sql}
        return org, _call(control_plane_url, token, "insights.query", body)

    # Two at a time per org, the per-org cap.
    with ThreadPoolExecutor(max_workers=2) as pool_a, ThreadPoolExecutor(max_workers=2) as pool_b:
        answers = [pool_a.submit(ask, seed.org_a) for _ in range(6)]
        answers += [pool_b.submit(ask, seed.org_b) for _ in range(6)]
        for answer in answers:
            org, r = answer.result()
            assert r.status_code == 200, r.text
            assert r.json()["rows"] == own[org]


def test_the_statement_text_stays_out_of_the_query_log(
    control_plane_url: str, seed: _Seed, ch_client
) -> None:
    marker = f"probe_{seed.nonce}"
    sql = f"SELECT '{marker}' AS m"
    # Settings in the caller's statement belong to the wrapper's subquery, and
    # the profile pins both at the top: refused or run, the text is cut.
    statements = {
        f"text-{seed.nonce}": sql,
        f"lift0-{seed.nonce}": f"{sql} SETTINGS log_queries_cut_to_length = 100000",
        f"lift1-{seed.nonce}": f"{sql} SETTINGS log_formatted_queries = 1",
    }
    for request_id, statement in statements.items():
        r = _query(control_plane_url, seed, statement, request_id=request_id)
        assert r.status_code in ({200} if statement == sql else {200, *_REFUSED}), r.text
    ch_client.command("SYSTEM FLUSH LOGS")
    # Each is logged, as on a server that pins log_queries, but not its text.
    for request_id in statements:
        logged = ch_client.query(
            "SELECT count() FROM system.query_log "
            "WHERE user = {analyst:String} AND startsWith(query_id, {rid:String})",
            parameters={"analyst": boundary.ANALYST_USER, "rid": f"{request_id}:"},
        ).result_rows[0][0]
        assert logged > 0, request_id
    # This read cuts its own text, which holds the marker.
    quoted = ch_client.query(
        "SELECT count() FROM system.query_log "
        "WHERE query LIKE {m:String} OR formatted_query LIKE {m:String}",
        parameters={"m": f"%{marker}%"},
        settings=boundary.LOG_TEXT_CUT,
    ).result_rows[0][0]
    assert quoted == 0


def test_the_scope_key_stays_out_of_the_query_log(seed: _Seed, ch_client) -> None:
    """The policies hold the key and its pads as `decrypt` arguments: no
    logged statement text or error, the boot's writes and the probe's refused
    reads included, carries one. The whole log is read: a build before version
    2 pinned log_queries = 0 for the analyst, so it left no such rows."""
    start = ch_client.query("SELECT now()").result_rows[0][0]
    secrets = _key_spellings(ch_client)

    def refused_since_start() -> bool:
        ch_client.command("SYSTEM FLUSH LOGS")
        return (
            ch_client.query(
                "SELECT count() FROM system.query_log WHERE event_time >= {start:DateTime} "
                "AND user = {analyst:String} AND exception LIKE '%UNKNOWN_SETTING%'",
                parameters={"start": start, "analyst": boundary.ANALYST_USER},
            ).result_rows[0][0]
            > 0
        )

    # The probe's unsigned reads, which quote the policy in their errors, ran in this run.
    wait_until(refused_since_start, max_wait=_PROBE_WAIT_S, interval=2, description="a probe")
    logged = ch_client.query(
        "SELECT count() FROM system.query_log WHERE multiSearchAnyCaseInsensitive(query, {k:Array(String)}) "
        "OR multiSearchAnyCaseInsensitive(formatted_query, {k:Array(String)}) "
        "OR multiSearchAnyCaseInsensitive(exception, {k:Array(String)})",
        parameters={"k": secrets},
        settings=boundary.LOG_TEXT_CUT,
    ).result_rows[0][0]
    assert logged == 0
    # The server log records each refused read's error, whatever log_queries says.
    errors = ch_client.query(
        "SELECT count() FROM system.text_log WHERE event_time >= {start:DateTime} "
        "AND level = 'Error' AND multiSearchAnyCaseInsensitive(message, {k:Array(String)})",
        parameters={"start": start, "k": secrets},
        settings=boundary.LOG_TEXT_CUT,
    ).result_rows[0][0]
    assert errors == 0


# The cases below change the analyst's entities by hand, as an admin could,
# and wait for the probe (every LOOM_CH_ANALYSIS_PROBE_INTERVAL_S on each web
# process; set it for the server and this run alike) to answer. Each puts the
# entities back and waits for every process to answer again.
_PROBE_INTERVAL_S = float(os.environ.get("LOOM_CH_ANALYSIS_PROBE_INTERVAL_S") or 60)
_PROBE_WAIT_S = 2 * _PROBE_INTERVAL_S + 30


def _probed_until(base: str, s: _Seed, done, description: str) -> None:
    """Wait for the probe, sending a statement on each try, until `done()` holds."""

    def tried() -> bool:
        _query(base, s, "SELECT 1")
        return bool(done())

    wait_until(tried, max_wait=_PROBE_WAIT_S, interval=2, description=description)


def _answers_again(base: str, s: _Seed) -> None:
    """Each web process keeps its own probe answer and probes again one interval
    after its last probe, so wait past that and for a run of answers."""
    start = time.monotonic()

    def healthy() -> bool:
        if time.monotonic() - start < _PROBE_INTERVAL_S + 5:
            return False
        return all(_query(base, s, "SELECT 1").status_code == 200 for _ in range(10))

    wait_until(healthy, max_wait=_PROBE_WAIT_S, interval=2, description="every process to answer")


def _rows_hidden_without_a_policy(ch_client, nonce: str) -> bool:
    """Whether the server runs with users_without_row_policies_can_read_rows
    off, as the configuration Loom ships sets it: a scratch user with no
    policy on a table another user has one on reads none of its rows."""
    table, user, policy = f"default.rp_{nonce}", f"rp_user_{nonce}", f"rp_policy_{nonce}"
    ch_client.command(f"CREATE TABLE {table} (a UInt8) ENGINE = Memory")
    try:
        ch_client.command(f"INSERT INTO {table} VALUES (1)")
        ch_client.command(f"CREATE USER {user} IDENTIFIED WITH no_password")
        ch_client.command(f"GRANT SELECT ON {table} TO {user}")
        ch_client.command(f"CREATE ROW POLICY {policy} ON {table} USING 1 TO CURRENT_USER")
        return ch_client.command(f"EXECUTE AS {user} SELECT count() FROM {table}") == 0
    finally:
        ch_client.command(f"DROP ROW POLICY IF EXISTS {policy} ON {table}")
        ch_client.command(f"DROP USER IF EXISTS {user}")
        ch_client.command(f"DROP TABLE IF EXISTS {table}")


def _scope_filter(ch_client, table: str) -> str:
    """The analyst's scope policy condition on `table`, read from
    system.row_policies: SHOW CREATE prints the key's literals as [HIDDEN]."""
    return ch_client.query(
        "SELECT select_filter FROM system.row_policies WHERE short_name = {p:String} "
        "AND database = 'loom' AND table = {t:String}",
        parameters={"p": boundary.POLICY_NAME, "t": table},
    ).result_rows[0][0]


def _policy(ch_client, table: str) -> str:
    """The analyst's scope policy on `table` as a CREATE statement."""
    analyst = f"`{boundary.ANALYST_USER}`"
    return boundary.policy_sql(
        "loom", boundary.POLICY_NAME, table, _scope_filter(ch_client, table), analyst
    )


def test_an_extra_grant_is_revoked_by_the_probe(
    control_plane_url: str, seed: _Seed, ch_client
) -> None:
    ch_client.command("GRANT SELECT ON loom.signing_keys TO loom_analyst")
    try:
        _probed_until(
            control_plane_url,
            seed,
            lambda: "signing_keys" not in str(ch_client.command("SHOW GRANTS FOR loom_analyst")),
            "the probe to revoke the extra grant",
        )
    finally:
        ch_client.command("REVOKE SELECT ON loom.signing_keys FROM loom_analyst")
    _answers_again(control_plane_url, seed)
    _assert_contained(_query(control_plane_url, seed, "SELECT * FROM loom.signing_keys"), seed)


@pytest.mark.parametrize("table", ["agent_events", "analysis_canary"])
def test_a_dropped_policy_reads_nothing_and_is_written_again(
    control_plane_url: str, seed: _Seed, ch_client, table: str
) -> None:
    """Under the configuration Loom ships, a table the analyst has no policy on
    reads no row; under the server default it reads every row until the probe
    writes the policy again. The canary's policy is the one the probe signs
    its own read with."""
    hidden = _rows_hidden_without_a_policy(ch_client, seed.nonce)

    statement = _policy(ch_client, table)
    ch_client.command(f"DROP ROW POLICY `{boundary.POLICY_NAME}` ON loom.{table}")
    try:
        r = _query(control_plane_url, seed, "SELECT DISTINCT org FROM loom.agent_events")
        assert r.status_code in {200, 503}, r.text
        if r.status_code == 200 and hidden:
            assert r.json()["rows"] in ([], [[seed.org_a]]), r.text
        _probed_until(
            control_plane_url,
            seed,
            lambda: (
                boundary.POLICY_NAME in str(ch_client.command(f"SHOW ROW POLICIES ON loom.{table}"))
            ),
            "the probe to write the policy again",
        )
    except BaseException:
        ch_client.command(statement, settings=boundary.LOG_TEXT_CUT)
        raise
    _answers_again(control_plane_url, seed)


def test_a_canary_under_another_key_is_written_again(
    control_plane_url: str, seed: _Seed, ch_client
) -> None:
    statement = _policy(ch_client, boundary.CANARY_TABLE)
    other_key = boundary.policy_sql(
        "loom",
        boundary.POLICY_NAME,
        boundary.CANARY_TABLE,
        boundary.policy_filter(os.urandom(32)),
        f"`{boundary.ANALYST_USER}`",
    )
    ch_client.command(other_key, settings=boundary.LOG_TEXT_CUT)
    try:
        _probed_until(
            control_plane_url,
            seed,
            lambda: _policy(ch_client, boundary.CANARY_TABLE) == statement,
            "the probe to write the canary's policy again",
        )
    except BaseException:
        ch_client.command(statement, settings=boundary.LOG_TEXT_CUT)
        raise
    _answers_again(control_plane_url, seed)


def test_a_widening_policy_makes_analysis_unavailable(
    control_plane_url: str, seed: _Seed, ch_client
) -> None:
    """A policy Loom did not write that lets the analyst read every row: the
    probe's reserved-scope read sees it, and the replica names it instead of
    writing anything."""
    ch_client.command(
        "CREATE ROW POLICY OR REPLACE `widen_test` ON loom.memory "
        "AS permissive FOR SELECT USING 1 TO `loom_analyst`"
    )
    try:
        r = wait_until(
            lambda: (r := _query(control_plane_url, seed, "SELECT 1")).status_code == 503 and r,
            max_wait=_PROBE_WAIT_S,
            interval=2,
            description="the probe to report the widening policy",
        )
        assert r.json()["detail"]["code"] == "ANALYSIS_UNAVAILABLE", r.text
        # The policy's name goes to the server log, never to a caller or to
        # /v1/health, which answers without a token.
        assert "widen_test" not in r.text, r.text

        # Each web process probes on its own clock: wait for one that reports it.
        def failing_health() -> str | None:
            text = httpx.get(f"{control_plane_url}/v1/health", timeout=30).text
            return text if "analysis_user_failing" in text else None

        health = wait_until(
            failing_health,
            max_wait=_PROBE_WAIT_S,
            interval=2,
            description="/v1/health to report the analysis problem",
        )
        assert "widen_test" not in health
        assert "widen_test" in str(ch_client.command("SHOW ROW POLICIES ON loom.memory"))
    finally:
        ch_client.command("DROP ROW POLICY IF EXISTS `widen_test` ON loom.memory")
    _answers_again(control_plane_url, seed)
