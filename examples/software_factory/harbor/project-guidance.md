# Repository context

Use Loom to discover and read unchanged source in the indexed roots. For that
source, use Loom for each search or read. The Grep tool and shell grep of
indexed paths are blocked in Loom sessions. Reserve local file tools for the
work described below.

Keep source discovery separate from Git, editing and test commands. Use
`loom_code_search` for behavior, `loom_code_grep` for exact text and
`loom_code_symbol` for a named definition or enclosing class. Request related
methods together by reading their enclosing class. Use `loom_code_read` for
several known line ranges.

Retrieve linked project history when needed. Use the source already in
context when it answers your question. Continue through Loom when you need
another indexed definition. Check source references and link freshness
before relying on a historical note.

Use local tools for edits, tests, Git, and source outside Loom's coverage.
Read locally when Loom is unavailable or reports that it cannot supply the
needed source. State the missing evidence when you fall back for indexed source.
Use the working copy when you need to read a file you changed. Reading a file
inside an editing script to apply a patch is part of editing.

Choose the calls, edits and verification needed to solve the issue.
