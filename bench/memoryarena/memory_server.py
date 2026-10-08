"""Memory server for the gym with loom plugged in — same three endpoints as
memory/server.py, only the systems that need no third-party memory API key."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import importlib.util
from typing import NamedTuple

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


def _load(name: str):
    # bypass memory_systems/__init__.py, which imports every third-party memory SDK
    path = os.path.join(os.path.dirname(__file__), "memory_systems", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"ms_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


RAGMemorySystem = _load("rag").RAGMemorySystem
LongContextMemorySystem = _load("long_context").LongContextMemorySystem
LoomMemorySystem = _load("loom_memory").LoomMemorySystem


class InitializeRequest(BaseModel):
    user_id: str
    memory_system_name: str


class AddRequest(BaseModel):
    user_id: str
    chunk: str
    memory_system_name: str


class QueryRequest(BaseModel):
    user_id: str
    question: str
    memory_system_name: str


app = FastAPI(title="Memory Agent Server (loom)")


class Entry(NamedTuple):
    name: str
    system: object


SYSTEMS: dict[str, Entry] = {}


def _get(user_id: str, name: str):
    e = SYSTEMS.get(user_id)
    if e is None:
        raise HTTPException(404, "User not initialized")
    if e.name != name:
        raise HTTPException(400, "Mismatched memory_system for user")
    return e.system


@app.post("/memory/initialize")
def initialize(req: InitializeRequest):
    name = req.memory_system_name
    if name in {"bm25", "text-embedding-3-small"}:
        sys_ = RAGMemorySystem(retrieval_method=name)
    elif name == "long_context":
        sys_ = LongContextMemorySystem()
    elif name == "loom-verbatim":
        sys_ = LoomMemorySystem(
            req.user_id, mode="verbatim", bodies="full", max_tokens=2048
        )  # first-slice shape
    elif name == "loom-verbatim-unit":
        sys_ = LoomMemorySystem(
            req.user_id, mode="verbatim"
        )  # whole bodies, one memory per subtask
    elif name == "loom-verbatim-unit-recent":
        sys_ = LoomMemorySystem(
            req.user_id, mode="verbatim", recent_first=1
        )  # server-side recent tier
    elif name == "loom-verbatim-unit-ledger":
        sys_ = LoomMemorySystem(
            req.user_id, mode="verbatim", recent_first=1, ledger=True
        )  # + results ledger
    elif name == "loom-verbatim-unit-recent-min":
        sys_ = LoomMemorySystem(req.user_id, mode="verbatim", recent_first=1, min_body_chars=60)
    elif name == "loom-verbatim-unit-this":
        sys_ = LoomMemorySystem(req.user_id, mode="verbatim", namespace_scope="this")
    elif name == "loom-verbatim-unit-run":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="verbatim",
            namespace_scope="this",
            shared_namespace="ma-verbatim-travel",
        )
    elif name == "loom-extract-run":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="extract",
            namespace_scope="this",
            shared_namespace="ma-extract-data_1_g",
        )
    elif name == "loom-verbatim-unit-shared":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="verbatim",
            namespace_scope="this",
            shared_namespace="ma-verbatim-shared130",
        )
    elif name == "loom-extract-shared":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="extract",
            namespace_scope="this",
            shared_namespace="ma-extract-shared130",
        )
    elif name == "loom-verbatim-unit-names":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="verbatim",
            namespace_scope="this",
            shared_namespace="ma-verbatim-travel",
            name_tags=True,
        )
    elif name == "loom-verbatim-unit-names2":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="verbatim",
            namespace_scope="this",
            shared_namespace="ma-verbatim-shared130",
            name_tags=True,
        )
    elif name == "loom-verbatim-unit-names3":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="verbatim",
            namespace_scope="this",
            shared_namespace="ma-verbatim-shared130",
            name_tags=True,
            body_budget_tokens=10700,
        )
    elif name == "loom-verbatim-unit-ents":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="verbatim",
            namespace_scope="this",
            shared_namespace="ma-verbatim-shared130",
            write_entities=True,
            body_budget_tokens=32000,
        )
    elif name == "loom-verbatim-unit-ents2":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="verbatim",
            namespace_scope="this",
            shared_namespace="ma-verbatim-shared130",
            write_entities=True,
            body_budget_tokens=32000,
        )
    elif name == "loom-verbatim-unit-ents3":
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="verbatim",
            namespace_scope="this",
            shared_namespace="ma-verbatim-shared130",
            write_entities=True,
            body_budget_tokens=32000,
            anchor_first=1,
        )
    elif name == "loom-verbatim-unit-ents2b":
        # the coverage pair's off arm
        sys_ = LoomMemorySystem(
            req.user_id,
            mode="verbatim",
            namespace_scope="this",
            shared_namespace="ma-verbatim-shared131",
            write_entities=True,
            body_budget_tokens=32000,
        )
    elif name == "loom-extract-actor":
        # each trace written under its traveler's user_id
        sys_ = LoomMemorySystem(req.user_id, mode="extract", namespace_scope="this", actor_ids=True)
    elif name == "loom-extract-actor-k25":
        # a fact-sized window
        sys_ = LoomMemorySystem(
            req.user_id, mode="extract", namespace_scope="this", actor_ids=True, top_k=25
        )
    elif name == "loom-extract-this":
        sys_ = LoomMemorySystem(req.user_id, mode="extract", namespace_scope="this")
    elif name == "loom-extract":
        sys_ = LoomMemorySystem(req.user_id, mode="extract")
    else:
        raise HTTPException(400, f"Unsupported memory_system: {name}")
    SYSTEMS[req.user_id] = Entry(name, sys_)
    return {"status": "ok", "user_id": req.user_id, "memory_system_name": name}


@app.post("/memory/add")
def add(req: AddRequest):
    system = _get(req.user_id, req.memory_system_name)
    return {"status": "ok", "user_id": req.user_id, "response": system.add_chunk(req.chunk)}


class FinishRequest(BaseModel):
    user_id: str
    status: str = "completed"
    metadata: dict | None = None


@app.post("/memory/finish")
def finish(req: FinishRequest):
    """Close the episode's loom run with its pass/fail."""
    e = SYSTEMS.get(req.user_id)
    if e is None:
        raise HTTPException(404, "User not initialized")
    if hasattr(e.system, "finish"):
        return e.system.finish(req.status, req.metadata)
    return {"status": "noop"}


@app.post("/memory/wrap_user_prompt")
def wrap(req: QueryRequest):
    system = _get(req.user_id, req.memory_system_name)
    return {"status": "ok", "user_id": req.user_id, "prompt": system.wrap_user_prompt(req.question)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
