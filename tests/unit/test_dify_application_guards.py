"""Pure guard regressions; no runtime, Docker, network or real signal operation."""

import signal
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipInfo

import httpx

from scripts.gates import dify_application_scenario as scenario
from scripts.gates import dify_application_smoke as smoke
from tests.unit.clock_fake import ManualClock


class DifyApplicationGuards(unittest.TestCase):
    def test_tools_only_host_disables_unused_vector_subsystem(self):
        with TemporaryDirectory() as directory:
            environment = {
                "GITHUB_ACTIONS": "true",
                "RUNNER_ENVIRONMENT": "github-hosted",
                "RUNNER_OS": "Linux",
                "GITHUB_WORKSPACE": directory,
                "RUNNER_TEMP": directory,
                "PATH": "/usr/bin",
                "COMPOSE_PROFILES": "foreign-profile",
                "VECTOR_STORE": "foreign-backend",
            }
            with (
                patch.dict(smoke.os.environ, environment, clear=True),
                patch.object(smoke.sys, "path", list(smoke.sys.path)),
                patch.object(smoke.Runtime, "persist"),
            ):
                runtime = smoke.Runtime()
            self.assertEqual(runtime.env["COMPOSE_PROFILES"], "postgresql")
            self.assertEqual(runtime.env["VECTOR_STORE"], "")
            self.assertEqual(
                smoke.SERVICES,
                {
                    "agent_backend",
                    "agent_ssrf_proxy",
                    "api",
                    "db_postgres",
                    "init_permissions",
                    "local_sandbox",
                    "nginx",
                    "plugin_daemon",
                    "redis",
                    "sandbox",
                    "ssrf_proxy",
                    "web",
                    "worker",
                    "worker_beat",
                },
            )

    def test_tools_only_host_refuses_resolved_backend_or_service_drift(self):
        def configuration():
            return {
                "services": {name: {"environment": {"VECTOR_STORE": ""}} for name in smoke.SERVICES}
            }

        smoke.validate_platform_services(configuration())
        for name in ("api", "worker", "worker_beat"):
            for value in (None, "foreign-backend"):
                config = configuration()
                config["services"][name]["environment"]["VECTOR_STORE"] = value
                with (
                    self.subTest(service=name, value=value),
                    self.assertRaisesRegex(RuntimeError, "Tools-only vector configuration differs"),
                ):
                    smoke.validate_platform_services(config)
        for service in ("unexpected", "api"):
            config = configuration()
            if service == "api":
                del config["services"][service]
            else:
                config["services"][service] = {}
            with (
                self.subTest(service=service),
                self.assertRaisesRegex(RuntimeError, "Official service closure differs"),
            ):
                smoke.validate_platform_services(config)

    @staticmethod
    def plugin_storage_service():
        return {
            "volumes": [
                {
                    "type": "bind",
                    "source": "/owned/dify/docker/volumes/plugin_daemon",
                    "target": "/app/storage",
                }
            ]
        }

    def test_plugin_storage_requires_exact_writable_official_bind_and_image_user(self):
        service = self.plugin_storage_service()
        self.assertEqual(
            smoke.plugin_storage_source(service, Path("/owned/dify")),
            Path("/owned/dify/docker/volumes/plugin_daemon"),
        )
        for field, value in (
            ("source", "/foreign/storage"),
            ("source", "/owned/dify/docker/volumes/other"),
            ("type", "volume"),
            ("read_only", True),
        ):
            changed = self.plugin_storage_service()
            changed["volumes"][0][field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaisesRegex(RuntimeError, "storage binding differs"),
            ):
                smoke.plugin_storage_source(changed, Path("/owned/dify"))
        for changed in (
            {"volumes": []},
            {"volumes": service["volumes"] * 2},
            {**service, "user": "0:0"},
        ):
            with (
                self.subTest(service=changed),
                self.assertRaisesRegex(RuntimeError, "storage binding differs"),
            ):
                smoke.plugin_storage_source(changed, Path("/owned/dify"))

    def storage_runtime(self):
        runtime = smoke.Runtime.__new__(smoke.Runtime)
        service = {**self.plugin_storage_service(), "image": "sha256:owned-image"}
        runtime.cfg = {"services": {"plugin_daemon": service}}
        runtime.expected_images = {"plugin_daemon": "sha256:owned-image"}
        runtime.path = Path("/owned/dify")
        runtime.project = "owned-project"
        runtime.docker_json = Mock(
            return_value=[{"Id": "sha256:owned-image", "Config": {"User": "dify"}}]
        )
        runtime.compose, runtime.discover, runtime.record = Mock(), Mock(), Mock()
        runtime.retire_storage_helper = Mock()
        storage = Mock()
        storage.exists.return_value = False
        storage.is_symlink.return_value = False
        storage.parent.resolve.return_value = storage.parent
        storage.iterdir.return_value = iter(())
        storage.stat.side_effect = [
            SimpleNamespace(st_dev=1, st_ino=2, st_uid=1001, st_gid=1001),
            SimpleNamespace(st_dev=1, st_ino=2, st_uid=1000, st_gid=1000),
        ]
        return runtime, storage

    def test_storage_init_checks_census_and_changes_only_empty_bind_owner(self):
        runtime, storage = self.storage_runtime()
        operations = Mock()
        operations.attach_mock(runtime.discover, "discover")
        operations.attach_mock(runtime.compose, "compose")
        operations.attach_mock(runtime.retire_storage_helper, "retire")
        with (
            patch.object(smoke, "plugin_storage_source", return_value=storage),
            patch.object(smoke.os, "getuid", return_value=1001),
        ):
            runtime.initialize_plugin_storage()
        storage.mkdir.assert_called_once_with(mode=0o700, parents=True)
        self.assertEqual(
            [call[0] for call in operations.mock_calls], ["discover", "compose", "retire"]
        )
        args, options = runtime.compose.call_args
        self.assertEqual(
            args[:-1],
            (
                "run",
                "--name",
                "owned-project-storage-init",
                "--no-deps",
                "--pull",
                "never",
                "--user",
                "0:0",
                "--entrypoint",
                "/bin/sh",
                "plugin_daemon",
                "-ec",
            ),
        )
        self.assertTrue(args[-1].endswith('chown "$(id -u dify):$(id -g dify)" /app/storage'))
        self.assertNotIn("chown -R", args[-1])
        self.assertNotIn("chmod", args[-1])
        self.assertEqual(options, {"timeout": 30})
        self.assertNotIn("--rm", args)
        self.assertNotIn("user", runtime.cfg["services"]["plugin_daemon"])
        runtime.retire_storage_helper.assert_called_once_with("owned-project-storage-init")

    @staticmethod
    def storage_helper():
        return {
            "Id": "helper-id",
            "Name": "/owned-project-storage-init",
            "Created": "helper-birth",
            "Image": "sha256:owned-image",
            "State": {"Status": "exited", "ExitCode": 0, "OOMKilled": False},
            "HostConfig": {"AutoRemove": False},
        }

    def test_successful_helper_retires_exact_id_before_platform_readiness(self):
        runtime = smoke.Runtime.__new__(smoke.Runtime)
        runtime.containers = {
            "helper-id": {"service": "plugin_daemon", "created": "helper-birth"},
            "service-id": {"service": "plugin_daemon", "created": "service-birth"},
        }
        runtime.expected_images = {"plugin_daemon": "sha256:owned-image"}
        runtime.discover, runtime.command, runtime.ids = Mock(), Mock(), Mock(return_value=set())
        runtime.docker_json = Mock(
            side_effect=[[self.storage_helper()]],
        )
        runtime.retire_storage_helper("owned-project-storage-init")
        self.assertEqual(set(runtime.containers), {"service-id"})
        runtime.command.assert_called_once_with(["docker", "container", "rm", "-v", "helper-id"])
        runtime.ids.assert_called_once_with("container", "-a", "--no-trunc")
        runtime.docker_json.side_effect = None
        runtime.docker_json.return_value = [{"State": {"Status": "running"}}]
        runtime.platform_ready()
        self.assertEqual(
            runtime.docker_json.call_args, unittest.mock.call("container", "inspect", "service-id")
        )

    def test_failed_or_unverified_helper_stays_in_census(self):
        for changed in (
            {"State": {"Status": "running", "ExitCode": 0}},
            {"State": {"Status": "exited", "ExitCode": 1}},
            {"State": {"Status": "exited", "ExitCode": 0, "OOMKilled": True}},
            {"Created": "reused-birth"},
            {"HostConfig": {"AutoRemove": True}},
        ):
            runtime = smoke.Runtime.__new__(smoke.Runtime)
            runtime.containers = {
                "helper-id": {"service": "plugin_daemon", "created": "helper-birth"}
            }
            runtime.expected_images = {"plugin_daemon": "sha256:owned-image"}
            runtime.discover, runtime.command = Mock(), Mock()
            runtime.docker_json = Mock(return_value=[{**self.storage_helper(), **changed}])
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(RuntimeError, "completion differs"),
            ):
                runtime.retire_storage_helper("owned-project-storage-init")
            runtime.command.assert_not_called()
            self.assertIn("helper-id", runtime.containers)

    def test_helper_census_retires_only_after_exact_removal_is_proven(self):
        runtime = smoke.Runtime.__new__(smoke.Runtime)
        runtime.containers = {"helper-id": {"service": "plugin_daemon", "created": "helper-birth"}}
        runtime.expected_images = {"plugin_daemon": "sha256:owned-image"}
        runtime.discover, runtime.command = Mock(), Mock()
        runtime.ids = Mock(return_value={"helper-id"})
        runtime.docker_json = Mock(return_value=[self.storage_helper()])
        with self.assertRaisesRegex(RuntimeError, "helper remains"):
            runtime.retire_storage_helper("owned-project-storage-init")
        self.assertIn("helper-id", runtime.containers)

    def test_failed_helper_remains_for_normal_stop_remove_and_remaining_resource_cleanup(self):
        runtime, storage = self.storage_runtime()
        runtime.compose.side_effect = RuntimeError("helper failed")
        with (
            patch.object(smoke, "plugin_storage_source", return_value=storage),
            patch.object(smoke.os, "getuid", return_value=1001),
            self.assertRaisesRegex(RuntimeError, "helper failed"),
        ):
            runtime.initialize_plugin_storage()
        runtime.retire_storage_helper.assert_not_called()
        autoremove = "--rm" in runtime.compose.call_args.args
        resources = {
            "container": {"helper-id"},
            "volume": {"owned-volume"},
            "network": {"owned-network"},
        }
        runtime.containers = {"helper-id": {"service": "plugin_daemon", "created": "helper-birth"}}
        runtime.volumes, runtime.networks = {"owned-volume": "birth"}, {"owned-network": "birth"}
        runtime.created, runtime.processes, runtime.cleanup_callbacks, runtime.threads = (
            True,
            [],
            [],
            [],
        )
        runtime.redact = str
        runtime.ids = Mock(side_effect=lambda kind, *_: resources[kind].copy())
        runtime.docker_json = Mock(
            side_effect=lambda kind, *_: [
                {
                    "Created": "helper-birth" if kind == "container" else "birth",
                    "CreatedAt": "birth",
                }
            ]
        )

        def command(args, **_):
            kind, action, ident = args[1], args[2], args[-1]
            if action == "rm":
                if ident not in resources[kind]:
                    raise RuntimeError("missing resource")
                resources[kind].remove(ident)
            elif action == "stop" and autoremove:
                resources[kind].remove(ident)

        runtime.command = Mock(side_effect=command)
        with patch.object(smoke, "stop_monitor"):
            runtime.cleanup()
        self.assertEqual(resources, {"container": set(), "volume": set(), "network": set()})
        self.assertTrue(runtime.record.call_args.args[1]["verified"])

    def discovery_runtime(self):
        runtime = smoke.Runtime.__new__(smoke.Runtime)
        runtime.project, runtime.path = "owned-project", Path("/owned/dify")
        runtime.started = "2026-01-01T00:00:00+00:00"
        runtime.before = {"container": set(), "volume": set(), "network": set()}
        runtime.expected_images = {"plugin_daemon": "sha256:owned-image"}
        runtime.containers = {"previous-id": {"service": "plugin_daemon", "created": "previous"}}
        runtime.volumes, runtime.networks = {}, {}
        resources = {
            "container": {"helper-id"},
            "volume": {"owned-volume"},
            "network": {"owned-network"},
        }
        labels = {"com.docker.compose.project": runtime.project}
        helper = {
            **self.storage_helper(),
            "Created": runtime.started,
            "Config": {"Labels": {**labels, "com.docker.compose.service": "plugin_daemon"}},
            "HostConfig": {"Memory": smoke.GIB, "AutoRemove": False},
            "Mounts": [],
        }
        rows = {
            "container": helper,
            "volume": {"Name": "owned-volume", "CreatedAt": runtime.started, "Labels": labels},
            "network": {"Created": runtime.started, "Labels": labels},
        }

        def inspect(kind, _operation, ident):
            if kind == "container" and ident == "owned-project-storage-init":
                ident = "helper-id"
            if ident not in resources[kind]:
                raise RuntimeError("inspected missing resource")
            return [rows[kind]]

        runtime.ids = Mock(side_effect=lambda kind, *_: resources[kind].copy())
        runtime.docker_json = Mock(side_effect=inspect)
        runtime.command = Mock(side_effect=lambda args, **_: resources[args[1]].remove(args[-1]))
        return runtime, resources, rows

    def test_deleted_helper_then_census_error_still_allows_remaining_normal_cleanup(self):
        runtime, resources, _ = self.discovery_runtime()
        observed_ids = runtime.ids.side_effect

        def ids(kind, *filters):
            if kind == "container" and "--filter" not in filters:
                raise RuntimeError("absence census interrupted")
            return observed_ids(kind, *filters)

        runtime.ids.side_effect = ids
        with self.assertRaisesRegex(RuntimeError, "absence census interrupted"):
            runtime.retire_storage_helper("owned-project-storage-init")
        self.assertEqual(resources["container"], set())
        self.assertIn("helper-id", runtime.containers)
        runtime.created, runtime.processes, runtime.cleanup_callbacks, runtime.threads = (
            True,
            [],
            [],
            [],
        )
        runtime.redact, runtime.record = str, Mock()
        with patch.object(smoke, "stop_monitor"):
            runtime.cleanup()
        self.assertEqual(runtime.containers, {})
        self.assertEqual(resources, {"container": set(), "volume": set(), "network": set()})
        self.assertTrue(runtime.record.call_args.args[1]["verified"])

    def test_failed_complete_census_keeps_previous_container_map(self):
        runtime, _, _ = self.discovery_runtime()
        previous = runtime.containers
        observed_ids = runtime.ids.side_effect

        def ids(kind, *filters):
            if kind == "network":
                raise RuntimeError("network census failed")
            return observed_ids(kind, *filters)

        runtime.ids.side_effect = ids
        with self.assertRaisesRegex(RuntimeError, "network census failed"):
            runtime.discover()
        self.assertIs(runtime.containers, previous)
        self.assertEqual(set(runtime.containers), {"previous-id"})
        runtime.command.assert_not_called()

    def test_fresh_census_refuses_foreign_replacement_without_replacing_old_map(self):
        runtime, resources, rows = self.discovery_runtime()
        previous = runtime.containers
        resources["container"] = {"replacement-id"}
        rows["container"].update(Id="replacement-id", Image="sha256:foreign-image")
        with self.assertRaisesRegex(RuntimeError, "Container ownership differs"):
            runtime.discover()
        self.assertIs(runtime.containers, previous)
        runtime.command.assert_not_called()

    def test_existing_or_symlinked_storage_refuses_before_privileged_helper(self):
        cases = ((True, False, False), (False, True, False), (False, False, True))
        for exists, symlink, foreign_parent in cases:
            runtime, storage = self.storage_runtime()
            storage.exists.return_value = exists
            storage.is_symlink.return_value = symlink
            if foreign_parent:
                storage.parent.resolve.return_value = Path("/foreign")
            with (
                self.subTest(exists=exists, symlink=symlink, foreign_parent=foreign_parent),
                patch.object(smoke, "plugin_storage_source", return_value=storage),
                self.assertRaisesRegex(RuntimeError, "must be fresh"),
            ):
                runtime.initialize_plugin_storage()
            storage.mkdir.assert_not_called()
            runtime.compose.assert_not_called()

    def test_changed_image_or_root_image_user_refuses_storage_initialization(self):
        for image in (
            {"Id": "sha256:other-image", "Config": {"User": "dify"}},
            {"Id": "sha256:owned-image", "Config": {"User": "0:0"}},
        ):
            runtime, _ = self.storage_runtime()
            runtime.docker_json.return_value = [image]
            with (
                self.subTest(image=image),
                patch.object(smoke, "plugin_storage_source") as source,
                self.assertRaisesRegex(RuntimeError, "image differs"),
            ):
                runtime.initialize_plugin_storage()
            source.assert_not_called()
            runtime.compose.assert_not_called()

    def test_starting_fixture_can_reset_before_healthy_within_same_budget(self):
        client = Mock()
        client.get.side_effect = [
            httpx.ReadError("reset"),
            httpx.Response(503),
            httpx.Response(200),
        ]
        clock = ManualClock()
        result = smoke.wait_fixture_ready(
            client, "http://127.0.0.1/ping", 3, now=clock.monotonic, pause=clock.sleep
        )
        self.assertEqual(result, [{"error_type": "ReadError"}, {"status": 503}, {"status": 200}])
        self.assertEqual(clock.t, 2)
        self.assertEqual(client.get.call_count, 3)

    def test_persistent_fixture_reset_exhausts_original_readiness_budget(self):
        client = Mock()
        client.get.side_effect = httpx.ReadError("reset")
        clock = ManualClock()
        with self.assertRaisesRegex(RuntimeError, "readiness failed.*ReadError"):
            smoke.wait_fixture_ready(
                client, "http://127.0.0.1/ping", 2, now=clock.monotonic, pause=clock.sleep
            )
        self.assertEqual(clock.t, 2)
        self.assertEqual(client.get.call_count, 2)

    def test_fixture_permission_refusal_is_terminal_without_another_poll(self):
        client = Mock()
        client.get.return_value = httpx.Response(403)
        clock = ManualClock()
        with self.assertRaisesRegex(RuntimeError, "permission refused"):
            smoke.wait_fixture_ready(
                client, "http://127.0.0.1/ping", 2, now=clock.monotonic, pause=clock.sleep
            )
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(clock.t, 0)

    def test_credentials_stay_in_memory_and_are_redacted_without_stdout(self):
        runtime = smoke.Runtime.__new__(smoke.Runtime)
        runtime.secrets = []
        with patch("builtins.print") as output:
            self.assertEqual(runtime.secret("test-credential"), "test-credential")
        output.assert_not_called()
        self.assertEqual(runtime.redact("value=test-credential"), "value=[REDACTED]")

    def test_uncovered_image_volume_gets_explicit_owned_compose_volume(self):
        service = {"volumes": [{"target": "/var/lib/postgresql/data/pgdata"}]}
        image = {"Config": {"Volumes": {"/var/lib/postgresql/data": {}}}}
        mounts, volumes = smoke.image_volume_mounts("db_postgres", service, image)
        self.assertEqual(
            mounts,
            [
                {
                    "type": "volume",
                    "source": "image-db_postgres-0",
                    "target": "/var/lib/postgresql/data",
                }
            ],
        )
        self.assertEqual(volumes, {"image-db_postgres-0": {}})
        self.assertEqual(service["volumes"], [{"target": "/var/lib/postgresql/data/pgdata"}])

    def test_covered_image_volume_needs_no_extra_mount(self):
        service = {"volumes": [{"target": "/data"}]}
        image = {"Config": {"Volumes": {"/data": {}}}}
        self.assertEqual(smoke.image_volume_mounts("redis", service, image), ([], {}))

    def test_invalid_image_volume_target_refuses(self):
        for target in ("/", "relative", "/data/../foreign"):
            with (
                self.subTest(target=target),
                self.assertRaisesRegex(RuntimeError, "target is invalid"),
            ):
                smoke.image_volume_mounts("service", {}, {"Config": {"Volumes": {target: {}}}})

    def test_init_password_respects_pinned_schema(self):
        for _ in range(8):
            password = smoke.initial_password()
            self.assertEqual(len(password), 27)
            self.assertTrue(password.startswith("Aa7"))

    def test_remote_current_context_refuses_before_bound_operation(self):
        resolve = Mock(return_value="ssh://remote-daemon")
        with self.assertRaisesRegex(RuntimeError, "Effective Docker context"):
            smoke.verify_docker_selection({}, resolve)
        resolve.assert_called_once_with()

    def test_docker_selector_overrides_refuse_before_context_lookup(self):
        for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
            with self.subTest(name=name):
                resolve = Mock()
                with self.assertRaisesRegex(RuntimeError, "overrides"):
                    smoke.verify_docker_selection({name: "foreign"}, resolve)
                resolve.assert_not_called()

    def test_verified_endpoint_is_bound_to_every_docker_operation(self):
        endpoint = smoke.verify_docker_selection({}, lambda: smoke.LOCAL_DOCKER)
        for command in (["container", "inspect", "owned"], ["container", "rm", "owned"]):
            self.assertEqual(
                smoke.docker_arguments(command, endpoint),
                ["docker", "--host", smoke.LOCAL_DOCKER, *command],
            )
        with self.assertRaisesRegex(RuntimeError, "unverified"):
            smoke.docker_arguments(["container", "rm", "owned"], "ssh://remote")

    def test_same_archive_count_cannot_replace_license_with_excluded_test(self):
        members = [ZipInfo(name) for name in smoke.EXPECTED_PLUGIN_FILES if name != "LICENSE"]
        members.append(ZipInfo("tests/native_concurrency_case.py"))
        self.assertEqual(len(members), 19)
        with self.assertRaisesRegex(RuntimeError, "reviewed set"):
            smoke.validate_archive_members(members)

    def test_archive_rejects_duplicate_and_directory_members(self):
        valid = [ZipInfo(name) for name in smoke.EXPECTED_PLUGIN_FILES]
        smoke.validate_archive_members(valid)
        for extra in (ZipInfo("LICENSE"), ZipInfo("tools/")):
            with self.subTest(extra=extra.filename), self.assertRaises(RuntimeError):
                smoke.validate_archive_members([*valid, extra])

    def test_native_run_reaches_tool_workflow_without_optional_editor_defaults(self):
        class WorkflowReached(Exception):
            pass

        ctx = SimpleNamespace(
            env={"INIT_PASSWORD": "fixture-password"},
            secret=lambda value: value,
            plugin_python=Path("/owned/plugin/bin/python"),
            plugin_dir=Path("/owned/plugin"),
            track_plugin=Mock(),
            cleanup_callbacks=[],
            org="fixture-org",
            namespace="fixture-namespace",
            agent="fixture-agent",
            token="fixture-token",
            record=Mock(),
            redact=str,
        )
        provider = {
            "id": "clickhouse/loom/loom",
            "name": "loom",
            "author": "clickhouse",
            "type": "builtin",
        }
        declarations = {
            "remember": [
                ("user_id", "form", "string", True),
                ("memory_key", "llm", "string", True),
                ("value", "llm", "string", True),
            ],
            "recall": [
                ("user_id", "form", "string", True),
                ("query", "llm", "string", True),
                ("top_k", "form", "number", False),
            ],
            "search_knowledge": [
                ("user_id", "form", "string", True),
                ("query", "llm", "string", True),
                ("top_k", "form", "number", False),
            ],
        }
        tools = [
            {
                "name": name,
                "parameters": [
                    dict(zip(("name", "form", "type", "required"), fields, strict=True))
                    for fields in parameters
                ],
            }
            for name, parameters in declarations.items()
        ]
        console = Mock()
        console.client.cookies.get.return_value = "fixture-csrf"

        def response(method, path, *payload):
            if path == "workspaces/current/plugin/debugging-key":
                return {"key": "fixture-debug", "host": "127.0.0.1", "port": 17883}
            if path == "workspaces/current/tool-providers":
                return [provider]
            if path.endswith("/tools"):
                return tools
            if method == "POST":
                return {"id": "fixture-app"} if path == "apps" else {}
            raise AssertionError("Unexpected optional editor-default request: " + path)

        console.json.side_effect = response
        child = Mock()
        child.poll.return_value = None
        with (
            patch.object(scenario, "Console", return_value=console),
            patch.object(scenario.subprocess, "Popen", return_value=child),
            patch.object(scenario.threading, "Thread"),
            patch.object(scenario, "workflow", side_effect=WorkflowReached) as workflow,
            self.assertRaises(WorkflowReached),
        ):
            scenario.run(ctx)
        self.assertEqual(workflow.call_count, 1)
        self.assertEqual(
            workflow.call_args.args[-4:],
            ("remember", "user-a", {"memory_key": "seat", "value": scenario.FACT}, "remember"),
        )
        self.assertEqual(ctx.phase, "native-workflow")

    def test_native_graph_keeps_application_user_in_form_configuration(self):
        graph = scenario.build_graph(
            {"id": "discovered-provider", "name": "loom"},
            "recall",
            "application-user",
            {"query": "aisle seats"},
        )
        start = next(node["data"] for node in graph["nodes"] if node["id"] == "start")
        end = next(node["data"] for node in graph["nodes"] if node["id"] == "end")
        self.assertEqual(start, {"type": "start", "title": "Start", "desc": "", "variables": []})
        self.assertEqual(end["type"], "end")
        self.assertEqual(
            end["outputs"],
            [
                {
                    "variable": "result",
                    "value_selector": ["tool", "json"],
                    "value_type": "array[object]",
                }
            ],
        )
        tool = next(node["data"] for node in graph["nodes"] if node["id"] == "tool")
        self.assertEqual(tool["provider_id"], "discovered-provider")
        self.assertEqual(
            tool["tool_configurations"]["user_id"],
            {"type": "constant", "value": "application-user"},
        )
        self.assertNotIn("user_id", tool["tool_parameters"])
        self.assertEqual(tool["tool_parameters"]["query"]["value"], "aisle seats")
        self.assertEqual(tool["tool_configurations"]["top_k"]["value"], 5)

    @staticmethod
    def saved_session():
        return {"pid": 41, "start": 8, "members": {41: (8, 41, 41, 1000)}}

    def stop_mocked(self, saved, snapshot, send):
        smoke.stop_owned_session(
            saved,
            observe=lambda _: dict(snapshot),
            current_birth=lambda pid: snapshot.get(pid, ())[:3],
            send=send,
            pause=Mock(side_effect=AssertionError("Unexpected wait")),
            now=lambda: 0,
            uid=1000,
        )

    def test_reused_session_leader_is_never_signalled(self):
        send = Mock()
        with self.assertRaisesRegex(RuntimeError, "leader generation"):
            self.stop_mocked(self.saved_session(), {41: (99, 41, 41, 1000)}, send)
        send.assert_not_called()

    def test_unrecorded_orphan_is_never_adopted_or_signalled(self):
        send = Mock()
        with self.assertRaisesRegex(RuntimeError, "Orphan member generation"):
            self.stop_mocked(self.saved_session(), {42: (9, 41, 41, 1000)}, send)
        send.assert_not_called()

    def test_exact_live_leader_allows_owned_members_and_saves_their_generations(self):
        saved = self.saved_session()
        snapshot = {41: (8, 41, 41, 1000), 42: (9, 41, 41, 1000)}
        send = Mock(side_effect=lambda pid, _: snapshot.pop(pid))
        self.stop_mocked(saved, snapshot, send)
        self.assertEqual(
            send.call_args_list,
            [unittest.mock.call(41, signal.SIGTERM), unittest.mock.call(42, signal.SIGTERM)],
        )
        self.assertEqual(saved["members"][42], (9, 41, 41, 1000))

    def test_exact_saved_orphan_remains_owned_without_leader(self):
        saved = self.saved_session()
        saved["members"][42] = (9, 41, 41, 1000)
        snapshot = {42: (9, 41, 41, 1000)}
        send = Mock(side_effect=lambda pid, _: snapshot.pop(pid))
        self.stop_mocked(saved, snapshot, send)
        send.assert_called_once_with(42, signal.SIGTERM)

    def test_completed_empty_command_retires_from_persisted_ownership(self):
        runtime = smoke.Runtime.__new__(smoke.Runtime)
        entry = {"child": SimpleNamespace(pid=41), "birth": 8, "members": {41: (8, 41, 41, 1000)}}
        runtime.processes = [entry]
        runtime.persist = Mock()
        with patch.object(smoke, "read_session", return_value={}):
            runtime.retire(entry)
        self.assertEqual(runtime.processes, [])
        runtime.persist.assert_called_once_with()

    def test_completed_command_cannot_retire_over_reused_leader(self):
        runtime = smoke.Runtime.__new__(smoke.Runtime)
        entry = {"child": SimpleNamespace(pid=41), "birth": 8, "members": {41: (8, 41, 41, 1000)}}
        runtime.processes = [entry]
        runtime.persist = Mock()
        with patch.object(smoke, "read_session", return_value={41: (99, 41, 41, 1000)}):
            with self.assertRaisesRegex(RuntimeError, "leader generation"):
                runtime.retire(entry)
        runtime.persist.assert_not_called()

    def test_monitor_failure_arriving_during_join_invalidates_final_success(self):
        runtime = SimpleNamespace(finished=Mock(), monitor_error=None)
        runtime.monitor_thread = Mock()
        runtime.monitor_thread.is_alive.return_value = False
        runtime.monitor_thread.join.side_effect = lambda **_: setattr(
            runtime, "monitor_error", "late reserve failure"
        )
        smoke.stop_monitor(runtime)
        self.assertEqual(smoke.final_failure(None, runtime.monitor_error), "late reserve failure")
        runtime.finished.set.assert_called_once_with()
        runtime.monitor_thread.join.assert_called_once_with(timeout=5)

    def test_live_monitor_after_join_fails_closed(self):
        runtime = SimpleNamespace(finished=Mock(), monitor_error=None, monitor_thread=Mock())
        runtime.monitor_thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, "monitor did not stop"):
            smoke.stop_monitor(runtime)
        self.assertEqual(smoke.final_failure("cleanup failed", None), "cleanup failed")


if __name__ == "__main__":
    unittest.main()
