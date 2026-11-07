"""No Loom pod reaches the instance metadata service, with the chart's
NetworkPolicy on or off (`scripts/gates/helm_metadata_egress.py`)."""

from __future__ import annotations

import shutil

import pytest

from scripts.gates import helm_metadata_egress


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm (the CI helm job runs it)")
def test_no_loom_pod_reaches_the_metadata_service() -> None:
    helm_metadata_egress.main()
