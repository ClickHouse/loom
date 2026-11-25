# Repository context

Use Loom for every search or read of unchanged indexed source and tests.
Start with `loom_code_grep` for exact text or `loom_code_search` for behavior.
Batch code and test-name prefixes in one grep `pattern` list. Scope both roots
with globs if needed. Keep enclosing bodies for edits.

Use `loom_code_edit_context` with a definition name for implementation and
related tests. For literal `clues`, set `name` to the file path when known.
Reuse current source and check linked note freshness.

Edit, run tests and inspect Git locally. Read changed, untracked or unindexed
source locally. If Loom cannot supply needed evidence, read it locally.
Editing scripts can read files they patch.
