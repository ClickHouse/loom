"""Rust fast-tier parsing — crate-path names, impl blocks as
skeletons, trait impls as inherits, `use` / `mod` resolution, path calls."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_rust")

from server.code.domain.extract.chunker import chunk_file
from server.code.domain.extract.edges_treesitter import collect_call_names, extract_edges_and_misses
from server.code.domain.extract.lang_rust import (
    crate_scope,
    parse_rust,
    resolve_uses,
    use_candidates,
)
from server.code.domain.extract.name_scope import qualified_name_class

_RS = b"""//! Incremental SSE framing.
use crate::capture::facts::Fact;
use super::openai_responses::{Capture, Kind as K};
use std::collections::HashMap;
mod tests;

/// A decoder.
#[derive(Default)]
pub struct SseDecoder {
    line: Vec<u8>,
}

pub enum EventState { Collecting, Discarding }

pub trait Sink {
    fn push(&mut self, b: &[u8]);
    fn name(&self) -> &str { "sink" }
}

impl SseDecoder {
    /// Push bytes.
    pub fn push(&mut self, bytes: &[u8]) -> bool {
        self.end_line();
        helper(bytes);
        let m: HashMap<u8, u8> = HashMap::new();
        let d = SseDecoder::default();
        println!("{}", m.len());
        Fact::record(d);
        true
    }
    fn end_line(&mut self) {}
}

impl Sink for SseDecoder {
    fn push(&mut self, b: &[u8]) { self.end_line(); }
}

