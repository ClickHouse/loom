"""The handwritten Go client sends and reads only fields the server's memory models declare."""

import re
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from server.memory.api.models_crud import (
    MemoryDeleteInput,
    MemoryDeleteOutput,
    MemoryGetInput,
    MemoryGetOutput,
    MemorySetInput,
    MemorySetOutput,
)
from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput

ROOT = Path(__file__).resolve().parents[2]
CLIENT = (ROOT / "sdk/go/client.go").read_text(encoding="utf-8")
MEMORY = (ROOT / "sdk/go/memory.go").read_text(encoding="utf-8")
SEARCH_HIT = typing.get_args(MemorySearchOutput.model_fields["results"].annotation)[0]


def json_names(go_struct_body: str) -> set[str]:
    return set(re.findall(r'`json:"(\w+)', go_struct_body))


def named_struct(source: str, name: str) -> str:
    return source.split(f"type {name} struct {{", 1)[1].split("\n}", 1)[0]


def request_struct(method: str) -> str:
    body = MEMORY.split(f") {method}(ctx context.Context", 1)[1]
    return body.split("in := struct {", 1)[1].split("\n\t}{", 1)[0]


def wire_names(model: type[BaseModel]) -> set[str]:
    return {field.alias or name for name, field in model.model_fields.items()}


IDENTITY = json_names(named_struct(CLIENT, "identity"))


def test_the_identity_names_org_namespace_and_agent() -> None:
    assert IDENTITY == {"org", "namespace", "agent"}


@pytest.mark.parametrize(
    ("method", "model"),
    [
        ("Set", MemorySetInput),
        ("Get", MemoryGetInput),
        ("Search", MemorySearchInput),
        ("Delete", MemoryDeleteInput),
    ],
)
def test_requests_send_only_input_fields(method: str, model: type[BaseModel]) -> None:
    sent = json_names(request_struct(method)) | IDENTITY
    assert sent - {"identity"} <= wire_names(model), sent - wire_names(model)


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("SetResult", MemorySetOutput),
        ("Memory", MemoryGetOutput),
        ("SearchResult", MemorySearchOutput),
        ("SearchHit", SEARCH_HIT),
        ("DeleteResult", MemoryDeleteOutput),
    ],
)
def test_results_read_only_output_fields(name: str, model: type[BaseModel]) -> None:
    read = json_names(named_struct(MEMORY, name))
    assert read and read <= wire_names(model), read - wire_names(model)
