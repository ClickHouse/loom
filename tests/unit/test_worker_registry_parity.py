"""Guard the worker registry's "single source of truth" invariant.

`worker_meta()` (the set rendered on /v1/admin/workers.status, including
disabled workers) and `enabled_workers()` (the set the supervisor actually
runs) are both read off `_WORKERS`. With every worker enabled, the two must
enumerate exactly the same set, so a worker cannot drop from the ops dashboard
or the supervised loop with green CI.
"""

import pytest

from server.settings import Settings, settings
from server.workers.registry import enabled_workers, worker_meta

# Every boolean `*_enabled` setting, switched on: the worker flags and the
# second gates some workers ask (`code_intel_enabled`). Derived rather than
# listed, so a new default-off worker cannot be missing from both sides and
# still pass.
_WORKER_ENABLE_FLAGS = sorted(
    name
    for name, field in Settings.model_fields.items()
    if name.endswith("_enabled") and field.annotation is bool
)


def test_worker_meta_matches_enabled_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for flag in _WORKER_ENABLE_FLAGS:
        monkeypatch.setattr(settings, flag, True)

    supervised = {w.name for w in enabled_workers()}
    declared = set(worker_meta())

    assert supervised == declared, (
        "worker_meta() and the supervised worker set disagree — a worker is "
        f"missing from one of them. only-in-meta={declared - supervised}; "
        f"only-supervised={supervised - declared}"
    )


def test_the_workers_reference_lists_exactly_the_registered_workers() -> None:
    """docs/reference/workers.md is what an operator reads to turn a worker on. Its
    table is generated from the registry rows; this holds it to the worker metadata
    too, so a worker the supervisor knows but the table lacks fails here."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "docs" / "reference" / "workers.md").read_text()
    head = text.index("| Worker ")
    table = text[head : text.index("\n\n", head)]
    documented = {
        m.group(1) for ln in table.splitlines()[2:] if (m := re.match(r"\| `([a-z_]+)` \|", ln))
    }
    assert documented == set(worker_meta()), (
        f"undocumented={set(worker_meta()) - documented}, "
        f"not registered={documented - set(worker_meta())}"
    )
