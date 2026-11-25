# Repository context

Use Loom to search and read unchanged indexed code and tests.
For implementation and related tests, use `loom_code_edit_context` with a
definition or literal `clues`. With clues, pass the known file path as `name`.

When finding exact text for a patch, keep the default enclosing bodies in
`loom_code_grep`. Request locations when you need a position. Use
`loom_code_search` for behavior. Batch related literals or known ranges.
Check source revision and note freshness, and reuse returned evidence.

Use local tools to edit, run tests, inspect Git and read working-copy changes,
untracked files or unindexed source. State the missing evidence before reading
unchanged indexed source locally. An editing script may read the file it
patches.
