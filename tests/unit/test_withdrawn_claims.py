"""A token's namespace claim ends with the grant it was minted on: taking the
grant away writes a cutoff, and tokens minted before it no longer reach the
namespace through their own claim. A later grant does not lift the cutoff, a
claim never backed by a grant keeps working, and a token minted afterwards
is not touched."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import server.auth.context as ctx
import server.auth.revocation as rev
import server.identity.app.grants as grants_mod
from server.auth.authz import check_authorized, claim_withdrawn
from server.auth.jwt import AuthInfo
from server.identity.api.models_grants import GrantsRevokeInput
from server.identity.domain.grants import GrantState
from server.identity.store import grants as grants_repo
from server.operations.base import Role

ORG = "acme"
BEFORE = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())


class _CH:
    """`loom.revoked_tokens` as written, and the subject's live grants."""

    def __init__(self) -> None:
        self.revoked: dict[str, dict[str, Any]] = {}
        self.live: dict[str, str] = {}  # namespace -> role
        self.log: list[dict[str, Any]] = []  # loom.namespace_grant_log rows

    async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
        assert table == "loom.revoked_tokens"
        self.revoked.update({r["jti"]: r for r in rows})

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "revoked_tokens" in sql:
            keys = (params or {}).get("keys")
            return [r for k, r in self.revoked.items() if keys is None or k in keys]
        if "namespace_grant_log" in sql:
            return self.log
        if "namespace_grants" in sql:
            return [{"namespace": ns} for ns in self.live]
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict | None:
        if "namespace_grants" in sql and (params or {}).get("ns") in self.live:
            return {"role": self.live[params["ns"]]}
        return None


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> Iterator[_CH]:
    client = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(rev, "_revoked_jtis", frozenset())
    monkeypatch.setattr(rev, "_revoked_before", {})
    yield client
    ctx.set_current_auth(None)


def _flat(*namespaces: str, issued_at: int = BEFORE) -> AuthInfo:
    return AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=namespaces,
        roles=(Role.WRITER,),
        is_service_account=False,
        issued_at=issued_at,
    )


def _scoped(namespace: str) -> AuthInfo:
    return AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=(namespace,),
        roles=(),
        is_service_account=False,
        ns_roles=((namespace, "writer"),),
        issued_at=BEFORE,
    )


def _admits(auth: AuthInfo, namespace: str, role: Role = Role.VIEWER) -> bool:
    try:
        asyncio.run(check_authorized(auth, role, ORG, namespace))
    except PermissionError:
        return False
    return True


def _end(namespace: str) -> None:
    asyncio.run(
        rev.write_grant_cutoffs(ORG, [namespace], ["alice"], reason="test", revoked_by="boss")
    )


def test_ending_a_grant_withdraws_the_claim_of_older_tokens(ch: _CH) -> None:
    assert _admits(_flat("team"), "team") and _admits(_scoped("team"), "team")
    _end("team")
    assert not _admits(_flat("team"), "team")
    assert not _admits(_scoped("team"), "team")
    assert _admits(_flat("team", "notes"), "notes"), "only the ended namespace is withdrawn"


