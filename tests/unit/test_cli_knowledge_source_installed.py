"""Installed server/SDK CLI contract; run in the hosted environment with both wheels."""

import argparse

import pytest
from loom import cli_context, source_cli

from server.cli import knowledge_source
from server.identity.api.models_collections import CollectionCreateInput, CollectionListInput
from server.identity.api.models_grants import GrantsCreateInput, GrantsListInput
from server.knowledge.api.models_mcp_connections import (
    MCPBrowseInput,
    MCPConnectInput,
    MCPInspectInput,
    MCPListInput,
    MCPSlotInput,
)
from server.knowledge.api.models_mcp_profiles import MCPProfilesInput
from server.knowledge.api.models_mcp_reads import MCPReadInput
from server.knowledge.api.models_mcp_sources import (
    MCPSourceAddInput,
    MCPSourceInput,
    MCPSourceInspectInput,
    MCPSourceListInput,
    MCPSourcePreviewInput,
    MCPSourceRemoveInput,
    MCPSourceUpdateInput,
)
from server.knowledge.api.models_sources import (
    KnowledgeSourceAddInput,
    KnowledgeSourceInspectInput,
    KnowledgeSourceListInput,
    KnowledgeSourceRemoveInput,
    KnowledgeSourceSyncNowInput,
)

MODELS = {
    "knowledge.connection.mcp_connect": MCPConnectInput,
    "knowledge.connection.mcp_list": MCPListInput,
    "knowledge.connection.mcp_inspect": MCPInspectInput,
    "knowledge.connection.mcp_discover": MCPSlotInput,
    "knowledge.connection.mcp_browse": MCPBrowseInput,
    "knowledge.connection.mcp_disconnect": MCPSlotInput,
    "knowledge.source.mcp_profiles": MCPProfilesInput,
    "knowledge.source.mcp_preview": MCPSourcePreviewInput,
    "knowledge.source.mcp_add": MCPSourceAddInput,
    "knowledge.source.mcp_update": MCPSourceUpdateInput,
    "knowledge.source.mcp_list": MCPSourceListInput,
    "knowledge.source.mcp_inspect": MCPSourceInspectInput,
    "knowledge.source.mcp_pause": MCPSourceInput,
    "knowledge.source.mcp_resume": MCPSourceInput,
    "knowledge.source.mcp_refresh": MCPSourceInput,
    "knowledge.source.mcp_read": MCPReadInput,
    "knowledge.source.mcp_remove": MCPSourceRemoveInput,
    "collection.create": CollectionCreateInput,
    "collection.list": CollectionListInput,
    "grants.create": GrantsCreateInput,
    "grants.list": GrantsListInput,
    "knowledge.source.add": KnowledgeSourceAddInput,
    "knowledge.source.list": KnowledgeSourceListInput,
    "knowledge.source.inspect": KnowledgeSourceInspectInput,
    "knowledge.source.remove": KnowledgeSourceRemoveInput,
    "knowledge.source.sync_now": KnowledgeSourceSyncNowInput,
}
COMMAND_GROUPS = [
    source_cli._COMMANDS,
    source_cli._CONNECTION_COMMANDS,
    source_cli._DESTINATION_COMMANDS,
    source_cli._RECORDING_COMMANDS,
    source_cli._RETAINED_COMMANDS,
]


def leaves(parser, path=()):
    result = {}
    children = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not children:
        result[path] = (
            parser.allow_abbrev,
            {(a.dest, a.required, tuple(a.option_strings)) for a in parser._actions},
        )
    for child in children:
        for name, nested in child.choices.items():
            result.update(leaves(nested, (*path, name)))
    return result


def test_server_and_sdk_entry_points_expose_same_commands_and_input_requirements():
    server = argparse.ArgumentParser()
    knowledge_source.register_knowledge_source_cli(server.add_subparsers(required=True))
    sdk = argparse.ArgumentParser()
    source_cli.register_source_cli(sdk.add_subparsers(required=True))
    assert leaves(server) == leaves(sdk)
    assert knowledge_source._SOURCE_CLI_API == source_cli.SOURCE_CLI_API
    assert source_cli.SOURCE_CLI_API == 2
    assert cli_context.CLI_CONTEXT_API == 1
    assert cli_context.CONTEXT_ENV == "LOOM_SOURCE_SESSION_CONTEXT"


@pytest.mark.parametrize(
    "command", [command for group in COMMAND_GROUPS for command in group.values()]
)
def test_cli_request_surface_matches_actual_api_fields(command):
    model = MODELS[command.op]
    fields = {"org", *command.fields.split()}
    if command.secret:
        fields.add(command.secret)
    assert fields == set(model.model_fields)
    required = {name for name, field in model.model_fields.items() if field.is_required()}
    assert {
        "org",
        *command.required.split(),
        *([command.secret] if command.secret == "credential" else []),
    } == required
