# Bounded HTTP schema contract

Run this pilot from an isolated source checkout:

```bash
uv sync --locked --extra dev --extra code --extra contract
uv run --no-sync python -m pytest -p tests.unit.conftest tests/contracts/test_http_schema.py -v
```

[Schemathesis](https://schemathesis.readthedocs.io/en/stable/guides/python-apps/)
generates the request cases in process. The **HTTP schema contract** workflow runs the same ten tests on standard public
runners. Three properties use a twenty-four-example generation budget each from the actual
`blacklist.list` OpenAPI request schema. A generation-only copy selects declared
fields and limits strings to thirty-two characters; the served schema and response
validation remain unchanged. Unknown names have a separate fixed regression. They validate successful responses, require
cross-org and cross-namespace refusal, and require missing or invalid tokens to fail
before the backing store is called. Six fixed cases check invalid JSON types and
limit bounds. A fixed Unicode-key case verifies the encoded header and unchanged JSON
feedback. The output must contain ten executed tests without skips or errors.

The pilot uses the actual HTTP router, wire models, signed synthetic tokens,
authorization gates and blacklist handler. The existing unit-test fixtures replace
external stores and side effects; the blacklist read returns a synthetic scoped row.
This qualifies the transport boundary with test doubles. It does not prove database
isolation, production deployment behavior or every operation's contract. Generated
positive payloads are bound to known synthetic identities before each request; this
is not a general negative-schema generation or whole-API coverage claim.

The generator has deterministic examples, no persistent example database and a
reproduction blob on failure. Shrinking shares the same runtime budget. A two-minute subprocess limit and ten-minute job limit
bound execution. No network target, server, database or provider is started. Security
findings use the private route in [SECURITY.md](../../SECURITY.md); public CI should
contain only synthetic cases.

PR runs have read-only permissions. Trusted main and weekly runs reconcile one
maintenance task for a broken pilot, append only changed statuses and preserve human
bodies and closed decisions while the task retains its original title or body marker.
Removing both identifiers makes it unrecognizable to later runs. The issue task
contains no request bodies or credentials.
No artifact upload, paid account or extra credential is required.