def test_the_cutoff_lands_in_this_process_without_waiting_for_a_refresh(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _stale() -> None:
        return None

    monkeypatch.setattr(rev, "refresh", _stale)
    _end("team")
    assert not _admits(_flat("team"), "team")


def test_a_token_minted_after_the_cutoff_keeps_its_claim(ch: _CH) -> None:
    _end("team")
    later = int(datetime.now(UTC).timestamp()) + 2
    assert _admits(_flat("team", issued_at=later), "team")


def test_a_token_minted_in_the_second_of_the_cutoff_is_cut(ch: _CH) -> None:
    now = int(datetime.now(UTC).timestamp())
    _end("team")
    assert not _admits(_flat("team", issued_at=now), "team")


def test_an_unreadable_directory_fails_the_cutoff(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> None:
    # A spelling the cutoff could not reach would keep its claim, so the grant
    # change fails instead of landing half-done.
    async def _down(*_a: Any, **_k: Any) -> list[str]:
        raise RuntimeError("replica away")

    monkeypatch.setattr("server.auth.aliases.cutoff_spellings", _down)
    with pytest.raises(RuntimeError):
        _end("team")
    assert ch.revoked == {}


def test_a_later_grant_does_not_restore_the_old_claim(ch: _CH) -> None:
    # Round-trip a self-join: the person's own viewer grant lands after the
    # cutoff. The older writer tokens, flat or scoped, read through that
    # grant, but their claims no longer carry the write they were minted with.
    _end("team")
    ch.live["team"] = "viewer"
    for old in (_flat("team"), _scoped("team")):
        assert _admits(old, "team", Role.VIEWER)
        assert not _admits(old, "team", Role.WRITER)


def test_a_claim_never_backed_by_a_grant_is_left_alone(ch: _CH) -> None:
    # An operator's `loom mint-token --namespaces notes`; no cutoff names it.
    assert _admits(_flat("notes"), "notes")
    assert not claim_withdrawn(_flat("*"), ORG, "team")


def test_the_check_reads_no_clickhouse(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> None:
    _end("team")

    class _Unreachable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("the claim check must not reach ClickHouse")

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Unreachable())
    assert claim_withdrawn(_scoped("team"), ORG, "team")


def test_every_spelling_of_the_subject_is_cut(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _aliases(org: str, subject: str, **_k: Any) -> list[str]:
        return [subject, "alice@acme.com"]

    monkeypatch.setattr("server.auth.aliases.cutoff_spellings", _aliases)
    _end("team")
    assert set(ch.revoked) == {
        rev.grant_claim_key(ORG, "team", "alice"),
        rev.grant_claim_key(ORG, "team", "alice@acme.com"),
    }
    by_email = AuthInfo(
        subject="alice@acme.com",
        org=ORG,
        namespaces=("team",),
        roles=(Role.WRITER,),
        is_service_account=False,
        issued_at=BEFORE,
    )
    assert not _admits(by_email, "team")


def test_a_spelling_in_another_case_is_cut_too(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> None:
    # Alias lookup matches emails case-insensitively but returns the stored
    # spelling; a token minted for the lowercase one must not keep the claim.
    async def _aliases(org: str, subject: str, **_k: Any) -> list[str]:
        return [subject, "Alice@Acme.com"]

    monkeypatch.setattr("server.auth.aliases.cutoff_spellings", _aliases)
    _end("team")
    lowercase = AuthInfo(
        subject="alice@acme.com",
        org=ORG,
        namespaces=("team",),
        roles=(Role.WRITER,),
        is_service_account=False,
        issued_at=BEFORE,
    )
    assert not _admits(lowercase, "team")


def test_the_callers_namespaces_leave_out_a_withdrawn_claim(ch: _CH) -> None:
    _end("team")
    ctx.set_current_auth(_flat("team", "notes"))
    assert asyncio.run(ctx.resolve_my_namespaces(ORG)) == ["notes"]
    ctx.set_current_auth(_scoped("team"))
    assert asyncio.run(ctx.resolve_my_namespaces(ORG)) == []


@pytest.mark.parametrize(("status", "cut"), [("active", True), ("pending", False)])
def test_revoking_ends_claims_only_when_the_grant_was_active(
    status: str,
    cut: bool,
    monkeypatch: pytest.MonkeyPatch,
    recorded_cutoff_writes: list[tuple[str, list[str], list[str]]],
) -> None:
    # Rejecting a join request granted nothing, so it ends no claim.
    async def _find(org: str, namespace: str, subject: str, **_k: Any) -> GrantState:
        return GrantState.from_row({"role": "writer", "status": status, "granted_by": "boss"})

    async def _ignore(*_a: Any, **_k: Any) -> Any:
        return 0

    monkeypatch.setattr(grants_repo, "find_any_grant", _find)
    monkeypatch.setattr(grants_repo, "insert_row", _ignore)
    monkeypatch.setattr(grants_repo, "insert_rows", _ignore)
    monkeypatch.setattr(
        "server.identity.app.token_grants.cascade_revoke_to_created_tokens", _ignore
    )
    revoke = GrantsRevokeInput(org=ORG, namespace="team", subject="alice")
    assert asyncio.run(grants_mod.handle_grants_revoke(revoke)).revoked
    assert recorded_cutoff_writes == ([(ORG, ["team"], ["alice"])] if cut else [])


def _logged_at(role: str, at: datetime, namespace: str = "team") -> dict[str, Any]:
    return {
        "namespace": namespace,
        "subject": "alice",
        "role": role,
        "status": "active",
        "revoked_at": None,
        "version": int(at.replace(tzinfo=UTC).timestamp() * 1_000_000_000),
    }


@pytest.mark.parametrize(
    ("roles", "fell"),
    [
        (["admin", "viewer"], True),
        (["admin", "admin"], False),
        (["viewer", "admin"], False),
        ([], False),
    ],
)
@pytest.mark.grant_log_check
def test_a_recheck_cuts_a_pair_whose_grant_ranks_below_one_it_held(
    ch: _CH, roles: list[str], fell: bool
) -> None:
    from server.identity.app import grant_writes

    then = datetime(2026, 5, 1, 12, 0, 0)
    ch.log = [_logged_at(role, then + timedelta(seconds=i)) for i, role in enumerate(roles)]
    pair = ("team", "alice")
    assert asyncio.run(grant_writes.recheck(ORG, [pair])) == ([pair] if fell else [])
    assert (rev.grant_claim_key(ORG, *pair) in ch.revoked) is fell


def _cut_raised_under(ch: _CH, roles: list[str], written: int) -> bool:
    """Whether writing log row `written` of `roles` (one per second) cuts the pair."""
    then = datetime(2026, 5, 1, 12, 0, 0)
    ch.log = [_logged_at(role, then + timedelta(seconds=i)) for i, role in enumerate(roles)]
    from server.identity.app import grant_writes

    row = {
        "org": ORG,
        "namespace": "team",
        "subject": "alice",
        "version": ch.log[written]["version"],
    }
    asyncio.run(grant_writes._cut_raised_under([row]))
    return rev.grant_claim_key(ORG, "team", "alice") in ch.revoked


@pytest.mark.parametrize(
    ("roles", "written", "cut"),
    [
        # A raise landed between the writer's read (viewer) and its write.
        (["viewer", "admin", "writer"], 2, True),
        (["viewer", "writer"], 1, False),
        # Only the row just before the write counts: an old lowering is history.
        (["admin", "viewer", "writer"], 2, False),
        # A later write is not this one's to judge.
        (["viewer", "writer", "admin", "viewer"], 1, False),
        (["writer"], 0, False),
    ],
)
@pytest.mark.grant_log_check
def test_a_write_is_cut_when_the_row_before_it_ranks_higher(
    ch: _CH, roles: list[str], written: int, cut: bool
) -> None:
    assert _cut_raised_under(ch, roles, written) is cut


def test_a_newer_cutoff_wins_in_clickhouse_however_late_it_lands(ch: _CH) -> None:
    _end("team")
    row = ch.revoked[rev.grant_claim_key(ORG, "team", "alice")]
    assert row["version"] == int(row["issued_before"].replace(tzinfo=UTC).timestamp()) * 10**9


def test_a_minting_path_reads_the_cutoffs_from_clickhouse(ch: _CH) -> None:
    # The snapshot of this process may not have the cutoff yet; ClickHouse has.
    _end("team")
    rev._revoked_before.clear()
    assert asyncio.run(rev.read_grant_cutoffs(ORG, "alice", ["team", "notes"], issued_at=None)) == {
        "team"
    }
    assert asyncio.run(rev.read_grant_cutoffs(ORG, "alice", ["team"], issued_at=BEFORE)) == {"team"}
    later = int(datetime.now(UTC).timestamp()) + 2
    assert asyncio.run(rev.read_grant_cutoffs(ORG, "alice", ["team"], issued_at=later)) == set()


def test_a_later_grant_does_not_end_a_cutoff_at_mint(ch: _CH) -> None:
    # Not even an admin one: the live grant decides access there, and an
    # exemption would rest on every path that can write an admin row.
    _end("team")
    ch.live = {"team": "admin"}
    assert asyncio.run(rev.read_grant_cutoffs(ORG, "alice", ["team"], issued_at=None)) == {"team"}


def test_a_subject_revocation_is_read_from_clickhouse(ch: _CH) -> None:
    now = int(datetime.now(UTC).timestamp())
    ch.revoked["alice"] = {"jti": "alice", "issued_before": now}
    assert asyncio.run(rev.subjects_revoked(["alice"], issued_at=now - 10))
    assert not asyncio.run(rev.subjects_revoked(["alice"], issued_at=now + 10))
    ch.revoked["alice"] = {"jti": "alice", "issued_before": None}
    assert asyncio.run(rev.subjects_revoked(["alice"], issued_at=now + 10)), "revoked outright"
    assert not asyncio.run(rev.subjects_revoked(["bob"], issued_at=now))


def test_the_mint_time_reads_are_sequentially_consistent(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FINAL resolves the versions a replica holds, not ones it has not fetched:
    # a mint that misses a cutoff keeps the namespace for the token's life.
    from server.core.ch import sequential_reads_active

    _end("team")
    consistent: list[bool] = []
    read = ch.query_all

    async def _recording(sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "revoked_tokens" in sql:
            consistent.append(sequential_reads_active())
        return await read(sql, params)

    monkeypatch.setattr(ch, "query_all", _recording)
    asyncio.run(rev.read_grant_cutoffs(ORG, "alice", ["team"], issued_at=None))
    asyncio.run(rev.subjects_revoked(["alice"], issued_at=BEFORE))
    assert consistent == [True, True], "the cutoffs, the subject"


def test_a_failed_mint_time_read_propagates(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down(*_a: Any, **_k: Any) -> list[dict]:
        raise RuntimeError("replica away")

    monkeypatch.setattr(ch, "query_all", _down)
    with pytest.raises(RuntimeError):
        asyncio.run(rev.read_grant_cutoffs(ORG, "alice", ["team"], issued_at=None))
    with pytest.raises(RuntimeError):
        asyncio.run(rev.subjects_revoked(["alice"], issued_at=BEFORE))


def test_several_subjects_are_checked_for_a_revocation_in_one_read(ch: _CH) -> None:
    reads: list[list[str]] = []
    read = ch.query_all

    async def _counting(sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        reads.append(list((params or {}).get("keys") or []))
        return await read(sql, params)

    ch.query_all = _counting  # type: ignore[method-assign]
    now = int(datetime.now(UTC).timestamp())
    ch.revoked["bob"] = {"jti": "bob", "issued_before": None}
    assert asyncio.run(rev.subjects_revoked(["u-bob", "bob", "u-bob"], issued_at=now))
    assert not asyncio.run(rev.subjects_revoked(["u-bob"], issued_at=now))
    assert not asyncio.run(rev.subjects_revoked([], issued_at=now))
    assert reads == [["bob", "u-bob"], ["u-bob"]]


def test_cutoff_spellings_count_deactivated_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    # A grant revoked after the account was soft-deleted must still reach the
    # token minted under its other spelling.
    from server.auth import aliases

    asked: list[dict[str, Any]] = []

    class _Directory:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict]:
            asked.append(params)
            return [{"owner_email": "alice", "contact_email": "Alice@Acme.com"}]

    got = asyncio.run(aliases.cutoff_spellings(ORG, "alice", ch=_Directory()))
    assert got == ["alice", "Alice@Acme.com"]
    assert asked[0]["all"] is True


def test_a_refusal_after_a_cutoff_says_the_claim_ended(ch: _CH) -> None:
    _end("team")
    for token in (_scoped("team"), _flat("team")):
        with pytest.raises(PermissionError, match="claim on .*/team ended") as refused:
            asyncio.run(check_authorized(token, Role.VIEWER, ORG, "team"))
        assert "does not allow VIEWER" in str(refused.value)


def test_a_lowered_scoped_token_keeps_what_the_new_grant_allows(ch: _CH) -> None:
    # Lowered from admin to writer: a writer-scoped token loses nothing it
    # could do, so it is not refused; it is capped at the grant held now.
    _end("team")
    ch.live["team"] = "writer"
    assert _admits(_scoped("team"), "team", Role.WRITER)
    ch.live["team"] = "viewer"
    assert not _admits(_scoped("team"), "team", Role.WRITER)


def test_a_refresh_that_read_before_the_cutoff_keeps_it(ch: _CH) -> None:
    # The refresh's read went out before the cutoff landed and comes back
    # after the write applied it: the old snapshot must not undo it here.
    stale_rows = dict(ch.revoked)
    _end("team")
    ch.revoked = stale_rows
    asyncio.run(rev.refresh())
    assert not _admits(_flat("team"), "team")


def test_a_newer_cutoff_from_another_process_stands_over_this_ones(ch: _CH) -> None:
    # This process cut at C1; another cut again later at C2. A token minted
    # between the two must be cut here too, not let back in by the older C1.
    _end("team")
    key = rev.grant_claim_key(ORG, "team", "alice")
    ch.revoked[key] = {**ch.revoked[key], "issued_before": datetime(2030, 1, 1)}
    asyncio.run(rev.refresh())
    between = int(datetime(2029, 1, 1, tzinfo=UTC).timestamp())
    assert rev.grant_cutoff_applies(ORG, "team", "alice", between)


def test_a_cutoff_refuses_rather_than_cut_short_past_its_spelling_bound(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server.auth import aliases
    from server.core.errors import TooManySpellingsError

    monkeypatch.setattr(aliases, "_CUTOFF_SPELLINGS", 2)

    class _Directory:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict]:
            assert "DISTINCT" in sql and params["lim"] == 3
            return [{"owner_email": f"u{i}", "contact_email": "a@x.io"} for i in range(3)]

    with pytest.raises(TooManySpellingsError, match="ask your Loom operator") as refused:
        asyncio.run(aliases.cutoff_spellings(ORG, "a@x.io", ch=_Directory()))
    assert refused.value.extra == {"subject": "a@x.io", "limit": 2}
    # How to fix it is for the operator, who reads the log.
    assert "errors.md#too-many-spellings" in caplog.text


def test_a_spelling_refusal_keeps_its_subject_and_bound_through_a_copy() -> None:
    import copy
    import pickle

    from server.core.errors import TooManySpellingsError

    refusal = TooManySpellingsError("too many accounts", subject="a@x.io", limit=2)
    refusal.add_note("while releasing vault")
    for again in (copy.copy(refusal), pickle.loads(pickle.dumps(refusal))):
        assert again.message == "too many accounts"
        assert again.extra == {"subject": "a@x.io", "limit": 2}
        assert again.__notes__ == ["while releasing vault"]


def test_the_documented_spelling_bound_is_the_one_a_cutoff_reads() -> None:
    from server.auth import aliases
    from server.core.errors import TooManySpellingsError
    from server.core.paths import REPO_ROOT

    n = aliases._CUTOFF_SPELLINGS
    errors_md = (REPO_ROOT / "docs/reference/errors.md").read_text()
    (table_row,) = [
        line for line in errors_md.splitlines() if line.startswith("| `TOO_MANY_SPELLINGS`")
    ]
    assert f"more than {n} accounts" in " ".join((TooManySpellingsError.__doc__ or "").split())
    assert f"More than {n} accounts" in table_row
    assert " ".join(errors_md.split()).count(f"at most {n} remain") == 2
    security_md = (REPO_ROOT / "docs/architecture/security.md").read_text()
    assert f"past {n} accounts" in " ".join(security_md.split())


def test_a_refused_cutoff_cancels_the_spelling_reads_still_running(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import TooManySpellingsError

    cancelled: list[str] = []

    async def _spellings(org: str, subject: str, **_k: Any) -> list[str]:
        if subject == "squatted":
            raise TooManySpellingsError("too many accounts", subject=subject, limit=1000)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(subject)
            raise
        return [subject]

    monkeypatch.setattr("server.auth.aliases.cutoff_spellings", _spellings)

    async def _cut() -> None:
        with pytest.raises(TooManySpellingsError):
            await rev.write_grant_cutoffs(
                ORG, ["team"], ["alice", "squatted"], reason="test", revoked_by="admin"
            )
        assert cancelled == ["alice"]

    asyncio.run(_cut())
    assert ch.revoked == {}


def test_every_cutoff_key_of_an_org_starts_with_its_prefix() -> None:
    key = rev.grant_claim_key("it-ok-0123abcd", "team", "alice")
    assert key.startswith(rev.grant_claim_prefix("it-ok-0123abcd"))
    assert not key.startswith(rev.grant_claim_prefix("it-ok-0123abc"))


def test_a_slash_in_an_org_or_namespace_cannot_make_two_scopes_share_a_key() -> None:
    assert rev.grant_claim_key("a/b", "c", "alice") != rev.grant_claim_key("a", "b/c", "alice")


def test_a_lowered_scoped_token_still_lists_the_namespace_its_gate_admits(ch: _CH) -> None:
    # The gate caps it at the live grant; the caller's namespaces must agree,
    # or all_mine and namespace.list_mine stop seeing what it may read.
    _end("team")
    ch.live["team"] = "viewer"
    ctx.set_current_auth(_scoped("team"))
    assert asyncio.run(ctx.resolve_my_namespaces(ORG)) == ["team"]
    ch.live.clear()
    assert asyncio.run(ctx.resolve_my_namespaces(ORG)) == []


@pytest.mark.parametrize(
    ("row", "rank"),
    [
        ({"role": "writer", "status": "active", "revoked_at": None}, 2),
        ({"role": "admin", "status": "pending", "revoked_at": None}, 0),
        ({"role": "admin", "status": "active", "revoked_at": datetime(2030, 1, 1)}, 0),
    ],
)
def test_a_log_row_grants_its_role_only_while_active(row: dict, rank: int) -> None:
    from server.identity.app import grant_writes

    assert grant_writes._rank(row) == rank


def test_a_grant_cutoff_acknowledged_late_does_not_move_this_process_back() -> None:
    key = rev.grant_claim_key(ORG, "team", "alice")
    rev._tighten_locally([key], 200)
    rev._tighten_locally([key], 100)  # an older write, acknowledged after
    assert rev._revoked_before[key] == 200 and rev._written[key][0] == 200


def test_a_cutoff_reads_the_spellings_with_sequential_consistency() -> None:
    from server.auth import aliases
    from server.core.ch import sequential_reads_active

    seen: list[bool] = []

    class _Directory:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict]:
            seen.append(sequential_reads_active())
            return []

    asyncio.run(aliases.cutoff_spellings(ORG, "alice", ch=_Directory()))
    asyncio.run(aliases.subject_aliases(ORG, "bob", ch=_Directory()))
    assert seen == [True, False]


@pytest.mark.grant_log_check
def test_a_lowering_cuts_every_namespace_in_one_insert_each_time(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.app import grant_writes

    async def _landed(rows: list[dict[str, Any]], **_k: Any) -> None:
        return None

    monkeypatch.setattr(grant_writes.grants_repo, "insert_rows", _landed)
    inserts: list[list[dict[str, Any]]] = []
    insert = ch.insert

    async def _counted(table: str, rows: list[dict[str, Any]], **k: Any) -> None:
        inserts.append(rows)
        await insert(table, rows, **k)

    monkeypatch.setattr(ch, "insert", _counted)
    rows = [{"org": ORG, "namespace": ns, "subject": "alice"} for ns in "abc"]
    asyncio.run(grant_writes.lower(ORG, rows, reason="test"))

    first, second = inserts
    keys = {rev.grant_claim_key(ORG, ns, "alice") for ns in "abc"}
    assert {r["jti"] for r in first} == keys == {r["jti"] for r in second}
    assert {r["reason"] for r in second} == {rev.RESTAMPED}
