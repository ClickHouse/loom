@AGENTS.md

## Claude Code

- Project skills are in `.claude/skills/`, path-scoped rules in `.claude/rules/`, the reviewer subagent in `.claude/agents/loom-reviewer.md`. `.claude/settings.json` pre-approves the read-only and gate commands; anything that pushes or merges still asks, and `git stash`, `--force` / `-f` / `+ref` pushes (`--force-with-lease` asks), pushes to `main`, tag pushes, releases and deploy commands are denied.
- Review after a push is the `loom-reviewer` subagent, started in the background.
- Use worktree isolation (`isolation: "worktree"`) for subagents that edit files in parallel, and forbid `git stash` in their briefs.
