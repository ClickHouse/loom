import tempfile
import unittest
from pathlib import Path

import yaml
from harbor.models.task.config import TaskConfig
from harbor.models.task.paths import TaskPaths
from harbor.models.task.verifier_mode import resolve_verifier_environment_definition
from harbor.models.trial.config import EnvironmentConfig

from examples.software_factory.harbor import study as harbor_study


class HarborSandboxPolicy(unittest.TestCase):
    def assert_constraints(self, path):
        policy = yaml.safe_load(path.read_text())["services"]["main"]
        self.assertEqual(policy["pids_limit"], 64)
        self.assertEqual(policy["cap_drop"], ["ALL"])
        self.assertEqual(set(policy["cap_add"]), {"SETUID", "SETGID", "KILL"})
        self.assertEqual(policy["security_opt"], ["no-new-privileges:true"])
        self.assertEqual(policy["network_mode"], "none")

    def test_agent_overlay_round_trips_through_native_environment_config(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = harbor_study.stage_sandbox_policy(Path(directory))
            config = EnvironmentConfig(type="docker", extra_docker_compose=[policy])
            loaded = EnvironmentConfig.model_validate_json(config.model_dump_json())
            self.assertEqual(loaded.extra_docker_compose, [policy])
            self.assert_constraints(policy)

    def test_separate_verifier_keeps_its_own_policy_when_job_overlays_are_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            task = (Path(directory) / "task").resolve()
            task.mkdir()
            policy = harbor_study.stage_sandbox_policy(task, verifier=True)
            image = "sha256:" + "a" * 64
            config = TaskConfig.model_validate(
                {
                    "environment": {"docker_image": image},
                    "verifier": {
                        "environment_mode": "separate",
                        "environment": {"docker_image": image},
                    },
                }
            )
            definition = resolve_verifier_environment_definition(config, TaskPaths(task))
            self.assertEqual(definition.directory, task / "tests")
            self.assertTrue(definition.bundled_tests)
            self.assertEqual(definition.directory / "docker-compose.yaml", policy)
            self.assert_constraints(policy)
