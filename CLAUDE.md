@AGENTS.md

## Claude Code

- After a push, start the `loom-reviewer` subagent in the background with the PR number and head sha.
- A subagent with `isolation: "worktree"` starts from `origin/main`, not from your branch (`worktree.baseRef` is `fresh`): use it for parallel work that starts from `main`, and run subagents that edit your branch in your own worktree, one at a time.
