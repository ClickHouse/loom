# Repository context

Use Loom for each search or read of unchanged indexed source, including test
files and module imports. For an edit, start with
`loom_code_edit_context` for its implementation, related tests, and linked
notes. Pass the smallest known definition as `name`, or literal code fragments
as `clues`. With clues, pass the file's path as `name` if known. Check the source
revision and note freshness. Reuse the returned source and test excerpts.
Use `loom_code_symbol` to inspect a definition.

Use `loom_code_search` for behavior and `loom_code_grep` for exact text. Once a
search identifies the edit target, use `loom_code_edit_context` with its
file and symbol to read the implementation and tests together. For specific
additional lines, batch known ranges with `loom_code_read`. To find more
tests, scope grep to their path and pass a list of literals or one regex.

Keep discovery separate from edits, test execution and Git. Use local tools
for these operations and to read working-copy changes, untracked or unindexed
files, and evidence Loom cannot supply. State the missing evidence when you
fall back for indexed source. An editing script may read the file it patches.
