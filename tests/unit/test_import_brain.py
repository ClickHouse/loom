"""import-brain markdown parsers: MEMORY.md -> HOT profile sections,
memory/contexts/*.md -> WARM knowledge docs."""

from __future__ import annotations

import os
import tempfile

from server.cli.import_brain import _brain_context_docs, _brain_sections, _brain_slug


def test_sections_split_by_h2_with_overview() -> None:
    md = "top preamble\n\n## Task Context\nfacts here\n\n## Active Context\nstubs\n"
    secs = dict(_brain_sections(md))
    assert secs["overview"].startswith("top preamble")
    assert "facts here" in secs["task-context"]
    assert "stubs" in secs["active-context"]


def test_h3_does_not_split() -> None:
    md = "## A\nbody\n### sub\nmore\n"
    secs = _brain_sections(md)
    assert len(secs) == 1 and "more" in secs[0][1]


def test_empty_sections_dropped_and_slug() -> None:
    assert _brain_sections("## Empty\n\n## Real\nx") == [("real", "## Real\nx")]
    assert _brain_slug("Worker Fleet v6!!") == "worker-fleet-v6"


def test_context_docs_reads_md_only() -> None:
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "memory", "contexts"))
        cdir = os.path.join(d, "memory", "contexts")
        open(os.path.join(cdir, "a.md"), "w").write("alpha body")
        open(os.path.join(cdir, "b.txt"), "w").write("ignored")
        open(os.path.join(cdir, "empty.md"), "w").write("   ")
        docs = _brain_context_docs(cdir)
        assert docs == [("contexts/a.md", "a", "alpha body")]


def test_missing_contexts_dir_is_empty() -> None:
    assert _brain_context_docs("/nonexistent/xyz") == []
