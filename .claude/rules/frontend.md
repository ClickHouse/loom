---
paths:
  - "frontend/**"
---

# Dashboard

- Biome formats and lints JS/TS/JSON/CSS: run `make format` from the repository root, where the pinned Biome reads `biome.json`.
- A worktree's `frontend/node_modules` may be a symlink into another checkout; `npm ci` through it empties that checkout's install. `rm` the link (not the target) before installing (`loom-dev-setup` §6).
- `npm --prefix frontend run test` (vitest + axe), `npm --prefix frontend run build` (`tsc -b` + Vite) and `npm --prefix frontend run budget` before pushing.
