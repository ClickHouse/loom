"""The test inventory and the feature-to-test ledger.

`build` derives one row per tracked file under `tests/` from the live tree;
`ledger` enumerates product features from their registries and finds the
tests that reference each one. Human judgement (owner, status, tags, flake
history, removal evidence) lives in `annotations/<area>.toml`; features with
no test yet are listed with a reason in `gaps.toml`. The unit gate
`tests/unit/test_test_inventory.py` keeps all three in step with the tree.
"""
