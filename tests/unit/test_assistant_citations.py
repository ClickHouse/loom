"""Citation parsing preserves complete source keys and section names."""

from __future__ import annotations

from server.assistant.domain.citations import parse

SQL = "SELECT agent, count() FROM memory GROUP BY agent"
ANSWER = f"""handbook-bot wrote 7 [memory:policy/deploy/freeze@v12], see
[doc:handbook/deploy-runbook.md § Deploy runbook > Canary, revision 3] and
[doc:contracts/hosting.txt] and [code:server/main.py:40], and
[memory:policy/deploy/freeze@v12] again.

```sql
{SQL}
```
"""


def test_every_citation_form_is_parsed_once() -> None:
    cited = parse(ANSWER)
    assert [c.kind for c in cited] == ["memory", "doc", "doc", "code", "sql"]
    memory, runbook, contract, code, sql = cited
    assert memory.key == "policy/deploy/freeze"
    assert runbook.source_uri == "handbook/deploy-runbook.md"
    assert runbook.section == ("Deploy runbook", "Canary")
    assert contract.section == () and code.path == "server/main.py"
    assert sql.sql == SQL


# The memory key the answer's own tool calls were returned (its run's exposure rows).
MEMORY_KEY = "policy/deploy/freeze"


def test_a_key_with_an_at_sign_and_a_section_with_a_comma_are_parsed_whole() -> None:
    memory, doc = parse("[memory:user/pref/a@b.com@v3] [doc:spec.pdf § Billing, refunds]")
    assert memory.key == "user/pref/a@b.com"
    assert doc.section == ("Billing, refunds",)
