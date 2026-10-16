---
paths:
  - "frontend/**"
---

# Dashboard

- Follow the [dashboard design guide](../../docs/development/dashboard-design.md): Click UI components and tokens for every control and style, and the accessibility rules it lists.
- Biome formats and lints JS/TS/JSON/CSS: run `make format` from the repository root, where the pinned Biome reads `biome.json`. `make lint` also runs Biome's accessibility rules over `frontend/src` (the `biome.json` overrides); a suppression names its rule and the reason.
- A worktree's `frontend/node_modules` may be a symlink into another checkout; `npm ci` through it empties that checkout's install. `rm` the link (not the target) before installing (`loom-dev-setup` §6).
- `npm --prefix frontend run test` (vitest + axe), `npm --prefix frontend run build` (`tsc -b` + Vite) and `npm --prefix frontend run budget` before pushing, and the Playwright run CI adds (`npx --prefix frontend playwright install --with-deps chromium` once, then `E2E_PORT=<free port> npm --prefix frontend run e2e`).
