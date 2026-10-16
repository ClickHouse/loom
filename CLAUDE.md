@AGENTS.md

## Claude Code

- After a push, start the `loom-reviewer` subagent in the background with the PR number and head sha.
- Give a subagent that edits files in parallel `isolation: "worktree"`.
