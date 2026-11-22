"""Qualify the adapters' actual request builders against the serving wire models."""

from examples.software_factory.factory.memory_tools import OPS
from examples.software_factory.factory.memory_tools import request_body as memory_request
from examples.software_factory.factory.retrieval_tools import request_body as code_request


def requests():
    code = {
        "code.symbol": {"name": "qualification"},
        "code.grep": {
            "pattern": "qualification",
            "path_prefix": "pkg/",
            "regex": True,
            "cursor": "",
        },
        "code.search": {"query": "qualification", "path_prefix": "pkg/", "cursor": ""},
        "code.outline": {"path": "pkg/source.py"},
        "code.enclosing": {"locations": ["pkg/source.py:1"]},
    }
    rows = [
        {
            "operation": op,
            "body": code_request(
                op, args, org="qualification", namespace="code", memory_namespaces=memories
            ),
        }
        for memories in ((), ("memory",))
        for op, args in [*code.items(), ("code.symbol", {"symbol_id": "qualification"})]
    ]
    memory = {
        "loom_memory_search": {"query": "qualification"},
        "loom_memory_get": {"memory_key": "qualification"},
        "loom_memory_set": {
            "memory_key": "qualification",
            "value": "qualification",
            "files": [],
            "tags": [],
        },
        "loom_memory_update": {
            "memory_key": "qualification",
            "value": "qualification",
            "if_match_sha256": "a" * 64,
        },
        "loom_memory_for_file": {"path": "pkg/source.py"},
    }
    rows.extend(
        {
            "operation": OPS[name],
            "body": memory_request(
                name,
                args,
                org="qualification",
                namespace="memory",
                agent="factory-pilot",
                user_id="-",
                session_id="-",
                repository_tag="repo:qualification",
            ),
        }
        for name, args in memory.items()
    )
    return rows
