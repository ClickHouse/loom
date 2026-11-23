# Repository context

When a Loom MCP server is configured, retrieve unchanged indexed source through
Loom. If no Loom server is configured, use the available local tools.

For source inside the indexed roots below, do not use Bash grep, rg, sed, cat,
find, or Python source-printing scripts, or local Read/Grep/Glob, to retrieve
that source. Keep source discovery separate from editing, Git and test commands.

- Find behavior or an implementation with `loom_code_search`; read a named
  definition or `path:line` with `loom_code_symbol`.
- Use `loom_code_grep` for exact text, regex matches and references,
  `loom_code_outline` for file or class structure, and `loom_code_callers` for
  callers. Discover deferred tools with ToolSearch.
- Read returned source and linked project lessons before requesting more
  evidence. Follow returned symbol names and callees through Loom when needed.
- Use `loom_search` for project history, decisions and documents. Check source
  references, freshness and code-link state before trusting a saved fact.
- Local reads are allowed for unindexed files, files changed during this task,
  or source Loom reports as unavailable. Explain the missing evidence before
  falling back. Needing another indexed definition is a reason to retrieve it
  through Loom. Do not repeat a successful retrieval through local tools.
- Use local editing tools for changes and Bash for tests, builds and Git.
  Reading a file inside an editing script to apply a change is allowed; do not
  use editing scripts as a separate source-discovery channel.
- Skip retrieval when current context already contains the needed evidence.

Choose the fix and verification yourself. No particular tool-call count,
retrieval sequence or solution is required.
