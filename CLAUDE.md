@AGENTS.md

## Claude Code

- Project skills are in `.claude/skills/`, path-scoped rules in `.claude/rules/`, the reviewer subagent in `.claude/agents/loom-reviewer.md`. `.claude/settings.json` pre-approves the read-only and gate commands; anything that pushes, merges or deploys still asks.
- Review after a push is the `loom-reviewer` subagent, started in the background.
- Use worktree isolation (`isolation: "worktree"`) for subagents that edit files in parallel, and forbid `git stash` in their briefs.
