"""A revocation row must outlive its TTL check. `loom.revoked_tokens` drops rows at
`toDateTime(expires_at) + 10 days`; DateTime ends in 2106, so an `expires_at` past
it wraps to a past date and the row is removed as soon as it is written."""

from datetime import datetime, timedelta, timezone

from server.auth.revocation import revocation_expiry

# The last second `toDateTime` represents, minus the TTL's 10-day tail.
_TTL_SAFE = datetime(2106, 2, 7, 6, 28, 15) - timedelta(days=10)
NOW = datetime(2026, 9, 30, 15, 0, 0)


def test_a_token_without_exp_gets_a_revocation_inside_the_ttl_range() -> None:
    expiry = revocation_expiry(None, NOW)
    assert NOW + timedelta(days=365) < expiry < _TTL_SAFE


def test_an_expiry_past_the_datetime_range_is_clamped() -> None:
    assert revocation_expiry(datetime(2200, 1, 1), NOW) < _TTL_SAFE


def test_an_ordinary_expiry_is_kept_as_naive_utc() -> None:
    aware = datetime(2026, 10, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert revocation_expiry(aware, NOW) == datetime(2026, 10, 1, 10, 0)
    assert revocation_expiry(datetime(2026, 10, 1, 10, 0), NOW) == datetime(2026, 10, 1, 10, 0)


def test_the_schema_ttl_is_the_one_this_clamp_protects() -> None:
    from server.core.paths import SCHEMA_SQL

    ddl = SCHEMA_SQL.read_text()
    table = ddl[ddl.index("CREATE TABLE IF NOT EXISTS loom.revoked_tokens") :]
    table = table[: table.index(";")]
    assert "TTL toDateTime(expires_at) + toIntervalDay(10)" in table
