"""Per-archetype acceptance benches.

One runnable bench per consumer archetype, each against any deployment
(`--base-url --token --namespace`), each scoring the precision of what loom
serves FIRST and the archetype's own economy second, and each printing a
one-screen report with pass / fail against its thresholds:

    code_agent   localization precision, then retrieval calls per answer
    memory_app   knowledge-update + stale-retrieval precision, then $ per ask
    plugin       wrong-file rate, then tool-call reduction
    harness      judged accuracy, then $ per answer from the ledger

Every bench speaks to the server through `loom.reference_client.ReferenceClient`
— the access layer docs/getting-started/quickstart.md hands a new consumer — so the kit is
exercised by the benches that accept it.
"""
