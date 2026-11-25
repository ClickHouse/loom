# Repository context

Use Loom for every search or read of unchanged indexed source and tests.
Start with `loom_code_grep` for exact text or `loom_code_search` for behavior.
Batch known implementation and test literals in one grep request. Keep the
default enclosing bodies when you need the code for an edit.

Use `loom_code_edit_context` to get a known definition and related tests
together. With literal `clues`, pass the known file path as `name`. Reuse
returned source from the current revision and check linked notes for freshness.

Use local tools to edit, run tests, inspect Git and read working-copy changes,
untracked files or unindexed source. If Loom cannot supply needed evidence,
read it locally. An editing script may read the file it patches.
