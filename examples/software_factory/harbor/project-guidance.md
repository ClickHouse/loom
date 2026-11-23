# Repository context

When a Loom MCP server is configured, use its tools for context it can supply.
If no Loom server is configured, use the available local tools.

- For indexed code, use `loom_code_search` for behavior, `loom_code_symbol` for a
  definition or `path:line`, `loom_code_outline` for structure,
  `loom_code_grep` for exact text or references, and `loom_code_callers` for callers.
- Read the returned bodies and linked lessons before requesting more context.
  Follow a returned symbol or callee through `loom_code_symbol` when needed.
  Discover deferred tools with ToolSearch.
- Use `loom_search` for relevant project history, decisions and documents. Check
  source references, freshness and code-link state before trusting a recalled fact.
- Do not repeat a successful Loom retrieval with Bash, Read, Grep or Glob unless
  the returned context is insufficient. Explain what is missing before falling back.
- Use local tools for files outside the index, files edited during this task,
  editing, tests, builds and Git. Fall back when Loom is unavailable or reports
  missing source. Keep source discovery separate from edit and test commands.
- Skip retrieval when the conversation already contains the needed evidence.

Choose the fix and verification yourself. These instructions do not require any
particular tool-call count or solution.
