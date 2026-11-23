"""The server CLI delegates source execution to the versioned SDK entry."""

import argparse
from types import SimpleNamespace

import pytest

from server.cli import knowledge_source


def parser():
    value = argparse.ArgumentParser()
    knowledge_source.register_knowledge_source_cli(value.add_subparsers(required=True))
    return value


def arguments(*action):
    return parser().parse_args(
        [
            "source",
            *action,
            "--server-url",
            "https://loom.invalid",
            "--org",
            "test-org",
            "--session-file",
            "/application/session.json",
        ]
    )


def test_missing_sdk_fails_with_supported_installation_message(monkeypatch, capsys):
    def missing(name):
        assert name == "loom.source_cli"
        raise ModuleNotFoundError("loom")

    monkeypatch.setattr(knowledge_source.importlib, "import_module", missing)
    args = arguments("read", "--request-file", "input.json")
    assert args.func(args) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "matching-release clickhouse-loom" in out.err
    assert "python -m loom.source_cli" in out.err
    assert "server-only wheel" in out.err


def test_incompatible_entry_does_not_dispatch(monkeypatch, capsys):
    entry = SimpleNamespace(SOURCE_CLI_API=0)
    monkeypatch.setattr(knowledge_source.importlib, "import_module", lambda name: entry)
    args = arguments("list")
    assert args.func(args) == 2
    assert "matching-release" in capsys.readouterr().err


def test_matching_entry_receives_exact_parsed_arguments(monkeypatch):
    seen = []

    def execute(args):
        seen.append(args)
        return 7

    entry = SimpleNamespace(SOURCE_CLI_API=2, run_server_entry=execute)
    monkeypatch.setattr(knowledge_source.importlib, "import_module", lambda name: entry)
    args = arguments("connection", "browse", "--request-file", "selection.json")
    assert args.func(args) == 7
    assert seen == [args]
    assert args.source_command == "connection" and args.source_action == "browse"
    assert args.request_file == "selection.json"
    assert args.intent == "automatic"


@pytest.mark.parametrize("action", knowledge_source._ACTIONS)
def test_mcp_source_actions_parse_without_importing_sdk(action, monkeypatch):
    def forbidden(name):
        raise AssertionError("Parser construction must not import SDK runtime")

    monkeypatch.setattr(knowledge_source.importlib, "import_module", forbidden)
    args = arguments(action, "--namespace", "col-destination")
    assert args.source_command == action
    assert args.namespace == "col-destination"
    assert args.intent == "automatic"


@pytest.mark.parametrize(
    "flag", ["--link", "--item-json", "--include", "--token", "--credential-file"]
)
def test_native_document_setup_and_argv_credentials_are_absent(flag):
    with pytest.raises(SystemExit) as caught:
        arguments("add", flag, "value")
    assert caught.value.code == 2


def test_recording_keeps_separate_credential_input():
    args = arguments(
        "recording", "add", "--request-file", "recording.json", "--credential-file", "-"
    )
    assert args.source_command == "recording" and args.source_action == "add"
    assert args.credential_file == "-"


def test_connect_does_not_accept_bearer_as_an_argv_option():
    with pytest.raises(SystemExit) as caught:
        arguments("connection", "connect", "--bearer", "value")
    assert caught.value.code == 2


def test_session_pointer_is_required():
    with pytest.raises(SystemExit) as caught:
        parser().parse_args(
            ["source", "list", "--server-url", "https://loom.invalid", "--org", "test-org"]
        )
    assert caught.value.code == 2


def test_root_explicit_intent_is_forwarded_to_one_source_dispatch(monkeypatch):
    from server.cli._session_context import invocation

    seen = []
    entry = SimpleNamespace(SOURCE_CLI_API=2, run_server_entry=lambda args: seen.append(args) or 7)
    monkeypatch.setattr(knowledge_source.importlib, "import_module", lambda name: entry)
    args = arguments("list")
    with invocation("explicit"):
        assert args.func(args) == 7
    assert seen[0] is not args and seen[0].intent == "explicit"
    assert args.intent == "automatic"
