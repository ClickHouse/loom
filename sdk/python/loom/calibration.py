"""Calibration client — the similarity thresholds in effect for a namespace
and where they came from (`source`: `namespace` = a per-namespace corpus,
`cluster` = the boot probe, `scalar` = no probe ran, the settings apply).

    cal = await agent.calibration.get()
    print(f"using {cal['source']} calibration on {cal['embedder_model']}")
    print(f"  noise floor: {cal['noise_floor']:.3f}")
    print(f"  rerank skip-above: {cal['rerank_skip_above']:.3f}")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class CalibrationClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def get(self) -> dict[str, Any]:
        """The effective calibration for this agent's namespace, shaped as
        `loom.ops.CalibrationGetOutput` (percentiles are zero when
        `source == "scalar"`)."""
        return await self._agent.transport.post(
            "/v1/calibration.get",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
            },
        )
