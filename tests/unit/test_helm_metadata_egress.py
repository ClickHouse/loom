"""The chart keeps every Loom pod off the instance metadata service, with the
NetworkPolicy on or off (`scripts/gates/helm_metadata_egress.py`)."""

from __future__ import annotations

import shutil

import pytest


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm (the CI helm job runs it)")
def test_no_loom_pod_reaches_the_metadata_service() -> None:
    from scripts.gates import helm_metadata_egress

    helm_metadata_egress.main()
