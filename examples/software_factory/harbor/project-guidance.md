# Repository context

Use Loom for each search or read of unchanged indexed source, including test
files and module imports. For a known edit target, start with
`loom_code_edit_context` for its implementation, related tests, and linked
notes. Name the method for a local change or the class when several methods
interact. If you know the file but need to find the target, pass its path as
`name` and literal code fragments as `clues`. Check the source revision and
note freshness. Reuse the returned
source and test excerpts before looking up more context. Use
`loom_code_symbol` to inspect a definition.

Use `loom_code_search` for behavior and `loom_code_grep` for exact text. Once a
search identifies the edit target, use `loom_code_edit_context` with its
file and symbol to read the implementation and tests together. For specific
additional lines, batch known ranges with `loom_code_read`. To find more
tests, scope grep to their path and pass a list of literals or one regex.

Keep discovery separate from edits, test execution and Git. Use local tools
for these operations and to read working-copy changes, untracked or unindexed
files, and evidence Loom cannot supply. State the missing evidence when you
fall back for indexed source. An editing script may read the file it patches.
