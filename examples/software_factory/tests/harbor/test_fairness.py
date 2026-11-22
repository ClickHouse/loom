"""Treatment configuration uses native MCP and the same coding agent."""

import tomllib
from unittest import TestCase
from unittest.mock import patch

from examples.software_factory.harbor.native_environment import agent_settings, mcp_settings


class NativeTreatment(TestCase):
    def test_baseline_has_no_mcp_or_extra_prompt_and_loom_scope_is_connection_config(self):
        protocol = {"loom_url": "http://owned-loom:7777"}
        baseline = {"order": 0, "mode": "baseline"}
        loom = {"order": 1, "mode": "natural"}
        binding = {"org": "org", "code_namespace": "code"}
        with patch(
            "examples.software_factory.harbor.native_environment.binding_for", return_value=binding
        ):
            self.assertEqual(mcp_settings(protocol, baseline), "")
            config = tomllib.loads(mcp_settings(protocol, loom))
        server = config["environment"]["mcp_servers"][0]
        self.assertEqual(server["url"], "http://owned-loom:7777/mcp")
        self.assertEqual(server["headers"]["X-Loom-Code-Scope"], "org/code")
        self.assertEqual(server["headers"]["Authorization"], "Bearer ${LOOM_ATTEMPT_TOKEN_1}")
        for slot in (baseline, loom):
            settings = tomllib.loads(agent_settings(protocol, slot))["agent"]
            self.assertEqual(settings["user"], "agent")
            self.assertEqual(
                set(settings) - {"network_mode", "allowed_hosts"}, {"user", "timeout_sec"}
            )
