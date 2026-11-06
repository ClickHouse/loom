---
paths:
  - "tests/**"
---

# Tests

- Unit tests need no ClickHouse server (`tests/unit/ch_fake.py`) and pass in any order and beside each other (`pytest -n auto -p randomly`; the rules are in [tests/README.md](../../tests/README.md#rules-every-test-keeps)); the few that run real SQL through the `clickhouse` binary (clickhouse-local) skip without it.
- Time goes through a seam (`tests/unit/clock_fake.py`); a literal date in a fixture plus a wall-clock check is a test that breaks on a future day. `await asyncio.sleep`, never `time.sleep`, in async tests.
- Integration tests skip without `LOOM_BASE_URL` / `LOOM_CH_HOST` (`os.environ.get(...) or pytest.skip(...)`), use `unique_org(...)` and `mint_test_token`, and never depend on self-signup.
- A write-path test writes a real row, reads it back and compares every field.
- Never `skip`, `xfail` or loosen an assertion to get green; fix the code or the test and say which in the PR.
- Guide: [testing](../../docs/development/testing.md).