pub fn helper(b: &[u8]) -> usize { b.len() }
pub const LIMIT: usize = 10;
pub type Map = HashMap<String, u8>;
macro_rules! shout { () => {} }
"""


def test_names_are_crate_paths_and_read_as_native() -> None:
    assert crate_scope("ai-gateway/src/capture/sse.rs") == ("ai-gateway", "capture::sse")
    assert crate_scope("ai-gateway/src/lib.rs") == ("ai-gateway", "")
    assert crate_scope("packages/native/src/telemetry/mod.rs") == ("native", "telemetry")
    assert crate_scope("packages/native/build.rs") == ("native", "build")
    pf = parse_rust("ai-gateway/src/capture/sse.rs", _RS)
    s = {x.qualified_name: x for x in pf.symbols}
    m = "ai-gateway::capture::sse"
    assert (
        s[f"{m}::SseDecoder"].kind == "struct"
        and s[f"{m}::SseDecoder"].doc_comment == "/// A decoder."
    )
    assert s[f"{m}::EventState"].kind == "enum"
    assert s[f"{m}::Sink"].kind == "trait"
    assert s[f"{m}::Sink::push"].has_body is False and s[f"{m}::Sink::name"].has_body is True
    assert s[f"{m}::SseDecoder::impl"].kind == "impl"
    assert s[f"{m}::SseDecoder::impl Sink"].kind == "impl"
    inherent_push = next(
        x for x in pf.symbols if x.qualified_name == f"{m}::SseDecoder::push" and x.doc_comment
    )
    assert inherent_push.signature == "(&mut self, bytes: &[u8]) -> bool"
    assert inherent_push.doc_comment == "/// Push bytes."
    assert s[f"{m}::helper"].kind == "function" and s[f"{m}::LIMIT"].kind == "const"
    assert s[f"{m}::Map"].kind == "type" and s[f"{m}::shout"].kind == "macro"
    assert pf.inherits == [(f"{m}::SseDecoder", "Sink")]
    assert qualified_name_class(f"{m}::SseDecoder::push") == "native"
    # both `push` methods share the qualified name and differ by signature
    pushes = [x for x in pf.symbols if x.name == "push" and "SseDecoder" in x.qualified_name]
    assert len(pushes) == 2 and len({x.id for x in pushes}) == 2


def test_impl_blocks_chunk_as_skeletons_with_methods_elided() -> None:
    pf = parse_rust("ai-gateway/src/capture/sse.rs", _RS)
    chunks = chunk_file(pf)
    skel = next(c for c in chunks if c.scope == "ai-gateway::capture::sse::SseDecoder::impl")
    assert skel.kind == "class_skeleton" and "// … body elided" in skel.content
    assert any(c.scope.endswith("::SseDecoder::push") and c.kind == "function" for c in chunks)
    header = next(c for c in chunks if c.kind == "file_header")
    assert "use crate::capture::facts::Fact;" in header.content and "mod tests;" in header.content


def test_use_and_mod_paths_resolve_to_module_files() -> None:
    pf = parse_rust("ai-gateway/src/capture/sse.rs", _RS)
    assert pf.includes == [
        "crate::capture::facts::Fact",
        "super::openai_responses::Capture",
        "super::openai_responses::Kind",
        "std::collections::HashMap",
        "mod tests",
    ]
    assert use_candidates("ai-gateway/src/capture/sse.rs", "crate::capture::facts::Fact")[2:4] == [
        "ai-gateway/src/capture/facts.rs",
        "ai-gateway/src/capture/facts/mod.rs",
    ]
    assert use_candidates("ai-gateway/src/capture/sse.rs", "mod tests") == [
        "ai-gateway/src/capture/sse/tests.rs",
        "ai-gateway/src/capture/sse/tests/mod.rs",
    ]
    assert use_candidates("ai-gateway/src/capture/mod.rs", "mod sse") == [
        "ai-gateway/src/capture/sse.rs",
        "ai-gateway/src/capture/sse/mod.rs",
    ]
    present = {
        "ai-gateway/src/capture/facts.rs",
        "ai-gateway/src/capture/openai_responses.rs",
        "ai-gateway/src/capture/sse/tests.rs",
    }
    assert resolve_uses("ai-gateway/src/capture/sse.rs", pf.includes, present) == [
        "ai-gateway/src/capture/facts.rs",
        "ai-gateway/src/capture/openai_responses.rs",
        "ai-gateway/src/capture/sse/tests.rs",
    ]


def test_calls_bind_through_paths_and_std_is_noise() -> None:
    pf = parse_rust("ai-gateway/src/capture/sse.rs", _RS)
    names = collect_call_names(pf)
    assert {"end_line", "helper", "record"} <= names
    # HashMap::new / m.len() / SseDecoder::default are std's verbs: noise
    assert "new" not in names and "len" not in names and "default" not in names
    table = {
        "record": [("f1", "ai-gateway::capture::facts::Fact::record", "function")],
        "Sink": [("t1", "ai-gateway::capture::sse::Sink", "trait")],
    }
    edges, misses = extract_edges_and_misses(pf, table, None)
    by = {
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name, e.edge_kind, e.confidence)
        for e in edges
    }
    assert ("push", "ai-gateway::capture::sse::SseDecoder::end_line", "calls", 1.0) in by
    assert ("push", "ai-gateway::capture::sse::helper", "calls", 1.0) in by
    assert ("push", "ai-gateway::capture::facts::Fact::record", "calls", 0.6) in by
    assert ("SseDecoder", "ai-gateway::capture::sse::Sink", "inherits", 0.9) in by
    assert misses == []


def test_ingest_resolves_a_use_to_the_file_the_namespace_holds() -> None:
    """The ingest pass reads a Rust file's `use` candidates through the Rust lane,
    not the TypeScript one."""
    import asyncio

    from server.code.app.index import ingest_resolve

    class _Repo:
        async def live_paths_present(self, org: str, ns: str, wanted: list[str]) -> list[str]:
            return [w for w in wanted if w == "src/util.rs"]

    pf = parse_rust("src/main.rs", b"use crate::util::helper;\nfn main() { helper(); }\n")
    asyncio.run(ingest_resolve._resolve_lane_includes(_Repo(), "o", "n", pf))  # type: ignore[arg-type]
    assert pf.includes == ["src/util.rs"]


def test_a_leaf_under_a_prefix_longer_than_a_path_is_dropped() -> None:
    long = "::".join(["a"] * 200)
    pf = parse_rust("src/lib.rs", f"use {long}::{{b, c}};\nuse m::{{d}};\n".encode())
    assert pf.includes == ["m::d"]
