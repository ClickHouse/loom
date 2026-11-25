# Repository context

Use Loom for unchanged indexed source and tests. For an edit, start with
`loom_code_edit_context` for full definitions and any linked tests or notes.
Pass a definition name or literal `clues`. With clues, set `name` to the file
path when known.

Use `loom_code_grep` for exact text and `loom_code_search` for behavior. Keep
default grep bodies for edits. Batch known code and test clues in one `pattern`
list with `path_globs` covering both roots. Use `loom_code_read` for exact ranges.
Reuse source and check note freshness.

Edit, run tests and inspect Git locally. Read changed, untracked or unindexed
source locally. If Loom cannot supply needed evidence, read it locally.
Editing scripts can read files they patch.
