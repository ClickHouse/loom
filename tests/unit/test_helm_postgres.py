"""The chart's Postgres memory authority: the DSN, password, CA mount and egress
every configured pod gets, and the refusals beside `postgres.host`
(`scripts/gates/helm_postgres.py`)."""

from __future__ import annotations

import shutil

import pytest

from scripts.gates import helm_postgres


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm (the CI helm job runs it)")
def test_every_configured_pod_connects_to_postgres_over_verify_full() -> None:
    helm_postgres.main()
