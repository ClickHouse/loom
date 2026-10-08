from server.code.domain.index.profiles import profile_for_repo


def test_clickhouse_profile_is_explicit() -> None:
    profile = profile_for_repo("ClickHouse/ClickHouse")
    assert profile.name == "clickhouse"
    assert profile.sql_registration_names
    assert profile.clickhouse_test_conventions
    assert profile.clickhouse_error_codes


def test_other_cpp_and_sql_repositories_stay_generic() -> None:
    assert profile_for_repo("acme/warehouse").name == "generic"
    assert profile_for_repo("gitlab.example/acme/clickhouse-fork").name == "generic"
