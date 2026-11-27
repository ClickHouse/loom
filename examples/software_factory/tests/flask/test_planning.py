"""Project metadata preserves authored families and their pinned dependency inheritance."""

import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.evals.flask.public_metadata import public_reproduction
from examples.software_factory.harbor import planning


class ProjectMetadata(unittest.TestCase):
    def test_authored_cases_inherit_packages_without_becoming_historical_issues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "authored.json"
            fixture.write_text(
                json.dumps(
                    dict(
                        case_id="authored",
                        family="authored-simulated-memory-history",
                        dependency_task_id="original",
                    )
                )
            )
            manifest = root / "cases.json"
            manifest.write_text(
                json.dumps(
                    dict(
                        tasks=[
                            dict(task_id="original", base_commit="a" * 40),
                            dict(
                                task_id="authored",
                                base_commit="b" * 40,
                                scenario_family="authored-simulated-memory-history",
                            ),
                        ]
                    )
                )
            )
            lock = dict(
                id="flask",
                study_role="development fixture",
                supplemental_records=[],
                selector_policy="complete-test-functions",
                repository_url="public",
                languages=["python"],
                source_roots=["src/flask"],
                model_id="model",
                dataset=dict(
                    repository="public", revision="pin", file="cases", sha256="c" * 64, bytes=1
                ),
                tasks=[
                    dict(
                        task_id="original",
                        record_sha256="d" * 64,
                        requirements=["sample==1"],
                        build_requirements=["build==2"],
                    )
                ],
                python_image="python@sha256:pin",
                package_sha256={"sample==1": ["e" * 64]},
                compiler_tools=dict(
                    dependency_environment=[],
                    scip_python="0.6.6",
                    node="22.23.4",
                    python_language_version="3.11",
                ),
                attributions=[],
            )
            identity = dict(commit="f" * 40, dirty=False, relative_files={})
            protocol = dict(
                tasks={
                    "original": dict(image="same"),
                    "authored": dict(image="same", public_fixture_file=str(fixture)),
                },
                historical_manifest=str(manifest),
                controller_source_identity=identity,
                server_source_identity=identity,
                server_commit="f" * 40,
                loom_mcp={
                    "schema_profile": "standard",
                    "tool_catalog_sha256": "a" * 64,
                    "standard_catalog_sha256": "a" * 64,
                },
                cost_basis={
                    key: 0.1
                    for key in (
                        "source",
                        "input_usd_per_million",
                        "output_usd_per_million",
                        "prompts_over_100000_input",
                        "prompts_over_100000_output",
                    )
                },
            )
            result = public_reproduction(lock, protocol)
            self.assertEqual(
                result["implementation"],
                planning.public_reproduction_base(lock, protocol)["implementation"],
            )
            self.assertEqual(
                [row["scenario_family"] for row in result["tasks"]],
                ["historical-issue", "authored-simulated-memory-history"],
            )
            self.assertEqual(
                result["dependencies"]["tasks"][0]["packages"],
                result["dependencies"]["tasks"][1]["packages"],
            )
            self.assertEqual(result["dependencies"]["tasks"][1]["dependency_task_id"], "original")
            self.assertEqual(result["tasks"][1]["base_commit"], "b" * 40)
